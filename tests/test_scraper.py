"""Product extraction against hand-written HTML fixtures. No network."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.services.scraper import (
    ExtractionFailed,
    ProductInfo,
    category_from_text,
    color_from_text,
    extract_html,
    extract_product,
    fetch_product_image,
    parse_price,
    rank_images,
)
from tests.conftest import image_bytes
from tests.shop import Shop as ShopBase

PAGES = Path(__file__).parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (PAGES / name).read_text()


def extract(name: str, url: str = "https://shop.example/p/1") -> ProductInfo:
    return extract_html(page(name), url)


def test_jsonld_simple_product() -> None:
    info = extract("jsonld_simple.html")
    assert info.title == "Slim Fit Oxford Shirt in Navy"
    assert info.brand == "Northfield"
    assert info.price == Decimal("59.90") and info.currency == "EUR"
    assert info.image_urls == ["https://cdn.northfield.example/img/oxford-navy-front.jpg"]
    assert info.color == "navy"
    assert info.description == "A classic button-down Oxford shirt in soft cotton."
    assert (info.category_hint, info.subtype_hint) == ("top", "shirt")
    assert info.source_url == "https://shop.example/p/1"


def test_jsonld_graph_with_type_list_offer_list_and_image_objects() -> None:
    info = extract("jsonld_graph.html", "https://rowe.example/men/blazer")
    assert info.title == "Unstructured Wool Blazer"
    assert info.brand == "Atelier Rowe"
    assert info.price == Decimal("1249.00") and info.currency == "SEK"  # first offer, "1,249.00"
    assert info.image_urls == [
        "https://rowe.example/media/blazer-charcoal-1.jpg",
        "https://rowe.example/media/blazer-charcoal-2.jpg",
    ]
    assert (info.category_hint, info.subtype_hint) == ("outerwear", "blazer")


def test_jsonld_product_group_borrows_from_variants() -> None:
    info = extract("jsonld_group.html")
    assert info.title == "Everyday Chinos"
    assert info.brand == "Plainwear"
    assert info.price == Decimal("49") and info.currency == "GBP"  # AggregateOffer lowPrice
    assert info.image_urls == [
        "https://img.plainwear.example/chinos/beige-1.jpg",
        "https://img.plainwear.example/chinos/olive-1.jpg",
    ]
    assert info.color == "beige"  # "Khaki" is a synonym for beige
    assert (info.category_hint, info.subtype_hint) == ("bottom", "chinos")


def test_microdata_product() -> None:
    info = extract("microdata.html", "https://hallam.example/boots")
    assert info.title == "Leather Chelsea Boots"
    assert info.brand == "Hallam"
    assert info.price == Decimal("189.00") and info.currency == "USD"
    assert info.image_urls == ["https://hallam.example/photos/chelsea-black.jpg"]
    assert info.color == "black"
    assert (info.category_hint, info.subtype_hint) == ("shoes", "boots")


def test_opengraph_only() -> None:
    info = extract("og_only.html")
    assert info.title == "Linen Summer Dress in Olive Green"
    assert info.brand == "Sunvale"
    assert info.price == Decimal("129.00") and info.currency == "EUR"  # "129,00"
    assert len(info.image_urls) == 2 and info.image_urls[0].endswith("olive-main.jpg")
    assert info.color == "olive"  # "olive green" -> olive
    assert (info.category_hint, info.subtype_hint) == ("dress", "casual dress")


def test_heuristics_when_there_is_no_structured_data() -> None:
    info = extract("heuristic_only.html", "https://marlow.example/loafers")
    assert info.title == "Black Leather Loafers"
    assert info.price == Decimal("95.00") and info.currency == "GBP"
    assert info.image_urls == ["https://marlow.example/products/loafers-black.jpg"]  # not the logo
    assert info.color == "black"
    assert (info.category_hint, info.subtype_hint) == ("shoes", "loafers")


def test_broken_jsonld_falls_back_to_opengraph() -> None:
    info = extract("jsonld_broken_og_fallback.html", "https://coats.example/o/1")
    assert info.title == "Camel Wool Overcoat"
    assert info.price == Decimal("249.50") and info.currency == "USD"
    assert info.image_urls == ["https://coats.example/img/overcoat-camel.jpg"]
    assert info.color == "tan"  # "camel"
    assert (info.category_hint, info.subtype_hint) == ("outerwear", "coat")


def test_strategies_fill_each_others_gaps() -> None:
    """JSON-LD without a price takes the price from OpenGraph; images from both are kept."""
    html = """<html><head>
      <meta property="og:image" content="https://cdn.example/b.jpg">
      <meta property="product:price:amount" content="30">
      <script type="application/ld+json">{"@type":"Product","name":"Linen Shirt",
        "image":"https://cdn.example/a.jpg"}</script></head></html>"""
    info = extract_html(html, "https://shop.example/x")
    assert info.price == Decimal("30")
    assert info.image_urls == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("blocked.html", "bot-check"),
        ("no_product.html", "does not look like a product page"),
        ("jsonld_no_image.html", "No product image"),
    ],
)
def test_bad_pages_raise_extraction_failed(name: str, reason: str) -> None:
    with pytest.raises(ExtractionFailed) as info:
        extract(name)
    assert reason in info.value.reason


def test_empty_and_garbage_html() -> None:
    for html in ("", "<html></html>", "not html at all", "<title></title>"):
        with pytest.raises(ExtractionFailed):
            extract_html(html, "https://shop.example/")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("49.99", "49.99"),
        (49.99, "49.99"),
        (49, "49"),
        ("1,299.00", "1299.00"),
        ("1.299,00", "1299.00"),
        ("49,99", "49.99"),
        ("1,299", "1299"),
        ("$ 49.99", "49.99"),
        ("EUR 59,90", "59.90"),
        ("12 900", "12900"),
        ("free", None),
        ("", None),
        (0, None),
        (-5, None),
        (None, None),
        (True, None),
    ],
)
def test_parse_price(raw: object, expected: str | None) -> None:
    assert parse_price(raw) == (Decimal(expected) if expected else None)


def test_color_and_category_helpers() -> None:
    assert color_from_text("Light blue linen shirt") == "light blue"  # longest match wins
    assert color_from_text("Navy Blue Polo") == "navy"
    assert color_from_text("Charcoal gray sweater") == "charcoal"  # palette name beats synonym
    assert color_from_text("Heather gray sweater") == "grey"
    assert color_from_text("The cotton shirt") is None
    assert category_from_text("Maxi dress in cotton") == ("dress", "maxi dress")
    assert category_from_text("Slim chino pants") == ("bottom", "trousers")
    assert category_from_text("Gift card") is None


def test_rank_images_drops_logos_sprites_and_unsupported_types() -> None:
    urls = [
        "https://x.example/assets/logo.png",
        "https://x.example/p/shirt-1.jpg",
        "https://x.example/img/sprite-sheet.png",
        "https://x.example/p/shirt.svg",
        "https://x.example/p/shirt-2.webp",
        "https://x.example/favicon.ico",
        "data:image/png;base64,AAAA",
    ]
    assert rank_images(urls) == [
        "https://x.example/p/shirt-1.jpg",
        "https://x.example/p/shirt-2.webp",
    ]


# ------------------------------------------------------------------ through the fetcher


class Shop(ShopBase):
    """The shared fake shop, with the page served at /p/* and images by path."""

    def __init__(self, html: str, images: dict[str, bytes] | None = None) -> None:
        super().__init__()
        self.pages["/p/1"] = html
        self.images = images or {}

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path != "/robots.txt"]


async def test_extract_product_end_to_end_through_the_fetcher() -> None:
    shop = Shop(page("jsonld_simple.html"))
    info = await extract_product("https://shop.example/p/1", shop.fetcher())
    assert info.title == "Slim Fit Oxford Shirt in Navy"


async def test_fetch_errors_become_extraction_failed() -> None:
    shop = Shop("")
    with pytest.raises(ExtractionFailed) as info:
        await extract_product("http://127.0.0.1/p/1", shop.fetcher())
    assert "private address" in info.value.reason
    with pytest.raises(ExtractionFailed):
        await extract_product("https://shop.example/missing", shop.fetcher())


async def test_blocked_page_through_the_fetcher() -> None:
    shop = Shop(page("blocked.html"))
    with pytest.raises(ExtractionFailed, match="bot-check"):
        await extract_product("https://shop.example/p/1", shop.fetcher())


async def test_best_image_prefers_a_reasonably_large_one() -> None:
    small = image_bytes(size=(120, 160))
    large = image_bytes(size=(600, 800))
    shop = Shop("", {"/a.jpg": small, "/b.jpg": large, "/c.jpg": large})
    info = ProductInfo(
        title="x",
        image_urls=[f"https://shop.example/{n}.jpg" for n in ("a", "b", "c")],
        source_url="https://shop.example/p/1",
    )
    image, url = await fetch_product_image(info, shop.fetcher(), 10**7)
    assert url.endswith("/b.jpg") and image.width == 600
    assert "/c.jpg" not in shop.paths  # stopped at the first good one


async def test_best_image_falls_back_to_the_largest_small_one() -> None:
    images = {"/a.jpg": image_bytes(size=(100, 100)), "/b.jpg": image_bytes(size=(200, 150))}
    shop = Shop("", images)
    info = ProductInfo(
        title="x",
        image_urls=["https://shop.example/a.jpg", "https://shop.example/b.jpg"],
        source_url="https://shop.example/p/1",
    )
    image, url = await fetch_product_image(info, shop.fetcher(), 10**7)
    assert url.endswith("/b.jpg") and image.width == 200


async def test_unusable_images_raise_extraction_failed() -> None:
    shop = Shop("", {"/a.jpg": b"not an image"})
    info = ProductInfo(
        title="x",
        image_urls=["https://shop.example/a.jpg", "https://shop.example/gone.jpg"],
        source_url="https://shop.example/p/1",
    )
    with pytest.raises(ExtractionFailed, match="images"):
        await fetch_product_image(info, shop.fetcher(), 10**7)


async def test_images_are_fetched_through_the_ssrf_guard() -> None:
    shop = Shop("")
    info = ProductInfo(
        title="x",
        image_urls=["http://169.254.169.254/latest/meta-data/x.jpg", "http://127.0.0.1/a.jpg"],
        source_url="https://shop.example/p/1",
    )
    with pytest.raises(ExtractionFailed):
        await fetch_product_image(info, shop.fetcher(), 10**7)
    assert shop.paths == []  # nothing was requested


def test_browser_fallback_is_off_by_default_and_optional() -> None:
    settings = Settings(database_url="postgresql+psycopg://x:y@localhost/z")
    assert settings.scraper_use_browser is False
