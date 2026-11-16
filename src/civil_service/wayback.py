"""Wayback Machine availability checker and Save Page Now submission client."""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, NamedTuple
from urllib.parse import quote, urlencode

import orjson
import structlog

from civil_service.catalog import ArchiveState, Capture
from civil_service.config import AppSettings, SpnCredentials
from civil_service.exceptions import ArchiveError, StopRequested, TransientFetchError
from civil_service.fetcher import FetchResult, Request, TieredFetcher

_LOGGER = structlog.get_logger(__name__)
_WAYBACK_TS_FORMAT = "%Y%m%d%H%M%S"
# status_ext values (without the "error:" prefix) that a later try can fix. Any other error is
# final for this URL: the page is blocked, gone, or over its daily capture limit.
_TRANSIENT_JOB_ERRORS = frozenset(
    {
        "user-session-limit",
        "too-many-requests",
        "service-unavailable",
        "bad-gateway",
        "gateway-timeout",
        "read-timeout",
        "browsing-timeout",
        "soft-time-limit-exceeded",
        "no-browsers-available",
        "cannot-fetch",
        "celery",
        "job-failed",
        "capture-location-error",
        "internal-server-error",
        "proxy-error",
        "protocol-error",
    }
)
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_JSON_ACCEPT = "application/json"


def _is_fresh_snapshot(
    closest: dict[str, object],
    max_age_days: int | None,
    current_time: datetime,
) -> bool:
    if not closest.get("available"):
        return False
    if max_age_days is None:
        return True
    raw_ts = str(closest.get("timestamp", ""))
    try:
        snapshot_dt = datetime.strptime(raw_ts, _WAYBACK_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return (current_time - snapshot_dt) < timedelta(days=max_age_days)


class Submission(NamedTuple):
    """The result of one Save Page Now submission."""

    state: ArchiveState
    status: int
    capture: Capture | None = None


def _text(value: object) -> str | None:
    """Return value when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _raise_if_transient(payload: dict[str, Any], job_id: str | None) -> None:
    """Log a failed job and raise TransientFetchError when its error is a temporary one."""
    status_ext = _text(payload.get("status_ext"))
    _LOGGER.warning(
        "archive_job",
        job_id=job_id,
        status="error",
        status_ext=status_ext,
        message=_text(payload.get("message")),
    )
    if status_ext is not None and status_ext.removeprefix("error:") in _TRANSIENT_JOB_ERRORS:
        msg = f"Save Page Now reported a temporary error: {status_ext}"
        raise TransientFetchError(msg)


def _parse_json(res: FetchResult, url: str) -> dict[str, Any]:
    """Return the JSON object in a Wayback response body."""
    try:
        payload = orjson.loads(res.body)
    except orjson.JSONDecodeError as exc:
        msg = f"Invalid JSON from Wayback for {url}"
        raise ArchiveError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"Unexpected JSON shape from Wayback for {url}"
        raise ArchiveError(msg)
    return payload


class WaybackClient:
    """Client for Wayback Machine availability and Save Page Now endpoints.

    With API keys in the environment, the client uses the authenticated SPN2 API and
    polls each job, so ARCHIVED means a confirmed capture. Without keys it uses the
    anonymous save URL, and SUBMITTED means archive.org accepted the request.
    """

    def __init__(
        self,
        fetcher: TieredFetcher,
        settings: AppSettings,
        credentials: SpnCredentials | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind a TieredFetcher, settings, optional keys, a sleep function, and a clock.

        Without a sleep_fn, waits use the fetcher's interruptible sleep.
        """
        self._fetcher = fetcher
        self._settings = settings
        self._authorization = credentials.authorization if credentials is not None else None
        self._sleep = sleep_fn or fetcher.interruptible_sleep
        self._clock = clock
        self._slot_lock = threading.Lock()
        self._next_slot = float("-inf")

    def _wait_for_slot(self) -> None:
        """Reserve the next submission slot and sleep until it opens.

        All workers share the slots, so the submission rate holds for any worker count.
        """
        delay = (
            self._settings.archive_delay_seconds
            if self._authorization is None
            else self._settings.spn2_delay_seconds
        )
        with self._slot_lock:
            now = self._clock()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + delay
        if wait > 0:
            self._sleep(wait)

    def is_archived(self, url: str, now: datetime | None = None) -> tuple[bool, int]:
        """Check whether url has an acceptable snapshot on archive.org.

        The query carries the current time, so the closest snapshot is the newest one.

        Args:
            url: Target URL to query.
            now: Optional UTC timestamp for deterministic age checks.

        Returns:
            Tuple of (is_available_and_fresh, http_status_code).

        Raises:
            ArchiveError: If the availability API returns non-2xx or invalid JSON.
        """
        current_time = now if now is not None else datetime.now(UTC)
        query = urlencode({"url": url, "timestamp": current_time.strftime(_WAYBACK_TS_FORMAT)})
        res = self._fetcher.fetch(
            Request(f"{self._settings.wayback_avail_url}?{query}", escalate_on_403=False)
        )
        if not (HTTPStatus.OK <= res.status_code < HTTPStatus.MULTIPLE_CHOICES):
            msg = f"Wayback availability returned HTTP {res.status_code} for {url}"
            raise ArchiveError(msg)
        snapshots = _parse_json(res, url).get("archived_snapshots", {})
        closest = snapshots.get("closest", {}) if isinstance(snapshots, dict) else {}
        if not isinstance(closest, dict):
            return False, res.status_code
        fresh = _is_fresh_snapshot(closest, self._settings.wayback_max_age_days, current_time)
        return fresh, res.status_code

    def submit_url(self, url: str) -> Submission:
        """Submit url to Save Page Now. Return the archive state, HTTP status, and capture."""
        self._wait_for_slot()
        if self._authorization is None:
            return self._submit_anonymous(url)
        return self._submit_spn2(url, self._authorization)

    def _submit_anonymous(self, url: str) -> Submission:
        save_endpoint = f"{self._settings.wayback_save_url.rstrip('/')}/{url}"
        res = self._fetcher.fetch(
            Request(save_endpoint, follow_redirects=False, escalate_on_403=False)
        )
        accepted = HTTPStatus.OK <= res.status_code < HTTPStatus.BAD_REQUEST
        return Submission(
            ArchiveState.SUBMITTED if accepted else ArchiveState.FAILED, res.status_code
        )

    def _submit_spn2(self, url: str, authorization: str) -> Submission:
        headers = {"Accept": _JSON_ACCEPT, "Authorization": authorization}
        res = self._fetcher.fetch(
            Request(
                self._settings.spn2_save_url,
                method="POST",
                headers={**headers, "Content-Type": _FORM_CONTENT_TYPE},
                body=urlencode({"url": url}).encode("utf-8"),
                follow_redirects=False,
                escalate_on_403=False,
            )
        )
        if not (HTTPStatus.OK <= res.status_code < HTTPStatus.MULTIPLE_CHOICES):
            return Submission(ArchiveState.FAILED, res.status_code)
        payload = _parse_json(res, url)
        job_id = _text(payload.get("job_id"))
        if job_id is None:
            _raise_if_transient(payload, None)
            return Submission(ArchiveState.FAILED, res.status_code)
        return self._poll_job(job_id, headers, res.status_code)

    def _poll_job(self, job_id: str, headers: dict[str, str], submit_status: int) -> Submission:
        """Poll an SPN2 job. A job still pending after the last poll counts as SUBMITTED.

        A success counts as ARCHIVED only when the job reports a snapshot time. A temporary
        job error raises TransientFetchError, so the caller tries the URL again later.

        A transient failure, an unreadable reply, or a stop request while polling also gives
        SUBMITTED. The job exists, so the tool must not post it again.
        """
        status_url = f"{self._settings.spn2_status_url}{quote(job_id, safe='')}"
        for attempt in range(self._settings.spn2_max_polls):
            try:
                if attempt:
                    self._sleep(self._settings.spn2_poll_seconds)
                res = self._fetcher.fetch(
                    Request(
                        status_url, headers=headers, follow_redirects=False, escalate_on_403=False
                    )
                )
                payload = _parse_json(res, status_url)
            except (TransientFetchError, StopRequested, ArchiveError):
                break
            state = payload.get("status")
            if state == "success":
                capture = Capture(
                    _text(payload.get("timestamp")), _text(payload.get("original_url"))
                )
                _LOGGER.info(
                    "archive_job",
                    job_id=job_id,
                    status="success",
                    timestamp=capture.timestamp,
                    original_url=capture.original_url,
                )
                if capture.timestamp is None:  # no snapshot time: not proof of a capture
                    return Submission(ArchiveState.SUBMITTED, res.status_code)
                return Submission(ArchiveState.ARCHIVED, res.status_code, capture)
            if state == "error":
                _raise_if_transient(payload, job_id)
                return Submission(ArchiveState.FAILED, res.status_code)
        _LOGGER.info("archive_job", job_id=job_id, status="pending")
        return Submission(ArchiveState.SUBMITTED, submit_status)
