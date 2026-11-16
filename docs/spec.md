# Technical Specification: Wayback Site Archiver

This spec matches the code in `src/civil_service/`. The PRD in `docs/prd.md` states the requirements.

## 1. Configuration

`AppSettings` (`pydantic_settings.BaseSettings`) uses `SettingsConfigDict(strict=True, extra="forbid", yaml_file="config.yml")`. The sources are the init arguments, then the YAML file in the working directory. The environment is not a source.

`load_settings(config_path)` reads `config_path` and passes its keys as init arguments. They win over the `config.yml` in the working directory. A missing working-directory file is not an error. A missing `config_path`, bad YAML, a file that is not a mapping, an unknown key, a missing key, or a value out of bounds raises `ConfigError`. For a bad value, the message lists each key and its problem. `AppSettings` declares no default in code. The "Shipped value" column lists what `config.yml` holds.

| Key | Type | Shipped value | Description |
| --- | --- | --- | --- |
| `user_agent` | `str` | `"Wayback Archiving"` | `User-Agent` header on every transport |
| `threads` | `int >= 1` | `2` | Crawl worker threads |
| `archive_threads` | `int >= 1` | `1` | Archive worker threads |
| `archive_delay_seconds` | `float >= 0` | `4.0` | Pause between anonymous Wayback submissions |
| `max_crawl_pages` | `int >= 1 \| None` | `None` | Stop claiming crawl URLs after this many. A retry does not count |
| `max_archive_urls` | `int >= 1 \| None` | `None` | Stop claiming archive URLs after this many. A retry does not count |
| `max_redirects` | `int >= 0` | `5` | Redirect hops that `fetch` follows |
| `max_body_bytes` | `int >= 1` | `10485760` | Largest response body kept in memory |
| `max_transient_attempts` | `int >= 1` | `3` | Attempts per URL and queue after a transient failure |
| `rearchive_window_days` | `int >= 1` | `7` | Days before a processed URL returns to the queues |
| `wayback_max_age_days` | `int >= 1 \| None` | `7` | A snapshot older than this triggers a new capture. `None` accepts any snapshot |
| `retry_delays_seconds` | `list[float >= 0]` | `[5.0, 10.0, 20.0]` | Rate-limit backoff when `Retry-After` is absent |
| `max_retry_after_seconds` | `float >= 0` | `300.0` | Cap on a parsed `Retry-After` |
| `request_timeout_seconds` | `float > 0` | `30.0` | Timeout for one request on one transport |
| `fallbacks` | `list[Literal["curl", "curl_cffi", "camoufox"]]`, at least one | all three, in that order | Transport chain |
| `curl_cffi_impersonate` | `str` | `"chrome"` | Browser profile for `curl_cffi` |
| `db_path` | `Path` | `data/{host}.sqlite3` | SQLite file. `{host}` becomes the seed host (characters other than letters, digits, `.`, and `-` become `_`), so each site has its own catalog |
| `wayback_avail_url` | `str` | `https://archive.org/wayback/available` | Availability API |
| `wayback_save_url` | `str` | `https://web.archive.org/save/` | Anonymous save URL |
| `spn2_save_url` | `str` | `https://web.archive.org/save` | SPN2 capture endpoint |
| `spn2_status_url` | `str` | `https://web.archive.org/save/status/` | SPN2 job status prefix |
| `spn2_delay_seconds` | `float >= 0` | `9.0` | Pause between SPN2 submissions (7 a minute allowed) |
| `spn2_poll_seconds` | `float >= 0` | `5.0` | Pause between job status polls |
| `spn2_max_polls` | `int >= 1` | `12` | Status polls per job |
| `max_sitemap_bytes` | `int >= 1` | `52428800` | Largest sitemap file, and largest gunzipped size |
| `max_sitemaps` | `int >= 0` | `20` | Sitemap files fetched in one run |
| `progress_refresh_seconds` | `float >= 0` | `1.0` | Least time between progress refreshes (catalog queries and display updates) |
| `respect_robots_txt` | `bool` | `true` | Gate our own fetches with `robots.txt` |
| `include_subdomains` | `bool` | `false` | Treat subdomains of the seed host as internal |
| `allowed_schemes` | `list[str]` | `["http", "https"]` | URL schemes that the tool accepts |
| `log_level` | `str` | `"INFO"` | Log threshold |

`SpnCredentials` (`BaseSettings`, `env_prefix="CIVIL_SERVICE_SPN_"`) holds `access_key` and `secret_key` as `SecretStr | None`. It reads the environment only. `authorization` returns `LOW <access>:<secret>` when both keys hold text, and `None` otherwise (a key set to an empty string counts as absent).

## 2. SQLite Schema

Before it opens the file, the catalog takes an exclusive `flock` on `<db_path>.lock` and holds it until `close`. A second `Catalog` on the same file raises `CatalogBusyError`. The kernel drops the lock when the process ends, so a crashed run leaves no stale lock. Windows has no `fcntl`, so Windows has no lock. The catalog opens the file with `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL` (safe in WAL mode, with far fewer disk syncs: 5,000 inserts took 0.3 s on the development machine), `PRAGMA busy_timeout=5000`, and `check_same_thread=False`. One `threading.Lock` guards the connection.

```sql
CREATE TABLE IF NOT EXISTS urls (
    url TEXT PRIMARY KEY,
    is_internal INTEGER NOT NULL,
    needs_crawl INTEGER NOT NULL,
    crawl_status INTEGER,
    crawled_at TEXT,
    archive_status TEXT NOT NULL,
    archive_http_status INTEGER,
    archived_at TEXT,
    crawl_attempts INTEGER NOT NULL DEFAULT 0,
    archive_attempts INTEGER NOT NULL DEFAULT 0,
    snapshot_ts TEXT,
    snapshot_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl ON urls (needs_crawl, crawl_attempts);
CREATE INDEX IF NOT EXISTS idx_archive ON urls (archive_status, archive_attempts);
```

Migration: when `PRAGMA table_info(urls)` lacks `crawl_attempts`, `archive_attempts`, `snapshot_ts`, or `snapshot_url`, the catalog adds the column with `ALTER TABLE ... ADD COLUMN`. The attempt columns are `NOT NULL DEFAULT 0`. The snapshot columns are nullable. Then it creates the indexes. Existing rows keep their data.

Timestamps are ISO 8601 UTC strings with microseconds. They sort in time order as text.

`crawl_status` holds the HTTP status of the crawl. Two values are not HTTP statuses: `0` (the fetch failed) and `-1` (`robots.txt` blocked the fetch). Both keep the URL eligible for archiving.

### State transitions

`needs_crawl`: `0` done (or external), `1` pending, `2` in progress.

| From | Event | To | Side effect |
| --- | --- | --- | --- |
| (new row) | insert internal URL | `1` | |
| (new row) | insert external URL | `0` | |
| `1` | worker claims URL (fewest `crawl_attempts` first) | `2` | |
| `2` | crawl ends (any HTTP status, or failure) | `0` | `crawl_status` = status, or `0` on failure. `crawled_at` set |
| `2` | transient failure and `crawl_attempts + 1 < max_transient_attempts` | `1` | `crawl_attempts + 1` |
| `2` | startup | `1` | Rows from an interrupted run |
| `0` | the same URL is inserted again, it is internal, and `crawled_at` is null or older than the window | `1` | `crawl_attempts = 0` |

`archive_status`:

| From | Event | To | Side effect |
| --- | --- | --- | --- |
| (new row) | insert | `PENDING` | |
| `PENDING` | worker claims an eligible URL (fewest `archive_attempts` first) | `IN_PROGRESS` | |
| any | crawl ends with status `404` or `410` | `SKIPPED` | Can overwrite `IN_PROGRESS`. The archive result that follows replaces `SKIPPED` |
| `IN_PROGRESS` | the target host has a non-public address | `SKIPPED` | `archived_at` set. No request leaves the tool |
| `IN_PROGRESS` | fresh snapshot found, or SPN2 job succeeded | `ARCHIVED` | `archived_at`, `archive_http_status` set |
| `IN_PROGRESS` | submission accepted, not confirmed | `SUBMITTED` | `archived_at`, `archive_http_status` set |
| `IN_PROGRESS` | submission refused, or a final error | `FAILED` | `archived_at` set. `archive_http_status` holds the refusal status, or null for an error without a status |
| `IN_PROGRESS` | transient failure and `archive_attempts + 1 < max_transient_attempts` | `PENDING` | `archive_attempts + 1` |
| `IN_PROGRESS` | startup | `PENDING` | Rows from an interrupted run |
| `ARCHIVED`, `SUBMITTED`, `FAILED`, `SKIPPED` | `COALESCE(archived_at, crawled_at)` is null or older than the window, on insert of the same URL | `PENDING` | `archive_attempts = 0` |

Archive eligibility: `is_internal = 0` or (`needs_crawl = 0` and `crawl_status NOT IN (404, 410)`). A crawl that failed (`crawl_status = 0`) is eligible.

`insert_discovered` is one `INSERT ... ON CONFLICT DO UPDATE` statement. It never lowers `is_internal`. An external row that later appears as internal becomes a crawl target.

`FAILED` and `SKIPPED` rows wait one window before they return to the queues.

## 3. Module Layout (`src/civil_service/`)

- `exceptions.py`: `CivilServiceError`; `ConfigError`; `CatalogBusyError` (another run holds the catalog lock); `FetchError`; `TransientFetchError(FetchError)` (timeout, network error, `5xx`); `RateLimitError(TransientFetchError)`; `BlockedError(FetchError)` (HTTP `403`); `StopRequested` (raised inside a worker after a stop; the URL stays unfinished); `ArchiveError`; `UnsafeTargetError`.
- `config.py`: `AppSettings`, `SpnCredentials`, `load_settings(config_path: Path) -> AppSettings`, `configure_logging(log_level: str) -> None`.
- `urls.py`:
  - `normalize_url(base_url, href, allowed_schemes) -> str | None`: drops malformed links and links with `user:password@`. Rewrites a non-ASCII host to the form that Python's IDNA codec writes, so the address check and the fetch use one name.
  - `is_ip_literal(host) -> bool`
  - `is_same_domain(url, root_netloc, include_subdomains) -> bool`
  - `host_port(url) -> tuple[str, int]`: raises `UnsafeTargetError` for a missing host or a bad port.
  - `validate_public_host(url) -> tuple[str, ...]`: returns every resolved IP, in resolver order, without duplicates. Raises `UnsafeTargetError` when any resolved address is non-global or the name cannot be written as a valid host name. Raises `TransientFetchError` when the host does not resolve: the resolver can fail while the network is down, so the caller retries.
  - `extract_links(html_text, base_url, allowed_schemes) -> list[str]`
  - `read_sitemap_bytes(raw, cap) -> str | None`
  - `parse_sitemap(text) -> tuple[bool, list[str]]`
- `catalog.py`: the constants `CRAWL_FAILED` (0) and `CRAWL_ROBOTS_BLOCKED` (-1), `CrawlState`, `ArchiveState`, `CatalogEntry`, and `Catalog` with `get_entry`, `seed_url`, `insert_discovered`, `claim_next_crawl`, `record_crawl_result`, `requeue_crawl`, `claim_next_archive`, `record_archive_result(url, status, http_status, now=None, capture=None)`, `requeue_archive`, `reset_stale_in_progress`, `pending_counts`, `archive_coverage(now=None) -> (covered, unconfirmed, eligible)`, `close`.
- `fetcher.py`:
  - `Request(url, method="GET", headers, body, max_bytes=0, follow_redirects=True, escalate_on_403=True, pin_ips=())`. `max_bytes=0` means `settings.max_body_bytes`.
  - `FetchResult(status_code, headers, body, content_type, final_url="", truncated=False)` with a `text` property.
  - `parse_retry_after(value, max_wait, now=None) -> float | None`
  - `fetch_with_curl(req, user_agent, timeout)`, `fetch_with_curl_cffi(req, user_agent, timeout, impersonate)`, `CamoufoxPool.fetch(req)`.
  - `TieredFetcher(settings, transports=None, sleep_fn=None, clock=time.monotonic)` with `fetch(request) -> FetchResult`, `interruptible_sleep(seconds)` (the default `sleep_fn`; raises `StopRequested` after a stop), and `request_stop()`.
- `wayback.py`: `WaybackClient(fetcher, settings, credentials=None, sleep_fn=None, clock=time.monotonic)` (the default `sleep_fn` is the fetcher's `interruptible_sleep`) with `is_archived(url, now=None) -> tuple[bool, int]` and `submit_url(url) -> Submission` (a named tuple of `state`, `status`, and `capture`). `submit_url` waits for its pacing slot first.
- `runner.py`: `CrawlSummary` (`crawled`, `robots_blocked`, `crawl_failed`, `archived`, `submitted`, `archive_skipped`, `archive_failed`, `covered`, `unconfirmed`, `eligible`, and the `coverage_percent` property), `RobotsGuard`, `catalog_path(db_path, seed_url) -> Path`, `run_archiver(start_url, settings, console=None, fetcher=None, sleep_fn=None) -> CrawlSummary`.
- `main.py`: Typer CLI. `start_url` is the argument. `--config` defaults to `config.yml`. Exit code `0` on completion, `1` on a `CivilServiceError` (the message goes to standard error as plain text), and `130` on Ctrl-C.

## 4. Fetch Policy

### Per-request rules (`TieredFetcher`)

| Event on one transport | Action |
| --- | --- |
| HTTP `429` or `503` | Wait for the host pause. Retry the same transport. The delay is `Retry-After` (capped) or `retry_delays_seconds[attempt]`. After the last retry, raise `RateLimitError`. Do not try the next transport. |
| HTTP `403` | Raise `BlockedError`. `fetch` tries the next transport, unless `escalate_on_403` is false. Then it raises at once. |
| HTTP `5xx` (not `503`) | Raise `TransientFetchError`. `fetch` tries the next transport. |
| Transport error (`curl` exit code other than 0, `curl_cffi` or `camoufox` exception) | Raise `TransientFetchError`. `fetch` tries the next transport. No wait. |
| `camoufox` browser cannot start | Raise `FetchError` (not transient) with the install command. `fetch` tries the next transport, or fails if none is left. |
| `curl` binary missing | Raise `FetchError` (not transient). `fetch` tries the next transport. |
| Any other status | Return the response. |

When every transport fails, `fetch` raises the first `TransientFetchError` if there is one. Otherwise it raises the last error.

The host pause is a map from host to a resume time, guarded by a lock. A `429` or `503` sets `max(old, now + delay)`. Every request waits until the resume time of its host. The pause covers all threads and all transports.

### Redirects

`fetch` resolves and validates the host of every hop with `validate_public_host`. It passes the validated addresses to the transports as `pin_ips`. It follows at most `max_redirects` hops. A redirect to a disallowed scheme or a non-public host raises `UnsafeTargetError`. A `301`, `302`, or `303` changes the method to `GET` and drops the body. A `307` or `308` keeps both. A redirect without a `Location` header returns the response. A redirect to another origin (another host, or https to http) drops all request headers, so credentials never leave the origin that the caller named. A redirect inside the same origin keeps them. Callers that send credentials also set `follow_redirects=False`, so the drop is a second layer.

### DNS pinning

`curl` gets `--resolve host:port:addr1,addr2`. `curl_cffi` gets `CurlOpt.RESOLVE` (a `Session` option) with the same list. Both connect to the addresses that passed the check and try them in order, so an unreachable first address (IPv6 on an IPv4-only network) does not stop the request. Certificate checks still use the host name. `curl` and `curl_cffi` skip the pin for an IP-literal host (nothing to resolve, and `--resolve` cannot take a literal). Children start with `start_new_session=True`, so a terminal Ctrl-C reaches only the Python process. `curl` also runs with `-q` as its first argument (no `~/.curlrc`, which could follow redirects or skip TLS checks) and `--noproxy "*"`. `curl_cffi` runs in a `Session(trust_env=False)`. Neither reads proxy variables, because a proxy would resolve the host itself and the pinned address would have no effect. The tool does not work behind a required proxy. `camoufox` cannot pin an address. Its route guard validates the requests that the page makes. Playwright calls the route hook for the first request of a redirect chain only, so after `page.goto` the pool walks `response.request.redirected_from` and checks every hop with `validate_public_host`. A non-public hop raises `UnsafeTargetError` and the page is dropped. The browser has already sent a GET to that address. Removing `camoufox` from `fallbacks` rules that out. A browser that fails to start or to open a page is discarded, so the next fetch starts a new one. `page.request.fetch` (a POST, or a GET with headers) is an API client that `page.route` does not intercept in normal Playwright use. That path gets only the check before the call, and it runs with `max_redirects=0`. A GET without headers loads the page with `page.goto`, which follows redirects inside the browser with no hop limit of ours. `page.route` is the only check on those hops. Whether the guard sees each redirect hop is browser-library behavior that this project has not verified. The result carries the browser's final URL. A non-HTML answer returns as raw bytes. A GET with headers, and any POST, use the API client with `max_redirects=0`.

### Body cap

`curl` runs with `-o -` and the tool reads at most `max_bytes + 1` bytes from the pipe. When the body is larger, the tool kills `curl`, keeps `max_bytes`, and sets `truncated`. The header file stays complete, because `curl` writes headers before the body. `curl_cffi` reads a stream up to the cap. `camoufox` cuts the page after it loads. `fetch` logs `body_capped` for each truncated response.

### Secrets and bodies

For `curl`, request headers and the body go into files in a private temporary directory (both files mode `0600`). The command line holds `-H @file` and `--data-binary @file`. A header value never appears in the process list. The `curl` child gets the environment without the `CIVIL_SERVICE_SPN_*` variables. The `camoufox` browser process inherits the full environment.

### Worst-case time

Symbols: T = `request_timeout_seconds` (30), n = number of `retry_delays_seconds` (3), R = `max_retry_after_seconds` (300), D = sum of `retry_delays_seconds` (35), F = number of `fallbacks` (3), H = `max_redirects` (5), A = `max_transient_attempts` (3), P = `spn2_max_polls` (12), S = `spn2_poll_seconds` (5).

One hop, worst case: every transport hits a rate limit n times and then fails with a `5xx` or `403`, so `fetch` moves to the next transport, and the last transport hits n rate limits and then succeeds. Each transport costs (n + 1) × T + n × R, so one hop costs F × ((n + 1) × T + n × R) = 3,060 s with `Retry-After` at the cap. Without the header, the retry waits add up to D, and one hop costs F × ((n + 1) × T + D) = 465 s. A final rate limit on the last transport raises `RateLimitError` and ends the fetch, so it adds no further hop.

| Operation | Formula | With `Retry-After` at the cap | Without the header |
| --- | --- | --- | --- |
| `fetch` with no redirect (one hop) | one hop | 3,060 s (51 min) | 465 s (8 min) |
| `fetch` that follows redirects | (H + 1) hops | 18,360 s (5.1 h) | 2,790 s (46 min) |
| One crawl URL | A × fetch with redirects | 55,080 s (15.3 h) | 8,370 s (2.3 h) |
| One anonymous archive attempt | availability (follows redirects) + one submission hop | 21,420 s (6.0 h) | 3,255 s (54 min) |
| One anonymous archive URL | A × attempt | 64,260 s (17.9 h) | 9,765 s (2.7 h) |
| One SPN2 archive attempt | availability + POST hop + P poll hops + (P − 1) × S | 58,195 s (16.2 h) | 8,890 s (2.5 h) |
| One SPN2 archive URL | A × attempt | 174,585 s (48.5 h) | 26,670 s (7.4 h) |

These bounds assume that every limit hits at its maximum. Normal use is far lower. They leave out the `robots.txt` fetch, the sitemap phase, submission pacing, DNS resolution time, and the wait in the single `camoufox` executor queue (the request timeout T does not cover that wait). A hop that waits for a host pause can wait longer when other threads renew the pause: each renewal sets the resume time to at most R seconds after the newest `429`, so a thread cannot wait forever unless the host keeps rate limiting. A rate limit also pauses the other threads of that host for the same time.

## 5. Robots and Sitemaps

`RobotsGuard` fetches `robots.txt` once for each origin and caches the parser. A lock for each origin means that a slow `robots.txt` holds back only the threads that wait for that origin. A fetch error or a non-`200` answer gives an empty rule set, which allows every URL. The guard gates the URL that a crawl worker claims. It never blocks a Wayback submission. It does not gate redirect targets or sitemap files.

Sitemap discovery runs once, before the crawl pools start. The queue starts with the `Sitemap:` lines of the seed origin's `robots.txt`, then `<origin>/sitemap.xml`. Each file is fetched with `max_bytes = max_sitemap_bytes`.

- A file that starts with the gzip magic bytes passes through `zlib.decompressobj(wbits=31).decompress(raw, cap + 1)`. Output over the cap, or a bad stream, rejects the file and logs `sitemap_rejected`.
- Memory: one sitemap can use several times `max_sitemap_bytes` while it is read (raw bytes, unzipped bytes, and the decoded text).
- `parse_sitemap` uses the regular expression `<loc>([^<]*)</loc>`. It builds no XML tree. A `<sitemapindex` tag marks an index. The expression cannot match across a `<`, so run time is linear.
- In an index, only locations on the same domain join the queue. The run fetches at most `max_sitemaps` files.
- In a URL set, each location passes `normalize_url`. A same-domain URL becomes an internal row. Any other URL becomes an external row.
- A fetch error logs `sitemap_unavailable`. A non-`200` status skips the file without a log entry.
- Any exception from reading `robots.txt` allows every URL. A sitemap cut at `max_sitemap_bytes` (plain or gzip) is parsed up to the cut. `fetch` logs `body_capped`. Only a gzip file that expands past the cap, or a corrupt gzip stream, is rejected whole.

## 6. Wayback Client

### Availability

`GET {wayback_avail_url}?url=<url>&timestamp=<now, YYYYMMDDhhmmss>`. A response with `archived_snapshots.closest.available` set and a timestamp newer than `wayback_max_age_days` counts as archived. The call raises `ArchiveError` for a non-2xx answer, invalid JSON, or JSON that is not an object. Calls use `escalate_on_403=False`.

### Anonymous submission (no keys)

`GET {wayback_save_url}/<url>` without redirects. A status from 200 up to 399 gives `SUBMITTED`. Other statuses give `FAILED`. Nothing confirms the capture.

A submission can reach archive.org more than once. A timeout moves the request to the next transport, and an in-run retry sends it again. For SPN2 the documentation says that a repeat inside one hour returns the recent capture and that one URL gets at most 5 captures a day. The documentation does not state this for the anonymous save URL.

### SPN2 submission (both keys set)

Source: the SPN2 public API documentation, updated 2026-07-22.

1. `POST {spn2_save_url}` with the form body `url=<url>` and these headers: `Accept: application/json`, `Content-Type: application/x-www-form-urlencoded`, `Authorization: LOW <access>:<secret>`.
2. A non-2xx answer gives `FAILED`. A 2xx answer must hold a JSON `job_id`. Without it the result is `FAILED`.
3. `GET {spn2_status_url}<job_id>` with the same `Accept` and `Authorization` headers, without redirects, up to `spn2_max_polls` times, with `spn2_poll_seconds` between polls (no wait after the last poll). The JSON field `status` is `pending`, `success`, or `error`. Any other value counts as `pending`. A transient error while polling stops the polling with `SUBMITTED`, because the job exists and the tool must not post it again.
4. `success` gives `ARCHIVED` only when the payload has a non-empty `timestamp`. The result then carries a `Capture(timestamp, original_url)`, and the catalog stores both in `snapshot_ts` and `snapshot_url`. A `success` without `timestamp` gives `SUBMITTED`. `error` gives `FAILED`, unless `status_ext` (without the `error:` prefix) is one of the temporary codes: `user-session-limit`, `too-many-requests`, `service-unavailable`, `bad-gateway`, `gateway-timeout`, `read-timeout`, `browsing-timeout`, `soft-time-limit-exceeded`, `no-browsers-available`, `cannot-fetch`, `celery`, `job-failed`, `capture-location-error`, `internal-server-error`, `proxy-error`, `protocol-error`. A temporary code raises `TransientFetchError`, and the runner tries the URL again. Still `pending` after the last poll gives `SUBMITTED`. A 2xx answer to the POST without `job_id` is sorted the same way: a temporary `status_ext` raises, any other answer gives `FAILED`.

The tool does not send `capture_outlinks`. It queues each outlink itself, so each page gets its own rate-limit slot and catalog row.

Documented limits (same source): 7 captures a minute and 30,000 a day for authenticated users, 5 captures a day for one URL. `spn2_delay_seconds` defaults to 9.0 so the rate stays under 7 a minute. A capture also saves the page embeds, so the tool does not submit images, scripts, and style sheets.

Not verified: the SPN2 documentation does not show the answer to a capture request when a capture newer than one hour already exists. The code treats a 2xx answer without `job_id` as `FAILED`. The tool posts only after the availability check finds no fresh snapshot, so this case is rare. The documentation also does not state the limits of the anonymous save URL.

### Pacing

`submit_url` reserves the next submission slot before it sends the request. A slot opens `archive_delay_seconds` (without keys) or `spn2_delay_seconds` (with keys) after the previous slot. All archive threads share the slots, so the rate holds for any thread count. The first submission does not wait. Availability checks are not paced.

## 7. Runner

- One `threading.Condition` guards the claim logic, the counters, and the shutdown flags of both queues.
- A crawl worker claims the URL with the fewest attempts. `max_crawl_pages` and `max_archive_urls` count claims, and a retry gives its claim back, so the limits count URLs that end. A worker stops when `max_crawl_pages` is reached, when an error aborts the run, or when no URL is pending and no crawl is active.
- An unhandled error in a crawl worker sets the abort flag and ends the crawl phase. Archive workers keep claiming the URLs that are already eligible, finish them, and stop. The error then reaches the caller.
- The crawl and archive queues share the catalog, the host pause table, the single `camoufox` executor, and the runner condition. A slow Wayback call holds none of them while it waits for the network.
- An archive worker waits while the crawl is active and no URL is eligible.
- `_crawl_one` order: robots check (a block records status `-1`), fetch, save the links of a 2xx `text/html` response on the seed site (a page on another domain gets no link extraction), record the final URL after a redirect (same domain: crawl target with the same status. Other domain: external link), and only then record the status of the page. A crash before the last step leaves the page in progress, so the next run fetches it again and every link is saved. The catalog is therefore at-least-once for fetches and exactly-once for rows.
- `_archive_one` order: `validate_public_host` on the target (a non-public address records `SKIPPED` and sends nothing to archive.org; a host that does not resolve is transient), availability check, submission, record the result. An `UnsafeTargetError` from the Wayback endpoint itself counts as a failure of that URL, not as a skip.
- `run_archiver` opens the catalog at `catalog_path(settings.db_path, seed)`.
- Every crawl and archive step catches all exceptions for its one URL, records a failure (or `SKIPPED` for a non-public target), and goes on, so one bad URL cannot end the run or crash every later run. `normalize_url` drops a malformed link. `host_port` and `validate_public_host` raise `UnsafeTargetError` for a malformed URL or host name.
- Ctrl-C raises `KeyboardInterrupt` in the main thread while it waits for the workers. The runner sets a stop flag and calls `fetcher.request_stop()`. Every wait ends (`interruptible_sleep` raises `StopRequested`), every transport attempt and redirect hop checks the stop flag before it sends, and each worker claims no more URLs. An error in an archive worker calls the same stop path and reaches the caller. The pools are joined in a `finally` block before the catalog closes. A request in flight finishes first, so the exit takes at most `request_timeout_seconds`. A worker that gets `StopRequested` leaves its URL in progress, and the row returns to pending at the next start. `main.hard_stop_on_second_interrupt` installs the signal handler: the first Ctrl-C raises `KeyboardInterrupt`, and the second calls `os._exit(130)`.
- After both pools end, `Catalog.archive_coverage` fills `CrawlSummary.covered` (`ARCHIVED` inside the window), `unconfirmed` (`SUBMITTED` inside the window), and `eligible` (every row except `SKIPPED`).

## 8. Logging

`configure_logging` sends `structlog` events as one JSON object per line (`orjson`) to standard error through the root `logging.StreamHandler`. The `rich` progress display uses the console (standard output).

Events: `archive_job` (one for each Save Page Now job: `job_id`, `status`, `status_ext`, `message`, `timestamp`, `original_url`; never headers), `robots_disallowed`, `crawl_failed`, `crawl_retry`, `archive_failed`, `archive_retry`, `archive_skipped`, `body_capped`, `sitemap_unavailable`, `sitemap_rejected`. Events hold URLs and error text. They never hold header values.

## 9. Quality and Security Requirements

- Code rules: Python 3.12 type syntax, Ruff (`C901 <= 10`, `PLR0913 <= 5`, `PLR0912 <= 12`, `PLR0915 <= 50`, Google docstrings), `mypy --strict`, `bandit -r src/`, `deptry src/`, `pip-audit`, and `pytest --cov=src --cov-fail-under=100` with `branch = true`. `checkpython.sh` runs these.
- `subprocess` calls use a list and no shell.
- `verify=True` applies on every transport.
- Tests fake all external services. The real `curl` transport runs against a local server on 127.0.0.1.

## 10. Requirement-to-Test Map

| Requirement | Tests |
| --- | --- |
| FR-1 | `test_config.py`: `test_load_settings_reads_yaml_and_rejects_extra_keys`, `test_load_settings_rejects_invalid_files`, `test_empty_fallback_chain_fails_validation`, `test_environment_does_not_override_settings`, `test_load_settings_works_without_working_directory_config`, `test_config_errors_name_the_key_and_the_problem`, the `configure_logging` test (logs go to standard error), `test_spn_credentials_come_from_environment_only` |
| FR-2 | `test_fetcher.py`: `test_rate_limit_retries_5_10_20_then_raises_without_trying_next_transport`, `test_rate_limit_then_success_and_retry_after_header`, `test_host_pause_holds_back_later_requests_to_the_same_host_only`, `test_parse_retry_after_seconds_http_date_and_cap`. `test_runner.py`: `test_runner_still_submits_robots_blocked_and_blocked_pages`, `test_runner_robots_fetch_error_fails_open` (403, 5xx, 429, network error, no transport, private host, unexpected error), `test_robots_guard_loads_each_origin_once_and_waits_only_for_that_origin` |
| FR-3 | `test_fetcher.py`: `test_403_and_5xx_and_transport_errors_move_to_next_transport_without_sleep`, `test_all_transports_failing_raises_the_most_useful_error`, `test_403_fails_at_once_when_escalation_is_off`, `test_redirect_to_another_host_drops_request_headers`, `test_redirect_that_downgrades_https_to_http_drops_request_headers`, the `curl_cffi` and `camoufox` transport tests (including the missing-browser case and TLS verification). `test_wayback.py`: `test_spn2_403_fails_without_escalating_to_other_transports` |
| FR-4 | `test_runner.py`: `test_a_redirect_to_another_domain_makes_the_target_an_external_link`. `test_urls.py`: `test_extract_links_collects_page_tags_and_ignores_assets`, `test_parse_sitemap_reads_urlset_and_index`, `test_read_sitemap_bytes_handles_plain_gzip_bomb_and_garbage`. `test_runner.py`: `test_runner_discovers_pages_from_robots_sitemap_index`, `test_runner_reads_gzip_sitemaps_and_rejects_bombs_and_bad_responses`, `test_runner_bounds_how_many_sitemaps_it_fetches`, `test_runner_canonicalizes_redirects_and_enforces_bounds` |
| FR-5 | `test_resume.py`: `test_a_finished_run_repeated_inside_the_window_changes_and_sends_nothing`, `test_a_run_killed_without_cleanup_resumes_and_finishes_every_page` (a real `SIGKILL`). `test_runner.py`: `test_a_crash_between_saving_links_and_marking_the_page_done_loses_no_link`. `test_catalog.py`: `test_catalog_seven_day_idempotency_and_reset_stale`, `test_expired_rows_reset_attempt_counters`, `test_old_database_gains_attempt_columns_and_indexes`, `test_startup_reset_returns_interrupted_archive_rows_to_pending`, `test_rediscovery_inside_the_window_changes_nothing`, `test_each_site_gets_its_own_catalog_file` |
| FR-6 | `test_resume.py`: `test_a_second_run_on_the_same_catalog_is_refused_and_a_later_run_is_not`. `test_runner.py`: `test_an_archive_worker_crash_stops_the_whole_run_and_joins_every_worker`, `test_ctrl_c_while_the_pools_shut_down_still_asks_every_worker_to_stop`, `test_a_stop_during_the_robots_fetch_ends_the_run_without_allowing_every_url`. `test_fetcher.py`: `test_a_stop_request_beats_the_next_transport_and_the_next_hop`. `test_sigint.py`: `test_one_ctrl_c_ends_a_run_that_waits_in_a_host_pause`, `test_a_request_in_flight_finishes_but_a_second_ctrl_c_quits_at_once` (a real subprocess and a real SIGINT), `test_hard_stop_handler_raises_once_then_exits_and_restores_the_old_handler`. `test_runner.py`: `test_a_stop_request_leaves_the_url_unfinished_and_the_next_run_completes_it`, `test_one_bad_url_never_ends_the_run`, `test_ctrl_c_stops_the_run_cleanly_and_the_next_run_resumes`. `test_main.py`: `test_cli_reports_an_interrupt_and_exits_130`. `test_catalog.py`: `test_catalog_concurrent_claims_never_duplicate`, `test_internal_url_waits_for_crawl_before_archiving`. `test_runner.py`: `test_runner_does_not_deadlock_on_uncaught_crawl_error` (archive workers drain), `test_the_crawl_keeps_going_while_a_wayback_call_is_blocked`, `test_runner_crawls_html_skips_binary_and_archives_external_links` |
| FR-7 | `test_runner.py`: `test_runner_throttles_progress_refreshes`, `test_runner_crawls_html_skips_binary_and_archives_external_links`. `test_fetcher.py`: `test_truncated_bodies_are_logged`. `test_main.py`: `test_cli_invokes_runner_and_handles_errors` |
| FR-8 | `test_catalog.py`: `test_failed_fetches_stay_eligible_and_only_gone_pages_skip_archiving`. `test_runner.py`: `test_runner_submits_unfetchable_pages_but_not_gone_pages` |
| FR-9 | `test_wayback.py`: `test_default_freshness_window_is_seven_days`, `test_snapshot_age_and_error_cases`, `test_anonymous_client_checks_availability_with_timestamp_and_submits_by_get` |
| FR-10 | `test_catalog.py`: `test_requeue_counts_attempts_and_orders_fresh_urls_first`. `test_runner.py`: `test_runner_retries_transient_crawl_and_archive_failures_inside_the_run`, `test_retries_do_not_use_up_the_page_and_archive_limits`, `test_hosts_that_do_not_resolve_retry_and_then_fail_and_are_never_skipped` |
| FR-11 | `test_wayback.py`: `test_a_success_is_confirmed_only_when_the_job_reports_a_snapshot_time`, `test_every_temporary_job_error_raises_so_the_url_is_tried_again`, `test_final_job_errors_fail_the_url`, `test_a_post_answer_without_a_job_id_is_logged_and_sorted_like_a_job_error`, `test_job_events_are_logged_and_hold_no_headers_or_keys`. `test_runner.py`: `test_a_temporary_job_error_requeues_the_url_and_a_final_one_fails_it`. `test_catalog.py`: `test_a_job_capture_is_stored_with_the_result_and_cleared_by_the_next_one`. `test_wayback.py`: `test_spn2_posts_with_authorization_and_polls_until_the_job_succeeds`, `test_spn2_job_outcomes`, `test_submissions_share_one_paced_rate_and_availability_checks_do_not_wait`, `test_spn2_transient_error_while_polling_keeps_the_job_and_never_posts_again`, `test_spn2_polling_stops_with_submitted_on_a_stop_request_or_an_unreadable_reply`. `test_runner.py`: `test_runner_uses_spn2_when_keys_are_set`, `test_runner_paces_only_submissions_with_the_archive_delay`, `test_api_keys_never_reach_logs`. `test_fetcher.py`: `test_curl_transport_sends_secrets_through_files_and_pins_the_address` |
| FR-12 | `test_urls.py`: `test_unicode_hosts_become_ascii_so_the_check_and_the_fetch_use_one_name`, `test_ip_literals_are_recognized`. `test_fetcher.py`: `test_curl_command_pins_names_but_not_ip_literals`, `test_curl_cffi_does_not_pin_ip_literals`, `test_camoufox_checks_the_redirect_chain_and_restarts_a_crashed_browser`. `test_runner.py`: `test_a_failure_at_the_wayback_endpoint_is_not_blamed_on_the_target_url`. `test_docs_hygiene.py` (public documents hold no internal notes, and the README follows the sentence, paragraph, phrase, and punctuation rules of `asd-ste.yaml`). `test_fetcher.py`: `test_curl_cffi_pins_a_list_of_addresses_and_falls_back_to_the_reachable_one`, `test_interruptible_sleep_waits_until_a_stop_request_ends_it`. `test_urls.py`: `test_validate_public_host_blocks_private_and_link_local_ips`, `test_host_port_uses_scheme_defaults_and_rejects_bad_input`. `test_fetcher.py`: `test_redirects_validate_targets_limit_hops_and_switch_method`, `test_redirect_edge_cases_and_post_method_rules`, `test_fetch_pins_validated_ip_and_applies_body_cap_default_and_override`, `test_curl_transport_caps_large_and_chunked_bodies_and_keeps_headers`, `test_read_capped_cuts_at_the_limit` |
| Coverage measure | `test_catalog.py`: `test_archive_coverage_counts_fresh_snapshots`. `test_runner.py`: `test_crawl_summary_percent_handles_an_empty_catalog` |
