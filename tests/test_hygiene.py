"""Laundry, unavailable and archived items; duplicate warnings.

The documented choice (docs/outfit-engine.md): archived and unavailable items are out of
everything. Items in the laundry are skipped only when *suggesting* outfits, and still count in
statistics and candidate analysis, so a wash day does not change what a new piece adds.
"""

from collections.abc import Callable

import httpx
from sqlmodel import Session

from app.engine.config import EngineConfig
from app.engine.types import Context
from app.models import Availability, Category, Item, User
from app.schemas import ItemFilters
from app.services.analysis import AnalysisConfig
from app.services.items import (
    archive_item,
    duplicate_warning,
    list_items,
    restore_item,
    set_availability,
)
from app.services.outfits import closet_stats_view, load_closet, save_outfit, suggest
from app.services.storage import LocalDiskStorage
from tests.conftest import post_form
from tests.test_closet_routes import add_item
from tests.test_outfit_routes import closet

CONFIG = EngineConfig()


def shoes(items: list[Item]) -> list[Item]:
    return [i for i in items if i.category == Category.SHOES]


def test_laundry_is_skipped_for_suggestions_but_still_counted(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    baseline = closet_stats_view(session, user_a, CONFIG).total
    assert baseline > 0 and suggest(session, user_a, CONFIG, Context()).outfits

    for pair in shoes(items):
        set_availability(session, pair, Availability.LAUNDRY)
    result = suggest(session, user_a, CONFIG, Context())
    assert result.outfits == [] and result.problems  # nothing to wear while the shoes are washed
    assert closet_stats_view(session, user_a, CONFIG).total == baseline  # but counts are stable
    assert len(load_closet(session, user_a).views) == len(items)  # candidate analysis sees them
    assert len(load_closet(session, user_a, for_suggestions=True).views) == len(items) - 2


def test_unavailable_items_are_out_of_everything(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    for pair in shoes(items):
        set_availability(session, pair, Availability.UNAVAILABLE)
    assert closet_stats_view(session, user_a, CONFIG).total == 0
    assert suggest(session, user_a, CONFIG, Context()).outfits == []
    assert len(load_closet(session, user_a).views) == len(items) - 2

    for pair in shoes(items):
        set_availability(session, pair, Availability.AVAILABLE)  # restored
    assert closet_stats_view(session, user_a, CONFIG).total > 0
    assert suggest(session, user_a, CONFIG, Context()).outfits


def test_archived_items_are_out_of_everything_until_restored(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    baseline = closet_stats_view(session, user_a, CONFIG).total
    target = shoes(items)[0]
    for pair in shoes(items):
        archive_item(session, pair)
    assert closet_stats_view(session, user_a, CONFIG).total == 0
    assert len(load_closet(session, user_a).views) == len(items) - 2
    listed = list_items(session, user_a, ItemFilters(), 1)
    assert target.id not in {i.id for i in listed.items}
    archived = list_items(session, user_a, ItemFilters(show="archived"), 1)
    assert {i.id for i in archived.items} == {i.id for i in shoes(items)}
    for pair in shoes(items):
        restore_item(session, pair)
    assert closet_stats_view(session, user_a, CONFIG).total == baseline
    assert list_items(session, user_a, ItemFilters(show="archived"), 1).total == 0


def test_filters_show_laundry_and_unavailable(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    set_availability(session, items[0], Availability.LAUNDRY)
    set_availability(session, items[1], Availability.UNAVAILABLE)

    def page(show: str | None) -> set[int | None]:
        return {i.id for i in list_items(session, user_a, ItemFilters(show=show), 1).items}

    assert page("laundry") == {items[0].id} and page("unavailable") == {items[1].id}
    assert items[0].id in page(None) and items[1].id in page(None)  # still in the closet view


def test_other_users_items_never_leak_into_hygiene_queries(
    session: Session, storage: LocalDiskStorage, user_a: User, user_b: User
) -> None:
    closet(session, storage, user_a)
    assert load_closet(session, user_b).views == []
    assert closet_stats_view(session, user_b, CONFIG).total == 0


# --------------------------------------------------------------------------- duplicates


def test_a_near_identical_piece_triggers_the_duplicate_warning(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    original = add_item(session, storage, user_a, subtype="shirt", primary_color="navy")
    twin = add_item(session, storage, user_a, subtype="shirt", primary_color="navy")
    found = duplicate_warning(session, user_a, twin, CONFIG, AnalysisConfig())
    assert found is not None and found.level == "near-identical"
    assert found.item_id in {original.id}
    different = add_item(session, storage, user_a, subtype="t-shirt", primary_color="red")
    assert duplicate_warning(session, user_a, different, CONFIG, AnalysisConfig()) is None


async def test_the_item_page_warns_about_a_duplicate_and_offers_archive(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    add_item(session, storage, user_a, subtype="shirt", primary_color="navy", name="Old navy")
    twin = add_item(session, storage, user_a, subtype="shirt", primary_color="navy", name="New")
    html = (await client.get(f"/items/{twin.id}")).text
    assert "Looks like a duplicate of" in html and "navy shirt" in html
    assert "Archive" in html and "Delete permanently" in html and 'for="availability"' in html


async def test_unique_items_show_no_warning(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    only = add_item(session, storage, user_a, subtype="shirt", primary_color="navy")
    assert "duplicate" not in (await client.get(f"/items/{only.id}")).text


# ------------------------------------------------------------------------------- routes


async def test_availability_archive_restore_routes(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = add_item(session, storage, user_a, name="Tee")
    wash = await post_form(client, f"/items/{item.id}/availability", {"value": "laundry"})
    assert wash.status_code == 303 and wash.headers["location"] == f"/items/{item.id}"
    session.refresh(item)
    assert item.availability == Availability.LAUNDRY
    assert "In the laundry" in (await client.get("/closet")).text
    assert (
        await post_form(client, f"/items/{item.id}/availability", {"value": "nonsense"})
    ).status_code == 422

    archived = await post_form(client, f"/items/{item.id}/archive")
    assert archived.status_code == 303 and archived.headers["location"] == "/closet"
    assert "Tee" not in (await client.get("/closet")).text
    shown = (await client.get("/closet?show=archived")).text
    assert "Archived" in shown and "1 item" in shown
    assert "Archived." in (await client.get(f"/items/{item.id}")).text

    restored = await post_form(client, f"/items/{item.id}/restore")
    assert restored.status_code == 303
    session.refresh(item)
    assert item.archived_at is None
    assert "1 item" in (await client.get("/closet")).text


async def test_nobody_else_can_change_availability_or_archive(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    item = add_item(session, storage, user_a)
    as_user(user_b)
    for path, data in (
        ("availability", {"value": "laundry"}),
        ("archive", {}),
        ("restore", {}),
    ):
        assert (await post_form(client, f"/items/{item.id}/{path}", data)).status_code == 404
    session.refresh(item)
    assert item.availability == Availability.AVAILABLE and item.archived_at is None


async def test_saved_outfits_flag_pieces_that_are_away(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    outfit = suggest(session, user_a, CONFIG, Context()).outfits[0]
    save_outfit(session, outfit)
    assert "Not available right now" not in (await client.get("/outfits")).text
    worn = session.get(Item, outfit.item_ids[0])
    assert worn is not None and worn in items
    set_availability(session, worn, Availability.LAUNDRY)
    page = (await client.get("/outfits")).text
    assert "Not available right now" in page and "laundry" in page
