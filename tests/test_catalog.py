import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from civil_service.catalog import (
    CRAWL_FAILED,
    CRAWL_ROBOTS_BLOCKED,
    ArchiveState,
    Capture,
    Catalog,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
PAGE = "https://example.com/"


def test_catalog_seven_day_idempotency_and_reset_stale(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "catalog.sqlite3", rearchive_window_days=7)
    assert cat._conn.execute("PRAGMA busy_timeout;").fetchone() == (5000,)
    assert cat._conn.execute("PRAGMA synchronous;").fetchone() == (1,)  # NORMAL
    assert cat._conn.execute("PRAGMA journal_mode;").fetchone() == ("wal",)
    assert cat.get_entry(PAGE) is None
    assert cat.claim_next_crawl() is None
    assert cat.claim_next_archive() is None

    cat.seed_url(PAGE, now=NOW)
    assert cat.pending_counts() == (1, 0)
    assert cat.claim_next_crawl() == PAGE
    cat.record_crawl_result(PAGE, 200, now=NOW)
    assert cat.pending_counts() == (0, 1)

    assert cat.claim_next_archive() == PAGE
    cat.record_archive_result(PAGE, ArchiveState.ARCHIVED, 200, now=NOW)
    assert cat.pending_counts() == (0, 0)

    entry = cat.get_entry(PAGE)
    assert entry is not None
    assert entry.is_internal is True
    assert entry.archive_status == "ARCHIVED"

    # Re-seed inside the window changes nothing. After the window both queues refill.
    cat.seed_url(PAGE, now=NOW + timedelta(days=3))
    assert cat.claim_next_crawl() is None
    assert cat.claim_next_archive() is None

    cat.seed_url(PAGE, now=NOW + timedelta(days=8))
    assert cat.claim_next_crawl() == PAGE
    cat.reset_stale_in_progress()
    assert cat.claim_next_crawl() == PAGE
    cat.close()


def test_startup_reset_returns_interrupted_archive_rows_to_pending(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "interrupted.sqlite3", rearchive_window_days=7)
    cat.insert_discovered("https://other.org/x", is_internal=False, now=NOW)
    assert cat.claim_next_archive() == "https://other.org/x"
    assert cat.claim_next_archive() is None
    cat.reset_stale_in_progress()
    assert cat.claim_next_archive() == "https://other.org/x"
    cat.close()


def test_rediscovery_inside_the_window_changes_nothing(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "one_day.sqlite3", rearchive_window_days=1)
    cat.seed_url(PAGE, now=NOW)
    assert cat.claim_next_crawl() == PAGE
    cat.record_crawl_result(PAGE, 200, now=NOW)
    cat.insert_discovered(PAGE, is_internal=True, now=NOW + timedelta(hours=1))
    assert cat.pending_counts() == (0, 1)
    assert cat.claim_next_crawl() is None
    cat.close()


def test_failed_fetches_stay_eligible_and_only_gone_pages_skip_archiving(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "ext.sqlite3", rearchive_window_days=7)
    cat.insert_discovered("https://other.org/ref", is_internal=False, now=NOW)
    assert cat.claim_next_crawl() is None
    assert cat.claim_next_archive() == "https://other.org/ref"

    cat.insert_discovered("https://other.org/ref", is_internal=True, now=NOW)
    assert cat.claim_next_crawl() == "https://other.org/ref"

    statuses = (CRAWL_FAILED, CRAWL_ROBOTS_BLOCKED, 403, 429, 500, 503)
    for status in statuses:
        cat.insert_discovered(f"https://example.com/fail{status}", is_internal=True, now=NOW)
    for _ in statuses:
        url = cat.claim_next_crawl()
        assert url is not None
        cat.record_crawl_result(url, int(url.removeprefix("https://example.com/fail")), now=NOW)
    claimed = {cat.claim_next_archive() for _ in statuses}
    assert claimed == {f"https://example.com/fail{s}" for s in statuses}

    for status in (404, 410):
        url = f"https://example.com/gone-{status}"
        cat.insert_discovered(url, is_internal=True, now=NOW)
        assert cat.claim_next_crawl() == url
        cat.record_crawl_result(url, status, now=NOW)
        entry = cat.get_entry(url)
        assert entry is not None
        assert entry.archive_status == "SKIPPED"
    assert cat.claim_next_archive() is None
    cat.close()


def test_internal_url_waits_for_crawl_before_archiving(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "wait.sqlite3", rearchive_window_days=7)
    cat.insert_discovered(PAGE, is_internal=True, now=NOW)
    assert cat.claim_next_archive() is None
    assert cat.pending_counts() == (1, 0)
    cat.close()


def test_requeue_counts_attempts_and_orders_fresh_urls_first(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "retry.sqlite3", rearchive_window_days=7)
    cat.insert_discovered("https://example.com/a", is_internal=True, now=NOW)
    cat.insert_discovered("https://example.com/b", is_internal=True, now=NOW)
    first = cat.claim_next_crawl()
    assert first == "https://example.com/a"
    assert cat.requeue_crawl(first, max_attempts=3) is True
    assert cat.claim_next_crawl() == "https://example.com/b"
    assert cat.claim_next_crawl() == first
    assert cat.requeue_crawl(first, max_attempts=3) is True
    assert cat.claim_next_crawl() == first
    assert cat.requeue_crawl(first, max_attempts=3) is False
    entry = cat.get_entry(first)
    assert entry is not None
    assert entry.crawl_attempts == 2

    cat.insert_discovered("https://other.org/x", is_internal=False, now=NOW)
    cat.insert_discovered("https://other.org/y", is_internal=False, now=NOW)
    url = cat.claim_next_archive()
    assert url == "https://other.org/x"
    assert cat.requeue_archive(url, max_attempts=2) is True
    assert cat.claim_next_archive() == "https://other.org/y"
    assert cat.claim_next_archive() == url
    assert cat.requeue_archive(url, max_attempts=2) is False
    cat.close()


def test_expired_rows_reset_attempt_counters(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "reset.sqlite3", rearchive_window_days=7)
    cat.seed_url(PAGE, now=NOW)
    assert cat.claim_next_crawl() == PAGE
    assert cat.requeue_crawl(PAGE, max_attempts=3) is True
    assert cat.claim_next_crawl() == PAGE
    cat.record_crawl_result(PAGE, 200, now=NOW)
    assert cat.claim_next_archive() == PAGE
    assert cat.requeue_archive(PAGE, max_attempts=3) is True
    assert cat.claim_next_archive() == PAGE
    cat.record_archive_result(PAGE, ArchiveState.FAILED, None, now=NOW)

    cat.seed_url(PAGE, now=NOW + timedelta(days=8))
    entry = cat.get_entry(PAGE)
    assert entry is not None
    assert (entry.needs_crawl, entry.archive_status) == (1, "PENDING")
    assert (entry.crawl_attempts, entry.archive_attempts) == (0, 0)
    cat.close()


def test_archive_coverage_counts_fresh_snapshots(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "cov.sqlite3", rearchive_window_days=7)
    assert cat.archive_coverage(NOW) == (0, 0, 0)
    for name, state in (
        ("a", ArchiveState.ARCHIVED),
        ("b", ArchiveState.SUBMITTED),
        ("c", ArchiveState.FAILED),
    ):
        url = f"https://other.org/{name}"
        cat.insert_discovered(url, is_internal=False, now=NOW)
        assert cat.claim_next_archive() is not None
        cat.record_archive_result(url, state, 200, now=NOW)
    cat.insert_discovered("https://other.org/d", is_internal=False, now=NOW)
    cat.insert_discovered("https://example.com/gone", is_internal=True, now=NOW)
    assert cat.claim_next_crawl() == "https://example.com/gone"
    cat.record_crawl_result("https://example.com/gone", 404, now=NOW)
    assert cat.archive_coverage(NOW) == (1, 1, 4)
    assert cat.archive_coverage(NOW + timedelta(days=8)) == (0, 0, 4)
    cat.close()


def test_old_database_gains_attempt_columns_and_indexes(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE urls (url TEXT PRIMARY KEY, is_internal INTEGER NOT NULL, "
        "needs_crawl INTEGER NOT NULL, crawl_status INTEGER, crawled_at TEXT, "
        "archive_status TEXT NOT NULL, archive_http_status INTEGER, archived_at TEXT)"
    )
    conn.execute("INSERT INTO urls VALUES (?, 1, 1, NULL, NULL, 'PENDING', NULL, NULL)", (PAGE,))
    conn.commit()
    conn.close()

    cat = Catalog(db, rearchive_window_days=7)
    entry = cat.get_entry(PAGE)
    assert entry is not None
    assert (entry.crawl_attempts, entry.archive_attempts) == (0, 0)
    assert (entry.snapshot_ts, entry.snapshot_url) == (None, None)
    indexes = {row[1] for row in cat._conn.execute("PRAGMA index_list(urls)")}
    assert {"idx_crawl", "idx_archive"} <= indexes
    cat.close()
    Catalog(db, rearchive_window_days=7).close()


def test_catalog_concurrent_claims_never_duplicate(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "conc.sqlite3", rearchive_window_days=7)
    for i in range(10):
        cat.insert_discovered(f"https://example.com/{i}", is_internal=True)
        cat.insert_discovered(f"https://other.org/{i}", is_internal=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        crawls = list(pool.map(lambda _: cat.claim_next_crawl(), range(12)))
        archives = list(pool.map(lambda _: cat.claim_next_archive(), range(12)))
    claimed_crawls = [u for u in crawls if u is not None]
    claimed_archives = [u for u in archives if u is not None]
    assert len(claimed_crawls) == len(set(claimed_crawls)) == 10
    assert len(claimed_archives) == len(set(claimed_archives)) == 10
    cat.close()


def test_a_job_capture_is_stored_with_the_result_and_cleared_by_the_next_one(
    tmp_path: Path,
) -> None:
    cat = Catalog(tmp_path / "capture.sqlite3", rearchive_window_days=7)
    cat.insert_discovered("https://other.org/x", is_internal=False, now=NOW)
    assert cat.claim_next_archive() == "https://other.org/x"
    capture = Capture("20261116042144", "https://other.org/x")
    cat.record_archive_result("https://other.org/x", ArchiveState.ARCHIVED, 200, NOW, capture)
    entry = cat.get_entry("https://other.org/x")
    assert entry is not None
    assert (entry.snapshot_ts, entry.snapshot_url) == ("20261116042144", "https://other.org/x")
    cat.record_archive_result("https://other.org/x", ArchiveState.SUBMITTED, 200, NOW)
    entry = cat.get_entry("https://other.org/x")
    assert entry is not None
    assert (entry.snapshot_ts, entry.snapshot_url) == (None, None)
    cat.close()
