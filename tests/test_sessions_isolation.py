"""The isolation guarantees again, with real accounts and real sessions (no overrides)."""

from collections.abc import Callable, Mapping

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from app.engine.config import EngineConfig
from app.engine.types import Context
from app.models import Candidate, Item, User
from app.services.outfits import save_outfit, suggest
from app.services.storage import LocalDiskStorage, cutout_key, cutout_thumb_key, thumb_key
from tests.conftest import VALID_FORM, image_bytes, signed_in, submit
from tests.test_closet_routes import add_item
from tests.test_outfit_routes import closet

MINE, THEIRS = "me@example.com", "them@example.com"


def user_row(session: Session, email: str) -> User:
    return session.exec(select(User).where(User.email == email)).one()


def with_cutouts(session: Session, storage: LocalDiskStorage, item: Item) -> Item:
    """The pipeline's extra files, so every media type exists."""
    png = image_bytes("PNG", size=(30, 40))
    storage.save(cutout_key(item.image_path), png)
    storage.save(cutout_thumb_key(item.image_path), png)
    item.cutout_image_path = cutout_key(item.image_path)
    session.add(item)
    session.flush()
    return item


MEDIA: dict[str, Callable[[Item], str]] = {
    "original jpg": lambda i: i.image_path,
    "thumbnail jpg": lambda i: thumb_key(i.image_path),
    "cutout png": lambda i: cutout_key(i.image_path),
    "cutout thumbnail png": lambda i: cutout_thumb_key(i.image_path),
}


@pytest.mark.parametrize("kind", list(MEDIA))
async def test_media_is_served_only_to_its_owner(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage, kind: str
) -> None:
    async with signed_in(auth_app, MINE) as me, signed_in(auth_app, THEIRS) as them:
        item = with_cutouts(session, storage, add_item(session, storage, user_row(session, MINE)))
        url = f"/media/{MEDIA[kind](item)}"
        owner = await me.get(url)
        assert owner.status_code == 200 and owner.headers["content-type"].startswith("image/")
        assert "private" in owner.headers["cache-control"]
        assert (await them.get(url)).status_code == 404
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
        ) as anonymous:
            gone = await anonymous.get(url)
            assert gone.status_code == 303 and gone.headers["location"].startswith("/login")


async def test_items_cannot_be_viewed_changed_or_deleted_by_someone_else(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app, MINE) as me, signed_in(auth_app, THEIRS) as them:
        item = add_item(session, storage, user_row(session, MINE), name="Private")
        assert (await me.get(f"/items/{item.id}")).status_code == 200
        for path in ("", "/edit", "/review", "/status"):
            assert (await them.get(f"/items/{item.id}{path}")).status_code == 404, path
        posts: dict[str, Mapping[str, object]] = {
            "": VALID_FORM,
            "/confirm": VALID_FORM,
            "/retag": {},
            "/availability": {"value": "laundry"},
            "/archive": {},
            "/restore": {},
            "/delete": {},
        }
        for path, data in posts.items():
            response = await submit(them, f"/items/{item.id}{path}", data, form_page="/closet")
            assert response.status_code == 404, path
        session.refresh(item)
        assert item.name == "Private"
        assert "Private" not in (await them.get("/closet")).text
        assert "Private" in (await me.get("/closet")).text


async def test_outfits_cannot_be_touched_by_someone_else(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app, MINE) as me, signed_in(auth_app, THEIRS) as them:
        owner = user_row(session, MINE)
        closet(session, storage, owner)
        outfit = suggest(session, owner, EngineConfig(), Context(), rng_seed=1).outfits[0]
        save_outfit(session, outfit)
        assert (await me.get("/outfits")).text.count('class="card outfit"') == 1
        assert 'class="card outfit"' not in (await them.get("/outfits")).text
        outfit_posts: dict[str, Mapping[str, object]] = {
            "save": {},
            "delete": {},
            "feedback": {"rating": "up"},
            "occasions": {"occasions": ["work"]},
        }
        for path, data in outfit_posts.items():
            response = await submit(them, f"/outfits/{outfit.id}/{path}", data, form_page="/closet")
            assert response.status_code == 404, path
        assert (
            await submit(
                them,
                "/planner/assign",
                {"day": "2026-10-05", "outfit_id": outfit.id},
                form_page="/planner",
            )
        ).status_code == 404
        assert (await me.get("/outfits")).text.count('class="card outfit"') == 1


async def test_candidates_cannot_be_opened_by_someone_else(
    auth_app: FastAPI, session: Session
) -> None:
    async with signed_in(auth_app, MINE) as me, signed_in(auth_app, THEIRS) as them:
        owner = user_row(session, MINE)
        candidate = Candidate(
            user_id=owner.id or 0, url="https://shop.example/p/1", url_hash="d" * 64
        )
        session.add(candidate)
        session.flush()
        assert (await me.get(f"/candidates/{candidate.id}")).status_code in (200, 303)
        for suffix in ("", "/panel", "/edit"):
            assert (await them.get(f"/candidates/{candidate.id}{suffix}")).status_code == 404
        for suffix in ("/retry", "/add", "/discard"):
            response = await submit(
                them, f"/candidates/{candidate.id}{suffix}", form_page="/analyze"
            )
            assert response.status_code == 404, suffix


async def test_anonymous_visitors_are_sent_to_the_login_page(auth_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
    ) as anonymous:
        for path in ("/closet", "/outfits", "/items/1", "/analyze", "/planner", "/account"):
            response = await anonymous.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"].startswith("/login?msg=login"), path
