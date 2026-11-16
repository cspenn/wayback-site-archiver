import os
from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError

from civil_service.config import AppSettings, SpnCredentials, configure_logging, load_settings
from civil_service.exceptions import ConfigError


def test_load_settings_reads_yaml_and_rejects_extra_keys(tmp_path: Path) -> None:
    default_settings = load_settings(Path("config.yml"))
    assert default_settings.user_agent == "Wayback Archiving"
    assert default_settings.wayback_max_age_days == 7
    assert default_settings.max_body_bytes == 10_485_760
    assert default_settings.max_sitemap_bytes == 52_428_800
    assert default_settings.max_transient_attempts == 3
    assert default_settings.db_path == Path("data/{host}.sqlite3")

    cfg = tmp_path / "config.yml"
    cfg.write_text('user_agent: "Wayback Archiving"\nthreads: 4\n', encoding="utf-8")
    settings = load_settings(cfg)
    assert isinstance(settings, AppSettings)
    assert settings.threads == 4
    assert settings.rearchive_window_days == 7
    assert settings.retry_delays_seconds == [5.0, 10.0, 20.0]

    bounded_cfg = tmp_path / "bounded.yml"
    bounded_cfg.write_text(
        "max_crawl_pages: 5\nmax_archive_urls: 10\ndb_path: relative.sqlite3\n", encoding="utf-8"
    )
    bounded = load_settings(bounded_cfg)
    assert bounded.max_crawl_pages == 5
    assert bounded.max_archive_urls == 10
    assert bounded.db_path == Path("relative.sqlite3")

    direct = AppSettings(db_path=tmp_path / "direct.sqlite3")
    assert direct.db_path == tmp_path / "direct.sqlite3"


@pytest.mark.parametrize(
    "bad_yaml",
    [
        "max_crawl_pages: 0\n",
        "unknown_flag: true\n",
        "fallbacks: []\n",
        "rearchive_window_days: 0\n",
        "- a\n- b\n",
        "spn_access_key: abc\n",
        "threads: [unclosed\n",
    ],
)
def test_load_settings_rejects_invalid_files(tmp_path: Path, bad_yaml: str) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(bad)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_unreadable_config_file_is_reported(tmp_path: Path) -> None:
    unreadable = tmp_path / "unreadable.yml"
    unreadable.write_text("threads: 1\n", encoding="utf-8")
    unreadable.chmod(0)
    try:
        with pytest.raises(ConfigError, match="cannot read"):
            load_settings(unreadable)
    finally:
        unreadable.chmod(0o600)


def test_config_errors_name_the_key_and_the_problem(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("rearchive_window_days: 0\nthreads: many\nunknown_flag: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings(bad)
    text = str(info.value)
    assert "rearchive_window_days" in text
    assert "greater than or equal to 1" in text
    assert "threads" in text
    assert "unknown_flag" in text
    bad.write_text("- a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a YAML mapping"):
        load_settings(bad)
    bad.write_text("threads: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bad YAML"):
        load_settings(bad)


def test_load_settings_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "missing.yml")


def test_empty_fallback_chain_fails_validation() -> None:
    with pytest.raises(ValidationError):
        AppSettings(fallbacks=[])


def test_environment_does_not_override_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREADS", "9")
    monkeypatch.setenv("CIVIL_SERVICE_THREADS", "9")
    assert AppSettings().threads == 2


def test_load_settings_works_without_working_directory_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_cfg = tmp_path / "standalone.yml"
    full_cfg.write_text(Path("config.yml").read_text(encoding="utf-8"), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = load_settings(full_cfg)
    assert loaded.user_agent == "Wayback Archiving"
    assert loaded.max_crawl_pages is None
    assert loaded.curl_cffi_impersonate == "chrome"


def test_spn_credentials_come_from_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert SpnCredentials().authorization is None
    monkeypatch.setenv("CIVIL_SERVICE_SPN_ACCESS_KEY", "access")
    assert SpnCredentials().authorization is None
    monkeypatch.setenv("CIVIL_SERVICE_SPN_SECRET_KEY", "s3cret")
    creds = SpnCredentials()
    assert creds.authorization == "LOW access:s3cret"
    assert "s3cret" not in repr(creds)
    assert "s3cret" not in str(creds.model_dump())
    monkeypatch.setenv("CIVIL_SERVICE_SPN_ACCESS_KEY", "")
    assert SpnCredentials().authorization is None


def test_configure_logging_accepts_valid_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.get_logger("test").info("hello", count=1)
    captured = capsys.readouterr()
    assert '"event":"hello"' in captured.err
    assert captured.out == ""

    with pytest.raises(ConfigError):
        configure_logging("NOT_A_LEVEL")
