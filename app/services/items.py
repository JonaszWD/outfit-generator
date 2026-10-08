import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.engine.config import EngineConfig
from app.models import Availability, Item, Source, TaggingStatus, User
from app.schemas import ItemData, ItemFilters
from app.services.analysis import AnalysisConfig, Redundancy, find_redundancy
from app.services.imaging import ProcessedImage
from app.services.outfits import delete_outfits_with_item, load_closet, to_view
from app.services.storage import Storage, derived_keys, new_image_keys

logger = logging.getLogger(__name__)

PAGE_SIZE = 24


def get_owned_item(session: Session, user: User, item_id: int) -> Item:
    """The only way routes load a single item. Missing and foreign items are both 404."""
    # Candidates (products being considered) are not part of the closet: 404 here.
    item = session.exec(
        select(Item).where(Item.id == item_id, Item.user_id == user.id, Item.source == Source.OWNED)
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    return item


@dataclass(frozen=True)
class ItemPage:
    items: list[Item]
    total: int
    page: int
    has_more: bool


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_items(
    session: Session,
    user: User,
    filters: ItemFilters,
    page: int = 1,
    per_page: int = PAGE_SIZE,
) -> ItemPage:
    page = max(page, 1)
    conditions = [col(Item.user_id) == user.id, col(Item.source) == Source.OWNED]
    if filters.show == "archived":
        conditions.append(col(Item.archived_at).is_not(None))
    else:  # archived items are out of sight until asked for
        conditions.append(col(Item.archived_at).is_(None))
        if filters.show:
            conditions.append(col(Item.availability) == Availability(filters.show))
    if filters.category:
        conditions.append(col(Item.category) == filters.category)
    if filters.color:
        conditions.append(col(Item.primary_color) == filters.color)
    if filters.season:
        conditions.append(col(Item.seasons).contains([filters.season]))
    if filters.needs_review:
        conditions.append(col(Item.tagging_status) == TaggingStatus.DONE)
        conditions.append(col(Item.tags_confirmed).is_(False))
    if filters.q:
        pattern = _like_pattern(filters.q)
        conditions.append(
            or_(
                col(Item.name).ilike(pattern, escape="\\"),
                col(Item.subtype).ilike(pattern, escape="\\"),
            )
        )
    total = session.exec(select(func.count()).select_from(Item).where(*conditions)).one()
    items = list(
        session.exec(
            select(Item)
            .where(*conditions)
            .order_by(col(Item.created_at).desc(), col(Item.id).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    )
    return ItemPage(items, total, page, has_more=page * per_page < total)


def delete_keys(storage: Storage, keys: list[str]) -> None:
    for k in keys:
        try:
            storage.delete(k)
        except OSError:
            logger.exception("Could not delete stored file %s", k)


def _delete_files(storage: Storage, key: str) -> None:
    """Delete an item's original, thumbnail, cutout and cutout thumbnail."""
    delete_keys(storage, list(derived_keys(key)))


def _store(storage: Storage, user_id: int, image: ProcessedImage) -> str:
    key, thumb = new_image_keys(user_id)
    try:
        storage.save(key, image.main)
        storage.save(thumb, image.thumb)
    except Exception:
        _delete_files(storage, key)
        raise
    return key


def create_item(
    session: Session, storage: Storage, user: User, data: ItemData, image: ProcessedImage
) -> Item:
    assert user.id is not None
    key = _store(storage, user.id, image)
    try:
        item = Item(
            user_id=user.id,
            image_path=key,
            original_image_path=key,
            tagging_status=TaggingStatus.DONE,
            tags_confirmed=True,
            **data.model_dump(),
        )
        session.add(item)
        session.commit()
    except Exception:
        session.rollback()
        _delete_files(storage, key)  # no orphan files when the DB write fails
        raise
    session.refresh(item)
    return item


def create_pending_item(
    session: Session,
    storage: Storage,
    user: User,
    image: ProcessedImage,
    source: Source = Source.OWNED,
) -> Item:
    """Save the normalized original and create an untagged item awaiting the pipeline."""
    assert user.id is not None
    key = _store(storage, user.id, image)
    try:
        item = Item(user_id=user.id, image_path=key, original_image_path=key, source=source)
        session.add(item)
        session.commit()
    except Exception:
        session.rollback()
        _delete_files(storage, key)
        raise
    session.refresh(item)
    return item


def update_item(
    session: Session,
    storage: Storage,
    item: Item,
    data: ItemData,
    image: ProcessedImage | None = None,
) -> Item:
    """Save user-confirmed tags. A replacement photo resets the item to pending (re-tag)."""
    old_key = item.image_path
    new_key = _store(storage, item.user_id, image) if image else None
    try:
        for field, value in data.model_dump().items():
            setattr(item, field, value)
        item.tags_confirmed = True
        if new_key:
            item.image_path = new_key
            item.original_image_path = new_key
            item.cutout_image_path = None
            item.ai_tags = None
            item.ai_confidence = None
            item.tagging_status = TaggingStatus.PENDING
            item.tagging_error = None
        session.add(item)
        session.commit()
    except Exception:
        session.rollback()
        if new_key:
            _delete_files(storage, new_key)
        raise
    if new_key:
        _delete_files(storage, old_key)
    session.refresh(item)
    return item


def request_retag(session: Session, item: Item) -> bool:
    """Reset an item to pending so the pipeline can claim it. False if already running."""
    if item.tagging_status == TaggingStatus.PROCESSING:
        return False
    item.tagging_status = TaggingStatus.PENDING
    item.tagging_error = None
    session.add(item)
    session.commit()
    session.refresh(item)
    return True


def ai_item_data(item: Item) -> ItemData | None:
    """The AI-proposed tags as validated ItemData, or None if absent or invalid."""
    tags = (item.ai_tags or {}).get("tags")
    if not tags:
        return None
    try:
        return ItemData.model_validate(tags)
    except ValueError:
        return None


def items_awaiting_review(session: Session, user: User) -> list[Item]:
    return list(
        session.exec(
            select(Item).where(
                Item.user_id == user.id,
                col(Item.source) == Source.OWNED,
                col(Item.tagging_status) == TaggingStatus.DONE,
                col(Item.tags_confirmed).is_(False),
            )
        )
    )


def confirm_all_from_ai(session: Session, items: list[Item]) -> int:
    """Confirm every item with its AI tags. Returns how many were confirmed."""
    confirmed = 0
    for item in items:
        data = ai_item_data(item)
        if data is None:
            continue
        for field, value in data.model_dump().items():
            if field in ("name", "notes") and getattr(item, field):
                continue
            setattr(item, field, value)
        item.tags_confirmed = True
        session.add(item)
        confirmed += 1
    session.commit()
    return confirmed


def set_availability(session: Session, item: Item, availability: Availability) -> Item:
    item.availability = availability
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def archive_item(session: Session, item: Item) -> Item:
    """Take an item out of the closet without losing it: it keeps its photo and tags, drops out
    of suggestions, counts and stats, and can be restored."""
    item.archived_at = datetime.now(UTC)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def restore_item(session: Session, item: Item) -> Item:
    item.archived_at = None
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def duplicate_warning(
    session: Session, user: User, item: Item, config: EngineConfig, analysis: AnalysisConfig
) -> Redundancy | None:
    """The closet piece this one looks like, when the redundancy logic says it is (nearly) the
    same. Compared against everything the user still has, laundry included."""
    view = to_view(item)
    if view is None or item.archived_at is not None:
        return None
    others = [v for v in load_closet(session, user).views if v.id != view.id]
    found = find_redundancy(others, view, analysis)
    return found if found.level != "none" else None


def delete_item(session: Session, storage: Storage, item: Item) -> None:
    key = item.image_path
    try:
        if item.id is not None:
            delete_outfits_with_item(session, item.user_id, item.id)
        session.delete(item)
        session.commit()
    except Exception:
        session.rollback()
        raise
    _delete_files(storage, key)
