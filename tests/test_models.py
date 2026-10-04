from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models import Candidate, Category, Item, Outfit, Source, User


def make_user(session: Session, email: str = "a@example.com") -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def make_item(session: Session, user: User, **kwargs: object) -> Item:
    item = Item(user_id=user.id, image_path="img/1.jpg", category=Category.TOP, **kwargs)  # type: ignore[arg-type]
    session.add(item)
    session.flush()
    return item


def test_user_roundtrip(session: Session) -> None:
    user = make_user(session)
    session.expire_all()
    loaded = session.get(User, user.id)
    assert loaded is not None
    assert loaded.email == "a@example.com"
    assert loaded.hashed_password is None
    assert loaded.created_at is not None


def test_user_email_unique(session: Session) -> None:
    make_user(session)
    with pytest.raises(IntegrityError):
        make_user(session)
    session.rollback()


def test_item_roundtrip_with_arrays(session: Session) -> None:
    user = make_user(session)
    item = make_item(
        session,
        user,
        primary_color="navy",
        secondary_colors=["white", "red"],
        seasons=["spring", "fall"],
        formality=3,
    )
    session.expire_all()
    loaded = session.get(Item, item.id)
    assert loaded is not None
    assert loaded.category is Category.TOP
    assert loaded.source is Source.OWNED
    assert loaded.secondary_colors == ["white", "red"]
    assert loaded.seasons == ["spring", "fall"]
    assert loaded.formality == 3


@pytest.mark.parametrize("formality", [0, 6])
def test_item_formality_check_rejects(session: Session, formality: int) -> None:
    user = make_user(session)
    with pytest.raises(IntegrityError, match="ck_items_formality"):
        make_item(session, user, formality=formality)
    session.rollback()


@pytest.mark.parametrize("formality", [1, 5])
def test_item_formality_check_accepts_bounds(session: Session, formality: int) -> None:
    user = make_user(session)
    assert make_item(session, user, formality=formality).id is not None


def test_outfit_roundtrip_with_array(session: Session) -> None:
    user = make_user(session)
    outfit = Outfit(user_id=user.id, item_ids=[1, 2, 3], score=0.75, occasion="work")  # type: ignore[arg-type]
    session.add(outfit)
    session.flush()
    session.expire_all()
    loaded = session.get(Outfit, outfit.id)
    assert loaded is not None
    assert loaded.item_ids == [1, 2, 3]
    assert loaded.score == 0.75


def test_candidate_roundtrip_and_unique(session: Session) -> None:
    user = make_user(session)
    item = make_item(session, user, source=Source.CANDIDATE)
    cand = Candidate(
        user_id=user.id,  # type: ignore[arg-type]
        url="https://shop.example/p/1",
        title="Shirt",
        price=Decimal("29.90"),
        item_id=item.id,
    )
    session.add(cand)
    session.flush()
    session.expire_all()
    loaded = session.get(Candidate, cand.id)
    assert loaded is not None
    assert loaded.price == Decimal("29.90")
    assert loaded.item_id == item.id

    session.add(Candidate(user_id=user.id, url="https://shop.example/p/1"))  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
