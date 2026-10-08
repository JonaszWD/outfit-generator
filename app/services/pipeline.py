"""The tagging pipeline: cutout, color analysis, tagging, save.

`process_item(item_id)` is a plain synchronous function of an id. It opens its own DB
session and builds its own dependencies from settings, so it holds no request-scoped state
and can be handed to a Celery or RQ worker unchanged (`process_item.delay(item_id)`).
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache, partial

from sqlalchemy import and_, func, or_, update
from sqlmodel import Session, col

from app.config import Settings, get_settings
from app.db import get_engine
from app.models import Item, TaggingStatus
from app.services import imaging
from app.services.items import delete_keys
from app.services.storage import (
    Storage,
    build_storage,
    cutout_key,
    cutout_thumb_key,
)
from app.services.tagging import Tagger, TaggingError, apply_color_check, build_tagger

logger = logging.getLogger(__name__)

# A "processing" item untouched for this long is assumed to belong to a crashed worker.
STALE_AFTER = timedelta(minutes=10)
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class PipelineDeps:
    settings: Settings
    storage: Storage
    session_factory: Callable[[], AbstractContextManager[Session]]
    tagger_factory: Callable[[], Tagger]
    remover: imaging.Remover | None  # None: no cutouts, the original photo is shown


def default_deps() -> PipelineDeps:
    settings = get_settings()
    return PipelineDeps(
        settings=settings,
        storage=build_storage(settings),
        session_factory=lambda: Session(get_engine()),
        tagger_factory=partial(build_tagger, settings),
        remover=(
            partial(imaging.remove_background, model=settings.rembg_model)
            if settings.cutout_backend == "rembg"
            else None
        ),
    )


@lru_cache(maxsize=8)
def _semaphore(limit: int) -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(limit)


def _claim(session: Session, item_id: int) -> bool:
    """Atomically move pending/failed (or stale processing) to processing."""
    stale = and_(
        col(Item.tagging_status) == TaggingStatus.PROCESSING,
        col(Item.updated_at) < func.now() - STALE_AFTER,
    )
    claimable = or_(
        col(Item.tagging_status).in_([TaggingStatus.PENDING, TaggingStatus.FAILED]), stale
    )
    result = session.execute(
        update(Item)
        .where(col(Item.id) == item_id, claimable)
        .values(tagging_status=TaggingStatus.PROCESSING, tagging_error=None)
        .returning(col(Item.id))
    )
    claimed = result.first() is not None
    session.commit()
    return claimed


def _mark_failed(session: Session, item_id: int, message: str) -> None:
    session.rollback()
    session.execute(
        update(Item)
        .where(col(Item.id) == item_id)
        .values(tagging_status=TaggingStatus.FAILED, tagging_error=message[:MAX_ERROR_LENGTH])
    )
    session.commit()


def _error_message(exc: Exception) -> str:
    if isinstance(exc, TaggingError):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "The stored photo is missing."
    return f"Unexpected error ({type(exc).__name__})."


def process_item(item_id: int, deps: PipelineDeps | None = None) -> None:
    """Run the pipeline for one item. Safe to call repeatedly or concurrently.

    Only pending/failed items are claimed, so a duplicate call for an item that is already
    processing or done does nothing. Failures are stored on the item, never raised.
    """
    deps = deps or default_deps()
    with _semaphore(deps.settings.max_concurrent_pipelines), deps.session_factory() as session:
        if not _claim(session, item_id):
            return
        try:
            tag_item(session, deps, item_id)
        except Exception as exc:
            logger.warning("Tagging failed for item %s: %s", item_id, type(exc).__name__)
            logger.debug("Tagging failure detail", exc_info=True)
            _mark_failed(session, item_id, _error_message(exc))


def tag_item(session: Session, deps: PipelineDeps, item_id: int) -> None:
    """Cutout, color analysis, tagging; stores the result and marks the item done.

    No claiming and no semaphore: callers (process_item, analyze_candidate) own those.
    Raises on failure and leaves the status to the caller.
    """
    item = session.get(Item, item_id)
    if item is None or item.original_image_path is None:
        raise FileNotFoundError
    original_key = item.original_image_path
    data = deps.storage.read(original_key)
    notes: list[str] = []

    cutout: imaging.Cutout | None = None
    if deps.remover is not None:
        try:
            cutout = imaging.make_cutout(data, deps.remover)
        except Exception:
            # Tagging is still useful without a cutout, so degrade instead of failing.
            logger.warning("Background removal failed for item %s", item_id, exc_info=True)
            notes.append("Background removal failed; the original photo is shown instead.")
    new_keys: list[str] = []
    if cutout:
        c_key, c_thumb = cutout_key(original_key), cutout_thumb_key(original_key)
        deps.storage.save(c_key, cutout.png)
        deps.storage.save(c_thumb, cutout.thumb_png)
        new_keys = [c_key, c_thumb]

    try:
        dominant = imaging.dominant_colors(
            cutout.png if cutout else data, drop_background=cutout is None
        )
        result = asyncio.run(deps.tagger_factory().tag(data))
        result = apply_color_check(result, dominant)

        item = session.get(Item, item_id, populate_existing=True)
        if item is None or item.original_image_path != original_key:
            # Deleted or photo replaced while we worked: our output is stale.
            delete_keys(deps.storage, new_keys)
            return
        item.cutout_image_path = cutout_key(original_key) if cutout else None
        item.ai_tags = {
            "tags": result.tags(),
            "flags": [*notes, *result.flags],
            "dominant_colors": [[name, round(share, 3)] for name, share in dominant],
            "backend": deps.settings.tagger_backend,
            "model": {
                "claude": deps.settings.tagger_model,
                "gemini": deps.settings.gemini_model,
            }.get(deps.settings.tagger_backend, "fake"),
        }
        item.ai_confidence = result.confidence
        item.tagging_status = TaggingStatus.DONE
        item.tagging_error = None
        session.add(item)
        session.commit()
    except Exception:
        delete_keys(deps.storage, new_keys)
        raise
