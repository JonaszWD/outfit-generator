from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from app.csrf import require_csrf
from app.deps import (
    AppSettings,
    CandidateDepsDep,
    CurrentUser,
    DbSession,
    OwnedCandidate,
    StorageDep,
)
from app.models import CandidateStatus, Item
from app.routes.item_forms import (
    form_page,
    process_upload,
    read_form,
    values_from_data,
    values_from_item,
)
from app.schemas import parse_item_data
from app.services import candidates as svc
from app.services.fetcher import FetchError
from app.services.items import ai_item_data, update_item
from app.services.jobs import run_or_schedule
from app.templating import render

router = APIRouter()
CSRF = [Depends(require_csrf)]


def _page(request: Request, **context: object) -> HTMLResponse:
    status_code = int(str(context.pop("status_code", 200)))
    return render(request, "analyze.html", context, status_code=status_code)


@router.get("/analyze", response_class=HTMLResponse)
def analyze_form(request: Request, _user: CurrentUser, settings: AppSettings) -> HTMLResponse:
    return _page(request, error=None, url="", limit=settings.analyses_per_hour)


@router.post("/candidates", dependencies=CSRF, response_model=None)
async def create(
    request: Request,
    background: BackgroundTasks,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
    deps: CandidateDepsDep,
) -> HTMLResponse | RedirectResponse:
    """A pasted link, or (the fallback) an uploaded photo. Either starts a background job."""
    form = await request.form()
    url = str(form.get("url") or "").strip()
    upload = form.get("image")
    photo = isinstance(upload, UploadFile) and bool(upload.filename)
    limit = settings.analyses_per_hour

    def fail(
        message: str, status_code: int = 422, headers: dict[str, str] | None = None
    ) -> HTMLResponse:
        response = _page(request, error=message, url=url, limit=limit, status_code=status_code)
        response.headers.update(headers or {})
        return response

    try:
        if photo:
            assert isinstance(upload, UploadFile)
            raw = await upload.read(settings.max_upload_bytes + 1)
            problems: dict[str, str] = {}
            image = await process_upload(raw, settings.max_upload_bytes, problems)
            if image is None:
                return fail(problems["image"])
            candidate = await run_in_threadpool(
                svc.start_from_photo, session, storage, user, image, settings
            )
            needs_job = True
        elif url:
            candidate, needs_job = await run_in_threadpool(
                svc.start_from_url, session, user, url, settings, storage
            )
        else:
            return fail("Paste a product link, or choose a photo.")
    except svc.RateLimited as exc:
        return fail(str(exc), 429, {"Retry-After": str(exc.retry_after)})
    except FetchError as exc:
        return fail(str(exc))
    assert candidate.id is not None
    if needs_job:
        await run_or_schedule(background, settings, svc.analyze_candidate, candidate.id, deps)
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.get("/candidates", response_class=HTMLResponse)
def history(
    request: Request, session: DbSession, user: CurrentUser, storage: StorageDep
) -> HTMLResponse:
    rows = []
    for candidate in svc.list_candidates(session, user):
        rows.append(svc.panel_data(session, user, candidate))
    return render(request, "candidates.html", {"rows": rows, "storage": storage})


def _panel_context(
    session: DbSession, user: CurrentUser, candidate: OwnedCandidate, storage: StorageDep
) -> dict[str, object]:
    data = svc.panel_data(session, user, candidate)
    return {"data": data, "storage": storage}


@router.get("/candidates/{candidate_id:int}", response_model=None)
def detail(
    request: Request,
    candidate: OwnedCandidate,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
) -> Response:
    data = svc.panel_data(session, user, candidate)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(_as_json(data))
    return render(request, "candidate.html", {"data": data, "storage": storage})


def _as_json(data: svc.PanelData) -> dict[str, object]:
    candidate, item = data.candidate, data.item
    tags: dict[str, object] = {}
    if item is not None:
        view = svc.candidate_view(item)
        if view is not None:
            tags = {
                "category": view.category.value,
                "subtype": view.subtype,
                "primary_color": view.primary_color,
                "confirmed": item.tags_confirmed,
            }
    return {
        "id": candidate.id,
        "status": candidate.status.value,
        "error": candidate.error,
        "product": candidate.product_info,
        "tags": tags,
        "result": candidate.result,
        "in_closet": data.adopted,
    }


@router.get("/candidates/{candidate_id:int}/panel", response_class=HTMLResponse)
def panel(
    request: Request,
    candidate: OwnedCandidate,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
) -> HTMLResponse:
    """The result panel. While the job runs it polls this URL; once finished it stops."""
    context = _panel_context(session, user, candidate, storage)
    return render(request, "_candidate_panel.html", context)


@router.post("/candidates/{candidate_id:int}/retry", dependencies=CSRF, response_model=None)
async def retry(
    candidate: OwnedCandidate,
    background: BackgroundTasks,
    session: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    deps: CandidateDepsDep,
) -> RedirectResponse:
    if candidate.status != CandidateStatus.FAILED:
        return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)
    try:
        svc.check_rate_limit(session, user, settings)
    except svc.RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    candidate.created_at = datetime.now(UTC)  # a retry is a new request for rate limiting
    session.add(candidate)
    session.commit()
    assert candidate.id is not None
    await run_or_schedule(background, settings, svc.analyze_candidate, candidate.id, deps)
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.get("/candidates/{candidate_id:int}/edit", response_class=HTMLResponse)
def edit_form(
    request: Request,
    candidate: OwnedCandidate,
    session: DbSession,
    storage: StorageDep,
    settings: AppSettings,
) -> HTMLResponse:
    item = svc.candidate_item(session, candidate)
    if item is None or candidate.status != CandidateStatus.DONE:
        raise HTTPException(status_code=409, detail="The analysis is not finished yet.")
    values = values_from_item(item) if item.tags_confirmed else _ai_values(item)
    return form_page(
        request,
        storage,
        item=item,
        values=values,
        errors={},
        action=f"/candidates/{candidate.id}/edit",
        threshold=settings.tag_confidence_threshold,
        review=True,
        cancel_url=f"/candidates/{candidate.id}",
    )


def _ai_values(item: Item) -> dict[str, object]:
    data = ai_item_data(item)
    return values_from_data(data) if data else values_from_item(item)


@router.post("/candidates/{candidate_id:int}/edit", dependencies=CSRF, response_model=None)
async def edit(
    request: Request,
    candidate: OwnedCandidate,
    session: DbSession,
    storage: StorageDep,
    settings: AppSettings,
    deps: CandidateDepsDep,
) -> HTMLResponse | RedirectResponse:
    """Save corrected tags and recompute the analysis with them."""
    item = svc.candidate_item(session, candidate)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    values, _ = await read_form(request, settings.max_upload_bytes)
    data, errors = parse_item_data(values)
    if errors or data is None:
        return form_page(
            request,
            storage,
            item=item,
            values=values,
            errors=errors,
            action=f"/candidates/{candidate.id}/edit",
            threshold=settings.tag_confidence_threshold,
            review=True,
            cancel_url=f"/candidates/{candidate.id}",
            status_code=422,
        )
    resolved = deps or svc.default_candidate_deps()
    await run_in_threadpool(update_item, session, storage, item, data, None)
    await run_in_threadpool(svc.reanalyze, session, candidate, resolved)
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.post("/candidates/{candidate_id:int}/add", dependencies=CSRF, response_model=None)
def add(candidate: OwnedCandidate, session: DbSession) -> RedirectResponse:
    try:
        item = svc.adopt_candidate(session, candidate)
    except svc.CandidateNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse(f"/items/{item.id}", status_code=303)


@router.post("/candidates/{candidate_id:int}/discard", dependencies=CSRF, response_model=None)
def discard(candidate: OwnedCandidate, session: DbSession, storage: StorageDep) -> RedirectResponse:
    svc.discard_candidate(session, storage, candidate)
    return RedirectResponse("/candidates", status_code=303)
