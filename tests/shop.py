"""A fake web shop for tests: pages and images served through the real SafeFetcher.

DNS and HTTP are mocked at the resolver and transport level, so the SSRF guard, robots
handling and size limits all run for real. No network access.
"""

from collections.abc import Callable
from io import BytesIO

import httpx
from PIL import Image

from app.config import Settings
from app.services.fetcher import HostPolicy, SafeFetcher

PUBLIC_IP = "93.184.216.34"


def jpeg(color: str | tuple[int, int, int] = "red", size: tuple[int, int] = (600, 800)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG")
    return out.getvalue()


def product_html(
    title: str = "Slim Fit Oxford Shirt",
    image: str = "https://shop.example/img/shirt.jpg",
    price: str = "59.90",
    brand: str = "Northfield",
    color: str | None = None,
) -> str:
    color_line = f'"color": "{color}",' if color else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Product", "name": "{title}",
  "image": "{image}", "brand": {{"@type": "Brand", "name": "{brand}"}}, {color_line}
  "offers": {{"@type": "Offer", "price": "{price}", "priceCurrency": "EUR"}}}}
</script></head><body><h1>{title}</h1></body></html>"""


class Shop:
    """Routes by path (the host is ignored): pages, images, an optional robots.txt."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.images: dict[str, bytes] = {}
        self.robots: str | None = None
        self.statuses: dict[str, int] = {}
        self.requests: list[httpx.Request] = []

    @property
    def page_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/p/")]

    def add_product(self, path: str, **kwargs: str | None) -> None:
        self.pages[path] = product_html(**kwargs)  # type: ignore[arg-type]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            if self.robots is None:
                return httpx.Response(404)
            headers = {"content-type": "text/plain"}
            return httpx.Response(200, headers=headers, content=self.robots.encode())
        if path in self.statuses:
            return httpx.Response(self.statuses[path])
        if path in self.pages:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=self.pages[path].encode()
            )
        if path in self.images:
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=self.images[path]
            )
        return httpx.Response(404)

    def fetcher(self, settings: Settings | None = None) -> SafeFetcher:
        async def resolve(host: str, port: int) -> list[str]:
            return [PUBLIC_IP]

        resolved = settings or Settings(database_url="postgresql+psycopg://x:y@localhost/z")
        return SafeFetcher(
            resolved,
            resolver=resolve,
            transport=httpx.MockTransport(self.handle),
            policy=HostPolicy(delay=0.0),
        )

    def factory(self, settings: Settings) -> Callable[[], SafeFetcher]:
        return lambda: self.fetcher(settings)
