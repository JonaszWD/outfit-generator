"""Product extraction from a web page.

Strategies, in order of trust, merged field by field (a later strategy only fills gaps):

1. schema.org ``Product`` as JSON-LD (via extruct), then as microdata,
2. OpenGraph and ``product:`` meta tags,
3. heuristics on the title and description (and product-looking images).

A page that yields no title, no image, or no sign of being a product page raises
``ExtractionFailed``; the UI turns that into the manual (upload a photo) fallback.
Site-specific adapters can be registered in ``ADAPTERS`` (host -> extractor).
"""

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import extruct
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from app.config import Settings
from app.engine import vocab
from app.services.fetcher import FetchError, SafeFetcher
from app.services.imaging import InvalidImageError, ProcessedImage, process_image

logger = logging.getLogger(__name__)

MAX_IMAGE_CANDIDATES = 4
GOOD_IMAGE_SIDE = 300
MAX_TITLE = 200
MAX_DESCRIPTION = 1000


class ExtractionFailed(Exception):
    """The page could not be turned into a product. The message is safe to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProductInfo(BaseModel):
    title: str
    brand: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    color: str | None = None  # a palette color name, when the page states one
    description: str | None = None
    category_hint: str | None = None  # a Category value, guessed from the text
    subtype_hint: str | None = None
    source_url: str


# ---------------------------------------------------------------------------- helpers

_PRICE_RE = re.compile(r"\d[\d.,\s ]*")
_PRICE_TEXT_RE = re.compile(
    r"(?:[$€£¥]|USD|EUR|GBP|CHF)\s?\d[\d.,]*|\d[\d.,]*\s?(?:[$€£¥]|USD|EUR|GBP|CHF)"
)
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_BLOCKED_MARKERS = (
    "access denied",
    "just a moment",
    "attention required",
    "verify you are human",
    "are you a robot",
    "captcha",
    "unusual traffic",
    "request blocked",
    "pardon our interruption",
)
_BAD_IMAGE_WORDS = re.compile(
    r"logo|sprite|icon|favicon|placeholder|pixel|blank|spacer|badge|avatar|payment|social",
    re.IGNORECASE,
)
_BAD_IMAGE_SUFFIXES = (".svg", ".gif", ".ico", ".webm", ".mp4")

COLOR_SYNONYMS = {
    "navy blue": "navy",
    "dark blue": "navy",
    "midnight": "navy",
    "sky blue": "light blue",
    "baby blue": "light blue",
    "denim blue": "blue",
    "royal blue": "blue",
    "cobalt": "blue",
    "gray": "grey",
    "silver": "grey",
    "ivory": "cream",
    "off-white": "cream",
    "off white": "cream",
    "ecru": "cream",
    "khaki": "beige",
    "sand": "beige",
    "stone": "beige",
    "camel": "tan",
    "cognac": "tan",
    "chocolate": "brown",
    "wine": "burgundy",
    "maroon": "burgundy",
    "olive green": "olive",
    "forest green": "green",
    "emerald": "green",
    "lilac": "purple",
    "violet": "purple",
    "plum": "purple",
    "coral": "orange",
    "rust": "orange",
    "mustard": "yellow",
    "blush": "pink",
    "rose": "pink",
}

CATEGORY_KEYWORDS: dict[str, tuple[str, str]] = {
    "t-shirt": ("top", "t-shirt"),
    "tee": ("top", "t-shirt"),
    "shirt": ("top", "shirt"),
    "oxford": ("top", "shirt"),
    "blouse": ("top", "blouse"),
    "polo": ("top", "polo"),
    "sweater": ("top", "sweater"),
    "jumper": ("top", "sweater"),
    "pullover": ("top", "sweater"),
    "hoodie": ("top", "hoodie"),
    "sweatshirt": ("top", "hoodie"),
    "jeans": ("bottom", "jeans"),
    "chinos": ("bottom", "chinos"),
    "trousers": ("bottom", "trousers"),
    "pants": ("bottom", "trousers"),
    "slacks": ("bottom", "trousers"),
    "shorts": ("bottom", "shorts"),
    "skirt": ("bottom", "skirt"),
    "maxi dress": ("dress", "maxi dress"),
    "cocktail dress": ("dress", "cocktail dress"),
    "dress": ("dress", "casual dress"),
    "blazer": ("outerwear", "blazer"),
    "jacket": ("outerwear", "jacket"),
    "bomber": ("outerwear", "jacket"),
    "parka": ("outerwear", "coat"),
    "coat": ("outerwear", "coat"),
    "trench": ("outerwear", "coat"),
    "overcoat": ("outerwear", "coat"),
    "raincoat": ("outerwear", "coat"),
    "puffer": ("outerwear", "jacket"),
    "windbreaker": ("outerwear", "jacket"),
    "joggers": ("bottom", "trousers"),
    "sweatpants": ("bottom", "trousers"),
    "cardigan": ("outerwear", "cardigan"),
    "sneakers": ("shoes", "sneakers"),
    "trainers": ("shoes", "sneakers"),
    "boots": ("shoes", "boots"),
    "loafers": ("shoes", "loafers"),
    "sandals": ("shoes", "sandals"),
    "heels": ("shoes", "heels"),
    "pumps": ("shoes", "heels"),
    "belt": ("accessory", "belt"),
    "scarf": ("accessory", "scarf"),
    "hat": ("accessory", "hat"),
    "beanie": ("accessory", "hat"),
    "bag": ("accessory", "bag"),
    "tote": ("accessory", "bag"),
    "watch": ("accessory", "watch"),
}


def clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] or None


def parse_price(value: Any) -> Decimal | None:
    """49.99, "1,299.00", "1.299,00", "49,99" and "$ 49.99" all work. Returns None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        text = str(value)
    elif isinstance(value, str):
        match = _PRICE_RE.search(value)
        if not match:
            return None
        text = re.sub(r"[\s ]", "", match.group(0)).strip(".,")
    else:
        return None
    if "," in text and "." in text:
        decimal_mark = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_mark == "," else ","
        text = text.replace(thousands, "").replace(decimal_mark, ".")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head}.{tail}" if len(tail) in (1, 2) else text.replace(",", "")
    try:
        price = Decimal(text)
    except InvalidOperation:
        return None
    return price if 0 < price < Decimal("1000000") else None


def color_from_text(text: str | None) -> str | None:
    """A palette color mentioned in the text (synonyms allowed), preferring the longest match."""
    if not text:
        return None
    lowered = text.lower()
    candidates = {**{name: name for name in vocab.COLOR_NAMES}, **COLOR_SYNONYMS}
    for phrase in sorted(candidates, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", lowered):
            return candidates[phrase]
    return None


def category_from_text(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    lowered = text.lower()
    for word in sorted(CATEGORY_KEYWORDS, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(word)}s?(?![a-z])", lowered):
            return CATEGORY_KEYWORDS[word]
    return None


def looks_blocked(html: str) -> bool:
    head = html[:6000].lower()
    return any(marker in head for marker in _BLOCKED_MARKERS)


# ------------------------------------------------------------------------ partial result


@dataclass
class Partial:
    """What one strategy found. Every field is optional."""

    title: str | None = None
    brand: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    images: list[str] = field(default_factory=list)
    color: str | None = None
    description: str | None = None
    category_text: str | None = None
    is_product: bool = False  # the page itself says it is a product page


class PageExtractor(Protocol):
    def extract(self, html: str, url: str) -> Partial | None: ...


# ---------------------------------------------------------------------- JSON-LD / microdata


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type") or node.get("type") or []
    return {str(t).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1] for t in _as_list(raw)}


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _first_str(value: Any, *keys: str) -> str | None:
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            for key in keys:
                found = _first_str(item.get(key), *keys)
                if found:
                    return found
    return None


def _images(value: Any, base_url: str) -> list[str]:
    urls: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            candidate = item.get("url") or item.get("contentUrl") or item.get("@id")
            urls.extend(_images(candidate, base_url))
        elif isinstance(item, list):
            urls.extend(_images(item, base_url))
    return [urljoin(base_url, u.strip()) for u in urls if isinstance(u, str) and u.strip()]


def _offer_price(offers: Any) -> tuple[Decimal | None, str | None]:
    """First usable price in an offer, offer list, AggregateOffer or priceSpecification."""
    for offer in _as_list(offers):
        if not isinstance(offer, dict):
            continue
        currency = _first_str(offer.get("priceCurrency"))
        for key in ("price", "lowPrice"):
            price = parse_price(offer.get(key))
            if price is not None:
                return price, currency
        for spec in _as_list(offer.get("priceSpecification")):
            if isinstance(spec, dict):
                price = parse_price(spec.get("price"))
                if price is not None:
                    return price, _first_str(spec.get("priceCurrency")) or currency
        nested_price, nested_currency = _offer_price(offer.get("offers"))
        if nested_price is not None:
            return nested_price, nested_currency or currency
    return None, None


def _from_product_node(node: dict[str, Any], base_url: str) -> Partial:
    variants = [v for v in _as_list(node.get("hasVariant")) if isinstance(v, dict)]
    price, currency = _offer_price(node.get("offers"))
    images = _images(node.get("image"), base_url)
    color = _first_str(node.get("color"))
    for variant in variants:  # ProductGroup: borrow what the group itself lacks
        if price is None:
            price, currency = _offer_price(variant.get("offers"))
        images += _images(variant.get("image"), base_url)
        color = color or _first_str(variant.get("color"))
    return Partial(
        title=clean_text(_first_str(node.get("name")), MAX_TITLE),
        brand=clean_text(_first_str(node.get("brand"), "name"), 80)
        or clean_text(_first_str(node.get("manufacturer"), "name"), 80),
        price=price,
        currency=currency,
        images=images,
        color=color_from_text(color),
        description=clean_text(_first_str(node.get("description")), MAX_DESCRIPTION),
        category_text=" ".join(
            filter(None, [_first_str(node.get("category")), _first_str(node.get("name"))])
        ),
        is_product=True,
    )


def _microdata_to_ld(item: Any) -> Any:
    if isinstance(item, dict) and "properties" in item:
        out: dict[str, Any] = {"@type": item.get("type", "")}
        for key, value in item["properties"].items():
            out[key] = _microdata_to_ld(value)
        return out
    if isinstance(item, list):
        return [_microdata_to_ld(v) for v in item]
    return item


class StructuredDataExtractor:
    """schema.org Product from JSON-LD, falling back to microdata."""

    def extract(self, html: str, url: str) -> Partial | None:
        try:
            data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata"])
        except Exception:  # extruct raises on some malformed pages; that is "no data"
            logger.debug("extruct failed", exc_info=True)
            return None
        microdata = [_microdata_to_ld(m) for m in data.get("microdata", [])]
        nodes = [*data.get("json-ld", []), *microdata]
        for node in _walk(nodes):
            if _types(node) & {"Product", "ProductGroup", "IndividualProduct"}:
                partial = _from_product_node(node, url)
                if partial.title:
                    return partial
        return None


# ----------------------------------------------------------------------------- OpenGraph


def _meta(soup: BeautifulSoup, *names: str) -> list[str]:
    values: list[str] = []
    for tag in soup.find_all("meta"):
        raw_key = tag.get("property") or tag.get("name") or tag.get("itemprop") or ""
        key = raw_key.lower() if isinstance(raw_key, str) else ""
        content = tag.get("content")
        if key in names and isinstance(content, str) and content.strip():
            values.append(content.strip())
    return values


def _first_meta(soup: BeautifulSoup, *names: str) -> str | None:
    values = _meta(soup, *names)
    return values[0] if values else None


class OpenGraphExtractor:
    def extract(self, html: str, url: str) -> Partial | None:
        soup = BeautifulSoup(html, "lxml")
        title = clean_text(_first_meta(soup, "og:title", "twitter:title"), MAX_TITLE)
        og_type = (_first_meta(soup, "og:type") or "").lower()
        images = [
            urljoin(url, u)
            for u in _meta(soup, "og:image", "og:image:secure_url", "og:image:url", "twitter:image")
        ]
        price = parse_price(_first_meta(soup, "product:price:amount", "og:price:amount"))
        currency = _first_meta(soup, "product:price:currency", "og:price:currency")
        brand = _first_meta(soup, "product:brand", "og:brand")
        description = _first_meta(soup, "og:description", "description", "twitter:description")
        if not (title or images or price):
            return None
        return Partial(
            title=title,
            brand=clean_text(brand, 80),
            price=price,
            currency=currency,
            images=images,
            color=color_from_text(_first_meta(soup, "product:color")),
            description=clean_text(description, MAX_DESCRIPTION),
            category_text=_first_meta(soup, "product:category"),
            is_product=og_type.startswith("product") or price is not None,
        )


# ---------------------------------------------------------------------------- heuristics

_TITLE_SPLIT = re.compile(r"\s+[|\-–—:]\s+")


class HeuristicExtractor:
    """Last resort: the page title, a price in the text, and images that look like the product."""

    def extract(self, html: str, url: str) -> Partial | None:
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.find("h1")
        page_title = soup.title.get_text(" ") if soup.title else None
        raw_title = (h1.get_text(" ") if h1 else None) or page_title
        title = clean_text(raw_title, MAX_TITLE)
        if title and not h1:
            title = clean_text(_TITLE_SPLIT.split(title)[0], MAX_TITLE)
        if not title:
            return None
        description = _first_meta(soup, "description")
        price, currency = None, None
        scope = soup.select_one("[itemprop=price]") or soup.find(class_=re.compile("price", re.I))
        if scope is not None:
            text = scope.get("content") if scope.has_attr("content") else scope.get_text(" ")
            price = parse_price(text)
            symbol = _PRICE_TEXT_RE.search(str(text))
            if symbol:
                found = symbol.group(0)
                currency = next((c for s, c in _CURRENCY_SYMBOLS.items() if s in found), None)
        images: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not isinstance(src, str) or not src.strip():
                continue
            classes = " ".join(img.get("class") or []).lower()
            width = str(img.get("width") or "")
            looks_main = (
                img.get("itemprop") == "image"
                or "product" in classes
                or "product" in src.lower()
                or (width.isdigit() and int(width) >= GOOD_IMAGE_SIDE)
            )
            if looks_main:
                images.append(urljoin(url, src.strip()))
        text = f"{title} {description or ''}"
        return Partial(
            title=title,
            price=price,
            currency=currency,
            images=images,
            color=color_from_text(text),
            description=clean_text(description, MAX_DESCRIPTION),
            category_text=text,
            is_product=price is not None,
        )


ADAPTERS: dict[str, PageExtractor] = {}  # site-specific extractors, keyed by host
DEFAULT_CHAIN: tuple[PageExtractor, ...] = (
    StructuredDataExtractor(),
    OpenGraphExtractor(),
    HeuristicExtractor(),
)


# --------------------------------------------------------------------------- public API


def extract_html(html: str, url: str) -> ProductInfo:
    """Turn a page into a ProductInfo or raise ExtractionFailed."""
    if looks_blocked(html):
        raise ExtractionFailed("The site showed a bot-check page instead of the product.")
    host = (urlsplit(url).hostname or "").lower()
    chain = ([ADAPTERS[host]] if host in ADAPTERS else []) + list(DEFAULT_CHAIN)
    parts = [p for extractor in chain if (p := extractor.extract(html, url)) is not None]
    if not parts:
        raise ExtractionFailed("No product details were found on that page.")

    def first[T](values: list[T | None]) -> T | None:
        return next((v for v in values if v), None)

    title = first([p.title for p in parts])
    images = list(dict.fromkeys(u for p in parts for u in p.images))
    price = first([p.price for p in parts])
    is_product = any(p.is_product for p in parts) or price is not None
    if not title:
        raise ExtractionFailed("The page has no product title.")
    if not is_product:
        raise ExtractionFailed("That page does not look like a product page.")
    if not images:
        raise ExtractionFailed("No product image was found on that page.")
    text = " ".join(filter(None, [title, *(p.category_text for p in parts)]))
    hint = category_from_text(text)
    return ProductInfo(
        title=title,
        brand=first([p.brand for p in parts]),
        price=price,
        currency=first([p.currency for p in parts]),
        image_urls=images[:12],
        color=first([p.color for p in parts])
        or color_from_text(title)
        or color_from_text(first([p.description for p in parts])),
        description=first([p.description for p in parts]),
        category_hint=hint[0] if hint else None,
        subtype_hint=hint[1] if hint else None,
        source_url=url,
    )


async def extract_product(
    url: str, fetcher: SafeFetcher, settings: Settings | None = None
) -> ProductInfo:
    """Fetch a product page safely and extract it. Fetch problems become ExtractionFailed too."""
    try:
        page = await fetcher.get(url, expect="html")
        html = page.body.decode(_charset(page.content_type, page.body), errors="replace")
    except FetchError as exc:
        raise ExtractionFailed(str(exc)) from exc
    try:
        return extract_html(html, page.final_url)
    except ExtractionFailed:
        if settings is not None and settings.scraper_use_browser:
            from app.services.browser_fetch import render_page  # optional dependency

            return extract_html(await render_page(url, settings), url)
        raise


def _charset(content_type: str, body: bytes) -> str:
    match = re.search(rb"charset=([\w-]+)", body[:2048], re.IGNORECASE)
    return match.group(1).decode() if match else "utf-8"


def rank_images(urls: list[str]) -> list[str]:
    """Drop logos, sprites and icons; keep the page's own order otherwise."""
    kept = []
    for url in urls:
        path = urlsplit(url).path.lower()
        if path.endswith(_BAD_IMAGE_SUFFIXES) or _BAD_IMAGE_WORDS.search(path.rsplit("/", 1)[-1]):
            continue
        if urlsplit(url).scheme in ("http", "https"):
            kept.append(url)
    return kept


async def fetch_product_image(
    info: ProductInfo, fetcher: SafeFetcher, max_bytes: int
) -> tuple[ProcessedImage, str]:
    """Download the best product image through the safe fetcher and normalize it.

    The first image with a reasonable size wins; otherwise the largest one that downloaded.
    """
    best: tuple[int, ProcessedImage, str] | None = None
    for url in rank_images(info.image_urls)[:MAX_IMAGE_CANDIDATES]:
        try:
            fetched = await fetcher.get(url, expect="image", max_bytes=max_bytes)
            image = process_image(fetched.body, max_bytes)
        except (FetchError, InvalidImageError):
            continue
        if min(image.width, image.height) >= GOOD_IMAGE_SIDE:
            return image, url
        area = image.width * image.height
        if best is None or area > best[0]:
            best = (area, image, url)
    if best is None:
        raise ExtractionFailed("None of the product images could be downloaded.")
    return best[1], best[2]
