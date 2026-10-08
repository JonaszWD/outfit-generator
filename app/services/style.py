"""Style references: read a taste profile from inspiration photos.

A `StyleExtractor` turns one photo into a small structured `StyleResult` (colors,
patterns, garment types) validated against the same vocabulary the closet uses. Only that
summary is stored next to the photo; the engine (`app/engine/style.py`) merges the summaries
into a profile that re-ranks suggestions. Backends: `fake` (offline), `claude` (Anthropic API)
and `ollama` (a local vision model, nothing leaves the machine).
"""

import base64
import hashlib
import json
import logging
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError, field_validator
from sqlmodel import Session, col, select

from app.config import Settings
from app.engine import vocab
from app.engine.style import StyleProfile, StyleSample, build_profile
from app.models import StyleReference, User, UserPreferences
from app.services.imaging import ProcessedImage, dominant_colors
from app.services.openai_compat import (
    GEMINI_BASE_URL,
    CompatError,
    chat_json,
    image_message,
)
from app.services.openai_compat import media_type as _media_type
from app.services.storage import Storage, new_image_keys, thumb_key
from app.services.tagging import _describe, _enum

logger = logging.getLogger(__name__)

MAX_REFERENCES = 12
ALL_SUBTYPES: tuple[str, ...] = tuple(
    dict.fromkeys(s for opts in vocab.SUBTYPES.values() for s in opts)
)


class StyleError(Exception):
    """Reading a reference failed. The message is safe to show to the user."""


class StyleResult(BaseModel):
    """Validated model output. Unknown colors, patterns and garment types are rejected."""

    colors: list[str]
    patterns: list[str]
    subtypes: list[str]
    summary: str = ""

    @field_validator("colors")
    @classmethod
    def _colors(cls, value: list[str]) -> list[str]:
        bad = [c for c in value if c not in vocab.COLOR_NAMES]
        if bad or not 1 <= len(value) <= 5:
            raise ValueError(f"colors must be 1-5 palette colors (got {value})")
        return list(dict.fromkeys(value))

    @field_validator("patterns")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        bad = [p for p in value if p not in vocab.PATTERNS]
        if bad:
            raise ValueError(f"unknown patterns: {bad}")
        return list(dict.fromkeys(value))

    @field_validator("subtypes")
    @classmethod
    def _subtypes(cls, value: list[str]) -> list[str]:
        bad = [s for s in value if s not in ALL_SUBTYPES]
        if bad:
            raise ValueError(f"unknown garment types: {bad}")
        return list(dict.fromkeys(value))

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return value.strip()[:200]

    def sample(self) -> StyleSample:
        return StyleSample(tuple(self.colors), tuple(self.patterns), tuple(self.subtypes))


class StyleExtractor(Protocol):
    async def extract(self, image_bytes: bytes) -> StyleResult: ...


# ---------------------------------------------------------------------------- prompt


def style_schema() -> dict[str, Any]:
    """JSON Schema for the model's answer, built from vocab.py so the two cannot drift."""
    return {
        "type": "object",
        "properties": {
            "colors": {"type": "array", "items": _enum(vocab.COLOR_NAMES)},
            "patterns": {"type": "array", "items": _enum(vocab.PATTERNS)},
            "subtypes": {"type": "array", "items": _enum(ALL_SUBTYPES)},
            "summary": {"type": "string"},
        },
        "required": ["colors", "patterns", "subtypes", "summary"],
        "additionalProperties": False,
    }


def system_prompt() -> str:
    return f"""You read the style of an outfit photo for a wardrobe app.
The photo shows a styled outfit (a person, a flat lay or a mood board). Describe the outfit,
not the background. Use only the allowed values below; pick the closest ones.

colors: the 1 to 4 main colors of the clothes, most prominent first.
Allowed: {", ".join(vocab.COLOR_NAMES)}
patterns: patterns visible on the clothes (use "solid" if there are none).
Allowed: {", ".join(vocab.PATTERNS)}
subtypes: every garment you can see, using only: {", ".join(ALL_SUBTYPES)}
summary: one short sentence describing the look (under 160 characters)."""


USER_PROMPT = "Read the style of this outfit."


def _parse(raw: str) -> StyleResult:
    return StyleResult.model_validate(json.loads(raw))


async def _extract_with_retry(ask: Any, image_bytes: bytes) -> StyleResult:
    """`ask(messages)` returns the model's raw text. One retry with the validation error."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": USER_PROMPT}]
    raw = await ask(messages, image_bytes)
    try:
        return _parse(raw)
    except (ValidationError, ValueError) as first:
        messages += [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": f"That output failed validation:\n{_describe(first)}\n"
                "Return corrected JSON that fixes these problems.",
            },
        ]
    raw = await ask(messages, image_bytes)
    try:
        return _parse(raw)
    except (ValidationError, ValueError) as second:
        logger.warning("Style output invalid after retry: %s", _describe(second))
        raise StyleError("The model's answer did not pass validation twice.") from None


# --------------------------------------------------------------------------- backends


class FakeStyleExtractor:
    """Deterministic and offline: palette from the pixels, the rest from a hash."""

    async def extract(self, image_bytes: bytes) -> StyleResult:
        digest = hashlib.sha256(image_bytes).digest()
        try:
            colors = [name for name, _ in dominant_colors(image_bytes, top=3)]
        except OSError:
            colors = []
        if not colors:
            colors = [vocab.COLOR_NAMES[digest[0] % len(vocab.COLOR_NAMES)]]
        picks = [ALL_SUBTYPES[digest[k] % len(ALL_SUBTYPES)] for k in (1, 2, 3)]
        return StyleResult(
            colors=colors,
            patterns=["solid"],
            subtypes=list(dict.fromkeys(picks)),
            summary="Placeholder style (offline backend).",
        )


class ClaudeStyleExtractor:
    """Vision model through the Anthropic SDK with schema-constrained JSON output."""

    def __init__(
        self, model: str, api_key: str | None = None, max_retries: int = 4, client: Any = None
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._client = client

    async def extract(self, image_bytes: bytes) -> StyleResult:
        import anthropic

        client: Any = self._client or anthropic.AsyncAnthropic(
            api_key=self._api_key, max_retries=self._max_retries
        )

        async def ask(messages: list[dict[str, Any]], image: bytes) -> str:
            first = messages[0]
            if first["role"] == "user" and isinstance(first["content"], str):
                first = {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": _media_type(image),
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": first["content"]},
                    ],
                }
            try:
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=1000,
                    system=system_prompt(),
                    messages=[first, *messages[1:]],
                    output_config={"format": {"type": "json_schema", "schema": style_schema()}},
                )
            except anthropic.APIStatusError as exc:
                raise StyleError(f"Style API error {exc.status_code}") from None
            except anthropic.APIConnectionError:
                raise StyleError("Could not reach the style API.") from None
            if response.stop_reason == "refusal":
                raise StyleError("The model declined to read this image.")
            text = next((b.text for b in response.content if b.type == "text"), "")
            if not text.strip():
                raise StyleError("The model returned nothing.")
            return str(text)

        try:
            return await _extract_with_retry(ask, image_bytes)
        finally:
            if self._client is None:
                await client.close()


class OllamaStyleExtractor:
    """A local vision model served by Ollama. The photo never leaves this machine.

    Uses Ollama's structured outputs (`format` = JSON Schema). Needs a vision-capable model,
    e.g. `ollama pull qwen2.5vl:7b`.
    """

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: float = 180.0,
        transport: Any = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport  # tests pass httpx.MockTransport

    async def extract(self, image_bytes: bytes) -> StyleResult:
        import httpx

        encoded = base64.standard_b64encode(image_bytes).decode("ascii")

        async def ask(messages: list[dict[str, Any]], _image: bytes) -> str:
            payload = {
                "model": self.model,
                "stream": False,
                "format": style_schema(),
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system_prompt()},
                    {**messages[0], "images": [encoded]},
                    *messages[1:],
                ],
            }
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self._transport
                ) as client:
                    response = await client.post(f"{self.base_url}/api/chat", json=payload)
            except httpx.TimeoutException:
                raise StyleError("The local model took too long to answer.") from None
            except httpx.HTTPError:
                raise StyleError(
                    "Could not reach the local model. Is Ollama running (`ollama serve`)?"
                ) from None
            if response.status_code != 200:
                raise StyleError(
                    f"Local model error {response.status_code}. "
                    f"Is `{self.model}` installed (`ollama pull {self.model}`)?"
                )
            return str(response.json().get("message", {}).get("content", ""))

        return await _extract_with_retry(ask, image_bytes)


class OpenAICompatStyleExtractor:
    """Any host that speaks the OpenAI chat API with JSON-schema output: Gemini's compatibility
    endpoint, OpenAI, Together, Fireworks, OpenRouter. The photo goes to that host."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        transport: Any = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._transport = transport  # tests pass httpx.MockTransport

    async def extract(self, image_bytes: bytes) -> StyleResult:
        async def ask(messages: list[dict[str, Any]], image: bytes) -> str:
            sent = [image_message(messages[0]["content"], image), *messages[1:]]
            try:
                return await chat_json(
                    label="style API",
                    base_url=self.base_url,
                    api_key=self._api_key,
                    model=self.model,
                    system=system_prompt(),
                    messages=sent,
                    schema=style_schema(),
                    schema_name="style",
                    timeout=self.timeout,
                    transport=self._transport,
                )
            except CompatError as exc:
                raise StyleError(str(exc)) from None

        return await _extract_with_retry(ask, image_bytes)


def build_style_extractor(settings: Settings) -> StyleExtractor:
    if settings.style_backend == "fake":
        return FakeStyleExtractor()
    if settings.style_backend == "gemini":
        if settings.gemini_api_key is None:
            raise StyleError("STYLE_BACKEND=gemini but GEMINI_API_KEY is not set.")
        return OpenAICompatStyleExtractor(
            settings.gemini_model,
            GEMINI_BASE_URL,
            settings.gemini_api_key.get_secret_value(),
            timeout=settings.ai_timeout_seconds,
        )
    if settings.style_backend == "openai":
        if settings.style_base_url is None or settings.style_api_key is None:
            raise StyleError("STYLE_BACKEND=openai needs STYLE_BASE_URL and STYLE_API_KEY.")
        return OpenAICompatStyleExtractor(
            settings.style_model,
            settings.style_base_url,
            settings.style_api_key.get_secret_value(),
            timeout=settings.ai_timeout_seconds,
        )
    if settings.style_backend == "ollama":
        return OllamaStyleExtractor(
            settings.ollama_model, settings.ollama_url, settings.ollama_timeout_seconds
        )
    if settings.anthropic_api_key is None:
        raise StyleError("STYLE_BACKEND=claude but ANTHROPIC_API_KEY is not set.")
    return ClaudeStyleExtractor(
        model=settings.style_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=settings.tagger_max_retries,
    )


# ----------------------------------------------------------------------------- storage


def list_references(session: Session, user: User) -> list[StyleReference]:
    return list(
        session.exec(
            select(StyleReference)
            .where(StyleReference.user_id == user.id)
            .order_by(col(StyleReference.id))
        )
    )


def save_reference(
    session: Session, storage: Storage, user: User, image: ProcessedImage, result: StyleResult
) -> StyleReference:
    assert user.id is not None
    key, thumb = new_image_keys(user.id)
    storage.save(key, image.main)
    storage.save(thumb, image.thumb)
    row = StyleReference(user_id=user.id, image_path=key, style=result.model_dump(mode="json"))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_reference(session: Session, storage: Storage, user: User, reference_id: int) -> bool:
    row = session.exec(
        select(StyleReference).where(
            StyleReference.id == reference_id, StyleReference.user_id == user.id
        )
    ).first()
    if row is None:
        return False
    storage.delete(row.image_path)
    storage.delete(thumb_key(row.image_path))
    session.delete(row)
    session.commit()
    return True


def delete_all_references(session: Session, storage: Storage, user: User) -> None:
    for row in list_references(session, user):
        storage.delete(row.image_path)
        storage.delete(thumb_key(row.image_path))
        session.delete(row)
    session.commit()


def profile_from(references: list[StyleReference]) -> StyleProfile | None:
    samples = []
    for row in references:
        try:
            samples.append(StyleResult.model_validate(row.style).sample())
        except ValidationError:  # vocabulary changed since it was stored: skip, don't crash
            logger.warning("Skipping unreadable style reference %s", row.id)
    return build_profile(samples)


def profile_for(session: Session, user: User) -> StyleProfile | None:
    return profile_from(list_references(session, user))


def remember_choice(session: Session, user: User, enabled: bool) -> None:
    """Keep the on/off choice, so the toggle on the Outfits page starts where it was left."""
    prefs = session.get(UserPreferences, user.id)
    if prefs is not None and prefs.use_style_references == enabled:
        return
    if prefs is None:
        assert user.id is not None
        prefs = UserPreferences(user_id=user.id)
    prefs.use_style_references = enabled
    session.add(prefs)
    session.commit()
