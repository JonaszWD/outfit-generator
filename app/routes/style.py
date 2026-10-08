"""Style reference photos: upload inspiration outfits, see what was read from them."""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from app.csrf import require_csrf
from app.deps import AppSettings, CurrentUser, DbSession, StorageDep, StyleExtractorDep
from app.routes.item_forms import process_upload
from app.services import style as svc
from app.services.storage import thumb_key
from app.templating import render

router = APIRouter()
CSRF = [Depends(require_csrf)]
PER_UPLOAD = 4  # a local model needs a while per photo
MESSAGES = {"removed": "Reference photo removed."}


def render_page(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
    status_code: int = 200,
    problems: list[tuple[str, str]] | None = None,
    added: int = 0,
    notice: str | None = None,
) -> HTMLResponse:
    references = svc.list_references(session, user)
    cards = [
        {
            "id": row.id,
            "thumb": storage.url_for(thumb_key(row.image_path)),
            "style": row.style,
        }
        for row in references
    ]
    context = {
        "references": cards,
        "profile": svc.profile_from(references),
        "limit": svc.MAX_REFERENCES,
        "per_upload": PER_UPLOAD,
        "backend": settings.style_backend,
        "problems": problems or [],
        "added": added,
        "notice": notice,
    }
    return render(request, "style.html", context, status_code=status_code)


@router.get("/settings/style", response_class=HTMLResponse)
def style_page(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
    ok: str = "",
) -> HTMLResponse:
    return render_page(request, session, user, storage, settings, notice=MESSAGES.get(ok))


@router.post("/settings/style/references", dependencies=CSRF, response_model=None)
async def add_references(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
    extractor: StyleExtractorDep,
) -> Response:
    """Read the style of each photo, then keep the photo and what was read from it."""
    form = await request.form()
    uploads = [u for u in form.getlist("images") if isinstance(u, UploadFile) and u.filename]
    problems: list[tuple[str, str]] = []
    room = svc.MAX_REFERENCES - len(await run_in_threadpool(svc.list_references, session, user))
    if len(uploads) > min(PER_UPLOAD, max(room, 0)):
        keep = min(PER_UPLOAD, max(room, 0))
        problems.append(
            ("", f"At most {keep} more photo{'' if keep == 1 else 's'} now. The rest were skipped.")
        )
        uploads = uploads[:keep]
    added = 0
    for upload_file in uploads:
        name = upload_file.filename or "file"
        raw = await upload_file.read(settings.max_upload_bytes + 1)
        errors: dict[str, str] = {}
        image = await process_upload(raw, settings.max_upload_bytes, errors)
        if image is None:
            problems.append((name, errors["image"]))
            continue
        try:
            result = await extractor.extract(image.main)
        except svc.StyleError as exc:
            problems.append((name, str(exc)))
            continue
        await run_in_threadpool(svc.save_reference, session, storage, user, image, result)
        added += 1
    if not uploads and not problems:
        problems.append(("", "Choose at least one photo."))
    if not problems:
        return RedirectResponse("/settings/style", status_code=303)
    return render_page(
        request,
        session,
        user,
        storage,
        settings,
        200 if added else 422,
        problems,
        added,
    )


@router.post("/settings/style/references/{reference_id:int}/delete", dependencies=CSRF)
def remove_reference(
    reference_id: int, session: DbSession, user: CurrentUser, storage: StorageDep
) -> Response:
    svc.delete_reference(session, storage, user, reference_id)
    return RedirectResponse("/settings/style?ok=removed", status_code=303)
