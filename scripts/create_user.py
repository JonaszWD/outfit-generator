"""Create an account from the command line, or give the passwordless dev account a password.

    make user EMAIL=you@example.com            # asks for the password
    PASSWORD=... make user EMAIL=you@example.com

Before Phase 5 the seeded dev user had no password. If an account with this email exists and
has none, this *claims* it: the password is set and every item and outfit stays attached.
An account that already has a password is never changed here (use the change-password page).
"""

import argparse
import getpass
import os

from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine
from app.models import User
from app.services.auth import create_user, find_user, set_password
from app.services.passwords import normalize_email, valid_email, validate_password


class UserError(Exception):
    """Something the person running the script can fix."""


def create_or_claim(session: Session, email: str, password: str, min_length: int) -> str:
    """Returns "created" or "claimed"; raises UserError otherwise."""
    email = normalize_email(email)
    if not valid_email(email):
        raise UserError(f"{email!r} is not a valid email address.")
    problem = validate_password(password, email, min_length)
    if problem:
        raise UserError(problem)
    existing: User | None = find_user(session, email)
    if existing is None:
        if create_user(session, email, password) is None:  # lost a race
            raise UserError(f"{email} already exists.")
        return "created"
    if existing.hashed_password is not None:
        raise UserError(f"{email} already has a password. Use the change-password page.")
    set_password(session, existing, password)
    return "claimed"


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default=settings.dev_user_email)
    args = parser.parse_args()
    password = os.environ.get("PASSWORD") or getpass.getpass("Password: ")
    try:
        with Session(get_engine()) as session:
            outcome = create_or_claim(session, args.email, password, settings.min_password_length)
    except UserError as exc:
        raise SystemExit(str(exc)) from None
    print(f"{outcome.capitalize()} {normalize_email(args.email)}. You can log in now.")


if __name__ == "__main__":
    main()
