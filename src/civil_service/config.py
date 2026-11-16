"""Application settings loaded from YAML and structured logging setup."""

import logging
from pathlib import Path
from typing import Annotated, Any, Literal

import orjson
import structlog
import yaml
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from civil_service.exceptions import ConfigError


def _orjson_dumps(event_dict: Any, **_kwargs: Any) -> str:
    """Serialize structured log events to UTF-8 JSON text with orjson."""
    return orjson.dumps(event_dict, default=str).decode("utf-8")


class AppSettings(BaseSettings):
    """Typed configuration loaded exclusively from YAML."""

    model_config = SettingsConfigDict(strict=True, extra="forbid", yaml_file="config.yml")

    user_agent: str
    threads: Annotated[int, Field(ge=1)]
    archive_threads: Annotated[int, Field(ge=1)]
    archive_delay_seconds: Annotated[float, Field(ge=0.0)]
    max_crawl_pages: Annotated[int, Field(ge=1)] | None
    max_archive_urls: Annotated[int, Field(ge=1)] | None
    max_redirects: Annotated[int, Field(ge=0)]
    max_body_bytes: Annotated[int, Field(ge=1)]
    max_transient_attempts: Annotated[int, Field(ge=1)]
    rearchive_window_days: Annotated[int, Field(ge=1)]
    wayback_max_age_days: Annotated[int, Field(ge=1)] | None
    retry_delays_seconds: list[Annotated[float, Field(ge=0.0)]]
    max_retry_after_seconds: Annotated[float, Field(ge=0.0)]
    request_timeout_seconds: Annotated[float, Field(gt=0.0)]
    fallbacks: Annotated[list[Literal["curl", "curl_cffi", "camoufox"]], Field(min_length=1)]
    curl_cffi_impersonate: str
    db_path: Annotated[Path, Field(strict=False)]
    wayback_avail_url: str
    wayback_save_url: str
    spn2_save_url: str
    spn2_status_url: str
    spn2_delay_seconds: Annotated[float, Field(ge=0.0)]
    spn2_poll_seconds: Annotated[float, Field(ge=0.0)]
    spn2_max_polls: Annotated[int, Field(ge=1)]
    max_sitemap_bytes: Annotated[int, Field(ge=1)]
    max_sitemaps: Annotated[int, Field(ge=0)]
    progress_refresh_seconds: Annotated[float, Field(ge=0.0)]
    respect_robots_txt: bool
    include_subdomains: bool
    allowed_schemes: list[str]
    log_level: str

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load init arguments, then the YAML file in the working directory."""
        del env_settings, dotenv_settings, file_secret_settings
        return (init_settings, YamlConfigSettingsSource(settings_cls))


class SpnCredentials(BaseSettings):
    """Archive.org S3-style API keys. They come from the environment only, never from YAML."""

    model_config = SettingsConfigDict(env_prefix="CIVIL_SERVICE_SPN_")

    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None

    @property
    def authorization(self) -> str | None:
        """Return the SPN2 Authorization header value, or None unless both keys hold text."""
        access = self.access_key.get_secret_value() if self.access_key else ""
        secret = self.secret_key.get_secret_value() if self.secret_key else ""
        return f"LOW {access}:{secret}" if access and secret else None


def load_settings(config_path: Path) -> AppSettings:
    """Load and validate application settings from a YAML file.

    The file overlays the config.yml in the working directory. Keys in the file win.

    Args:
        config_path: Path to the user YAML configuration file.

    Returns:
        Validated application settings.

    Raises:
        ConfigError: If the file is missing, invalid YAML, or fails validation.
    """
    resolved = config_path.resolve()
    if not resolved.is_file():
        msg = f"Config file not found: {config_path}"
        raise ConfigError(msg)
    try:
        return AppSettings(**YamlConfigSettingsSource(AppSettings, yaml_file=resolved)())
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        msg = f"Invalid configuration in {config_path}: {problems}"
        raise ConfigError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"Invalid configuration in {config_path}: bad YAML ({exc})"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"Invalid configuration in {config_path}: cannot read the file ({exc})"
        raise ConfigError(msg) from exc
    except (ValueError, TypeError) as exc:
        msg = f"Invalid configuration in {config_path}: the file is not a YAML mapping of settings"
        raise ConfigError(msg) from exc


def configure_logging(log_level: str) -> None:
    """Configure structlog and stdlib logging with orjson JSON output.

    Args:
        log_level: Logging level name such as INFO or DEBUG.

    Raises:
        ConfigError: If the logging level name is unknown.
    """
    level = logging.getLevelNamesMapping().get(log_level.upper())
    if level is None:
        msg = f"Unknown log level: {log_level}"
        raise ConfigError(msg)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(serializer=_orjson_dumps),
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
