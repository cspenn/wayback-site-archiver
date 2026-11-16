# Changelog

All notable changes to this project are listed here.

## [0.1.0] - 2026-11-15

First public release.

### Added

- Crawl a site from a start URL and submit every page and outbound link without a recent snapshot to the Wayback Machine.
- Coverage first: pages that fail to fetch, that `robots.txt` disallows for our crawler, or that answer `403` or `5xx` are still submitted. Only pages that the site reports as gone (HTTP 404 or 410) are skipped.
- Page discovery from sitemaps (plain, gzip, and nested indexes) and from `a`, `area`, `iframe`, `frame`, and `link rel="canonical"` or `rel="alternate"` links.
- A fallback chain of three transports: `curl`, `curl_cffi` (browser-like TLS), and a headless Camoufox browser. The tool never solves or bypasses CAPTCHAs.
- Rate-limit handling: `429`, `503`, and `Retry-After`, with a pause that applies to every thread of the same host.
- A local SQLite catalog, one file for each site, so a run can stop and resume. Transient failures retry inside the run.
- Safe to repeat and to resume: a repeat inside the freshness window fetches nothing and sends nothing. A run that dies without cleanup (`kill -9`, a crash) continues from the saved state and loses no link. A second run on the same catalog stops with an error (not on Windows).
- A configurable freshness window: a snapshot older than 7 days (default) triggers a new capture.
- Optional authenticated Save Page Now with archive.org API keys from the environment. The tool then confirms each capture.
- With API keys, a capture counts as confirmed only when the job reports a snapshot time. The catalog keeps the snapshot time and the captured URL. Each job writes one `archive_job` log event. A temporary job error (a session limit, a timeout) retries the URL.
- A coverage report at the end of each run, and live progress with counts of done and remaining items.
- Ctrl-C stops cleanly and exits with code 130. A second Ctrl-C quits at once. The next run resumes.
- JSON logs on standard error. A `civil-service` command and a `python -m civil_service.main` entry point.
- GitHub Actions workflow that runs the quality gate.

### Security

- Every request and every redirect hop is checked against private, loopback, and link-local addresses. `curl` and `curl_cffi` connect only to the addresses that passed the check.
- Request headers never follow a redirect to another origin. API keys stay out of command lines, logs, and the catalog.
- Links with a user name or password are ignored.
- Response bodies are capped in memory. Sitemaps are parsed without an XML parser and with a decompression cap.

### Known limitations

- A required proxy is not supported. The tool connects directly.
- Without API keys, the tool cannot confirm that a capture succeeded.
- The headless browser cannot pin addresses. It checks the redirect chain after the load, so a request to a private address can still be sent once. Remove `camoufox` from `fallbacks` to rule that out.
- The anonymous submission rate that archive.org allows for the older save URL is not documented. The default pace is 15 a minute. Raise `archive_delay_seconds` if archive.org answers `429`.
- Run one process for each catalog.
