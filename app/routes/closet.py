from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.deps import AppSettings, CurrentUser, DbSession, EngineConfigDep, StorageDep
from app.schemas import ItemFilters
from app.services.items import items_awaiting_review, list_items
from app.services.outfits import closet_stats_view
from app.services.tagging import low_confidence_fields
from app.templating import render

router = APIRouter()


def _page_number(raw: str | None) -> int:
    try:
        return max(int(raw or 1), 1)
    except ValueError:
        return 1


@router.get("/closet", response_class=HTMLResponse)
def closet(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    settings: AppSettings,
) -> HTMLResponse:
    filters = ItemFilters.from_params(request.query_params)
    page = list_items(session, user, filters, _page_number(request.query_params.get("page")))
    waiting = items_awaiting_review(session, user)
    context = {
        "review_count": len(waiting),
        "low_review_count": sum(
            bool(low_confidence_fields(i.ai_confidence, settings.tag_confidence_threshold))
            for i in waiting
        ),
        "filters": filters,
        "page": page,
        "storage": storage,
        "next_query": filters.query_string(page=page.page + 1),
    }
    is_htmx = request.headers.get("HX-Request") == "true" and (
        request.headers.get("HX-History-Restore-Request") != "true"
    )
    if is_htmx and page.page > 1:
        template = "_cards.html"  # "load more": just the next batch
    elif is_htmx:
        template = "_closet_results.html"  # filter change: count + grid
    else:
        template = "closet.html"
    response = render(request, template, context)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/closet/stats", response_model=None)
def stats(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    storage: StorageDep,
    config: EngineConfigDep,
) -> Response:
    """Total valid outfits and how many each piece appears in (computed by the engine)."""
    view = closet_stats_view(session, user, config)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "total": view.total,
                "unconfirmed_items": view.unconfirmed,
                "items": [
                    {"id": item.id, "name": item.name or item.subtype, "outfits": count}
                    for item, count in view.ranked
                ],
            }
        )
    return render(
        request,
        "stats.html",
        {
            "view": view,
            "storage": storage,
            "most": view.ranked[:5],
            "least": list(reversed(view.ranked[-5:])) if len(view.ranked) > 5 else [],
        },
    )
