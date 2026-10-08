"""End-to-end demo of link analysis against the seeded closet, with no internet.

    uv run python -m scripts.demo_link_analysis   # makes and removes its own account

It serves a hand-written fixture product page (and its photos) from a tiny local web server,
starts the app with the SSRF guard's dev switch on (FETCH_ALLOW_PRIVATE_HOSTS=true, which only
admits loopback/private addresses, never link-local or cloud-metadata ones), submits the page's
URL, waits for the analysis and prints the result JSON. It then shows the guard rejecting
http://169.254.169.254/ both in that dev mode and in the default (production) mode.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw
from sqlmodel import Session

from app.config import Settings, get_settings
from app.db import get_engine
from app.services.fetcher import FetchError, SafeFetcher
from app.services.imaging import process_image
from app.services.storage import LocalDiskStorage
from app.services.tagging import FakeTagger
from scripts._demo import demo_account, log_in
from scripts.seed_closet import seed_closet

PAGE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pages" / "jsonld_graph.html"
APP_PORT = 8001


def blazer_photo() -> bytes:
    """A charcoal garment-shaped block. A pixel is nudged until the offline FakeTagger sees
    outerwear, so the demo reads naturally (a real tagger would look at the photo)."""
    for nudge in range(256):
        image = Image.new("RGB", (600, 800), (232, 232, 232))
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 120, 450, 700), fill=(68, 68, 68))
        draw.rectangle((90, 120, 150, 460), fill=(68, 68, 68))
        draw.rectangle((450, 120, 510, 460), fill=(68, 68, 68))
        image.putpixel((1, 1), (nudge, 0, 0))
        out = BytesIO()
        image.save(out, format="JPEG", quality=92)
        data = out.getvalue()
        normalized = process_image(data, 10**7).main
        if asyncio.run(FakeTagger().tag(normalized)).category.value == "outerwear":
            return data
    return data


class Fixtures(BaseHTTPRequestHandler):
    photo = b""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        routes = {
            "/products/blazer": ("text/html; charset=utf-8", PAGE.read_bytes()),
            "/media/blazer-charcoal-1.jpg": ("image/jpeg", self.photo),
            "/media/blazer-charcoal-2.jpg": ("image/jpeg", self.photo),
        }
        found = routes.get(self.path.split("?")[0])
        if found is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", found[0])
        self.send_header("Content-Length", str(len(found[1])))
        self.end_headers()
        self.wfile.write(found[1])

    def log_message(self, format: str, *args: object) -> None:
        pass


def wait_for_app(client: httpx.Client) -> None:
    for _ in range(60):
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit("the app did not start")


async def guard_demo(settings: Settings) -> None:
    print("\n== SSRF guard, default (production) mode ==")
    fetcher = SafeFetcher(settings)
    for url in (
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8001/health",
        "http://localhost/",
        "http://[::1]/",
        "file:///etc/passwd",
    ):
        try:
            await fetcher.get(url)
            print(f"  ALLOWED (bad!)  {url}")
        except FetchError as exc:
            print(f"  rejected  {url}\n            -> {type(exc).__name__}: {exc}")


def main() -> None:
    Fixtures.photo = blazer_photo()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixtures)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture_url = f"http://127.0.0.1:{server.server_port}/products/blazer"

    env = {
        **os.environ,
        "FETCH_ALLOW_PRIVATE_HOSTS": "true",
        "FETCH_HOST_DELAY_SECONDS": "0",
        "TAGGER_BACKEND": "fake",
    }
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory"]
        + ["--port", str(APP_PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with (
            demo_account() as (email, password),
            httpx.Client(base_url=f"http://127.0.0.1:{APP_PORT}", timeout=60) as client,
        ):
            wait_for_app(client)
            with Session(get_engine()) as session:  # the closet the analysis compares against
                seed_closet(session, LocalDiskStorage(get_settings().media_root), email)
            token = log_in(client, email, password)

            print(f"Fixture page: {fixture_url}")
            response = client.post("/candidates", data={"url": fixture_url, "csrf_token": token})
            candidate_url = response.headers["location"]
            print(f"POST /candidates -> {response.status_code} {candidate_url}")

            started = time.time()
            while True:
                data = client.get(candidate_url, headers={"Accept": "application/json"}).json()
                if data["status"] in ("done", "failed") or time.time() - started > 60:
                    break
                time.sleep(0.5)
            print(f"\n== Final result ({data['status']} after {time.time() - started:.1f}s) ==")
            print(json.dumps(data, indent=2))

            print("\n== SSRF attempt through the app (dev mode keeps metadata blocked) ==")
            bad = client.post(
                "/candidates",
                data={"url": "http://169.254.169.254/latest/meta-data/", "csrf_token": token},
            )
            message = bad.text.split('role="alert">')[1].split("<")[0]
            print(f"POST /candidates url=http://169.254.169.254/ -> {bad.status_code}: {message}")

            client.post(f"{candidate_url}/discard", data={"csrf_token": token})  # tidy up
    finally:
        app.terminate()
        app.wait(timeout=10)
        server.shutdown()

    asyncio.run(guard_demo(Settings(database_url=get_settings().database_url)))


if __name__ == "__main__":
    main()
