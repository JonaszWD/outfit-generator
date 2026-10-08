"""Everything we store about a user: export it, or delete it (rows and files)."""

import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from io import BytesIO
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, SQLModel, col, select

from app.models import (
    Candidate,
    Item,
    Outfit,
    OutfitFeedback,
    PlannedOutfit,
    User,
    UserPreferences,
)
from app.services.storage import Storage, derived_keys

logger = logging.getLogger(__name__)

# Never exported: credentials and bookkeeping, not the user's content.
EXCLUDED_USER_FIELDS = {"hashed_password"}


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _row(model: SQLModel, drop: set[str] = frozenset()) -> dict[str, Any]:  # type: ignore[assignment]
    return {k: _plain(v) for k, v in model.model_dump().items() if k not in drop}


@dataclass(frozen=True)
class Export:
    data: dict[str, Any]
    files: list[tuple[str, str]]  # (storage key, name inside the zip)


def collect(session: Session, user: User) -> Export:
    """Only rows where user_id is this user's; image files are listed by storage key."""
    items = list(session.exec(select(Item).where(Item.user_id == user.id).order_by(col(Item.id))))
    files: list[tuple[str, str]] = []
    item_rows = []
    for item in items:
        row = _row(item)
        images = {}
        for key in derived_keys(item.image_path):
            name = f"images/{item.id}/{key.rsplit('/', 1)[-1]}"
            files.append((key, name))
            images[key.rsplit("/", 1)[-1]] = name
        row["image_files"] = images
        row.pop("image_path", None)
        row.pop("original_image_path", None)
        row.pop("cutout_image_path", None)
        item_rows.append(row)

    def rows(model: type[SQLModel], order: Any) -> list[dict[str, Any]]:
        found = session.exec(select(model).where(model.user_id == user.id).order_by(order))  # type: ignore[attr-defined]
        return [_row(r) for r in found]

    prefs = session.get(UserPreferences, user.id)
    data = {
        "format": "wardrobe-export-v1",
        "account": _row(user, EXCLUDED_USER_FIELDS),
        "preferences": _row(prefs) if prefs else None,
        "items": item_rows,
        "outfits": rows(Outfit, col(Outfit.id)),
        "feedback": rows(OutfitFeedback, col(OutfitFeedback.id)),
        "planned_outfits": rows(PlannedOutfit, col(PlannedOutfit.day)),
        "candidates": rows(Candidate, col(Candidate.id)),
    }
    return Export(data, files)


def export_zip(session: Session, storage: Storage, user: User) -> BytesIO:
    """A zip with `data.json` and the user's photos. Missing files are skipped."""
    export = collect(session, user)
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.json", json.dumps(export.data, indent=2, ensure_ascii=False))
        for key, name in export.files:
            try:
                archive.writestr(name, storage.read(key))
            except FileNotFoundError:
                continue
    out.seek(0)
    return out


def delete_account(session: Session, storage: Storage, user: User) -> None:
    """Delete the user row (every table cascades from it), then every stored file.

    The rows go first and are committed, so a file problem leaves stray files rather than a
    half-deleted account; the folder sweep also catches files no row points to any more.
    """
    assert user.id is not None
    user_id = user.id
    paths = session.exec(select(Item.image_path).where(Item.user_id == user_id)).all()
    keys = [key for path in paths for key in derived_keys(path)]
    session.execute(delete(User).where(col(User.id) == user_id))
    session.commit()
    for key in keys:
        try:
            storage.delete(key)
        except OSError:
            logger.warning("could not delete a stored file")
    storage.delete_prefix(str(user_id))
