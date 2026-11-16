from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import structlog

from civil_service.catalog import ArchiveState, Capture
from civil_service.config import AppSettings, SpnCredentials
from civil_service.exceptions import (
    ArchiveError,
    BlockedError,
    StopRequested,
    TransientFetchError,
)
from civil_service.fetcher import FetchResult, Request, TieredFetcher
from civil_service.wayback import _TRANSIENT_JOB_ERRORS, Submission, WaybackClient

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
JSON = "application/json"


def outcome(submission: Submission) -> tuple[ArchiveState, int]:
    return submission.state, submission.status


def json_result(body: bytes, status: int = 200) -> FetchResult:
    return FetchResult(status, {}, body, JSON)


def build(
    transport: object,
    credentials: SpnCredentials | None = None,
    slept: list[float] | None = None,
    clock: Callable[[], float] = lambda: 0.0,
    **settings: object,
) -> WaybackClient:
    config = AppSettings(fallbacks=["curl"], **settings)  # type: ignore[arg-type]
    fetcher = TieredFetcher(config, transports={"curl": transport})  # type: ignore[arg-type]
    return WaybackClient(
        fetcher,
        config,
        credentials,
        sleep_fn=(slept if slept is not None else []).append,
        clock=clock,
    )


def spn_credentials(monkeypatch: pytest.MonkeyPatch) -> SpnCredentials:
    monkeypatch.setenv("CIVIL_SERVICE_SPN_ACCESS_KEY", "access")
    monkeypatch.setenv("CIVIL_SERVICE_SPN_SECRET_KEY", "s3cret")
    return SpnCredentials()


def test_anonymous_client_checks_availability_with_timestamp_and_submits_by_get() -> None:
    calls: list[Request] = []

    def transport(req: Request) -> FetchResult:
        calls.append(req)
        if "available" in req.url:
            return json_result(b'{"archived_snapshots": {}}')
        return FetchResult(200, {}, b"saved", "text/html")

    client = build(transport)
    assert client.is_archived("https://example.com/page", now=NOW) == (False, 200)
    assert outcome(client.submit_url("https://example.com/page")) == (ArchiveState.SUBMITTED, 200)
    avail, save = calls
    assert avail.url == (
        "https://archive.org/wayback/available?url=https%3A%2F%2Fexample.com%2Fpage"
        "&timestamp=20261115120000"
    )
    assert (avail.method, avail.escalate_on_403) == ("GET", False)
    assert save.url == "https://web.archive.org/save/https://example.com/page"
    assert (save.method, save.follow_redirects, save.escalate_on_403) == ("GET", False, False)


def test_availability_uses_the_clock_when_no_time_is_given() -> None:
    seen: list[str] = []

    def transport(req: Request) -> FetchResult:
        seen.append(req.url)
        return json_result(b"{}")

    assert build(transport).is_archived("https://example.com/") == (False, 200)
    assert "timestamp=20" in seen[0]


def test_snapshot_age_and_error_cases() -> None:
    template = b'{"archived_snapshots":{"closest":{"available":true,"timestamp":"%s"}}}'
    payloads = {
        "fresh": template % b"20261110120000",
        "stale": template % b"20260101000000",
        "badts": template % b"not-a-date",
        "weird": b'{"archived_snapshots":{"closest":null}}',
        "badjson": b"{bad",
        "notdict": b"[1, 2]",
    }

    def transport(req: Request) -> FetchResult:
        if "save/" in req.url:
            return FetchResult(400, {}, b"bad", "text/plain")
        if "http404" in req.url:
            return FetchResult(404, {}, b"missing", "text/plain")
        for key, body in payloads.items():
            if key in req.url:
                return json_result(body)
        raise AssertionError(req.url)

    no_age = build(transport, wayback_max_age_days=None)
    assert no_age.is_archived("https://example.com/stale", now=NOW) == (True, 200)

    aged = build(transport, wayback_max_age_days=30)
    assert aged.is_archived("https://example.com/fresh", now=NOW) == (True, 200)
    assert aged.is_archived("https://example.com/stale", now=NOW) == (False, 200)
    assert aged.is_archived("https://example.com/badts", now=NOW) == (False, 200)
    assert aged.is_archived("https://example.com/weird", now=NOW) == (False, 200)
    assert outcome(aged.submit_url("https://example.com/bad")) == (ArchiveState.FAILED, 400)
    for bad in ("badjson", "notdict", "http404"):
        with pytest.raises(ArchiveError):
            aged.is_archived(f"https://example.com/{bad}", now=NOW)


def test_default_freshness_window_is_seven_days() -> None:
    body = b'{"archived_snapshots":{"closest":{"available":true,"timestamp":"%s"}}}'

    def transport(req: Request) -> FetchResult:
        stamp = b"20261110120000" if "recent" in req.url else b"20261101120000"
        return json_result(body % stamp)

    client = build(transport)
    assert client.is_archived("https://example.com/recent", now=NOW) == (True, 200)
    assert client.is_archived("https://example.com/old", now=NOW) == (False, 200)


def test_submissions_share_one_paced_rate_and_availability_checks_do_not_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    slept: list[float] = []

    def transport(_req: Request) -> FetchResult:
        return json_result(b'{"archived_snapshots": {}}')

    client = build(transport, slept=slept, clock=lambda: now[0], archive_delay_seconds=4.0)
    client.is_archived("https://example.com/a")
    client.is_archived("https://example.com/b")
    assert slept == []
    client.submit_url("https://example.com/a")
    client.submit_url("https://example.com/b")
    client.submit_url("https://example.com/c")
    assert slept == [4.0, 8.0]
    now[0] = 100.0
    client.submit_url("https://example.com/d")
    assert slept == [4.0, 8.0]

    spn_slept: list[float] = []
    spn = build(
        lambda _r: json_result(b"{}", status=400),
        spn_credentials(monkeypatch),
        spn_slept,
        spn2_delay_seconds=9.0,
    )
    spn.submit_url("https://example.com/a")
    spn.submit_url("https://example.com/b")
    assert spn_slept == [9.0]

    quiet: list[float] = []
    free = build(transport, slept=quiet, archive_delay_seconds=0.0)
    free.submit_url("https://example.com/a")
    free.submit_url("https://example.com/b")
    assert quiet == []


def test_spn2_posts_with_authorization_and_polls_until_the_job_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Request] = []
    statuses = iter([b'{"status":"pending"}', b'{"status":"success","timestamp":"2026"}'])

    def transport(req: Request) -> FetchResult:
        calls.append(req)
        if req.method == "POST":
            return json_result(b'{"url":"https://example.com/p","job_id":"job/1 2"}')
        return json_result(next(statuses))

    slept: list[float] = []
    client = build(transport, spn_credentials(monkeypatch), slept, spn2_poll_seconds=5.0)
    assert outcome(client.submit_url("https://example.com/p")) == (ArchiveState.ARCHIVED, 200)
    post, poll1, poll2 = calls
    assert post.url == "https://web.archive.org/save"
    assert post.headers["Authorization"] == "LOW access:s3cret"
    assert post.headers["Accept"] == "application/json"
    assert post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert post.body == b"url=https%3A%2F%2Fexample.com%2Fp"
    assert (post.follow_redirects, post.escalate_on_403) == (False, False)
    assert (poll1.follow_redirects, poll1.escalate_on_403) == (False, False)
    assert poll1.url == poll2.url == "https://web.archive.org/save/status/job%2F1%202"
    assert poll1.headers["Authorization"] == "LOW access:s3cret"
    assert slept == [5.0]


def test_spn2_job_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    creds = spn_credentials(monkeypatch)

    def job_transport(job_status: bytes) -> object:
        def transport(req: Request) -> FetchResult:
            if req.method == "POST":
                return json_result(b'{"job_id":"abc"}', status=200)
            return json_result(job_status)

        return transport

    error = build(job_transport(b'{"status":"error","status_ext":"error:blocked"}'), creds)
    assert outcome(error.submit_url("https://example.com/")) == (ArchiveState.FAILED, 200)

    slept: list[float] = []
    pending = build(
        job_transport(b'{"status":"pending"}'),
        creds,
        slept,
        spn2_max_polls=3,
        spn2_poll_seconds=2.0,
    )
    assert outcome(pending.submit_url("https://example.com/")) == (ArchiveState.SUBMITTED, 200)
    assert slept == [2.0, 2.0]

    rejected = build(lambda _r: json_result(b'{"message":"bad"}', status=400), creds)
    assert outcome(rejected.submit_url("https://example.com/")) == (ArchiveState.FAILED, 400)
    no_job = build(lambda _r: json_result(b'{"message":"recent capture"}'), creds)
    assert outcome(no_job.submit_url("https://example.com/")) == (ArchiveState.FAILED, 200)
    garbled = build(lambda _r: json_result(b"<html>"), creds)
    with pytest.raises(ArchiveError):
        garbled.submit_url("https://example.com/")


def test_spn2_transient_error_while_polling_keeps_the_job_and_never_posts_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[Request] = []

    def transport(req: Request) -> FetchResult:
        if req.method == "POST":
            posts.append(req)
            return json_result(b'{"job_id":"abc"}')
        raise TransientFetchError("reset")

    client = build(transport, spn_credentials(monkeypatch))
    assert outcome(client.submit_url("https://example.com/")) == (ArchiveState.SUBMITTED, 200)
    assert len(posts) == 1


def test_spn2_403_fails_without_escalating_to_other_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(req: Request) -> FetchResult:
        calls.append(req.url)
        return FetchResult(403, {}, b"bad keys", "text/plain")

    config = AppSettings(fallbacks=["curl", "curl_cffi"])
    fetcher = TieredFetcher(
        config, transports={"curl": forbidden, "curl_cffi": lambda _r: json_result(b"{}")}
    )
    client = WaybackClient(fetcher, config, spn_credentials(monkeypatch))
    with pytest.raises(BlockedError):
        client.submit_url("https://example.com/")
    assert calls == ["https://web.archive.org/save"]


def test_spn2_polling_stops_with_submitted_on_a_stop_request_or_an_unreadable_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[Request] = []
    state = {"polls": 0, "reply": b"<html>not json</html>"}

    def transport(req: Request) -> FetchResult:
        if req.method == "POST":
            posts.append(req)
            return json_result(b'{"job_id":"abc"}')
        state["polls"] += 1
        return FetchResult(404, {}, state["reply"], "text/html")

    creds = spn_credentials(monkeypatch)
    unreadable = build(transport, creds)
    assert outcome(unreadable.submit_url("https://example.com/")) == (ArchiveState.SUBMITTED, 200)
    assert state["polls"] == 1

    def stop_during_wait(_seconds: float) -> None:
        raise StopRequested("stop")

    state["reply"] = b'{"status":"pending"}'
    config = AppSettings(fallbacks=["curl"], spn2_delay_seconds=0.0)
    fetcher = TieredFetcher(config, transports={"curl": transport})
    stopped = WaybackClient(fetcher, config, creds, sleep_fn=stop_during_wait)
    assert outcome(stopped.submit_url("https://example.com/")) == (ArchiveState.SUBMITTED, 200)
    assert len(posts) == 2  # one post for each submission, never a repeat for a known job


JOB_OK = b'{"status":"success","timestamp":"20261116042144","original_url":"https://example.com/p"}'


def job_web(job_reply: bytes, post_reply: bytes = b'{"job_id":"abc"}') -> object:
    def transport(req: Request) -> FetchResult:
        return json_result(post_reply if req.method == "POST" else job_reply)

    return transport


def test_a_success_is_confirmed_only_when_the_job_reports_a_snapshot_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = spn_credentials(monkeypatch)
    confirmed = build(job_web(JOB_OK), creds).submit_url("https://example.com/p")
    assert confirmed.state is ArchiveState.ARCHIVED
    assert confirmed.capture == Capture("20261116042144", "https://example.com/p")
    for no_time in (
        b'{"status":"success"}',
        b'{"status":"success","timestamp":""}',
        b'{"status":"success","timestamp":null}',
        b'{"status":"success","timestamp":7}',
    ):
        unconfirmed = build(job_web(no_time), creds).submit_url("https://example.com/p")
        assert (unconfirmed.state, unconfirmed.capture) == (ArchiveState.SUBMITTED, None)


@pytest.mark.parametrize("ext", sorted(_TRANSIENT_JOB_ERRORS))
def test_every_temporary_job_error_raises_so_the_url_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, ext: str
) -> None:
    reply = b'{"status":"error","status_ext":"error:%s","message":"try later"}' % ext.encode()
    client = build(job_web(reply), spn_credentials(monkeypatch))
    with pytest.raises(TransientFetchError, match=ext):
        client.submit_url("https://example.com/p")


@pytest.mark.parametrize(
    "reply",
    [
        b'{"status":"error","status_ext":"error:blocked-url"}',
        b'{"status":"error","status_ext":"error:not-found"}',
        b'{"status":"error","status_ext":"error:too-many-daily-captures"}',
        b'{"status":"error"}',
        b'{"status":"error","status_ext":""}',
    ],
)
def test_final_job_errors_fail_the_url(monkeypatch: pytest.MonkeyPatch, reply: bytes) -> None:
    client = build(job_web(reply), spn_credentials(monkeypatch))
    assert outcome(client.submit_url("https://example.com/p")) == (ArchiveState.FAILED, 200)


def test_a_post_answer_without_a_job_id_is_logged_and_sorted_like_a_job_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = spn_credentials(monkeypatch)
    busy = job_web(
        b"{}", b'{"message":"All sessions busy","status_ext":"error:user-session-limit"}'
    )
    with structlog.testing.capture_logs() as logs, pytest.raises(TransientFetchError):
        build(busy, creds).submit_url("https://example.com/p")
    final = job_web(b"{}", b'{"message":"A newer capture exists"}')
    with structlog.testing.capture_logs() as more:
        assert outcome(build(final, creds).submit_url("https://example.com/p")) == (
            ArchiveState.FAILED,
            200,
        )
    assert logs[0]["message"] == "All sessions busy"
    assert more[0]["message"] == "A newer capture exists"


def test_job_events_are_logged_and_hold_no_headers_or_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = spn_credentials(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        build(job_web(JOB_OK), creds).submit_url("https://example.com/p")
        build(job_web(b'{"status":"error","status_ext":"error:blocked-url"}'), creds).submit_url(
            "https://example.com/p"
        )
        build(job_web(b'{"status":"pending"}'), creds, spn2_max_polls=1).submit_url(
            "https://example.com/p"
        )
    assert [(e["event"], e["status"]) for e in logs] == [
        ("archive_job", "success"),
        ("archive_job", "error"),
        ("archive_job", "pending"),
    ]
    assert logs[0]["timestamp"] == "20261116042144"
    assert logs[1]["status_ext"] == "error:blocked-url"
    text = str(logs)
    assert "s3cret" not in text
    assert "Authorization" not in text
