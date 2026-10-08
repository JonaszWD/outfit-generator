"""Data export, account deletion, and the response headers that protect the browser."""

import io
import json
import zipfile
from datetime import date
from typing import Any

from fastapi import FastAPI
from sqlalchemy import func
from sqlmodel import Session, col, select
from starlette.responses import Response

from app.config import Settings, get_settings
from app.engine.config import EngineConfig
from app.engine.types import Context
from app.models import (
    Candidate,
    Item,
    Outfit,
    OutfitFeedback,
    PasswordReset,
    PlannedOutfit,
    User,
    UserPreferences,
    UserSession,
)
from app.security import CSP, apply_security_headers
from app.services import feedback as feedback_svc
from app.services import planner
from app.services.outfits import save_outfit, suggest
from app.services.storage import LocalDiskStorage
from tests.conftest import EMAIL, PASSWORD, new_client, sign_up, signed_in, stored_files, submit
from tests.test_closet_routes import add_item
from tests.test_outfit_routes import closet
from tests.test_sessions_isolation import THEIRS, user_row, with_cutouts


def populate(
    session: Session, storage: LocalDiskStorage, user: User, with_cutout: bool = True
) -> None:
    """A little of everything the user can own."""
    items = closet(session, storage, user)
    if with_cutout:
        with_cutouts(session, storage, items[0])
    session.add(
        UserPreferences(user_id=user.id or 0, home_city="Oslo", latitude=59.91, longitude=10.75)
    )
    outfit = suggest(session, user, EngineConfig(), Context(), rng_seed=2).outfits[0]
    save_outfit(session, outfit)
    feedback_svc.record_feedback(session, user, outfit, "down", "colors clash")
    planner.assign(session, user, planner.week_start(date.today()), outfit)
    session.add(Candidate(user_id=user.id or 0, url="https://shop.example/p/1", url_hash="e" * 64))
    session.flush()


def count(session: Session, model: Any, user_id: int | None) -> int:
    return session.exec(
        select(func.count()).select_from(model).where(col(model.user_id) == user_id)
    ).one()


# ------------------------------------------------------------------------------- export


async def test_export_has_this_users_data_and_photos_only(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app, EMAIL) as me, signed_in(auth_app, THEIRS):
        mine, theirs = user_row(session, EMAIL), user_row(session, THEIRS)
        populate(session, storage, mine)
        add_item(session, storage, theirs, name="Their secret")
        response = await me.get("/account/export")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    data = json.loads(archive.read("data.json"))

    assert data["account"]["email"] == EMAIL
    assert len(data["items"]) == 9 and len(data["outfits"]) >= 1
    assert len(data["feedback"]) == 1 and data["preferences"]["home_city"] == "Oslo"
    assert len(data["planned_outfits"]) == 1 and len(data["candidates"]) == 1
    owned_ids = {i["id"] for i in data["items"]}
    assert owned_ids == {i.id for i in session.exec(select(Item).where(Item.user_id == mine.id))}

    text = archive.read("data.json").decode()
    assert THEIRS not in text and "Their secret" not in text
    assert "hashed_password" not in text and "argon2" not in text and PASSWORD not in text

    images = [n for n in names if n.startswith("images/")]
    assert len(images) >= 9 * 2 + 2  # originals and thumbnails, plus the cutouts that exist
    assert all(int(n.split("/")[1]) in owned_ids for n in images)
    first = next(i for i in data["items"] if i["image_files"])
    assert set(first["image_files"].values()) <= set(names)
    assert not any(str(theirs.id) + "/" in n for n in names)


async def test_export_of_an_empty_account_is_still_a_valid_zip(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me:
        archive = zipfile.ZipFile(io.BytesIO((await me.get("/account/export")).content))
    data = json.loads(archive.read("data.json"))
    assert data["items"] == [] and data["feedback"] == [] and data["preferences"] is None


# ------------------------------------------------------------------------------ deletion


async def test_deleting_an_account_removes_every_row_and_file(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app, EMAIL) as me, signed_in(auth_app, THEIRS) as them:
        mine, theirs = user_row(session, EMAIL), user_row(session, THEIRS)
        populate(session, storage, mine)
        add_item(session, storage, theirs, name="Keep me")
        orphan = storage.root / str(mine.id) / "stray-file.txt"
        orphan.write_text("left over from a crash")
        mine_id, their_files = (
            mine.id,
            [p for p in stored_files(storage) if f"/{theirs.id}/" in str(p)],
        )
        assert any(f"/{mine_id}/" in str(p) for p in stored_files(storage))

        response = await submit(
            me, "/account/delete", {"password": PASSWORD, "confirm": "DELETE"}, form_page="/account"
        )
        assert response.status_code == 303 and response.headers["location"] == "/login?msg=deleted"
        assert (
            'wardrobe_session=""' in response.headers["set-cookie"]
            or "Max-Age=0" in response.headers["set-cookie"]
        )

        session.expire_all()
        assert session.get(User, mine_id) is None
        for model in (
            Item,
            Outfit,
            OutfitFeedback,
            PlannedOutfit,
            Candidate,
            UserSession,
            PasswordReset,
            UserPreferences,
        ):
            assert count(session, model, mine_id) == 0, model.__name__
        assert not [p for p in stored_files(storage) if f"/{mine_id}/" in str(p)]
        assert not (storage.root / str(mine_id)).exists()

        assert session.get(User, theirs.id) is not None  # the other account is untouched
        assert count(session, Item, theirs.id) == 1
        assert [p for p in stored_files(storage) if f"/{theirs.id}/" in str(p)] == their_files
        assert (await them.get("/closet")).status_code == 200

        again = await me.get("/closet")  # the deleted user's cookie is dead
        assert again.status_code == 303


async def test_deletion_needs_the_password_and_the_word_delete(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app) as me:
        mine = user_row(session, EMAIL)
        add_item(session, storage, mine)
        wrong = await submit(
            me, "/account/delete", {"password": "nope", "confirm": "DELETE"}, form_page="/account"
        )
        assert wrong.status_code == 401
        unconfirmed = await submit(
            me,
            "/account/delete",
            {"password": PASSWORD, "confirm": "delete me"},
            form_page="/account",
        )
        assert unconfirmed.status_code == 422
        assert session.get(User, mine.id) is not None and count(session, Item, mine.id) == 1
        assert len(stored_files(storage)) == 2


async def test_deletion_guesses_are_rate_limited(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me:
        statuses = []
        for _ in range(7):
            response = await submit(
                me,
                "/account/delete",
                {"password": "wrong", "confirm": "DELETE"},
                form_page="/account",
            )
            statuses.append(response.status_code)
        assert statuses[:5] == [401] * 5 and statuses[-1] == 429


async def test_a_deleted_email_can_register_again(auth_app: FastAPI, session: Session) -> None:
    async with signed_in(auth_app) as me:
        await submit(
            me, "/account/delete", {"password": PASSWORD, "confirm": "DELETE"}, form_page="/account"
        )
    async with signed_in(auth_app) as again:
        assert (await again.get("/closet")).status_code == 200


# ------------------------------------------------------------------------------- headers


async def test_security_headers_on_every_kind_of_response(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me:
        responses = [
            await me.get("/closet"),
            await me.get("/health"),
            await me.get("/static/style.css"),
            await me.get("/no-such-page", headers={"Accept": "text/html"}),
            await new_client(auth_app).get("/login"),
        ]
    for response in responses:
        h = response.headers
        assert h["content-security-policy"] == CSP, response.url
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert (
            "camera=(self)" in h["permissions-policy"]
            and "geolocation=()" in h["permissions-policy"]
        )
        assert h["x-frame-options"] == "DENY"
    assert responses[0].headers["cache-control"] == "no-store"  # pages with personal data


def test_the_csp_forbids_inline_and_eval_scripts_and_foreign_origins() -> None:
    directives = dict(part.strip().split(" ", 1) for part in CSP.split(";"))
    assert directives["script-src"] == "'self'"
    assert directives["default-src"] == "'none'" and directives["frame-ancestors"] == "'none'"
    assert "unsafe-eval" not in CSP and "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-inline'" not in directives["style-src"]
    assert directives["connect-src"] == "'self'" and directives["form-action"] == "'self'"
    assert "https://fonts.googleapis.com" in directives["style-src"]
    assert directives["font-src"] == "https://fonts.gstatic.com"


def test_hsts_only_with_secure_cookies() -> None:
    assert "strict-transport-security" not in apply_security_headers(Response(), False).headers
    assert (
        "max-age=" in apply_security_headers(Response(), True).headers["strict-transport-security"]
    )


async def test_there_is_no_cors(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me:
        for request in (
            me.get("/closet", headers={"Origin": "https://evil.example"}),
            me.options(
                "/closet",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            ),
        ):
            response = await request
            assert not any(k.lower().startswith("access-control-") for k in response.headers)
    assert "CORS" not in repr(auth_app.user_middleware)


async def test_the_secure_cookie_setting_is_honoured(auth_app: FastAPI, settings: Settings) -> None:
    secure = settings.model_copy(update={"cookie_secure": True})
    auth_app.dependency_overrides[get_settings] = lambda: secure
    async with new_client(auth_app) as client:
        await sign_up(client)
        response = await submit(client, "/login", {"email": EMAIL, "password": PASSWORD})
        cookie = response.headers["set-cookie"].lower()
        assert response.status_code == 303 and "httponly" in cookie and "samesite=lax" in cookie
        assert "secure" in cookie
