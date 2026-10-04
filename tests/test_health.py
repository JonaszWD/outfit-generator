from collections.abc import Iterator

import httpx
from fastapi import FastAPI
from sqlmodel import Session, create_engine

from app.db import get_session


async def test_health_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "up"}


async def test_health_db_down(app: FastAPI) -> None:
    # Port 1 refuses connections, simulating an unreachable database.
    dead_engine = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )

    def dead_session() -> Iterator[Session]:
        with Session(dead_engine) as s:
            yield s

    app.dependency_overrides[get_session] = dead_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == "down"


async def test_index_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "Wardrobe" in response.text
