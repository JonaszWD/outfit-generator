from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    if settings.db_serverless:
        # One short-lived connection per request, through the provider's pooler (pgbouncer in
        # transaction mode), which cannot keep server-side prepared statements.
        return create_engine(
            settings.database_url, poolclass=NullPool, connect_args={"prepare_threshold": None}
        )
    return create_engine(settings.database_url, pool_pre_ping=True)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
