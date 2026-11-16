import runpy
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import civil_service.main as main_mod
from civil_service.config import AppSettings
from civil_service.exceptions import UnsafeTargetError
from civil_service.main import app
from civil_service.runner import CrawlSummary


def test_cli_invokes_runner_and_handles_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(f"db_path: {tmp_path / 'cli.sqlite3'}\n", encoding="utf-8")
    runner = CliRunner()

    def fake_run(start_url: str, settings: AppSettings) -> CrawlSummary:
        assert start_url == "https://example.com"
        assert settings.db_path == tmp_path / "cli.sqlite3"
        return CrawlSummary(
            crawled=2, archived=1, submitted=1, covered=3, unconfirmed=1, eligible=5
        )

    monkeypatch.setattr(main_mod, "run_archiver", fake_run)
    res = runner.invoke(app, ["https://example.com", "--config", str(cfg)])
    assert res.exit_code == 0
    assert "crawled=2" in res.output
    assert "archive_failed=0" in res.output
    assert "Coverage: 3/5 (60.0%) confirmed, 1 submitted but unconfirmed" in res.output

    def failing_run(_start_url: str, _settings: AppSettings) -> CrawlSummary:
        raise UnsafeTargetError("blocked")

    monkeypatch.setattr(main_mod, "run_archiver", failing_run)
    err_res = runner.invoke(app, ["http://127.0.0.1/", "--config", str(cfg)])
    assert err_res.exit_code == 1
    assert "Error:" in err_res.output


def test_cli_reports_an_interrupt_and_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(f"db_path: {tmp_path / 'cli.sqlite3'}\n", encoding="utf-8")

    def interrupted(_start_url: str, _settings: AppSettings) -> CrawlSummary:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "run_archiver", interrupted)
    res = CliRunner().invoke(app, ["https://example.com", "--config", str(cfg)])
    assert res.exit_code == 130
    assert "Run the same command to resume" in res.output


def test_main_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"count": 0}
    monkeypatch.delitem(sys.modules, "civil_service.main", raising=False)
    monkeypatch.setattr("typer.Typer.__call__", lambda _self: called.__setitem__("count", 1))
    runpy.run_module("civil_service.main", run_name="__main__")
    assert called["count"] == 1
