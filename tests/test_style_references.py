"""Reference photos: upload, privacy, the on/off toggle and the extractors."""

import json
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from app.deps import get_style_extractor
from app.models import Outfit, StyleReference, User, UserPreferences
from app.services.storage import LocalDiskStorage
from app.services.style import (
    FakeStyleExtractor,
    OllamaStyleExtractor,
    OpenAICompatStyleExtractor,
    StyleError,
    StyleResult,
)
from tests.conftest import csrf_token, image_bytes, stored_files
from tests.test_outfit_routes import closet


class Scripted:
    """Always reads the same smart, navy look."""

    async def extract(self, image: bytes) -> StyleResult:
        return StyleResult(
            colors=["navy", "white"],
            patterns=["solid"],
            subtypes=["shirt", "chinos", "loafers"],
            summary="Smart navy look.",
        )


@pytest.fixture(autouse=True)
def extractor(app: FastAPI) -> None:
    app.dependency_overrides[get_style_extractor] = lambda: Scripted()


async def upload(client: httpx.AsyncClient, *colors: str) -> httpx.Response:
    files = [("images", (f"{c}.jpg", image_bytes(color=c), "image/jpeg")) for c in colors]
    return await client.post(
        "/settings/style/references",
        data={"csrf_token": await csrf_token(client)},
        files=files,
    )


async def test_upload_stores_photo_and_what_was_read(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    response = await upload(client, "navy", "white")
    assert response.status_code == 303
    rows = list(session.exec(select(StyleReference)))
    assert len(rows) == 2 and all(r.user_id == user_a.id for r in rows)
    assert rows[0].style["colors"] == ["navy", "white"]
    assert len(stored_files(storage)) == 4  # photo + thumbnail each
    page = await client.get("/settings/style")
    assert "Smart navy look." in page.text and "Your style, as read from 2 photos" in page.text


async def test_bad_files_and_failed_reads_are_reported_not_stored(
    client: httpx.AsyncClient, app: FastAPI, session: Session
) -> None:
    junk = await client.post(
        "/settings/style/references",
        data={"csrf_token": await csrf_token(client)},
        files=[("images", ("x.jpg", b"not an image", "image/jpeg"))],
    )
    assert junk.status_code == 422 and "x.jpg" in junk.text

    class Broken:
        async def extract(self, image: bytes) -> StyleResult:
            raise StyleError("The local model took too long to answer.")

    app.dependency_overrides[get_style_extractor] = lambda: Broken()
    slow = await upload(client, "navy")
    assert slow.status_code == 422 and "took too long" in slow.text
    assert list(session.exec(select(StyleReference))) == []


async def test_references_are_private_and_removable(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_b: User,
    as_user: Callable[[User], None],
) -> None:
    await upload(client, "navy")
    mine = session.exec(select(StyleReference)).one()
    as_user(user_b)
    await client.post(
        f"/settings/style/references/{mine.id}/delete",
        data={"csrf_token": await csrf_token(client)},
    )
    assert session.get(StyleReference, mine.id) is not None  # not b's to delete
    assert "Smart navy look." not in (await client.get("/settings/style")).text


async def test_remove_deletes_row_and_files(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage
) -> None:
    await upload(client, "navy")
    mine = session.exec(select(StyleReference)).one()
    response = await client.post(
        f"/settings/style/references/{mine.id}/delete",
        data={"csrf_token": await csrf_token(client)},
    )
    assert response.status_code == 303
    assert list(session.exec(select(StyleReference))) == []
    assert stored_files(storage) == []


def remembered(session: Session, user: User) -> bool:
    prefs = session.get(UserPreferences, user.id)
    assert prefs is not None
    return prefs.use_style_references


async def generate(client: httpx.AsyncClient, **extra: str) -> httpx.Response:
    data = {"csrf_token": await csrf_token(client), "season": "", **extra}
    return await client.post("/outfits/generate", data=data)


async def test_toggle_compares_standard_and_style_ranking(
    client: httpx.AsyncClient, session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    closet(session, storage, user_a)
    off = await generate(client)
    assert "Ranking" not in off.text  # no references yet: nothing to toggle

    await upload(client, "navy")
    off = await generate(client)
    assert "standard rules only" in off.text
    assert all(
        "style" not in ((o.breakdown or {}).get("adjusted_by") or [])
        for o in session.exec(select(Outfit))
    )
    assert remembered(session, user_a) is False

    on = await generate(client, style_ai="1")
    assert "with your reference photos" in on.text
    assert 'name="style_ai" value="1"' in on.text  # Regenerate keeps the choice
    assert remembered(session, user_a) is True
    page = await client.get("/outfits")
    assert 'id="style-ai"' in page.text and "checked" in page.text.split('id="style-ai"')[1][:80]


def test_result_validation_rejects_unknown_vocabulary() -> None:
    ok = {"colors": ["navy"], "patterns": [], "subtypes": ["shirt"]}
    assert StyleResult(**ok).colors == ["navy"]
    for bad in ({"colors": ["fuchsia"]}, {"subtypes": ["cape"]}):
        with pytest.raises(ValueError):
            StyleResult(**{**ok, **bad})


async def test_fake_extractor_is_deterministic() -> None:
    data = image_bytes(color="navy")
    a, b = await FakeStyleExtractor().extract(data), await FakeStyleExtractor().extract(data)
    assert a == b and a.colors


GOOD = {
    "colors": ["navy"],
    "patterns": ["solid"],
    "subtypes": ["blazer"],
    "summary": "ok",
}


async def test_ollama_extractor_sends_the_image_and_schema_and_retries_once() -> None:
    calls: list[dict[str, object]] = []
    answers = [{**GOOD, "colors": ["fuchsia"]}, GOOD]  # first answer breaks the vocabulary

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(answers.pop(0))}})

    extractor = OllamaStyleExtractor("m", transport=httpx.MockTransport(handler))
    result = await extractor.extract(image_bytes())
    assert result.colors == ["navy"] and len(calls) == 2
    first = calls[0]
    assert first["stream"] is False and "properties" in first["format"]  # type: ignore[operator]
    assert first["messages"][1]["images"]  # type: ignore[index]


async def test_ollama_down_is_a_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    extractor = OllamaStyleExtractor("m", transport=httpx.MockTransport(handler))
    with pytest.raises(StyleError, match="ollama serve"):
        await extractor.extract(image_bytes())


async def test_openai_compat_extractor_sends_key_image_and_schema() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = json.dumps(GOOD)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    extractor = OpenAICompatStyleExtractor(
        "gemini-x",
        "https://host.test/v1beta/openai/",
        "k3y",
        transport=httpx.MockTransport(handler),
    )
    result = await extractor.extract(image_bytes())
    assert result.subtypes == ["blazer"]
    request = seen[0]
    assert str(request.url) == "https://host.test/v1beta/openai/chat/completions"
    assert request.headers["authorization"] == "Bearer k3y"
    body = json.loads(request.content)
    assert body["model"] == "gemini-x"
    assert body["response_format"]["json_schema"]["schema"]["required"]
    parts = body["messages"][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_openai_compat_errors_are_readable_and_leak_no_photo() -> None:
    def respond(status: int) -> OpenAICompatStyleExtractor:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": {"message": "bad schema"}})

        return OpenAICompatStyleExtractor(
            "m", "https://host.test", "k3y", transport=httpx.MockTransport(handler)
        )

    with pytest.raises(StyleError, match="rejected the API key"):
        await respond(403).extract(image_bytes())
    with pytest.raises(StyleError, match="rate limit"):
        await respond(429).extract(image_bytes())
    with pytest.raises(StyleError, match="bad schema") as caught:
        await respond(400).extract(image_bytes())
    assert "base64" not in str(caught.value) and "k3y" not in str(caught.value)
