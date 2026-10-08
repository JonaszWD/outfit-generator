from io import BytesIO

import pytest
from PIL import Image

from app.services.imaging import (
    InvalidImageError,
    dominant_colors,
    make_cutout,
    process_image,
)
from tests.conftest import image_bytes, stub_remover

MAX = 1024 * 1024


def test_heic_is_accepted_and_converted_to_jpeg() -> None:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    out = BytesIO()
    Image.new("RGB", (64, 48), "green").save(out, format="HEIF")
    result = process_image(out.getvalue(), MAX)
    assert Image.open(BytesIO(result.main)).format == "JPEG"
    assert (result.width, result.height) == (64, 48)


def test_oversized_dimensions_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.imaging.MAX_PIXELS", 100)
    with pytest.raises(InvalidImageError, match="dimensions"):
        process_image(image_bytes(size=(50, 50)), MAX)


def test_make_cutout_returns_transparent_png_and_thumb() -> None:
    cutout = make_cutout(image_bytes(size=(800, 1000)), stub_remover)
    png = Image.open(BytesIO(cutout.png))
    thumb = Image.open(BytesIO(cutout.thumb_png))
    assert png.format == "PNG" and png.mode == "RGBA"
    assert png.getpixel((0, 0))[3] == 0 and png.getpixel((400, 500))[3] == 255  # type: ignore[index]
    assert max(thumb.size) == 400


def _png(color: str, size: tuple[int, int] = (100, 100), alpha: int = 255) -> bytes:
    out = BytesIO()
    img = Image.new("RGBA", size, color)
    img.putalpha(alpha)
    img.save(out, format="PNG")
    return out.getvalue()


def test_dominant_color_of_solid_block() -> None:
    assert dominant_colors(_png("#c0392b"))[0][0] == "red"


def test_dominant_colors_ignore_transparent_pixels() -> None:
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 0))  # transparent "white"
    for x in range(30, 70):
        for y in range(30, 70):
            img.putpixel((x, y), (20, 30, 70, 255))  # navy item
    out = BytesIO()
    img.save(out, format="PNG")
    colors = dominant_colors(out.getvalue())
    assert colors[0][0] == "navy" and colors[0][1] > 0.95
    assert "white" not in [name for name, _ in colors]


def test_dominant_colors_empty_for_fully_transparent() -> None:
    assert dominant_colors(_png("red", alpha=0)) == []


def test_dominant_colors_shares_sum_to_at_most_one() -> None:
    img = Image.new("RGB", (100, 100), "white")
    for x in range(50):
        for y in range(100):
            img.putpixel((x, y), (0, 0, 0))
    out = BytesIO()
    img.save(out, format="PNG")
    colors = dominant_colors(out.getvalue(), top=2)
    assert {n for n, _ in colors} == {"black", "white"}
    assert sum(share for _, share in colors) == pytest.approx(1.0)


@pytest.mark.slow
def test_real_rembg_removes_background() -> None:
    """Needs the u2net model (downloaded on first use). Run with: pytest -m slow"""
    from app.services.imaging import remove_background

    img = Image.new("RGB", (320, 320), "white")
    for x in range(100, 220):
        for y in range(80, 260):
            img.putpixel((x, y), (180, 30, 30))
    result = remove_background(img)
    assert result.mode == "RGBA"
    assert result.getpixel((5, 5))[3] < 128  # type: ignore[index]
