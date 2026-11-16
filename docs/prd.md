# Product Requirements Document: Wayback Site Archiver

## Problem Statement

Web pages vanish. Some pages change in hours, such as news, notices, and price lists. The Wayback Machine cannot reach every page. It misses deep pages that no popular link points to. It also misses pages that block its crawler.

The archiver closes that gap. It finds as many pages of a site as it can, and every external page that the site links to. It asks the Wayback Machine to capture each page that has no recent capture.

Coverage is the first goal. A page that our crawler cannot read can still be captured by the Wayback Machine from its own servers. So the archiver submits such pages and does not drop them. Politeness limits protect the origin server, but they never remove a page from the submission queue.

## Success Measure

**Coverage** is the share of eligible URLs in the site's catalog that hold a confirmed Wayback snapshot newer than the freshness window (7 days by default) at the end of a run.

- A URL is *confirmed* when the availability check finds a fresh snapshot, or when an authenticated capture job succeeds. Its status is `ARCHIVED`. Coverage counts a confirmed result that the tool recorded within `rearchive_window_days`. The freshness check on the snapshot itself (`wayback_max_age_days`) happens when the tool checks the URL.
- A URL that archive.org accepted without confirmation has the status `SUBMITTED`. The tool reports these separately as *unconfirmed*. They do not count as covered.
- A URL is *eligible* unless its status is `SKIPPED`. A URL is skipped when the origin reports it as gone (HTTP 404 or 410) or when its host has a non-public address. Eligible URLs that the run did not reach count as not covered.
- Each seed host has its own catalog file, so one run measures one site.

The tool prints coverage in its last output line: `Coverage: <covered>/<eligible> (<percent>%) confirmed, <unconfirmed> submitted but unconfirmed`. The PRD sets no numeric target. The owner sets one after the first full runs.

## Target User and CLI Workflow

Archivists, researchers, and site owners who run Python 3.12 locally. They need a resumable CLI tool with a low resource footprint:

```bash
uv run python -m civil_service.main https://example.com --config config.yml
```

An optional second mode uses an archive.org account. The user sets two environment variables. The tool then confirms each capture. See FR-11.

## Scope

In scope:

- Crawl HTML pages on the start URL's domain.
- Discover pages from sitemaps and from page links.
- Check and submit internal pages and depth-1 external links to the Wayback Machine.
- Resume an interrupted run from local state.

Out of scope:

- Crawl of external domains beyond the one linked page.
- Separate submission of images, scripts, and style sheets. The Wayback Machine captures these with each page. The SPN2 documentation (updated 2026-07-22) states that a capture also saves the page embeds.
- Solving or bypassing CAPTCHAs and other interactive challenges.
- Form submission, login, and page interaction.
- Distributed queues and LLM use during a crawl.
- A numeric coverage target.

## Functional Requirements

Each requirement lists acceptance criteria. A criterion is true when a test in `tests/` proves it.

### FR-1 Config-Driven Execution

All tunable parameters live in `config.yml` and load through `pydantic-settings`. The code declares no default value. Fixed protocol and library choices stay in code: the meaning of HTTP statuses, the SQLite busy timeout (5,000 ms), and the browser launch options. `--config FILE` overlays the file on the `config.yml` in the working directory. Only the two archive.org API keys come from the environment.

- A key in `--config FILE` overrides the same key in the working-directory file. A key that neither file holds stops the run with a configuration error.
- An unknown key, a file that is not a YAML mapping, or a value outside its bound stops the run with a configuration error. This includes an empty `fallbacks` list and a `rearchive_window_days` below 1.
- Environment variables do not change any setting except the two API keys.
- The API keys never appear in `config.yml`, in logs, in `model_dump` output, or in a command line.

### FR-2 Polite Crawling and Backoff

The crawler obeys `robots.txt` for its own fetches. It respects HTTP `429` and `503`, and `Retry-After` in seconds or as an HTTP date. The cap is `max_retry_after_seconds`.

- On `429` or `503` without `Retry-After`, the fetcher retries the same transport after 5, 10, and 20 seconds. Then it raises a rate-limit error.
- `Retry-After` replaces the default delay for that retry.
- A rate limit never moves the request to the next transport, because all transports share one IP address.
- A rate limit on one host pauses every thread for that host until the delay ends. Other hosts continue.
- A disallowed URL gets no crawl request. It still goes to the Wayback Machine (FR-8).
- `robots.txt` gates the URL that a crawl worker claims. It does not gate the redirect targets that the fetch follows or the sitemap files (sitemaps are published for crawlers).
- The host pause is per host name. `archive.org` (availability) and `web.archive.org` (saves) are separate hosts with separate pauses.
- A slow `robots.txt` holds back only the threads that wait for the same origin.
- An unreachable `robots.txt` (network error, `403`, or `5xx`) allows every URL. A `4xx` answer also allows every URL.

### FR-3 Tiered Transport Fallback

The fetcher tries `curl`, then `curl_cffi` (browser TLS impersonation), then one lazy shared `camoufox` browser. All use `User-Agent: Wayback Archiving`. The browser starts only when needed.

- A transport error, an HTTP `403`, or an HTTP `5xx` (except `503`) moves the request to the next transport at once, with no wait.
- Impersonation and the stealth browser are permitted. The tool never solves or bypasses a CAPTCHA or any interactive challenge.
- On a call to archive.org, a `403` fails at once. It means bad API keys or a refusal, not an anti-bot gate.
- If every transport fails, the error is transient when any transport failed with a timeout, network error, or `5xx`. Otherwise it is final.
- A missing `camoufox` browser is a final error for that transport. The run continues with the other transports.
- The browser follows redirects inside the page. Its route guard checks the first request of a navigation, and the tool checks every hop of the redirect chain after the load. A page that came through a non-public hop is dropped, but the browser has already sent a request to that address. Removing `camoufox` from `fallbacks` rules that out. The route guard also checks the other requests that the page makes against the public-address rule. A POST, or a GET with headers, goes through the browser's API client instead. That client is not covered by the route guard. It gets only the check before the call, and it follows no redirects. The tool cannot pin the address for the browser. The browser reads non-HTML answers as raw bytes.

### FR-4 Scope and Discovery

The crawler spiders `text/html` pages on the start URL's domain (and subdomains when `include_subdomains` is true). It checks and archives internal URLs of any type. It also archives depth-1 external links without spidering them.

- The crawler reads page links from `a`, `area`, `iframe`, `frame`, and `link rel="alternate"` or `rel="canonical"`. It ignores `img`, `script`, and style sheet links.
- Before the crawl starts, the tool reads the `Sitemap:` lines of `robots.txt` and `/sitemap.xml`. It follows sitemap indexes on the same domain. It reads gzip sitemaps. It fetches at most `max_sitemaps` sitemap files.
- A sitemap file is read up to `max_sitemap_bytes`, which is 50 MB by default (the sitemaps.org limit). A gzip file that expands beyond the limit is rejected and logged.
- A redirect to another URL on the same domain records the final URL as a crawled page.
- A redirect to another domain makes the target an external link. The tool submits it and does not read its links. To crawl a site that redirects to a `www` host, seed with the final host or set `include_subdomains: true`.

### FR-5 Idempotent SQLite Catalog

The catalog records the crawl status, the archive status, the attempt counts, and the timestamps of every URL in SQLite (WAL mode).

- A URL processed less than `rearchive_window_days` ago (7 by default) is not queued again.
- When a run finds a URL again after the window, the URL returns to the queues. Internal URLs return to both queues. External URLs return to the archive queue. Attempt counts reset to zero. This includes `FAILED` and `SKIPPED` URLs.
- The window is at least 1 day, so a URL that the same run finds a second time never re-queues.
- Each seed host has its own catalog file (`{host}` in `db_path`). A catalog never mixes sites.
- An interrupted run resumes. Rows left in progress return to pending at startup. This holds after a clean stop and after a kill with no cleanup.
- A run that repeats inside the window fetches no page, sends nothing to archive.org, and changes no catalog row. It reads `robots.txt` and the sitemaps again.
- The tool saves the links of a page before it marks the page done. A crash between the two steps loses no link.
- Only one run can use a catalog at a time. A second run stops with an error. The lock is a POSIX file lock, so Windows has no lock.
- A catalog from an earlier version gains the attempt columns and indexes with no data loss.

### FR-6 Decoupled Two-Queue Execution

Crawl workers (`threads: 2`) and archive workers (`archive_threads: 1`) run on separate queues.

- A slow Wayback call does not delay page fetches. The two queues share only the catalog, the host pause table, and the single `camoufox` browser.
- An internal URL becomes eligible for archiving when its crawl ends, so the catalog can tell whether the page is gone.
- The tool stops when both queues are empty, or when `max_crawl_pages` and `max_archive_urls` are reached.
- Cleanup never hides the real exit reason. A terminal Ctrl-C also kills the browser's helper process, and closing that dead browser fails. The tool logs a `browser_close_failed` event, ignores the failure, and still exits with code 130.
- An error in an archive worker stops the whole run. Every worker is joined before the catalog closes.
- One bad URL never ends the run. Each crawl and archive step catches every error, records a failure for that URL, and goes on. A malformed link is dropped at parse time. Any failure to read `robots.txt` allows every URL.
- Ctrl-C asks all workers to finish and claim no more. Every wait ends at once: a host pause, the pacing between submissions, and a job poll. A request that is already in flight finishes first, which takes at most `request_timeout_seconds`. A URL that a stop interrupts stays in progress and returns to pending at the next start. After a stop request, no transport sends another request. The `curl` and browser children start in their own session, so a terminal Ctrl-C does not kill a request in flight. The tool exits with code 130. A second Ctrl-C quits immediately. The next run resumes.
- An unhandled error in a crawl worker ends the crawl phase. Archive workers finish the URLs that are already eligible. Then the error reaches the caller. No worker waits forever.

### FR-7 Observability

- The tool shows live progress with counts of done and remaining items. The catalog queries and the display updates happen at most once each `progress_refresh_seconds`.
- The tool writes structured JSON logs to standard error.
- The last output lines show the counters and the coverage. `crawled` counts pages that returned any HTTP answer. `robots_blocked` and `crawl_failed` count pages that we could not fetch (both still go to Wayback). `archived` counts confirmed snapshots. `submitted` counts accepted, unconfirmed submissions. `archive_skipped` counts non-public URLs. `archive_failed` counts refusals.
- The tool logs a `body_capped` event whenever a response is cut at the size limit.

### FR-8 Submit Pages We Cannot Fetch

An internal URL goes to the Wayback Machine even when our fetch fails. Only a page that the origin reports as gone is skipped.

- Statuses `404` and `410` set the archive status to `SKIPPED`. The URL is not submitted.
- A robots block, a `403`, a `5xx`, a timeout, and a rate limit do not skip the URL. It stays eligible.
- A failed crawl records status `0`. A robots block records status `-1`. The catalog keeps the two apart.

### FR-9 Freshness

The tool submits a URL when its newest Wayback capture is older than `wayback_max_age_days` (7 by default). A value of `null` accepts any capture.

- The availability query carries the current time, so "closest" means the newest capture.

### FR-10 In-Run Retry of Transient Failures

A timeout, network error, `5xx`, or rate limit does not end a URL at once. The tool queues it again, behind URLs that have fewer attempts.

- A URL gets at most `max_transient_attempts` attempts (3 by default) in each queue. A retry does not use up `max_crawl_pages` or `max_archive_urls`. Those limits count URLs that end.
- A host that does not resolve counts as transient, because the resolver can fail while the network is down. After the attempts, the URL fails. A host name that cannot be written as a valid ASCII name, or a host with a non-public address, is not transient: the URL is skipped and never sent.
- At the limit, a crawl failure records status `0` and the URL stays eligible for archiving. An archive failure records `FAILED`.

### FR-11 Authenticated Save Page Now (Optional)

When `CIVIL_SERVICE_SPN_ACCESS_KEY` and `CIVIL_SERVICE_SPN_SECRET_KEY` are both set, the tool uses the SPN2 API.

- The tool sends a `POST` with an `Authorization: LOW <access>:<secret>` header and polls the job status.
- A job that succeeds gives status `ARCHIVED`. A job that fails gives `FAILED`. A job still pending after `spn2_max_polls` polls gives `SUBMITTED`.
- Without keys, the tool uses the anonymous save URL. `SUBMITTED` then means that archive.org accepted the request. Nothing confirms the capture.
- Submissions share one pacing clock across all archive threads: `spn2_delay_seconds` with keys, `archive_delay_seconds` without keys. Availability checks are not paced.
- A job success counts as `ARCHIVED` only when the job reports a snapshot time. The tool stores the snapshot time and the captured URL with the row (`snapshot_ts`, `snapshot_url`). A success without a snapshot time leaves the status `SUBMITTED`.
- A job that ends in a temporary error (a session limit, a timeout, a busy service) goes back to the queue like any transient failure. A job that ends in a final error (a blocked URL, a page that is gone, the daily limit for one URL) fails the URL.
- The tool writes one `archive_job` log event for each job, with the job id, the status, the error code, the message, the snapshot time, and the captured URL. It never logs headers.
- A transient error while polling a job leaves the status `SUBMITTED`. The tool does not post the URL again for that reason.
- Without keys, the tool cannot confirm a capture. Its own submissions stay `SUBMITTED`. With the default settings, `rearchive_window_days` and `wayback_max_age_days` are both 7, so the next run after the window finds the old snapshot stale and submits the URL again. A `wayback_max_age_days` above `rearchive_window_days` lets the next run confirm the snapshot instead.
- A timeout or a transient error can make the tool send a submission more than once (the next transport or an in-run retry sends it again). The SPN2 documentation says that a repeat inside one hour returns the recent capture, and it caps captures at 5 a day for one URL. The documentation does not state this for the anonymous save URL.

### FR-12 Request Safety

- Before every request and every redirect hop, the tool resolves the host and refuses non-public addresses.
- A host name with non-ASCII letters becomes its ASCII form (as Python's IDNA codec writes it) before the check, so the check and the fetch use one name. An IP-literal URL is checked but has no name to pin.
- `curl` and `curl_cffi` connect to the addresses that passed the check (every address that the name resolved to, so a host with an unreachable IPv6 address still connects over IPv4).
- The tool drops a link that carries a user name or password. A start URL with credentials is refused. This closes the gap between check and use.
- The tool follows at most `max_redirects` redirects and validates each target.
- The tool keeps at most `max_body_bytes` of a response in memory (10 MB by default). A larger body is cut and logged.
- Requests that carry credentials never follow redirects. A redirect to another host, or from https to http, drops all request headers, so credentials never leave the origin that the caller named.
- `curl` runs with `-q` (no `~/.curlrc`) and `--noproxy "*"`. `curl_cffi` runs with `trust_env=False`. Neither reads proxy settings, so the address check and the pinned address stay valid. The tool does not work behind a required proxy.
- The `curl` child process does not receive the archive.org API keys in its environment. The `camoufox` browser process inherits the environment.
- The tool verifies TLS certificates on every transport.

## Non-Functional Requirements

- **NFR-1 Runtime and Packaging.** Python 3.12 managed only through `uv`. `uv.lock` is committed.
- **NFR-2 Low Resource Footprint.** The tool uses stdlib `sqlite3`, `html.parser`, and `urllib.robotparser`. It builds no DOM tree and no XML tree. It runs at most one `camoufox` instance. No page body stays in memory beyond `max_body_bytes`, except a page that `camoufox` loads (the browser holds the whole page before the tool cuts it). A sitemap can use several times `max_sitemap_bytes` while it is read (raw bytes, unzipped bytes, and the decoded text).
- **NFR-3 Quality Gate.** `pytest` shows 100% line and branch coverage. `mypy --strict`, `ruff` check and format, `deptry`, `bandit`, and `pip-audit` report no issue. `checkpython.sh` runs the gate.
- **NFR-4 No Network in Unit Tests.** Tests fake every external service. The only sockets that tests open are to a local HTTP server on 127.0.0.1, to prove the `curl` transport.

## Limits That Shape the Design

- The SPN2 documentation (updated 2026-07-22) lists these limits: 7 captures a minute and 30,000 a day for authenticated users, 3 a minute and 200 a day for anonymous users, 5 captures a day for one URL, and a 2-minute maximum capture time. The default `spn2_delay_seconds` of 9.0 keeps authenticated use under 7 a minute.
- The anonymous save URL used without keys is the older interface. The SPN2 documentation does not state its limits. The default `archive_delay_seconds` of 4.0 (15 requests a minute) is a design choice that no source confirms. A `429` from `web.archive.org` pauses later submissions through the host pause.
