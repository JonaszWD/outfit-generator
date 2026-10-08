"""Shared helpers for the demo scripts: a throwaway account that logs in over HTTP."""

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine
from app.services import account
from app.services.auth import create_user, find_user
from app.services.storage import LocalDiskStorage


@contextmanager
def demo_account() -> Iterator[tuple[str, str]]:
    """Register a throwaway account, yield (email, password), delete it and its files after."""
    email = f"demo-{secrets.token_hex(4)}@example.com"
    password = secrets.token_urlsafe(18)
    with Session(get_engine()) as session:
        assert create_user(session, email, password) is not None
    try:
        yield email, password
    finally:
        with Session(get_engine()) as session:
            user = find_user(session, email)
            if user is not None:
                account.delete_account(session, LocalDiskStorage(get_settings().media_root), user)


def page_token(html: str) -> str:
    match = re.search(r'X-CSRF-Token": "([^"]+)"', html)
    if match is None:
        raise SystemExit("the page carried no CSRF token")
    return match.group(1)


def log_in(client: httpx.Client, email: str, password: str) -> str:
    """Log in the way a browser does and return the session's CSRF token."""
    token = page_token(client.get("/login").text)
    response = client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token}
    )
    if response.status_code != 303:
        raise SystemExit(f"login failed: {response.status_code}")
    return page_token(client.get("/closet").text)
