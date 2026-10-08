"""Upload validation, normalization, background removal and color analysis."""

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from app.engine import vocab

register_heif_opener()

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF"}
MAX_SIDE = 1600
THUMB_SIDE = 400
MAX_PIXELS = 50_000_000
JPEG_QUALITY = 85
OPAQUE_ALPHA = 128

Remover = Callable[[Image.Image], Image.Image]


class InvalidImageError(ValueError):
    """Upload rejected. The message is safe to show to the user."""


@dataclass(frozen=True)
class ProcessedImage:
    main: bytes
    thumb: bytes
    width: int
    height: int


@dataclass(frozen=True)
class Cutout:
    png: bytes
    thumb_png: bytes


def _encode(image: Image.Image) -> bytes:
    out = BytesIO()
    # No exif= argument and a fresh pixel copy, so no metadata (EXIF, GPS) is written.
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def process_image(data: bytes, max_bytes: int) -> ProcessedImage:
    """Validate by content, fix orientation, cap size, strip metadata, make a thumbnail.

    This is the only form of the upload that is ever stored or sent to a third party, so
    GPS and other EXIF data never leave the request.
    """
    if not data:
        raise InvalidImageError("Add a photo of the item.")
    if len(data) > max_bytes:
        raise InvalidImageError(f"Photo is too large (max {max_bytes // (1024 * 1024)} MB).")
    try:
        with Image.open(BytesIO(data)) as probe:
            if probe.format not in ALLOWED_FORMATS:
                raise InvalidImageError(
                    "Unsupported file type. Use a JPEG, PNG, WebP or HEIC photo."
                )
            if probe.width * probe.height > MAX_PIXELS:
                raise InvalidImageError("Image dimensions are too large.")
            probe.load()  # decode fully so truncated or corrupt files fail here
            image = ImageOps.exif_transpose(probe)
            image.load()
    except InvalidImageError:
        raise
    except UnidentifiedImageError:
        raise InvalidImageError(
            "Unsupported file type. Use a JPEG, PNG, WebP or HEIC photo."
        ) from None
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise InvalidImageError("Could not read this image. The file may be corrupt.") from None

    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        rgb = flat
    else:
        rgb = image.convert("RGB")

    rgb.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    thumb = rgb.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    return ProcessedImage(_encode(rgb), _encode(thumb), rgb.width, rgb.height)


_session_lock = threading.Lock()


@lru_cache(maxsize=4)
def _rembg_session(model: str) -> Any:
    """Load the rembg model once per process (and model name), not per request."""
    from rembg import new_session  # heavy import, deferred until first use

    return new_session(model)


def remove_background(image: Image.Image, model: str = "u2net") -> Image.Image:
    from rembg import remove

    with _session_lock:  # onnxruntime sessions are not guaranteed thread-safe to init
        session = _rembg_session(model)
    result = remove(image.convert("RGB"), session=session)
    assert isinstance(result, Image.Image)
    return result.convert("RGBA")


def make_cutout(main_jpeg: bytes, remover: Remover) -> Cutout:
    """Transparent PNG cutout (and a 400px thumbnail) from the normalized image."""
    with Image.open(BytesIO(main_jpeg)) as src:
        rgba = remover(src.convert("RGB")).convert("RGBA")
    out = BytesIO()
    rgba.save(out, format="PNG", optimize=True)
    thumb = rgba.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    thumb_out = BytesIO()
    thumb.save(thumb_out, format="PNG", optimize=True)
    return Cutout(out.getvalue(), thumb_out.getvalue())


BACKGROUND_DISTANCE = 40.0  # RGB distance from the border color that still counts as backdrop


def dominant_colors(
    image_bytes: bytes, top: int = 3, drop_background: bool = False
) -> list[tuple[str, float]]:
    """Palette colors by share of (non-transparent) pixels, largest first.

    With `drop_background` (no cutout available), pixels close to the photo's border color are
    ignored, so a white studio backdrop does not count as the garment's color.

    Pixels are quantized to 4 bits per channel, counted, and each bucket is mapped to the
    nearest palette color.
    """
    with Image.open(BytesIO(image_bytes)) as src:
        rgba = src.convert("RGBA")
    rgba.thumbnail((128, 128))
    pixels = np.asarray(rgba).reshape(-1, 4)
    opaque = pixels[pixels[:, 3] >= OPAQUE_ALPHA][:, :3]
    if drop_background and len(opaque):
        grid = np.asarray(rgba)[:, :, :3]
        border = np.concatenate([grid[0], grid[-1], grid[:, 0], grid[:, -1]]).astype(np.float64)
        backdrop = np.median(border, axis=0)
        distance = np.linalg.norm(opaque.astype(np.float64) - backdrop, axis=1)
        garment = opaque[distance > BACKGROUND_DISTANCE]
        if len(garment) >= max(20, len(opaque) // 50):  # keep the old behavior for odd photos
            opaque = garment
    if len(opaque) == 0:
        return []
    buckets = (opaque >> 4).astype(np.int32)
    keys = buckets[:, 0] * 256 + buckets[:, 1] * 16 + buckets[:, 2]
    counts = Counter(keys.tolist())
    shares: Counter[str] = Counter()
    for key, count in counts.items():
        r, g, b = (key >> 8) & 15, (key >> 4) & 15, key & 15
        centre = (r * 16 + 8, g * 16 + 8, b * 16 + 8)
        shares[vocab.nearest_color(centre)] += count
    total = sum(shares.values())
    return [(name, count / total) for name, count in shares.most_common(top)]
