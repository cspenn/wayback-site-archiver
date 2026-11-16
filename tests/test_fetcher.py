import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from civil_service.config import AppSettings
from civil_service.exceptions import (
    BlockedError,
    FetchError,
    RateLimitError,
    StopRequested,
    TransientFetchError,
    UnsafeTargetError,
)
from civil_service.fetcher import (
    CamoufoxPool,
    FetchResult,
    Request,
    TieredFetcher,
    _curl_command,
    _parse_curl_headers,
    _read_capped,
    fetch_with_curl,
    fetch_with_curl_cffi,
    parse_retry_after,
)

URL = "https://example.com/"


def ok(body: bytes = b"ok") -> FetchResult:
    return FetchResult(200, {}, body, "text/html")


def make_fetcher(
    transports: dict[str, object], slept: list[float] | None = None, **settings: object
) -> TieredFetcher:
    return TieredFetcher(
        AppSettings(fallbacks=list(transports), **settings),  # type: ignore[arg-type]
        transports=transports,  # type: ignore[arg-type]
        sleep_fn=(slept if slept is not None else []).append,
        clock=lambda: 0.0,
    )


def test_parse_retry_after_seconds_http_date_and_cap() -> None:
    now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after(None, max_wait=300.0, now=now) is None
    assert parse_retry_after("12", max_wait=300.0, now=now) == 12.0
    assert parse_retry_after("9999", max_wait=300.0, now=now) == 300.0
    future = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert parse_retry_after(future, max_wait=300.0, now=now) == 45.0
    naive_past = "Sun, 15 Nov 2026 11:59:00"
    assert parse_retry_after(naive_past, max_wait=300.0) == 0.0
    for invalid in ("garbage", "-5", "12.5", "²"):
        assert parse_retry_after(invalid, max_wait=300.0, now=now) is None


def test_rate_limit_retries_5_10_20_then_raises_without_trying_next_transport() -> None:
    calls: list[str] = []
    slept: list[float] = []

    def limited(_req: Request) -> FetchResult:
        calls.append("curl")
        return FetchResult(429, {}, b"slow down", "text/plain")

    def never(_req: Request) -> FetchResult:
        calls.append("curl_cffi")
        return ok()

    fetcher = make_fetcher({"curl": limited, "curl_cffi": never}, slept)
    with pytest.raises(RateLimitError):
        fetcher.fetch(Request(URL))
    assert slept == [5.0, 10.0, 20.0]
    assert calls == ["curl"] * 4
    assert issubclass(RateLimitError, TransientFetchError)


def test_rate_limit_then_success_and_retry_after_header() -> None:
    slept: list[float] = []
    answers = iter([FetchResult(503, {"retry-after": "2"}, b"", ""), ok(b"fine")])
    fetcher = make_fetcher({"curl": lambda _r: next(answers)}, slept)
    assert fetcher.fetch(Request(URL)).text == "fine"
    assert slept == [2.0]

    slept_exhausted: list[float] = []
    always = make_fetcher(
        {"curl": lambda _r: FetchResult(429, {"retry-after": "2"}, b"", "")},
        slept_exhausted,
        retry_delays_seconds=[5.0, 10.0],
    )
    with pytest.raises(RateLimitError):
        always.fetch(Request(URL))
    assert slept_exhausted == [2.0, 2.0]


def test_host_pause_holds_back_later_requests_to_the_same_host_only() -> None:
    slept: list[float] = []
    answers = iter([FetchResult(429, {"retry-after": "7"}, b"", ""), ok(), ok(), ok()])
    fetcher = make_fetcher({"curl": lambda _r: next(answers)}, slept)
    fetcher.fetch(Request("https://example.com/a"))
    assert slept == [7.0]
    fetcher.fetch(Request("https://example.com/b"))
    assert slept == [7.0, 7.0]
    fetcher.fetch(Request("https://other.org/"))
    assert slept == [7.0, 7.0]


def test_403_and_5xx_and_transport_errors_move_to_next_transport_without_sleep() -> None:
    slept: list[float] = []
    calls: list[str] = []

    def forbidden(_req: Request) -> FetchResult:
        calls.append("curl")
        return FetchResult(403, {}, b"denied", "text/html")

    def server_error(_req: Request) -> FetchResult:
        calls.append("curl_cffi")
        return FetchResult(500, {}, b"oops", "text/html")

    def browser(_req: Request) -> FetchResult:
        calls.append("camoufox")
        return ok(b"escalated")

    chain = {"curl": forbidden, "curl_cffi": server_error, "camoufox": browser}
    assert make_fetcher(chain, slept).fetch(Request(URL)).text == "escalated"
    assert calls == ["curl", "curl_cffi", "camoufox"]

    def broken(_req: Request) -> FetchResult:
        raise TransientFetchError("network-down")

    assert (
        make_fetcher({"curl": broken, "curl_cffi": browser}, slept).fetch(Request(URL)).text
        == "escalated"
    )
    assert slept == []


def test_all_transports_failing_raises_the_most_useful_error() -> None:
    def blocked(_req: Request) -> FetchResult:
        return FetchResult(403, {}, b"", "")

    def missing_binary(_req: Request) -> FetchResult:
        raise FetchError("curl-missing")

    def timeout(_req: Request) -> FetchResult:
        raise TransientFetchError("timeout")

    with pytest.raises(BlockedError):
        make_fetcher({"curl": blocked, "curl_cffi": blocked}).fetch(Request(URL))
    with pytest.raises(FetchError, match="curl-missing"):
        make_fetcher({"curl": blocked, "curl_cffi": missing_binary}).fetch(Request(URL))
    with pytest.raises(TransientFetchError):
        make_fetcher({"curl": missing_binary, "curl_cffi": timeout}).fetch(Request(URL))
    with pytest.raises(FetchError) as info:
        make_fetcher({"curl": missing_binary}).fetch(Request(URL))
    assert not isinstance(info.value, TransientFetchError)


def test_403_fails_at_once_when_escalation_is_off() -> None:
    calls: list[str] = []

    def forbidden(_req: Request) -> FetchResult:
        calls.append("curl")
        return FetchResult(403, {}, b"", "")

    def never(_req: Request) -> FetchResult:
        calls.append("curl_cffi")
        return ok()

    fetcher = make_fetcher({"curl": forbidden, "curl_cffi": never})
    with pytest.raises(BlockedError):
        fetcher.fetch(Request(URL, escalate_on_403=False))
    assert calls == ["curl"]


def test_fetch_pins_validated_ip_and_applies_body_cap_default_and_override() -> None:
    seen: list[Request] = []

    def spy(req: Request) -> FetchResult:
        seen.append(req)
        return ok()

    fetcher = make_fetcher({"curl": spy}, max_body_bytes=1234)
    fetcher.fetch(Request(URL))
    fetcher.fetch(Request(URL, max_bytes=77))
    assert [(r.pin_ips, r.max_bytes) for r in seen] == [
        (("93.184.216.34",), 1234),
        (("93.184.216.34",), 77),
    ]
    fetcher.fetch(Request("https://dual.example/"))
    assert seen[-1].pin_ips == ("2606:2800:220:1::1946", "93.184.216.34")


def test_truncated_bodies_are_logged() -> None:
    cut = FetchResult(200, {}, b"abc", "text/html", truncated=True)
    fetcher = make_fetcher({"curl": lambda _r: cut})
    with structlog.testing.capture_logs() as logs:
        assert fetcher.fetch(Request(URL)).truncated is True
    assert [e["event"] for e in logs] == ["body_capped"]


def test_redirects_validate_targets_limit_hops_and_switch_method() -> None:
    visited: list[Request] = []

    def ssrf(req: Request) -> FetchResult:
        visited.append(req)
        return FetchResult(302, {"location": "http://meta.internal/latest/"}, b"", "")

    with pytest.raises(UnsafeTargetError):
        make_fetcher({"curl": ssrf}).fetch(Request("https://example.com/redir"))
    assert [r.url for r in visited] == ["https://example.com/redir"]

    def loop(req: Request) -> FetchResult:
        step = "step2" if "step1" in req.url else "step1"
        return FetchResult(301, {"location": f"https://example.com/{step}"}, b"", "")

    with pytest.raises(FetchError):
        make_fetcher({"curl": loop}, max_redirects=1).fetch(Request("https://example.com/step1"))

    def one_hop(req: Request) -> FetchResult:
        if req.url == "https://example.com/start":
            return FetchResult(302, {"location": "/final"}, b"", "")
        return ok(b"final")

    res = make_fetcher({"curl": one_hop}, max_redirects=2).fetch(
        Request("https://example.com/start")
    )
    assert (res.text, res.final_url) == ("final", "https://example.com/final")

    def bad_scheme(_req: Request) -> FetchResult:
        return FetchResult(302, {"location": "ftp://example.com/file"}, b"", "")

    with pytest.raises(UnsafeTargetError):
        make_fetcher({"curl": bad_scheme}).fetch(Request("https://example.com/start"))


def test_redirect_edge_cases_and_post_method_rules() -> None:
    def redirect_without_location(_req: Request) -> FetchResult:
        return FetchResult(302, {}, b"", "")

    res = make_fetcher({"curl": redirect_without_location}).fetch(Request(URL))
    assert res.status_code == 302

    def redirect(_req: Request) -> FetchResult:
        return FetchResult(302, {"location": "/next"}, b"", "")

    stopped = make_fetcher({"curl": redirect}).fetch(Request(URL, follow_redirects=False))
    assert stopped.status_code == 302

    seen: list[Request] = []

    def post_redirects(req: Request) -> FetchResult:
        seen.append(req)
        codes = {"https://example.com/see": 303, "https://example.com/keep": 307}
        if req.url in codes:
            return FetchResult(codes[req.url], {"location": "/done"}, b"", "")
        return ok()

    fetcher = make_fetcher({"curl": post_redirects})
    fetcher.fetch(Request("https://example.com/see", method="POST", body=b"a=1"))
    fetcher.fetch(Request("https://example.com/keep", method="POST", body=b"a=1"))
    final_see, final_keep = seen[1], seen[3]
    assert (final_see.method, final_see.body) == ("GET", None)
    assert (final_keep.method, final_keep.body) == ("POST", b"a=1")


def test_redirect_that_downgrades_https_to_http_drops_request_headers() -> None:
    seen: list[Request] = []

    def hop(req: Request) -> FetchResult:
        seen.append(req)
        if req.url == "https://a.example/":
            return FetchResult(302, {"location": "http://a.example/plain"}, b"", "")
        return ok()

    make_fetcher({"curl": hop}).fetch(Request("https://a.example/", headers={"X-Key": "v"}))
    assert [dict(r.headers) for r in seen] == [{"X-Key": "v"}, {}]


def test_redirect_to_another_host_drops_request_headers() -> None:
    seen: list[Request] = []

    def hop(req: Request) -> FetchResult:
        seen.append(req)
        targets = {
            "https://a.example/": "https://evil.example/x",
            "https://b.example/": "/next",
        }
        if req.url in targets:
            return FetchResult(302, {"location": targets[req.url]}, b"", "")
        return ok()

    fetcher = make_fetcher({"curl": hop})
    secret = {"Authorization": "LOW k:s"}
    fetcher.fetch(Request("https://a.example/", headers=secret))
    fetcher.fetch(Request("https://b.example/", headers=secret))
    assert [dict(r.headers) for r in seen] == [secret, {}, secret, secret]


def test_parse_curl_headers_handles_redirect_blocks_and_bad_output() -> None:
    raw = (
        "HTTP/1.1 301 Moved\r\nLocation: /final\r\n\r\n"
        "HTTP/2 200 \r\nMalformedLine\r\nContent-Type: text/html\r\nX-Test: 1\r\n\r\n"
    )
    status, headers = _parse_curl_headers(raw)
    assert (status, headers["content-type"], headers["x-test"]) == (200, "text/html", "1")
    with pytest.raises(FetchError):
        _parse_curl_headers("   ")
    with pytest.raises(FetchError):
        _parse_curl_headers("NOTHTTP\r\n\r\n")


def test_read_capped_cuts_at_the_limit() -> None:
    assert _read_capped([b"ab", b"cd"], 10) == (b"abcd", False)
    assert _read_capped([b"ab", b"cd"], 4) == (b"abcd", False)
    assert _read_capped([b"ab", b"cdef", b"gh"], 5) == (b"abcde", True)


def test_curl_transport_against_a_real_server(local_server: str) -> None:
    res = fetch_with_curl(Request(f"{local_server}/html", max_bytes=1_000), "UA", 10.0)
    assert (res.status_code, res.content_type, res.body) == (
        200,
        "text/html",
        b"<html>hello</html>",
    )
    assert res.truncated is False
    missing = fetch_with_curl(Request(f"{local_server}/nope", max_bytes=1_000), "UA", 10.0)
    assert (missing.status_code, missing.body) == (404, b"no")


@pytest.mark.parametrize("path", ["/big", "/chunked"])
def test_curl_transport_caps_large_and_chunked_bodies_and_keeps_headers(
    local_server: str, path: str
) -> None:
    for _ in range(3):
        res = fetch_with_curl(Request(f"{local_server}{path}", max_bytes=1_000), "UA", 10.0)
        assert res.status_code == 200
        assert len(res.body) == 1_000
        assert res.truncated is True
        assert res.headers["content-type"] in {"text/html", "text/plain"}


def test_curl_command_pins_names_but_not_ip_literals(tmp_path: Path) -> None:
    named = _curl_command(
        Request("http://pinned.test:81/", pin_ips=("::1", "10.0.0.1")), "UA", 5.0, tmp_path
    )
    assert named[named.index("--resolve") + 1] == "pinned.test:81:::1,10.0.0.1"
    for literal in ("http://[::1]:81/", "http://127.0.0.1:81/"):
        assert "--resolve" not in _curl_command(
            Request(literal, pin_ips=("::1",)), "UA", 5.0, tmp_path
        )


def test_curl_transport_sends_secrets_through_files_and_pins_the_address(
    local_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    real_popen = subprocess.Popen

    modes: list[int] = []
    environments: list[dict[str, str]] = []
    monkeypatch.setenv("CIVIL_SERVICE_SPN_SECRET_KEY", "child-must-not-see-this")

    def spy_popen(cmd: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        commands.append(cmd)
        assert kwargs["start_new_session"] is True  # a terminal Ctrl-C must not reach curl
        environments.append(dict(kwargs["env"]))  # type: ignore[call-overload]
        modes.extend(os.stat(arg[1:]).st_mode & 0o777 for arg in cmd if arg.startswith("@"))
        return real_popen(cmd, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    port = local_server.rsplit(":", 1)[1]
    req = Request(
        f"http://pinned.test:{port}/echo",
        method="POST",
        headers={"X-Secret": "hunter2"},
        body=b"url=abc",
        max_bytes=1_000,
        pin_ips=("::1", "127.0.0.1"),
    )
    res = fetch_with_curl(req, "UA", 10.0)
    assert (res.status_code, res.body) == (201, b"hunter2|url=abc")
    argv = " ".join(commands[0])
    assert "hunter2" not in argv
    assert "--resolve" in argv
    assert modes == [0o600, 0o600]
    assert not {"-k", "--insecure"} & set(commands[0])
    assert commands[0][:2] == ["curl", "-q"]
    assert commands[0][commands[0].index("--noproxy") : commands[0].index("--noproxy") + 2] == [
        "--noproxy",
        "*",
    ]
    assert environments
    assert not any(key.startswith("CIVIL_SERVICE_SPN_") for key in environments[0])
    assert f"pinned.test:{port}:::1,127.0.0.1" in argv


def test_curl_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TransientFetchError):
        fetch_with_curl(Request("http://127.0.0.1:1/", max_bytes=10), "UA", 5.0)

    def no_binary(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise FileNotFoundError("curl")

    monkeypatch.setattr(subprocess, "Popen", no_binary)
    with pytest.raises(FetchError) as info:
        fetch_with_curl(Request(URL, max_bytes=10), "UA", 5.0)
    assert not isinstance(info.value, TransientFetchError)


class FakeSession:
    """Stand-in for curl_cffi.requests.Session with the same parameter names as the real one.

    A keyword that the real class does not accept raises TypeError here too.
    """

    init_kwargs: dict[str, object] = {}
    request_kwargs: dict[str, object] = {}
    chunks: list[bytes] = [b'{"ok":', b" true}"]
    stream_error: Exception | None = None
    request_error: Exception | None = None
    closed = 0

    def __init__(
        self, *, trust_env: bool = True, curl_options: dict[object, object] | None = None
    ) -> None:
        FakeSession.init_kwargs = {"trust_env": trust_env, "curl_options": curl_options}

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def request(  # noqa: PLR0913
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: bytes | None,
        impersonate: str,
        timeout: float,
        verify: bool,
        allow_redirects: bool,
        stream: bool,
    ) -> SimpleNamespace:
        FakeSession.request_kwargs = {
            "method": method,
            "url": url,
            "headers": headers,
            "data": data,
            "impersonate": impersonate,
            "timeout": timeout,
            "verify": verify,
            "allow_redirects": allow_redirects,
            "stream": stream,
        }
        if FakeSession.request_error is not None:
            raise FakeSession.request_error

        def iterate() -> object:
            if FakeSession.stream_error is not None:
                raise FakeSession.stream_error
            return iter(FakeSession.chunks)

        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            iter_content=iterate,
            close=lambda: setattr(FakeSession, "closed", FakeSession.closed + 1),
        )


def test_curl_cffi_transport_pins_caps_and_wraps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import curl_cffi.requests as curl_requests
    from curl_cffi import CurlOpt

    monkeypatch.setattr(curl_requests, "Session", FakeSession)
    FakeSession.closed = 0
    FakeSession.stream_error = FakeSession.request_error = None
    req = Request(
        "https://example.com:8443/x",
        method="POST",
        headers={"Authorization": "LOW a:b"},
        body=b"url=1",
        max_bytes=100,
        pin_ips=("93.184.216.34", "2606:2800:220:1::1946"),
    )
    res = fetch_with_curl_cffi(req, "Wayback Archiving", 10.0, impersonate="chrome")
    assert (res.status_code, res.content_type, res.body) == (
        200,
        "application/json",
        b'{"ok": true}',
    )
    assert res.truncated is False
    assert FakeSession.init_kwargs == {
        "trust_env": False,
        "curl_options": {CurlOpt.RESOLVE: ["example.com:8443:93.184.216.34,2606:2800:220:1::1946"]},
    }
    kwargs = FakeSession.request_kwargs
    assert kwargs["impersonate"] == "chrome"
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {"User-Agent": "Wayback Archiving", "Authorization": "LOW a:b"}
    assert kwargs["data"] == b"url=1"

    FakeSession.chunks = [b"abcdef"]
    capped = fetch_with_curl_cffi(Request(URL, max_bytes=3), "UA", 10.0, impersonate="chrome")
    assert (capped.body, capped.truncated) == (b"abc", True)
    assert FakeSession.closed == 2

    FakeSession.stream_error = RuntimeError("reset-mid-stream")
    with pytest.raises(TransientFetchError):
        fetch_with_curl_cffi(Request(URL, max_bytes=3), "UA", 10.0, impersonate="chrome")
    assert FakeSession.closed == 3

    FakeSession.request_error = RuntimeError("tls-error")
    with pytest.raises(TransientFetchError):
        fetch_with_curl_cffi(Request(URL, max_bytes=3), "UA", 10.0, impersonate="chrome")
    FakeSession.chunks = [b'{"ok":', b" true}"]
    FakeSession.stream_error = FakeSession.request_error = None


class FakePage:
    closed = 0
    api_kwargs: dict[str, object] = {}

    def __init__(self) -> None:
        self.route_handler: object = None
        self.request = SimpleNamespace(fetch=self._api_fetch)

    @staticmethod
    def _api_fetch(_url: str, **kwargs: object) -> SimpleNamespace:
        FakePage.api_kwargs = kwargs
        return SimpleNamespace(
            status=201, headers={"content-type": "text/plain"}, body=lambda: b"posted-body"
        )

    def route(self, _pattern: str, handler: object) -> None:
        self.route_handler = handler

    def _probe_route_guard(self) -> None:
        events: list[str] = []
        for target in (
            "https://example.com/",
            "http://127.0.0.1/admin",
            "http://dns-flaky.invalid/",
        ):
            self.route_handler(  # type: ignore[operator]
                SimpleNamespace(
                    request=SimpleNamespace(url=target),
                    abort=lambda: events.append("abort"),
                    continue_=lambda: events.append("continue"),
                )
            )
        assert events == ["continue", "abort", "abort"]

    def goto(self, url: str, **_kw: object) -> SimpleNamespace | None:
        self._probe_route_guard()
        if "boom" in url:
            raise RuntimeError("page-crash")
        if "none" in url:
            return None
        content_type = "application/json" if "json" in url else "text/html"
        chain = SimpleNamespace(url="https://example.com/landing", redirected_from=None)
        if "internal-redirect" in url:  # the browser followed a redirect to a private address
            first = SimpleNamespace(url="http://meta.internal/latest", redirected_from=None)
            chain = SimpleNamespace(url="https://example.com/landing", redirected_from=first)
        return SimpleNamespace(
            status=200,
            request=chain,
            url="https://example.com/landing",
            all_headers=lambda: {"content-type": content_type},
            body=lambda: b'{"raw": true}',
        )

    def content(self) -> str:
        return "<html>camoufox</html>"

    def close(self) -> None:
        FakePage.closed += 1


class FakeBrowser:
    fail_new_page = False

    def new_page(self, **_kw: object) -> FakePage:
        if FakeBrowser.fail_new_page:
            raise RuntimeError("browser-crashed")
        return FakePage()


class FakeCamoufox:
    last_kwargs: dict[str, object] = {}
    entered = 0
    exited = 0

    def __init__(self, **kw: object) -> None:
        FakeCamoufox.last_kwargs = kw

    def __enter__(self) -> FakeBrowser:
        FakeCamoufox.entered += 1
        return FakeBrowser()

    def __exit__(self, *_args: object) -> None:
        FakeCamoufox.exited += 1


class BrokenCamoufox(FakeCamoufox):
    def __enter__(self) -> FakeBrowser:
        raise RuntimeError("launch-error")


def test_camoufox_pool_lazy_browser_get_post_caps_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import civil_service.fetcher as fetcher_mod

    FakePage.closed = FakeCamoufox.entered = FakeCamoufox.exited = 0
    monkeypatch.setattr(fetcher_mod, "Camoufox", FakeCamoufox)
    pool = CamoufoxPool("Wayback Archiving", 10.0)
    res_get = pool.fetch(Request(URL, max_bytes=1_000))
    assert FakeCamoufox.last_kwargs == {"headless": True, "i_know_what_im_doing": True}
    assert (res_get.status_code, res_get.text, res_get.truncated) == (
        200,
        "<html>camoufox</html>",
        False,
    )
    assert res_get.final_url == "https://example.com/landing"
    raw = pool.fetch(Request("https://example.com/json", max_bytes=1_000))
    assert raw.body == b'{"raw": true}'
    with_headers = pool.fetch(Request(URL, headers={"Authorization": "x"}, max_bytes=1_000))
    assert with_headers.body == b"posted-body"
    assert FakePage.api_kwargs["headers"] == {"Authorization": "x"}
    post = Request(URL, method="POST", headers={"X-Key": "v"}, body=b"u=1", max_bytes=4)
    res_post = pool.fetch(post)
    assert (res_post.status_code, res_post.body, res_post.truncated) == (201, b"post", True)
    assert FakePage.api_kwargs["max_redirects"] == 0
    assert FakePage.api_kwargs["headers"] == {"X-Key": "v"}
    assert FakePage.api_kwargs["data"] == b"u=1"
    with pytest.raises(FetchError):
        pool.fetch(Request("https://example.com/none", max_bytes=10))
    with pytest.raises(TransientFetchError):
        pool.fetch(Request("https://example.com/boom", max_bytes=10))
    assert (FakeCamoufox.entered, FakePage.closed) == (1, 6)
    pool.close()
    assert FakeCamoufox.exited == 1


def test_camoufox_checks_the_redirect_chain_and_restarts_a_crashed_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import civil_service.fetcher as fetcher_mod

    FakePage.closed = FakeCamoufox.entered = FakeCamoufox.exited = 0
    monkeypatch.setattr(fetcher_mod, "Camoufox", FakeCamoufox)
    pool = CamoufoxPool("Wayback Archiving", 10.0)
    with pytest.raises(UnsafeTargetError):  # a redirect hop to a private address
        pool.fetch(Request("https://example.com/internal-redirect", max_bytes=100))
    assert pool.fetch(Request(URL, max_bytes=100)).status_code == 200

    FakeBrowser.fail_new_page = True
    try:
        with pytest.raises(FetchError, match="could not start"):
            pool.fetch(Request(URL, max_bytes=100))
    finally:
        FakeBrowser.fail_new_page = False
    assert FakeCamoufox.exited == 1  # the crashed browser was dropped
    assert pool.fetch(Request(URL, max_bytes=100)).status_code == 200
    assert FakeCamoufox.entered == 2  # and the next fetch started a new one
    pool.close()


def test_camoufox_pool_reports_a_missing_browser_as_a_fetch_error_and_default_fetcher_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import civil_service.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "Camoufox", BrokenCamoufox)
    broken_pool = CamoufoxPool("Wayback Archiving", 10.0)
    with pytest.raises(FetchError, match="uv run python -m camoufox fetch") as info:
        broken_pool.fetch(Request(URL, max_bytes=10))
    assert not isinstance(info.value, TransientFetchError)
    broken_pool.close()

    default_fetcher = TieredFetcher(AppSettings())
    default_fetcher.close()


def test_interruptible_sleep_waits_until_a_stop_request_ends_it() -> None:
    fetcher = TieredFetcher(AppSettings(fallbacks=["curl"]))
    fetcher.interruptible_sleep(0.01)
    started = time.monotonic()
    threading.Timer(0.1, fetcher.request_stop).start()
    with pytest.raises(StopRequested):
        fetcher.interruptible_sleep(30.0)
    assert time.monotonic() - started < 5.0
    with pytest.raises(StopRequested):
        fetcher.interruptible_sleep(30.0)


def test_curl_cffi_pins_a_list_of_addresses_and_falls_back_to_the_reachable_one(
    local_server: str,
) -> None:
    port = local_server.rsplit(":", 1)[1]
    req = Request(f"http://pinned.test:{port}/html", max_bytes=1_000, pin_ips=("::1", "127.0.0.1"))
    res = fetch_with_curl_cffi(req, "UA", 10.0, impersonate="chrome")
    assert (res.status_code, res.body) == (200, b"<html>hello</html>")


def test_a_stop_request_beats_the_next_transport_and_the_next_hop() -> None:
    calls: list[str] = []
    holder: dict[str, TieredFetcher] = {}

    def dies_on_ctrl_c(_req: Request) -> FetchResult:
        calls.append("curl")
        holder["fetcher"].request_stop()  # the terminal's SIGINT also kills the child
        raise TransientFetchError("curl-killed")

    def must_not_run(_req: Request) -> FetchResult:
        calls.append("curl_cffi")
        return ok()

    fetcher = make_fetcher({"curl": dies_on_ctrl_c, "curl_cffi": must_not_run})
    holder["fetcher"] = fetcher
    with pytest.raises(StopRequested):
        fetcher.fetch(Request(URL))
    assert calls == ["curl"]
    with pytest.raises(StopRequested):  # a later fetch stops before it sends anything
        fetcher.fetch(Request(URL))
    assert calls == ["curl"]


def test_curl_cffi_does_not_pin_ip_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    import curl_cffi.requests as curl_requests

    monkeypatch.setattr(curl_requests, "Session", FakeSession)
    FakeSession.stream_error = FakeSession.request_error = None
    FakeSession.chunks = [b"ok"]
    fetch_with_curl_cffi(
        Request("http://127.0.0.1:81/", max_bytes=10, pin_ips=("127.0.0.1",)),
        "UA",
        5.0,
        impersonate="chrome",
    )
    assert FakeSession.init_kwargs["curl_options"] == {}


def test_closing_a_browser_that_already_died_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import civil_service.fetcher as fetcher_mod

    class DeadOnClose(FakeCamoufox):
        def __exit__(self, *_args: object) -> None:
            raise RuntimeError("driver-connection-closed")

    FakeCamoufox.entered = 0
    monkeypatch.setattr(fetcher_mod, "Camoufox", DeadOnClose)
    pool = CamoufoxPool("Wayback Archiving", 10.0)
    assert pool.fetch(Request(URL, max_bytes=100)).status_code == 200
    with structlog.testing.capture_logs() as logs:
        pool.close()  # must not raise
    assert [e["event"] for e in logs] == ["browser_close_failed"]
    assert "driver-connection-closed" in logs[0]["error"]
    # The pool forgot the dead browser, so a later fetch starts a new one.
    assert pool.fetch(Request(URL, max_bytes=100)).status_code == 200
    assert FakeCamoufox.entered == 2
    with structlog.testing.capture_logs():
        pool.close()


def test_a_stop_still_ends_as_an_interrupt_when_the_browser_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's case: Ctrl-C kills the browser helper, then cleanup closes a dead browser."""
    import civil_service.fetcher as fetcher_mod
    import civil_service.runner as runner_mod

    class DeadOnClose(FakeCamoufox):
        def __exit__(self, *_args: object) -> None:
            raise RuntimeError("driver-connection-closed")

    def interrupted(req: Request) -> FetchResult:
        if req.url.endswith(("/robots.txt", "/sitemap.xml")):
            return FetchResult(404, {}, b"", "text/plain")
        raise KeyboardInterrupt

    monkeypatch.setattr(fetcher_mod, "Camoufox", DeadOnClose)
    settings = AppSettings(
        db_path=tmp_path / "close.sqlite3",
        fallbacks=["curl", "camoufox"],
        respect_robots_txt=False,
        max_sitemaps=0,
        threads=1,
    )
    fetcher = TieredFetcher(settings, transports={"curl": interrupted})
    fetcher._camoufox_pool.fetch(Request(URL, max_bytes=100))  # the browser is running
    # run_archiver builds and closes its own fetcher, as it does for the command line.
    monkeypatch.setattr(runner_mod, "TieredFetcher", lambda _settings: fetcher)
    with structlog.testing.capture_logs() as logs, pytest.raises(KeyboardInterrupt):
        runner_mod.run_archiver("https://example.com/", settings)
    assert "browser_close_failed" in [e["event"] for e in logs]
