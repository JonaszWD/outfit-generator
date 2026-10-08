from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app.config import get_settings
from app.engine import vocab
from app.engine.personalize import REASONS
from app.models import Item
from app.services.storage import cutout_thumb_key, thumb_key


def display_key(item: Item) -> str:
    """Full-size image to show: the cutout when there is one, else the original."""
    return item.cutout_image_path or item.image_path


def card_key(item: Item) -> str:
    """Grid thumbnail key."""
    if item.cutout_image_path:
        return cutout_thumb_key(item.image_path)
    return thumb_key(item.image_path)


templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.globals.update(
    vocab=vocab,
    reasons_for_dislike=REASONS,
    thumb_key=thumb_key,
    display_key=display_key,
    card_key=card_key,
    color_hex=lambda name: vocab.COLOR_HEX.get(name, "transparent"),
    process_in_request=lambda: get_settings().process_in_request,
)


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    ctx = {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "logged_in": getattr(request.state, "user_id", None) is not None,
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
