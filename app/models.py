from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func
from sqlmodel import Field, SQLModel


class Category(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    DRESS = "dress"
    OUTERWEAR = "outerwear"
    SHOES = "shoes"
    ACCESSORY = "accessory"


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
    __table_args__ = (CheckConstraint("formality BETWEEN 1 AND 5", name="ck_items_formality"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    image_path: str
    category: Category = Field(
        sa_column=Column(
            Enum(
                Category,
                name="item_category",
                values_callable=_enum_values,
            ),
            nullable=False,
        )
    )
    subtype: str | None = None
    primary_color: str | None = None
    secondary_colors: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text), nullable=False, server_default="{}"),
    )
    pattern: str | None = None
    formality: int | None = None
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


class Outfit(SQLModel, table=True):
    __tablename__ = "outfits"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    item_ids: list[int] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Integer), nullable=False),
    )
    score: float = 0.0
    occasion: str | None = None
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )


class Candidate(SQLModel, table=True):
    __tablename__ = "candidates"
    # The (user_id, url) unique index also serves lookups by user_id, so no separate index there.
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_candidates_user_url"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE")
    url: str
    title: str | None = None
    brand: str | None = None
    price: Decimal | None = Field(default=None, sa_column=Column(Numeric(10, 2)))
    image_url: str | None = None
    item_id: int | None = Field(
        default=None, foreign_key="items.id", ondelete="SET NULL", index=True
    )
    created_at: datetime = Field(
        default_factory=_created_at,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
