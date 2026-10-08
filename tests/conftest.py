import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from app.config import Settings, get_settings
from app.db import get_session
from app.deps import (
    get_candidate_deps,
    get_current_user,
    get_email_sender,
    get_pipeline_deps,
    get_storage,
    get_weather_provider,
)
from app.engine.config import EngineConfig
from app.main import create_app
from app.models import User
from app.services.analysis import AnalysisConfig
from app.services.candidates import CandidateDeps
from app.services.pipeline import PipelineDeps
from app.services.storage import LocalDiskStorage
from app.services.tagging import FakeTagger
from app.services.weather import FakeWeatherProvider
from tests.shop import Shop


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatible-API client waits between retries of 503s; tests must not."""
    monkeypatch.setattr("app.services.openai_compat.RETRY_DELAYS", ())


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = get_settings().test_database_url
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set (see .env.example)")
    return url


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    cfg = Config("alembic.ini")
    # configparser treats % as interpolation, so escape it.
    cfg.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    eng = create_engine(test_database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Session inside an outer transaction that is rolled back after each test."""
    with engine.connect() as connection:
        outer = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint") as s:
            yield s
        outer.rollback()


@pytest.fixture
def storage(tmp_path: Path) -> LocalDiskStorage:
    return LocalDiskStorage(tmp_path / "media")


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(database_url=test_database_url, media_root=tmp_path / "media", max_upload_mb=1)


@pytest.fixture
def user_a(session: Session) -> User:
    return make_user(session, "a@example.com")


@pytest.fixture
def user_b(session: Session) -> User:
    return make_user(session, "b@example.com")


def make_user(session: Session, email: str) -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def stub_remover(image: Image.Image) -> Image.Image:
    """Stands in for rembg: makes a 1px border transparent. No model, no network."""
    rgba = image.convert("RGBA")
    w, h = rgba.size
    for x in range(w):
        rgba.putpixel((x, 0), (0, 0, 0, 0))
        rgba.putpixel((x, h - 1), (0, 0, 0, 0))
    return rgba


@pytest.fixture
def pipeline_deps(session: Session, storage: LocalDiskStorage, settings: Settings) -> PipelineDeps:
    @contextmanager
    def shared_session() -> Iterator[Session]:
        yield session  # the test's transactional session; must not be closed

    def factory() -> AbstractContextManager[Session]:
        return shared_session()

    return PipelineDeps(
        settings=settings,
        storage=storage,
        session_factory=factory,
        tagger_factory=FakeTagger,
        remover=stub_remover,
    )


@pytest.fixture
def shop() -> Shop:
    return Shop()


@pytest.fixture
def candidate_deps(pipeline_deps: PipelineDeps, shop: Shop, settings: Settings) -> CandidateDeps:
    return CandidateDeps(
        pipeline=pipeline_deps,
        fetcher_factory=shop.factory(settings),
        engine_config=EngineConfig(),
        analysis_config=AnalysisConfig(),
    )


@pytest.fixture
def weather() -> FakeWeatherProvider:
    """The weather every test app uses: a mild dry day in Oslo, never the network."""
    return FakeWeatherProvider()


@pytest.fixture
def app(
    weather: FakeWeatherProvider,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    pipeline_deps: PipelineDeps,
    candidate_deps: CandidateDeps,
) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_session] = lambda: session
    application.dependency_overrides[get_storage] = lambda: storage
    application.dependency_overrides[get_current_user] = lambda: user_a
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_pipeline_deps] = lambda: pipeline_deps
    application.dependency_overrides[get_candidate_deps] = lambda: candidate_deps
    application.dependency_overrides[get_weather_provider] = lambda: weather
    return application


@pytest.fixture
def as_user(app: FastAPI) -> Callable[[User], None]:
    def switch(user: User) -> None:
        app.dependency_overrides[get_current_user] = lambda: user

    return switch


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def image_bytes(
    fmt: str = "JPEG", size: tuple[int, int] = (120, 80), color: str = "red", **save_kwargs: object
) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt, **save_kwargs)
    return out.getvalue()


def stored_files(storage: LocalDiskStorage) -> list[Path]:
    return sorted(p for p in storage.root.rglob("*") if p.is_file())


VALID_FORM = {
    "category": "top",
    "subtype": "shirt",
    "primary_color": "navy",
    "name": "Navy shirt",
}


async def csrf_token(client: httpx.AsyncClient) -> str:
    """Fetch a page so the middleware sets the cookie, then return its token."""
    if "csrf_token" not in client.cookies:
        await client.get("/closet")
    return client.cookies["csrf_token"]


async def post_form(
    client: httpx.AsyncClient,
    url: str,
    data: Mapping[str, object] | None = None,
    image: bytes | None = None,
    token: str | None = None,
) -> httpx.Response:
    fields = dict(data or {})
    fields["csrf_token"] = token if token is not None else await csrf_token(client)
    files = {"image": ("whatever name.jpg", image, "image/jpeg")} if image is not None else None
    return await client.post(url, data=fields, files=files)


async def post_upload(
    client: httpx.AsyncClient, files: Sequence[tuple[str, bytes]], token: str | None = None
) -> httpx.Response:
    fields = {"csrf_token": token if token is not None else await csrf_token(client)}
    multipart = [("images", (name, data, "image/jpeg")) for name, data in files]
    return await client.post("/items", data=fields, files=multipart)


def item_id_from(response: httpx.Response) -> int:
    match = re.search(r"/items/(\d+)", response.headers["location"])
    assert match
    return int(match.group(1))


# ---------------------------------------------------------------- real sessions


class Outbox:
    """Collects the emails the app would send."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.messages.append((to, subject, body))

    def to(self, address: str) -> list[tuple[str, str, str]]:
        return [m for m in self.messages if m[0] == address]


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture
def auth_app(app: FastAPI, outbox: Outbox) -> FastAPI:
    """The app with *real* authentication: only the database, storage and mail are faked."""
    del app.dependency_overrides[get_current_user]
    app.dependency_overrides[get_email_sender] = lambda: outbox
    return app


def new_client(application: FastAPI) -> httpx.AsyncClient:
    """A browser: its own cookie jar."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


def page_token(html: str) -> str:
    """The CSRF token a rendered page carries (the same one HTMX sends as a header)."""
    match = re.search(r'X-CSRF-Token": "([^"]+)"', html)
    assert match, "page has no CSRF token"
    return match.group(1)


async def form_token(client: httpx.AsyncClient, path: str = "/login") -> str:
    return page_token((await client.get(path)).text)


async def submit(
    client: httpx.AsyncClient,
    path: str,
    data: Mapping[str, object] | None = None,
    *,
    form_page: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """POST a form the way a browser would: load a page for its token first."""
    token = await form_token(client, form_page or path)
    return await client.post(path, data={**(data or {}), "csrf_token": token}, **kwargs)


EMAIL = "me@example.com"
PASSWORD = "correct horse battery"


async def sign_up(
    client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> httpx.Response:
    return await submit(client, "/register", {"email": email, "password": password})


async def log_in(
    client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> httpx.Response:
    return await submit(client, "/login", {"email": email, "password": password})


@asynccontextmanager
async def signed_in(
    application: FastAPI, email: str = EMAIL, password: str = PASSWORD
) -> AsyncIterator[httpx.AsyncClient]:
    """A browser that has registered and logged in."""
    async with new_client(application) as client:
        await sign_up(client, email, password)
        response = await log_in(client, email, password)
        assert response.status_code == 303, response.text
        yield client
