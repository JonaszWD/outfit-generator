"""Validated item input and closet query filters (no DB access)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import ErrorDetails

from app.engine import vocab
from app.models import Category

REQUIRED_FIELDS = ("category", "subtype", "primary_color")
OPTIONAL_TEXT_FIELDS = ("pattern", "material", "layer_role", "name", "notes")


def _one_of(value: str | None, allowed: tuple[str, ...], label: str) -> str | None:
    if value is not None and value not in allowed:
        raise ValueError(f"Choose a valid {label}.")
    return value


class ItemData(BaseModel):
    """Hand-entered (Phase 1) or auto-detected (Phase 2) item tags."""

    model_config = ConfigDict(str_strip_whitespace=True)

    category: Category
    subtype: str
    primary_color: str
    secondary_colors: list[str] = Field(default_factory=list)
    pattern: str | None = None
    seasons: list[str] = Field(default_factory=list)
    material: str | None = None
    layer_role: str | None = None
    name: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("subtype")
    @classmethod
    def _subtype_matches_category(cls, value: str, info: Any) -> str:
        category = info.data.get("category")
        if category is not None and not vocab.is_valid_subtype(category, value):
            raise ValueError(f"'{value}' is not a {category.value} subtype.")
        return value

    @field_validator("primary_color")
    @classmethod
    def _primary_color(cls, value: str) -> str:
        if not vocab.is_valid_color(value):
            raise ValueError("Choose a color from the palette.")
        return value

    @field_validator("secondary_colors")
    @classmethod
    def _secondary_colors(cls, value: list[str]) -> list[str]:
        if any(not vocab.is_valid_color(c) for c in value):
            raise ValueError("Choose colors from the palette.")
        return list(dict.fromkeys(value))

    @field_validator("seasons")
    @classmethod
    def _seasons(cls, value: list[str]) -> list[str]:
        if any(s not in vocab.SEASONS for s in value):
            raise ValueError("Choose valid seasons.")
        return list(dict.fromkeys(value))

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str | None) -> str | None:
        return _one_of(value, vocab.PATTERNS, "pattern")

    @field_validator("material")
    @classmethod
    def _material(cls, value: str | None) -> str | None:
        return _one_of(value, vocab.MATERIALS, "material")

    @field_validator("layer_role")
    @classmethod
    def _layer_role(cls, value: str | None) -> str | None:
        return _one_of(value, vocab.LAYER_ROLES, "layer role")


def _message(err: ErrorDetails) -> str:
    kind = err["type"]
    if kind == "enum":
        return "Choose a category."
    if kind == "missing":
        return "This field is required."
    if kind == "string_too_long":
        return f"Too long (max {err['ctx']['max_length']} characters)."
    return err["msg"].removeprefix("Value error, ")


def parse_item_data(values: Mapping[str, Any]) -> tuple[ItemData | None, dict[str, str]]:
    """Validate raw form values. Returns (data, {}) or (None, {field: message})."""
    raw = dict(values)
    for key in OPTIONAL_TEXT_FIELDS:
        if raw.get(key) in ("", None):
            raw[key] = None
    errors: dict[str, str] = {}
    for key in REQUIRED_FIELDS:
        if raw.get(key) in ("", None):
            errors[key] = "Choose a subtype." if key == "subtype" else "Choose a value."
            raw.pop(key, None)  # pydantic then reports it missing; our message wins
    try:
        data = ItemData.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "form"
            errors.setdefault(field, _message(err))
        return None, errors
    return data, {}


SHOW_OPTIONS = ("laundry", "unavailable", "archived")


@dataclass(frozen=True)
class ItemFilters:
    """Closet filters. Parsing is lenient so stale or hand-edited URLs never 422."""

    category: Category | None = None
    color: str | None = None
    season: str | None = None
    q: str | None = None
    needs_review: bool = False
    show: str | None = None  # laundry, unavailable or archived; None = everything not archived

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> "ItemFilters":
        raw_category = params.get("category") or None
        try:
            category = Category(raw_category) if raw_category else None
        except ValueError:
            category = None
        color = params.get("color") or None
        season = params.get("season") or None
        return cls(
            category=category,
            color=color if color and vocab.is_valid_color(color) else None,
            season=season if season in vocab.SEASONS else None,
            q=(params.get("q") or "").strip()[:80] or None,
            needs_review=params.get("review") == "1",
            show=params.get("show") if params.get("show") in SHOW_OPTIONS else None,
        )

    @property
    def active(self) -> bool:
        return (
            any(
                v is not None
                for v in (
                    self.category,
                    self.color,
                    self.season,
                    self.q,
                    self.show,
                )
            )
            or self.needs_review
        )

    def query_string(self, **extra: int) -> str:
        pairs: dict[str, str] = {}
        if self.category:
            pairs["category"] = self.category.value
        for key in ("color", "season", "q", "show"):
            value = getattr(self, key)
            if value is not None:
                pairs[key] = str(value)
        if self.needs_review:
            pairs["review"] = "1"
        pairs.update({k: str(v) for k, v in extra.items()})
        return urlencode(pairs)
