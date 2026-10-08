import re

from fastapi import APIRouter, HTTPException, Response

from app.deps import CurrentUser, StorageDep

router = APIRouter()

_FILENAME_RE = re.compile(r"^[0-9a-f]{32}(_thumb|_cutout|_cutout_thumb)?\.(jpg|png)$")


@router.get("/media/{user_id:int}/{filename}")
def get_media(user_id: int, filename: str, user: CurrentUser, storage: StorageDep) -> Response:
    """Serve images only to their owner. Everyone else gets 404."""
    if user_id != user.id or not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        data = storage.read(f"{user_id}/{filename}")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None
    return Response(
        data,
        media_type="image/png" if filename.endswith(".png") else "image/jpeg",
        # Filenames are random and never reused, so the content is immutable.
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )
