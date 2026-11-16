import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import civil_service.main as main_mod

DRIVER = Path(__file__).parent / "sigint_driver.py"
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")


READY_TIMEOUT_SECONDS = 20


def start_driver(mode: str, tmp_path: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed arguments, test helper
        [sys.executable, str(DRIVER), mode, str(tmp_path / "sigint.sqlite3")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], READY_TIMEOUT_SECONDS)
    if not ready or proc.stdout.readline().strip() != "READY":
        proc.kill()
        pytest.fail("the driver never became ready")
    return proc


@pytest.fixture
def drivers() -> Iterator[list[subprocess.Popen[str]]]:
    """Collect started drivers and kill any that a failed test leaves running."""
    started: list[subprocess.Popen[str]] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


@posix_only
def test_one_ctrl_c_ends_a_run_that_waits_in_a_host_pause(
    tmp_path: Path, drivers: list[subprocess.Popen[str]]
) -> None:
    proc = start_driver("host-pause", tmp_path)
    drivers.append(proc)
    time.sleep(0.5)
    started = time.monotonic()
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=15) == 130
    assert time.monotonic() - started < 10


@posix_only
def test_a_request_in_flight_finishes_but_a_second_ctrl_c_quits_at_once(
    tmp_path: Path, drivers: list[subprocess.Popen[str]]
) -> None:
    proc = start_driver("slow-request", tmp_path)
    drivers.append(proc)
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=1.5)
    started = time.monotonic()
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=10) == 130
    assert time.monotonic() - started < 5


@posix_only
def test_hard_stop_handler_raises_once_then_exits_and_restores_the_old_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    before = signal.getsignal(signal.SIGINT)
    with main_mod.hard_stop_on_second_interrupt():
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
        assert exits == []
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
    assert exits == [130]
    assert signal.getsignal(signal.SIGINT) == before
