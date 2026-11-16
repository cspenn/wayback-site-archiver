"""URL normalization, domain filtering, HTML link parsing, sitemap parsing, and SSRF protection."""

import html
import ipaddress
import re
import socket
import zlib
from html.parser import HTMLParser
from typing import override
from urllib.parse import SplitResult, urldefrag, urljoin, urlsplit, urlunsplit

from civil_service.exceptions import TransientFetchError, UnsafeTargetError

_LINK_ATTRS = {"a": "href", "area": "href", "iframe": "src", "frame": "src", "link": "href"}
_LINK_RELS = frozenset({"alternate", "canonical"})
_LOC_RE = re.compile(r"<loc>([^<]*)</loc>", re.IGNORECASE)
_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_WBITS = 31
_HTTP_PORT = 80
_HTTPS_PORT = 443


class _AnchorExtractor(HTMLParser):
    """Collect page-link URLs from anchor, area, frame, and alternate/canonical link elements."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        wanted = _LINK_ATTRS.get(tag)
        if wanted is None:
            return
        attributes = dict(attrs)
        value = attributes.get(wanted)
        if value is None:
            return
        if tag == "link" and not _LINK_RELS & set((attributes.get("rel") or "").lower().split()):
            return
        self.hrefs.append(value)


def normalize_url(base_url: str, href: str, allowed_schemes: tuple[str, ...]) -> str | None:
    """Resolve and normalize a candidate link against a base URL.

    Args:
        base_url: Absolute page URL used to resolve relative references.
        href: Raw anchor href string.
        allowed_schemes: Permitted URL schemes such as http and https.

    Returns:
        Normalized absolute URL without fragment, or None if invalid/disallowed.
    """
    cleaned = href.strip()
    if not cleaned or cleaned.startswith("#"):
        return None
    try:
        joined, _fragment = urldefrag(urljoin(base_url, cleaned))
        parts = urlsplit(joined)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme not in allowed_schemes or not netloc or "@" in netloc:
        return None  # a link with user:password@ would send credentials to a public archive
    if not netloc.isascii():
        netloc = _ascii_netloc(parts)
        if not netloc:
            return None
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _ascii_netloc(parts: SplitResult) -> str:
    """Return the host as Python's IDNA codec writes it, or "" if it cannot be written.

    Every later step (the address check and the fetch) then uses the same ASCII name. A
    transport with its own IDN rules could otherwise resolve a different name than we checked.
    """
    try:
        host, port = parts.hostname, parts.port
        ascii_host = host.encode("idna").decode("ascii") if host else ""
    except (UnicodeError, ValueError):
        return ""
    return f"{ascii_host}:{port}" if ascii_host and port else ascii_host


def is_ip_literal(host: str) -> bool:
    """Return True when host is an IPv4 or IPv6 address, so no DNS lookup can be pinned."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_same_domain(url: str, root_netloc: str, include_subdomains: bool) -> bool:
    """Return True when url belongs to root_netloc (or its subdomains)."""
    host = (urlsplit(url).hostname or "").lower()
    base_host = (urlsplit(f"//{root_netloc}").hostname or root_netloc).lower()
    if host == base_host:
        return True
    return include_subdomains and host.endswith(f".{base_host}")


def host_port(url: str) -> tuple[str, int]:
    """Return the host name and port of url, using the scheme default port when absent.

    Raises:
        UnsafeTargetError: If the host is missing or the port is not a number in range.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        msg = f"Invalid URL or port: {url}"
        raise UnsafeTargetError(msg) from exc
    if not parts.hostname:
        msg = f"Missing hostname in URL: {url}"
        raise UnsafeTargetError(msg)
    return parts.hostname, port or (_HTTPS_PORT if parts.scheme == "https" else _HTTP_PORT)


def validate_public_host(url: str) -> tuple[str, ...]:
    """Verify via DNS resolution that url does not target a non-public IP address.

    Args:
        url: Target URL to resolve and check.

    Returns:
        Every resolved IP address, in resolver order and without duplicates. Callers pin the
        connection to this set, so a host with an unreachable address family still connects.

    Raises:
        UnsafeTargetError: If the host is missing, malformed, or has a non-global address.
        TransientFetchError: If the host does not resolve. The resolver may be offline, so the
            caller retries instead of treating the URL as permanently unusable.
    """
    host, _port = host_port(url)
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except UnicodeError as exc:
        msg = f"Invalid host name: {host}"
        raise UnsafeTargetError(msg) from exc
    except OSError as exc:  # includes gaierror: a resolver can fail while the network is down
        msg = f"Failed to resolve host: {host}"
        raise TransientFetchError(msg) from exc

    addresses = [str(record[4][0]).split("%", 1)[0] for record in records]
    for raw_ip in addresses:
        ip_obj = ipaddress.ip_address(raw_ip)
        if not ip_obj.is_global or ip_obj.is_multicast:
            msg = f"Blocked non-public address {raw_ip} for host {host}"
            raise UnsafeTargetError(msg)
    return tuple(dict.fromkeys(addresses))


def extract_links(html_text: str, base_url: str, allowed_schemes: tuple[str, ...]) -> list[str]:
    """Extract normalized, deduplicated page-link URLs from HTML preserving order."""
    parser = _AnchorExtractor()
    parser.feed(html_text)
    parser.close()
    normalized = (normalize_url(base_url, href, allowed_schemes) for href in parser.hrefs)
    return list(dict.fromkeys(url for url in normalized if url is not None))


def read_sitemap_bytes(raw: bytes, cap: int) -> str | None:
    """Return sitemap text, gunzipping when needed, or None if gzip data is bad or over cap."""
    if raw.startswith(_GZIP_MAGIC):
        try:
            raw = zlib.decompressobj(wbits=_GZIP_WBITS).decompress(raw, cap + 1)
        except zlib.error:
            return None
        if len(raw) > cap:
            return None
    return raw.decode("utf-8", errors="replace")


def parse_sitemap(text: str) -> tuple[bool, list[str]]:
    """Return (is_sitemap_index, loc values) from sitemap XML text.

    The parser uses a linear regular expression. It builds no XML tree, so
    entity-expansion attacks do not apply.
    """
    is_index = "<sitemapindex" in text.lower()
    return is_index, [html.unescape(match.strip()) for match in _LOC_RE.findall(text)]
