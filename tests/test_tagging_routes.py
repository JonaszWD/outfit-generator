from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlmodel import Session, select

from app.models import Item, TaggingStatus, User
from app.services.imaging import process_image
from app.services.items import create_pending_item
from app.services.storage import LocalDiskStorage
from app.services.tagging import TAGGED_FIELDS
from tests.conftest import VALID_FORM, image_bytes, post_form, post_upload, stored_files

AI_TAGS = {
    "category": "top",
    "subtype": "shirt",
    "primary_color": "navy",
    "secondary_colors": ["white"],
    "pattern": "striped",
    "seasons": ["spring"],
    "material": "cotton",
    "layer_role": "base",
}


def tagged_item(
    session: Session,
    storage: LocalDiskStorage,
    user: User,
    low: tuple[str, ...] = (),
    status: TaggingStatus = TaggingStatus.DONE,
    **tags: Any,
) -> Item:
    """An item as the pipeline leaves it: done, AI tags present, not yet confirmed."""
    item = create_pending_item(session, storage, user, process_image(image_bytes(), 10**7))
    confidence = {f: (0.4 if f in low else 0.9) for f in TAGGED_FIELDS}
    item.ai_tags = {"tags": {**AI_TAGS, **tags}, "flags": [], "dominant_colors": []}
    item.ai_confidence = confidence
    item.tagging_status = status
    session.add(item)
    session.commit()
    return item


@pytest.fixture
def no_background(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stop uploads from running the pipeline, so items stay pending."""
    queued: list[int] = []
    monkeypatch.setattr(
        "app.routes.items.process_item", lambda item_id, deps=None: queued.append(item_id)
    )
    return queued


async def test_batch_upload_creates_one_pending_item_per_file(
    client: httpx.AsyncClient, session: Session, no_background: list[int]
) -> None:
    files = [(f"p{n}.jpg", image_bytes(color=c)) for n, c in enumerate(["red", "blue", "green"])]
    response = await post_upload(client, files)
    assert response.status_code == 303
    items = list(session.exec(select(Item)))
    assert len(items) == 3
    assert all(i.tagging_status == TaggingStatus.PENDING and not i.tags_confirmed for i in items)
    assert all(i.category is None and i.original_image_path == i.image_path for i in items)
    assert sorted(no_background) == sorted(i.id for i in items if i.id)


async def test_batch_upload_keeps_good_files_and_reports_bad_ones(
    client: httpx.AsyncClient, session: Session, no_background: list[int]
) -> None:
    files = [("good.jpg", image_bytes()), ("bad.jpg", b"nope"), ("also-good.jpg", image_bytes())]
    response = await post_upload(client, files)
    assert response.status_code == 200
    assert "2 photos uploaded" in response.text and "bad.jpg" in response.text
    assert len(list(session.exec(select(Item)))) == 2


async def test_batch_upload_respects_file_limit(
    client: httpx.AsyncClient, session: Session, no_background: list[int]
) -> None:
    response = await post_upload(client, [(f"{n}.jpg", image_bytes()) for n in range(12)])
    assert "at most 10" in response.text
    assert len(list(session.exec(select(Item)))) == 10


async def test_status_partial_polls_until_done(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = tagged_item(session, storage, user_a, status=TaggingStatus.PENDING)
    pending = await client.get(f"/items/{item.id}/status")
    assert 'hx-trigger="every 2s"' in pending.text and "spinner" in pending.text

    item.tagging_status = TaggingStatus.PROCESSING
    session.commit()
    assert 'hx-trigger="every 2s"' in (await client.get(f"/items/{item.id}/status")).text

    item.tagging_status = TaggingStatus.DONE
    session.commit()
    done = await client.get(f"/items/{item.id}/status")
    assert "hx-trigger" not in done.text and "needs review" in done.text

    item.tagging_status = TaggingStatus.FAILED
    item.tagging_error = "model said no"
    session.commit()
    failed = await client.get(f"/items/{item.id}/status")
    assert "hx-trigger" not in failed.text
    assert "model said no" in failed.text and f"/items/{item.id}/retag" in failed.text


async def test_closet_cards_poll_only_while_unfinished(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    tagged_item(session, storage, user_a, status=TaggingStatus.PENDING)
    assert (await client.get("/closet")).text.count("every 2s") == 1
    tagged_item(session, storage, user_a)
    assert (await client.get("/closet")).text.count("every 2s") == 1


async def test_review_prefills_ai_tags_and_highlights_low_confidence(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = tagged_item(session, storage, user_a, low=("subtype",))
    page = await client.get(f"/items/{item.id}/review")
    assert page.status_code == 200
    assert 'value="navy" selected' in page.text
    assert 'value="shirt" selected' in page.text
    assert 'value="striped" selected' in page.text
    assert page.text.count('class="badge">Please check') == 1 and "40% sure" in page.text
    assert page.text.count('class="field  low"') == 1  # only the subtype field is highlighted
    assert f"/items/{item.id}/confirm" in page.text


async def test_review_redirects_while_processing(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = tagged_item(session, storage, user_a, status=TaggingStatus.PROCESSING)
    response = await client.get(f"/items/{item.id}/review")
    assert response.status_code == 303 and response.headers["location"] == "/closet"


async def test_confirm_saves_user_edits_not_ai_values(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = tagged_item(session, storage, user_a)
    edited = {**VALID_FORM, "subtype": "polo", "primary_color": "olive", "name": "Mine"}
    response = await post_form(client, f"/items/{item.id}/confirm", edited)
    assert response.status_code == 303
    session.refresh(item)
    assert item.tags_confirmed and item.subtype == "polo" and item.primary_color == "olive"
    assert item.ai_tags is not None and item.ai_tags["tags"]["subtype"] == "shirt"  # kept as-is


async def test_confirm_rejects_values_outside_vocabulary(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = tagged_item(session, storage, user_a)
    bad_forms: list[dict[str, Any]] = [
        {**VALID_FORM, "primary_color": "turquoise"},
        {**VALID_FORM, "category": "hat"},
        {**VALID_FORM, "subtype": "tuxedo"},
        {**VALID_FORM, "pattern": "paisley"},
        {**VALID_FORM, "material": "unobtainium"},
        {**VALID_FORM, "seasons": ["monsoon"]},
        {**VALID_FORM, "secondary_colors": ["nope"]},
        {**VALID_FORM, "layer_role": "sideways"},
    ]
    for form in bad_forms:
        response = await post_form(client, f"/items/{item.id}/confirm", form)
        assert response.status_code == 422, form
    session.refresh(item)
    assert not item.tags_confirmed and item.subtype is None


async def test_confirm_redirects_to_next_review_or_detail(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    first = tagged_item(session, storage, user_a)
    second = tagged_item(session, storage, user_a)
    r1 = await post_form(client, f"/items/{first.id}/confirm", VALID_FORM)
    assert r1.headers["location"] == "/closet?review=1"
    r2 = await post_form(client, f"/items/{second.id}/confirm", VALID_FORM)
    assert r2.headers["location"] == f"/items/{second.id}"


async def test_retag_reruns_pipeline_for_failed_item(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = tagged_item(session, storage, user_a, status=TaggingStatus.FAILED)
    item.tagging_error = "boom"
    item.ai_tags = None
    session.commit()
    response = await post_form(client, f"/items/{item.id}/retag")
    assert response.status_code == 303
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE and item.tagging_error is None
    assert item.ai_tags is not None  # the background task ran the fake pipeline


async def test_retag_via_htmx_returns_the_card(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    no_background: list[int],
) -> None:
    item = tagged_item(session, storage, user_a, status=TaggingStatus.FAILED)
    await client.get("/closet")
    token = client.cookies["csrf_token"]
    response = await client.post(
        f"/items/{item.id}/retag", headers={"HX-Request": "true", "X-CSRF-Token": token}
    )
    assert response.status_code == 200 and 'hx-trigger="every 2s"' in response.text
    assert no_background == [item.id]


async def test_needs_review_filter(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    waiting = tagged_item(session, storage, user_a)
    tagged_item(session, storage, user_a, status=TaggingStatus.PENDING)
    confirmed = tagged_item(session, storage, user_a)
    await post_form(client, f"/items/{confirmed.id}/confirm", VALID_FORM)
    page = await client.get("/closet", params={"review": "1"})
    assert "1 item" in page.text and f"/items/{waiting.id}/review" in page.text
    assert "1 item need" in page.text  # banner


async def test_confirm_all_requires_no_low_confidence_fields(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    sure = tagged_item(session, storage, user_a)
    unsure = tagged_item(session, storage, user_a, low=("material",))

    page = await client.get("/closet")
    assert "disabled" in page.text and "low-confidence" in page.text
    refused = await post_form(client, "/items/confirm-all")
    assert refused.status_code == 409
    session.refresh(sure)
    assert not sure.tags_confirmed

    await post_form(client, f"/items/{unsure.id}/confirm", VALID_FORM)
    page = await client.get("/closet")
    assert "disabled" not in page.text.split("Confirm all")[0][-200:]
    ok = await post_form(client, "/items/confirm-all")
    assert ok.status_code == 303
    session.refresh(sure)
    assert sure.tags_confirmed and sure.subtype == "shirt" and sure.primary_color == "navy"


async def test_user_b_cannot_see_or_change_user_a_ai_items(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    item = tagged_item(session, storage, user_a)
    files = stored_files(storage)
    as_user(user_b)
    assert (await client.get(f"/items/{item.id}/review")).status_code == 404
    assert (await client.get(f"/items/{item.id}/status")).status_code == 404
    assert (await post_form(client, f"/items/{item.id}/confirm", VALID_FORM)).status_code == 404
    assert (await post_form(client, f"/items/{item.id}/retag")).status_code == 404
    cutout = item.image_path.replace(".jpg", "_cutout.png")
    assert (await client.get(f"/media/{cutout}")).status_code == 404
    # B's confirm-all only touches B's items
    tagged_item(session, storage, user_b)
    assert (await post_form(client, "/items/confirm-all")).status_code == 303
    session.refresh(item)
    assert not item.tags_confirmed
    assert set(files) <= set(stored_files(storage))


async def test_upload_sends_original_to_pipeline_with_exif_removed(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage
) -> None:
    from io import BytesIO

    from PIL import Image

    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (52.0, 31.0, 0.0), 3: "E", 4: (13.0, 24.0, 0.0)}
    response = await post_upload(client, [("gps.jpg", image_bytes(exif=exif))])
    assert response.status_code == 303
    item = session.exec(select(Item)).one()
    assert item.original_image_path
    stored = storage.read(item.original_image_path)
    assert not Image.open(BytesIO(stored)).getexif().get_ifd(0x8825)
