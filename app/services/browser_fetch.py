"""Optional JavaScript rendering through Playwright (SCRAPER_USE_BROWSER=true).

Experimental and off by default; install with ``uv sync --extra browser`` and
``playwright install chromium``. Every network request the page makes is checked against the
same public-address rules as the safe fetcher. A browser resolves DNS itself, so unlike the
safe fetcher it cannot pin the connection: the DNS-rebinding defense does not apply here. Keep
this disabled unless you need it and run it in a network-isolated worker.
"""

import asyncio
from urllib.parse import urlsplit

from app.config import Settings
from app.services.fetcher import (
    FetchError,
    is_allowed_ip,
    parse_target,
    system_resolver,
)


class BrowserUnavailable(RuntimeError):
    pass


async def _allowed(url: str, settings: Settings) -> bool:
    if urlsplit(url).scheme in ("data", "blob", "about"):
        return True
    try:
        target = parse_target(url, settings.fetch_allow_private_hosts)
        addresses = await system_resolver(target.host, target.port)
    except FetchError:
        return False
    return all(is_allowed_ip(a, settings.fetch_allow_private_hosts) for a in addresses)


async def render_page(url: str, settings: Settings) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise BrowserUnavailable(
            "SCRAPER_USE_BROWSER is on but Playwright is not installed (uv sync --extra browser)."
        ) from None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(user_agent=settings.fetch_user_agent)

            async def guard(route, request):  # type: ignore[no-untyped-def]
                if await _allowed(request.url, settings):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", guard)
            page = await context.new_page()
            async with asyncio.timeout(settings.fetch_total_timeout):
                await page.goto(url, wait_until="networkidle")
                return str(await page.content())
        finally:
            await browser.close()
