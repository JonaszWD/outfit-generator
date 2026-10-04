from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from app.config import get_settings
from app.db import get_session
from app.main import create_app


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
def app(session: Session) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_session] = lambda: session
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
