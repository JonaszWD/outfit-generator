from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.deps import OptionalUser
from app.templating import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse, response_model=None)
def index(request: Request, user: OptionalUser) -> Response:
    if user is not None:
        return RedirectResponse("/closet", status_code=303)
    return render(request, "index.html")
