"""Candidates: a product the user is considering buying.

`analyze_candidate(candidate_id)` is the background job, written like `process_item`: a plain
function of an id that opens its own session, claims the row atomically (so calling it twice
is safe) and stores failures instead of raising. A candidate's garment is an Item with
source=candidate; every closet query filters on source=owned, so it stays invisible until the
user adopts it.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache, partial

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.config import Settings, get_settings
from app.db import get_engine
from app.engine import vocab
from app.engine.config import EngineConfig
from app.engine.types import ALL_SEASONS, ItemView
from app.models import Candidate, CandidateStatus, Item, Source, TaggingStatus, User
from app.schemas import ItemData
from app.services import imaging
from app.services.analysis import AnalysisConfig, AnalysisResult, analyze
from app.services.fetcher import (
    FetchError,
    SafeFetcher,
    build_fetcher,
    normalize_url,
    parse_target,
    url_hash,
)
from app.services.imaging import ProcessedImage
from app.services.items import ai_item_data, create_pending_item, delete_item
from app.services.outfits import load_closet, to_view
from app.services.pipeline import PipelineDeps, tag_item
from app.services.preferences import engine_config_for
from app.services.scraper import ExtractionFailed, extract_product, fetch_product_image
from app.services.storage import Storage, build_storage
from app.services.tagging import TaggingError, build_tagger

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
MAX_ERROR_LENGTH = 500
COLOR_DISAGREEMENT = 20.0  # delta E between the page's color and the tagger's
RATE_WINDOW = timedelta(hours=1)


class RateLimited(Exception):
    def __init__(self, limit: int, retry_after: int) -> None:
        minutes = max(1, round(retry_after / 60))
        super().__init__(
            f"You can analyze {limit} products per hour. Try again in about {minutes} "
            f"minute{'s' if minutes != 1 else ''}."
        )
        self.limit = limit
        self.retry_after = retry_after


class CandidateNotReady(Exception):
    """The candidate cannot be added to the closet yet (no usable tags)."""


@dataclass(frozen=True)
class CandidateDeps:
    pipeline: PipelineDeps
    fetcher_factory: Callable[[], SafeFetcher]
    engine_config: EngineConfig
    analysis_config: AnalysisConfig

    @property
    def settings(self) -> Settings:
        return self.pipeline.settings


def default_candidate_deps() -> CandidateDeps:
    settings = get_settings()
    pipeline = PipelineDeps(
        settings=settings,
        storage=build_storage(settings),
        session_factory=lambda: Session(get_engine()),
        tagger_factory=partial(build_tagger, settings),
        remover=partial(imaging.remove_background, model=settings.rembg_model),
    )
    return CandidateDeps(
        pipeline=pipeline,
        fetcher_factory=partial(build_fetcher, settings),
        engine_config=EngineConfig(),
        analysis_config=AnalysisConfig(),
    )


@lru_cache(maxsize=8)
def _semaphore(limit: int) -> threading.BoundedSemaphore:
    # Distinct from the tagging pipeline's semaphore: an analysis runs tag_item inside it.
    return threading.BoundedSemaphore(limit)


# --------------------------------------------------------------------- views of a candidate


def get_owned_candidate(session: Session, user: User, candidate_id: int) -> Candidate:
    candidate = session.exec(
        select(Candidate).where(Candidate.id == candidate_id, Candidate.user_id == user.id)
    ).first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Not found")
    return candidate


def candidate_item(session: Session, candidate: Candidate) -> Item | None:
    return session.get(Item, candidate.item_id) if candidate.item_id else None


def candidate_view(item: Item) -> ItemView | None:
    """Engine view of the candidate: confirmed tags if the user edited them, else the AI's."""
    if item.tags_confirmed:
        return to_view(item)
    data = ai_item_data(item)
    if data is None or item.id is None:
        return None
    return view_from_data(item.id, data)


def view_from_data(item_id: int, data: ItemData) -> ItemView:
    return ItemView(
        id=item_id,
        category=data.category,
        subtype=data.subtype,
        primary_color=data.primary_color,
        secondary_colors=tuple(data.secondary_colors),
        pattern=data.pattern or "solid",
        seasons=frozenset(data.seasons) or ALL_SEASONS,
        material=data.material,
        layer_role=data.layer_role,
    )


def list_candidates(session: Session, user: User) -> list[Candidate]:
    return list(
        session.exec(
            select(Candidate)
            .where(Candidate.user_id == user.id)
            .order_by(col(Candidate.created_at).desc(), col(Candidate.id).desc())
        )
    )


# ---------------------------------------------------------------------- starting a request


def check_rate_limit(session: Session, user: User, settings: Settings) -> None:
    """At most `analyses_per_hour` requests per user. A request is a candidate row's
    `created_at`, which is reset whenever the analysis is re-run."""
    since = datetime.now(UTC) - RATE_WINDOW
    recent = list(
        session.exec(
            select(Candidate.created_at)
            .where(Candidate.user_id == user.id, col(Candidate.created_at) > since)
            .order_by(col(Candidate.created_at))
        )
    )
    if len(recent) >= settings.analyses_per_hour:
        oldest = recent[len(recent) - settings.analyses_per_hour]
        wait = int((oldest + RATE_WINDOW - datetime.now(UTC)).total_seconds()) + 1
        raise RateLimited(settings.analyses_per_hour, max(wait, 1))


def purge_old_candidates(session: Session, storage: Storage, user: User, settings: Settings) -> int:
    """Delete this user's unadopted analyses (and their product photos) past the retention
    period. Adopted pieces stay in the closet; only their candidate record goes."""
    cutoff = datetime.now(UTC) - timedelta(days=settings.candidate_retention_days)
    old = list(
        session.exec(
            select(Candidate).where(
                Candidate.user_id == user.id, col(Candidate.created_at) < cutoff
            )
        )
    )
    for candidate in old:
        discard_candidate(session, storage, candidate)
    return len(old)


def start_from_url(
    session: Session, user: User, url: str, settings: Settings, storage: Storage
) -> tuple[Candidate, bool]:
    """Create (or reuse) the candidate for a link. Returns (candidate, needs_a_job).

    Raises a FetchError subclass for an unusable link (checked without any network access),
    or RateLimited. A link analyzed within `candidate_cache_hours` is reused for free.
    """
    parse_target(url, settings.fetch_allow_private_hosts)  # scheme, credentials, private literals
    purge_old_candidates(session, storage, user, settings)
    key = url_hash(url)
    existing = session.exec(
        select(Candidate).where(Candidate.user_id == user.id, Candidate.url_hash == key)
    ).first()
    now = datetime.now(UTC)
    if existing is not None and existing.status != CandidateStatus.FAILED:
        unfinished = existing.status != CandidateStatus.DONE
        if unfinished and now - existing.updated_at > STALE_AFTER:
            return existing, True  # the job was lost (restart?): let a new one claim it
        age = now - (existing.analyzed_at or existing.created_at)
        if age < timedelta(hours=settings.candidate_cache_hours):
            return existing, False
    check_rate_limit(session, user, settings)
    if existing is not None:  # stale or failed: run it again on the same row
        _reset(session, storage, existing)
        existing.created_at = now
        session.add(existing)
        session.commit()
        return existing, True
    assert user.id is not None
    candidate = Candidate(user_id=user.id, url=normalize_url(url), url_hash=key)
    session.add(candidate)
    try:
        session.commit()
    except IntegrityError:  # a double submit won the race
        session.rollback()
        winner = session.exec(
            select(Candidate).where(Candidate.user_id == user.id, Candidate.url_hash == key)
        ).one()
        return winner, False
    session.refresh(candidate)
    return candidate, True


def start_from_photo(
    session: Session, storage: Storage, user: User, image: ProcessedImage, settings: Settings
) -> Candidate:
    """A candidate from an uploaded photo: same pipeline, minus the scraper."""
    purge_old_candidates(session, storage, user, settings)
    check_rate_limit(session, user, settings)
    item = create_pending_item(session, storage, user, image, source=Source.CANDIDATE)
    assert user.id is not None
    candidate = Candidate(user_id=user.id, item_id=item.id)
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate


def _reset(session: Session, storage: Storage, candidate: Candidate) -> None:
    """Forget a previous analysis. The old candidate item goes unless the user adopted it."""
    item = candidate_item(session, candidate)
    candidate.item_id = None
    candidate.status = CandidateStatus.PENDING
    candidate.error = None
    candidate.result = None
    candidate.product_info = None
    candidate.analyzed_at = None
    session.add(candidate)
    session.commit()
    if item is not None and item.source == Source.CANDIDATE:
        delete_item(session, storage, item)


# ---------------------------------------------------------------------------- the job


def _claim(session: Session, candidate_id: int) -> bool:
    stale = and_(
        col(Candidate.status) == CandidateStatus.PROCESSING,
        col(Candidate.updated_at) < func.now() - STALE_AFTER,
    )
    claimable = or_(
        col(Candidate.status).in_([CandidateStatus.PENDING, CandidateStatus.FAILED]), stale
    )
    result = session.execute(
        update(Candidate)
        .where(col(Candidate.id) == candidate_id, claimable)
        .values(status=CandidateStatus.PROCESSING, error=None)
        .returning(col(Candidate.id))
    )
    claimed = result.first() is not None
    session.commit()
    return claimed


def _mark_failed(session: Session, candidate_id: int, message: str) -> None:
    session.rollback()
    session.execute(
        update(Candidate)
        .where(col(Candidate.id) == candidate_id)
        .values(status=CandidateStatus.FAILED, error=message[:MAX_ERROR_LENGTH])
    )
    session.commit()


def _error_message(exc: Exception) -> str:
    if isinstance(exc, ExtractionFailed):
        return exc.reason
    if isinstance(exc, FetchError | TaggingError):
        return str(exc)
    return f"Unexpected error ({type(exc).__name__})."


def analyze_candidate(candidate_id: int, deps: CandidateDeps | None = None) -> None:
    """Fetch, extract, tag and analyze one candidate. Safe to call repeatedly or concurrently."""
    deps = deps or default_candidate_deps()
    limit = deps.settings.max_concurrent_analyses
    with _semaphore(limit), deps.pipeline.session_factory() as session:
        if not _claim(session, candidate_id):
            return
        try:
            _run(session, deps, candidate_id)
        except Exception as exc:
            logger.warning("Candidate %s failed: %s", candidate_id, type(exc).__name__)
            logger.debug("Candidate failure detail", exc_info=True)
            _mark_failed(session, candidate_id, _error_message(exc))


async def _fetch(url: str, deps: CandidateDeps) -> tuple[dict[str, object], ProcessedImage, str]:
    fetcher = deps.fetcher_factory()
    info = await extract_product(url, fetcher, deps.settings)
    image, used = await fetch_product_image(info, fetcher, deps.settings.max_upload_bytes)
    return info.model_dump(mode="json"), image, used


def _run(session: Session, deps: CandidateDeps, candidate_id: int) -> None:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        return
    user = session.get(User, candidate.user_id)
    if user is None:
        return
    item = candidate_item(session, candidate)

    if item is None:  # a link: download the product and its photo
        if not candidate.url:
            raise ExtractionFailed("This candidate has neither a link nor a photo.")
        info, image, image_url = asyncio.run(_fetch(candidate.url, deps))
        candidate.product_info = info
        candidate.title = str(info.get("title") or "")[:200] or None
        candidate.brand = str(info["brand"])[:80] if info.get("brand") else None
        price = info.get("price")
        candidate.price = None if price is None else Decimal(str(price))
        candidate.image_url = image_url
        item = create_pending_item(
            session, deps.pipeline.storage, user, image, source=Source.CANDIDATE
        )
        candidate.item_id = item.id
        session.add(candidate)
        session.commit()
        session.refresh(item)
    assert item.id is not None

    item.tagging_status = TaggingStatus.PROCESSING
    session.add(item)
    session.commit()
    tag_item(session, deps.pipeline, item.id)
    session.refresh(item)
    _note_page_color(session, candidate, item)
    store_analysis(session, candidate, item, deps)


def _note_page_color(session: Session, candidate: Candidate, item: Item) -> None:
    """If the product page names a color the tagger disagrees with, flag it for review."""
    page_color = (candidate.product_info or {}).get("color")
    data = ai_item_data(item)
    if not page_color or data is None or page_color not in vocab.COLOR_HEX:
        return
    if vocab.color_distance(str(page_color), data.primary_color) <= COLOR_DISAGREEMENT:
        return
    tags = dict(item.ai_tags or {})
    tags["flags"] = [
        *tags.get("flags", []),
        f"The product page says {page_color}, but the tagger says {data.primary_color}.",
    ]
    confidence = dict(item.ai_confidence or {})
    confidence["primary_color"] = min(confidence.get("primary_color", 1.0), 0.4)
    item.ai_tags, item.ai_confidence = tags, confidence
    session.add(item)
    session.commit()


def compute_analysis(
    session: Session, user: User, item: Item, deps: CandidateDeps
) -> AnalysisResult:
    view = candidate_view(item)
    if view is None:
        raise TaggingError("The item could not be tagged, so it cannot be analyzed.")
    closet = load_closet(session, user)  # owned, confirmed items only
    config = engine_config_for(session, user, deps.engine_config)  # the user's own preferences
    return analyze(closet.views, view, config, deps.analysis_config)


def store_analysis(
    session: Session, candidate: Candidate, item: Item, deps: CandidateDeps
) -> AnalysisResult:
    user = session.get(User, candidate.user_id)
    assert user is not None
    result = compute_analysis(session, user, item, deps)
    candidate.result = result.to_dict()
    candidate.status = CandidateStatus.DONE
    candidate.error = None
    candidate.analyzed_at = datetime.now(UTC)
    session.add(candidate)
    session.commit()
    return result


def reanalyze(session: Session, candidate: Candidate, deps: CandidateDeps) -> AnalysisResult:
    """Recompute the analysis after the user corrected the candidate's tags."""
    item = candidate_item(session, candidate)
    if item is None:
        raise CandidateNotReady("There is nothing to analyze yet.")
    return store_analysis(session, candidate, item, deps)


# ------------------------------------------------------------------- adopt and discard


def adopt_candidate(session: Session, candidate: Candidate) -> Item:
    """Move the candidate's item into the closet with its current (or AI-proposed) tags."""
    item = candidate_item(session, candidate)
    if item is None or candidate.status != CandidateStatus.DONE:
        raise CandidateNotReady("The analysis is not finished yet.")
    if item.source == Source.OWNED:
        return item  # already in the closet
    if not item.tags_confirmed:
        data = ai_item_data(item)
        if data is None:
            raise CandidateNotReady("Check the tags first: they are incomplete.")
        for field, value in data.model_dump().items():
            if field in ("name", "notes") and getattr(item, field):
                continue
            setattr(item, field, value)
        if not item.name and candidate.title:
            item.name = candidate.title[:80]
        item.tags_confirmed = True
    item.source = Source.OWNED
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def discard_candidate(session: Session, storage: Storage, candidate: Candidate) -> None:
    item = candidate_item(session, candidate)
    session.delete(candidate)
    session.commit()
    if item is not None and item.source == Source.CANDIDATE:
        delete_item(session, storage, item)


# -------------------------------------------------------------------------- display


@dataclass(frozen=True)
class PanelData:
    candidate: Candidate
    item: Item | None
    result: AnalysisResult | None
    items: dict[int, Item]  # the candidate's item and every owned item the result mentions
    adopted: bool  # the piece now lives in the closet

    @property
    def finished(self) -> bool:
        return self.candidate.status in (CandidateStatus.DONE, CandidateStatus.FAILED)


def panel_data(session: Session, user: User, candidate: Candidate) -> PanelData:
    item = candidate_item(session, candidate)
    result = None
    if candidate.status == CandidateStatus.DONE and candidate.result:
        result = AnalysisResult.from_dict(candidate.result)
    wanted: set[int] = {item.id} if item and item.id else set()
    if result is not None:
        wanted |= {i for e in result.examples for i in e.item_ids}
        wanted |= {i for i, _ in result.pairs_with} | set(result.never_pairs_with)
        if result.redundancy.item_id:
            wanted.add(result.redundancy.item_id)
    items = {
        row.id: row
        for row in session.exec(
            select(Item).where(Item.user_id == user.id, col(Item.id).in_(wanted))
        )
        if row.id is not None
    }
    return PanelData(candidate, item, result, items, bool(item and item.source == Source.OWNED))
