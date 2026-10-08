"""The weekly planner and its warnings."""

from collections.abc import Callable
from datetime import date, timedelta

import httpx
from sqlmodel import Session, col, select

from app.engine.config import EngineConfig
from app.engine.types import Context
from app.engine.vocab import Category
from app.engine.weather import Conditions
from app.models import Availability, Item, Outfit, PlannedOutfit, User
from app.services import planner as svc
from app.services.outfits import save_outfit, suggest
from app.services.storage import LocalDiskStorage
from app.services.weather import FakeWeatherProvider, Forecast, load_forecast
from tests.conftest import post_form
from tests.test_outfit_routes import closet, piece

CONFIG = EngineConfig()
TODAY = date.today()
MONDAY = svc.week_start(TODAY)


def saved_outfits(session: Session, user: User, count: int = 3) -> list[Outfit]:
    found = suggest(session, user, CONFIG, Context(), 12, rng_seed=1).outfits
    chosen = found[:count]
    for outfit in chosen:
        save_outfit(session, outfit)
    return chosen


def kinds(session: Session, outfit: Outfit) -> set[Category]:
    rows = session.exec(select(Item).where(col(Item.id).in_(outfit.item_ids))).all()
    return {r.category for r in rows if r.category}


def plan_for(
    session: Session, user: User, forecast: Forecast | None = None
) -> list[svc.PlannedDay]:
    return svc.plan_week(session, user, MONDAY, forecast or Forecast([]), CONFIG)


def test_week_helpers() -> None:
    assert svc.week_start(date(2026, 10, 7)) == date(2026, 10, 5)  # a Monday
    assert svc.week_start(date(2026, 10, 11)) == date(2026, 10, 5)  # the Sunday before next Monday
    assert svc.parse_day("2026-10-07") == date(2026, 10, 7)
    assert svc.parse_day("nope") is None and svc.parse_day(None, TODAY) == TODAY


def test_assign_replaces_and_clear_removes(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    first, second, *_ = saved_outfits(session, user_a)
    svc.assign(session, user_a, MONDAY, first)
    svc.assign(session, user_a, MONDAY, second)
    rows = session.exec(select(PlannedOutfit)).all()
    assert len(rows) == 1 and rows[0].outfit_id == second.id
    svc.clear(session, user_a, MONDAY)
    assert session.exec(select(PlannedOutfit)).all() == []


def test_same_piece_on_consecutive_days_warns_on_the_later_day(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    svc.assign(session, user_a, MONDAY, outfit)
    svc.assign(session, user_a, MONDAY + timedelta(days=1), outfit)
    week = plan_for(session, user_a)
    assert week[0].warnings == []
    assert any("also worn the day before" in w for w in week[1].warnings)


def test_a_gap_day_resets_the_consecutive_warning(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    svc.assign(session, user_a, MONDAY, outfit)
    svc.assign(session, user_a, MONDAY + timedelta(days=2), outfit)
    assert all(not d.warnings for d in plan_for(session, user_a))


def test_the_day_before_the_week_counts_for_monday(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    svc.assign(session, user_a, MONDAY - timedelta(days=1), outfit)  # last Sunday
    svc.assign(session, user_a, MONDAY, outfit)
    assert any("day before" in w for w in plan_for(session, user_a)[0].warnings)


def test_a_cold_day_warns_when_the_outfit_has_no_layer(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    outfits = saved_outfits(session, user_a, 12)
    bare = next(o for o in outfits if Category.OUTERWEAR not in kinds(session, o))
    layered = next(o for o in outfits if Category.OUTERWEAR in kinds(session, o))
    cold = FakeWeatherProvider(conditions=Conditions(2.0, 0.0), start=MONDAY)
    forecast = load_forecast(cold, 59.91, 10.75)
    svc.assign(session, user_a, TODAY, bare)
    svc.assign(session, user_a, TODAY + timedelta(days=1), layered)
    week = {d.day: d for d in svc.plan_week(session, user_a, MONDAY, forecast, CONFIG)}
    assert any("layer on top" in w and "forecast" in w for w in week[TODAY].warnings)
    tomorrow = week.get(TODAY + timedelta(days=1))
    if tomorrow is not None:
        assert not any("layer on top" in w for w in tomorrow.warnings)


def test_rain_warns_about_open_shoes(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    piece(session, storage, user_a, Category.SHOES, "sandals", "tan", "Sandals")
    outfits = saved_outfits(session, user_a, 40)
    sandals = next(
        o
        for o in outfits
        if any(
            i.subtype == "sandals"
            for i in session.exec(select(Item).where(col(Item.id).in_(o.item_ids)))
        )
    )
    wet = FakeWeatherProvider(conditions=Conditions(20.0, 17.0, 90.0, 5.0), start=MONDAY)
    svc.assign(session, user_a, TODAY, sandals)
    week = {
        d.day: d
        for d in svc.plan_week(session, user_a, MONDAY, load_forecast(wet, 59.91, 10.75), CONFIG)
    }
    assert any("rain" in w.lower() for w in week[TODAY].warnings)


def test_no_forecast_means_no_weather_warning(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    svc.assign(session, user_a, TODAY, outfit)
    day = {d.day: d for d in plan_for(session, user_a)}[TODAY]
    assert day.warnings == []


def test_pieces_in_the_laundry_or_archived_are_flagged(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    svc.assign(session, user_a, TODAY, outfit)
    piece = session.get(Item, outfit.item_ids[0])
    assert piece is not None
    piece.availability = Availability.LAUNDRY
    session.add(piece)
    session.flush()
    assert any(
        "laundry" in w for w in {d.day: d for d in plan_for(session, user_a)}[TODAY].warnings
    )
    piece.availability = Availability.AVAILABLE
    piece.archived_at = piece.created_at
    session.add(piece)
    session.flush()
    assert any(
        "Archived" in w for w in {d.day: d for d in plan_for(session, user_a)}[TODAY].warnings
    )


# ------------------------------------------------------------------------------- routes


async def test_planner_page_and_assign_clear_routes(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    empty = (await client.get("/planner")).text
    assert "No saved outfits yet" in empty
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    page = (await client.get("/planner")).text
    assert "Weekly planner" in page and page.count("Nothing planned.") == 7
    assert 'for="outfit-' in page  # every select has a label

    day = TODAY.isoformat()
    done = await post_form(client, "/planner/assign", {"day": day, "outfit_id": outfit.id})
    assert done.status_code == 303 and done.headers["location"] == f"/planner?start={MONDAY}"
    assert (await client.get("/planner")).text.count("Nothing planned.") == 6
    cleared = await post_form(client, "/planner/clear", {"day": day})
    assert cleared.status_code == 303
    assert session.exec(select(PlannedOutfit)).all() == []


async def test_navigation_between_weeks(client: httpx.AsyncClient) -> None:
    html = (await client.get("/planner?start=2026-10-14")).text
    assert "Week of 12 October 2026" in html  # snapped to the Monday
    assert "start=2026-10-05" in html and "start=2026-10-19" in html
    assert "Week of" in (await client.get("/planner?start=garbage")).text


async def test_only_saved_own_outfits_can_be_planned(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    closet(session, storage, user_a)
    unsaved = suggest(session, user_a, CONFIG, Context(), rng_seed=1).outfits[0]
    await post_form(client, "/planner/assign", {"day": TODAY.isoformat(), "outfit_id": unsaved.id})
    assert session.exec(select(PlannedOutfit)).all() == []  # suggestions are not planned
    (saved,) = saved_outfits(session, user_a, 1)
    as_user(user_b)
    stolen = await post_form(
        client, "/planner/assign", {"day": TODAY.isoformat(), "outfit_id": saved.id}
    )
    assert stolen.status_code == 404
    assert session.exec(select(PlannedOutfit)).all() == []


async def test_planner_page_shows_warnings(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    (outfit,) = saved_outfits(session, user_a, 1)
    svc.assign(session, user_a, MONDAY, outfit)
    svc.assign(session, user_a, MONDAY + timedelta(days=1), outfit)
    assert "also worn the day before" in (await client.get("/planner")).text
