"""Outfit persistence and the edge between DB rows and the pure engine."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import ColumnElement, delete
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.engine import vocab
from app.engine.config import EngineConfig
from app.engine.generator import closet_stats, explain_empty, generate_outfits
from app.engine.personalize import Personalization
from app.engine.style import StyleProfile
from app.engine.types import ALL_SEASONS, ClosetStats, Context, ItemView
from app.engine.types import Outfit as EngineOutfit
from app.models import Availability, Item, Outfit, Source, User
from app.models import Category as Cat

SUGGESTION_TTL = timedelta(days=1)
SLOT_ORDER = {
    Cat.TOP: 0,
    Cat.DRESS: 0,
    Cat.BOTTOM: 1,
    Cat.OUTERWEAR: 2,
    Cat.SHOES: 3,
    Cat.ACCESSORY: 4,
}


class AlreadySaved(Exception):
    pass


# ----------------------------------------------------------------------- db <-> engine


def to_view(item: Item) -> ItemView | None:
    """Engine view of a confirmed item; None while the item lacks confirmed tags."""
    if (
        item.id is None
        or not item.tags_confirmed
        or item.category is None
        or not item.subtype
        or not item.primary_color
    ):
        return None
    return ItemView(
        id=item.id,
        category=item.category,
        subtype=item.subtype,
        primary_color=item.primary_color,
        secondary_colors=tuple(item.secondary_colors),
        pattern=item.pattern or "solid",
        seasons=frozenset(item.seasons) or ALL_SEASONS,  # untagged = any season
        material=item.material,
        layer_role=item.layer_role,
    )


@dataclass(frozen=True)
class Closet:
    items: dict[int, Item]  # every item the user owns
    views: list[ItemView]  # the confirmed ones, ready for the engine

    @property
    def unconfirmed(self) -> int:
        return len(self.items) - len(self.views)


def load_closet(session: Session, user: User, *, for_suggestions: bool = False) -> Closet:
    """The user's closet as the engine sees it.

    Archived and unavailable items are out of everything: suggestions, counts, statistics and
    candidate analysis. Items in the laundry are temporary: they are skipped when *suggesting*
    (`for_suggestions=True`) but still count in statistics and candidate analysis, so one wash
    day does not change what a new piece "adds" to the closet.
    """
    owned: list[ColumnElement[bool]] = [
        col(Item.user_id) == user.id,
        col(Item.source) == Source.OWNED,
        col(Item.archived_at).is_(None),
        col(Item.availability) != Availability.UNAVAILABLE,
    ]
    if for_suggestions:
        owned.append(col(Item.availability) == Availability.AVAILABLE)
    rows = list(session.exec(select(Item).where(*owned)))
    items = {r.id: r for r in rows if r.id is not None}
    views = [v for r in rows if (v := to_view(r)) is not None]
    return Closet(items, views)


def parse_context(season: str | None) -> Context:
    """Form values to a Context. Unknown values are ignored rather than rejected."""
    return Context(season=season if season in vocab.SEASONS else None)


def parse_seen(values: list[str]) -> set[tuple[int, ...]]:
    """`seen` form values look like "3-5-9" (sorted base item ids)."""
    seen: set[tuple[int, ...]] = set()
    for raw in values:
        try:
            ids = tuple(sorted(int(part) for part in raw.split("-") if part))
        except ValueError:
            continue
        if ids:
            seen.add(ids)
    return seen


def base_key(outfit: Outfit) -> str:
    base = (outfit.breakdown or {}).get("base_ids") or outfit.item_ids
    return "-".join(str(i) for i in sorted(base))


# ------------------------------------------------------------------------ generation


@dataclass(frozen=True)
class Suggestions:
    outfits: list[Outfit]
    problems: list[str]  # why nothing could be suggested (empty when outfits exist)
    exhausted: bool = False  # everything possible has been shown already


def _breakdown(outfit: EngineOutfit) -> dict[str, Any]:
    return {
        "rules": [
            {"rule": b.rule, "value": round(b.value, 4), "reason": b.reason}
            for b in outfit.breakdown
        ],
        "reasons": list(outfit.reasons),
        "base_ids": sorted(outfit.base_ids),
        "adjusted_score": (
            None if outfit.adjusted_score is None else round(outfit.adjusted_score, 4)
        ),
        "adjustment_notes": list(outfit.adjustment_notes),
        "adjusted_by": list(outfit.adjusted_by),
    }


def suggest(
    session: Session,
    user: User,
    config: EngineConfig,
    context: Context,
    limit: int = 3,
    seen: set[tuple[int, ...]] | None = None,
    rng_seed: int | None = None,
    personalization: Personalization | None = None,
    style: StyleProfile | None = None,
) -> Suggestions:
    """Generate outfits for the user's confirmed closet and store them as suggestions."""
    closet = load_closet(session, user, for_suggestions=True)
    found = generate_outfits(
        closet.views,
        context,
        config,
        limit,
        rng_seed=rng_seed,
        exclude=seen or (),
        personalization=personalization,
        style=style,
    )
    if not found:
        if seen:
            return Suggestions([], [], exhausted=True)
        return Suggestions([], explain_empty(closet.views, config, context))
    _prune_old_suggestions(session, user)
    rows = [_store(session, user, outfit, context) for outfit in found]
    session.commit()
    for row in rows:
        session.refresh(row)
    return Suggestions(rows, [])


def _prune_old_suggestions(session: Session, user: User) -> None:
    cutoff = datetime.now(UTC) - SUGGESTION_TTL
    session.execute(
        delete(Outfit).where(
            col(Outfit.user_id) == user.id,
            col(Outfit.favorite).is_(False),
            col(Outfit.created_at) < cutoff,
        )
    )


def _store(session: Session, user: User, outfit: EngineOutfit, context: Context) -> Outfit:
    """Insert, or refresh the score and explanation of the row for the same pieces."""
    ids = sorted(outfit.item_ids)
    values = {
        "score": outfit.score,
        "context": {
            "season": context.season,
            "temperature_c": context.temperature_c,
            "rain": context.rain,
        },
        "breakdown": _breakdown(outfit),
    }
    session.execute(
        insert(Outfit)
        .values(user_id=user.id, item_ids=ids, **values)
        .on_conflict_do_update(constraint="uq_outfits_user_items", set_=values)
    )
    row = session.exec(
        select(Outfit).where(Outfit.user_id == user.id, col(Outfit.item_ids) == ids)
    ).one()
    return row


# --------------------------------------------------------------------- saved outfits


def get_owned_outfit(session: Session, user: User, outfit_id: int) -> Outfit:
    """Missing and foreign outfits are both 404, like items."""
    outfit = session.exec(
        select(Outfit).where(Outfit.id == outfit_id, Outfit.user_id == user.id)
    ).first()
    if outfit is None:
        raise HTTPException(status_code=404, detail="Not found")
    return outfit


def save_outfit(session: Session, outfit: Outfit) -> Outfit:
    if outfit.favorite:
        raise AlreadySaved
    outfit.favorite = True
    session.add(outfit)
    session.commit()
    session.refresh(outfit)
    return outfit


def delete_outfit(session: Session, outfit: Outfit) -> None:
    session.delete(outfit)
    session.commit()


def list_saved(session: Session, user: User, occasion: str | None = None) -> list[Outfit]:
    conditions: list[ColumnElement[bool]] = [
        col(Outfit.user_id) == user.id,
        col(Outfit.favorite).is_(True),
    ]
    if occasion in vocab.OCCASIONS:
        conditions.append(col(Outfit.occasions).contains([occasion]))
    return list(
        session.exec(
            select(Outfit)
            .where(*conditions)
            .order_by(col(Outfit.created_at).desc(), col(Outfit.id).desc())
        )
    )


def set_occasions(session: Session, outfit: Outfit, occasions: list[str]) -> Outfit:
    """Occasion tags on a saved outfit (unknown values are dropped, order is the vocabulary's)."""
    wanted = set(occasions)
    outfit.occasions = [o for o in vocab.OCCASIONS if o in wanted]
    session.add(outfit)
    session.commit()
    session.refresh(outfit)
    return outfit


def delete_outfits_with_item(session: Session, user_id: int, item_id: int) -> None:
    """An outfit cannot outlive its pieces. Called when an item is deleted (no commit)."""
    session.execute(
        delete(Outfit).where(
            col(Outfit.user_id) == user_id, col(Outfit.item_ids).contains([item_id])
        )
    )


# -------------------------------------------------------------------------- display


@dataclass(frozen=True)
class OutfitCard:
    outfit: Outfit
    items: list[Item]  # slot order
    reasons: list[str]
    score_percent: int  # the base score, always shown
    key: str
    adjusted_percent: int | None = None  # set when the user's feedback moved the ranking
    adjustment_notes: list[str] | None = None
    rating: int = 0  # the user's own thumb: +1, -1 or 0
    unavailable: list[Item] | None = None  # pieces in the laundry, unavailable or archived
    adjusted_by: list[str] | None = None  # "feedback" and/or "style"


def _adjusted(outfit: Outfit) -> tuple[int | None, list[str] | None, list[str] | None]:
    """The stored adjustment of a fresh suggestion. Saved outfits show only the base score: their
    stored adjustment may predate the user's latest feedback or a personalization reset."""
    info = outfit.breakdown or {}
    value = info.get("adjusted_score")
    if value is None or outfit.favorite:
        return None, None, None
    return (
        round(float(value) * 100),
        list(info.get("adjustment_notes", [])),
        list(info.get("adjusted_by", ["feedback"])),  # rows from before references: feedback
    )


def cards_for(
    session: Session,
    user: User,
    outfits: list[Outfit],
    ratings: dict[int, int] | None = None,
) -> list[OutfitCard]:
    """Attach the user's items to outfits, dropping any whose items no longer exist."""
    wanted = {i for o in outfits for i in o.item_ids}
    rows = {
        r.id: r
        for r in session.exec(
            select(Item).where(
                Item.user_id == user.id,
                col(Item.source) == Source.OWNED,
                col(Item.id).in_(wanted),
            )
        )
        if r.id is not None
    }
    cards: list[OutfitCard] = []
    for outfit in outfits:
        if not all(i in rows for i in outfit.item_ids):
            continue
        items = sorted(
            (rows[i] for i in outfit.item_ids),
            key=lambda r: (SLOT_ORDER.get(r.category or Cat.ACCESSORY, 9), r.id or 0),
        )
        reasons = list((outfit.breakdown or {}).get("reasons", []))
        adjusted, notes, by = _adjusted(outfit)
        away = [
            i
            for i in items
            if i.archived_at is not None or i.availability != Availability.AVAILABLE
        ]
        cards.append(
            OutfitCard(
                outfit,
                items,
                reasons,
                round(outfit.score * 100),
                base_key(outfit),
                adjusted,
                notes,
                (ratings or {}).get(outfit.id or 0, 0),
                away or None,
                by,
            )
        )
    return cards


# ---------------------------------------------------------------------------- stats


@dataclass(frozen=True)
class StatsView:
    total: int
    ranked: list[tuple[Item, int]]  # most versatile first
    unconfirmed: int
    problems: list[str]


def closet_stats_view(session: Session, user: User, config: EngineConfig) -> StatsView:
    closet = load_closet(session, user)
    stats: ClosetStats = closet_stats(closet.views, config)
    ranked = sorted(
        ((closet.items[i], n) for i, n in stats.per_item.items() if i in closet.items),
        key=lambda pair: (-pair[1], pair[0].id or 0),
    )
    problems = [] if stats.total else explain_empty(closet.views, config)
    return StatsView(stats.total, ranked, closet.unconfirmed, problems)
