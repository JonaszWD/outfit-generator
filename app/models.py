from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql import func
from sqlmodel import Field, SQLModel

from app.engine.vocab import Category as Category  # re-exported for existing imports


class TaggingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class Availability(StrEnum):
    AVAILABLE = "available"
    LAUNDRY = "laundry"  # temporary: skipped when suggesting outfits, still part of the closet
    UNAVAILABLE = "unavailable"  # lost, lent out, donated: out of everything until restored


class Source(StrEnum):
    OWNED = "owned"
    CANDIDATE = "candidate"


def _created_at() -> datetime:
    return datetime.now(UTC)


def _enum_values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class Item(SQLModel, table=True):
    __tablename__ = "items"
    __table_args__ = (
        # Also serves plain user_id lookups (leading column).
        Index("ix_items_user_id_category", "user_id", "category"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE")
    image_path: str  # display original (normalized, EXIF-stripped); same key as original_image_path
    original_image_path: str | None = None
    cutout_image_path: str | None = None
    tagging_status: TaggingStatus = Field(
        default=TaggingStatus.PENDING,
        sa_column=Column(
            Enum(TaggingStatus, name="tagging_status", values_callable=_enum_values),
            nullable=False,
            server_default=TaggingStatus.PENDING.value,
        ),
    )
    tagging_error: str | None = Field(default=None, sa_type=Text)
    tags_confirmed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    availability: Availability = Field(
        default=Availability.AVAILABLE,
        sa_column=Column(
            Enum(Availability, name="item_availability", values_callable=_enum_values),
            nullable=False,
            server_default=Availability.AVAILABLE.value,
        ),
    )
    archived_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    ai_tags: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    ai_confidence: dict[str, float] | None = Field(default=None, sa_column=Column(JSONB))
    name: str | None = None
    notes: str | None = Field(default=None, sa_type=Text)
    # Null until the user confirms tags (items start out pending AI tagging).
    category: Category | None = Field(
        default=None,
        sa_column=Column(
            Enum(
                Category,
                name="item_category",
                values_callable=_enum_values,
            ),
            nullable=True,
        ),
    )
    subtype: str | None = None
    primary_color: str | None = None
    secondary_colors: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text), nullable=False, server_default="{}"),
    )
    pattern: str | None = None
    seasons: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text), nullable=False, server_default="{}"),
    )
    material: str | None = None
    layer_role: str | None = None
    source: Source = Field(
        default=Source.OWNED,
        sa_column=Column(
            Enum(Source, name="item_source", values_callable=_enum_values),
            nullable=False,
            server_default=Source.OWNED.value,
        ),
    )
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )


class Outfit(SQLModel, table=True):
    """A generated outfit. Suggestions are stored too (favorite=False) so they have an id
    to save or delete; saving flips `favorite`. `item_ids` is always sorted."""

    __tablename__ = "outfits"
    __table_args__ = (
        UniqueConstraint("user_id", "item_ids", name="uq_outfits_user_items"),
        # Also serves plain user_id lookups (leading column).
        Index("ix_outfits_user_favorite", "user_id", "favorite"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE")
    item_ids: list[int] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Integer), nullable=False),
    )
    score: float = 0.0
    context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    breakdown: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    favorite: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    occasions: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text), nullable=False, server_default="{}"),
    )
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class Candidate(SQLModel, table=True):
    """A product the user is considering. Its garment is an Item with source=candidate, which
    never appears in the closet, outfit generation or stats until it is adopted."""

    __tablename__ = "candidates"
    # One row per normalized link and user; re-pasting a link within the cache window reuses it.
    # Photo uploads have no url or url_hash (NULLs never collide).
    __table_args__ = (UniqueConstraint("user_id", "url_hash", name="uq_candidates_user_urlhash"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE")
    url: str | None = Field(default=None, sa_type=Text)
    url_hash: str | None = Field(default=None, index=True, max_length=64)
    status: CandidateStatus = Field(
        default=CandidateStatus.PENDING,
        sa_column=Column(
            Enum(CandidateStatus, name="candidate_status", values_callable=_enum_values),
            nullable=False,
            server_default=CandidateStatus.PENDING.value,
        ),
    )
    error: str | None = Field(default=None, sa_type=Text)
    title: str | None = None
    brand: str | None = None
    price: Decimal | None = Field(default=None, sa_column=Column(Numeric(10, 2)))
    image_url: str | None = Field(default=None, sa_type=Text)
    item_id: int | None = Field(
        default=None, foreign_key="items.id", ondelete="SET NULL", index=True
    )
    product_info: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    analyzed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )


# --------------------------------------------------------------------------- accounts


class UserSession(SQLModel, table=True):
    """A login. The cookie holds a random token; only its SHA-256 is stored here."""

    __tablename__ = "sessions"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    token_hash: str = Field(unique=True, max_length=64)
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    last_seen_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class PasswordReset(SQLModel, table=True):
    """A single-use reset link. Only the token's SHA-256 is stored."""

    __tablename__ = "password_resets"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    token_hash: str = Field(unique=True, max_length=64)
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class AuthAttempt(SQLModel, table=True):
    """One counted attempt (failed login, reset request, registration) for rate limiting.
    `key` is a hash of the IP address or email, never the raw value."""

    __tablename__ = "auth_attempts"
    __table_args__ = (Index("ix_auth_attempts_lookup", "kind", "key", "created_at"),)

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(max_length=20)
    key: str = Field(max_length=64)
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class UserPreferences(SQLModel, table=True):
    __tablename__ = "user_preferences"

    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", primary_key=True)
    home_city: str | None = Field(default=None, max_length=120)
    latitude: float | None = None  # rounded to 2 decimals before it is stored or sent
    longitude: float | None = None
    color_strictness: float = 0.5  # 0 loose .. 1 strict; 0.5 is the engine default
    units: str = Field(default="metric", max_length=10)
    # Rank suggestions with the user's style reference photos. Remembered from the last request.
    use_style_references: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default="false")
    )


class StyleReference(SQLModel, table=True):
    """An inspiration photo and the style a vision model read from it (colors,
    patterns, garment types). Only that summary feeds the ranking, never the photo itself."""

    __tablename__ = "style_references"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    image_path: str  # storage key; the thumbnail key is derived from it
    style: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class OutfitFeedback(SQLModel, table=True):
    """A thumbs up (+1) or down (-1) on a suggested outfit. One row per user and outfit; the
    latest rating wins. `rules` snapshots the rule values the outfit scored at the time."""

    __tablename__ = "outfit_feedback"
    __table_args__ = (UniqueConstraint("user_id", "item_ids", name="uq_feedback_user_items"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    item_ids: list[int] = Field(sa_column=Column(ARRAY(Integer), nullable=False))
    rating: int = Field(sa_column=Column(SmallInteger, nullable=False))
    reason: str | None = Field(default=None, max_length=40)
    rules: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class PlannedOutfit(SQLModel, table=True):
    __tablename__ = "planned_outfits"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_planned_user_day"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    day: date = Field(sa_column=Column(Date, nullable=False))
    outfit_id: int = Field(foreign_key="outfits.id", ondelete="CASCADE")
