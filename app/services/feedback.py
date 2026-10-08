"""Thumbs up/down on suggested outfits and the personalization derived from them.

Only the ratings are stored. The adjusted scoring is computed from them on every request
(`derive` is deterministic), so deleting the ratings restores the default ranking exactly.
"""

from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.engine.config import EngineConfig
from app.engine.personalize import (
    REASONS,
    FeedbackSample,
    Personalization,
    PersonalizationConfig,
    derive,
)
from app.models import Outfit, OutfitFeedback, User

RATINGS = {"up": 1, "down": -1}


def _samples(rows: list[OutfitFeedback]) -> list[FeedbackSample]:
    return [FeedbackSample(tuple(r.item_ids), r.rating, r.reason, r.rules or {}) for r in rows]


def list_feedback(session: Session, user: User) -> list[OutfitFeedback]:
    return list(
        session.exec(
            select(OutfitFeedback)
            .where(OutfitFeedback.user_id == user.id)
            .order_by(col(OutfitFeedback.id))
        )
    )


def personalization_for(session: Session, user: User, config: EngineConfig) -> Personalization:
    return derive(config.scoring, _samples(list_feedback(session, user)), PersonalizationConfig())


def rule_snapshot(outfit: Outfit) -> dict[str, float]:
    return {
        r["rule"]: float(r["value"])
        for r in (outfit.breakdown or {}).get("rules", [])
        if "rule" in r and "value" in r
    }


def record_feedback(
    session: Session, user: User, outfit: Outfit, rating: str, reason: str | None
) -> OutfitFeedback | None:
    """Set, change or (rating "clear") remove this user's rating of the outfit."""
    if rating == "clear":
        session.execute(
            delete(OutfitFeedback).where(
                col(OutfitFeedback.user_id) == user.id,
                col(OutfitFeedback.item_ids) == sorted(outfit.item_ids),
            )
        )
        session.commit()
        return None
    value = RATINGS[rating]
    kept_reason = reason if value < 0 and reason in REASONS else None
    values = {
        "user_id": user.id,
        "item_ids": sorted(outfit.item_ids),
        "rating": value,
        "reason": kept_reason,
        "rules": rule_snapshot(outfit),
    }
    session.execute(
        insert(OutfitFeedback)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_feedback_user_items",
            set_={k: values[k] for k in ("rating", "reason", "rules")},
        )
    )
    session.commit()
    return get_feedback(session, user, outfit)


def get_feedback(session: Session, user: User, outfit: Outfit) -> OutfitFeedback | None:
    return session.exec(
        select(OutfitFeedback).where(
            OutfitFeedback.user_id == user.id,
            col(OutfitFeedback.item_ids) == sorted(outfit.item_ids),
        )
    ).first()


def ratings_for(session: Session, user: User, outfits: list[Outfit]) -> dict[int, int]:
    """Outfit id to the user's rating (+1 / -1) for the outfits that have one."""
    by_ids = {tuple(sorted(f.item_ids)): f.rating for f in list_feedback(session, user)}
    return {
        o.id: by_ids[tuple(sorted(o.item_ids))]
        for o in outfits
        if o.id is not None and tuple(sorted(o.item_ids)) in by_ids
    }


def reset_personalization(session: Session, user: User) -> int:
    result = session.execute(delete(OutfitFeedback).where(col(OutfitFeedback.user_id) == user.id))
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class PersonalizationSummary:
    ratings: int
    likes: int
    dislikes: int
    active: bool
    weights: list[tuple[str, float, float]]  # rule, default weight, personalized weight
    pairs_up: int
    pairs_down: int


def summarize(session: Session, user: User, config: EngineConfig) -> PersonalizationSummary:
    rows = list_feedback(session, user)
    p = derive(config.scoring, _samples(rows), PersonalizationConfig())
    base = config.scoring.weights
    changed = [
        (rule, base[rule], p.scoring.weights[rule])
        for rule in sorted(p.weight_ratios)
        if abs(p.scoring.weights[rule] - base[rule]) > 1e-6
    ]
    return PersonalizationSummary(
        ratings=len(rows),
        likes=sum(r.rating > 0 for r in rows),
        dislikes=sum(r.rating < 0 for r in rows),
        active=p.active,
        weights=changed,
        pairs_up=sum(v > 0 for v in p.pair_adjustments.values()),
        pairs_down=sum(v < 0 for v in p.pair_adjustments.values()),
    )
