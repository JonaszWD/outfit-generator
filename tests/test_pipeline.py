from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlmodel import Session

from app.models import Item, TaggingStatus, User
from app.services.imaging import process_image
from app.services.items import create_pending_item, delete_item
from app.services.pipeline import PipelineDeps, process_item
from app.services.storage import LocalDiskStorage, cutout_key
from app.services.tagging import FakeTagger, TaggingError, TagResult
from tests.conftest import image_bytes, stored_files


def iid(item: Item) -> int:
    assert item.id is not None
    return item.id


def pending_item(session: Session, storage: LocalDiskStorage, user: User) -> Item:
    return create_pending_item(
        session, storage, user, process_image(image_bytes(color="#1f2a44", size=(90, 120)), 10**7)
    )


class CountingTagger:
    def __init__(self) -> None:
        self.calls = 0

    async def tag(self, image_bytes: bytes) -> TagResult:
        self.calls += 1
        return await FakeTagger().tag(image_bytes)


class FailingTagger:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def tag(self, image_bytes: bytes) -> TagResult:
        raise self.error


def with_tagger(deps: PipelineDeps, tagger: object) -> PipelineDeps:
    return replace(deps, tagger_factory=lambda: tagger)  # type: ignore[arg-type,return-value]


def test_pending_to_processing_to_done(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)
    assert item.tagging_status == TaggingStatus.PENDING
    seen: list[TaggingStatus] = []

    class SpyTagger:
        async def tag(self, image_bytes: bytes) -> TagResult:
            seen.append(session.get(Item, item.id, populate_existing=True).tagging_status)  # type: ignore[union-attr]
            return await FakeTagger().tag(image_bytes)

    process_item(iid(item), with_tagger(pipeline_deps, SpyTagger()))

    assert seen == [TaggingStatus.PROCESSING]
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE  # type: ignore[comparison-overlap]
    assert item.tagging_error is None
    assert item.cutout_image_path == cutout_key(item.image_path)
    assert item.tags_confirmed is False
    assert item.category is None  # AI proposes; confirmed columns stay untouched
    assert item.ai_tags is not None and item.ai_tags["tags"]["subtype"]
    assert item.ai_tags["backend"] == "fake"
    assert item.ai_tags["dominant_colors"][0][0] == "navy"
    assert set(item.ai_confidence or {}) >= {"category", "subtype", "primary_color"}
    assert len(stored_files(storage)) == 4  # original, thumb, cutout, cutout thumb


def test_pending_to_processing_to_failed_stores_error(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)
    failing = FailingTagger(TaggingError("model said no"))
    process_item(iid(item), with_tagger(pipeline_deps, failing))
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.FAILED
    assert item.tagging_error == "model said no"
    assert item.cutout_image_path is None and item.ai_tags is None
    assert len(stored_files(storage)) == 2  # cutout files cleaned up after the failure


def test_unexpected_error_is_stored_without_internals(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)
    boom = FailingTagger(RuntimeError("/secret/path sk-ant-123"))
    process_item(iid(item), with_tagger(pipeline_deps, boom))
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.FAILED
    assert item.tagging_error == "Unexpected error (RuntimeError)."


def test_process_item_twice_is_safe(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)
    tagger = CountingTagger()
    deps = with_tagger(pipeline_deps, tagger)
    process_item(iid(item), deps)
    session.refresh(item)
    first_tags = dict(item.ai_tags or {})
    process_item(iid(item), deps)  # already done: nothing to claim
    session.refresh(item)
    assert tagger.calls == 1
    assert item.tagging_status == TaggingStatus.DONE and item.ai_tags == first_tags
    assert len(stored_files(storage)) == 4


def test_failed_item_can_be_processed_again(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)
    process_item(iid(item), with_tagger(pipeline_deps, FailingTagger(TaggingError("x"))))
    process_item(iid(item), pipeline_deps)
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE and item.tagging_error is None


def test_fresh_processing_is_not_stolen_but_stale_is_reclaimed(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)
    tagger = CountingTagger()
    deps = with_tagger(pipeline_deps, tagger)
    session.execute(
        update(Item).where(Item.id == item.id).values(tagging_status=TaggingStatus.PROCESSING)  # type: ignore[arg-type]
    )
    session.commit()
    process_item(iid(item), deps)  # another worker is on it
    assert tagger.calls == 0

    stale = datetime.now(UTC) - timedelta(hours=1)
    session.execute(update(Item).where(Item.id == item.id).values(updated_at=stale))  # type: ignore[arg-type]
    session.commit()
    process_item(iid(item), deps)  # crashed worker: reclaim
    session.refresh(item)
    assert tagger.calls == 1 and item.tagging_status == TaggingStatus.DONE


def test_missing_item_is_a_noop(pipeline_deps: PipelineDeps) -> None:
    process_item(999_999, pipeline_deps)


def test_cutout_failure_degrades_but_still_tags(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    def broken_remover(image: object) -> object:
        raise RuntimeError("model download failed")

    item = pending_item(session, storage, user_a)
    process_item(iid(item), replace(pipeline_deps, remover=broken_remover))  # type: ignore[arg-type]
    session.refresh(item)
    assert item.tagging_status == TaggingStatus.DONE
    assert item.cutout_image_path is None
    assert any("Background removal failed" in f for f in item.ai_tags["flags"])  # type: ignore[index]
    assert item.ai_tags["dominant_colors"][0][0] == "navy"  # type: ignore[index]


def test_item_deleted_mid_run_leaves_no_files(
    session: Session, storage: LocalDiskStorage, user_a: User, pipeline_deps: PipelineDeps
) -> None:
    item = pending_item(session, storage, user_a)

    class DeletingTagger:
        async def tag(self, image_bytes: bytes) -> TagResult:
            delete_item(session, storage, item)
            return await FakeTagger().tag(image_bytes)

    process_item(iid(item), with_tagger(pipeline_deps, DeletingTagger()))
    assert stored_files(storage) == []


@pytest.mark.parametrize("backend", ["fake"])
def test_ai_tags_record_the_backend(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    pipeline_deps: PipelineDeps,
    backend: str,
) -> None:
    item = pending_item(session, storage, user_a)
    process_item(iid(item), pipeline_deps)
    session.refresh(item)
    assert item.ai_tags is not None and item.ai_tags["backend"] == backend
