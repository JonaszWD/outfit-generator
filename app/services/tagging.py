"""Item tagging: the Tagger interface, Claude and fake implementations, color cross-check."""

import base64
import hashlib
import json
import logging
from io import BytesIO
from typing import Any, Protocol

from PIL import Image
from pydantic import ValidationError, field_validator

from app.config import Settings
from app.engine import vocab
from app.models import Category
from app.schemas import ItemData
from app.services.openai_compat import (
    GEMINI_BASE_URL,
    CompatError,
    chat_json,
    image_message,
)
from app.services.openai_compat import media_type as _media_type

logger = logging.getLogger(__name__)

TAGGED_FIELDS = (
    "category",
    "subtype",
    "primary_color",
    "secondary_colors",
    "pattern",
    "seasons",
    "material",
    "layer_role",
)
# Model and pixel-based colors closer than this (CIE76 delta E) count as agreeing.
COLOR_AGREE_DELTA = 20.0
# A pixel color covering at least this share of the item is a plausible "main" color.
COLOR_MIN_SHARE = 0.25
MISMATCH_CONFIDENCE_CAP = 0.4


class TaggingError(Exception):
    """Tagging failed. The message is safe to store and show to the user."""


class TagResult(ItemData):
    """Validated model output: ItemData (all vocab checks) plus confidence and flags."""

    confidence: dict[str, float]
    flags: list[str] = []

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value: dict[str, float]) -> dict[str, float]:
        missing = [f for f in TAGGED_FIELDS if f not in value]
        if missing:
            raise ValueError(f"confidence is missing fields: {', '.join(missing)}")
        if any(not 0.0 <= v <= 1.0 for v in value.values()):
            raise ValueError("confidence values must be between 0 and 1")
        return {f: float(value[f]) for f in TAGGED_FIELDS}

    def tags(self) -> dict[str, Any]:
        """JSON-safe tag fields (what gets stored in Item.ai_tags)."""
        return self.model_dump(mode="json", include=set(TAGGED_FIELDS))


class Tagger(Protocol):
    async def tag(self, image_bytes: bytes) -> TagResult: ...


def low_confidence_fields(confidence: dict[str, float] | None, threshold: float) -> list[str]:
    if not confidence:
        return []
    return [f for f in TAGGED_FIELDS if confidence.get(f, 1.0) < threshold]


# ---------------------------------------------------------------- color cross-check


def apply_color_check(result: TagResult, dominant: list[tuple[str, float]]) -> TagResult:
    """Compare the model's primary color with pixel analysis.

    On disagreement, lower the confidence for that field and add a flag. Neither answer
    is overridden: the user decides in the review form.
    """
    if not dominant:
        return result
    plausible = [
        name for i, (name, share) in enumerate(dominant) if i == 0 or share >= COLOR_MIN_SHARE
    ]
    closest = min(vocab.color_distance(result.primary_color, name) for name in plausible)
    if closest <= COLOR_AGREE_DELTA:
        return result
    confidence = dict(result.confidence)
    confidence["primary_color"] = min(confidence["primary_color"] * 0.5, MISMATCH_CONFIDENCE_CAP)
    flag = f"Pixel analysis suggests {dominant[0][0]}, but the tagger said {result.primary_color}."
    return result.model_copy(update={"confidence": confidence, "flags": [*result.flags, flag]})


# ---------------------------------------------------------------------- fake tagger


class FakeTagger:
    """Deterministic, offline tagger for tests and local development."""

    async def tag(self, image_bytes: bytes) -> TagResult:
        digest = hashlib.sha256(image_bytes).digest()
        categories = list(Category)
        category = categories[digest[0] % len(categories)]
        subtypes = vocab.SUBTYPES[category]
        subtype = subtypes[digest[1] % len(subtypes)]
        try:
            with Image.open(BytesIO(image_bytes)) as src:
                rgb = src.convert("RGB")
            w, h = rgb.size
            centre = rgb.crop((w // 3, h // 3, 2 * w // 3 + 1, 2 * h // 3 + 1)).resize((1, 1))
            primary = vocab.nearest_color(centre.getpixel((0, 0)))  # type: ignore[arg-type]
        except OSError:
            primary = vocab.COLOR_NAMES[digest[2] % len(vocab.COLOR_NAMES)]
        start = digest[3] % 4
        seasons = [vocab.SEASONS[(start + i) % 4] for i in range(1 + digest[4] % 3)]
        confidence = dict.fromkeys(TAGGED_FIELDS, 0.9)
        if digest[5] % 4 == 0:
            confidence["subtype"] = 0.55  # so the review UI has something to highlight
        return TagResult(
            category=category,
            subtype=subtype,
            primary_color=primary,
            secondary_colors=[],
            pattern="solid",
            seasons=seasons,
            material=vocab.MATERIALS[digest[7] % len(vocab.MATERIALS)],
            layer_role={"top": "base", "outerwear": "outer"}.get(category.value),
            confidence=confidence,
        )


# --------------------------------------------------------------------- claude tagger


def _enum(values: tuple[str, ...] | list[Any], nullable: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["string", "null"] if nullable else "string"}
    schema["enum"] = [*values, None] if nullable else list(values)
    return schema


def tag_schema() -> dict[str, Any]:
    """JSON Schema sent to the model; built from vocab.py so the two cannot drift."""
    colors = vocab.COLOR_NAMES
    properties: dict[str, Any] = {
        "category": _enum([c.value for c in Category]),
        "subtype": {"type": "string"},
        "primary_color": _enum(colors),
        "secondary_colors": {"type": "array", "items": _enum(colors)},
        "pattern": _enum(vocab.PATTERNS),
        "seasons": {"type": "array", "items": _enum(vocab.SEASONS)},
        "material": _enum(vocab.MATERIALS, nullable=True),
        "layer_role": _enum(vocab.LAYER_ROLES, nullable=True),
        "confidence": {
            "type": "object",
            "properties": {f: {"type": "number"} for f in TAGGED_FIELDS},
            "required": list(TAGGED_FIELDS),
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def system_prompt() -> str:
    subtypes = "\n".join(
        f"- {category.value}: {', '.join(options)}" for category, options in vocab.SUBTYPES.items()
    )
    return f"""You tag photos of single clothing items for a wardrobe app.
Describe the main garment in the photo. Ignore the background, hangers and people.
Use only the allowed values below; pick the closest one.

Subtypes, which must belong to the chosen category:
{subtypes}

Colors (primary_color is the dominant color; secondary_colors are other clearly visible
colors, empty if none): {", ".join(vocab.COLOR_NAMES)}
Patterns: {", ".join(vocab.PATTERNS)}
Seasons the item is suitable for (one or more): {", ".join(vocab.SEASONS)}
Materials (null if you cannot tell): {", ".join(vocab.MATERIALS)}
Layer roles (null for shoes, accessories and dresses): {", ".join(vocab.LAYER_ROLES)}

For every field give a confidence from 0 to 1 that honestly reflects how sure you are from
the photo alone. Material is often hard to see, so use a lower value when you are guessing."""


class ClaudeTagger:
    """Vision tagging through the Anthropic SDK with schema-constrained JSON output.

    Transient API errors (429, 5xx, connection) are retried with exponential backoff by
    the SDK itself (`max_retries`). Output that fails vocabulary validation is retried
    once with the validation error appended, then raises TaggingError.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        max_retries: int = 4,
        client: Any = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._client = client

    def _make_client(self) -> Any:
        import anthropic

        return anthropic.AsyncAnthropic(api_key=self._api_key, max_retries=self._max_retries)

    async def tag(self, image_bytes: bytes) -> TagResult:
        client = self._client or self._make_client()
        try:
            return await self._tag(client, image_bytes)
        finally:
            if self._client is None:
                await client.close()

    async def _request(self, client: Any, messages: list[dict[str, Any]]) -> str:
        import anthropic

        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=system_prompt(),
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": tag_schema()}},
            )
        except anthropic.APIStatusError as exc:
            # Never include request content; the status and request id are enough.
            raise TaggingError(
                f"Tagging API error {exc.status_code} (request {getattr(exc, 'request_id', None)})"
            ) from None
        except anthropic.APIConnectionError:
            raise TaggingError("Could not reach the tagging API.") from None
        if response.stop_reason == "refusal":
            raise TaggingError("The model declined to tag this image.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text.strip():
            raise TaggingError("The model returned no tags.")
        return str(text)

    async def _tag(self, client: Any, image_bytes: bytes) -> TagResult:
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _media_type(image_bytes),
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        }
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [image_block, {"type": "text", "text": "Tag this clothing item."}],
            }
        ]
        raw = await self._request(client, messages)
        try:
            return self._parse(raw)
        except (ValidationError, ValueError) as first_error:
            messages += [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "That output failed validation:\n"
                    f"{_describe(first_error)}\nReturn corrected JSON that fixes these problems.",
                },
            ]
        raw = await self._request(client, messages)
        try:
            return self._parse(raw)
        except (ValidationError, ValueError) as second_error:
            logger.warning("Tagger output invalid after retry: %s", _describe(second_error))
            raise TaggingError("The model's tags did not pass validation twice.") from None

    @staticmethod
    def _parse(raw: str) -> TagResult:
        return TagResult.model_validate(json.loads(raw))


class OpenAICompatTagger:
    """Vision tagging through any OpenAI-compatible host (Gemini by default; see
    build_tagger). Same schema, validation and single retry as ClaudeTagger. The photo is sent
    to that host."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        transport: Any = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self.timeout = timeout
        self._transport = transport  # tests pass httpx.MockTransport

    async def _ask(self, messages: list[dict[str, Any]]) -> str:
        try:
            return await chat_json(
                label="tagging API",
                base_url=self.base_url,
                api_key=self._api_key,
                model=self.model,
                system=system_prompt(),
                messages=messages,
                schema=tag_schema(),
                schema_name="tags",
                timeout=self.timeout,
                transport=self._transport,
            )
        except CompatError as exc:
            raise TaggingError(str(exc)) from None

    async def tag(self, image_bytes: bytes) -> TagResult:
        messages = [image_message("Tag this clothing item.", image_bytes)]
        raw = await self._ask(messages)
        try:
            return TagResult.model_validate(json.loads(raw))
        except (ValidationError, ValueError) as first_error:
            messages += [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "That output failed validation:\n"
                    f"{_describe(first_error)}\nReturn corrected JSON that fixes these problems.",
                },
            ]
        raw = await self._ask(messages)
        try:
            return TagResult.model_validate(json.loads(raw))
        except (ValidationError, ValueError) as second_error:
            logger.warning("Tagger output invalid after retry: %s", _describe(second_error))
            raise TaggingError("The model's tags did not pass validation twice.") from None


def _describe(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'output'}: {e['msg']}" for e in error.errors()
        )
    return str(error)


def build_tagger(settings: Settings) -> Tagger:
    if settings.tagger_backend == "fake":
        return FakeTagger()
    if settings.tagger_backend == "gemini":
        if settings.gemini_api_key is None:
            raise TaggingError("TAGGER_BACKEND=gemini but GEMINI_API_KEY is not set.")
        return OpenAICompatTagger(
            settings.gemini_model,
            GEMINI_BASE_URL,
            settings.gemini_api_key.get_secret_value(),
            timeout=settings.ai_timeout_seconds,
        )
    if settings.anthropic_api_key is None:
        raise TaggingError("TAGGER_BACKEND=claude but ANTHROPIC_API_KEY is not set.")
    return ClaudeTagger(
        model=settings.tagger_model,
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=settings.tagger_max_retries,
    )
