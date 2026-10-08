import os
import shutil
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import Settings


class Storage(Protocol):
    """Where image bytes live. Routes and services only use this interface."""

    def save(self, key: str, data: bytes) -> None: ...

    def read(self, key: str) -> bytes:
        """Raise FileNotFoundError if the key does not exist."""
        ...

    def delete(self, key: str) -> None:
        """Delete if present; a missing key is not an error."""
        ...

    def url_for(self, key: str) -> str: ...

    def delete_prefix(self, prefix: str) -> int:
        """Delete every key under `prefix/` (a user's whole folder). Returns files removed."""
        ...


def new_image_keys(user_id: int) -> tuple[str, str]:
    """Random (image_key, thumbnail_key). The client's filename is never used."""
    stem = uuid.uuid4().hex
    return f"{user_id}/{stem}.jpg", f"{user_id}/{stem}_thumb.jpg"


def thumb_key(key: str) -> str:
    return key.removesuffix(".jpg") + "_thumb.jpg"


def cutout_key(key: str) -> str:
    """Transparent PNG cutout, derived from the original's key."""
    return key.removesuffix(".jpg") + "_cutout.png"


def cutout_thumb_key(key: str) -> str:
    return key.removesuffix(".jpg") + "_cutout_thumb.png"


def derived_keys(key: str) -> tuple[str, ...]:
    """Every file that belongs to one item, given its original's key."""
    return key, thumb_key(key), cutout_key(key), cutout_thumb_key(key)


class LocalDiskStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError(f"Invalid storage key: {key!r}")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def url_for(self, key: str) -> str:
        return f"/media/{key}"

    def delete_prefix(self, prefix: str) -> int:
        folder = self._path(prefix.strip("/"))
        if not folder.is_dir():
            return 0
        removed = sum(1 for p in folder.rglob("*") if p.is_file())
        shutil.rmtree(folder, ignore_errors=True)
        return removed


# ----------------------------------------------------------------------- remote storage


def _checked(key: str) -> str:
    """Remote keys are `<user id>/<random name>`: never absolute, never `..`."""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"Invalid storage key: {key!r}")
    return key


def _content_type(key: str) -> str:
    return "image/png" if key.endswith(".png") else "image/jpeg"


class SupabaseStorage:
    """A private Supabase Storage bucket, through its REST API with the service key.

    The bucket must be private: photos are only ever served by the login-checked /media route.
    """

    def __init__(
        self,
        url: str,
        service_key: str,
        bucket: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._base = f"{url.rstrip('/')}/storage/v1"
        self._bucket = bucket
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        try:
            return self._client.request(method, f"{self._base}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # The message of an httpx error can include the URL; keep only the type.
            raise OSError(f"Storage unreachable ({type(exc).__name__})") from None

    def save(self, key: str, data: bytes) -> None:
        key = _checked(key)
        response = self._request(
            "POST",
            f"/object/{self._bucket}/{key}",
            content=data,
            headers={"Content-Type": _content_type(key), "x-upsert": "true"},
        )
        if response.status_code not in (200, 201):
            raise OSError(f"Storage refused the upload ({response.status_code})")

    def read(self, key: str) -> bytes:
        key = _checked(key)
        response = self._request("GET", f"/object/authenticated/{self._bucket}/{key}")
        if response.status_code in (400, 404):
            raise FileNotFoundError(key)
        if response.status_code != 200:
            raise OSError(f"Storage refused the download ({response.status_code})")
        return response.content

    def delete(self, key: str) -> None:
        self._remove([_checked(key)])

    def _remove(self, keys: list[str]) -> None:
        if not keys:
            return
        response = self._request("DELETE", f"/object/{self._bucket}", json={"prefixes": keys})
        if response.status_code not in (200, 204, 404):
            raise OSError(f"Storage refused the delete ({response.status_code})")

    def url_for(self, key: str) -> str:
        return f"/media/{key}"

    def delete_prefix(self, prefix: str) -> int:
        folder = _checked(prefix.strip("/"))
        removed = 0
        while True:
            response = self._request(
                "POST",
                f"/object/list/{self._bucket}",
                json={"prefix": folder, "limit": 100, "offset": 0},
            )
            if response.status_code != 200:
                raise OSError(f"Storage refused the listing ({response.status_code})")
            names = [f"{folder}/{o['name']}" for o in response.json() if o.get("name")]
            if not names:
                return removed
            self._remove(names)
            removed += len(names)


class S3Storage:
    """Any S3-compatible bucket (Cloudflare R2, Backblaze B2, AWS S3), via the minio client."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def save(self, key: str, data: bytes) -> None:
        key = _checked(key)
        self._client.put_object(
            self._bucket, key, BytesIO(data), len(data), content_type=_content_type(key)
        )

    def read(self, key: str) -> bytes:
        from minio.error import S3Error

        key = _checked(key)
        try:
            response = self._client.get_object(self._bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                raise FileNotFoundError(key) from None
            raise
        try:
            return bytes(response.read())
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        self._client.remove_object(self._bucket, _checked(key))  # a missing key is not an error

    def url_for(self, key: str) -> str:
        return f"/media/{key}"

    def delete_prefix(self, prefix: str) -> int:
        folder = _checked(prefix.strip("/")) + "/"
        removed = 0
        for obj in list(self._client.list_objects(self._bucket, prefix=folder, recursive=True)):
            self._client.remove_object(self._bucket, obj.object_name)
            removed += 1
        return removed


def build_storage(settings: Settings) -> Storage:
    """The configured photo storage. Raises a clear error when a remote one is half set up."""
    if settings.storage_backend == "local":
        return LocalDiskStorage(settings.media_root)
    if settings.storage_backend == "supabase":
        if not settings.supabase_url or settings.supabase_service_key is None:
            raise RuntimeError(
                "STORAGE_BACKEND=supabase needs SUPABASE_URL and SUPABASE_SERVICE_KEY."
            )
        return SupabaseStorage(
            settings.supabase_url,
            settings.supabase_service_key.get_secret_value(),
            settings.supabase_bucket,
        )
    if not (settings.s3_endpoint and settings.s3_bucket and settings.s3_access_key) or (
        settings.s3_secret_key is None
    ):
        raise RuntimeError(
            "STORAGE_BACKEND=s3 needs S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY and S3_SECRET_KEY."
        )
    from minio import Minio

    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        secure=settings.s3_secure,
        region=settings.s3_region,
    )
    return S3Storage(client, settings.s3_bucket)
