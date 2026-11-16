# Wayback Site Archiver Design

**Date:** 2026-11-15  
**Status:** Approved

## Intent and Why

Web pages and outbound citations disappear without warning. This CLI tool crawls a website from a seed URL, checks whether each public page and direct external link exists in the Wayback Machine (`archive.org`), and submits unarchived URLs politely. It prioritizes low memory overhead, strict adherence to HTTP rate limits, and idempotent resumption via local state.

## Goals, Non-Goals, and Anti-Goals

- **Goals:**
  - Maximize coverage: the share of eligible URLs (every catalog URL that is not gone or non-public) that hold a confirmed Wayback snapshot newer than the freshness window at the end of a run.
  - Crawl same-domain HTML pages (`Content-Type: text/html*`) starting from a seed URL using 2 configurable worker threads.
  - Check and submit both internal URLs and direct depth-1 external links found on crawled pages to the Wayback Machine.
  - Fetch pages using a strict fallback chain: `curl` CLI → `curl_cffi` → one lazy shared `camoufox` browser instance, with `User-Agent: Wayback Archiving`.
  - Honor `robots.txt`, HTTP `429`/`503` status codes, and `Retry-After` headers; when no `Retry-After` header exists, retry 3 times at `5`, `10`, and `20` seconds before recording failure and continuing.
  - Persist URL crawl and archive status in SQLite (WAL mode) and skip URLs processed within `rearchive_window_days` (default `7` days) for idempotent resume.
- **Non-Goals:**
  - Recursive crawling of external domains (external links stop at depth 1).
  - JavaScript SPA user interaction, form submission, or media transcoding.
  - Distributed multi-node queues or LLM inference during crawling.
- **Anti-Goals:**
  - Hardcoded constants or magic numbers outside `config.yml`.
  - High memory consumption from spawning multiple browser instances.
  - Rapid-fire requests that ignore `429` or `Retry-After` signals.
  - Solving or bypassing CAPTCHAs or other interactive challenges. TLS impersonation and the stealth browser are allowed (see the decisions below).

## Approaches Considered

1. **Lean Stdlib, Single Pass:** Two `ThreadPoolExecutor` workers fetch a page, extract links, check Wayback, and submit inline. Simple, but Wayback's submission rate limits would block site link discovery.
2. **Lean Stdlib, Two-Queue Pass (Selected):** Crawl threads populate SQLite with discovered URLs (`needs_crawl`), while a dedicated archive worker drains `archive_status = 'PENDING'` URLs against Wayback. Site discovery never stalls on Wayback rate limits while memory stays minimal.
3. **Heavy Stack (ORM, migrations, HTTPX, retry library):** Adds `SQLAlchemy`, `Alembic`, `HTTPX`, and `stamina` on top of the crawl stack. Rejected due to unnecessary ORM, migration, and duplicate HTTP client footprint for a single-table local CLI tool.

## Waivers from the Heavy-Stack Defaults

Approved by the user under Approach 2:
- Use stdlib `sqlite3` (WAL mode) instead of `SQLAlchemy` + `Alembic`.
- Use `curl` CLI → `curl_cffi` → `camoufox` instead of `HTTPX` + `stamina`.

## Architecture and Builder Decisions

- **Queue 1 (Crawl Pool):** `ThreadPoolExecutor(max_workers=settings.threads)` claims `needs_crawl = 1` rows from SQLite, enforces `robots.txt` (`urllib.robotparser`), fetches via `TieredFetcher`, parses `<a href>` with stdlib `html.parser`, normalizes URLs (stripping `#fragments`, resolving relatives, restricting to `allowed_schemes`, blocking private/loopback IPs post-DNS), inserts new internal (`needs_crawl = 1`) and external (`needs_crawl = 0`) rows, and updates the parent row's `crawl_status` and `crawled_at`.
- **Queue 2 (Wayback Archive Loop):** Dedicated archive worker thread(s) (`archive_threads: 1`) claim `archive_status = 'PENDING'` rows (eligible when external, or when the crawl ended with a status other than `404` or `410`), check `wayback_avail_url`, and submit missing or stale snapshots. Without API keys the worker sends an anonymous `GET` to `wayback_save_url`. With keys in the environment it sends a `POST` to the SPN2 endpoint and polls the job.
- **Shared Fetcher & Retries:** A `429` or `503` retries the same transport after fixed delays (`retry_delays_seconds = [5.0, 10.0, 20.0]`, or `Retry-After`) and never moves to the next transport. A `403`, a `5xx`, or a transport error moves to the next transport at once. Non-HTML responses on internal URLs are marked crawled without HTML parsing and remain eligible for Wayback archiving.

## Decisions After the 2026-11-15 Review

The owner's goal is to archive as much of the web as possible, especially pages that the Wayback Machine might never reach and time-sensitive pages. These decisions follow from it:

- **Unfetchable pages still go to Wayback.** A robots block, a `403`, a `5xx`, a timeout, or a rate limit leaves the URL eligible. Only `404` and `410` skip it.
- **robots.txt gates our own fetches only.** Disallowed URLs still go to Wayback. An unreachable `robots.txt` allows every URL.
- **Freshness is 7 days.** A newer snapshot is not required. An older one triggers a new capture.
- **A `403` escalates through the transports.** TLS impersonation and the stealth browser are permitted. CAPTCHA solving and bypass stay forbidden. On archive.org calls, a `403` fails at once.
- **New capabilities:** sitemap discovery, in-run retry of transient failures, optional authenticated SPN2 with job confirmation, page-link discovery from `area`, `iframe`, `frame`, and `link rel=alternate|canonical`, a per-host rate-limit pause, a body-size cap, and DNS pinning.
- **Embeds are not submitted separately.** The SPN2 documentation states that a page capture also saves its embeds.
- **API keys come from the environment only.** They never appear in `config.yml`, in logs, or in a command line.
- **Configuration & Logging:** `pydantic-settings[yaml]` loads all settings from `config.yml`; `structlog` + `orjson` emits structured JSON logs alongside `rich.progress` terminal output; `typer` provides the CLI entry point.
