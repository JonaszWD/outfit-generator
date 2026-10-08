from typing import Any

import httpx
from sqlmodel import Session, select

from app.models import Category, Item, TaggingStatus, User
from app.schemas import ItemData
from app.services.imaging import process_image
from app.services.items import PAGE_SIZE, create_item
from app.services.storage import LocalDiskStorage
from tests.conftest import (
    VALID_FORM,
    image_bytes,
    post_form,
    post_upload,
    stored_files,
)


def add_item(session: Session, storage: LocalDiskStorage, user: User, **overrides: Any) -> Item:
    fields: dict[str, Any] = {
        "category": Category.TOP,
        "subtype": "shirt",
        "primary_color": "navy",
        "seasons": [],
    }
    fields.update(overrides)
    image = process_image(image_bytes(size=(60, 80)), 1024 * 1024)
    return create_item(session, storage, user, ItemData(**fields), image)


async def test_upload_confirm_edit_delete_cycle(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage
) -> None:
    uploaded = await post_upload(client, [("shirt.jpg", image_bytes())])
    assert uploaded.status_code == 303 and uploaded.headers["location"] == "/closet"
    item = session.exec(select(Item)).one()
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE  # background task ran
    assert item.tags_confirmed is False and item.cutout_image_path
    item_id = item.id

    confirmed = await post_form(
        client, f"/items/{item_id}/confirm", {**VALID_FORM, "seasons": ["summer", "spring"]}
    )
    assert confirmed.status_code == 303
    detail = await client.get(f"/items/{item_id}")
    assert detail.status_code == 200 and "Navy shirt" in detail.text
    assert "1 item" in (await client.get("/closet")).text

    old_files = {p.name for p in stored_files(storage)}
    updated = await post_form(
        client,
        f"/items/{item_id}",
        {**VALID_FORM, "name": "Renamed", "subtype": "blouse"},
        image_bytes(color="blue"),
    )
    assert updated.status_code == 303
    detail = await client.get(f"/items/{item_id}")
    assert "Renamed" in detail.text and "Blouse" in detail.text
    new_files = {p.name for p in stored_files(storage)}
    assert not (new_files & old_files)  # old photo and cutout deleted
    session.refresh(item)
    assert item.tags_confirmed and item.tagging_status == TaggingStatus.DONE  # re-tagged

    again = await post_form(client, f"/items/{item_id}", {**VALID_FORM, "name": "Kept"})
    assert again.status_code == 303
    assert {p.name for p in stored_files(storage)} == new_files

    deleted = await post_form(client, f"/items/{item_id}/delete")
    assert deleted.status_code == 303 and deleted.headers["location"] == "/closet"
    assert (await client.get(f"/items/{item_id}")).status_code == 404
    assert stored_files(storage) == []


async def test_forms_render(client: httpx.AsyncClient, session: Session) -> None:
    new = await client.get("/items/new")
    assert new.status_code == 200 and "Add photos" in new.text
    options = await client.get("/items/subtype-options", params={"category": "shoes"})
    assert "Sneakers" in options.text and "Jeans" not in options.text


async def test_empty_state(client: httpx.AsyncClient) -> None:
    page = await client.get("/closet")
    assert "Add your first photos" in page.text and "0 items" in page.text


async def _pending(client: httpx.AsyncClient, session: Session) -> Item:
    await post_upload(client, [("a.jpg", image_bytes())])
    return session.exec(select(Item)).one()


async def test_rejects_subtype_not_matching_category(
    client: httpx.AsyncClient, session: Session
) -> None:
    item = await _pending(client, session)
    response = await post_form(
        client, f"/items/{item.id}/confirm", {**VALID_FORM, "category": "shoes", "subtype": "shirt"}
    )
    assert response.status_code == 422 and "is not a shoes subtype" in response.text


async def test_form_keeps_input_and_shows_inline_errors(
    client: httpx.AsyncClient, session: Session
) -> None:
    item = await _pending(client, session)
    response = await post_form(
        client,
        f"/items/{item.id}/confirm",
        {
            **VALID_FORM,
            "primary_color": "fuchsia",
            "name": "My special shirt",
            "seasons": ["winter"],
        },
    )
    assert response.status_code == 422
    assert 'value="My special shirt"' in response.text
    assert "Choose a color from the palette." in response.text
    assert 'value="winter" checked' in response.text


async def test_rejected_uploads(client: httpx.AsyncClient, storage: LocalDiskStorage) -> None:
    fake = await post_upload(client, [("evil.jpg", b"not an image at all")])
    assert fake.status_code == 422 and "Unsupported file type" in fake.text
    assert "evil.jpg" in fake.text

    none = await client.post("/items", data={"csrf_token": client.cookies["csrf_token"]})
    assert none.status_code == 422 and "Choose at least one photo" in none.text

    huge = await post_upload(client, [("big.jpg", b"\xff\xd8" + b"0" * (2 * 1024 * 1024))])
    assert huge.status_code == 422 and "too large" in huge.text
    assert stored_files(storage) == []


async def test_filters_and_combinations(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    add_item(
        session,
        storage,
        user_a,
        name="Red summer tee",
        primary_color="red",
        subtype="t-shirt",
        seasons=["summer"],
    )
    add_item(
        session,
        storage,
        user_a,
        name="Navy wool sweater",
        subtype="sweater",
        seasons=["winter", "autumn"],
    )
    add_item(
        session,
        storage,
        user_a,
        name="White sneakers",
        category=Category.SHOES,
        subtype="sneakers",
        primary_color="white",
        seasons=["summer", "spring"],
    )
    add_item(
        session,
        storage,
        user_a,
        name="Black loafers",
        category=Category.SHOES,
        subtype="loafers",
        primary_color="black",
        seasons=["summer"],
    )

    async def names(**params: str) -> str:
        return (await client.get("/closet", params=params)).text

    shoes = await names(category="shoes")
    assert "2 items" in shoes and "White sneakers" in shoes and "sweater" not in shoes
    both = await names(category="shoes", season="summer")
    assert "2 items" in both
    narrowed = await names(category="shoes", season="spring")
    assert "1 item" in narrowed and "White sneakers" in narrowed
    assert "1 item" in await names(color="red")
    assert "1 item" in await names(season="winter")
    assert "1 item" in await names(q="sweat")  # matches name
    assert "1 item" in await names(q="LOAFERS")  # matches subtype, case-insensitive
    assert "0 items" in await names(q="%")  # wildcard is escaped, not a match-all
    assert "4 items" in await names(category="nonsense", season="")


async def test_htmx_returns_fragments(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    add_item(session, storage, user_a, name="Only item")
    full = await client.get("/closet")
    fragment = await client.get("/closet", headers={"HX-Request": "true"})
    assert "<html" in full.text and "<html" not in fragment.text
    assert "1 item" in fragment.text and fragment.headers["vary"] == "HX-Request"


async def test_pagination_load_more(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    for n in range(PAGE_SIZE + 3):
        add_item(session, storage, user_a, name=f"Item {n:02d}")
    first = await client.get("/closet")
    assert f"{PAGE_SIZE + 3} items" in first.text
    assert first.text.count('class="card"') == PAGE_SIZE
    assert "page=2" in first.text and "Load more" in first.text
    assert "Item 26" in first.text and "Item 02" not in first.text  # newest first

    more = await client.get("/closet", params={"page": 2}, headers={"HX-Request": "true"})
    assert more.text.count('class="card"') == 3
    assert "Load more" not in more.text and "Item 02" in more.text


async def test_load_more_keeps_filters(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    for n in range(PAGE_SIZE + 1):
        add_item(
            session, storage, user_a, name=f"Shoe {n}", category=Category.SHOES, subtype="boots"
        )
    page = await client.get("/closet", params={"category": "shoes"})
    assert "category=shoes&amp;page=2" in page.text
