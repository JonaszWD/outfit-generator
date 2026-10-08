"""Fetch user-supplied URLs without becoming an SSRF proxy.

Defenses, in the order a request meets them:

1. Only http/https, no credentials in the URL, only ports 80 and 443.
2. The hostname is resolved by us, and **every** address must be public (no private,
   loopback, link-local, reserved or multicast ranges, IPv4 or IPv6, including IPv4-mapped
   forms). One bad address rejects the host.
3. The connection is pinned to the validated address (the URL is rewritten to the IP, with the
   original name in the Host header and as the TLS server name), so a second DNS answer cannot
   redirect us after the check (DNS rebinding).
4. Redirects are followed by hand, at most N, and steps 1-3 run again for every hop.
5. robots.txt is honored (cached per host), hosts are paced, responses are streamed and aborted
   at a size limit, and content types are checked.
6. System proxy settings are ignored (`trust_env=False`) because a proxy would bypass step 3.

Only the host is ever logged, never the path or query string (tokens hide there).
"""

import asyncio
import hashlib
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
# httpx logs "GET <full url>" at INFO. We connect to a pinned IP and the query string may carry
# tokens, so keep the HTTP libraries quiet; this module logs only the host.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

Expect = Literal["html", "image", "text"]
Resolver = Callable[[str, int], Awaitable[list[str]]]

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
STANDARD_PORTS = {80, 443}
MAX_URL_LENGTH = 2048
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
ROBOTS_TTL_SECONDS = 3600
HTML_TYPES = ("text/html", "application/xhtml+xml")
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")
TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "igshid", "ref", "ref_src", "mc_cid", "mc_eid"}
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class FetchError(Exception):
    """Base class. Messages are safe to show to the user and never contain the URL."""


class InvalidUrl(FetchError):
    pass


class UnsupportedScheme(InvalidUrl):
    pass


class BlockedAddress(FetchError):
    pass


class TooManyRedirects(FetchError):
    pass


class ResponseTooLarge(FetchError):
    pass


class UnsupportedContentType(FetchError):
    pass


class RobotsDisallowed(FetchError):
    pass


class FetchTimeout(FetchError):
    pass


class NetworkFailure(FetchError):
    pass


class HttpStatusError(FetchError):
    def __init__(self, status: int) -> None:
        super().__init__(f"The site refused the request (HTTP {status}).")
        self.status = status


# ---------------------------------------------------------------------- address checks


def is_public_ip(value: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or _embedded_ipv4(addr)
        if embedded is not None:
            return is_public_ip(str(embedded))
    return addr.is_global and not addr.is_multicast


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv4 hidden inside NAT64 (64:ff9b::/96) and 6to4 (2002::/16) addresses."""
    packed = addr.packed
    if packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):
        return ipaddress.IPv4Address(packed[12:])
    if packed[:2] == b"\x20\x02":
        return ipaddress.IPv4Address(packed[2:6])
    return None


def is_allowed_ip(value: str, allow_private: bool = False) -> bool:
    """Public addresses always; with `allow_private` (dev/test only) also loopback and private
    ranges. Link-local (including the 169.254.169.254 cloud metadata address), unspecified,
    multicast and reserved addresses stay blocked in every mode."""
    if is_public_ip(value):
        return True
    if not allow_private:
        return False
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return True
    return addr.is_private and not (
        addr.is_link_local or addr.is_unspecified or addr.is_multicast or addr.is_reserved
    )


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise NetworkFailure("Could not find that website.") from None
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


# ---------------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path_query: str  # path plus query, never logged

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"

    def url(self) -> str:
        return f"{self.scheme}://{self.host_header}{self.path_query}"


def parse_target(url: str, allow_private: bool = False) -> Target:
    """Syntactic validation (no DNS). Raises InvalidUrl / UnsupportedScheme / BlockedAddress."""
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrl("That link is too long.")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise InvalidUrl("That doesn't look like a valid link.") from None
    if parts.scheme not in ("http", "https"):
        raise UnsupportedScheme("Only http and https links are supported.")
    if parts.username is not None or parts.password is not None:
        raise InvalidUrl("Links with a username or password are not supported.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidUrl("That doesn't look like a valid link.")
    default = 443 if parts.scheme == "https" else 80
    port = port or default
    if not allow_private and (host == "localhost" or host.endswith(LOCAL_SUFFIXES)):
        raise BlockedAddress("That link points to a private address.")
    if _is_ip_literal(host) and not is_allowed_ip(host, allow_private):
        raise BlockedAddress("That link points to a private address.")
    if not allow_private and port not in STANDARD_PORTS:
        raise InvalidUrl("Only the standard web ports (80 and 443) are supported.")
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return Target(parts.scheme, host, port, path + query)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def normalize_url(url: str) -> str:
    """Canonical form for caching: lowercase host, no fragment or tracking parameters."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in STANDARD_PORTS:
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


# ----------------------------------------------------------------------- shared policy


@dataclass
class HostPolicy:
    """Per-process state shared by all fetchers: robots.txt cache and politeness pacing."""

    delay: float = 1.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _robots: dict[str, tuple[float, RobotFileParser | None]] = field(default_factory=dict)
    _next_slot: dict[str, float] = field(default_factory=dict)

    async def pace(self, host: str) -> None:
        """Reserve the next slot for this host, then wait for it."""
        now = self.clock()
        slot = max(now, self._next_slot.get(host, now))
        self._next_slot[host] = slot + self.delay
        if slot > now:
            await self.sleep(slot - now)

    def cached_robots(self, origin: str) -> tuple[bool, RobotFileParser | None]:
        entry = self._robots.get(origin)
        if entry and self.clock() - entry[0] < ROBOTS_TTL_SECONDS:
            return True, entry[1]
        return False, None

    def store_robots(self, origin: str, parser: RobotFileParser | None) -> None:
        self._robots[origin] = (self.clock(), parser)


_default_policy: HostPolicy | None = None


def default_policy(settings: Settings) -> HostPolicy:
    global _default_policy
    if _default_policy is None:
        _default_policy = HostPolicy(delay=settings.fetch_host_delay_seconds)
    return _default_policy


# ------------------------------------------------------------------------------ fetcher


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    status: int
    content_type: str
    body: bytes


class SafeFetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        policy: HostPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.transport = transport
        self.policy = policy or default_policy(settings)
        self.user_agent = settings.fetch_user_agent
        self.robots_token = self.user_agent.split("/", 1)[0]

    async def get(
        self,
        url: str,
        *,
        expect: Expect = "html",
        max_bytes: int | None = None,
        respect_robots: bool = True,
    ) -> FetchResult:
        if max_bytes is None:
            html_limit = self.settings.fetch_max_html_kb * 1024
            max_bytes = MAX_IMAGE_BYTES if expect == "image" else html_limit
        try:
            async with asyncio.timeout(self.settings.fetch_total_timeout):
                return await self._follow(url, expect, max_bytes, respect_robots)
        except TimeoutError:
            raise FetchTimeout("The site took too long to respond.") from None

    # ---- redirects: every hop is validated again
    async def _follow(
        self, url: str, expect: Expect, max_bytes: int, respect_robots: bool
    ) -> FetchResult:
        current = url
        for _ in range(self.settings.fetch_max_redirects + 1):
            target = parse_target(current, self.settings.fetch_allow_private_hosts)
            addresses = await self._validated_addresses(target)
            if respect_robots and not await self._robots_allow(target):
                raise RobotsDisallowed("The site's robots.txt does not allow automated access.")
            await self.policy.pace(target.host)
            response = await self._request(target, addresses, expect, max_bytes)
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise NetworkFailure("The site sent an invalid redirect.")
                current = urljoin(target.url(), location)
                continue
            return response.result(target.url())
        raise TooManyRedirects("The link redirects too many times.")

    async def _validated_addresses(self, target: Target) -> list[str]:
        addresses = await self.resolver(target.host, target.port)
        if not addresses:
            raise NetworkFailure("Could not find that website.")
        allow = self.settings.fetch_allow_private_hosts
        if not all(is_allowed_ip(a, allow) for a in addresses):
            logger.warning("Blocked non-public address for host %s", target.host)
            raise BlockedAddress("That link points to a private address.")
        return addresses[:3]

    # ---- one pinned request
    async def _request(
        self, target: Target, addresses: list[str], expect: Expect, max_bytes: int
    ) -> "_Raw":
        accept = {
            "html": "text/html,application/xhtml+xml",
            "image": "image/webp,image/jpeg,image/png,*/*;q=0.5",
            "text": "text/plain,*/*;q=0.5",
        }[expect]
        headers = {"Host": target.host_header, "User-Agent": self.user_agent, "Accept": accept}
        timeout = httpx.Timeout(
            self.settings.fetch_total_timeout, connect=self.settings.fetch_connect_timeout
        )
        last_error: Exception | None = None
        for address in addresses:
            pinned = httpx.URL(
                scheme=target.scheme,
                host=address,
                port=target.port,
                raw_path=target.path_query.encode(),
            )
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
            try:
                async with (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=timeout,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        "GET", pinned, headers=headers, extensions=extensions
                    ) as response,
                ):
                    return await self._read(response, expect, max_bytes, target.host)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc  # try the next validated address
            except httpx.TimeoutException:
                raise FetchTimeout("The site took too long to respond.") from None
            except httpx.HTTPError:
                raise NetworkFailure("Could not download the page.") from None
        raise NetworkFailure("Could not connect to the site.") from last_error

    async def _read(
        self, response: httpx.Response, expect: Expect, max_bytes: int, host: str
    ) -> "_Raw":
        status = response.status_code
        logger.info("fetch %s %s -> %s", host, expect, status)
        if status in REDIRECT_STATUSES:
            return _Raw(status, "", b"", dict(response.headers))
        if status >= 400:
            raise HttpStatusError(status)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        allowed = {"html": HTML_TYPES, "image": IMAGE_TYPES, "text": ()}[expect]
        if allowed and content_type not in allowed:
            message = "The link did not return a web page."
            raise UnsupportedContentType(message if expect == "html" else "Not a supported image.")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge("The response is too large.")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise ResponseTooLarge("The response is too large.")
        return _Raw(status, content_type, bytes(body), dict(response.headers))

    # ---- robots.txt
    async def _robots_allow(self, target: Target) -> bool:
        cached, parser = self.policy.cached_robots(target.origin)
        if not cached:
            parser = await self._load_robots(target)
            self.policy.store_robots(target.origin, parser)
        if parser is None:  # unreachable or server error: assume disallowed (RFC 9309)
            return False
        return parser.can_fetch(self.robots_token, target.url())

    async def _load_robots(self, target: Target) -> RobotFileParser | None:
        robots_url = f"{target.scheme}://{target.host_header}/robots.txt"
        parser = RobotFileParser()
        try:
            # Same validation, pinning and redirect handling as any other fetch.
            result = await self._follow(robots_url, "text", MAX_ROBOTS_BYTES, respect_robots=False)
        except HttpStatusError as exc:
            if exc.status < 500:  # no robots.txt: everything is allowed
                parser.parse([])
                return parser
            return None
        except FetchError:
            return None
        parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
        return parser


@dataclass(frozen=True)
class _Raw:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str]

    def result(self, final_url: str) -> FetchResult:
        return FetchResult(final_url, self.status, self.content_type, self.body)


def build_fetcher(settings: Settings) -> SafeFetcher:
    return SafeFetcher(settings)
