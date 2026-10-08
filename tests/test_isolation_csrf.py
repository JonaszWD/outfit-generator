from collections.abc import Callable

import httpx
from sqlmodel import Session, select

from app.models import Item, User
from app.services.storage import LocalDiskStorage
from tests.conftest import VALID_FORM, image_bytes, post_form, post_upload, stored_files
from tests.test_closet_routes import add_item


async def test_user_b_cannot_touch_user_a_item(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    item = add_item(session, storage, user_a, name="Private")
    item_id, image_path = item.id, item.image_path
    thumb = image_path.replace(".jpg", "_thumb.jpg")
    files_before = stored_files(storage)

    # A can see everything
    assert (await client.get(f"/items/{item_id}")).status_code == 200
    assert (await client.get(f"/media/{image_path}")).status_code == 200
    assert (await client.get(f"/media/{thumb}")).status_code == 200

    as_user(user_b)
    assert (await client.get(f"/items/{item_id}")).status_code == 404
    assert (await client.get(f"/items/{item_id}/edit")).status_code == 404
    update = await post_form(client, f"/items/{item_id}", {**VALID_FORM, "name": "Hacked"})
    assert update.status_code == 404
    assert (await post_form(client, f"/items/{item_id}/delete")).status_code == 404
    assert (await client.get(f"/media/{image_path}")).status_code == 404
    assert (await client.get(f"/media/{thumb}")).status_code == 404

    # B's closet does not list A's item, and the item is untouched
    closet = await client.get("/closet")
    assert "0 items" in closet.text and "Private" not in closet.text
    session.expire_all()
    still_there = session.get(Item, item_id)
    assert still_there is not None and still_there.name == "Private"
    assert stored_files(storage) == files_before

    # B uploading gets files under B's own directory only
    created = await post_upload(client, [("b.jpg", image_bytes())])
    assert created.status_code == 303
    own = [p for p in stored_files(storage) if p not in files_before]
    assert own and all(p.parent.name == str(user_b.id) for p in own)

    # A cannot read B's media either
    as_user(user_a)
    b_item = session.exec(select(Item).where(Item.user_id == user_b.id)).one()
    assert (await client.get(f"/media/{b_item.image_path}")).status_code == 404


async def test_media_rejects_odd_filenames(client: httpx.AsyncClient, user_a: User) -> None:
    for name in ("passwd", "x.jpg", "..%2Fx.jpg", f"{'a' * 32}.png"):
        assert (await client.get(f"/media/{user_a.id}/{name}")).status_code == 404


async def test_unknown_item_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/items/999999")).status_code == 404


async def test_post_without_csrf_token_is_rejected(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = add_item(session, storage, user_a)
    await client.get("/closet")  # cookie present, but no token submitted

    no_token = await client.post("/items", data=VALID_FORM)
    assert no_token.status_code == 403
    delete = await client.post(f"/items/{item.id}/delete")
    assert delete.status_code == 403
    update = await client.post(f"/items/{item.id}", data=VALID_FORM)
    assert update.status_code == 403
    session.expire_all()
    assert session.get(Item, item.id) is not None


async def test_csrf_token_must_match_cookie(client: httpx.AsyncClient) -> None:
    response = await post_upload(client, [("a.jpg", image_bytes())], token="x" * 40)
    assert response.status_code == 403


async def test_csrf_header_accepted_for_htmx(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = add_item(session, storage, user_a)
    await client.get("/closet")
    response = await client.post(
        f"/items/{item.id}/delete", headers={"X-CSRF-Token": client.cookies["csrf_token"]}
    )
    assert response.status_code == 303
