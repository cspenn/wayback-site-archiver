"""Thread-safe SQLite WAL catalog for crawl and archive state."""

import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import IO

from civil_service.exceptions import CatalogBusyError

if sys.platform != "win32":  # pragma: no branch
    import fcntl

_BUSY_TIMEOUT_MS = 5000
# crawl_status values that are not HTTP statuses.
CRAWL_FAILED = 0
CRAWL_ROBOTS_BLOCKED = -1

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS urls (
    url TEXT PRIMARY KEY,
    is_internal INTEGER NOT NULL,
    needs_crawl INTEGER NOT NULL,
    crawl_status INTEGER,
    crawled_at TEXT,
    archive_status TEXT NOT NULL,
    archive_http_status INTEGER,
    archived_at TEXT,
    crawl_attempts INTEGER NOT NULL DEFAULT 0,
    archive_attempts INTEGER NOT NULL DEFAULT 0
);
"""
_ATTEMPT_COLUMNS = ("crawl_attempts", "archive_attempts")
_CREATE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_crawl ON urls (needs_crawl, crawl_attempts)",
    "CREATE INDEX IF NOT EXISTS idx_archive ON urls (archive_status, archive_attempts)",
)
_COLUMNS = (
    "url, is_internal, needs_crawl, crawl_status, crawled_at, archive_status, "
    "archive_http_status, archived_at, crawl_attempts, archive_attempts"
)
# An internal URL waits for its crawl so the catalog knows whether the page is gone (404/410).
_ARCHIVE_ELIGIBLE = "(is_internal = 0 OR (needs_crawl = 0 AND crawl_status NOT IN (404, 410)))"

_RECRAWL = (
    "excluded.is_internal = 1 AND needs_crawl = 0 AND (crawled_at IS NULL OR crawled_at <= :cutoff)"
)
_REARCHIVE = (
    "archive_status NOT IN (:pending, :in_progress) AND "
    "(COALESCE(archived_at, crawled_at) IS NULL OR COALESCE(archived_at, crawled_at) <= :cutoff)"
)
_UPSERT_SQL = f"""
INSERT INTO urls (url, is_internal, needs_crawl, archive_status)
VALUES (:url, :internal, :initial_crawl, :pending)
ON CONFLICT (url) DO UPDATE SET
    is_internal = MAX(is_internal, excluded.is_internal),
    needs_crawl = CASE WHEN {_RECRAWL} THEN :pending_crawl ELSE needs_crawl END,
    crawl_attempts = CASE WHEN {_RECRAWL} THEN 0 ELSE crawl_attempts END,
    archive_status = CASE WHEN {_REARCHIVE} THEN :pending ELSE archive_status END,
    archive_attempts = CASE WHEN {_REARCHIVE} THEN 0 ELSE archive_attempts END
"""  # noqa: S608  # nosec B608 - module constants only, values bind as parameters


class CrawlState(IntEnum):
    """Crawl queue state values stored in SQLite."""

    DONE = 0
    PENDING = 1
    IN_PROGRESS = 2


class ArchiveState(StrEnum):
    """Wayback archive state values stored in SQLite."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    ARCHIVED = "ARCHIVED"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    """Snapshot of one catalog row."""

    url: str
    is_internal: bool
    needs_crawl: int
    crawl_status: int | None
    crawled_at: str | None
    archive_status: str
    archive_http_status: int | None
    archived_at: str | None
    crawl_attempts: int
    archive_attempts: int


class Catalog:
    """Thread-safe SQLite WAL catalog enforcing the re-archive window."""

    def __init__(self, db_path: Path, rearchive_window_days: int) -> None:
        """Open WAL SQLite database at db_path, ensure the schema, and add new columns."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._lock_catalog(db_path)
        self._rearchive_window = timedelta(days=rearchive_window_days)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
            self._conn.execute("PRAGMA synchronous=NORMAL;")  # safe in WAL mode, far fewer syncs
            self._conn.execute(_CREATE_TABLE_SQL)
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(urls)")}
            for column in _ATTEMPT_COLUMNS:
                if column not in existing:
                    self._conn.execute(
                        f"ALTER TABLE urls ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            for statement in _CREATE_INDEXES_SQL:
                self._conn.execute(statement)

    @staticmethod
    def _lock_catalog(db_path: Path) -> IO[str]:
        """Hold an exclusive lock on the catalog until close. One run for each catalog.

        Two runs on one catalog would reset each other's in-progress rows and repeat work.
        The lock does not exist on Windows.
        """
        lock_file = db_path.with_name(db_path.name + ".lock").open("w")  # noqa: SIM115
        if sys.platform != "win32":  # pragma: no branch
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                lock_file.close()
                msg = f"Another run uses the catalog {db_path}. Wait for it, or stop it first."
                raise CatalogBusyError(msg) from exc
        return lock_file

    @staticmethod
    def _stamp(now: datetime | None) -> str:
        """Return now (or the current UTC time) as a fixed-width ISO string that sorts by time."""
        return (now or datetime.now(UTC)).isoformat(timespec="microseconds")

    def get_entry(self, url: str) -> CatalogEntry | None:
        """Return the catalog row for url, or None if absent."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM urls WHERE url = ?",  # noqa: S608  # nosec B608
                (url,),
            ).fetchone()
        return None if row is None else CatalogEntry(row[0], bool(row[1]), *row[2:])

    def seed_url(self, url: str, now: datetime | None = None) -> None:
        """Queue or re-queue the seed URL as an internal crawl target."""
        self.insert_discovered(url, is_internal=True, now=now)

    def insert_discovered(self, url: str, is_internal: bool, now: datetime | None = None) -> None:
        """Insert a discovered URL, or re-queue it when its last work is outside the window."""
        cutoff = self._stamp((now or datetime.now(UTC)) - self._rearchive_window)
        with self._lock, self._conn:
            self._conn.execute(
                _UPSERT_SQL,
                {
                    "url": url,
                    "internal": int(is_internal),
                    "initial_crawl": CrawlState.PENDING if is_internal else CrawlState.DONE,
                    "pending_crawl": CrawlState.PENDING,
                    "pending": ArchiveState.PENDING,
                    "in_progress": ArchiveState.IN_PROGRESS,
                    "cutoff": cutoff,
                },
            )

    def claim_next_crawl(self) -> str | None:
        """Atomically claim the next pending internal crawl URL, fewest attempts first."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "UPDATE urls SET needs_crawl = :running WHERE url = ("
                "SELECT url FROM urls WHERE needs_crawl = :pending "
                "ORDER BY crawl_attempts LIMIT 1) RETURNING url",
                {"running": CrawlState.IN_PROGRESS, "pending": CrawlState.PENDING},
            ).fetchall()
        return str(rows[0][0]) if rows else None

    def record_crawl_result(self, url: str, status_code: int, now: datetime | None = None) -> None:
        """Persist the crawl HTTP status. Status 404 or 410 skips archiving."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE urls SET needs_crawl = :done, crawl_status = :status, crawled_at = :now, "
                "archive_status = CASE WHEN :status IN (404, 410) THEN :skipped "
                "ELSE archive_status END WHERE url = :url",
                {
                    "done": CrawlState.DONE,
                    "status": status_code,
                    "now": self._stamp(now),
                    "skipped": ArchiveState.SKIPPED,
                    "url": url,
                },
            )

    def requeue_crawl(self, url: str, max_attempts: int) -> bool:
        """Put a claimed URL back in the crawl queue. Return False when attempts are used up."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE urls SET needs_crawl = :pending, crawl_attempts = crawl_attempts + 1 "
                "WHERE url = :url AND crawl_attempts + 1 < :max",
                {"pending": CrawlState.PENDING, "url": url, "max": max_attempts},
            )
            return cursor.rowcount == 1

    def claim_next_archive(self) -> str | None:
        """Atomically claim the next eligible URL for Wayback checking, fewest attempts first."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "UPDATE urls SET archive_status = :running WHERE url = ("  # noqa: S608
                f"SELECT url FROM urls WHERE archive_status = :pending AND {_ARCHIVE_ELIGIBLE} "  # nosec B608
                "ORDER BY archive_attempts LIMIT 1) RETURNING url",
                {"running": ArchiveState.IN_PROGRESS, "pending": ArchiveState.PENDING},
            ).fetchall()
        return str(rows[0][0]) if rows else None

    def record_archive_result(
        self,
        url: str,
        archive_status: ArchiveState,
        http_status: int | None,
        now: datetime | None = None,
    ) -> None:
        """Persist Wayback availability or submission outcome."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE urls SET archive_status = ?, archive_http_status = ?, archived_at = ? "
                "WHERE url = ?",
                (archive_status, http_status, self._stamp(now), url),
            )

    def requeue_archive(self, url: str, max_attempts: int) -> bool:
        """Put a claimed URL back in the archive queue. Return False when attempts are used up."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE urls SET archive_status = :pending, "
                "archive_attempts = archive_attempts + 1 "
                "WHERE url = :url AND archive_attempts + 1 < :max",
                {"pending": ArchiveState.PENDING, "url": url, "max": max_attempts},
            )
            return cursor.rowcount == 1

    def reset_stale_in_progress(self) -> None:
        """Restore in-progress rows left by an interrupted run."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE urls SET needs_crawl = ? WHERE needs_crawl = ?",
                (CrawlState.PENDING, CrawlState.IN_PROGRESS),
            )
            self._conn.execute(
                "UPDATE urls SET archive_status = ? WHERE archive_status = ?",
                (ArchiveState.PENDING, ArchiveState.IN_PROGRESS),
            )

    def pending_counts(self) -> tuple[int, int]:
        """Return counts of (remaining_crawl, remaining_archive) items."""
        with self._lock:
            crawl_row = self._conn.execute(
                "SELECT COUNT(*) FROM urls WHERE needs_crawl IN (?, ?)",
                (CrawlState.PENDING, CrawlState.IN_PROGRESS),
            ).fetchone()
            archive_row = self._conn.execute(
                "SELECT COUNT(*) FROM urls WHERE archive_status IN (?, ?) "  # noqa: S608
                f"AND {_ARCHIVE_ELIGIBLE}",  # nosec B608
                (ArchiveState.PENDING, ArchiveState.IN_PROGRESS),
            ).fetchone()
            return (int(crawl_row[0]), int(archive_row[0]))

    def archive_coverage(self, now: datetime | None = None) -> tuple[int, int, int]:
        """Return (covered, unconfirmed, eligible) URL counts for the end-of-run coverage.

        Eligible rows are every row except SKIPPED. Covered rows hold an ARCHIVED result inside
        the re-archive window. Unconfirmed rows hold a SUBMITTED result inside the window.
        """
        cutoff = self._stamp((now or datetime.now(UTC)) - self._rearchive_window)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FILTER "
                "(WHERE archive_status = :archived AND archived_at > :cutoff), "
                "COUNT(*) FILTER "
                "(WHERE archive_status = :submitted AND archived_at > :cutoff), "
                "COUNT(*) FROM urls WHERE archive_status != :skipped",
                {
                    "archived": ArchiveState.ARCHIVED,
                    "submitted": ArchiveState.SUBMITTED,
                    "skipped": ArchiveState.SKIPPED,
                    "cutoff": cutoff,
                },
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def close(self) -> None:
        """Close the SQLite database connection."""
        with self._lock:
            self._conn.close()
        self._lock_file.close()
