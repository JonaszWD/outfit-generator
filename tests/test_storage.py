from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session

from app.models import Item, User
from app.schemas import ItemData
from app.services.imaging import InvalidImageError, process_image
from app.services.items import create_item, delete_item
from app.services.storage import LocalDiskStorage, new_image_keys, thumb_key
from tests.conftest import image_bytes, stored_files

MAX = 1024 * 1024
DATA = ItemData(category="top", subtype="shirt", primary_color="navy")


def test_rejects_text_file_renamed_jpg() -> None:
    with pytest.raises(InvalidImageError, match="Unsupported"):
        process_image(b"just some text pretending to be photo.jpg", MAX)


def test_rejects_other_image_formats() -> None:
    with pytest.raises(InvalidImageError, match="Unsupported"):
        process_image(image_bytes("GIF"), MAX)


def test_rejects_empty() -> None:
    with pytest.raises(InvalidImageError):
        process_image(b"", MAX)


def test_rejects_oversized() -> None:
    with pytest.raises(InvalidImageError, match="too large"):
        process_image(image_bytes(size=(600, 600)), max_bytes=100)


def test_rejects_corrupt_image() -> None:
    good = image_bytes(size=(300, 300))
    with pytest.raises(InvalidImageError, match="corrupt"):
        process_image(good[: len(good) // 2], MAX)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_accepts_allowed_formats(fmt: str) -> None:
    result = process_image(image_bytes(fmt), MAX)
    assert Image.open(BytesIO(result.main)).format == "JPEG"


def test_longest_side_capped_and_thumbnail_made() -> None:
    result = process_image(image_bytes(size=(3200, 1600)), 10 * MAX)
    main = Image.open(BytesIO(result.main))
    thumb = Image.open(BytesIO(result.thumb))
    assert max(main.size) == 1600
    assert max(thumb.size) == 400
    assert main.size == (1600, 800)


def test_small_images_are_not_upscaled() -> None:
    result = process_image(image_bytes(size=(100, 50)), MAX)
    assert Image.open(BytesIO(result.main)).size == (100, 50)


def test_exif_gps_stripped_and_orientation_applied() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    exif[0x010F] = "SecretCameraMaker"
    exif[0x8825] = {1: "N", 2: (52.0, 31.0, 0.0), 3: "E", 4: (13.0, 24.0, 0.0)}
    original = image_bytes(size=(200, 100), exif=exif)
    assert Image.open(BytesIO(original)).getexif().get_ifd(0x8825), "fixture must contain GPS"

    result = process_image(original, MAX)
    for blob in (result.main, result.thumb):
        out = Image.open(BytesIO(blob))
        assert len(out.getexif()) == 0
        assert not out.getexif().get_ifd(0x8825)
        assert b"SecretCameraMaker" not in blob
    assert Image.open(BytesIO(result.main)).size == (100, 200)  # orientation baked in


def test_transparent_png_is_flattened_on_white() -> None:
    out = BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(out, format="PNG")
    result = process_image(out.getvalue(), MAX)
    pixel = Image.open(BytesIO(result.main)).getpixel((10, 10))
    assert isinstance(pixel, tuple)
    assert min(pixel) > 245


def test_keys_are_random_and_ignore_client_names() -> None:
    first, _ = new_image_keys(7)
    second, _ = new_image_keys(7)
    assert first != second
    assert first.startswith("7/") and first.endswith(".jpg")
    assert thumb_key(first).endswith("_thumb.jpg")


@pytest.mark.parametrize("key", ["../evil.jpg", "1/../../evil.jpg", "/etc/passwd", ""])
def test_local_storage_blocks_path_traversal(storage: LocalDiskStorage, key: str) -> None:
    with pytest.raises(ValueError):
        storage.save(key, b"x")
    assert not (storage.root.parent / "evil.jpg").exists()


def test_local_storage_roundtrip(storage: LocalDiskStorage) -> None:
    storage.save("1/a.jpg", b"abc")
    assert storage.read("1/a.jpg") == b"abc"
    storage.delete("1/a.jpg")
    storage.delete("1/a.jpg")  # idempotent
    with pytest.raises(FileNotFoundError):
        storage.read("1/a.jpg")
    assert storage.url_for("1/a.jpg") == "/media/1/a.jpg"


def test_files_stored_under_user_dir_and_deleted_with_item(
    session: Session, storage: LocalDiskStorage, user_a: User
) -> None:
    item = create_item(session, storage, user_a, DATA, process_image(image_bytes(), MAX))
    files = stored_files(storage)
    assert len(files) == 2
    assert all(p.parent.name == str(user_a.id) for p in files)
    assert item.image_path.startswith(f"{user_a.id}/")

    delete_item(session, storage, item)
    assert stored_files(storage) == []
    assert session.get(Item, item.id) is None


def test_no_orphan_file_when_commit_fails(
    session: Session,
    storage: LocalDiskStorage,
    user_a: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        create_item(session, storage, user_a, DATA, process_image(image_bytes(), MAX))
    assert stored_files(storage) == []


def test_storage_save_failure_leaves_nothing(
    session: Session, user_a: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(tmp_path / "m")
    real_save = storage.save
    calls: list[str] = []

    def flaky(key: str, data: bytes) -> None:
        calls.append(key)
        if len(calls) == 2:
            raise OSError("disk full")
        real_save(key, data)

    monkeypatch.setattr(storage, "save", flaky)
    with pytest.raises(OSError):
        create_item(session, storage, user_a, DATA, process_image(image_bytes(), MAX))
    assert stored_files(storage) == []
