"""Accounts: registration, login, sessions, password change and reset, rate limits."""

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import AuthAttempt, PasswordReset, User, UserSession
from app.services import auth as auth_service
from tests.conftest import (
    EMAIL,
    PASSWORD,
    Outbox,
    form_token,
    log_in,
    new_client,
    page_token,
    sign_up,
    signed_in,
    submit,
)

SESSION = auth_service.SESSION_COOKIE


def set_cookie_header(response: httpx.Response, name: str) -> str:
    return next(v for v in response.headers.get_list("set-cookie") if v.startswith(f"{name}="))


def reset_link(outbox: Outbox, to: str = EMAIL) -> str:
    (_, _, body) = outbox.to(to)[-1]
    match = re.search(r"/reset/([\w-]+)", body)
    assert match
    return match.group(1)


# ------------------------------------------------------------------- registration


async def test_register_creates_an_account_with_an_argon2_hash(
    auth_app: FastAPI, session: Session
) -> None:
    async with new_client(auth_app) as client:
        response = await sign_up(client)
    assert response.status_code == 303 and response.headers["location"] == "/login?msg=registered"
    user = session.exec(select(User).where(User.email == EMAIL)).one()
    assert user.hashed_password and user.hashed_password.startswith("$argon2id$")
    assert PASSWORD not in user.hashed_password


async def test_emails_are_normalized(auth_app: FastAPI, session: Session) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client, "  Me@Example.COM ")
        assert (await log_in(client, "ME@example.com")).status_code == 303
    assert session.exec(select(User).where(User.email == "me@example.com")).one()


async def test_registering_an_existing_email_looks_exactly_the_same(
    auth_app: FastAPI, session: Session, outbox: Outbox
) -> None:
    async with new_client(auth_app) as first, new_client(auth_app) as second:
        fresh = await sign_up(first)
        taken = await sign_up(second, EMAIL, "a completely different password")
        landing_fresh = await first.get(fresh.headers["location"])
        landing_taken = await second.get(taken.headers["location"])
    assert (fresh.status_code, fresh.headers["location"]) == (
        taken.status_code,
        taken.headers["location"],
    )
    assert landing_fresh.text.replace(
        page_token(landing_fresh.text), ""
    ) == landing_taken.text.replace(page_token(landing_taken.text), "")
    assert len(list(session.exec(select(User).where(User.email == EMAIL)))) == 1
    # the real owner is told, quietly
    assert [subject for _, subject, _ in outbox.to(EMAIL)] == [
        "Someone tried to register with your email"
    ]
    # ... and the original password still works, the attempted one does not
    async with new_client(auth_app) as client:
        assert (await log_in(client, EMAIL, "a completely different password")).status_code == 401
        assert (await log_in(client)).status_code == 303


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        (EMAIL, "short", "at least 10 characters"),
        (EMAIL, "password123", "too common"),
        (EMAIL, "Password1234", "too common"),
        (EMAIL, "qwertyuiop", "too common"),
        (EMAIL, "aaaaaaaaaaaa", "mix of different characters"),
        ("jonathan@example.com", "my-jonathan-secret", "email address in your password"),
        ("not-an-email", "correct horse battery", "valid email"),
        ("", "correct horse battery", "valid email"),
        ("a@b", "correct horse battery", "valid email"),
        (EMAIL, "x" * 300, "too long"),
    ],
)
async def test_weak_credentials_are_rejected(
    auth_app: FastAPI, session: Session, email: str, password: str, message: str
) -> None:
    async with new_client(auth_app) as client:
        response = await sign_up(client, email, password)
    assert response.status_code == 422 and message in response.text
    assert (
        session.exec(select(User).where(User.email == email.strip().lower())).first() is None
        or (email == "jonathan@example.com") is False
    )


async def test_the_minimum_password_length_is_a_setting(
    auth_app: FastAPI, settings: Settings
) -> None:
    from app.config import get_settings as real_get_settings

    auth_app.dependency_overrides[real_get_settings] = lambda: settings.model_copy(
        update={"min_password_length": 14}
    )
    async with new_client(auth_app) as client:
        response = await sign_up(client, EMAIL, "only twelve ch")  # 14 chars is the minimum
        assert response.status_code == 303
        short = await sign_up(client, "x@example.com", "twelve chars")
        assert short.status_code == 422 and "at least 14" in short.text


# ------------------------------------------------------------------------- login


async def test_login_sets_a_safe_session_cookie(auth_app: FastAPI, session: Session) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        response = await log_in(client)
        assert response.status_code == 303 and response.headers["location"] == "/closet"
        header = set_cookie_header(response, SESSION).lower()
        assert "httponly" in header and "samesite=lax" in header and "path=/" in header
        assert "secure" not in header.replace("samesite", "")  # dev default: plain http works
        assert (await client.get("/closet")).status_code == 200
    token = response.cookies[SESSION]
    stored = session.exec(select(UserSession)).one()
    assert stored.token_hash == auth_service.digest(token) and token not in stored.token_hash
    assert stored.expires_at > datetime.now(UTC) + timedelta(days=13)


async def test_the_secure_flag_is_configurable(auth_app: FastAPI, settings: Settings) -> None:
    secure = settings.model_copy(update={"cookie_secure": True})
    auth_app.dependency_overrides[get_settings] = lambda: secure
    async with new_client(auth_app) as client:
        await sign_up(client)
        response = await log_in(client)
    assert "secure" in set_cookie_header(response, SESSION).lower()
    assert Settings(database_url="x", env="production").secure_cookies is True
    assert Settings(database_url="x", env="development").secure_cookies is False


@pytest.mark.parametrize("who", ["unknown@example.com", EMAIL])
async def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(
    auth_app: FastAPI, who: str
) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        response = await log_in(client, who, "definitely wrong password")
    assert response.status_code == 401 and "Incorrect email or password." in response.text
    assert SESSION not in response.headers.get("set-cookie", "")


async def test_login_rotates_the_session(auth_app: FastAPI, session: Session) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        first = await log_in(client)
        old = first.cookies[SESSION]
        second = await submit(  # logging in again while holding a session
            client, "/login", {"email": EMAIL, "password": PASSWORD}, form_page="/account"
        )
        new = second.cookies[SESSION]
    assert old != new
    assert auth_service.user_for_token(session, old) is None
    assert auth_service.user_for_token(session, new) is not None


@pytest.mark.parametrize(
    ("next_url", "expected"),
    [
        ("/outfits", "/outfits"),
        ("/closet?review=1", "/closet?review=1"),
        ("//evil.example/x", "/closet"),
        ("https://evil.example/", "/closet"),
        ("/\\evil.example", "/closet"),
        ("javascript:alert(1)", "/closet"),
        ("", "/closet"),
    ],
)
async def test_login_only_redirects_to_local_paths(
    auth_app: FastAPI, next_url: str, expected: str
) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        response = await submit(
            client, "/login", {"email": EMAIL, "password": PASSWORD, "next": next_url}
        )
    assert response.headers["location"] == expected


async def test_protected_pages_send_you_to_login_and_back(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as client:
        response = await client.get("/outfits?x=1")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?msg=login&next=%2Foutfits%3Fx%3D1"
        page = await client.get(response.headers["location"])
        assert "Please log in to continue." in page.text
        assert 'name="next" value="/outfits?x=1"' in page.text


async def test_unauthenticated_htmx_and_json_requests(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as client:
        htmx = await client.get("/items/1/status", headers={"HX-Request": "true"})
        assert htmx.status_code == 200 and htmx.headers["hx-redirect"].startswith("/login")
        api = await client.get("/closet/stats", headers={"Accept": "application/json"})
        assert api.status_code == 401 and api.json() == {"detail": "Not authenticated"}


async def test_logged_in_users_skip_the_login_page(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as client:
        assert (await client.get("/login")).headers["location"] == "/closet"
        assert (await client.get("/")).headers["location"] == "/closet"


# ------------------------------------------------------------- logout and expiry


async def test_logout_ends_the_session(auth_app: FastAPI, session: Session) -> None:
    async with signed_in(auth_app) as client:
        token = client.cookies[SESSION]
        response = await submit(client, "/logout", form_page="/closet")
        assert (
            response.status_code == 303 and response.headers["location"] == "/login?msg=loggedout"
        )
        assert SESSION not in client.cookies
        assert (await client.get("/closet")).headers["location"].startswith("/login")
    assert auth_service.user_for_token(session, token) is None
    assert session.exec(select(UserSession)).first() is None


async def test_an_expired_session_no_longer_works(auth_app: FastAPI, session: Session) -> None:
    async with signed_in(auth_app) as client:
        assert (await client.get("/closet")).status_code == 200
        row = session.exec(select(UserSession)).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
        session.commit()
        response = await client.get("/closet")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


async def test_the_session_lifetime_is_a_setting(
    auth_app: FastAPI, settings: Settings, session: Session
) -> None:
    short = settings.model_copy(update={"session_days": 1})
    auth_app.dependency_overrides[get_settings] = lambda: short
    async with signed_in(auth_app):
        pass
    row = session.exec(select(UserSession)).one()
    assert row.expires_at < datetime.now(UTC) + timedelta(days=1, minutes=1)


async def test_a_forged_or_garbled_cookie_is_ignored(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as client:
        for value in ("nonsense", "a" * 43, "", "x" * 5000):
            client.cookies.set(SESSION, value)
            assert (await client.get("/closet")).status_code == 303


async def test_log_out_everywhere_ends_every_session(auth_app: FastAPI, session: Session) -> None:
    async with signed_in(auth_app) as laptop, new_client(auth_app) as phone:
        await log_in(phone)
        assert (await phone.get("/closet")).status_code == 200
        response = await submit(laptop, "/account/logout-everywhere", form_page="/account")
        assert response.headers["location"] == "/login?msg=loggedout"
        assert (await phone.get("/closet")).status_code == 303
        assert (await laptop.get("/closet")).status_code == 303
    assert session.exec(select(UserSession)).first() is None


async def test_logging_out_everywhere_leaves_other_accounts_alone(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as mine, signed_in(auth_app, "other@example.com") as theirs:
        await submit(mine, "/account/logout-everywhere", form_page="/account")
        assert (await theirs.get("/closet")).status_code == 200


# ------------------------------------------------------------- change password


async def change(client: httpx.AsyncClient, current: str, new: str) -> httpx.Response:
    return await submit(
        client,
        "/account/password",
        {"current_password": current, "new_password": new},
        form_page="/account",
    )


async def test_change_password(auth_app: FastAPI, session: Session) -> None:
    async with signed_in(auth_app) as here, new_client(auth_app) as elsewhere:
        await log_in(elsewhere)
        old_here = here.cookies[SESSION]
        response = await change(here, PASSWORD, "a brand new passphrase")
        assert (
            response.status_code == 303 and response.headers["location"] == "/account?ok=password"
        )
        assert here.cookies[SESSION] != old_here  # this device got a fresh session
        assert (await here.get("/closet")).status_code == 200
        assert (await elsewhere.get("/closet")).status_code == 303  # other devices were logged out
    async with new_client(auth_app) as client:
        assert (await log_in(client, EMAIL, PASSWORD)).status_code == 401
        assert (await log_in(client, EMAIL, "a brand new passphrase")).status_code == 303


async def test_change_password_checks_the_current_one_and_the_policy(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as client:
        wrong = await change(client, "not my password", "a brand new passphrase")
        assert wrong.status_code == 401 and "current password is not right" in wrong.text
        weak = await change(client, PASSWORD, "password123")
        assert weak.status_code == 422 and "too common" in weak.text
        assert (await client.get("/closet")).status_code == 200  # nothing changed


async def test_changing_a_password_cannot_be_used_to_guess_the_old_one(
    auth_app: FastAPI, settings: Settings
) -> None:
    auth_app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"login_attempts_per_account": 3}
    )
    async with signed_in(auth_app) as client:
        for _ in range(3):
            assert (await change(client, "guess", "a brand new passphrase")).status_code == 401
        blocked = await change(client, PASSWORD, "a brand new passphrase")
        assert blocked.status_code == 429


# ------------------------------------------------------------ password reset


async def test_the_reset_flow(auth_app: FastAPI, session: Session, outbox: Outbox) -> None:
    async with signed_in(auth_app) as signed, new_client(auth_app) as anon:
        response = await submit(anon, "/forgot", {"email": EMAIL})
        assert response.status_code == 200 and "we have sent a link" in response.text
        token = reset_link(outbox)
        assert (await anon.get(f"/reset/{token}")).status_code == 200
        weak = await submit(
            anon, f"/reset/{token}", {"password": "password123"}, form_page=f"/reset/{token}"
        )
        assert weak.status_code == 422 and "too common" in weak.text
        done = await submit(
            anon,
            f"/reset/{token}",
            {"password": "my new reset passphrase"},
            form_page=f"/reset/{token}",
        )
        assert done.status_code == 303 and done.headers["location"] == "/login?msg=reset"
        assert (await signed.get("/closet")).status_code == 303  # existing logins are revoked
        async with new_client(auth_app) as other:
            assert (await log_in(other, EMAIL, PASSWORD)).status_code == 401
        assert (await log_in(anon, EMAIL, "my new reset passphrase")).status_code == 303


async def test_reset_tokens_are_single_use(auth_app: FastAPI, outbox: Outbox) -> None:
    async with new_client(auth_app) as anon:
        await sign_up(anon)
        await submit(anon, "/forgot", {"email": EMAIL})
        token = reset_link(outbox)
        first = await submit(
            anon,
            f"/reset/{token}",
            {"password": "first reset passphrase"},
            form_page=f"/reset/{token}",
        )
        assert first.status_code == 303
        again = await anon.get(f"/reset/{token}")
        assert again.status_code == 410 and "already used" in again.text
        second = await submit(
            anon, f"/reset/{token}", {"password": "second reset passphrase"}, form_page="/forgot"
        )
        assert second.status_code == 410
        assert (await log_in(anon, EMAIL, "first reset passphrase")).status_code == 303


async def test_expired_reset_tokens_are_refused(
    auth_app: FastAPI, session: Session, outbox: Outbox
) -> None:
    async with new_client(auth_app) as anon:
        await sign_up(anon)
        await submit(anon, "/forgot", {"email": EMAIL})
        token = reset_link(outbox)
        row = session.exec(select(PasswordReset)).one()
        assert (
            timedelta(minutes=59) < row.expires_at - row.created_at <= timedelta(hours=1, seconds=5)
        )
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
        session.commit()
        assert (await anon.get(f"/reset/{token}")).status_code == 410
        refused = await submit(
            anon, f"/reset/{token}", {"password": "never gets used here"}, form_page="/forgot"
        )
        assert refused.status_code == 410
        assert (await log_in(anon, EMAIL, PASSWORD)).status_code == 303  # password unchanged


async def test_reset_tokens_are_stored_hashed_and_a_new_request_replaces_the_old_one(
    auth_app: FastAPI, session: Session, outbox: Outbox
) -> None:
    async with new_client(auth_app) as anon:
        await sign_up(anon)
        await submit(anon, "/forgot", {"email": EMAIL})
        first = reset_link(outbox)
        await submit(anon, "/forgot", {"email": EMAIL})
        second = reset_link(outbox)
        assert first != second
        rows = list(session.exec(select(PasswordReset)))
        assert len(rows) == 1 and rows[0].token_hash == auth_service.digest(second)
        assert first not in rows[0].token_hash and second not in rows[0].token_hash
        assert (await anon.get(f"/reset/{first}")).status_code == 410
        assert (await anon.get(f"/reset/{second}")).status_code == 200


async def test_the_reset_form_never_reveals_whether_an_account_exists(
    auth_app: FastAPI, outbox: Outbox
) -> None:
    async with new_client(auth_app) as one, new_client(auth_app) as two:
        await sign_up(one)
        known = await submit(one, "/forgot", {"email": EMAIL})
        unknown = await submit(two, "/forgot", {"email": "nobody@example.com"})
        garbage = await submit(two, "/forgot", {"email": "not an email"})

    def strip(response: httpx.Response) -> str:
        return response.text.replace(page_token(response.text), "")

    assert known.status_code == unknown.status_code == garbage.status_code == 200
    assert strip(known) == strip(unknown) == strip(garbage)
    assert len(outbox.messages) == 1 and outbox.messages[0][0] == EMAIL


async def test_reset_requests_are_rate_limited_per_account_and_per_ip(
    auth_app: FastAPI, settings: Settings, outbox: Outbox
) -> None:
    async with new_client(auth_app) as anon:
        await sign_up(anon)
        for _ in range(5):
            response = await submit(anon, "/forgot", {"email": EMAIL})
            assert response.status_code == 200  # the answer never changes...
        assert len(outbox.to(EMAIL)) == 3  # ...but only 3 emails go out per hour
    auth_app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"reset_requests_per_ip": 2}
    )
    async with new_client(auth_app) as anon:
        await submit(anon, "/forgot", {"email": "a@example.com"})
        await submit(anon, "/forgot", {"email": "b@example.com"})
        blocked = await submit(anon, "/forgot", {"email": "c@example.com"})
        assert blocked.status_code == 429 and "Too many attempts" in blocked.text


# --------------------------------------------------------------------- rate limits


async def test_login_is_locked_after_too_many_failures_per_account(
    auth_app: FastAPI, session: Session
) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        for _ in range(5):
            assert (await log_in(client, EMAIL, "wrong password here")).status_code == 401
        locked = await log_in(client, EMAIL, PASSWORD)  # even the right password
        assert locked.status_code == 429 and "Too many attempts" in locked.text
        assert int(locked.headers["retry-after"]) > 0
        # an unrelated account from the same address is not locked by the account counter
        other = await sign_up(client, "other@example.com")
        assert other.status_code == 303
    # the window moves on
    old = datetime.now(UTC) - timedelta(minutes=16)
    for row in session.exec(select(AuthAttempt)):
        row.created_at = old
        session.add(row)
    session.commit()
    async with new_client(auth_app) as client:
        assert (await log_in(client)).status_code == 303


async def test_login_is_limited_per_ip_across_accounts(
    auth_app: FastAPI, settings: Settings
) -> None:
    auth_app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"login_attempts_per_ip": 4}
    )
    async with new_client(auth_app) as client:
        await sign_up(client)
        for n in range(4):
            await log_in(client, f"user{n}@example.com", "wrong password here")
        blocked = await log_in(client, EMAIL, PASSWORD)
        assert blocked.status_code == 429


async def test_a_successful_login_clears_the_accounts_failures(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        for _ in range(4):
            await log_in(client, EMAIL, "wrong password here")
        assert (await log_in(client)).status_code == 303
    async with new_client(auth_app) as again:  # a fresh browser: the counter started over
        for _ in range(4):
            assert (await log_in(again, EMAIL, "wrong password here")).status_code == 401


async def test_attempt_counters_never_store_the_email_or_ip(
    auth_app: FastAPI, session: Session
) -> None:
    async with new_client(auth_app) as client:
        await log_in(client, "someone@example.com", "wrong password here")
        await submit(client, "/forgot", {"email": "someone@example.com"})
    rows = list(session.exec(select(AuthAttempt)))
    assert rows
    for row in rows:
        assert "someone" not in row.key and "127.0.0.1" not in row.key and len(row.key) == 64


async def test_registration_is_rate_limited_per_ip(auth_app: FastAPI, settings: Settings) -> None:
    auth_app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"register_per_ip": 2}
    )
    async with new_client(auth_app) as client:
        for n in range(2):
            assert (await sign_up(client, f"n{n}@example.com")).status_code == 303
        assert (await sign_up(client, "n3@example.com")).status_code == 429


# ------------------------------------------------------------------------- CSRF


async def test_csrf_with_sessions_and_htmx(auth_app: FastAPI, session: Session) -> None:
    async with signed_in(auth_app) as client:
        assert (await client.post("/logout")).status_code == 403  # no token
        assert (await client.post("/logout", data={"csrf_token": "x" * 40})).status_code == 403
        token = page_token((await client.get("/account")).text)
        header = await client.post("/account/logout-everywhere", headers={"X-CSRF-Token": token})
        assert header.status_code == 303  # the HTMX way: a header


async def test_a_token_from_another_session_does_not_validate(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me, signed_in(auth_app, "other@example.com") as you:
        yours = page_token((await you.get("/account")).text)
        mine = page_token((await me.get("/account")).text)
        assert yours != mine
        assert (await me.post("/logout", data={"csrf_token": yours})).status_code == 403
        assert (await me.post("/logout", data={"csrf_token": mine})).status_code == 303


async def test_login_forms_have_login_csrf_protection(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as client:
        await sign_up(client)
        assert (
            await client.post("/login", data={"email": EMAIL, "password": PASSWORD})
        ).status_code == 403
        await form_token(client)
        assert (
            await client.post(
                "/login", data={"email": EMAIL, "password": PASSWORD, "csrf_token": "y" * 43}
            )
        ).status_code == 403


async def test_a_session_csrf_token_changes_after_login(auth_app: FastAPI) -> None:
    async with new_client(auth_app) as client:
        before = page_token((await client.get("/login")).text)
        await sign_up(client)
        await log_in(client)
        after = page_token((await client.get("/account")).text)
    assert before != after
