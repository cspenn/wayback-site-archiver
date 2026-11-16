import http.server
import socket
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

_HOST_IPS = {
    "private.internal": "10.0.0.5",
    "meta.internal": "169.254.169.254",
    "169.254.169.254": "169.254.169.254",
    "127.0.0.1": "127.0.0.1",
}
_BIG_BODY_BYTES = 30_000_000
_CHUNK = b"y" * 100_000


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public IP unless a test names a private one."""

    def fake_getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        if host == "unresolvable.invalid":
            raise socket.gaierror(socket.EAI_NONAME, "unresolvable")
        if host == "dns-flaky.invalid":
            raise socket.gaierror(socket.EAI_AGAIN, "try-again")
        if ".." in host or any(len(label) > 63 for label in host.split(".")):
            raise UnicodeError("bad-label")
        if host == "oserror.invalid":
            raise OSError("network-down")
        if host == "dual.example":
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1946", 0, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            ]
        if host == "mixed.example":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ]
        ip = _HOST_IPS.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _repo_root_as_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings read ./config.yml, so run every test from the repo root."""
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def _no_spn_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep keys from the developer's shell out of the tests."""
    monkeypatch.delenv("CIVIL_SERVICE_SPN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("CIVIL_SERVICE_SPN_SECRET_KEY", raising=False)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/big":
            self._start(200, "text/html", length=_BIG_BODY_BYTES)
            self._write_all(lambda: self.wfile.write(b"x" * 100_000))
        elif self.path == "/chunked":
            self._start(200, "text/plain", chunked=True)
            self._write_all(lambda: self.wfile.write(b"186a0\r\n" + _CHUNK + b"\r\n"))
        elif self.path == "/html":
            body = b"<html>hello</html>"
            self._start(200, "text/html", length=len(body))
            self.wfile.write(body)
        else:
            self._start(404, "text/plain", length=2)
            self.wfile.write(b"no")

    def do_POST(self) -> None:  # noqa: N802
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        out = (self.headers.get("X-Secret", "") + "|" + data.decode()).encode()
        self._start(201, "text/plain", length=len(out))
        self.wfile.write(out)

    def _start(
        self, status: int, content_type: str, length: int | None = None, chunked: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _write_all(self, write_once: object) -> None:
        """Write a large body in pieces. A client that stops early is not an error."""
        try:
            for _ in range(300):
                write_once()  # type: ignore[operator]
        except (BrokenPipeError, ConnectionResetError):
            return


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address


@pytest.fixture
def local_server() -> Iterator[str]:
    """Serve /html, /big, /chunked, a POST echo, and 404s on 127.0.0.1. Yield the base URL."""
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
