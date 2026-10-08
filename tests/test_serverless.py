"""What running on a serverless host needs: remote storage, closed registration, processing
inside a request, no cutouts, and a fresh database connection per request."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.pool import NullPool
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import Item, TaggingStatus, User
from app.routes.auth import client_ip
from app.services import openai_compat
from app.services.imaging import dominant_colors, process_image
from app.services.items import create_pending_item
from app.services.pipeline import PipelineDeps, process_item
from app.services.storage import (
    LocalDiskStorage,
    S3Storage,
    SupabaseStorage,
    build_storage,
)
from tests.conftest import csrf_token, image_bytes, stored_files

# ------------------------------------------------------------------ Supabase storage


class FakeSupabase:
    """The few Storage REST endpoints the app uses, over an in-memory bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/storage/v1")
        if request.method == "POST" and path.startswith("/object/list/"):
            prefix = json.loads(request.content)["prefix"] + "/"
            names = [{"name": k.removeprefix(prefix)} for k in self.objects if k.startswith(prefix)]
            return httpx.Response(200, json=names)
        if request.method == "POST" and path.startswith("/object/wardrobe/"):
            self.objects[path.removeprefix("/object/wardrobe/")] = request.content
            return httpx.Response(200, json={"Key": "ok"})
        if request.method == "GET" and path.startswith("/object/authenticated/wardrobe/"):
            key = path.removeprefix("/object/authenticated/wardrobe/")
            if key not in self.objects:
                return httpx.Response(400, json={"error": "not_found"})
            return httpx.Response(200, content=self.objects[key])
        if request.method == "DELETE" and path == "/object/wardrobe":
            for key in json.loads(request.content)["prefixes"]:
                self.objects.pop(key, None)
            return httpx.Response(200, json=[])
        return httpx.Response(500)


def supabase() -> tuple[SupabaseStorage, FakeSupabase]:
    api = FakeSupabase()
    store = SupabaseStorage(
        "https://p.supabase.co", "service-key", "wardrobe", transport=httpx.MockTransport(api)
    )
    return store, api


def test_supabase_storage_round_trip_and_privacy_headers() -> None:
    store, api = supabase()
    store.save("3/" + "a" * 32 + ".jpg", b"photo")
    assert store.read("3/" + "a" * 32 + ".jpg") == b"photo"
    assert api.requests[0].headers["authorization"] == "Bearer service-key"
    assert api.requests[0].headers["content-type"] == "image/jpeg"
    # reads use the authenticated endpoint, never the public one
    assert "/object/authenticated/" in str(api.requests[1].url)
    store.delete("3/" + "a" * 32 + ".jpg")
    with pytest.raises(FileNotFoundError):
        store.read("3/" + "a" * 32 + ".jpg")
    assert store.url_for("3/x.jpg") == "/media/3/x.jpg"  # always through the login-checked route


def test_supabase_delete_prefix_removes_a_whole_user_folder() -> None:
    store, api = supabase()
    for name in ("a", "b", "c"):
        store.save(f"3/{name}.jpg", b"x")
    store.save("4/keep.jpg", b"x")
    assert store.delete_prefix("3") == 3
    assert list(api.objects) == ["4/keep.jpg"]


def test_remote_storage_rejects_dangerous_keys_and_hides_the_url_on_failure() -> None:
    store, _ = supabase()
    for key in ("../x", "/abs.jpg", "3/../4/x.jpg", ""):
        with pytest.raises(ValueError):
            store.save(key, b"x")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    down = SupabaseStorage(
        "https://secret-project.supabase.co", "k", "b", httpx.MockTransport(boom)
    )
    with pytest.raises(OSError) as caught:
        down.read("3/x.jpg")
    assert "secret-project" not in str(caught.value)


# ------------------------------------------------------------------------ S3 storage


class FakeObject:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class FakeMinio:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, bucket: str, key: str, data: Any, length: int, content_type: str) -> None:
        self.objects[key] = data.read()
        assert length == len(self.objects[key]) and content_type.startswith("image/")

    def get_object(self, bucket: str, key: str) -> FakeObject:
        from minio.error import S3Error

        if key not in self.objects:
            raise S3Error(None, "NoSuchKey", "missing", key, "req", "host")  # type: ignore[arg-type]
        return FakeObject(self.objects[key])

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop(key, None)

    def list_objects(self, bucket: str, prefix: str, recursive: bool) -> list[Any]:
        return [type("O", (), {"object_name": k}) for k in self.objects if k.startswith(prefix)]


def test_s3_storage_round_trip_missing_key_and_prefix_delete() -> None:
    client = FakeMinio()
    store = S3Storage(client, "bucket")
    store.save("3/a.png", b"png")
    store.save("3/b.jpg", b"jpg")
    store.save("4/c.jpg", b"jpg")
    assert store.read("3/a.png") == b"png"
    with pytest.raises(FileNotFoundError):
        store.read("3/missing.jpg")
    assert store.delete_prefix("3") == 2
    assert list(client.objects) == ["4/c.jpg"]


def test_build_storage_picks_the_backend_and_names_missing_settings(settings: Settings) -> None:
    assert isinstance(build_storage(settings), LocalDiskStorage)
    remote = settings.model_copy(
        update={
            "storage_backend": "supabase",
            "supabase_url": "https://p.supabase.co",
            "supabase_service_key": SecretStr("k"),
        }
    )
    assert isinstance(build_storage(remote), SupabaseStorage)
    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_KEY"):
        build_storage(settings.model_copy(update={"storage_backend": "supabase"}))
    with pytest.raises(RuntimeError, match="S3_ENDPOINT"):
        build_storage(settings.model_copy(update={"storage_backend": "s3"}))
    s3 = settings.model_copy(
        update={
            "storage_backend": "s3",
            "s3_endpoint": "acct.r2.cloudflarestorage.com",
            "s3_bucket": "b",
            "s3_access_key": "a",
            "s3_secret_key": SecretStr("s"),
        }
    )
    assert isinstance(build_storage(s3), S3Storage)


# ------------------------------------------------------------ closed registration


def use_settings(app: FastAPI, settings: Settings, **changes: Any) -> Settings:
    changed = settings.model_copy(update=changes)
    app.dependency_overrides[get_settings] = lambda: changed
    return changed


async def register(client: httpx.AsyncClient, email: str, **extra: str) -> httpx.Response:
    data = {"csrf_token": await csrf_token(client), "email": email, "password": "a-long-password-1"}
    return await client.post("/register", data={**data, **extra})


async def test_registration_is_closed_in_production_without_an_invite_code(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, session: Session
) -> None:
    use_settings(app, settings, env="production", secret_key=SecretStr("x" * 40))
    page = await client.get("/register")
    assert "by invitation only" in page.text and 'name="password"' not in page.text
    response = await register(client, "new@example.com")
    assert response.status_code == 403
    assert session.exec(select(User).where(User.email == "new@example.com")).first() is None


async def test_invite_code_gates_registration(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings, session: Session
) -> None:
    use_settings(
        app,
        settings,
        env="production",
        secret_key=SecretStr("x" * 40),
        invite_code=SecretStr("open-sesame"),
    )
    assert 'name="invite"' in (await client.get("/register")).text
    wrong = await register(client, "friend@example.com", invite="nope")
    assert wrong.status_code == 422 and "invite code" in wrong.text
    assert session.exec(select(User).where(User.email == "friend@example.com")).first() is None
    ok = await register(client, "friend@example.com", invite="open-sesame")
    assert ok.status_code == 303
    assert session.exec(select(User).where(User.email == "friend@example.com")).first()


async def test_registration_can_be_forced_either_way(
    client: httpx.AsyncClient, app: FastAPI, settings: Settings
) -> None:
    use_settings(app, settings, registration_open=False)  # development, but switched off
    assert (await register(client, "a@example.com")).status_code == 403
    use_settings(
        app, settings, env="production", registration_open=True, secret_key=SecretStr("x" * 40)
    )
    assert (await register(client, "b@example.com")).status_code == 303


# ------------------------------------------------------------------- proxy addresses


def test_client_ip_only_trusts_forwarded_for_when_told_to() -> None:
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")],
        "client": ("10.1.1.1", 1234),
    }
    request = Request(scope)
    assert client_ip(request) == "10.1.1.1"  # a client could send this header itself
    assert client_ip(request, trust_proxy=True) == "203.0.113.9"
    assert client_ip(Request({"type": "http", "headers": [], "client": None}), True) == "unknown"


# ------------------------------------------------------- processing inside a request


def pending(session: Session, storage: LocalDiskStorage, user: User) -> Item:
    return create_pending_item(
        session, storage, user, process_image(image_bytes(color="#1f2a44", size=(90, 120)), 10**7)
    )


async def test_process_route_tags_the_item_and_returns_its_card(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = pending(session, storage, user_a)
    response = await client.post(
        f"/items/{item.id}/process", data={"csrf_token": await csrf_token(client)}
    )
    assert response.status_code == 200 and f'id="item-{item.id}"' in response.text
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE
    assert "hx-post" not in response.text  # a finished card does not ask again
    again = await client.post(
        f"/items/{item.id}/process", data={"csrf_token": await csrf_token(client)}
    )
    assert again.status_code == 200  # a second call claims nothing and is harmless


async def test_process_route_needs_csrf_and_ownership(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    item = pending(session, storage, user_a)
    assert (await client.post(f"/items/{item.id}/process", data={})).status_code == 403
    as_user(user_b)
    other = await client.post(
        f"/items/{item.id}/process", data={"csrf_token": await csrf_token(client)}
    )
    assert other.status_code == 404


async def test_pending_cards_ask_for_their_own_processing_only_in_request_mode(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = pending(session, storage, user_a)
    page = await client.get(f"/items/{item.id}/status")
    assert f'hx-get="/items/{item.id}/status"' in page.text  # background mode: poll
    monkeypatch.setattr(
        "app.templating.get_settings", lambda: Settings(database_url="x", process_in_request=True)
    )
    page = await client.get(f"/items/{item.id}/status")
    assert f'hx-post="/items/{item.id}/process"' in page.text and 'hx-trigger="load"' in page.text
    assert "every 2s" not in page.text


async def test_upload_in_request_mode_leaves_items_pending_for_their_cards(
    client: httpx.AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: Session,
    user_a: User,
) -> None:
    use_settings(app, settings, process_in_request=True)
    response = await client.post(
        "/items",
        data={"csrf_token": await csrf_token(client)},
        files=[("images", ("a.jpg", image_bytes(), "image/jpeg"))],
    )
    assert response.status_code == 303
    item = session.exec(select(Item)).one()
    assert item.tagging_status == TaggingStatus.PENDING  # nothing ran in the background


# ------------------------------------------------------------------- no cutouts


def test_without_a_remover_the_original_is_kept_and_the_backdrop_is_ignored(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending(session, storage, user_a)
    process_item(item.id, replace(pipeline_deps, remover=None))  # type: ignore[arg-type]
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE and item.cutout_image_path is None
    assert not [p for p in stored_files(storage) if "cutout" in p.name]
    flags = (item.ai_tags or {}).get("flags", [])
    assert not any("Background removal" in f for f in flags)  # not a failure, a setting


def test_dominant_colors_can_ignore_a_white_studio_backdrop() -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (200, 200), "white")
    image.paste(Image.new("RGB", (80, 100), "#1f2a44"), (60, 50))
    out = BytesIO()
    image.save(out, format="PNG")
    assert dominant_colors(out.getvalue())[0][0] in {"white", "cream", "grey"}
    assert dominant_colors(out.getvalue(), drop_background=True)[0][0] in {
        "navy",
        "charcoal",
        "black",
    }


# ------------------------------------------------------------ database and API retries


def test_serverless_database_uses_no_pool_and_no_prepared_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import db

    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/x", db_serverless=True)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    try:
        engine = db.get_engine()
        assert isinstance(engine.pool, NullPool)
    finally:
        db.get_engine.cache_clear()


async def test_retry_after_from_the_server_is_honored_but_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_compat, "RETRY_DELAYS", (1.0, 1.0))
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    answers = iter([(429, {"retry-after": "7"}), (429, {"retry-after": "999"}), (200, {})])

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers = next(answers)
        body = {"choices": [{"message": {"content": "{}"}}]} if status == 200 else {}
        return httpx.Response(status, json=body, headers=headers)

    text = await openai_compat.chat_json(
        label="x",
        base_url="https://h.test",
        api_key="k",
        model="m",
        system="s",
        messages=[],
        schema={},
        schema_name="n",
        transport=httpx.MockTransport(handler),
    )
    assert text == "{}" and slept == [7.0, openai_compat.MAX_RETRY_AFTER]


def test_get_settings_still_defaults_to_the_laptop_setup() -> None:
    s = Settings(database_url="x")
    assert (s.storage_backend, s.cutout_backend, s.process_in_request, s.db_serverless) == (
        "local",
        "rembg",
        False,
        False,
    )


def test_production_configuration_is_checked_for_a_serverless_host() -> None:
    from app.main import production_problems

    base = Settings(database_url="x", env="production", secret_key=SecretStr("x" * 40))
    assert production_problems(base.model_copy(update={"env": "development"}), True) == ([], [])
    errors, warnings = production_problems(base, serverless=True)
    assert len(errors) == 2 and "STORAGE_BACKEND" in errors[0] and "CUTOUT_BACKEND" in errors[1]
    assert any("TAGGER_BACKEND" in w for w in warnings)
    assert any("DB_SERVERLESS" in w for w in warnings)
    ready = base.model_copy(
        update={
            "storage_backend": "supabase",
            "cutout_backend": "none",
            "tagger_backend": "gemini",
            "email_backend": "smtp",
            "app_base_url": "https://wardrobe.example.com",
            "db_serverless": True,
            "trust_proxy_headers": True,
        }
    )
    assert production_problems(ready, serverless=True) == ([], [])
    # on a normal server, local disk and rembg are fine
    assert production_problems(base, serverless=False)[0] == []
