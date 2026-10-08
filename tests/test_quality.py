"""Error pages, logging hygiene, and the accessibility checks that are cheap to automate."""

import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session

from app.services import feedback as feedback_svc
from app.services.outfits import suggest
from app.services.storage import LocalDiskStorage
from tests.conftest import EMAIL, PASSWORD, form_token, new_client, signed_in, submit
from tests.test_closet_routes import add_item
from tests.test_outfit_routes import closet
from tests.test_privacy import populate
from tests.test_sessions_isolation import user_row

FRAGMENTS = {"suggestions"}  # HTMX partials: no <html>, checked for CSP and labels only
CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()


# ------------------------------------------------------------------------- error pages


async def test_404_is_an_app_styled_page(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me:
        page = await me.get("/definitely/not/here", headers={"Accept": "text/html"})
    assert page.status_code == 404
    assert (
        "Not found" in page.text
        and "/static/style.css" in page.text
        and "Skip to content" in page.text
    )
    assert "Traceback" not in page.text and "detail" not in page.text.lower()


async def test_404_for_json_callers_stays_json(auth_app: FastAPI) -> None:
    async with signed_in(auth_app) as me:
        response = await me.get("/definitely/not/here", headers={"Accept": "application/json"})
    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}


async def test_500_is_a_friendly_page_with_no_internals(auth_app: FastAPI) -> None:
    def boom() -> None:
        raise RuntimeError("secret internal detail: db password hunter2")

    auth_app.add_api_route("/boom", boom, methods=["GET"])
    transport = httpx.ASGITransport(app=auth_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        html = await client.get("/boom", headers={"Accept": "text/html"})
        data = await client.get("/boom", headers={"Accept": "application/json"})
    assert html.status_code == 500 and "Something went wrong" in html.text
    assert "hunter2" not in html.text and "RuntimeError" not in html.text
    assert "content-security-policy" in html.headers  # even error pages carry the headers
    assert data.status_code == 500 and data.json() == {"detail": "Internal Server Error"}


async def test_403_and_409_pages_are_app_styled(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app) as me:
        forbidden = await me.post(
            "/logout", data={"csrf_token": "x" * 43}, headers={"Accept": "text/html"}
        )
        assert forbidden.status_code == 403 and "Not allowed" in forbidden.text


# ---------------------------------------------------------------------------- logging


async def test_logs_contain_no_personal_data(
    auth_app: FastAPI, caplog: pytest.LogCaptureFixture, session: Session, storage: LocalDiskStorage
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_name = "Grandmas-secret-sweater"
    async with signed_in(auth_app, "private.person@example.com") as me:
        user = user_row(session, "private.person@example.com")
        add_item(session, storage, user, name=secret_name)
        await me.get(f"/closet?q={secret_name}")
        await me.get("/settings/preferences")
        async with new_client(auth_app) as anon:
            await post_form_anon(anon, "/forgot", {"email": "private.person@example.com"})
            await post_form_anon(
                anon,
                "/login",
                {"email": "private.person@example.com", "password": "wrong-password-x"},
            )
    text = "\n".join(
        r.getMessage() + json.dumps(getattr(r, "fields", {}), default=str) + str(r.args)
        for r in caplog.records
    )
    for private in (
        "private.person",
        "example.com",
        "wrong-password-x",
        PASSWORD,
        secret_name,
        "127.0.0.1",
    ):
        assert private not in text, private
    assert any(r.name == "app.request" for r in caplog.records)  # but requests are logged
    routes = {
        getattr(r, "fields", {}).get("route") for r in caplog.records if r.name == "app.request"
    }
    assert "/closet" in routes and "/forgot" in routes


async def post_form_anon(
    client: httpx.AsyncClient, path: str, data: dict[str, str]
) -> httpx.Response:
    token = await form_token(client, "/login")
    return await client.post(path, data={**data, "csrf_token": token})


# ---------------------------------------------------------------------------- the pages


class Page(HTMLParser):
    """Collects what the checks need from one HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.labels_for: set[str] = set()
        self.label_depth = 0
        self.controls: list[tuple[str, dict[str, str | None], bool]] = []
        self.inline_script = False
        self.style_block = False
        self.in_script = False
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.tags.append((tag, a))
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.labels_for.add(a["for"] or "")
        if tag in ("input", "select", "textarea"):
            self.controls.append((tag, a, self.label_depth > 0))
        if tag == "script" and not a.get("src"):
            self.in_script = True
        if tag == "style":
            self.style_block = True
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        if tag == "script":
            self.in_script = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_script and data.strip():
            self.inline_script = True
        if self._in_title:
            self.title += data


def parse(html: str) -> Page:
    page = Page()
    page.feed(html)
    return page


@pytest.fixture
async def pages(auth_app: FastAPI, session: Session, storage: LocalDiskStorage) -> dict[str, str]:
    """Every page a logged-in person can see, rendered with data in it."""
    async with signed_in(auth_app) as me:
        user = user_row(session, EMAIL)
        populate(session, storage, user, with_cutout=False)
        item = add_item(session, storage, user, name="Navy shirt")
        outfit = suggest(
            session,
            user,
            __import__("app.engine.config", fromlist=["EngineConfig"]).EngineConfig(),
            __import__("app.engine.types", fromlist=["Context"]).Context(),
            rng_seed=1,
        ).outfits[0]
        feedback_svc.record_feedback(session, user, outfit, "up", None)
        urls = [
            "/closet",
            f"/items/{item.id}",
            f"/items/{item.id}/edit",
            "/items/new",
            "/outfits",
            "/planner",
            "/settings/preferences",
            "/account",
            "/analyze",
            "/candidates",
            "/closet/stats",
            "/closet?show=archived",
        ]
        rendered = {u: (await me.get(u)).text for u in urls}
        rendered["suggestions"] = (
            await submit(me, "/outfits/generate", {}, form_page="/outfits")
        ).text
        anon = new_client(auth_app)
        for u in ("/login", "/register", "/forgot", "/reset/not-a-real-token"):
            rendered[u] = (await anon.get(u)).text
        rendered["/"] = (await anon.get("/")).text
        await anon.aclose()
    return {k: v for k, v in rendered.items() if v}


async def test_pages_follow_the_csp_with_no_inline_code_or_foreign_resources(
    pages: dict[str, str],
) -> None:
    assert len(pages) >= 14
    allowed_hosts = {"fonts.googleapis.com", "fonts.gstatic.com"}
    for url, html in pages.items():
        page = parse(html)
        assert not page.inline_script, f"{url}: inline script"
        assert not page.style_block, f"{url}: <style> block"
        for tag, attrs in page.tags:
            assert not [k for k in attrs if k.startswith("on")], f"{url}: inline handler on <{tag}>"
            for key in ("src", "href", "action"):
                value = attrs.get(key) or ""
                if value.startswith(("http://", "https://", "//")):
                    host = re.sub(r"^(https?:)?//", "", value).split("/")[0]
                    if tag == "a":
                        continue  # a link out is not a load
                    assert host in allowed_hosts, f"{url}: <{tag} {key}={value}> breaks the CSP"
            assert attrs.get("hx-on") is None and not any(k.startswith("hx-on") for k in attrs), url
        if url not in FRAGMENTS:
            assert '"X-CSRF-Token"' in html, f"{url}: HTMX would send no CSRF token"


async def test_pages_have_the_basics_for_screen_readers_and_keyboards(
    pages: dict[str, str],
) -> None:
    for url, html in pages.items():
        if url in FRAGMENTS:
            continue
        page = parse(html)
        names = [t for t, _ in page.tags]
        assert '<html lang="en">' in html, url
        assert page.title.strip(), f"{url}: no <title>"
        assert names.count("h1") == 1, f"{url}: needs exactly one h1"
        assert any(a.get("id") == "main" for t, a in page.tags if t == "main"), url
        assert any(t == "a" and a.get("href") == "#main" for t, a in page.tags), f"{url}: skip link"
        assert any(t == "nav" and a.get("aria-label") for t, a in page.tags), url
        for tag, attrs in page.tags:
            if tag == "img":
                assert attrs.get("alt") is not None, f"{url}: <img> without alt"
            if tag == "button":
                assert attrs.get("type") in ("submit", "button", "reset"), f"{url}: untyped button"
            if tag == "div" and (attrs.get("onclick") or attrs.get("role") == "button"):
                pytest.fail(f"{url}: clickable div, use a button")
            assert attrs.get("tabindex") in (None, "0", "-1"), f"{url}: positive tabindex"


async def test_every_form_control_has_a_label(pages: dict[str, str]) -> None:
    checked = 0
    for url, html in pages.items():
        page = parse(html)
        for tag, attrs, inside_label in page.controls:
            if attrs.get("type") in ("hidden", "submit", "button", "reset"):
                continue
            labelled = (
                inside_label
                or attrs.get("aria-label")
                or attrs.get("aria-labelledby")
                or (attrs.get("id") and attrs["id"] in page.labels_for)
            )
            assert labelled, f"{url}: <{tag} name={attrs.get('name')!r}> has no label"
            checked += 1
    assert checked > 25


async def test_item_images_get_alt_text_from_their_tags(
    pages: dict[str, str],
) -> None:
    detail = next(h for u, h in pages.items() if re.fullmatch(r"/items/\d+", u))
    assert 'alt="Navy shirt"' in detail
    closet_page = pages["/closet"]
    alts = re.findall(r'<img[^>]*alt="([^"]*)"', closet_page)
    assert alts and all(a.strip() for a in alts)


async def test_outfit_card_alt_text_names_type_and_color(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app) as me:
        closet(session, storage, user_row(session, EMAIL))
        html = (await submit(me, "/outfits/generate", {}, form_page="/outfits")).text
    alts = re.findall(r'<img[^>]*alt="([^"]*)"', html)
    assert alts and all(re.search(r"\w+, [a-z ]+$", a) or a for a in alts)
    assert any(", " in a for a in alts)  # "jeans, blue"


async def test_the_rating_form_is_keyboard_usable_and_labelled(
    auth_app: FastAPI, session: Session, storage: LocalDiskStorage
) -> None:
    async with signed_in(auth_app) as me:
        closet(session, storage, user_row(session, EMAIL))
        html = (await submit(me, "/outfits/generate", {}, form_page="/outfits")).text
    assert re.search(r'<label[^>]*for="reason-\d+"', html)
    assert ">Like</button>" in html and ">Dislike</button>" in html  # text, not icon-only


# ---------------------------------------------------------------------------- contrast


def tokens() -> dict[str, str]:
    root = re.search(r":root\s*{(.*?)}", CSS, re.S)
    assert root
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", root.group(1)))


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (text token, background token) pairs the stylesheet actually uses for readable text.
TEXT_PAIRS = [
    ("ink", "canvas"),
    ("ink", "surface-card"),
    ("ink", "surface-strong"),
    ("body", "canvas"),
    ("body", "surface-card"),
    ("body", "surface-strong"),
    ("on-primary", "primary"),
    ("on-primary", "primary-active"),
    ("semantic-error", "surface-card"),
]


@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_meets_wcag_aa(fg: str, bg: str) -> None:
    t = tokens()
    assert contrast(t[fg], t[bg]) >= 4.5, f"{fg} on {bg}: {contrast(t[fg], t[bg]):.2f}"


def test_the_muted_color_is_not_used_for_text_it_cannot_carry() -> None:
    """--muted is 4.43:1 on --canvas, just under AA for small text, so no rule uses it."""
    t = tokens()
    assert contrast(t["muted"], t["canvas"]) < 4.5
    uses = re.findall(r"[^{}]*\{[^{}]*[^-]color:\s*var\(--muted\)[^{}]*\}", CSS)
    assert uses == []


def test_error_text_always_sits_on_a_white_plate() -> None:
    rule = re.search(r"\.error\s*{(.*?)}", CSS, re.S)
    assert (
        rule and "var(--surface-card)" in rule.group(1) and "var(--semantic-error)" in rule.group(1)
    )


def test_focus_is_visible_and_never_removed_without_a_replacement() -> None:
    assert re.search(r":focus-visible\s*{[^}]*outline:\s*2px solid var\(--ink\)", CSS)
    for block in re.findall(r"([^{}]+)\{([^{}]*outline:\s*none[^{}]*)\}", CSS):
        selector, body = block
        assert "border-color" in body or "box-shadow" in body, f"{selector.strip()}: no replacement"


def test_the_stylesheet_only_uses_design_tokens_for_colors() -> None:
    body = CSS.split("}", 1)[1]  # everything after the :root block
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", body.replace("var(", "")), "raw hex outside :root"


async def test_the_add_sheet_offers_exactly_library_and_camera(pages: dict[str, str]) -> None:
    html = pages["/items/new"]
    assert 'id="add-sheet"' in html and 'action="/items" enctype="multipart/form-data"' in html
    assert html.count('name="images"') == 2 and 'capture="environment"' in html
    assert re.search(r'<label for="pick-library">Choose from photos</label>', html)
    assert "Take photo now" in html and "No file chosen" not in html
    # the plus button opens it, as "More" opens its own sheet
    assert re.search(r'class="tab tab-add"[^>]*data-dialog-open="#add-sheet"', html)


async def test_static_files_are_revalidated_in_development(auth_app: FastAPI) -> None:
    """So a CSS or JS edit in the Docker bind mount shows on the next refresh."""
    async with new_client(auth_app) as client:
        response = await client.get("/static/style.css")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    assert "etag" in response.headers
