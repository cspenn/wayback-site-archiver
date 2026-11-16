"""Custom exception hierarchy for the Wayback site archiver."""


class CivilServiceError(Exception):
    """Base exception for application errors."""


class ConfigError(CivilServiceError):
    """Raised when configuration or logging setup is invalid."""


class FetchError(CivilServiceError):
    """Raised when all configured fetch transports fail for a URL."""


class TransientFetchError(FetchError):
    """Raised for failures that a later retry can fix: timeouts, network errors, 5xx."""


class RateLimitError(TransientFetchError):
    """Raised when rate limits exhaust all configured retries."""


class BlockedError(FetchError):
    """Raised when a server answers HTTP 403 on a transport."""


class CatalogBusyError(CivilServiceError):
    """Raised when another run already uses the same catalog file."""


class StopRequested(CivilServiceError):
    """Raised inside a worker when the user asked the run to stop. The URL stays unfinished."""


class ArchiveError(CivilServiceError):
    """Raised when Wayback Machine availability or save parsing fails."""


class UnsafeTargetError(CivilServiceError):
    """Raised when a target URL resolves to a disallowed host or scheme."""
