"""Ratings, the personalization derived from them, and resetting it."""

import re
from collections.abc import Callable

import httpx
from sqlmodel import Session, select

from app.engine.config import EngineConfig
from app.engine.personalize import PersonalizationConfig
from app.engine.types import Context
from app.models import Category, Outfit, OutfitFeedback, User
from app.services import feedback as svc
from app.services.outfits import suggest
from app.services.storage import LocalDiskStorage
from tests.conftest import post_form
from tests.test_outfit_routes import card_ids, closet, piece

CONFIG = EngineConfig()


def shown(rows: list[Outfit]) -> list[tuple[list[int], float, float | None]]:
    return [(o.item_ids, o.score, (o.breakdown or {}).get("adjusted_score")) for o in rows]


def run(session: Session, user: User, limit: int = 12) -> list[Outfit]:
    personalization = svc.personalization_for(session, user, CONFIG)
    result = suggest(
        session, user, CONFIG, Context(), limit, rng_seed=3, personalization=personalization
    )
    return result.outfits


def test_a_dislike_lowers_and_a_like_raises_the_outfits_adjusted_score(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    first = run(session, user_a)
    target = first[-1]  # the last one stays in the list however it is re-ranked
    base = target.score
    svc.record_feedback(session, user_a, target, "down", "colors clash")
    after = {tuple(o.item_ids): o for o in run(session, user_a, 200)}
    disliked = after[tuple(target.item_ids)]
    assert disliked.score == base  # the base score is never rewritten
    assert (disliked.breakdown or {})["adjusted_score"] < base
    assert "Contains a pairing you disliked" in disliked.breakdown["adjustment_notes"]  # type: ignore[index]

    svc.record_feedback(session, user_a, target, "up", None)  # change of mind: latest wins
    assert [f.rating for f in svc.list_feedback(session, user_a)] == [1]

    # red with blue jeans is only complementary (below 1.0), so a like has room to raise it
    piece(session, storage, user_a, Category.TOP, "t-shirt", "red", "Red tee")
    room = next(o for o in run(session, user_a, 200) if o.score < 0.99)
    svc.record_feedback(session, user_a, room, "up", None)
    liked = {tuple(o.item_ids): o for o in run(session, user_a, 200)}[tuple(room.item_ids)]
    assert (liked.breakdown or {})["adjusted_score"] > room.score
    assert "Contains a pairing you liked" in liked.breakdown["adjustment_notes"]  # type: ignore[index]


def test_reset_restores_the_default_output_exactly(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    baseline = shown(run(session, user_a))
    assert all(adjusted is None for *_, adjusted in baseline)
    for outfit in run(session, user_a)[:4]:
        svc.record_feedback(session, user_a, outfit, "down", "colors clash")
    assert shown(run(session, user_a)) != baseline
    assert svc.reset_personalization(session, user_a) == 4
    assert shown(run(session, user_a)) == baseline
    assert not svc.summarize(session, user_a, CONFIG).active


def test_adjustments_are_capped_however_much_feedback_there_is(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    cfg = PersonalizationConfig()
    for _ in range(3):  # rate everything it can show, down, with a reason
        for outfit in run(session, user_a, limit=30):
            svc.record_feedback(session, user_a, outfit, "down", "colors clash")
    p = svc.personalization_for(session, user_a, CONFIG)
    assert p.active and p.samples >= 3
    assert all(cfg.pair_min <= v <= cfg.pair_max for v in p.pair_adjustments.values())
    for rule, ratio in p.weight_ratios.items():
        assert 1 - cfg.max_weight_change <= ratio <= 1 + cfg.max_weight_change, rule
    for outfit in run(session, user_a, limit=30):
        adjusted = (outfit.breakdown or {}).get("adjusted_score")
        if adjusted is not None:
            assert outfit.score + cfg.outfit_adjustment_min - 0.5 <= adjusted <= 1.0


def test_unknown_reasons_are_dropped_and_ups_carry_no_reason(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    first, second = run(session, user_a)[:2]
    row = svc.record_feedback(session, user_a, first, "down", "because I said so")
    assert row is not None and row.reason is None and row.rating == -1
    row = svc.record_feedback(session, user_a, second, "up", "colors clash")
    assert row is not None and row.reason is None and row.rating == 1
    assert row.rules and "color" in row.rules  # the rule values at the time are kept


def test_clear_removes_only_that_rating(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    first, second = run(session, user_a)[:2]
    svc.record_feedback(session, user_a, first, "up", None)
    svc.record_feedback(session, user_a, second, "down", None)
    assert svc.record_feedback(session, user_a, first, "clear", None) is None
    assert [f.rating for f in svc.list_feedback(session, user_a)] == [-1]


# ------------------------------------------------------------------------------ routes


async def test_thumbs_flow_through_the_routes(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    page = await post_form(client, "/outfits/generate", {})
    first, *_ = card_ids(page.text)
    assert "Like" in page.text and "Dislike" in page.text and "adjusted" not in page.text.lower()

    rated = await post_form(
        client, f"/outfits/{first}/feedback", {"rating": "down", "reason": "wrong for weather"}
    )
    assert rated.status_code in (200, 303)
    stored = session.exec(select(OutfitFeedback)).one()
    assert (stored.rating, stored.reason) == (-1, "wrong for weather")

    bad = await post_form(client, f"/outfits/{first}/feedback", {"rating": "sideways"})
    assert bad.status_code == 422
    undone = await post_form(client, f"/outfits/{first}/feedback", {"rating": "clear"})
    assert undone.status_code in (200, 303)
    assert session.exec(select(OutfitFeedback)).all() == []


async def test_card_shows_both_the_base_score_and_the_adjustment(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    for outfit in run(session, user_a, limit=12):
        svc.record_feedback(session, user_a, outfit, "down", "colors clash")
    html = (await post_form(client, "/outfits/generate", {})).text
    assert re.search(r"Score \d+%", html)
    assert re.search(r"Adjusted by your feedback: \d+%", html)
    assert (
        "Weighted by what you liked and disliked before" in html or "pairing you disliked" in html
    )


async def test_feedback_on_someone_elses_outfit_is_a_404_and_ratings_are_private(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    closet(session, storage, user_a)
    mine = run(session, user_a)[0]
    as_user(user_b)
    response = await post_form(client, f"/outfits/{mine.id}/feedback", {"rating": "up"})
    assert response.status_code == 404
    assert session.exec(select(OutfitFeedback)).all() == []
    assert svc.personalization_for(session, user_b, CONFIG).samples == 0


async def test_reset_button_deletes_ratings_and_says_so(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    for outfit in run(session, user_a)[:3]:
        svc.record_feedback(session, user_a, outfit, "up", None)
    page = (await client.get("/settings/preferences")).text
    assert "3 ratings: 3 liked, 0 disliked" in page and "Reset personalization" in page
    done = await post_form(client, "/settings/personalization/reset")
    assert done.status_code == 303
    after = (await client.get("/settings/preferences?ok=reset")).text
    assert "0 ratings" in after and "back to the defaults" in after
    assert session.exec(select(OutfitFeedback)).all() == []
