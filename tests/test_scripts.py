"""The seed and create-user scripts work against real accounts."""

import pytest
from sqlmodel import Session, select

from app.models import Item, User
from app.services.auth import authenticate
from app.services.storage import LocalDiskStorage
from scripts.create_user import UserError, create_or_claim
from scripts.seed_closet import SEEDS, NoSuchAccount, seed_closet

PASSWORD = "a long enough passphrase"


def test_create_user_makes_an_account_that_can_log_in(session: Session) -> None:
    assert create_or_claim(session, "  New@Example.com ", PASSWORD, 10) == "created"
    user = authenticate(session, "new@example.com", PASSWORD)
    assert user is not None and user.hashed_password and PASSWORD not in user.hashed_password


def test_create_user_claims_the_passwordless_dev_user_and_keeps_its_data(
    session: Session, storage: LocalDiskStorage
) -> None:
    dev = User(email="dev@example.com")  # how the pre-Phase-5 dev user looks
    session.add(dev)
    session.flush()
    seed_closet(session, storage, "dev@example.com")
    assert create_or_claim(session, "dev@example.com", PASSWORD, 10) == "claimed"
    claimed = authenticate(session, "dev@example.com", PASSWORD)
    assert claimed is not None and claimed.id == dev.id
    assert len(session.exec(select(Item).where(Item.user_id == dev.id)).all()) == len(SEEDS)


def test_create_user_never_touches_an_account_that_has_a_password(session: Session) -> None:
    create_or_claim(session, "me@example.com", PASSWORD, 10)
    with pytest.raises(UserError, match="already has a password"):
        create_or_claim(session, "me@example.com", "another long passphrase", 10)
    assert authenticate(session, "me@example.com", PASSWORD) is not None


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("not-an-email", PASSWORD, "valid email"),
        ("me@example.com", "short", "at least 10"),
        ("me@example.com", "password123", "too common"),
    ],
)
def test_create_user_applies_the_same_rules_as_registration(
    session: Session, email: str, password: str, message: str
) -> None:
    with pytest.raises(UserError, match=message):
        create_or_claim(session, email, password, 10)
    assert session.exec(select(User)).all() == []


def test_seed_fills_a_given_account_and_is_idempotent(
    session: Session, storage: LocalDiskStorage
) -> None:
    create_or_claim(session, "me@example.com", PASSWORD, 10)
    other = User(email="other@example.com")
    session.add(other)
    session.flush()
    assert seed_closet(session, storage, "me@example.com") == (len(SEEDS), 0)
    assert seed_closet(session, storage, "me@example.com") == (0, len(SEEDS))
    assert session.exec(select(Item).where(Item.user_id == other.id)).all() == []


def test_seed_refuses_unknown_accounts_but_can_create_the_dev_user(
    session: Session, storage: LocalDiskStorage
) -> None:
    with pytest.raises(NoSuchAccount):
        seed_closet(session, storage, "nobody@example.com")
    assert seed_closet(session, storage, "dev@example.com", create_dev_user=True)[0] == len(SEEDS)
    dev = session.exec(select(User).where(User.email == "dev@example.com")).one()
    assert dev.hashed_password is None
