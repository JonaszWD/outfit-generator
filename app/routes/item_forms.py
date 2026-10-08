"""Helpers shared by the routes that show the tag form (items and candidates)."""

from typing import Any

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile

from app.deps import StorageDep
from app.engine import vocab
from app.models import Category, Item
from app.schemas import ItemData
from app.services.imaging import InvalidImageError, ProcessedImage, process_image
from app.services.tagging import low_confidence_fields
from app.templating import render

SCALAR_FIELDS = (
    "category",
    "subtype",
    "primary_color",
    "pattern",
    "material",
    "layer_role",
    "name",
    "notes",
)
LIST_FIELDS = ("secondary_colors", "seasons")


def values_from_data(data: ItemData) -> dict[str, Any]:
    return {
        "category": data.category.value,
        "subtype": data.subtype,
        "primary_color": data.primary_color,
        "pattern": data.pattern or "",
        "material": data.material or "",
        "layer_role": data.layer_role or "",
        "name": data.name or "",
        "notes": data.notes or "",
        "secondary_colors": list(data.secondary_colors),
        "seasons": list(data.seasons),
    }


def values_from_item(item: Item) -> dict[str, Any]:
    return {
        "category": item.category.value if item.category else "",
        "subtype": item.subtype or "",
        "primary_color": item.primary_color or "",
        "pattern": item.pattern or "",
        "material": item.material or "",
        "layer_role": item.layer_role or "",
        "name": item.name or "",
        "notes": item.notes or "",
        "secondary_colors": list(item.secondary_colors),
        "seasons": list(item.seasons),
    }


def subtype_options_for(category: str | None) -> tuple[str, ...]:
    try:
        return vocab.subtypes_for(Category(category)) if category else ()
    except ValueError:
        return ()


def form_page(
    request: Request,
    storage: StorageDep,
    *,
    item: Item | None,
    values: dict[str, Any],
    errors: dict[str, str],
    action: str,
    threshold: float,
    review: bool = False,
    status_code: int = 200,
    cancel_url: str | None = None,
) -> HTMLResponse:
    low: list[str] = []
    confidence: dict[str, float] = {}
    flags: list[str] = []
    if review and item is not None and not item.tags_confirmed:
        confidence = item.ai_confidence or {}
        low = low_confidence_fields(confidence, threshold)
        flags = list((item.ai_tags or {}).get("flags", []))
    return render(
        request,
        "item_form.html",
        {
            "item": item,
            "values": values,
            "errors": errors,
            "storage": storage,
            "subtype_options": subtype_options_for(values.get("category")),
            "action": action,
            "review": review,
            "low_fields": low,
            "confidence": confidence,
            "flags": flags,
            "cancel_url": cancel_url,
        },
        status_code=status_code,
    )


async def read_form(request: Request, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    form = await request.form()
    values: dict[str, Any] = {k: str(form.get(k) or "").strip() for k in SCALAR_FIELDS}
    for key in LIST_FIELDS:
        values[key] = [str(v) for v in form.getlist(key)]
    upload = form.get("image")
    raw = b""
    if isinstance(upload, UploadFile) and upload.filename:
        raw = await upload.read(max_bytes + 1)
    return values, raw


async def process_upload(
    raw: bytes, max_bytes: int, errors: dict[str, str]
) -> ProcessedImage | None:
    try:
        return await run_in_threadpool(process_image, raw, max_bytes)
    except InvalidImageError as exc:
        errors["image"] = str(exc)
        return None
