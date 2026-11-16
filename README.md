# Wayback Site Archiver

This tool crawls a website. It asks the [Wayback Machine](https://web.archive.org) to save each page that has no recent snapshot.

The Wayback Machine cannot reach all pages. It misses deep pages, pages that change fast, and pages that block its crawler. This tool finds those pages and submits them. If our crawler cannot read a page, the tool still submits the page. The Wayback Machine then reads it from its own servers.

- **Coverage first.** The tool submits a page even if our fetch fails, `robots.txt` disallows it, or the site answers `403` or `5xx`. The tool skips only pages that the site reports as gone (HTTP 404 or 410).
- **More pages.** The tool reads sitemaps (plain, gzip, and nested). It follows `a`, `area`, `iframe`, `frame`, and `link rel="canonical"` or `rel="alternate"` links.
- **Safe to repeat.** The tool keeps its state in a local SQLite file. You can stop a run at any time and start it again.
- **Gentle on servers.** The tool obeys `429`, `503`, and `Retry-After`. A rate limit on one host pauses all threads for that host.
- **Fresh snapshots.** A snapshot older than 7 days (you can change this) causes a new capture.
- **Optional confirmation.** With free archive.org API keys, the tool confirms each capture.
- **Small footprint.** The tool uses the Python standard library for the database, HTML, and `robots.txt`.

## Contents

- [How it works](#how-it-works)
- [Requirements and install](#requirements-and-install)
- [Quick start](#quick-start)
- [What the tool does with each URL](#what-the-tool-does-with-each-url)
- [Configuration](#configuration)
- [Stop, resume, and repeat](#stop-resume-and-repeat)
- [Authenticated Save Page Now (optional)](#authenticated-save-page-now-optional)
- [Responsible use](#responsible-use)
- [Limits](#limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Documentation](#documentation)

## How it works

```text
start URL ──► sitemaps + robots.txt ──► catalog (SQLite) ◄──────────────┐
                                            │                           │
                           ┌────────────────┴───────────────┐           │
                           ▼                                ▼           │
                  crawl queue (2 threads)         archive queue (1 thread)
                  fetch page, read links          check Wayback for a fresh snapshot
                  curl → curl_cffi → camoufox     submit the URL if there is none
                           │                                │
                           └── new URLs go back to the catalog ──────────┘
```

The two queues run at the same time. A slow Wayback call does not stop the page fetches.

The crawl stays on the start site. It also stays on subdomains if you set `include_subdomains`. The tool checks and submits links to other sites. It does not crawl them.

## Requirements and install

You need these items:

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- The `curl` command (macOS and most Linux systems have it)

Clone the repository. Go to its folder. Install the dependencies:

```bash
uv sync
```

The tool fetches a page with up to three transports. It tries `curl` first. It tries `curl_cffi` (browser-like TLS) second. It tries a headless [Camoufox](https://camoufox.com) browser last. The browser starts only if the first two transports fail. Download the browser one time:

```bash
uv run python -m camoufox fetch
```

If you skip this step, the tool still runs. A page that needs the browser counts as a failed fetch. The tool still submits the URL.

## Quick start

Run this command in the project folder. The folder holds `config.yml`.

```bash
uv run civil-service https://example.com
```

This command does the same work without the console script:

```bash
uv run python -m civil_service.main https://example.com --config config.yml
```

The tool shows two progress bars. One bar shows the crawl. The other bar shows the archive work. The tool writes logs to standard error. Each log line is one JSON object. The last lines look like this (the numbers are an example):

```text
Done: crawled=120 robots_blocked=2 crawl_failed=5 archived=40 submitted=75 archive_skipped=1 archive_failed=3
Coverage: 40/118 (33.9%) confirmed, 75 submitted but unconfirmed
```

This example comes from a run without API keys. In that mode the tool cannot confirm a capture. It counts its own submissions as "unconfirmed". With [API keys](#authenticated-save-page-now-optional), the tool confirms each capture. Then `archived` replaces most of `submitted`.

| Counter | Description |
| --- | --- |
| `crawled` | Pages that returned an HTTP answer. Any status counts. |
| `robots_blocked` | Pages that `robots.txt` did not let us fetch. The tool still submits them. |
| `crawl_failed` | Pages that the tool could not fetch. The tool still submits them. |
| `archived` | URLs that hold a confirmed snapshot inside the freshness window. |
| `submitted` | URLs that archive.org accepted. Nothing confirms the capture. |
| `archive_skipped` | URLs whose host has a private network address. The tool never sends them. |
| `archive_failed` | URLs that archive.org refused or could not process. |

The coverage line counts the eligible URLs that hold a confirmed snapshot. The tool must have recorded the snapshot in the last `rearchive_window_days` days. A URL is eligible unless the tool skips it. The tool skips a URL if the site reports it as gone or if its host is not public. A URL that the run did not reach is eligible. It counts as not covered.

Without API keys, the next run after the window finds that the tool's own snapshot is too old. Both `wayback_max_age_days` and `rearchive_window_days` default to 7 days. The tool then submits the URL again. This gives a weekly refresh. To confirm the snapshot instead, set `wayback_max_age_days` above `rearchive_window_days` (for example, 14 and 7). API keys also confirm captures.

| Exit code | Description |
| --- | --- |
| `0` | The run finished. |
| `1` | An error stopped the run. The cause can be the configuration, the start URL, a start host that does not resolve, or a catalog that another run uses. |
| `130` | You pressed Ctrl-C. |

Error messages on standard error are plain text. Only the log lines are JSON.

## What the tool does with each URL

| Situation | Result |
| --- | --- |
| The page loads and is HTML | The tool adds its links to the catalog. It checks and submits the page. |
| The page loads and is not HTML (PDF, image) | The tool does not parse it. It checks and submits the URL. |
| HTTP 404 or 410 | The tool skips the URL. The site says the page is gone. |
| `robots.txt` disallows the page | We do not fetch the page. The tool still checks and submits it. |
| HTTP 403 | The tool tries the next transports. If all refuse, the fetch counts as failed. The tool still submits the URL. |
| HTTP 5xx, timeout, network error | The tool tries the next transport. Then it retries later in the run (3 attempts). It still submits the URL. |
| HTTP 429 or 503 | The tool waits (`Retry-After`, or 5, 10, and 20 seconds). It retries on the same transport. Then it retries later. The host pauses for all threads. |
| Redirect to the same site | The tool records the final URL as the page. |
| Redirect to another site | The target becomes an external link. The tool submits it and does not crawl it. |
| Private or loopback address | The tool never fetches it and never submits it. |
| Link to another site | The tool checks and submits it. It does not crawl it. |
| Link with a user name or password | The tool ignores it. Credentials never reach a public archive. |

A start URL can redirect from `example.com` to `www.example.com`. The tool treats `www.example.com` as another site. In this case, start with the final host. You can also set `include_subdomains: true`.

The tool does not submit images, scripts, and style sheets one by one. Wayback saves them with each page.

## Configuration

All options are in [`config.yml`](config.yml). Comments explain the options that need them. Use `--config` to pass another file. Keys in that file override the `config.yml` in the working directory.

The tool stops with an error in three cases:

- A key is unknown.
- A key is missing.
- A value is out of range.

The error names the key and the problem.

The code has no built-in defaults. A custom file needs every key, or a complete `config.yml` must be in the working directory. The [technical spec](docs/spec.md) lists every key.

These are the settings that you change most:

| Key | Default | What it does |
| --- | --- | --- |
| `user_agent` | `Wayback Archiving` | The tool sends it on every request. See [Responsible use](#responsible-use). |
| `threads` | `2` | The number of crawl threads. |
| `max_crawl_pages` | `null` | The run stops after this many pages. `null` means no limit. |
| `max_archive_urls` | `null` | The run stops after this many URLs checked against Wayback. A retry does not count. |
| `include_subdomains` | `false` | If `true`, `blog.example.com` belongs to `example.com`. |
| `respect_robots_txt` | `true` | The crawler obeys `robots.txt` for its own fetches. |
| `wayback_max_age_days` | `7` | A snapshot older than this causes a new capture. `null` accepts any snapshot. |
| `rearchive_window_days` | `7` | The number of days before the tool queues a processed URL again. |
| `archive_delay_seconds` | `4.0` | The pause between anonymous submissions. |
| `max_sitemaps` | `20` | The number of sitemap files that one run reads. |
| `max_body_bytes` | `10485760` | The largest page body that the tool keeps in memory. |
| `db_path` | `data/{host}.sqlite3` | The catalog file. `{host}` becomes the start host. |

To test your setup, use a small run:

```yaml
max_crawl_pages: 20
max_archive_urls: 20
```

## Stop, resume, and repeat

The tool saves its state after each step. You can stop it at any time. To continue, run the same command again.

**Repeat a finished run.** If all URLs are inside the window, the tool fetches no page. It sends nothing to archive.org. It changes no catalog row. It reads `robots.txt` and the sitemaps again.

**Run again after the window.** If a run finds a URL after the window, the URL returns to the queues. The tool fetches and checks it again. A URL that no link or sitemap reaches stays unchanged.

**Stop with Ctrl-C.** All waits end now. A request that is in flight finishes first. This takes at most `request_timeout_seconds`.

Then the tool saves its state and exits with code 130. Press Ctrl-C a second time to quit immediately. A URL that the stop interrupted returns to the queue.

**Stop without cleanup.** If the process dies (`kill -9`, a crash), run the same command. The tool puts the URLs that were in progress back in the queue.

The tool saves the links of a page before it marks the page done. A crash therefore loses no link. The tool can fetch again a page that was in progress.

A power loss can lose the last few saved steps. The next run does that work again.

**One catalog for each site.** The tool names the catalog file after the start host. A start URL with another host name uses another catalog. For example, `example.com` and `www.example.com` have two catalogs.

**One run for each catalog.** A second run on the same catalog stops with an error. The lock ends when the other process ends. A crashed run does not block you. Windows has no lock, so run one process for each catalog there.

To look inside the catalog, use `sqlite3`:

```bash
sqlite3 data/example.com.sqlite3 "SELECT archive_status, COUNT(*) FROM urls GROUP BY 1"
sqlite3 data/example.com.sqlite3 "SELECT url, crawl_status FROM urls WHERE archive_status = 'FAILED'"
```

`crawl_status` holds the HTTP status. It holds `0` if the fetch failed. It holds `-1` if `robots.txt` blocked the crawl.

An older catalog (without the retry counters) upgrades itself when you first open it.

## Authenticated Save Page Now (optional)

An archive.org account gives higher capture limits. The tool can then confirm each capture. Without keys, the tool works, but it cannot confirm a capture.

### Set up the keys

1. Create an account at <https://archive.org>. Read the archive.org terms. You must do this yourself.
2. Open <https://archive.org/account/s3.php>. Enter your account name and password if the page asks. The page shows your **access key** and **secret key**. Create the keys there if none exist.
3. Put both keys in the environment of the shell that runs the tool. Do not put them in `config.yml`. The tool reads no `.env` file.

   ```bash
   export CIVIL_SERVICE_SPN_ACCESS_KEY=your-access-key
   export CIVIL_SERVICE_SPN_SECRET_KEY=your-secret-key
   ```

   To keep the keys between sessions, add the two lines to your shell profile (`~/.zshrc` or `~/.bashrc`). Restrict that file to your user with `chmod 600`. To use the keys for one command only, put them before the command: `CIVIL_SERVICE_SPN_ACCESS_KEY=... CIVIL_SERVICE_SPN_SECRET_KEY=... uv run civil-service https://example.com`.
4. Test the keys with a small run on a site that you own. Set `max_crawl_pages: 5` and `max_archive_urls: 5` in `config.yml`. Run the tool:

   ```bash
   uv run civil-service https://your-site.example 2> run.log
   sqlite3 data/your-site.example.sqlite3 "SELECT archive_status, COUNT(*) FROM urls GROUP BY 1"
   ```

   A good result shows `ARCHIVED` rows and a coverage line with confirmed URLs. The tool polls each job for about one minute (`spn2_max_polls` times `spn2_poll_seconds`). A slower capture stays `SUBMITTED`. The tool does not confirm it.

   If you see `FAILED` rows, read the `error` field in `run.log`. Use `jq -c 'select(.event == "archive_failed")' run.log`. An HTTP 401 or 403 in the message most likely means that a key is wrong. When the test passes, remove the two limits from `config.yml`.

Set both keys. If one key is empty or missing, the tool uses anonymous mode. The tool does not warn you.

With keys, the tool sends each submission through the SPN2 API. It polls the job and records `ARCHIVED` for a confirmed capture.

The keys stay out of logs, the catalog, and command lines. A request that carries keys never follows a redirect. As a second layer, a redirect to another host, or from https to http, drops all request headers.

The `curl` child process gets no keys in its environment. The headless browser is the last fallback. It is a child process that inherits your environment. It never sends the keys. Other programs that run as your user can read them from its environment.

## Responsible use

- **Archive only public content that you may archive.** The tool sends URLs to a public archive. Everything that it submits becomes public. Do not point it at private or sensitive sites.
- **`robots.txt` applies to our own requests.** The crawler skips pages that `robots.txt` disallows. The tool still submits those URLs to the Wayback Machine. The Wayback Machine applies its own policies. If you do not want a page archived, tell archive.org. Do not use this tool for that page.
- **Tell site owners who you are.** The default user agent is `Wayback Archiving`. Add a way to contact you. For example: `Wayback Archiving (+https://example.org/contact)`.
- **Fallback transports.** After a `403`, the tool retries the page with browser-like TLS. Then it tries a headless browser. The tool never solves or bypasses a CAPTCHA or other interactive challenge.
- **Be gentle with small sites.** Lower `threads` or set `max_crawl_pages` before you crawl a site that you do not run.

## Limits

- Archive.org limits the submission rate. The authenticated SPN2 interface allows 7 captures a minute and 30,000 a day. It allows 5 captures a day for one URL ([SPN2 documentation](https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA/edit)). The tool keeps authenticated runs under 7 a minute.
- The same document gives lower limits for anonymous SPN2 users: 3 captures a minute and 200 a day. The default delay of 4 seconds gives 15 a minute. This is a design choice, and it is higher than the documented figure.
- The document does not state the limits of the older anonymous save URL that the tool uses without keys. If archive.org answers `429`, raise `archive_delay_seconds`. For a large site, use API keys. They allow 30,000 captures a day.
- A large site takes hours or days at these rates. The catalog lets you stop and resume.
- The tool connects directly. It ignores proxy variables and `~/.curlrc`, so its address checks stay valid. The tool does not work behind a required proxy.
- The headless browser cannot pin addresses. After a page loads, the tool checks each hop of the redirect chain. It drops the page if a hop is not public. The browser has already sent one request to that address. To prevent this, remove `camoufox` from `fallbacks`.
- Only the browser transport (the last fallback) runs page scripts. The tool usually does not find links that exist only after JavaScript runs. A sitemap can list them.
- Without API keys, the tool cannot confirm a capture. See the coverage notes in [Quick start](#quick-start).

## Troubleshooting

| Message or symptom | Cause and fix |
| --- | --- |
| `Config file not found` | Pass `--config path/to/config.yml`, or run from the project folder. |
| `Invalid configuration in ...` | The message lists each key and its problem. Start from the shipped `config.yml`. |
| `Failed to resolve host` | The start host does not resolve. Check the spelling and your network. |
| The crawl stops at the start page | The start URL redirected to another site, often `example.com` to `www.example.com`. Start with the final host, or set `include_subdomains: true`. |
| `Invalid or disallowed seed URL` | Use a full `http://` or `https://` URL. |
| `Blocked non-public address` | The host has a private or loopback address. The tool refuses it on purpose. |
| `Another run uses the catalog` | Another process uses this catalog. Wait until it ends, or stop it. |
| `Camoufox could not start` | Run `uv run python -m camoufox fetch`. The run continues without the browser. |
| Many `crawl_retry` or `archive_retry` log events | The site rate limits the tool. Lower `threads`, or wait. The tool waits longer after each rate limit. |
| Many `archive_failed` events | Read the `error` field in the log lines. If archive.org answers `429`, raise `archive_delay_seconds`. |
| The run seems stuck | A host pause or a long `Retry-After` is active. The progress bar moves again after the wait. |

The logs are JSON lines on standard error. Write them to a file. Filter them with `jq`:

```bash
uv run civil-service https://example.com 2> run.log
jq -c 'select(.level == "warning")' run.log
```

## Development

Run the full quality gate:

```bash
uv sync
uv run ./checkpython.sh
```

GitHub Actions runs the same script on each push and pull request (`.github/workflows/ci.yml`).

The script runs `ruff` (lint and format check), `mypy --strict`, `pytest`, `deptry`, `bandit`, and `pip-audit`. `pytest` needs 100% line and branch coverage. The tests use fake network layers. The `curl` transport tests use a small local HTTP server on `127.0.0.1`. No test uses the internet.

```text
src/civil_service/
├── exceptions.py error types
├── main.py       command line (Typer)
├── config.py     settings from YAML, API keys from the environment, logs
├── runner.py     crawl and archive queues, robots.txt, sitemaps, progress
├── fetcher.py    curl / curl_cffi / camoufox chain, retries, redirects, size cap
├── wayback.py    availability check, anonymous save, SPN2 submit and poll
├── catalog.py    SQLite catalog, queue state, lock
└── urls.py       URL rules, link and sitemap parsing, address checks
tests/            unit and integration tests
docs/             requirements, spec, design notes
```

Pre-commit hooks are in `.pre-commit-config.yaml`. Run `pre-commit install` to use them.

Read the [spec](docs/spec.md) before you change behavior. Each requirement in it points to the tests that prove it.

## License

MIT. See [LICENSE](LICENSE).

## Documentation

- [Product requirements](docs/prd.md): goals, requirements, and acceptance criteria
- [Technical specification](docs/spec.md): configuration, catalog states, fetch policy, Wayback client, test map
- [Design notes](docs/superpowers/specs/2026-11-15-wayback-archiver-design.md): the decisions behind the design
- [Changelog](CHANGELOG.md)
