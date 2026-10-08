"""The candidate pipeline: link or photo -> product -> tags -> analysis, on the Postgres test DB."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import Session, select

from app.config import Settings
from app.models import Candidate, CandidateStatus, Category, Item, Source, TaggingStatus, User
from app.schemas import ItemFilters
from app.services import candidates as svc
from app.services.analysis import AnalysisResult
from app.services.candidates import CandidateDeps, RateLimited, analyze_candidate
from app.services.fetcher import BlockedAddress, InvalidUrl, UnsupportedScheme
from app.services.imaging import process_image
from app.services.items import list_items
from app.services.outfits import load_closet
from app.services.storage import LocalDiskStorage
from app.services.tagging import TAGGED_FIELDS, TaggingError, TagResult
from tests.conftest import image_bytes, stored_files
from tests.shop import Shop, jpeg
from tests.test_outfit_routes import closet

URL = "https://shop.example/p/shirt"


def tag(**overrides: Any) -> TagResult:
    base: dict[str, Any] = {
        "category": "top",
        "subtype": "shirt",
        "primary_color": "navy",
        "secondary_colors": [],
        "pattern": "solid",
        "seasons": ["spring", "summer", "autumn", "winter"],
        "material": "cotton",
        "layer_role": "base",
        "confidence": dict.fromkeys(TAGGED_FIELDS, 0.9),
    }
    return TagResult.model_validate({**base, **overrides})


class StaticTagger:
    def __init__(self, result: TagResult | Exception) -> None:
        self.result = result
        self.calls = 0

    async def tag(self, image_bytes: bytes) -> TagResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def with_tagger(deps: CandidateDeps, tagger: Any) -> CandidateDeps:
    return replace(deps, pipeline=replace(deps.pipeline, tagger_factory=lambda: tagger))


@pytest.fixture
def stocked(shop: Shop) -> Shop:
    shop.add_product("/p/shirt")
    shop.images["/img/shirt.jpg"] = jpeg("#1f2a44")
    return shop


def start(
    session: Session, user: User, storage: LocalDiskStorage, settings: Settings, url: str = URL
) -> Candidate:
    candidate, needs_job = svc.start_from_url(session, user, url, settings, storage)
    assert needs_job
    return candidate


def item_of(session: Session, candidate: Candidate) -> Item:
    item = svc.candidate_item(session, candidate)
    assert item is not None
    return item


# -------------------------------------------------------------------------- the pipeline


def test_pending_to_processing_to_done(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    closet(session, storage, user_a)
    candidate = start(session, user_a, storage, settings, URL + "?utm_source=newsletter")
    assert candidate.status == CandidateStatus.PENDING
    assert candidate.url == URL  # tracking parameters are not stored
    seen: list[CandidateStatus] = []

    class Spy(StaticTagger):
        async def tag(self, image_bytes: bytes) -> TagResult:
            seen.append(session.get(Candidate, candidate.id, populate_existing=True).status)  # type: ignore[union-attr]
            return await super().tag(image_bytes)

    assert candidate.id is not None
    analyze_candidate(candidate.id, with_tagger(candidate_deps, Spy(tag())))

    assert seen == [CandidateStatus.PROCESSING]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.DONE  # type: ignore[comparison-overlap]
    assert candidate.error is None
    assert candidate.title == "Slim Fit Oxford Shirt" and candidate.brand == "Northfield"
    assert str(candidate.price) == "59.90" and candidate.analyzed_at is not None
    assert candidate.product_info is not None and candidate.product_info["currency"] == "EUR"
    assert candidate.image_url == "https://shop.example/img/shirt.jpg"

    item = item_of(session, candidate)
    assert item.source == Source.CANDIDATE and item.tagging_status == TaggingStatus.DONE
    assert item.cutout_image_path and item.ai_tags is not None and not item.tags_confirmed
    assert candidate.result is not None
    result = AnalysisResult.from_dict(candidate.result)
    assert result.new_outfits > 0 and result.verdict_level != "empty"
    assert result.closet_size == 9  # the fixture closet, not counting the candidate


def test_a_blocked_page_fails_with_the_reason(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    shop.pages["/p/shirt"] = "<html><title>Just a moment...</title><body>captcha</body></html>"
    candidate = start(session, user_a, storage, settings)
    analyze_candidate(candidate.id, candidate_deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FAILED
    assert "bot-check" in (candidate.error or "")
    assert candidate.item_id is None and candidate.result is None


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda s: s.statuses.update({"/p/shirt": 404}), "HTTP 404"),
        (lambda s: setattr(s, "robots", "User-agent: *\nDisallow: /"), "robots.txt"),
        (lambda s: s.pages.update({"/p/shirt": "<html><title>x</title></html>"}), "product"),
    ],
)
def test_fetch_and_extraction_problems_are_stored_not_raised(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
    setup: Any,
    message: str,
) -> None:
    setup(stocked)
    candidate = start(session, user_a, storage, settings)
    analyze_candidate(candidate.id, candidate_deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FAILED and message in (candidate.error or "")


def test_a_tagger_failure_is_stored(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = start(session, user_a, storage, settings)
    deps = with_tagger(candidate_deps, StaticTagger(TaggingError("model said no")))
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FAILED and candidate.error == "model said no"
    assert svc.candidate_item(session, candidate) is not None  # kept, so a retry skips the download


def test_a_retry_after_a_tagger_failure_does_not_download_again(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = start(session, user_a, storage, settings)
    bad = with_tagger(candidate_deps, StaticTagger(TaggingError("flaky")))
    analyze_candidate(candidate.id, bad)  # type: ignore[arg-type]
    downloads = len(stocked.requests)
    analyze_candidate(candidate.id, with_tagger(candidate_deps, StaticTagger(tag())))  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.DONE
    assert len(stocked.requests) == downloads  # same photo, only the tagging was redone


def test_analyze_candidate_twice_is_safe(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    closet(session, storage, user_a)
    tagger = StaticTagger(tag())
    deps = with_tagger(candidate_deps, tagger)
    candidate = start(session, user_a, storage, settings)
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    first = dict(candidate.result or {})
    requests, files = len(stocked.requests), len(stored_files(storage))
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert tagger.calls == 1 and candidate.result == first
    assert len(stocked.requests) == requests and len(stored_files(storage)) == files
    assert len(list(session.exec(select(Item).where(Item.source == Source.CANDIDATE)))) == 1


def test_a_missing_candidate_is_a_noop(candidate_deps: CandidateDeps) -> None:
    analyze_candidate(999_999, candidate_deps)


def test_a_stuck_job_is_reclaimed_but_a_running_one_is_not(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    from sqlalchemy import update

    candidate = start(session, user_a, storage, settings)
    session.execute(
        update(Candidate)
        .where(Candidate.id == candidate.id)  # type: ignore[arg-type]
        .values(status=CandidateStatus.PROCESSING)
    )
    session.commit()
    tagger = StaticTagger(tag())
    deps = with_tagger(candidate_deps, tagger)
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    assert tagger.calls == 0  # somebody else is on it
    stale = datetime.now(UTC) - timedelta(hours=1)
    session.execute(update(Candidate).where(Candidate.id == candidate.id).values(updated_at=stale))  # type: ignore[arg-type]
    session.commit()
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert tagger.calls == 1 and candidate.status == CandidateStatus.DONE


def test_the_page_color_is_cross_checked_against_the_tagger(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    shop.add_product("/p/shirt", color="Navy")
    shop.images["/img/shirt.jpg"] = jpeg("red")
    candidate = start(session, user_a, storage, settings)
    deps = with_tagger(candidate_deps, StaticTagger(tag(primary_color="red")))
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    item = item_of(session, candidate)
    assert any("product page says navy" in f for f in item.ai_tags["flags"])  # type: ignore[index]
    assert item.ai_confidence is not None and item.ai_confidence["primary_color"] <= 0.4


def test_a_photo_candidate_skips_the_scraper(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    closet(session, storage, user_a)
    photo = process_image(image_bytes(size=(300, 400)), 10**7)
    candidate = svc.start_from_photo(session, storage, user_a, photo, settings)
    assert candidate.url is None and candidate.url_hash is None
    analyze_candidate(candidate.id, with_tagger(candidate_deps, StaticTagger(tag())))  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.DONE and shop.requests == []
    assert candidate.result is not None and candidate.result["new_outfits"] > 0


# ---------------------------------------------------------------------------- caching


def test_relinking_the_same_page_reuses_the_analysis(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = start(session, user_a, storage, settings)
    analyze_candidate(candidate.id, with_tagger(candidate_deps, StaticTagger(tag())))  # type: ignore[arg-type]
    again, needs_job = svc.start_from_url(
        session, user_a, URL + "?utm_campaign=x#reviews", settings, storage
    )
    assert again.id == candidate.id and not needs_job
    assert len(stocked.page_requests) == 1  # nothing was fetched a second time


def test_the_cache_expires_after_24_hours_and_reuses_the_row(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = start(session, user_a, storage, settings)
    deps = with_tagger(candidate_deps, StaticTagger(tag()))
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    old_item = item_of(session, candidate).id
    candidate.analyzed_at = datetime.now(UTC) - timedelta(hours=25)
    session.add(candidate)
    session.commit()
    again, needs_job = svc.start_from_url(session, user_a, URL, settings, storage)
    assert again.id == candidate.id and needs_job
    assert again.status == CandidateStatus.PENDING and again.result is None
    assert session.get(Item, old_item) is None  # the stale candidate item is gone
    analyze_candidate(candidate.id, deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.DONE and len(stocked.page_requests) == 2


def test_the_cache_window_is_configurable(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = start(session, user_a, storage, settings)
    analyze_candidate(candidate.id, with_tagger(candidate_deps, StaticTagger(tag())))  # type: ignore[arg-type]
    candidate.analyzed_at = datetime.now(UTC) - timedelta(hours=2)
    session.add(candidate)
    session.commit()
    short = settings.model_copy(update={"candidate_cache_hours": 1})
    _, needs_job = svc.start_from_url(session, user_a, URL, short, storage)
    assert needs_job


def test_a_failed_analysis_can_be_requested_again(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    shop.statuses["/p/shirt"] = 503
    candidate = start(session, user_a, storage, settings)
    analyze_candidate(candidate.id, candidate_deps)  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.FAILED
    again, needs_job = svc.start_from_url(session, user_a, URL, settings, storage)
    assert again.id == candidate.id and needs_job


def test_a_lost_job_is_picked_up_again(
    session: Session, storage: LocalDiskStorage, user_a: User, settings: Settings, stocked: Shop
) -> None:
    from sqlalchemy import update

    candidate = start(session, user_a, storage, settings)
    stale = datetime.now(UTC) - timedelta(hours=1)
    session.execute(update(Candidate).where(Candidate.id == candidate.id).values(updated_at=stale))  # type: ignore[arg-type]
    session.commit()
    again, needs_job = svc.start_from_url(session, user_a, URL, settings, storage)
    assert again.id == candidate.id and needs_job


# ------------------------------------------------------------------ unusable links


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://169.254.169.254/latest/meta-data/", BlockedAddress),
        ("http://127.0.0.1:8000/admin", BlockedAddress),
        ("http://localhost/", BlockedAddress),
        ("http://[::1]/", BlockedAddress),
        ("file:///etc/passwd", UnsupportedScheme),
        ("ftp://shop.example/a", UnsupportedScheme),
        ("http://user:pw@shop.example/", InvalidUrl),
    ],
)
def test_unsafe_links_never_become_candidates(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    url: str,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        svc.start_from_url(session, user_a, url, settings, storage)
    assert session.exec(select(Candidate)).first() is None


# ----------------------------------------------------------------------- rate limit


def test_the_eleventh_request_in_an_hour_is_rejected(
    session: Session, storage: LocalDiskStorage, user_a: User, settings: Settings
) -> None:
    for n in range(10):
        svc.start_from_url(session, user_a, f"https://shop.example/p/{n}", settings, storage)
    with pytest.raises(RateLimited) as info:
        svc.start_from_url(session, user_a, "https://shop.example/p/10", settings, storage)
    assert "10 products per hour" in str(info.value) and "minute" in str(info.value)
    assert info.value.retry_after > 0
    assert len(list(session.exec(select(Candidate)))) == 10


def test_cached_links_do_not_count_against_the_limit(
    session: Session, storage: LocalDiskStorage, user_a: User, settings: Settings
) -> None:
    for n in range(10):
        svc.start_from_url(session, user_a, f"https://shop.example/p/{n}", settings, storage)
    link = "https://shop.example/p/3"
    again, needs_job = svc.start_from_url(session, user_a, link, settings, storage)
    assert not needs_job and again.url == "https://shop.example/p/3"


def test_the_limit_is_per_user_and_per_hour(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    settings: Settings,
) -> None:
    small = settings.model_copy(update={"analyses_per_hour": 2})
    for n in range(2):
        svc.start_from_url(session, user_a, f"https://shop.example/p/{n}", small, storage)
    other = "https://shop.example/p/0"
    svc.start_from_url(session, user_b, other, small, storage)  # B is unaffected
    with pytest.raises(RateLimited):
        svc.start_from_url(session, user_a, "https://shop.example/p/9", small, storage)
    old = datetime.now(UTC) - timedelta(hours=2)
    for c in session.exec(select(Candidate).where(Candidate.user_id == user_a.id)):
        c.created_at = old
        session.add(c)
    session.commit()
    later = "https://shop.example/p/9"
    svc.start_from_url(session, user_a, later, small, storage)  # the window moved on


def test_photo_candidates_count_too(
    session: Session, storage: LocalDiskStorage, user_a: User, settings: Settings
) -> None:
    small = settings.model_copy(update={"analyses_per_hour": 1})
    photo = process_image(image_bytes(), 10**7)
    svc.start_from_photo(session, storage, user_a, photo, small)
    with pytest.raises(RateLimited):
        svc.start_from_photo(session, storage, user_a, photo, small)


# ------------------------------------------------------------- tags, adopt, discard


def done_candidate(
    session: Session,
    storage: LocalDiskStorage,
    user: User,
    settings: Settings,
    deps: CandidateDeps,
    result: TagResult | None = None,
    url: str = URL,
) -> Candidate:
    candidate = start(session, user, storage, settings, url)
    analyze_candidate(candidate.id, with_tagger(deps, StaticTagger(result or tag())))  # type: ignore[arg-type]
    session.refresh(candidate)
    assert candidate.status == CandidateStatus.DONE
    return candidate


def test_editing_the_tags_recomputes_the_analysis(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    from app.schemas import ItemData
    from app.services.items import update_item

    closet(session, storage, user_a)
    wrong = tag(category="shoes", subtype="boots", primary_color="black")
    candidate = done_candidate(session, storage, user_a, settings, candidate_deps, wrong)
    before = AnalysisResult.from_dict(candidate.result or {})
    item = item_of(session, candidate)
    fixed = ItemData(category=Category.TOP, subtype="blouse", primary_color="cream")
    update_item(session, storage, item, fixed)
    after = svc.reanalyze(session, candidate, candidate_deps)
    assert after != before and after.kind == "outfit"
    session.refresh(candidate)
    assert AnalysisResult.from_dict(candidate.result or {}) == after
    assert svc.candidate_view(item_of(session, candidate)).subtype == "blouse"  # type: ignore[union-attr]


def test_adopting_moves_the_piece_into_the_closet(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    closet(session, storage, user_a)
    before = len(load_closet(session, user_a).views)
    candidate = done_candidate(session, storage, user_a, settings, candidate_deps)
    assert len(load_closet(session, user_a).views) == before  # not yet
    item = svc.adopt_candidate(session, candidate)
    assert item.source == Source.OWNED and item.tags_confirmed
    assert item.name == "Slim Fit Oxford Shirt" and item.subtype == "shirt"
    assert len(load_closet(session, user_a).views) == before + 1
    assert svc.adopt_candidate(session, candidate).id == item.id  # idempotent
    svc.discard_candidate(session, storage, candidate)  # discarding the record keeps the item
    assert session.get(Item, item.id) is not None


def test_adopting_requires_a_finished_analysis(
    session: Session, storage: LocalDiskStorage, user_a: User, settings: Settings, stocked: Shop
) -> None:
    candidate = start(session, user_a, storage, settings)
    with pytest.raises(svc.CandidateNotReady):
        svc.adopt_candidate(session, candidate)


def test_discarding_removes_the_item_and_its_files(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = done_candidate(session, storage, user_a, settings, candidate_deps)
    assert stored_files(storage)
    item_id = candidate.item_id
    svc.discard_candidate(session, storage, candidate)
    assert session.get(Item, item_id) is None and stored_files(storage) == []
    assert session.exec(select(Candidate)).first() is None


# --------------------------------------------------------------- never in the closet


def test_a_candidate_never_leaks_into_the_closet_stats_or_generation(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    from app.engine.config import EngineConfig
    from app.engine.types import Context
    from app.services.items import items_awaiting_review
    from app.services.outfits import closet_stats_view, suggest

    closet(session, storage, user_a)
    done_candidate(session, storage, user_a, settings, candidate_deps)

    candidate_items = list(session.exec(select(Item).where(Item.source == Source.CANDIDATE)))
    assert len(candidate_items) == 1
    cid = candidate_items[0].id

    page = list_items(session, user_a, ItemFilters())
    assert cid not in {i.id for i in page.items} and page.total == 9
    assert list_items(session, user_a, ItemFilters(needs_review=True)).total == 0
    assert items_awaiting_review(session, user_a) == []
    loaded = load_closet(session, user_a)
    assert cid not in loaded.items and cid not in {v.id for v in loaded.views}
    stats = closet_stats_view(session, user_a, EngineConfig())
    assert cid not in {item.id for item, _ in stats.ranked} and len(stats.ranked) == 9
    config = EngineConfig()
    for _ in range(5):
        suggestions = suggest(session, user_a, config, Context(), 3)
        assert all(cid not in o.item_ids for o in suggestions.outfits)


# ------------------------------------------------------------------------- retention


def test_old_analyses_and_their_photos_are_purged_but_adopted_pieces_stay(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    shop.add_product("/p/old")
    shop.add_product("/p/kept")
    shop.images["/img/shirt.jpg"] = jpeg("#1f2a44")
    old = done_candidate(
        session, storage, user_a, settings, candidate_deps, url="https://shop.example/p/old"
    )
    kept = done_candidate(
        session, storage, user_a, settings, candidate_deps, url="https://shop.example/p/kept"
    )
    adopted = svc.adopt_candidate(session, kept)
    ancient = datetime.now(UTC) - timedelta(days=40)
    for row in (old, kept):
        row.created_at = ancient
        session.add(row)
    session.commit()
    old_id, kept_id, old_item = old.id, kept.id, old.item_id
    svc.purge_old_candidates(session, storage, user_a, settings)
    assert session.get(Candidate, old_id) is None and session.get(Item, old_item) is None
    assert session.get(Candidate, kept_id) is None  # the record goes ...
    assert session.get(Item, adopted.id) is not None  # ... the closet piece stays


def test_recent_analyses_are_not_purged(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    settings: Settings,
    candidate_deps: CandidateDeps,
    stocked: Shop,
) -> None:
    candidate = done_candidate(session, storage, user_a, settings, candidate_deps)
    assert svc.purge_old_candidates(session, storage, user_a, settings) == 0
    assert session.get(Candidate, candidate.id) is not None
