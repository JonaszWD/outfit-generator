"""SSRF guard, limits and politeness of the safe fetcher. No network: DNS and HTTP are mocked
at the resolver and transport level."""

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.services.fetcher import (
    BlockedAddress,
    FetchTimeout,
    HostPolicy,
    HttpStatusError,
    InvalidUrl,
    ResponseTooLarge,
    RobotsDisallowed,
    SafeFetcher,
    TooManyRedirects,
    UnsupportedContentType,
    UnsupportedScheme,
    is_allowed_ip,
    is_public_ip,
    normalize_url,
    parse_target,
    url_hash,
)

PUBLIC = "93.184.216.34"
PUBLIC2 = "151.101.1.69"
HTML = {"content-type": "text/html; charset=utf-8"}

Handler = Callable[[httpx.Request], Any]


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+psycopg://x:y@localhost/z",
        "fetch_host_delay_seconds": 0.0,
        "fetch_total_timeout": 2.0,
    }
    return Settings(**{**base, **overrides})


def typed(content_type: str, body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": content_type}, content=body)


class Recorder:
    """A fake network: a DNS table, a request log and a per-path response table."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.dns: dict[str, list[str]] = {"shop.example": [PUBLIC], "cdn.example": [PUBLIC2]}
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.lookups: list[str] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        return self.dns[host]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        route = self.routes.get(path)
        if route is None:
            if path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers=HTML, content=b"<html>ok</html>")
        response = route(request) if callable(route) else route
        if inspect.isawaitable(response):
            response = await response
        assert isinstance(response, httpx.Response)
        return response

    def fetcher(self, **overrides: Any) -> SafeFetcher:
        return SafeFetcher(
            settings(**overrides),
            resolver=self.resolve,
            transport=httpx.MockTransport(self.handle),
            policy=HostPolicy(delay=0.0),
        )

    @property
    def pages(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/robots.txt"]


# ------------------------------------------------------------------ address classification


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.1",
        "172.16.5.4",
        "172.31.255.255",
        "192.168.0.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",
        "240.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_ip(address)


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert is_public_ip(address)


# -------------------------------------------------------------------------- URL syntax


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://shop.example/a",
        "gopher://shop.example/",
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "dict://localhost:11211/stat",
        "//shop.example/path",
        "shop.example/path",
    ],
)
async def test_only_http_and_https(url: str) -> None:
    with pytest.raises(UnsupportedScheme):
        await Recorder().fetcher().get(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://[::1]/",
        "http://[::1]:80/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00::1]/",
        "http://localhost/",
        "http://localhost./",
        "http://LOCALHOST/",
        "http://foo.localhost/",
        "http://printer.local/",
        "http://metadata.internal/",
        "http://0.0.0.0/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",
    ],
)
async def test_private_targets_are_rejected_before_any_request(url: str) -> None:
    net = Recorder()
    with pytest.raises(BlockedAddress):
        await net.fetcher().get(url)
    assert net.requests == [] and net.lookups == []


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@shop.example/",
        "http://shop.example:6379/",
        "http://shop.example:22/",
        "http://",
        "",
    ],
)
async def test_credentials_odd_ports_and_garbage_are_rejected(url: str) -> None:
    net = Recorder()
    with pytest.raises(InvalidUrl):
        await net.fetcher().get(url)
    assert net.requests == []


async def test_overlong_url_is_rejected() -> None:
    with pytest.raises(InvalidUrl):
        parse_target("http://shop.example/" + "a" * 3000)


# ----------------------------------------------------------------------- DNS resolution


async def test_hostname_resolving_to_private_ip_is_rejected() -> None:
    net = Recorder()
    net.dns["evil.example"] = ["10.0.0.7"]
    with pytest.raises(BlockedAddress):
        await net.fetcher().get("http://evil.example/")
    assert net.requests == []


@pytest.mark.parametrize("private", ["127.0.0.1", "169.254.169.254", "::1", "192.168.1.5"])
async def test_one_private_answer_among_public_ones_rejects_the_host(private: str) -> None:
    net = Recorder()
    net.dns["mixed.example"] = [PUBLIC, private]
    with pytest.raises(BlockedAddress):
        await net.fetcher().get("http://mixed.example/")
    assert net.requests == []


async def test_numeric_host_tricks_are_caught_after_resolution() -> None:
    """Decimal, octal and hex spellings of 127.0.0.1 only reveal themselves in DNS/libc."""
    net = Recorder()
    for host in ("2130706433", "0x7f.1", "0177.0.0.1", "127.1"):
        net.dns[host] = ["127.0.0.1"]
        with pytest.raises(BlockedAddress):
            await net.fetcher().get(f"http://{host}/")
    assert net.requests == []


async def test_dns_rebinding_cannot_swap_the_address_after_validation() -> None:
    """The first answer is public, later answers are private (the rebinding trick)."""
    answers = iter([[PUBLIC], [PUBLIC], ["127.0.0.1"]])
    net = Recorder()

    async def rebinding(host: str, port: int) -> list[str]:
        return next(answers)

    fetcher = SafeFetcher(
        settings(),
        resolver=rebinding,
        transport=httpx.MockTransport(net.handle),
        policy=HostPolicy(delay=0.0),
    )
    result = await fetcher.get("http://shop.example/page")
    assert result.status == 200
    # robots.txt and the page each resolved once, and each connected to the validated address
    assert [r.url.host for r in net.requests] == [PUBLIC, PUBLIC]
    # the next lookup now answers 127.0.0.1: a fresh fetch must refuse it
    again = SafeFetcher(
        settings(),
        resolver=rebinding,
        transport=httpx.MockTransport(net.handle),
        policy=HostPolicy(delay=0.0),
    )
    with pytest.raises(BlockedAddress):
        await again.get("http://shop.example/other", respect_robots=False)
    assert len(net.requests) == 2  # nothing was sent to 127.0.0.1


async def test_connection_is_pinned_to_the_validated_ip() -> None:
    net = Recorder()
    await net.fetcher().get("https://shop.example/p?id=1")
    page = net.pages[0]
    assert page.url.host == PUBLIC and page.url.scheme == "https"
    assert page.headers["host"] == "shop.example"
    assert page.extensions["sni_hostname"] == "shop.example"  # TLS verifies the real name
    assert page.url.raw_path == b"/p?id=1"
    assert net.lookups.count("shop.example") == 2  # robots.txt + page, each validated


async def test_identifies_itself_with_a_user_agent() -> None:
    net = Recorder()
    await net.fetcher(fetch_user_agent="WardrobeBot/9 (test)").get("http://shop.example/")
    assert all(r.headers["user-agent"] == "WardrobeBot/9 (test)" for r in net.requests)


# ---------------------------------------------------------------------------- redirects


async def test_redirect_to_metadata_ip_is_rejected() -> None:
    net = Recorder(
        {
            "/go": httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        }
    )
    with pytest.raises(BlockedAddress):
        await net.fetcher().get("http://shop.example/go")
    assert [r.url.host for r in net.pages] == [PUBLIC]  # the second hop never connected


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://localhost/admin",
        "file:///etc/passwd",
        "http://evil.example/",
    ],
)
async def test_redirects_are_revalidated(location: str) -> None:
    net = Recorder({"/go": httpx.Response(301, headers={"location": location})})
    net.dns["evil.example"] = ["10.1.2.3"]
    with pytest.raises((BlockedAddress, UnsupportedScheme)):
        await net.fetcher().get("http://shop.example/go")
    assert len(net.pages) == 1


async def test_redirect_to_a_public_host_is_followed_and_pinned_again() -> None:
    net = Recorder({"/go": httpx.Response(302, headers={"location": "https://cdn.example/final"})})
    result = await net.fetcher().get("http://shop.example/go")
    assert result.status == 200 and result.final_url == "https://cdn.example/final"
    assert [r.url.host for r in net.pages] == [PUBLIC, PUBLIC2]
    assert net.pages[1].headers["host"] == "cdn.example"


async def test_relative_redirects_work() -> None:
    net = Recorder({"/a": httpx.Response(302, headers={"location": "/b"})})
    result = await net.fetcher().get("http://shop.example/a")
    assert result.final_url == "http://shop.example/b"


async def test_redirect_limit() -> None:
    net = Recorder({"/loop": httpx.Response(302, headers={"location": "/loop"})})
    with pytest.raises(TooManyRedirects):
        await net.fetcher(fetch_max_redirects=5).get("http://shop.example/loop")
    assert len(net.pages) == 6  # the original request plus 5 redirects


async def test_five_redirects_are_allowed() -> None:
    hops = {f"/h{n}": httpx.Response(302, headers={"location": f"/h{n + 1}"}) for n in range(5)}
    net = Recorder(hops)
    result = await net.fetcher(fetch_max_redirects=5).get("http://shop.example/h0")
    assert result.final_url.endswith("/h5")


# ------------------------------------------------------------------------------- limits


async def test_oversized_declared_length_is_rejected_without_reading() -> None:
    declared = httpx.Response(200, headers={**HTML, "content-length": "999999"}, content=b"x")
    net = Recorder({"/big": declared})
    with pytest.raises(ResponseTooLarge):
        await net.fetcher().get("http://shop.example/big", max_bytes=1000)


async def test_streaming_body_is_aborted_at_the_limit() -> None:
    sent = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(10_000):
            sent += 1
            yield b"x" * 1024

    net = Recorder({"/big": httpx.Response(200, headers=HTML, content=endless())})
    with pytest.raises(ResponseTooLarge):
        await net.fetcher().get("http://shop.example/big", max_bytes=10 * 1024)
    assert sent < 50  # stopped early instead of downloading ~10 MB


async def test_default_html_limit_comes_from_settings() -> None:
    body = b"<html>" + b"a" * 2048 + b"</html>"
    net = Recorder({"/page": httpx.Response(200, headers=HTML, content=body)})
    with pytest.raises(ResponseTooLarge):
        await net.fetcher(fetch_max_html_kb=1).get("http://shop.example/page")


async def test_timeout() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, headers=HTML, content=b"late")

    net = Recorder({"/slow": slow})
    with pytest.raises(FetchTimeout):
        await net.fetcher(fetch_total_timeout=0.1).get("http://shop.example/slow")


async def test_connect_timeout_is_a_fetch_error_not_a_crash() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("nope")

    net = Recorder({"/x": refuse})
    with pytest.raises(Exception) as info:
        await net.fetcher().get("http://shop.example/x")
    assert type(info.value).__name__ in {"NetworkFailure", "FetchTimeout"}


async def test_content_type_is_checked() -> None:
    net = Recorder(
        {
            "/json": typed("application/json", b"{}"),
            "/svg": typed("image/svg+xml", b"<svg/>"),
            "/page": httpx.Response(200, headers=HTML, content=b"<html/>"),
            "/img": typed("image/png", b"png"),
        }
    )
    fetcher = net.fetcher()
    with pytest.raises(UnsupportedContentType):
        await fetcher.get("http://shop.example/json")
    with pytest.raises(UnsupportedContentType):
        await fetcher.get("http://shop.example/svg", expect="image")
    with pytest.raises(UnsupportedContentType):
        await fetcher.get("http://shop.example/page", expect="image")
    with pytest.raises(UnsupportedContentType):
        await fetcher.get("http://shop.example/img", expect="html")
    assert (await fetcher.get("http://shop.example/img", expect="image")).body == b"png"


async def test_error_status() -> None:
    net = Recorder({"/blocked": httpx.Response(403, content=b"nope")})
    with pytest.raises(HttpStatusError) as info:
        await net.fetcher().get("http://shop.example/blocked")
    assert info.value.status == 403 and "403" in str(info.value)


# ----------------------------------------------------------------------------- robots.txt


def robots(text: str) -> dict[str, Any]:
    return {"/robots.txt": typed("text/plain", text.encode())}


async def test_robots_disallow_is_respected() -> None:
    net = Recorder(robots("User-agent: *\nDisallow: /"))
    with pytest.raises(RobotsDisallowed):
        await net.fetcher().get("http://shop.example/p/1")
    assert net.pages == []


async def test_robots_rules_for_our_agent_and_specific_paths() -> None:
    rules = "User-agent: WardrobeBot\nDisallow: /private\n\nUser-agent: *\nDisallow: /"
    net = Recorder(robots(rules))
    fetcher = net.fetcher()
    assert (await fetcher.get("http://shop.example/public")).status == 200
    with pytest.raises(RobotsDisallowed):
        await fetcher.get("http://shop.example/private/thing")


async def test_missing_robots_means_allowed() -> None:
    net = Recorder()
    assert (await net.fetcher().get("http://shop.example/p")).status == 200


async def test_unreachable_robots_means_disallowed() -> None:
    net = Recorder({"/robots.txt": httpx.Response(503)})
    with pytest.raises(RobotsDisallowed):
        await net.fetcher().get("http://shop.example/p")


async def test_robots_is_cached_per_host() -> None:
    net = Recorder()
    fetcher = net.fetcher()
    await fetcher.get("http://shop.example/a")
    await fetcher.get("http://shop.example/b")
    await fetcher.get("http://cdn.example/c")
    robots_requests = [r for r in net.requests if r.url.path == "/robots.txt"]
    assert len(robots_requests) == 2  # once per host, not once per page


async def test_robots_redirecting_to_a_private_address_counts_as_unreachable() -> None:
    net = Recorder(
        {"/robots.txt": httpx.Response(302, headers={"location": "http://127.0.0.1/robots.txt"})}
    )
    with pytest.raises(RobotsDisallowed):
        await net.fetcher().get("http://shop.example/p")


async def test_robots_can_be_skipped_for_images_on_a_cdn_we_already_checked() -> None:
    net = Recorder(robots("User-agent: *\nDisallow: /"))
    result = await net.fetcher().get("http://shop.example/x", respect_robots=False)
    assert result.status == 200


# ---------------------------------------------------------------------------- politeness


async def test_requests_to_the_same_host_are_spaced_out() -> None:
    now = 100.0
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    policy = HostPolicy(delay=2.0, clock=lambda: now, sleep=fake_sleep)
    await policy.pace("a.example")
    await policy.pace("a.example")
    await policy.pace("a.example")
    await policy.pace("b.example")
    assert waits == [2.0, 2.0]  # first request is free, then 2s apart; other hosts independent


# --------------------------------------------------------------------------------- logging


async def test_only_the_host_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    net = Recorder()
    await net.fetcher().get("https://shop.example/secret/path?token=SUPERSECRET&email=a@b.c")
    assert "shop.example" in caplog.text
    assert "SUPERSECRET" not in caplog.text and "secret/path" not in caplog.text


async def test_errors_never_echo_the_url() -> None:
    net = Recorder({"/x": httpx.Response(403)})
    with pytest.raises(HttpStatusError) as info:
        await net.fetcher().get("http://shop.example/x?token=SUPERSECRET")
    assert "SUPERSECRET" not in str(info.value)


# ------------------------------------------------------------------- dev/test escape hatch


async def test_private_hosts_are_only_allowed_with_the_explicit_setting() -> None:
    net = Recorder()
    net.dns["local.test"] = ["127.0.0.1"]
    with pytest.raises(BlockedAddress):
        await net.fetcher().get("http://local.test/")
    result = await net.fetcher(fetch_allow_private_hosts=True).get("http://local.test:8123/p")
    assert result.status == 200 and net.pages[-1].url.port == 8123


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[fe80::1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "file:///etc/passwd",
    ],
)
async def test_even_the_dev_mode_never_allows_metadata_or_reserved_addresses(target: str) -> None:
    net = Recorder()
    with pytest.raises((BlockedAddress, UnsupportedScheme)):
        await net.fetcher(fetch_allow_private_hosts=True).get(target)
    assert net.requests == []


async def test_dev_mode_still_checks_dns_answers_and_redirects() -> None:
    net = Recorder({"/go": httpx.Response(302, headers={"location": "http://169.254.169.254/"})})
    net.dns["meta.test"] = ["169.254.169.254"]
    fetcher = net.fetcher(fetch_allow_private_hosts=True)
    with pytest.raises(BlockedAddress):
        await fetcher.get("http://meta.test/")
    with pytest.raises(BlockedAddress):
        await fetcher.get("http://shop.example/go")


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("192.168.0.9", True),
        ("::1", True),
        ("169.254.169.254", False),
        ("fe80::1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("8.8.8.8", True),
    ],
)
def test_dev_mode_address_policy(address: str, allowed: bool) -> None:
    assert is_allowed_ip(address, allow_private=True) is allowed
    if allowed and address not in ("8.8.8.8",):
        assert is_allowed_ip(address, allow_private=False) is False


def test_the_guard_is_on_by_default() -> None:
    assert settings().fetch_allow_private_hosts is False


# --------------------------------------------------------------------- url normalization


def test_normalize_url_for_caching() -> None:
    a = "HTTPS://Shop.Example/p/1?color=navy&utm_source=x&gclid=1#reviews"
    b = "https://shop.example/p/1?color=navy"
    assert normalize_url(a) == b
    assert url_hash(a) == url_hash(b)
    assert url_hash("https://shop.example/p/2") != url_hash(b)
    assert url_hash("https://shop.example/p?a=1&b=2") == url_hash("https://shop.example/p?b=2&a=1")
