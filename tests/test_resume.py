"""A run is safe to repeat and to resume, also after the process dies without cleanup."""

import os
import select
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sigint_driver import SITE_PAGES

from civil_service.catalog import Catalog
from civil_service.config import AppSettings
from civil_service.fetcher import FetchResult, Request, TieredFetcher
from civil_service.runner import run_archiver

DRIVER = Path(__file__).parent / "sigint_driver.py"
SEED = "https://example.com/"


class Site:
    """A fake site that counts how often each URL is requested."""

    def __init__(self) -> None:
        self.requests: Counter[str] = Counter()

    def __call__(self, req: Request) -> FetchResult:
        self.requests[req.url] += 1
        if req.url.endswith(("/robots.txt", "/sitemap.xml")):
            return FetchResult(404, {}, b"", "text/plain")
        if "archive.org/wayback/available" in req.url:
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S").encode()
            body = b'{"archived_snapshots":{"closest":{"available":true,"timestamp":"%s"}}}' % stamp
            return FetchResult(200, {}, body, "application/json")
        links = "".join(f'<a href="/p{i}">p</a>' for i in range(SITE_PAGES))
        body = links.encode() if req.url == SEED else b"<p>leaf</p>"
        return FetchResult(200, {}, body, "text/html")

    def page_requests(self) -> Counter[str]:
        return Counter(
            {
                u: n
                for u, n in self.requests.items()
                if u.startswith(SEED) and not u.endswith(("/robots.txt", "/sitemap.xml"))
            }
        )


def run(site: Site, db: Path) -> object:
    settings = AppSettings(
        db_path=db, fallbacks=["curl"], archive_delay_seconds=0.0, max_sitemaps=0, threads=2
    )
    fetcher = TieredFetcher(settings, transports={"curl": site})
    return run_archiver(SEED, settings, fetcher=fetcher)


def test_a_finished_run_repeated_inside_the_window_changes_and_sends_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "site.sqlite3"
    first = Site()
    run(first, db)
    assert sum(first.page_requests().values()) == SITE_PAGES + 1

    def snapshot_of_catalog() -> list[tuple[object, ...]]:
        catalog = Catalog(db, 7)
        try:
            rows = catalog._conn.execute("SELECT * FROM urls ORDER BY url").fetchall()
        finally:
            catalog.close()
        return [tuple(r) for r in rows]

    before = snapshot_of_catalog()
    second = Site()
    summary = run(second, db)
    assert second.page_requests() == Counter()  # no page fetched again
    assert not [u for u in second.requests if "archive.org" in u]  # nothing checked or sent
    assert (summary.crawled, summary.archived, summary.submitted) == (0, 0, 0)
    assert snapshot_of_catalog() == before  # not one row changed


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")
def test_a_run_killed_without_cleanup_resumes_and_finishes_every_page(tmp_path: Path) -> None:
    db = tmp_path / "killed.sqlite3"
    proc = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed arguments, test helper
        [sys.executable, str(DRIVER), "site", str(db)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    fetched = 0
    deadline = time.monotonic() + 30
    try:
        while fetched < 8 and time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready and proc.stdout.readline().startswith("FETCHED"):
                fetched += 1
        assert fetched >= 8, "the driver did not crawl fast enough"
        proc.send_signal(signal.SIGKILL)  # no handler runs, no cleanup, no clean close
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()

    resumed = Site()
    summary = run(resumed, db)
    catalog = Catalog(db, 7)
    try:
        rows = catalog._conn.execute(
            "SELECT url, needs_crawl, crawl_status FROM urls WHERE is_internal = 1"
        ).fetchall()
        pending = catalog.pending_counts()
    finally:
        catalog.close()
    assert len(rows) == SITE_PAGES + 1  # no link was lost
    assert {(r[1], r[2]) for r in rows} == {(0, 200)}  # every page is done
    assert pending == (0, 0)
    assert max(resumed.page_requests().values()) == 1  # nothing fetched twice in the resumed run
    assert sum(resumed.page_requests().values()) < SITE_PAGES + 1  # finished pages stayed done
    assert summary.crawl_failed == 0


def test_a_second_run_on_the_same_catalog_is_refused_and_a_later_run_is_not(
    tmp_path: Path,
) -> None:
    db = tmp_path / "locked.sqlite3"
    first = Catalog(db, 7)
    try:
        with pytest.raises(Exception, match="Another run uses the catalog"):
            Catalog(db, 7)
        with pytest.raises(Exception, match="Another run uses the catalog"):
            run(Site(), tmp_path / "{host}.sqlite3".replace("{host}", "locked"))
    finally:
        first.close()
    Catalog(db, 7).close()
