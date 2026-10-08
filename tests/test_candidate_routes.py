"""Candidate routes: paste a link or photo, poll, read the result, edit, add, discard."""

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
from fastapi import FastAPI
from sqlmodel import Session, select

from app.deps import get_candidate_deps
from app.models import Candidate, CandidateStatus, Item, Outfit, Source, User
from app.services.candidates import CandidateDeps
from app.services.storage import LocalDiskStorage
from tests.conftest import VALID_FORM, image_bytes, post_form, stored_files
from tests.shop import Shop, jpeg
from tests.test_candidates import StaticTagger, tag
from tests.test_outfit_routes import closet

LINK = "https://shop.example/p/shirt"


def stocked(shop: Shop, **kwargs: str | None) -> Shop:
    shop.add_product("/p/shirt", **kwargs)
    shop.images["/img/shirt.jpg"] = jpeg("#1f2a44")
    return shop


def use_tagger(app: FastAPI, deps: CandidateDeps, result: Any = None) -> StaticTagger:
    tagger = StaticTagger(result or tag())
    swapped = replace(deps, pipeline=replace(deps.pipeline, tagger_factory=lambda: tagger))
    app.dependency_overrides[get_candidate_deps] = lambda: swapped
    return tagger


async def analyze(client: httpx.AsyncClient, url: str = LINK) -> httpx.Response:
    return await post_form(client, "/candidates", {"url": url})


def candidate_id(response: httpx.Response) -> int:
    match = re.search(r"/candidates/(\d+)", response.headers["location"])
    assert match
    return int(match.group(1))


async def test_the_forms_render(client: httpx.AsyncClient) -> None:
    page = await client.get("/analyze")
    assert page.status_code == 200
    assert 'name="url"' in page.text and "Upload a photo instead" in page.text
    assert 'name="image"' in page.text and 'name="csrf_token"' in page.text
    assert (await client.get("/candidates")).status_code == 200


async def test_paste_a_link_and_read_the_result(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    stocked(shop)
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps)
    response = await analyze(client)
    assert response.status_code == 303
    cid = candidate_id(response)

    page = await client.get(f"/candidates/{cid}")
    assert page.status_code == 200
    assert "Slim Fit Oxford Shirt" in page.text and "Northfield" in page.text
    assert "New outfits" in page.text and 'class="display-xl headline-number"' in page.text
    assert re.search(r"Unlocks \d+ (new )?outfits?", page.text) or "Adds" in page.text
    assert "Outfits it would make" in page.text and "is-candidate" in page.text
    assert "Pairs best with" in page.text
    assert "Add to my closet" in page.text and "Discard" in page.text
    assert "hx-trigger" not in page.text  # finished: no polling
    assert "shop.example" in page.text and "utm" not in page.text

    panel = await client.get(f"/candidates/{cid}/panel")
    assert panel.status_code == 200 and "hx-trigger" not in panel.text

    data = (await client.get(f"/candidates/{cid}", headers={"Accept": "application/json"})).json()
    assert data["status"] == "done" and data["product"]["title"] == "Slim Fit Oxford Shirt"
    assert data["tags"]["subtype"] == "shirt" and data["in_closet"] is False
    assert data["result"]["new_outfits"] > 0 and data["result"]["verdict"]


async def test_the_panel_polls_only_while_the_job_is_unfinished(
    client: httpx.AsyncClient, session: Session, user_a: User
) -> None:
    pending = Candidate(user_id=user_a.id, url="https://shop.example/p/x", url_hash="b" * 64)  # type: ignore[arg-type]
    session.add(pending)
    session.commit()
    for status in (CandidateStatus.PENDING, CandidateStatus.PROCESSING):
        pending.status = status
        session.commit()
        panel = await client.get(f"/candidates/{pending.id}/panel")
        assert 'hx-trigger="every 2s"' in panel.text and "spinner" in panel.text
    pending.status = CandidateStatus.FAILED
    pending.error = "The site refused the request (HTTP 403)."
    session.commit()
    panel = await client.get(f"/candidates/{pending.id}/panel")
    assert "hx-trigger" not in panel.text and "HTTP 403" in panel.text


async def test_extraction_failure_shows_the_message_and_the_photo_fallback(
    client: httpx.AsyncClient, shop: Shop
) -> None:
    shop.pages["/p/shirt"] = "<html><title>Just a moment...</title><body>captcha</body></html>"
    response = await analyze(client)
    page = await client.get(f"/candidates/{candidate_id(response)}")
    assert page.status_code == 200  # not a 500
    assert "We couldn&#39;t analyze that" in page.text or "We couldn't analyze that" in page.text
    assert "bot-check" in page.text
    assert 'type="file"' in page.text and "Analyze photo" in page.text
    assert "Try the link again" in page.text


async def test_an_empty_closet_is_explained(
    client: httpx.AsyncClient, app: FastAPI, candidate_deps: CandidateDeps, shop: Shop
) -> None:
    stocked(shop)
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    page = await client.get(f"/candidates/{cid}")
    assert "Your closet is empty" in page.text and "Add photos" in page.text
    assert "closet first" in page.text


async def test_a_piece_that_makes_no_outfit_says_why(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    from app.models import Category
    from tests.test_closet_routes import add_item

    stocked(shop)
    shoes = {"category": Category.SHOES, "subtype": "sneakers", "primary_color": "white"}
    add_item(session, storage, user_a, **shoes)
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    page = await client.get(f"/candidates/{cid}")
    assert "You have no bottoms, so this top creates no outfits yet" in page.text
    assert "Why no outfits yet" in page.text


async def test_a_duplicate_is_called_out(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    from tests.test_closet_routes import add_item

    stocked(shop)
    items = closet(session, storage, user_a)
    own = add_item(
        session, storage, user_a, subtype="shirt", primary_color="navy",
        seasons=["spring", "summer", "autumn", "winter"],
    )  # fmt: skip
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    page = await client.get(f"/candidates/{cid}")
    assert "duplicates your navy shirt" in page.text
    assert "Near-identical" in page.text and f"/items/{own.id}" in page.text
    assert items  # the rest of the closet exists


async def test_editing_the_tags_updates_the_numbers(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    stocked(shop)
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps, tag(category="shoes", subtype="boots", primary_color="black"))
    cid = candidate_id(await analyze(client))
    first = (await client.get(f"/candidates/{cid}")).text

    form = await client.get(f"/candidates/{cid}/edit")
    assert form.status_code == 200 and f"/candidates/{cid}/edit" in form.text
    assert 'value="boots" selected' in form.text and f'href="/candidates/{cid}"' in form.text

    bad_form = {**VALID_FORM, "primary_color": "fuchsia"}
    bad = await post_form(client, f"/candidates/{cid}/edit", bad_form)
    assert bad.status_code == 422 and "Choose a color from the palette." in bad.text

    fixed = await post_form(
        client,
        f"/candidates/{cid}/edit",
        {**VALID_FORM, "subtype": "blouse", "primary_color": "cream"},
    )
    assert fixed.status_code == 303 and fixed.headers["location"] == f"/candidates/{cid}"
    second = (await client.get(f"/candidates/{cid}")).text
    assert second != first and "cream" in second and "blouse" in second
    row = session.exec(select(Candidate)).one()
    session.refresh(row)
    assert row.result is not None and row.result["kind"] == "outfit"


async def test_add_to_my_closet(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    stocked(shop)
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    assert "1 item" not in (await client.get("/closet?q=Slim")).text
    added = await post_form(client, f"/candidates/{cid}/add")
    assert added.status_code == 303 and added.headers["location"].startswith("/items/")
    assert "Slim Fit Oxford Shirt" in (await client.get("/closet?q=Slim")).text
    page = await client.get(f"/candidates/{cid}")
    assert "In your closet" in page.text and "Add to my closet" not in page.text
    stats = await client.get("/closet/stats", headers={"Accept": "application/json"})
    assert stats.json()["total"] > 0


async def test_discard_and_history(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    stocked(shop)
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    history = await client.get("/candidates")
    assert "Slim Fit Oxford Shirt" in history.text and "new outfits" in history.text
    before = len(stored_files(storage))
    discarded = await post_form(client, f"/candidates/{cid}/discard")
    assert discarded.status_code == 303 and discarded.headers["location"] == "/candidates"
    assert (await client.get(f"/candidates/{cid}")).status_code == 404
    assert "Nothing analyzed yet" in (await client.get("/candidates")).text
    assert len(stored_files(storage)) < before


async def test_photo_fallback_upload(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps)
    token = await _token(client)
    response = await client.post(
        "/candidates",
        data={"csrf_token": token},
        files={"image": ("tee.jpg", image_bytes(size=(300, 400)), "image/jpeg")},
    )
    assert response.status_code == 303
    cid = candidate_id(response)
    page = await client.get(f"/candidates/{cid}")
    assert "your photo" in page.text and "New outfits" in page.text
    assert shop.requests == []  # no scraping happened

    bad = await client.post(
        "/candidates",
        data={"csrf_token": token},
        files={"image": ("x.jpg", b"not an image", "image/jpeg")},
    )
    assert bad.status_code == 422 and "Unsupported file type" in bad.text


async def _token(client: httpx.AsyncClient) -> str:
    await client.get("/analyze")
    return client.cookies["csrf_token"]


# ----------------------------------------------------------------------------- SSRF


async def test_ssrf_attempts_are_rejected_and_nothing_is_stored(
    client: httpx.AsyncClient, session: Session, shop: Shop
) -> None:
    attempts = {
        "http://169.254.169.254/": "private address",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/": "private address",
        "http://127.0.0.1:8000/": "private address",
        "http://localhost/admin": "private address",
        "http://[::1]/": "private address",
        "http://10.0.0.1/": "private address",
        "file:///etc/passwd": "Only http and https",
        "gopher://shop.example/": "Only http and https",
        "http://user:pw@shop.example/": "username or password",
    }
    for url, message in attempts.items():
        response = await analyze(client, url)
        assert response.status_code == 422, url
        assert message in response.text, url
    assert session.exec(select(Candidate)).first() is None
    assert shop.requests == []  # the guard fired before any request


async def test_the_form_needs_a_link_or_a_photo(client: httpx.AsyncClient) -> None:
    response = await analyze(client, "")
    assert response.status_code == 422 and "Paste a product link" in response.text


# --------------------------------------------------------------------------- rate limit


async def test_the_eleventh_analysis_in_an_hour_is_rejected(
    client: httpx.AsyncClient,
    session: Session,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    for n in range(11):
        shop.add_product(f"/p/{n}")
    shop.images["/img/shirt.jpg"] = jpeg("#1f2a44")
    use_tagger(app, candidate_deps)
    for n in range(10):
        response = await analyze(client, f"https://shop.example/p/{n}")
        assert response.status_code == 303, n
    response = await analyze(client, "https://shop.example/p/10")
    assert response.status_code == 429
    assert "10 products per hour" in response.text and "minute" in response.text
    assert int(response.headers["retry-after"]) > 0
    assert len(list(session.exec(select(Candidate)))) == 10  # nothing was created
    cached = await analyze(client, "https://shop.example/p/3")  # re-pasting a finished link is free
    assert cached.status_code == 303


async def test_retrying_counts_against_the_limit(
    client: httpx.AsyncClient, session: Session, user_a: User
) -> None:
    for n in range(10):
        session.add(
            Candidate(
                user_id=user_a.id,  # type: ignore[arg-type]
                url=f"https://shop.example/p/{n}",
                url_hash=f"{n:064d}",
                status=CandidateStatus.FAILED,
            )
        )
    session.commit()
    first = session.exec(select(Candidate)).first()
    assert first is not None
    response = await post_form(client, f"/candidates/{first.id}/retry")
    assert response.status_code == 429


# -------------------------------------------------------------------------- isolation


async def test_a_candidate_never_appears_in_the_closet_stats_or_suggestions(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
) -> None:
    stocked(shop)
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    row = session.get(Candidate, cid)
    assert row is not None and row.item_id is not None
    item_id = row.item_id
    assert session.get(Item, item_id).source == Source.CANDIDATE  # type: ignore[union-attr]

    closet_page = await client.get("/closet")
    assert "9 items" in closet_page.text and f"/items/{item_id}" not in closet_page.text
    assert "Slim Fit" not in closet_page.text
    assert "0 items" in (await client.get("/closet?review=1")).text
    stats = (await client.get("/closet/stats", headers={"Accept": "application/json"})).json()
    assert item_id not in {i["id"] for i in stats["items"]} and len(stats["items"]) == 9
    assert stats["unconfirmed_items"] == 0

    for _ in range(4):
        await post_form(client, "/outfits/generate", {})
        for outfit in session.exec(select(Outfit)):
            assert item_id not in outfit.item_ids
    # the candidate item is not reachable through the closet's own pages
    for path in (f"/items/{item_id}", f"/items/{item_id}/review", f"/items/{item_id}/edit"):
        assert (await client.get(path)).status_code == 404, path
    for action in ("confirm", "delete", "retag"):
        path = f"/items/{item_id}/{action}"
        assert (await post_form(client, path, VALID_FORM)).status_code == 404, path
    confirmed = await post_form(client, "/items/confirm-all")
    assert confirmed.status_code == 303
    assert session.get(Item, item_id).tags_confirmed is False  # type: ignore[union-attr]


async def test_user_b_gets_404_on_user_a_candidates(
    client: httpx.AsyncClient,
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    user_b: User,
    app: FastAPI,
    candidate_deps: CandidateDeps,
    shop: Shop,
    as_user: Callable[[User], None],
) -> None:
    stocked(shop)
    closet(session, storage, user_a)
    use_tagger(app, candidate_deps)
    cid = candidate_id(await analyze(client))
    row = session.get(Candidate, cid)
    assert row is not None and row.item_id
    files = stored_files(storage)

    as_user(user_b)
    for path in (f"/candidates/{cid}", f"/candidates/{cid}/panel", f"/candidates/{cid}/edit"):
        assert (await client.get(path)).status_code == 404, path
    for path in (
        f"/candidates/{cid}/edit",
        f"/candidates/{cid}/add",
        f"/candidates/{cid}/discard",
        f"/candidates/{cid}/retry",
    ):
        assert (await post_form(client, path, VALID_FORM)).status_code == 404, path
    assert "Nothing analyzed yet" in (await client.get("/candidates")).text
    image = session.get(Item, row.item_id)
    assert image is not None
    assert (await client.get(f"/media/{image.image_path}")).status_code == 404
    session.expire_all()
    assert session.get(Candidate, cid) is not None and set(files) <= set(stored_files(storage))

    # B analyzing the same link gets a separate candidate (the cache is per user)
    other = await analyze(client)
    assert other.status_code == 303 and candidate_id(other) != cid


async def test_unknown_candidate_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/candidates/999999")).status_code == 404
    assert (await post_form(client, "/candidates/999999/add")).status_code == 404


async def test_candidate_posts_require_csrf(
    client: httpx.AsyncClient, session: Session, user_a: User
) -> None:
    row = Candidate(user_id=user_a.id, url="https://shop.example/p/x", url_hash="c" * 64)  # type: ignore[arg-type]
    session.add(row)
    session.commit()
    await client.get("/analyze")
    assert (await client.post("/candidates", data={"url": LINK})).status_code == 403
    for action in ("add", "discard", "retry", "edit"):
        assert (await client.post(f"/candidates/{row.id}/{action}")).status_code == 403, action
