"""Tiered HTTP fetcher with Retry-After handling and curl/curl_cffi/camoufox fallbacks."""

import contextlib
import os
import subprocess  # nosec B404 - fixed list-form curl CLI invocation with shell=False
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

import curl_cffi.requests as curl_requests
import structlog
from camoufox.sync_api import Camoufox
from curl_cffi import CurlOpt

from civil_service.config import AppSettings
from civil_service.exceptions import (
    BlockedError,
    FetchError,
    RateLimitError,
    StopRequested,
    TransientFetchError,
    UnsafeTargetError,
)
from civil_service.urls import host_port, is_ip_literal, normalize_url, validate_public_host

_LOGGER = structlog.get_logger(__name__)
_MS_PER_SECOND = 1000
_MIN_STATUS_PARTS = 2
_PRIVATE_FILE_MODE = 0o600
_HIDDEN_ENV_PREFIX = "CIVIL_SERVICE_SPN_"  # variables that child processes must not see
_RATE_LIMIT_STATUSES = frozenset({HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE})
_REDIRECT_STATUSES = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
)
_GET_REDIRECT_STATUSES = frozenset(
    {HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND, HTTPStatus.SEE_OTHER}
)

type TransportFn = Callable[["Request"], "FetchResult"]


@dataclass(slots=True, frozen=True)
class Request:
    """One HTTP request. Callers fill the first fields. The fetcher fills pin_ip and max_bytes."""

    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    max_bytes: int = 0  # 0 means use settings.max_body_bytes
    follow_redirects: bool = True
    escalate_on_403: bool = True
    pin_ips: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class FetchResult:
    """Normalized HTTP response payload from any transport."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    content_type: str
    final_url: str = ""
    truncated: bool = False

    @property
    def text(self) -> str:
        """Decode body bytes as UTF-8 replacing invalid sequences."""
        return self.body.decode("utf-8", errors="replace")


def parse_retry_after(
    value: str | None,
    max_wait: float,
    now: datetime | None = None,
) -> float | None:
    """Parse an HTTP Retry-After header into bounded delay seconds.

    Args:
        value: Raw Retry-After header value (seconds or HTTP-date).
        max_wait: Maximum wait duration allowed by configuration.
        now: Optional UTC reference timestamp for deterministic testing.

    Returns:
        Delay in seconds clamped to [0.0, max_wait], or None if unparsable.
    """
    if not value:
        return None
    stripped = value.strip()
    if stripped.isascii() and stripped.isdigit():
        return min(float(stripped), max_wait)
    try:
        target = parsedate_to_datetime(stripped)
    except (ValueError, TypeError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    return max(0.0, min((target - current).total_seconds(), max_wait))


def _parse_curl_headers(raw_headers: str) -> tuple[int, dict[str, str]]:
    """Extract final HTTP status code and lowercase headers from curl dump."""
    blocks = [b for b in raw_headers.replace("\r\n", "\n").strip().split("\n\n") if b.strip()]
    if not blocks:
        msg = "Empty HTTP header output from curl"
        raise FetchError(msg)
    lines = blocks[-1].splitlines()
    status_parts = lines[0].split()
    if len(status_parts) < _MIN_STATUS_PARTS or not status_parts[1].isdigit():
        msg = f"Invalid HTTP status line from curl: {lines[0]}"
        raise FetchError(msg)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()
    return int(status_parts[1]), headers


def _read_capped(chunks: Iterable[bytes], max_bytes: int) -> tuple[bytes, bool]:
    """Join chunks up to max_bytes. Return the bytes and whether more data was cut."""
    buffer = bytearray()
    for chunk in chunks:
        buffer += chunk
        if len(buffer) > max_bytes:
            return bytes(buffer[:max_bytes]), True
    return bytes(buffer), False


def _curl_command(req: Request, user_agent: str, timeout: float, tmp: Path) -> list[str]:
    """Build the curl argument list. Secrets and bodies go in files, never in argv."""
    cmd = [
        "curl",
        "-q",  # first argument: ignore ~/.curlrc, which could follow redirects or skip TLS checks
        "-sS",
        "--noproxy",
        "*",  # connect directly, so the address check and the pinned address stay valid
        "--max-time",
        str(timeout),
        "-A",
        user_agent,
        "-X",
        req.method,
        "-D",
        str(tmp / "headers.txt"),
        "-o",
        "-",
    ]
    if req.headers:
        header_file = tmp / "request-headers.txt"
        header_file.write_text("".join(f"{k}: {v}\n" for k, v in req.headers.items()))
        header_file.chmod(_PRIVATE_FILE_MODE)
        cmd += ["-H", f"@{header_file}"]
    if req.body is not None:
        body_file = tmp / "request-body.bin"
        body_file.write_bytes(req.body)
        body_file.chmod(_PRIVATE_FILE_MODE)
        cmd += ["--data-binary", f"@{body_file}"]
    host, port = host_port(req.url)
    if req.pin_ips and not is_ip_literal(host):
        cmd += ["--resolve", f"{host}:{port}:{','.join(req.pin_ips)}"]
    return [*cmd, "--", req.url]


def _child_environment() -> dict[str, str]:
    """Return the process environment without the archive.org API keys, for child processes."""
    return {k: v for k, v in os.environ.items() if not k.startswith(_HIDDEN_ENV_PREFIX)}


def _run_curl(cmd: list[str], max_bytes: int) -> tuple[bytes, bool, int, str]:
    """Run curl and read at most max_bytes of body. Kill curl when the body is larger."""
    try:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_environment(),
            start_new_session=True,  # a terminal Ctrl-C must not kill a request in flight
        )
    except OSError as exc:
        msg = "curl could not start"
        raise FetchError(msg) from exc
    with proc:
        body = proc.stdout.read(max_bytes + 1)  # type: ignore[union-attr]
        truncated = len(body) > max_bytes
        if truncated:
            proc.kill()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")  # type: ignore[union-attr]
        returncode = proc.wait()
    return body[:max_bytes], truncated, returncode, stderr.strip()


def fetch_with_curl(req: Request, user_agent: str, timeout: float) -> FetchResult:
    """Execute an HTTP request using the system curl CLI without a shell."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cmd = _curl_command(req, user_agent, timeout, tmp)
        body, truncated, returncode, stderr = _run_curl(cmd, req.max_bytes)
        if returncode != 0 and not truncated:
            msg = f"curl exit {returncode} for {req.url}: {stderr}"
            raise TransientFetchError(msg)
        status_code, headers = _parse_curl_headers(
            (tmp / "headers.txt").read_text(encoding="latin-1")
        )
    return FetchResult(
        status_code=status_code,
        headers=headers,
        body=body,
        content_type=headers.get("content-type", ""),
        truncated=truncated,
    )


def fetch_with_curl_cffi(
    req: Request,
    user_agent: str,
    timeout: float,
    impersonate: str,
) -> FetchResult:
    """Execute an HTTP request using curl_cffi with browser TLS impersonation."""
    options: dict[CurlOpt, list[str]] = {}
    host, port = host_port(req.url)
    if req.pin_ips and not is_ip_literal(host):
        options[CurlOpt.RESOLVE] = [f"{host}:{port}:{','.join(req.pin_ips)}"]
    try:
        with curl_requests.Session(trust_env=False, curl_options=options) as session:  # no proxy
            resp = session.request(
                method=req.method,
                url=req.url,
                headers={"User-Agent": user_agent, **req.headers},
                data=req.body,
                impersonate=impersonate,
                timeout=timeout,
                verify=True,
                allow_redirects=False,
                stream=True,
            )
            try:
                body, truncated = _read_capped(resp.iter_content(), req.max_bytes)
            finally:
                resp.close()
    except Exception as exc:
        msg = f"curl_cffi failed for {req.url}"
        raise TransientFetchError(msg) from exc

    headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
    return FetchResult(
        status_code=int(resp.status_code),
        headers=headers,
        body=body,
        content_type=headers.get("content-type", ""),
        truncated=truncated,
    )


def _guard_browser_route(route: object) -> None:
    """Abort browser requests that resolve to non-public IP addresses."""
    req = getattr(route, "request", None)
    url = str(getattr(req, "url", ""))
    try:
        validate_public_host(url)
    except (UnsafeTargetError, TransientFetchError):
        route.abort()  # type: ignore[attr-defined]
        return
    route.continue_()  # type: ignore[attr-defined]


def _validate_redirect_chain(response: object) -> None:
    """Raise if the browser followed a redirect to a non-public address.

    Playwright calls the route hook only for the first request of a redirect chain, so the
    hops are checked here, after the fact. The page content is dropped, but the browser has
    already sent a GET to such an address. Remove camoufox from `fallbacks` to rule that out.
    """
    request = getattr(response, "request", None)
    while request is not None:
        validate_public_host(str(request.url))
        request = request.redirected_from


class CamoufoxPool:
    """Lazy single-instance Camoufox browser pinned to one dedicated thread."""

    def __init__(self, user_agent: str, timeout: float) -> None:
        """Store browser launch options without starting Camoufox eagerly."""
        self._user_agent = user_agent
        self._timeout_ms = int(timeout * _MS_PER_SECOND)
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._cm: Camoufox | None = None
        self._browser: object | None = None

    def _ensure_browser(self) -> object:
        if self._browser is None:
            cm = Camoufox(headless=True, i_know_what_im_doing=True)  # type: ignore[no-untyped-call]
            browser = cm.__enter__()
            self._cm = cm
            self._browser = browser
        return self._browser

    def _read_page(self, page: object, req: Request) -> FetchResult:
        """Load req in the browser. A request with headers or a body uses the API client."""
        page.route("**/*", _guard_browser_route)  # type: ignore[attr-defined]
        final_url = ""
        if req.method.upper() == "GET" and not req.headers:
            response = page.goto(  # type: ignore[attr-defined]
                req.url, timeout=self._timeout_ms, wait_until="domcontentloaded"
            )
            if response is None:
                msg = f"Camoufox received empty response for {req.url}"
                raise FetchError(msg)
            headers = {str(k).lower(): str(v) for k, v in response.all_headers().items()}
            final_url = str(response.url)
            _validate_redirect_chain(response)
            if "html" in headers.get("content-type", "").lower():
                raw = str(page.content()).encode("utf-8")  # type: ignore[attr-defined]
            else:
                raw = bytes(response.body())
        else:
            response = page.request.fetch(  # type: ignore[attr-defined]
                req.url,
                method=req.method,
                headers=dict(req.headers),
                data=req.body,
                timeout=self._timeout_ms,
                max_redirects=0,
            )
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            raw = bytes(response.body())
        return FetchResult(
            status_code=int(response.status),
            headers=headers,
            body=raw[: req.max_bytes],
            content_type=headers.get("content-type", ""),
            final_url=final_url,
            truncated=len(raw) > req.max_bytes,
        )

    def _discard_browser(self) -> None:
        """Forget a browser that failed, so the next fetch starts a fresh one."""
        cm, self._cm, self._browser = self._cm, None, None
        if cm is not None:
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

    def _fetch_on_thread(self, req: Request) -> FetchResult:
        try:
            browser = self._ensure_browser()
            page = browser.new_page(user_agent=self._user_agent)  # type: ignore[attr-defined]
        except Exception as exc:
            self._discard_browser()
            msg = "Camoufox could not start. Run: uv run python -m camoufox fetch"
            raise FetchError(msg) from exc
        try:
            return self._read_page(page, req)
        except (FetchError, UnsafeTargetError):
            raise
        except Exception as exc:
            msg = f"Camoufox failed for {req.url}"
            raise TransientFetchError(msg) from exc
        finally:
            page.close()

    def fetch(self, req: Request) -> FetchResult:
        """Fetch req inside the single dedicated Camoufox browser thread."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1)
            executor = self._executor
        return executor.submit(self._fetch_on_thread, req).result()

    def _close_on_thread(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None
            self._browser = None

    def close(self) -> None:
        """Close the shared Camoufox browser instance if it was started."""
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.submit(self._close_on_thread).result()
            executor.shutdown(wait=True)


class TieredFetcher:
    """Retry and fallback coordinator across curl, curl_cffi, and Camoufox.

    Policy:
        * HTTP 429 and 503: retry the same transport with backoff, then raise RateLimitError.
          A pause for the host holds back every thread. The next transport shares the same IP
          address, so it gets no turn.
        * HTTP 403 and other 5xx, and any transport error: move to the next transport at once.
        * Every other status returns to the caller.

    A redirect to another host, or from https to http, drops the request headers, so credentials
    never leave the origin that the caller named.
    """

    def __init__(
        self,
        settings: AppSettings,
        transports: dict[str, TransportFn] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize fallback chain from settings.

        Without a sleep_fn, waits use interruptible_sleep, so a stop request ends them at once.
        """
        self._settings = settings
        self._stop = threading.Event()
        self._sleep = sleep_fn or self.interruptible_sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._resume_at: dict[str, float] = {}
        self._camoufox_pool = CamoufoxPool(
            user_agent=settings.user_agent,
            timeout=settings.request_timeout_seconds,
        )
        default_transports: dict[str, TransportFn] = {
            "curl": lambda r: fetch_with_curl(
                r, settings.user_agent, settings.request_timeout_seconds
            ),
            "curl_cffi": lambda r: fetch_with_curl_cffi(
                r,
                settings.user_agent,
                settings.request_timeout_seconds,
                settings.curl_cffi_impersonate,
            ),
            "camoufox": self._camoufox_pool.fetch,
        }
        self._transports = transports if transports is not None else default_transports

    def request_stop(self) -> None:
        """Make every wait end now and make later waits raise StopRequested."""
        self._stop.set()

    def interruptible_sleep(self, seconds: float) -> None:
        """Sleep for seconds. Raise StopRequested if a stop request arrives first."""
        if self._stop.wait(seconds):
            msg = "Stop requested"
            raise StopRequested(msg)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            msg = "Stop requested"
            raise StopRequested(msg)

    def _wait_for_host(self, host: str) -> None:
        with self._lock:
            delay = self._resume_at.get(host, 0.0) - self._clock()
        if delay > 0:
            self._sleep(delay)

    def _pause_host(self, host: str, delay: float) -> None:
        with self._lock:
            self._resume_at[host] = max(self._resume_at.get(host, 0.0), self._clock() + delay)

    def _retry_delay(self, headers: dict[str, str], attempt: int) -> float:
        parsed = parse_retry_after(
            headers.get("retry-after"), self._settings.max_retry_after_seconds
        )
        if parsed is not None:
            return parsed
        return self._settings.retry_delays_seconds[attempt]

    def _run_with_retries(self, name: str, transport: TransportFn, req: Request) -> FetchResult:
        host, _port = host_port(req.url)
        delays = self._settings.retry_delays_seconds
        for attempt in range(len(delays) + 1):
            self._check_stop()
            self._wait_for_host(host)
            res = transport(req)
            if res.status_code in _RATE_LIMIT_STATUSES:
                if attempt >= len(delays):
                    msg = f"{name} hit HTTP {res.status_code} for {req.url}"
                    raise RateLimitError(msg)
                self._pause_host(host, self._retry_delay(res.headers, attempt))
                continue
            if res.status_code == HTTPStatus.FORBIDDEN:
                msg = f"{name} returned HTTP 403 for {req.url}"
                raise BlockedError(msg)
            if res.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
                msg = f"{name} returned HTTP {res.status_code} for {req.url}"
                raise TransientFetchError(msg)
            return res
        msg = f"{name} exhausted retries for {req.url}"  # pragma: no cover
        raise FetchError(msg)  # pragma: no cover

    def _fetch_single(self, req: Request) -> FetchResult:
        """Fetch one URL across configured fallbacks without following redirects."""
        errors: list[FetchError] = []
        for name in self._settings.fallbacks:
            try:
                return self._run_with_retries(name, self._transports[name], req)
            except RateLimitError:
                raise
            except BlockedError as exc:
                if not req.escalate_on_403:
                    raise
                errors.append(exc)
            except FetchError as exc:
                errors.append(exc)
        raise next((e for e in errors if isinstance(e, TransientFetchError)), errors[-1])

    def fetch(self, request: Request) -> FetchResult:
        """Fetch a URL across fallbacks. Validate every redirect target against SSRF.

        Args:
            request: URL, method, headers, body, and policy flags.

        Returns:
            The final response. Its final_url holds the last URL fetched.

        Raises:
            RateLimitError: If the host keeps answering 429 or 503.
            BlockedError: If the server answers 403 and no transport succeeds.
            TransientFetchError: If a timeout, network error, or 5xx exhausts every transport.
            FetchError: If redirects exceed the limit or no transport works.
            UnsafeTargetError: If the URL or a redirect target is not a public host.
        """
        current = request
        allowed_schemes = tuple(self._settings.allowed_schemes)
        max_bytes = request.max_bytes or self._settings.max_body_bytes
        for hop in range(self._settings.max_redirects + 1):
            self._check_stop()
            pin_ips = validate_public_host(current.url)
            res = self._fetch_single(replace(current, pin_ips=pin_ips, max_bytes=max_bytes))
            if res.truncated:
                _LOGGER.warning("body_capped", url=current.url, limit=max_bytes)
            location = res.headers.get("location")
            if (
                not request.follow_redirects
                or res.status_code not in _REDIRECT_STATUSES
                or not location
            ):
                return replace(res, final_url=res.final_url or current.url)
            if hop >= self._settings.max_redirects:
                msg = f"Too many redirects for {request.url}"
                raise FetchError(msg)
            target = normalize_url(current.url, location, allowed_schemes)
            if target is None:
                msg = f"Disallowed redirect target {location} from {current.url}"
                raise UnsafeTargetError(msg)
            to_get = res.status_code in _GET_REDIRECT_STATUSES
            same_origin = urlsplit(target)[:2] == urlsplit(current.url)[:2]
            current = replace(
                current,
                url=target,
                method="GET" if to_get else current.method,
                body=None if to_get else current.body,
                headers=current.headers if same_origin else {},
            )
        msg = f"Too many redirects for {request.url}"  # pragma: no cover
        raise FetchError(msg)  # pragma: no cover

    def close(self) -> None:
        """Release any open Camoufox browser resources."""
        self._camoufox_pool.close()
