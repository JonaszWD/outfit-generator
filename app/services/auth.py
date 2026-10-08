"""Accounts: users, server-side sessions, password resets and attempt counting.

Sessions are rows in Postgres. The cookie carries a random 256-bit token; only its SHA-256 is
stored, so a leaked database cannot be replayed as cookies, and a session can be revoked at
any time (logout, "log out everywhere", password change). Reset tokens work the same way.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import AuthAttempt, PasswordReset, User, UserSession
from app.services.passwords import hash_password, verify_password

SESSION_COOKIE = "wardrobe_session"
RESET_LIFETIME = timedelta(hours=1)
ATTEMPT_RETENTION = timedelta(days=1)
TOUCH_INTERVAL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ------------------------------------------------------------------------------ users


def find_user(session: Session, email: str) -> User | None:
    return session.exec(select(User).where(User.email == email)).first()


def create_user(session: Session, email: str, password: str) -> User | None:
    """None if the email is taken. The password is hashed either way (same timing)."""
    hashed = hash_password(password)
    user = User(email=email, hashed_password=hashed)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    session.refresh(user)
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = find_user(session, email)
    ok = verify_password(password, user.hashed_password if user else None)
    return user if (ok and user is not None) else None


def set_password(session: Session, user: User, password: str) -> None:
    user.hashed_password = hash_password(password)
    session.add(user)
    session.commit()


# --------------------------------------------------------------------------- sessions


def create_session(session: Session, user: User, settings: Settings) -> str:
    """A new login. Returns the raw cookie token (never stored)."""
    assert user.id is not None
    token = secrets.token_urlsafe(32)
    now = _now()
    session.execute(
        delete(UserSession).where(
            col(UserSession.user_id) == user.id, col(UserSession.expires_at) < now
        )
    )
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=digest(token),
            expires_at=now + timedelta(days=settings.session_days),
            last_seen_at=now,
        )
    )
    session.commit()
    return token


def user_for_token(session: Session, token: str) -> User | None:
    row = session.exec(
        select(UserSession).where(
            UserSession.token_hash == digest(token), col(UserSession.expires_at) > _now()
        )
    ).first()
    if row is None:
        return None
    now = _now()
    if now - row.last_seen_at > TOUCH_INTERVAL:
        row.last_seen_at = now
        session.add(row)
        session.commit()
    return session.get(User, row.user_id)


def revoke_session(session: Session, token: str) -> None:
    session.execute(delete(UserSession).where(col(UserSession.token_hash) == digest(token)))
    session.commit()


def revoke_all_sessions(session: Session, user_id: int) -> int:
    result = session.execute(delete(UserSession).where(col(UserSession.user_id) == user_id))
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def count_sessions(session: Session, user_id: int) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.user_id == user_id, col(UserSession.expires_at) > _now())
        ).one()
    )


# -------------------------------------------------------------------- password resets


def create_reset_token(session: Session, user: User) -> str:
    """A fresh single-use token; earlier unused ones are invalidated."""
    assert user.id is not None
    session.execute(
        delete(PasswordReset).where(
            col(PasswordReset.user_id) == user.id, col(PasswordReset.used_at).is_(None)
        )
    )
    token = secrets.token_urlsafe(32)
    session.add(
        PasswordReset(user_id=user.id, token_hash=digest(token), expires_at=_now() + RESET_LIFETIME)
    )
    session.commit()
    return token


def reset_token_valid(session: Session, token: str) -> bool:
    row = session.exec(
        select(PasswordReset).where(PasswordReset.token_hash == digest(token))
    ).first()
    return row is not None and row.used_at is None and row.expires_at > _now()


def reset_token_owner(session: Session, token: str) -> User | None:
    row = session.exec(
        select(PasswordReset).where(PasswordReset.token_hash == digest(token))
    ).first()
    return session.get(User, row.user_id) if row else None


def consume_reset_token(session: Session, token: str) -> User | None:
    """Atomically use a token. Returns its user, or None if unknown, used or expired."""
    result = session.execute(
        update(PasswordReset)
        .where(
            col(PasswordReset.token_hash) == digest(token),
            col(PasswordReset.used_at).is_(None),
            col(PasswordReset.expires_at) > _now(),
        )
        .values(used_at=_now())
        .returning(col(PasswordReset.user_id))
    )
    row = result.first()
    session.commit()
    return session.get(User, row[0]) if row else None


# -------------------------------------------------------------------- attempt counting


def record_attempt(session: Session, kind: str, subject: str) -> None:
    """Count an attempt. `subject` (an IP or email) is stored only as a hash."""
    session.execute(
        delete(AuthAttempt).where(col(AuthAttempt.created_at) < _now() - ATTEMPT_RETENTION)
    )
    session.add(AuthAttempt(kind=kind, key=digest(f"{kind}:{subject}")))
    session.commit()


def attempts_in_window(session: Session, kind: str, subject: str, window: timedelta) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(AuthAttempt)
            .where(
                AuthAttempt.kind == kind,
                AuthAttempt.key == digest(f"{kind}:{subject}"),
                col(AuthAttempt.created_at) > _now() - window,
            )
        ).one()
    )


def clear_attempts(session: Session, kind: str, subject: str) -> None:
    session.execute(
        delete(AuthAttempt).where(
            col(AuthAttempt.kind) == kind, col(AuthAttempt.key) == digest(f"{kind}:{subject}")
        )
    )
    session.commit()


def over_limit(session: Session, kind: str, subject: str, limit: int, window: timedelta) -> bool:
    return attempts_in_window(session, kind, subject, window) >= limit
