"""Run the archiver against a fake slow network. test_sigint.py starts this as a subprocess.

Usage: python sigint_driver.py MODE DB_PATH
  MODE host-pause   the site answers 429 with a long Retry-After, so workers wait in a host pause
  MODE slow-request a request in flight takes 60 seconds and cannot be interrupted
  MODE site         a small site of SITE_PAGES pages. Each page fetch prints "FETCHED <url>".
                    test_resume.py kills this process with SIGKILL and then resumes the run.
"""

import socket
import sys
import time
from pathlib import Path

from civil_service.config import AppSettings
from civil_service.fetcher import FetchResult, Request, TieredFetcher
from civil_service.main import hard_stop_on_second_interrupt
from civil_service.runner import run_archiver

_PUBLIC_IP = "93.184.216.34"
_SLOW_REQUEST_SECONDS = 60
_LONG_RETRY_AFTER = "300"
SITE_PAGES = 30
_SITE_PAGE_SECONDS = 0.05


def main(mode: str, db_path: str) -> int:
    socket.getaddrinfo = lambda *_a, **_k: [  # type: ignore[assignment]
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))
    ]

    def site_transport(req: Request) -> FetchResult:
        if req.url.endswith(("/robots.txt", "/sitemap.xml")):
            return FetchResult(404, {}, b"", "text/plain")
        if "archive.org/wayback/available" in req.url:
            stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime()).encode()
            body = b'{"archived_snapshots":{"closest":{"available":true,"timestamp":"%s"}}}' % stamp
            return FetchResult(200, {}, body, "application/json")
        print(f"FETCHED {req.url}", flush=True)
        time.sleep(_SITE_PAGE_SECONDS)
        links = "".join(f'<a href="/p{i}">p</a>' for i in range(SITE_PAGES))
        body = links.encode() if req.url == "https://example.com/" else b"<p>leaf</p>"
        return FetchResult(200, {}, body, "text/html")

    def transport(req: Request) -> FetchResult:
        if mode == "site":
            return site_transport(req)
        if req.url.endswith(("/robots.txt", "/sitemap.xml")):  # the main thread fetches these
            return FetchResult(404, {}, b"", "text/plain")
        print("READY", flush=True)
        if mode == "slow-request":
            time.sleep(_SLOW_REQUEST_SECONDS)
        return FetchResult(429, {"retry-after": _LONG_RETRY_AFTER}, b"", "text/plain")

    settings = AppSettings(
        db_path=Path(db_path),
        fallbacks=["curl"],
        respect_robots_txt=False,
        max_sitemaps=0,
        threads=2 if mode == "site" else 1,
        archive_delay_seconds=0.0,
    )
    fetcher = TieredFetcher(settings, transports={"curl": transport})
    try:
        with hard_stop_on_second_interrupt():
            run_archiver("https://example.com/", settings, fetcher=fetcher)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
