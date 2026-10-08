import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlmodel import Session, select

from app.engine.config import EngineConfig
from app.engine.generator import count_outfits
from app.models import Category, Item, Outfit, User
from app.services.outfits import load_closet
from app.services.storage import LocalDiskStorage
from tests.conftest import post_form
from tests.test_closet_routes import add_item

CONFIG = EngineConfig()


def piece(
    session: Session,
    storage: LocalDiskStorage,
    user: User,
    category: Category,
    subtype: str,
    color: str,
    name: str | None = None,
) -> Item:
    return add_item(
        session,
        storage,
        user,
        category=category,
        subtype=subtype,
        primary_color=color,
        name=name,
    )


def closet(session: Session, storage: LocalDiskStorage, user: User) -> list[Item]:
    """A small confirmed closet: 3 tops, 2 bottoms, 2 shoes, a jacket and a belt."""
    T, B, S = Category.TOP, Category.BOTTOM, Category.SHOES
    rows = [
        (T, "t-shirt", "white", "White tee"),
        (T, "shirt", "light blue", "Oxford"),
        (T, "polo", "navy", "Polo"),
        (B, "jeans", "blue", None),
        (B, "chinos", "beige", None),
        (S, "sneakers", "white", None),
        (S, "loafers", "brown", None),
        (Category.OUTERWEAR, "jacket", "olive", None),
        (Category.ACCESSORY, "belt", "brown", None),
    ]
    return [piece(session, storage, user, *row) for row in rows]


def seen_values(html: str) -> list[str]:
    return re.findall(r'name="seen" value="([^"]+)"', html)


def card_ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'id="outfit-(\d+)"', html)]


async def test_generate_returns_three_cards_with_reasons_and_regenerate(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    response = await post_form(client, "/outfits/generate", {"season": ""})
    assert response.status_code == 200
    assert response.text.count('class="card outfit"') == 3
    assert "Regenerate" in response.text and "Score" in response.text
    assert response.text.count("<li>") >= 6  # 2-4 reasons per card
    rows = list(session.exec(select(Outfit)))
    assert len(rows) == 3 and all(not r.favorite for r in rows)
    assert all(r.item_ids == sorted(r.item_ids) for r in rows)
    expected = {"season": None, "temperature_c": None, "rain": False}
    assert all(r.context == expected for r in rows)


async def test_cards_show_items_in_slot_order(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    category_of = {(i.name or i.subtype): i.category for i in items}
    rank = {
        Category.TOP: 0,
        Category.BOTTOM: 1,
        Category.OUTERWEAR: 2,
        Category.SHOES: 3,
        Category.ACCESSORY: 4,
    }
    page = await post_form(client, "/outfits/generate", {})
    for card in page.text.split('class="card outfit"')[1:]:
        alts = [a.split(",")[0] for a in re.findall(r'alt="([^"]*)"', card.split("</article>")[0])]
        ranks = [rank[c] for a in alts if (c := category_of.get(a)) is not None]
        assert len(ranks) >= 3 and ranks == sorted(ranks)


async def test_regenerate_never_repeats_shown_outfits(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    first = await post_form(client, "/outfits/generate", {})
    seen = seen_values(first.text)
    assert len(seen) == 3
    second = await post_form(client, "/outfits/generate", {"seen": seen})
    assert set(card_ids(first.text)).isdisjoint(card_ids(second.text))
    assert set(seen) < set(seen_values(second.text))  # the seen list keeps growing


async def test_regenerate_says_when_everything_has_been_shown(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    add_item(session, storage, user_a, subtype="t-shirt", primary_color="white")
    bottom = {"category": Category.BOTTOM, "subtype": "jeans", "primary_color": "blue"}
    add_item(session, storage, user_a, **bottom)
    shoes = {"category": Category.SHOES, "primary_color": "white"}
    add_item(session, storage, user_a, subtype="sneakers", **shoes)
    add_item(session, storage, user_a, subtype="loafers", **shoes)

    first = await post_form(client, "/outfits/generate", {})
    assert first.text.count('class="card outfit"') == 2  # only two outfits exist
    second = await post_form(client, "/outfits/generate", {"seen": seen_values(first.text)})
    assert "every outfit I can suggest" in second.text and "Start over" in second.text
    assert 'class="card outfit"' not in second.text
    restart = await post_form(client, "/outfits/generate", {})
    assert restart.text.count('class="card outfit"') == 2


async def test_same_outfit_is_stored_once(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    add_item(session, storage, user_a, subtype="t-shirt", primary_color="white")
    piece(session, storage, user_a, Category.BOTTOM, "jeans", "blue")
    piece(session, storage, user_a, Category.SHOES, "sneakers", "white")
    await post_form(client, "/outfits/generate", {})
    await post_form(client, "/outfits/generate", {})
    assert len(list(session.exec(select(Outfit)))) == 1


async def test_empty_closet_says_what_is_missing(client: httpx.AsyncClient) -> None:
    page = await post_form(client, "/outfits/generate", {})
    assert page.status_code == 200
    for missing in ("No shoes yet.", "No tops yet", "No bottoms yet"):
        assert missing in page.text
    assert "Add photos" in page.text


async def test_missing_shoes_only(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    add_item(session, storage, user_a, subtype="t-shirt", primary_color="white")
    piece(session, storage, user_a, Category.BOTTOM, "jeans", "blue")
    page = await post_form(client, "/outfits/generate", {})
    assert "No shoes yet." in page.text and "No tops yet" not in page.text


async def test_unconfirmed_items_are_not_used(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    from tests.test_tagging_routes import tagged_item

    for _ in range(3):
        tagged_item(session, storage, user_a)  # done, but not confirmed
    page = await post_form(client, "/outfits/generate", {})
    assert "No shoes yet." in page.text
    assert load_closet(session, user_a).unconfirmed == 3


async def test_season_is_validated(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    junk = {"season": "<script>"}
    page = await post_form(client, "/outfits/generate", junk)
    assert page.status_code == 200 and "<script>" not in page.text
    assert page.text.count('class="card outfit"') == 3


async def test_save_list_duplicate_and_delete(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    page = await post_form(client, "/outfits/generate", {})
    outfit_id = card_ids(page.text)[0]

    assert "Nothing saved yet" in (await client.get("/outfits")).text
    saved = await post_form(client, f"/outfits/{outfit_id}/save")
    assert saved.status_code == 303
    row = session.get(Outfit, outfit_id)
    assert row is not None
    session.refresh(row)
    assert row.favorite
    assert (await client.get("/outfits")).text.count('class="card outfit"') == 1

    again = await post_form(client, f"/outfits/{outfit_id}/save")
    assert again.status_code == 409 and "already saved" in again.json()["detail"]
    assert len(list(session.exec(select(Outfit).where(Outfit.favorite.is_(True))))) == 1  # type: ignore[attr-defined]

    deleted = await post_form(client, f"/outfits/{outfit_id}/delete")
    assert deleted.status_code == 303
    assert session.get(Outfit, outfit_id) is None
    assert "Nothing saved yet" in (await client.get("/outfits")).text


async def test_save_and_delete_via_htmx(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    page = await post_form(client, "/outfits/generate", {})
    outfit_id = card_ids(page.text)[0]
    headers = {"HX-Request": "true", "X-CSRF-Token": client.cookies["csrf_token"]}
    saved = await client.post(f"/outfits/{outfit_id}/save", headers=headers)
    assert saved.status_code == 200 and "Saved" in saved.text and 'id="outfit-' in saved.text
    removed = await client.post(f"/outfits/{outfit_id}/delete", headers=headers)
    assert removed.status_code == 200 and removed.text == ""


async def test_user_b_cannot_save_delete_or_see_user_a_outfits(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    closet(session, storage, user_a)
    page = await post_form(client, "/outfits/generate", {})
    outfit_id = card_ids(page.text)[0]
    await post_form(client, f"/outfits/{outfit_id}/save")

    as_user(user_b)
    assert (await post_form(client, f"/outfits/{outfit_id}/save")).status_code == 404
    assert (await post_form(client, f"/outfits/{outfit_id}/delete")).status_code == 404
    listing = await client.get("/outfits")
    assert 'class="card outfit"' not in listing.text
    session.expire_all()
    row = session.get(Outfit, outfit_id)
    assert row is not None and row.favorite  # untouched

    # B's closet is empty, and A's pieces never leak into B's suggestions
    empty = await post_form(client, "/outfits/generate", {})
    assert "No shoes yet." in empty.text
    assert session.exec(select(Outfit).where(Outfit.user_id == user_b.id)).first() is None


async def test_unknown_outfit_is_404(client: httpx.AsyncClient) -> None:
    assert (await post_form(client, "/outfits/999999/save")).status_code == 404


async def test_outfit_posts_require_csrf(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    await client.get("/closet")
    assert (await client.post("/outfits/generate", data={})).status_code == 403
    page = await post_form(client, "/outfits/generate", {})
    outfit_id = card_ids(page.text)[0]
    assert (await client.post(f"/outfits/{outfit_id}/save")).status_code == 403
    assert (await client.post(f"/outfits/{outfit_id}/delete")).status_code == 403


async def test_deleting_an_item_removes_outfits_that_use_it(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    page = await post_form(client, "/outfits/generate", {})
    outfit_id = card_ids(page.text)[0]
    row = session.get(Outfit, outfit_id)
    assert row is not None
    victim = row.item_ids[0]
    assert (await post_form(client, f"/items/{victim}/delete")).status_code == 303
    session.expire_all()
    assert session.get(Outfit, outfit_id) is None
    assert all(victim not in o.item_ids for o in session.exec(select(Outfit)))
    assert any(i.id == victim for i in items)


async def test_old_unsaved_suggestions_are_pruned_but_saved_ones_stay(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    old = datetime.now(UTC) - timedelta(days=3)
    stale = Outfit(user_id=user_a.id, item_ids=[9001, 9002, 9003], created_at=old)  # type: ignore[arg-type]
    kept = Outfit(user_id=user_a.id, item_ids=[9004, 9005, 9006], created_at=old, favorite=True)  # type: ignore[arg-type]
    session.add_all([stale, kept])
    session.commit()
    stale_id, kept_id = stale.id, kept.id
    await post_form(client, "/outfits/generate", {})
    session.expire_all()
    assert session.get(Outfit, stale_id) is None
    assert session.get(Outfit, kept_id) is not None


async def test_stats_json_matches_the_engine(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    items = closet(session, storage, user_a)
    response = await client.get("/closet/stats", headers={"Accept": "application/json"})
    assert response.status_code == 200
    data = response.json()
    views = load_closet(session, user_a).views
    assert data["total"] == count_outfits(views, CONFIG).count > 0
    by_id = {row["id"]: row["outfits"] for row in data["items"]}
    assert set(by_id) == {i.id for i in items}
    counts = [row["outfits"] for row in data["items"]]
    assert counts == sorted(counts, reverse=True)  # most versatile first
    sneakers = next(i for i in items if i.subtype == "sneakers")
    loafers = next(i for i in items if i.subtype == "loafers")
    assert by_id[sneakers.id] > 0 and by_id[loafers.id] > 0


async def test_stats_page_lists_most_and_least_versatile(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    page = await client.get("/closet/stats")
    assert page.status_code == 200
    assert "Most versatile" in page.text and "Least versatile" in page.text
    assert "Valid outfits" in page.text


async def test_stats_for_an_empty_closet_explains_why(client: httpx.AsyncClient) -> None:
    page = await client.get("/closet/stats")
    assert page.status_code == 200 and "No shoes yet." in page.text
    data = (await client.get("/closet/stats", headers={"Accept": "application/json"})).json()
    assert data == {"total": 0, "unconfirmed_items": 0, "items": []}


async def test_stats_are_per_user(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    closet(session, storage, user_a)
    as_user(user_b)
    data = (await client.get("/closet/stats", headers={"Accept": "application/json"})).json()
    assert data["total"] == 0 and data["items"] == []
