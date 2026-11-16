import gzip
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
import structlog
from rich.console import Console

from civil_service.catalog import CRAWL_FAILED, CRAWL_ROBOTS_BLOCKED, Catalog
from civil_service.config import AppSettings
from civil_service.exceptions import (
    FetchError,
    StopRequested,
    TransientFetchError,
    UnsafeTargetError,
)
from civil_service.fetcher import FetchResult, Request, TieredFetcher
from civil_service.runner import CrawlSummary, RobotsGuard, catalog_path, run_archiver

NO_SNAPSHOT = b'{"archived_snapshots": {}}'
PLAIN = "text/plain"
HTML = "text/html"

Page = FetchResult | Callable[[Request], FetchResult]


def snapshot(days_old: int = 0) -> bytes:
    stamp = (datetime.now(UTC) - timedelta(days=days_old)).strftime("%Y%m%d%H%M%S")
    return (
        b'{"archived_snapshots":{"closest":{"available":true,"timestamp":"%s"}}}' % stamp.encode()
    )


def html(body: str = "", status: int = 200) -> FetchResult:
    return FetchResult(status, {}, body.encode(), HTML)


def gone(status: int = 404) -> FetchResult:
    return FetchResult(status, {}, b"", PLAIN)


class FakeWeb:
    """A fake network. Pages map a URL to a result. Archive.org answers come from the fields."""

    def __init__(self, pages: dict[str, Page], default_snapshot: bytes = NO_SNAPSHOT) -> None:
        self.pages = pages
        self.default_snapshot = default_snapshot
        self.snapshots: dict[str, bytes] = {}
        self.save_status: dict[str, int] = {}
        self.requests: list[Request] = []

    @property
    def urls(self) -> list[str]:
        return [r.url for r in self.requests]

    def __call__(self, req: Request) -> FetchResult:
        self.requests.append(req)
        url = req.url
        if url in self.pages:
            page = self.pages[url]
            return page(req) if callable(page) else page
        if "archive.org/wayback/available" in url:
            for key, body in self.snapshots.items():
                if key in url:
                    return FetchResult(200, {}, body, "application/json")
            return FetchResult(200, {}, self.default_snapshot, "application/json")
        if url.startswith("https://web.archive.org/save/"):
            target = url.removeprefix("https://web.archive.org/save/")
            return FetchResult(self.save_status.get(target, 200), {}, b"ok", HTML)
        return gone()


def settings_for(tmp_path: Path, name: str, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "db_path": tmp_path / f"{name}.sqlite3",
        "fallbacks": ["curl"],
        "archive_delay_seconds": 0.0,
        "retry_delays_seconds": [0.0],
        "progress_refresh_seconds": 0.0,
        **overrides,
    }
    return AppSettings(**values)  # type: ignore[arg-type]


def run(
    web: Callable[[Request], FetchResult],
    settings: AppSettings,
    seed: str = "https://example.com/",
    **kwargs: object,
) -> CrawlSummary:
    fetcher = TieredFetcher(settings, transports={"curl": web})
    return run_archiver(seed, settings, fetcher=fetcher, **kwargs)  # type: ignore[arg-type]


def entry(settings: AppSettings, url: str):  # type: ignore[no-untyped-def]
    catalog = Catalog(settings.db_path, settings.rearchive_window_days)
    try:
        return catalog.get_entry(url)
    finally:
        catalog.close()


def test_runner_crawls_html_skips_binary_and_archives_external_links(tmp_path: Path) -> None:
    def home(_req: Request) -> FetchResult:
        time.sleep(0.02)
        return html(
            '<a href="/page2">P2</a><a href="/doc.pdf">PDF</a><a href="https://ext.org/page">E</a>'
        )

    web = FakeWeb(
        {
            "https://example.com/": home,
            "https://example.com/page2": html("<p>Page 2</p>"),
            "https://example.com/doc.pdf": FetchResult(
                200, {}, b"%PDF-1.4\xff\xfe", "application/pdf"
            ),
        }
    )
    settings = settings_for(tmp_path, "cat")
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)
    fetcher = TieredFetcher(settings, transports={"curl": web})
    summary = run_archiver("https://example.com/", settings, console=console, fetcher=fetcher)
    assert (summary.crawled, summary.submitted, summary.archived) == (3, 4, 0)
    assert (summary.crawl_failed, summary.archive_failed) == (0, 0)
    assert (summary.covered, summary.unconfirmed, summary.eligible) == (0, 4, 4)
    assert summary.coverage_percent == 0.0
    rendered = output.getvalue()
    assert "3/3 done (0 left) | ok=3 skip=0 fail=0" in rendered
    assert "4/4 done (0 left) | arch=0 sub=4 fail=0" in rendered


def test_runner_still_submits_robots_blocked_and_blocked_pages(tmp_path: Path) -> None:
    home = (
        '<a href="/blocked">Blocked</a><a href="/broken">Broken</a>'
        '<a href="https://private.internal/x">Private</a>'
        '<a href="https://already.org/">Already</a><a href="https://badsave.org/">BadSave</a>'
        '<a href="https://badavail.org/">BadAvail</a>'
    )
    web = FakeWeb(
        {
            "https://example.com/robots.txt": FetchResult(
                200, {}, b"User-agent: *\nDisallow: /blocked\n", PLAIN
            ),
            "https://example.com/": html(home),
            "https://example.com/broken": FetchResult(403, {}, b"denied", HTML),
        }
    )
    web.snapshots = {"already.org": snapshot(), "badavail.org": b"not-json"}
    web.save_status = {"https://badsave.org/": 400}
    settings = settings_for(tmp_path, "edge")
    summary = run(web, settings)
    assert (summary.crawled, summary.robots_blocked, summary.crawl_failed) == (1, 1, 1)
    assert (summary.archived, summary.submitted) == (1, 3)
    assert (summary.archive_failed, summary.archive_skipped) == (2, 1)
    assert (summary.covered, summary.unconfirmed, summary.eligible) == (1, 3, 6)
    assert summary.coverage_percent == pytest.approx(100 / 6)
    assert "https://example.com/blocked" not in web.urls
    saved = {u for u in web.urls if u.startswith("https://web.archive.org/save/")}
    assert "https://web.archive.org/save/https://example.com/blocked" in saved
    assert "https://web.archive.org/save/https://example.com/broken" in saved
    assert "https://web.archive.org/save/https://private.internal/x" not in saved
    blocked = entry(settings, "https://example.com/blocked")
    broken = entry(settings, "https://example.com/broken")
    private = entry(settings, "https://private.internal/x")
    assert (blocked.crawl_status, blocked.archive_status) == (CRAWL_ROBOTS_BLOCKED, "SUBMITTED")
    assert (broken.crawl_status, broken.archive_status) == (CRAWL_FAILED, "SUBMITTED")
    assert private.archive_status == "SKIPPED"


def test_runner_submits_unfetchable_pages_but_not_gone_pages(tmp_path: Path) -> None:
    attempts: dict[str, int] = {}

    def counting(status: int) -> Callable[[Request], FetchResult]:
        def respond(req: Request) -> FetchResult:
            attempts[req.url] = attempts.get(req.url, 0) + 1
            return FetchResult(status, {}, b"", HTML)

        return respond

    def timeout(req: Request) -> FetchResult:
        attempts[req.url] = attempts.get(req.url, 0) + 1
        raise TransientFetchError("timeout")

    links = "".join(f'<a href="/{n}">{n}</a>' for n in "abcde")
    web = FakeWeb(
        {
            "https://example.com/": html(links),
            "https://example.com/a": counting(500),
            "https://example.com/b": gone(404),
            "https://example.com/c": gone(410),
            "https://example.com/d": timeout,
            "https://example.com/e": counting(429),
        }
    )
    settings = settings_for(tmp_path, "unfetchable", max_transient_attempts=2)
    summary = run(web, settings)
    assert (summary.crawled, summary.crawl_failed) == (3, 3)
    assert summary.submitted == 4
    assert attempts == {
        "https://example.com/a": 2,
        "https://example.com/d": 2,
        "https://example.com/e": 4,
    }
    for gone_page in ("b", "c"):
        row = entry(settings, f"https://example.com/{gone_page}")
        assert row is not None
        assert row.archive_status == "SKIPPED"
        assert f"https://web.archive.org/save/https://example.com/{gone_page}" not in web.urls
    for kept in ("a", "d", "e"):
        assert f"https://web.archive.org/save/https://example.com/{kept}" in web.urls


def test_runner_retries_transient_crawl_and_archive_failures_inside_the_run(
    tmp_path: Path,
) -> None:
    flaky = {"crawl": 0, "avail": 0}

    def flaky_page(_req: Request) -> FetchResult:
        flaky["crawl"] += 1
        if flaky["crawl"] == 1:
            raise TransientFetchError("reset")
        return html("<p>fine</p>")

    class FlakyAvailability(FakeWeb):
        def __call__(self, req: Request) -> FetchResult:
            if "available" in req.url and "ext.org" in req.url:
                flaky["avail"] += 1
                if flaky["avail"] == 1:
                    raise TransientFetchError("reset")
            return super().__call__(req)

    web = FlakyAvailability(
        {
            "https://example.com/": html('<a href="/flaky">f</a><a href="https://ext.org/">e</a>'),
            "https://example.com/flaky": flaky_page,
        },
        default_snapshot=snapshot(),
    )
    summary = run(web, settings_for(tmp_path, "flaky"))
    assert (summary.crawled, summary.crawl_failed) == (2, 0)
    assert (summary.archived, summary.archive_failed) == (3, 0)
    assert flaky == {"crawl": 2, "avail": 2}

    def never_works(req: Request) -> FetchResult:
        if "available" in req.url and "ext.org" in req.url:
            raise TransientFetchError("down")
        if "available" in req.url:
            return FetchResult(200, {}, snapshot(), "application/json")
        return html('<a href="https://ext.org/">e</a>')

    capped = settings_for(tmp_path, "capped", max_transient_attempts=2)
    summary = run(never_works, capped)
    assert summary.archive_failed == 1
    row = entry(capped, "https://ext.org/")
    assert (row.archive_status, row.archive_attempts) == ("FAILED", 1)


def test_hosts_that_do_not_resolve_retry_and_then_fail_and_are_never_skipped(
    tmp_path: Path,
) -> None:
    web = FakeWeb(
        {
            "https://example.com/": html(
                '<a href="https://dns-flaky.invalid/">flaky</a>'
                '<a href="https://unresolvable.invalid/">gone</a>'
            )
        }
    )
    settings = settings_for(tmp_path, "dns")
    summary = run(web, settings)
    assert (summary.archive_failed, summary.archive_skipped) == (2, 0)
    for host in ("dns-flaky", "unresolvable"):
        row = entry(settings, f"https://{host}.invalid/")
        assert (row.archive_status, row.archive_attempts) == ("FAILED", 2)
    assert not any("invalid" in u for u in web.urls)


def test_retries_do_not_use_up_the_page_and_archive_limits(tmp_path: Path) -> None:
    calls = {"flaky": 0, "avail": 0}

    def flaky(_req: Request) -> FetchResult:
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise TransientFetchError("reset")
        return html("")

    class FlakyAvailability(FakeWeb):
        def __call__(self, req: Request) -> FetchResult:
            if "available" in req.url and "example.com%2Fflaky" in req.url:
                calls["avail"] += 1
                if calls["avail"] == 1:
                    raise TransientFetchError("reset")
            return super().__call__(req)

    web = FlakyAvailability(
        {
            "https://example.com/": html('<a href="/flaky">f</a>'),
            "https://example.com/flaky": flaky,
        },
        default_snapshot=snapshot(),
    )
    settings = settings_for(tmp_path, "limits", threads=1, max_crawl_pages=2, max_archive_urls=2)
    summary = run(web, settings)
    assert (summary.crawled, summary.crawl_failed) == (2, 0)
    assert (summary.archived, summary.archive_failed) == (2, 0)
    assert calls == {"flaky": 2, "avail": 2}


def test_runner_rejects_invalid_seed_and_supports_default_fetcher_and_no_robots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path, "default", respect_robots_txt=False)
    with pytest.raises(UnsafeTargetError):
        run_archiver("ftp://example.com/", settings)
    with pytest.raises(UnsafeTargetError):
        run_archiver("http://private.internal/", settings)

    def fake_fetch(_self: TieredFetcher, request: Request) -> FetchResult:
        if "available" in request.url:
            return FetchResult(200, {}, snapshot(), "application/json")
        return html("<p>leaf</p>")

    monkeypatch.setattr(TieredFetcher, "fetch", fake_fetch)
    summary = run_archiver("https://example.com/", settings)
    assert (summary.crawled, summary.archived) == (1, 1)


@pytest.mark.parametrize(
    "robots",
    [
        FetchResult(403, {}, b"forbidden", PLAIN),
        FetchResult(500, {}, b"", PLAIN),
        FetchResult(429, {}, b"", PLAIN),
        TransientFetchError("network-down"),
        FetchError("no-transport"),
        UnsafeTargetError("private"),
        RuntimeError("surprise"),
    ],
)
def test_runner_robots_fetch_error_fails_open(
    tmp_path: Path, robots: FetchResult | Exception
) -> None:
    def robots_page(_req: Request) -> FetchResult:
        if isinstance(robots, Exception):
            raise robots
        return robots

    web = FakeWeb(
        {
            "https://example.com/robots.txt": robots_page,
            "https://example.com/": html("<p>ok</p>"),
        },
        default_snapshot=snapshot(),
    )
    summary = run(web, settings_for(tmp_path, "robots_err"))
    assert (summary.crawled, summary.robots_blocked) == (1, 0)


def test_robots_guard_loads_each_origin_once_and_waits_only_for_that_origin() -> None:
    started = threading.Event()
    release = threading.Event()
    fetched: list[str] = []

    def transport(req: Request) -> FetchResult:
        fetched.append(req.url)
        if req.url.startswith("https://slow.example"):
            started.set()
            release.wait(timeout=5)
        return FetchResult(200, {}, b"User-agent: *\nDisallow: /private\n", PLAIN)

    settings = AppSettings(fallbacks=["curl"])
    guard = RobotsGuard("Wayback Archiving", TieredFetcher(settings, {"curl": transport}), True)
    results: list[bool] = []
    first = threading.Thread(
        target=lambda: results.append(guard.is_allowed("https://slow.example/a"))
    )
    first.start()
    assert started.wait(timeout=5)
    second = threading.Thread(
        target=lambda: results.append(guard.is_allowed("https://slow.example/b"))
    )
    second.start()
    time.sleep(0.2)
    started_at = time.monotonic()
    assert guard.is_allowed("https://fast.example/private") is False
    assert not release.is_set()
    assert time.monotonic() - started_at < 2.0  # the slow origin still holds its own lock
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert results == [True, True]
    assert fetched.count("https://slow.example/robots.txt") == 1
    assert guard.sitemaps("https://fast.example/") == []


def test_runner_does_not_deadlock_on_uncaught_crawl_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    settings = settings_for(tmp_path, "deadlock", threads=2)

    def broken_claim(_self: Catalog) -> str | None:
        raise RuntimeError("boom-in-crawl-worker")

    monkeypatch.setattr(Catalog, "claim_next_crawl", broken_claim)
    web = FakeWeb({}, default_snapshot=snapshot())
    seeded = Catalog(settings.db_path, settings.rearchive_window_days)
    seeded.insert_discovered("https://waiting.org/", is_internal=False)
    seeded.close()
    fetcher = TieredFetcher(settings, transports={"curl": web})
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(run_archiver, "https://example.com/", settings, fetcher=fetcher)
    try:
        with pytest.raises(RuntimeError, match="boom-in-crawl-worker"):
            fut.result(timeout=5.0)
    finally:
        pool.shutdown(wait=False)
    drained = entry(settings, "https://waiting.org/")
    assert drained.archive_status == "ARCHIVED"


def test_the_crawl_keeps_going_while_a_wayback_call_is_blocked(tmp_path: Path) -> None:
    last_page_fetched = threading.Event()
    order: list[str] = []

    def last_page(_req: Request) -> FetchResult:
        order.append("crawled-last-page")
        last_page_fetched.set()
        return html("")

    class SlowWayback(FakeWeb):
        def __call__(self, req: Request) -> FetchResult:
            if "available" in req.url:
                order.append("wayback-call-started")
                if not last_page_fetched.wait(timeout=5):
                    order.append("crawl-waited-for-wayback")
                order.append("wayback-call-ended")
            return super().__call__(req)

    web = SlowWayback(
        {
            "https://example.com/": html('<a href="/a">a</a><a href="/b">b</a><a href="/z">z</a>'),
            "https://example.com/a": html(""),
            "https://example.com/b": html(""),
            "https://example.com/z": last_page,
        },
        default_snapshot=snapshot(),
    )
    settings = settings_for(tmp_path, "parallel", threads=1)
    summary = run(web, settings)
    assert summary.crawled == 4
    assert "crawl-waited-for-wayback" not in order
    assert order.index("crawled-last-page") < order.index("wayback-call-ended")


def test_runner_paces_only_submissions_with_the_archive_delay(tmp_path: Path) -> None:
    slept: list[float] = []
    web = FakeWeb({"https://example.com/": html('<a href="https://ext.org/1">1</a>')})
    settings = settings_for(tmp_path, "throttle", archive_delay_seconds=4.0)
    summary = run(web, settings, sleep_fn=slept.append)
    assert summary.submitted == 2
    assert len(slept) == 1
    assert 0.0 < slept[0] <= 4.0

    quiet: list[float] = []
    fresh = FakeWeb(
        {"https://example.com/": html('<a href="https://ext.org/1">1</a>')},
        default_snapshot=snapshot(),
    )
    run(fresh, settings_for(tmp_path, "no_wait", archive_delay_seconds=4.0), sleep_fn=quiet.append)
    assert quiet == []


def test_runner_canonicalizes_redirects_and_enforces_bounds(tmp_path: Path) -> None:
    crawled: list[str] = []

    def record(result: FetchResult) -> Callable[[Request], FetchResult]:
        def respond(req: Request) -> FetchResult:
            crawled.append(req.url)
            return result

        return respond

    web = FakeWeb(
        {
            "https://example.com/": html('<a href="/about/">About</a><a href="/other">Other</a>'),
            "https://example.com/about/": record(
                FetchResult(301, {"location": "https://example.com/about"}, b"", "")
            ),
            "https://example.com/about": record(
                html('<a href="team">Team</a><a href="https://ext.org/1">Ext</a>')
            ),
            "https://example.com/other": record(html("<p>other</p>")),
        },
        default_snapshot=snapshot(),
    )
    settings = settings_for(tmp_path, "canonical", threads=1, max_crawl_pages=3, max_archive_urls=2)
    summary = run(web, settings)
    assert (summary.crawled, summary.archived) == (3, 2)
    assert len([u for u in web.urls if "available" in u]) == 2
    assert crawled == [
        "https://example.com/about/",
        "https://example.com/about",
        "https://example.com/other",
    ]
    about = entry(settings, "https://example.com/about")
    assert about is not None
    assert about.crawl_status == 200
    assert entry(settings, "https://example.com/team") is not None


def test_a_redirect_to_another_domain_makes_the_target_an_external_link(tmp_path: Path) -> None:
    web = FakeWeb(
        {
            "https://example.com/": FetchResult(
                301, {"location": "https://other.org/landing"}, b"", ""
            ),
            "https://other.org/landing": html('<a href="/sub">Sub</a>'),
        },
        default_snapshot=snapshot(),
    )
    settings = settings_for(tmp_path, "ext_redir")
    summary = run(web, settings)
    assert (summary.crawled, summary.archived) == (1, 2)
    landing = entry(settings, "https://other.org/landing")
    assert landing is not None
    assert landing.is_internal is False
    assert entry(settings, "https://other.org/sub") is None


def sitemap(*urls: str, index: bool = False) -> FetchResult:
    tag, inner = ("sitemapindex", "sitemap") if index else ("urlset", "url")
    body = "".join(f"<{inner}><loc>{u}</loc></{inner}>" for u in urls)
    return FetchResult(200, {}, f"<{tag}>{body}</{tag}>".encode(), "application/xml")


def test_runner_discovers_pages_from_robots_sitemap_index(tmp_path: Path) -> None:
    web = FakeWeb(
        {
            "https://example.com/robots.txt": FetchResult(
                200, {}, b"Sitemap: https://example.com/maps/index.xml\n", PLAIN
            ),
            "https://example.com/maps/index.xml": sitemap(
                "https://example.com/maps/a.xml",
                "https://example.com/maps/a.xml",
                "https://evil.org/s.xml",
                "mailto:x@example.com",
                index=True,
            ),
            "https://example.com/maps/a.xml": sitemap(
                "https://example.com/from-sitemap",
                "https://other.org/ext",
                "ftp://example.com/skipped",
            ),
            "https://example.com/": html("<p>no links</p>"),
            "https://example.com/from-sitemap": html("<p>found</p>"),
        },
        default_snapshot=snapshot(),
    )
    settings = settings_for(tmp_path, "sitemap")
    summary = run(web, settings)
    assert summary.crawled == 2
    assert summary.archived == 3
    assert "https://evil.org/s.xml" not in web.urls
    assert "https://example.com/sitemap.xml" in web.urls
    assert entry(settings, "https://other.org/ext") is not None
    sitemap_requests = [r for r in web.requests if r.url.endswith(".xml")]
    assert {r.max_bytes for r in sitemap_requests} == {settings.max_sitemap_bytes}


def test_runner_reads_gzip_sitemaps_and_rejects_bombs_and_bad_responses(tmp_path: Path) -> None:
    zipped = gzip.compress(b"<urlset><url><loc>https://example.com/gz-page</loc></url></urlset>")
    web = FakeWeb(
        {
            "https://example.com/sitemap.xml": FetchResult(200, {}, zipped, "application/gzip"),
            "https://example.com/": html(""),
            "https://example.com/gz-page": html("<p>zipped</p>"),
        },
        default_snapshot=snapshot(),
    )
    assert run(web, settings_for(tmp_path, "gz")).crawled == 2

    bomb = gzip.compress(b"a" * 100_000)
    hostile = FakeWeb(
        {
            "https://example.com/robots.txt": FetchResult(
                200,
                {},
                b"Sitemap: https://example.com/gone.xml\nSitemap: https://private.internal/s.xml\n"
                b"Sitemap: https://example.com/broken.xml\n",
                PLAIN,
            ),
            "https://example.com/sitemap.xml": FetchResult(200, {}, bomb, "application/gzip"),
            "https://example.com/gone.xml": gone(410),
            "https://example.com/broken.xml": FetchResult(500, {}, b"", PLAIN),
            "https://example.com/": html(""),
        },
        default_snapshot=snapshot(),
    )
    settings = settings_for(tmp_path, "bomb", max_sitemap_bytes=1_000)
    with structlog.testing.capture_logs() as logs:
        summary = run(hostile, settings)
    events = [e["event"] for e in logs]
    assert summary.crawled == 1
    assert "sitemap_rejected" in events
    assert events.count("sitemap_unavailable") == 2


def test_runner_bounds_how_many_sitemaps_it_fetches(tmp_path: Path) -> None:
    robots = FetchResult(
        200,
        {},
        b"Sitemap: https://example.com/one.xml\nSitemap: https://example.com/two.xml\n",
        PLAIN,
    )
    web = FakeWeb({"https://example.com/robots.txt": robots, "https://example.com/": html("")})
    run(web, settings_for(tmp_path, "limited", max_sitemaps=1))
    assert [u for u in web.urls if u.endswith(".xml")] == ["https://example.com/one.xml"]

    none = FakeWeb({"https://example.com/robots.txt": robots, "https://example.com/": html("")})
    run(none, settings_for(tmp_path, "none", max_sitemaps=0))
    assert [u for u in none.urls if u.endswith(".xml")] == []


def test_runner_throttles_progress_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    original = Catalog.pending_counts

    def counting(self: Catalog) -> tuple[int, int]:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Catalog, "pending_counts", counting)
    pages: dict[str, Page] = {
        "https://example.com/": html("".join(f'<a href="/p{i}">{i}</a>' for i in range(6)))
    }
    pages.update({f"https://example.com/p{i}": html("") for i in range(6)})
    run(FakeWeb(pages, default_snapshot=snapshot()), settings_for(tmp_path, "busy"))
    unthrottled = len(calls)
    calls.clear()
    run(
        FakeWeb(pages, default_snapshot=snapshot()),
        settings_for(tmp_path, "calm", progress_refresh_seconds=3600.0),
    )
    assert len(calls) == 2
    assert unthrottled > 2


def spn2_web() -> FakeWeb:
    return FakeWeb(
        {
            "https://example.com/": html('<a href="/two">2</a>'),
            "https://example.com/two": html(""),
            "https://web.archive.org/save": FetchResult(
                200, {}, b'{"job_id":"j1"}', "application/json"
            ),
            "https://web.archive.org/save/status/j1": FetchResult(
                200, {}, b'{"status":"success"}', "application/json"
            ),
        }
    )


def test_runner_uses_spn2_when_keys_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIVIL_SERVICE_SPN_ACCESS_KEY", "access")
    monkeypatch.setenv("CIVIL_SERVICE_SPN_SECRET_KEY", "s3cret")
    slept: list[float] = []
    web = spn2_web()
    settings = settings_for(tmp_path, "spn2", archive_delay_seconds=1.0, spn2_delay_seconds=9.0)
    summary = run(web, settings, sleep_fn=slept.append)
    assert (summary.archived, summary.submitted, summary.archive_failed) == (2, 0, 0)
    assert (summary.covered, summary.unconfirmed, summary.eligible) == (2, 0, 2)
    assert len(slept) == 1
    assert 8.0 < slept[0] <= 9.0
    posts = [r for r in web.requests if r.method == "POST"]
    assert len(posts) == 2
    assert all(r.headers["Authorization"] == "LOW access:s3cret" for r in posts)
    row = entry(settings, "https://example.com/two")
    assert row is not None
    assert row.archive_status == "ARCHIVED"


def test_api_keys_never_reach_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CIVIL_SERVICE_SPN_ACCESS_KEY", "access-key-value")
    monkeypatch.setenv("CIVIL_SERVICE_SPN_SECRET_KEY", "secret-key-value")
    web = spn2_web()
    web.pages["https://web.archive.org/save/status/j1"] = FetchResult(
        200, {}, b'{"status":"error","status_ext":"error:blocked"}', "application/json"
    )
    with structlog.testing.capture_logs() as logs, caplog.at_level("DEBUG"):
        summary = run(web, settings_for(tmp_path, "keys", spn2_delay_seconds=0.0))
    assert summary.archive_failed == 2
    assert logs
    text = f"{logs} {caplog.text}"
    assert "access-key-value" not in text
    assert "secret-key-value" not in text


def test_each_site_gets_its_own_catalog_file(tmp_path: Path) -> None:
    template = tmp_path / "{host}.sqlite3"
    assert catalog_path(template, "https://example.com/") == tmp_path / "example.com.sqlite3"
    assert (
        catalog_path(template, "http://example.com:8080/x") == tmp_path / "example.com_8080.sqlite3"
    )
    shared = tmp_path / "shared.sqlite3"
    assert catalog_path(shared, "https://a.com/") == shared
    web = FakeWeb({"https://example.com/": html("")}, default_snapshot=snapshot())
    run(web, settings_for(tmp_path, "unused", db_path=template))
    assert (tmp_path / "example.com.sqlite3").is_file()


def test_crawl_summary_percent_handles_an_empty_catalog() -> None:
    assert CrawlSummary().coverage_percent == 0.0
    assert CrawlSummary(covered=1, eligible=4).coverage_percent == 25.0


def test_one_bad_url_never_ends_the_run(tmp_path: Path) -> None:
    links = (
        '<a href="http://[oops/">bad-ipv6</a><a href="https://a..b.com/">bad-label</a>'
        '<a href="/boom">boom</a><a href="https://ext.org/boom">ext-boom</a>'
    )

    def boom(_req: Request) -> FetchResult:
        raise RuntimeError("surprise")

    class SurpriseAvailability(FakeWeb):
        def __call__(self, req: Request) -> FetchResult:
            if "available" in req.url and "ext.org%2Fboom" in req.url:
                raise RuntimeError("surprise")
            return super().__call__(req)

    web = SurpriseAvailability(
        {"https://example.com/": html(links), "https://example.com/boom": boom}
    )
    settings = settings_for(tmp_path, "poison")
    summary = run(web, settings)
    assert (summary.crawled, summary.crawl_failed) == (1, 1)
    assert (summary.archive_skipped, summary.archive_failed) == (1, 1)
    assert entry(settings, "https://a..b.com/").archive_status == "SKIPPED"
    assert entry(settings, "https://ext.org/boom").archive_status == "FAILED"
    assert "https://web.archive.org/save/https://example.com/boom" in web.urls
    assert entry(settings, "http://[oops/") is None


def test_ctrl_c_stops_the_run_cleanly_and_the_next_run_resumes(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def interrupted(_req: Request) -> FetchResult:
        raise KeyboardInterrupt

    pages: dict[str, Page] = {
        "https://example.com/": html('<a href="/a">a</a><a href="/b">b</a>'),
        "https://example.com/a": interrupted,
        "https://example.com/b": html(""),
    }
    settings = settings_for(tmp_path, "resume", threads=2)
    fetcher = TieredFetcher(settings, transports={"curl": FakeWeb(pages)})
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(run_archiver, "https://example.com/", settings, fetcher=fetcher)
    try:
        with pytest.raises(KeyboardInterrupt):
            fut.result(timeout=10.0)
    finally:
        pool.shutdown(wait=False)

    pages["https://example.com/a"] = html("<p>fixed</p>")
    summary = run(FakeWeb(pages, default_snapshot=snapshot()), settings)
    assert entry(settings, "https://example.com/a").crawl_status == 200
    assert entry(settings, "https://example.com/b").crawl_status == 200
    assert summary.crawl_failed == 0


def test_a_stop_request_leaves_the_url_unfinished_and_the_next_run_completes_it(
    tmp_path: Path,
) -> None:
    def stopped(_req: Request) -> FetchResult:
        raise StopRequested("stop")

    settings = settings_for(tmp_path, "stopped")
    first = run(FakeWeb({"https://example.com/": stopped}), settings)
    assert (first.crawled, first.crawl_failed) == (0, 0)
    assert entry(settings, "https://example.com/").needs_crawl == 2

    class StopOnAvailability(FakeWeb):
        def __call__(self, req: Request) -> FetchResult:
            if "available" in req.url:
                raise StopRequested("stop")
            return super().__call__(req)

    pages: dict[str, Page] = {"https://example.com/": html('<a href="https://ext.org/">e</a>')}
    second = run(StopOnAvailability(pages), settings)
    assert second.archive_failed == 0
    assert entry(settings, "https://example.com/").archive_status == "IN_PROGRESS"
    assert entry(settings, "https://ext.org/").archive_status == "PENDING"

    third = run(FakeWeb(pages, default_snapshot=snapshot()), settings)
    assert (third.archived, third.archive_failed) == (2, 0)
    assert entry(settings, "https://ext.org/").archive_status == "ARCHIVED"


def test_an_archive_worker_crash_stops_the_whole_run_and_joins_every_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def slow_page(_req: Request) -> FetchResult:
        time.sleep(0.05)
        return html("")

    count = 40
    pages: dict[str, Page] = {
        "https://example.com/": html("".join(f'<a href="/p{i}">{i}</a>' for i in range(count)))
    }
    pages.update({f"https://example.com/p{i}": slow_page for i in range(count)})

    def broken_claim(_self: Catalog) -> str | None:
        raise RuntimeError("catalog-failed")

    monkeypatch.setattr(Catalog, "claim_next_archive", broken_claim)
    settings = settings_for(tmp_path, "archive_crash", threads=1)
    web = FakeWeb(pages)
    fetcher = TieredFetcher(settings, transports={"curl": web})
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(run_archiver, "https://example.com/", settings, fetcher=fetcher)
    try:
        with pytest.raises(RuntimeError, match="catalog-failed"):
            fut.result(timeout=20.0)
    finally:
        pool.shutdown(wait=False)
    crawled = [u for u in web.urls if u.startswith("https://example.com/p")]
    assert len(crawled) < count  # the crawl stopped instead of visiting the whole site
    monkeypatch.undo()
    assert entry(settings, "https://example.com/") is not None  # the catalog closed cleanly


def test_a_failure_at_the_wayback_endpoint_is_not_blamed_on_the_target_url(
    tmp_path: Path,
) -> None:
    class SinkholedWayback(FakeWeb):
        def __call__(self, req: Request) -> FetchResult:
            if "archive.org" in req.url:
                raise UnsafeTargetError("endpoint-redirected-to-a-private-address")
            return super().__call__(req)

    web = SinkholedWayback({"https://example.com/": html("")})
    settings = settings_for(tmp_path, "sinkhole")
    summary = run(web, settings)
    assert (summary.archive_failed, summary.archive_skipped) == (1, 0)
    assert entry(settings, "https://example.com/").archive_status == "FAILED"


def test_a_stop_during_the_robots_fetch_ends_the_run_without_allowing_every_url(
    tmp_path: Path,
) -> None:
    def stopped(_req: Request) -> FetchResult:
        raise StopRequested("stop")

    web = FakeWeb({"https://example.com/robots.txt": stopped, "https://example.com/": html("")})
    with pytest.raises(StopRequested):
        run(web, settings_for(tmp_path, "robots_stop"))
    assert "https://example.com/" not in web.urls


def test_ctrl_c_while_the_pools_shut_down_still_asks_every_worker_to_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import civil_service.runner as runner_mod

    class Interrupting(ThreadPoolExecutor):
        interrupted = False

        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
            if not Interrupting.interrupted:
                Interrupting.interrupted = True
                raise KeyboardInterrupt

    monkeypatch.setattr(runner_mod, "ThreadPoolExecutor", Interrupting)
    settings = settings_for(tmp_path, "shutdown_interrupt")
    fetcher = TieredFetcher(
        settings, transports={"curl": FakeWeb({"https://example.com/": html("")})}
    )
    with pytest.raises(KeyboardInterrupt):
        run_archiver("https://example.com/", settings, fetcher=fetcher)
    with pytest.raises(StopRequested):
        fetcher.interruptible_sleep(30.0)


def test_a_crash_between_saving_links_and_marking_the_page_done_loses_no_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    pages: dict[str, Page] = {
        "https://example.com/": html('<a href="/a">a</a><a href="/b">b</a><a href="/c">c</a>'),
        "https://example.com/a": html(""),
        "https://example.com/b": html(""),
        "https://example.com/c": html(""),
    }
    settings = settings_for(tmp_path, "crash_between", threads=1)
    original = Catalog.insert_discovered
    inserted: list[str] = []

    def dies_on_the_second_link(
        self: Catalog, url: str, is_internal: bool, **_kwargs: object
    ) -> None:
        inserted.append(url)
        if len(inserted) == 3:  # the seed, then the first link, then this one
            raise KeyboardInterrupt  # a crash that no handler in the tool catches
        original(self, url, is_internal)

    monkeypatch.setattr(Catalog, "insert_discovered", dies_on_the_second_link)
    fetcher = TieredFetcher(settings, transports={"curl": FakeWeb(pages)})
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(run_archiver, "https://example.com/", settings, fetcher=fetcher)
    try:
        with pytest.raises(KeyboardInterrupt):
            fut.result(timeout=10.0)
    finally:
        pool.shutdown(wait=False)
    monkeypatch.undo()
    seed = entry(settings, "https://example.com/")
    assert seed.needs_crawl == 2  # not done: the next run fetches it again

    summary = run(FakeWeb(pages, default_snapshot=snapshot()), settings)
    assert summary.crawl_failed == 0
    for name in "abc":
        assert entry(settings, f"https://example.com/{name}").crawl_status == 200
