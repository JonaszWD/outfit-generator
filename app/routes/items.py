from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile

from app.csrf import require_csrf
from app.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    EngineConfigDep,
    OwnedItem,
    PipelineDepsDep,
    StorageDep,
)
from app.models import Availability, Item, TaggingStatus
from app.routes.item_forms import (
    form_page,
    process_upload,
    read_form,
    subtype_options_for,
    values_from_data,
    values_from_item,
)
from app.schemas import parse_item_data
from app.services.analysis import AnalysisConfig
from app.services.items import (
    ai_item_data,
    archive_item,
    confirm_all_from_ai,
    create_pending_item,
    delete_item,
    duplicate_warning,
    items_awaiting_review,
    request_retag,
    restore_item,
    set_availability,
    update_item,
)
from app.services.jobs import run_or_schedule
from app.services.pipeline import process_item
from app.services.tagging import low_confidence_fields
from app.templating import render

router = APIRouter()
CSRF = [Depends(require_csrf)]

# ------------------------------------------------------------------- upload (batch)


@router.get("/items/new", response_class=HTMLResponse)
def upload_form(request: Request, _user: CurrentUser, settings: AppSettings) -> HTMLResponse:
    return render(request, "upload.html", {"max_files": settings.max_batch_files, "results": None})


@router.post("/items", dependencies=CSRF, response_model=None)
async def upload(
    request: Request,
    background: BackgroundTasks,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
    pipeline_deps: PipelineDepsDep,
) -> HTMLResponse | RedirectResponse:
    """One pending item per valid file; tagging runs in the background."""
    form = await request.form()
    uploads = [u for u in form.getlist("images") if isinstance(u, UploadFile) and u.filename]
    problems: list[tuple[str, str]] = []
    if len(uploads) > settings.max_batch_files:
        limit = settings.max_batch_files
        problems.append(("", f"Too many files: at most {limit} per upload. The rest were skipped."))
        uploads = uploads[: settings.max_batch_files]
    created = 0
    for upload_file in uploads:
        raw = await upload_file.read(settings.max_upload_bytes + 1)
        errors: dict[str, str] = {}
        image = await process_upload(raw, settings.max_upload_bytes, errors)
        if image is None:
            problems.append((upload_file.filename or "file", errors["image"]))
            continue
        item = await run_in_threadpool(create_pending_item, session, storage, user, image)
        assert item.id is not None
        if not settings.process_in_request:  # there, each card asks for its own processing
            background.add_task(process_item, item.id, pipeline_deps)
        created += 1
    if not uploads and not problems:
        problems.append(("", "Choose at least one photo."))
    if not problems:
        return RedirectResponse("/closet", status_code=303)
    results = {"created": created, "problems": problems}
    return render(
        request,
        "upload.html",
        {"max_files": settings.max_batch_files, "results": results},
        status_code=200 if created else 422,
    )


# ------------------------------------------------------------------------- helpers


@router.get("/items/subtype-options", response_class=HTMLResponse)
def subtype_options(
    request: Request,
    _user: CurrentUser,
    category: Annotated[str, Query()] = "",
    subtype: Annotated[str, Query()] = "",
) -> HTMLResponse:
    return render(
        request,
        "_subtype_select.html",
        {
            "subtype_options": subtype_options_for(category),
            "values": {"subtype": subtype},
            "errors": {},
        },
    )


@router.post("/items/confirm-all", dependencies=CSRF, response_model=None)
def confirm_all(session: DbSession, user: CurrentUser, settings: AppSettings) -> RedirectResponse:
    """Confirm every untouched AI result. Refused while any item has low-confidence fields."""
    waiting = items_awaiting_review(session, user)
    if any(
        low_confidence_fields(i.ai_confidence, settings.tag_confidence_threshold) for i in waiting
    ):
        raise HTTPException(
            status_code=409, detail="Some items have low-confidence fields. Review them first."
        )
    confirm_all_from_ai(session, waiting)
    return RedirectResponse("/closet", status_code=303)


# ------------------------------------------------------------------ single item


@router.get("/items/{item_id:int}", response_class=HTMLResponse)
def detail(
    request: Request,
    item: OwnedItem,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    config: EngineConfigDep,
) -> HTMLResponse:
    duplicate = duplicate_warning(session, user, item, config, AnalysisConfig())
    twin = session.get(Item, duplicate.item_id) if duplicate and duplicate.item_id else None
    context = {
        "item": item,
        "storage": storage,
        "duplicate": duplicate,
        "twin": twin,
        "availabilities": list(Availability),
    }
    return render(request, "item_detail.html", context)


@router.get("/items/{item_id:int}/status", response_class=HTMLResponse)
def status(request: Request, item: OwnedItem, storage: StorageDep) -> HTMLResponse:
    """The item's grid card. While pending/processing it polls this URL; once done it doesn't."""
    return render(request, "_item_card.html", {"item": item, "storage": storage})


@router.get("/items/{item_id:int}/edit", response_class=HTMLResponse)
def edit_form(
    request: Request, item: OwnedItem, storage: StorageDep, settings: AppSettings
) -> HTMLResponse:
    return form_page(
        request,
        storage,
        item=item,
        values=values_from_item(item),
        errors={},
        action=f"/items/{item.id}",
        threshold=settings.tag_confidence_threshold,
    )


@router.post("/items/{item_id:int}", dependencies=CSRF, response_model=None)
async def update(
    request: Request,
    item: OwnedItem,
    background: BackgroundTasks,
    session: DbSession,
    storage: StorageDep,
    settings: AppSettings,
    pipeline_deps: PipelineDepsDep,
) -> HTMLResponse | RedirectResponse:
    values, raw = await read_form(request, settings.max_upload_bytes)
    errors: dict[str, str] = {}
    image = await process_upload(raw, settings.max_upload_bytes, errors) if raw else None
    data, field_errors = parse_item_data(values)
    errors.update(field_errors)
    if errors or data is None:
        return form_page(
            request,
            storage,
            item=item,
            values=values,
            errors=errors,
            action=f"/items/{item.id}",
            threshold=settings.tag_confidence_threshold,
            status_code=422,
        )
    await run_in_threadpool(update_item, session, storage, item, data, image)
    if image is not None:
        assert item.id is not None
        await run_or_schedule(background, settings, process_item, item.id, pipeline_deps)
    return RedirectResponse(f"/items/{item.id}", status_code=303)


@router.get("/items/{item_id:int}/review", response_model=None)
def review(
    request: Request, item: OwnedItem, storage: StorageDep, settings: AppSettings
) -> HTMLResponse | RedirectResponse:
    """Cutout next to the form, pre-filled with the AI tags (or the confirmed tags)."""
    if item.tagging_status in (TaggingStatus.PENDING, TaggingStatus.PROCESSING):
        return RedirectResponse("/closet", status_code=303)
    ai_data = None if item.tags_confirmed else ai_item_data(item)
    values = values_from_data(ai_data) if ai_data else values_from_item(item)
    return form_page(
        request,
        storage,
        item=item,
        values=values,
        errors={},
        action=f"/items/{item.id}/confirm",
        threshold=settings.tag_confidence_threshold,
        review=True,
    )


@router.post("/items/{item_id:int}/confirm", dependencies=CSRF, response_model=None)
async def confirm(
    request: Request,
    item: OwnedItem,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
) -> HTMLResponse | RedirectResponse:
    values, _ = await read_form(request, settings.max_upload_bytes)
    data, errors = parse_item_data(values)
    if errors or data is None:
        return form_page(
            request,
            storage,
            item=item,
            values=values,
            errors=errors,
            action=f"/items/{item.id}/confirm",
            threshold=settings.tag_confidence_threshold,
            review=True,
            status_code=422,
        )
    await run_in_threadpool(update_item, session, storage, item, data, None)
    remaining = await run_in_threadpool(items_awaiting_review, session, user)
    target = "/closet?review=1" if remaining else f"/items/{item.id}"
    return RedirectResponse(target, status_code=303)


@router.post("/items/{item_id:int}/retag", dependencies=CSRF, response_model=None)
async def retag(
    request: Request,
    item: OwnedItem,
    background: BackgroundTasks,
    session: DbSession,
    storage: StorageDep,
    settings: AppSettings,
    pipeline_deps: PipelineDepsDep,
) -> HTMLResponse | RedirectResponse:
    assert item.id is not None
    htmx = request.headers.get("HX-Request") == "true"
    if request_retag(session, item) and not (htmx and settings.process_in_request):
        # (an HTMX card in process-in-request mode asks for its own processing when it loads)
        await run_or_schedule(background, settings, process_item, item.id, pipeline_deps)
    if htmx:
        return render(request, "_item_card.html", {"item": item, "storage": storage})
    return RedirectResponse("/closet", status_code=303)


@router.post("/items/{item_id:int}/process", dependencies=CSRF, response_class=HTMLResponse)
async def process(
    request: Request,
    item: OwnedItem,
    session: DbSession,
    storage: StorageDep,
    pipeline_deps: PipelineDepsDep,
) -> HTMLResponse:
    """Tag this item now and return its card. Serverless hosting: a pending card calls this when
    it loads, so the work happens inside a request instead of a background task. Safe to call
    twice: only a pending or failed item is claimed."""
    assert item.id is not None
    await run_in_threadpool(process_item, item.id, pipeline_deps)
    session.refresh(item)
    return render(request, "_item_card.html", {"item": item, "storage": storage})


def _back(item: OwnedItem, target: str) -> RedirectResponse:
    allowed = {"closet": "/closet", "item": f"/items/{item.id}"}
    return RedirectResponse(allowed.get(target, allowed["item"]), status_code=303)


@router.post("/items/{item_id:int}/availability", dependencies=CSRF, response_model=None)
def availability(
    item: OwnedItem,
    session: DbSession,
    value: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "item",
) -> RedirectResponse:
    """Available, in the laundry (skipped for suggestions) or unavailable (out of everything)."""
    try:
        new = Availability(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown availability.") from None
    set_availability(session, item, new)
    return _back(item, back)


@router.post("/items/{item_id:int}/archive", dependencies=CSRF, response_model=None)
def archive(
    item: OwnedItem, session: DbSession, back: Annotated[str, Form()] = "closet"
) -> RedirectResponse:
    archive_item(session, item)
    return _back(item, back)


@router.post("/items/{item_id:int}/restore", dependencies=CSRF, response_model=None)
def restore(
    item: OwnedItem, session: DbSession, back: Annotated[str, Form()] = "item"
) -> RedirectResponse:
    restore_item(session, item)
    return _back(item, back)


@router.post("/items/{item_id:int}/delete", dependencies=CSRF)
def delete(item: OwnedItem, session: DbSession, storage: StorageDep) -> RedirectResponse:
    delete_item(session, storage, item)
    return RedirectResponse("/closet", status_code=303)
