"""Two-queue crawl and Wayback archive runner with robots.txt and sitemap support."""

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from civil_service.catalog import CRAWL_FAILED, CRAWL_ROBOTS_BLOCKED, ArchiveState, Catalog
from civil_service.config import AppSettings, SpnCredentials
from civil_service.exceptions import (
    FetchError,
    StopRequested,
    TransientFetchError,
    UnsafeTargetError,
)
from civil_service.fetcher import Request, TieredFetcher
from civil_service.urls import (
    extract_links,
    is_same_domain,
    normalize_url,
    parse_sitemap,
    read_sitemap_bytes,
    validate_public_host,
)
from civil_service.wayback import Submission, WaybackClient

_LOGGER = structlog.get_logger(__name__)
_HTML_MIME = "text/html"
_PERCENT = 100.0
_HOST_PLACEHOLDER = "{host}"
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9.-]")


@dataclass(slots=True)
class CrawlSummary:
    """Counters summarizing one archiver run.

    crawled: pages that returned an HTTP answer (any status).
    robots_blocked: pages that robots.txt kept us from fetching. They still go to Wayback.
    crawl_failed: pages that we could not fetch. They still go to Wayback.
    archived: URLs that hold a confirmed fresh snapshot.
    submitted: URLs that archive.org accepted without a confirmed capture.
    archive_skipped: URLs that are not public, so no Wayback submission is possible.
    archive_failed: URLs that archive.org refused or could not process.
    covered, unconfirmed, eligible: catalog totals for the coverage line.
    """

    crawled: int = 0
    robots_blocked: int = 0
    crawl_failed: int = 0
    archived: int = 0
    submitted: int = 0
    archive_skipped: int = 0
    archive_failed: int = 0
    covered: int = 0
    unconfirmed: int = 0
    eligible: int = 0

    @property
    def coverage_percent(self) -> float:
        """Return the share of eligible URLs that hold a confirmed fresh snapshot."""
        return _PERCENT * self.covered / self.eligible if self.eligible else 0.0


def _bounded(pending: int, done: int, limit: int | None) -> int:
    """Return pending items, cut to the number of items a configured limit still allows."""
    return pending if limit is None else min(pending, max(0, limit - done))


class RobotsGuard:
    """Thread-safe per-origin robots.txt policy cache.

    An unreachable robots.txt allows every URL. The guard gates our own fetches only.
    A slow robots.txt holds back only the threads that wait for the same origin.
    """

    def __init__(self, user_agent: str, fetcher: TieredFetcher, enabled: bool) -> None:
        """Initialize robots.txt cache for user_agent."""
        self._user_agent = user_agent
        self._fetcher = fetcher
        self._enabled = enabled
        self._lock = threading.Lock()
        self._parsers: dict[str, RobotFileParser] = {}
        self._origin_locks: dict[str, threading.Lock] = {}

    def _load_parser(self, origin: str) -> RobotFileParser:
        parser = RobotFileParser()
        try:
            res = self._fetcher.fetch(Request(f"{origin}/robots.txt"))
        except StopRequested:
            raise
        except Exception:  # any failure to read robots.txt allows every URL
            parser.parse([])
            return parser
        parser.parse(res.text.splitlines() if res.status_code == HTTPStatus.OK else [])
        return parser

    def _parser_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            cached = self._parsers.get(origin)
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        if cached is not None:
            return cached
        with origin_lock:
            parser = self._parsers.get(origin)
            if parser is None:
                parser = self._load_parser(origin)
                with self._lock:
                    self._parsers[origin] = parser
            return parser

    def is_allowed(self, url: str) -> bool:
        """Return True when crawling url is permitted by robots.txt."""
        if not self._enabled:
            return True
        return self._parser_for(url).can_fetch(self._user_agent, url)

    def sitemaps(self, url: str) -> list[str]:
        """Return the Sitemap URLs that the robots.txt of url's origin lists."""
        return self._parser_for(url).site_maps() or []


class _ArchiverRunner:
    """Coordinator for parallel crawling and Wayback archiving workers."""

    _root_netloc: str
    _progress: Progress
    _crawl_task: TaskID
    _archive_task: TaskID

    def __init__(
        self,
        settings: AppSettings,
        catalog: Catalog,
        fetcher: TieredFetcher,
        wayback: WaybackClient,
    ) -> None:
        """Initialize thread synchronization primitives and domain collaborators."""
        self._settings = settings
        self._catalog = catalog
        self._fetcher = fetcher
        self._wayback = wayback
        self._robots = RobotsGuard(settings.user_agent, fetcher, settings.respect_robots_txt)
        self._allowed_schemes = tuple(settings.allowed_schemes)
        self._summary = CrawlSummary()
        self._cond = threading.Condition()
        self._active_crawls = 0
        self._claimed_crawls = 0
        self._claimed_archives = 0
        self._remaining_crawlers = settings.threads
        self._aborted = False
        self._stop = False
        self._crawl_done = False
        self._last_refresh = float("-inf")

    def _refresh_progress(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._cond:
            if not force and now - self._last_refresh < self._settings.progress_refresh_seconds:
                return
            self._last_refresh = now
            s = self._summary
            crawl_done = s.crawled + s.robots_blocked + s.crawl_failed
            archive_done = s.archived + s.submitted + s.archive_skipped + s.archive_failed
            crawl_detail = f"ok={s.crawled} skip={s.robots_blocked} fail={s.crawl_failed}"
            archive_detail = f"arch={s.archived} sub={s.submitted} fail={s.archive_failed}"
        raw_crawl, raw_archive = self._catalog.pending_counts()
        crawl_left = _bounded(raw_crawl, crawl_done, self._settings.max_crawl_pages)
        if archive_done == 0 and raw_archive == 0 and crawl_left > 0:
            raw_archive = 1
        archive_left = _bounded(raw_archive, archive_done, self._settings.max_archive_urls)
        self._progress.update(
            self._crawl_task,
            completed=crawl_done,
            total=crawl_done + crawl_left,
            left=crawl_left,
            detail=crawl_detail,
        )
        self._progress.update(
            self._archive_task,
            completed=archive_done,
            total=archive_done + archive_left,
            left=archive_left,
            detail=archive_detail,
        )

    def _record_canonical_target(
        self, original_url: str, effective_url: str, status_code: int
    ) -> None:
        """Record the URL that a redirect led to. A page on another domain is an external link."""
        if effective_url == original_url:
            return
        internal = is_same_domain(
            effective_url, self._root_netloc, self._settings.include_subdomains
        )
        self._catalog.insert_discovered(effective_url, is_internal=internal)
        if internal:
            self._catalog.record_crawl_result(effective_url, status_code)

    def _fail_crawl(self, url: str, exc: Exception) -> None:
        self._catalog.record_crawl_result(url, CRAWL_FAILED)
        with self._cond:
            self._summary.crawl_failed += 1
        _LOGGER.warning("crawl_failed", url=url, error=str(exc))

    def _crawl_one(self, url: str) -> bool:
        """Crawl one URL. Return True when the URL went back to the queue for a retry."""
        if not self._robots.is_allowed(url):
            self._catalog.record_crawl_result(url, CRAWL_ROBOTS_BLOCKED)
            with self._cond:
                self._summary.robots_blocked += 1
            _LOGGER.info("robots_disallowed", url=url)
            return False

        try:
            res = self._fetcher.fetch(Request(url))
        except TransientFetchError as exc:
            if self._catalog.requeue_crawl(url, self._settings.max_transient_attempts):
                _LOGGER.info("crawl_retry", url=url, error=str(exc))
                return True
            self._fail_crawl(url, exc)
            return False
        except StopRequested:
            raise
        except Exception as exc:  # FetchError, UnsafeTargetError, or a surprise: never end the run
            self._fail_crawl(url, exc)
            return False

        effective_url = res.final_url or url
        mime = res.content_type.lower().split(";", 1)[0].strip()
        on_site = is_same_domain(
            effective_url, self._root_netloc, self._settings.include_subdomains
        )
        # Save the links first and mark the page done last. A crash in between leaves the
        # page in progress, so the next run fetches it again and loses no link.
        if (
            on_site
            and HTTPStatus.OK <= res.status_code < HTTPStatus.MULTIPLE_CHOICES
            and mime == _HTML_MIME
        ):
            for link in extract_links(res.text, effective_url, self._allowed_schemes):
                internal = is_same_domain(
                    link, self._root_netloc, self._settings.include_subdomains
                )
                self._catalog.insert_discovered(link, is_internal=internal)
        self._record_canonical_target(url, effective_url, res.status_code)
        self._catalog.record_crawl_result(url, res.status_code)
        with self._cond:
            self._summary.crawled += 1
        return False

    def _claim_crawl_url(self) -> str | None:
        limit = self._settings.max_crawl_pages
        with self._cond:
            while True:
                if (
                    self._aborted
                    or self._stop
                    or (limit is not None and self._claimed_crawls >= limit)
                ):
                    return None
                url = self._catalog.claim_next_crawl()
                if url is not None:
                    self._claimed_crawls += 1
                    self._active_crawls += 1
                    return url
                if self._active_crawls == 0:
                    self._cond.notify_all()
                    return None
                self._cond.wait()

    def _crawl_worker(self) -> None:
        try:
            while (url := self._claim_crawl_url()) is not None:
                requeued = False
                try:
                    requeued = self._crawl_one(url)
                except StopRequested:
                    pass  # the row stays in progress and returns to pending at the next start
                finally:
                    with self._cond:
                        self._active_crawls -= 1
                        self._claimed_crawls -= requeued
                        self._cond.notify_all()
                    self._refresh_progress()
        except Exception:
            with self._cond:
                self._aborted = True
            raise
        finally:
            with self._cond:
                self._remaining_crawlers -= 1
                if self._remaining_crawlers == 0 or self._aborted:
                    self._crawl_done = True
                self._cond.notify_all()

    def _fail_archive(self, url: str, status: int | None, reason: object) -> None:
        self._catalog.record_archive_result(url, ArchiveState.FAILED, status)
        with self._cond:
            self._summary.archive_failed += 1
        _LOGGER.warning("archive_failed", url=url, error=str(reason))

    def _check_and_submit(self, url: str) -> Submission:
        archived, status = self._wayback.is_archived(url)
        if archived:
            return Submission(ArchiveState.ARCHIVED, status)
        return self._wayback.submit_url(url)

    def _archive_one(self, url: str) -> bool:
        """Archive one URL. Return True when the URL went back to the queue for a retry."""
        try:
            try:
                validate_public_host(url)  # an address that is not public is skipped, never sent
            except UnsafeTargetError as exc:
                self._catalog.record_archive_result(url, ArchiveState.SKIPPED, None)
                with self._cond:
                    self._summary.archive_skipped += 1
                _LOGGER.info("archive_skipped", url=url, error=str(exc))
                return False
            submission = self._check_and_submit(url)
        except TransientFetchError as exc:
            if self._catalog.requeue_archive(url, self._settings.max_transient_attempts):
                _LOGGER.info("archive_retry", url=url, error=str(exc))
                return True
            self._fail_archive(url, None, exc)
            return False
        except StopRequested:
            raise
        except Exception as exc:  # FetchError, ArchiveError, or a surprise: never end the run
            self._fail_archive(url, None, exc)
            return False

        state, status, capture = submission
        if state is ArchiveState.FAILED:
            self._fail_archive(url, status, f"Save Page Now answered HTTP {status}")
            return False
        self._catalog.record_archive_result(url, state, status, capture=capture)
        with self._cond:
            if state is ArchiveState.ARCHIVED:
                self._summary.archived += 1
            else:
                self._summary.submitted += 1
        return False

    def _claim_archive_url(self) -> str | None:
        limit = self._settings.max_archive_urls
        with self._cond:
            while True:
                if self._stop or (limit is not None and self._claimed_archives >= limit):
                    return None
                url = self._catalog.claim_next_archive()
                if url is not None:
                    self._claimed_archives += 1
                    return url
                if self._crawl_done:
                    return None
                self._cond.wait()

    def _archive_worker(self) -> None:
        try:
            while (url := self._claim_archive_url()) is not None:
                try:
                    requeued = self._archive_one(url)
                except StopRequested:
                    return  # the row stays in progress and returns to pending at the next start
                with self._cond:
                    self._claimed_archives -= requeued
                self._refresh_progress()
        except Exception:
            self._request_stop()  # a catalog or code error: end the whole run, never crawl on
            raise

    def _fetch_sitemap(self, url: str) -> str | None:
        try:
            res = self._fetcher.fetch(Request(url, max_bytes=self._settings.max_sitemap_bytes))
        except (FetchError, UnsafeTargetError) as exc:
            _LOGGER.info("sitemap_unavailable", url=url, error=str(exc))
            return None
        if res.status_code != HTTPStatus.OK:
            return None
        text = read_sitemap_bytes(res.body, self._settings.max_sitemap_bytes)
        if text is None:
            _LOGGER.warning("sitemap_rejected", url=url, limit=self._settings.max_sitemap_bytes)
        return text

    def _discover_sitemaps(self, seed_url: str) -> None:
        """Queue every URL that the sitemaps of the seed site list. Sitemap indexes nest."""
        parts = urlsplit(seed_url)
        listed = [*self._robots.sitemaps(seed_url), f"{parts.scheme}://{parts.netloc}/sitemap.xml"]
        pending = deque(dict.fromkeys(listed))
        seen = set(pending)
        for _ in range(self._settings.max_sitemaps):
            if not pending:
                return
            sitemap_url = pending.popleft()
            text = self._fetch_sitemap(sitemap_url)
            if text is None:
                continue
            is_index, locations = parse_sitemap(text)
            for location in locations:
                url = normalize_url(sitemap_url, location, self._allowed_schemes)
                internal = url is not None and is_same_domain(
                    url, self._root_netloc, self._settings.include_subdomains
                )
                if url is None or (is_index and not internal):
                    continue
                if not is_index:
                    self._catalog.insert_discovered(url, is_internal=internal)
                elif url not in seen:
                    seen.add(url)
                    pending.append(url)

    def _request_stop(self) -> None:
        """Ask every worker to finish its current URL and claim no more."""
        self._fetcher.request_stop()
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def run(self, seed_url: str, console: Console | None) -> CrawlSummary:
        """Discover sitemaps, then run crawl and archive pools until both queues drain."""
        self._root_netloc = urlsplit(seed_url).netloc
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description:<10}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn(
                "{task.completed}/{task.total} done ({task.fields[left]} left)"
                " | {task.fields[detail]}"
            ),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            self._progress = progress
            self._crawl_task = progress.add_task(
                "Crawling", total=1, completed=0, left=1, detail="ok=0 skip=0 fail=0"
            )
            self._archive_task = progress.add_task(
                "Archiving", total=1, completed=0, left=1, detail="arch=0 sub=0 fail=0"
            )
            self._refresh_progress(force=True)
            self._discover_sitemaps(seed_url)
            crawl_pool = ThreadPoolExecutor(max_workers=self._settings.threads)
            archive_pool = ThreadPoolExecutor(max_workers=self._settings.archive_threads)
            try:
                futures = [
                    *(crawl_pool.submit(self._crawl_worker) for _ in range(self._settings.threads)),
                    *(
                        archive_pool.submit(self._archive_worker)
                        for _ in range(self._settings.archive_threads)
                    ),
                ]
                for future in wait(futures, return_when=FIRST_EXCEPTION).done:
                    future.result()
            except KeyboardInterrupt:
                self._request_stop()
                raise
            finally:
                # Join every worker before the caller closes the catalog.
                try:
                    crawl_pool.shutdown(wait=True)
                    archive_pool.shutdown(wait=True)
                except KeyboardInterrupt:
                    self._request_stop()
                    raise
            self._refresh_progress(force=True)
        totals = self._catalog.archive_coverage()
        self._summary.covered, self._summary.unconfirmed, self._summary.eligible = totals
        return self._summary


def catalog_path(db_path: Path, seed_url: str) -> Path:
    """Return db_path with {host} replaced by the seed host, so each site has its own catalog."""
    host = _UNSAFE_FILENAME_CHARS.sub("_", urlsplit(seed_url).netloc)
    return Path(str(db_path).replace(_HOST_PLACEHOLDER, host))


def run_archiver(
    start_url: str,
    settings: AppSettings,
    console: Console | None = None,
    fetcher: TieredFetcher | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> CrawlSummary:
    """Validate start_url, resume catalog state, and archive discovered URLs.

    Args:
        start_url: Seed URL to begin same-domain crawling.
        settings: Validated application settings.
        console: Optional Rich console for progress output.
        fetcher: Optional pre-configured TieredFetcher for testing.
        sleep_fn: Sleep callable for Wayback pacing and polling. Default: interruptible sleep.

    Returns:
        Summary counts of the crawl and archive results, and the coverage totals.

    Raises:
        UnsafeTargetError: If start_url uses a disallowed scheme or non-public IP.
    """
    normalized = normalize_url(start_url, start_url, tuple(settings.allowed_schemes))
    if normalized is None:
        msg = f"Invalid or disallowed seed URL: {start_url}"
        raise UnsafeTargetError(msg)
    validate_public_host(normalized)

    catalog = Catalog(catalog_path(settings.db_path, normalized), settings.rearchive_window_days)
    owns_fetcher = fetcher is None
    active_fetcher = fetcher if fetcher is not None else TieredFetcher(settings)
    try:
        catalog.reset_stale_in_progress()
        catalog.seed_url(normalized)
        wayback = WaybackClient(active_fetcher, settings, SpnCredentials(), sleep_fn)
        return _ArchiverRunner(settings, catalog, active_fetcher, wayback).run(normalized, console)
    finally:
        catalog.close()
        if owns_fetcher:
            active_fetcher.close()
