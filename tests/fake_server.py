"""A server that can be told to behave badly, in each way the design claims to survive.

One payload, served in the shapes a real host is allowed to have: proper ranges,
ranges it ignores, a body that dies half way, and a range it refuses outright.
Every request is recorded, because the interesting assertions are about what was
asked for — starting a resumed window at the frontier rather than at zero.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILE_NAME = "blob.bin"
DISPOSITION = f'attachment; filename="{FILE_NAME}"'


class FakeServer:
    """A host under test.

    ranges=False     — ignores `Range` and answers the whole file with 200
    drop_ranges      — these window starts get half a body and then EOF, once
    drop_whole       — the same, for the single-stream answer
    refuse_ranges    — every range answers 416 (the probe's own 0-0 included)
    head_status      — answer HEAD with this status instead of 200
    flow_chunk/delay — drip the body out, so a download can be caught mid-flight
    """

    def __init__(
        self,
        payload: bytes,
        *,
        ranges: bool = True,
        drop_ranges: object = (),
        drop_whole: bool = False,
        refuse_ranges: bool = False,
        head_status: int | None = None,
        disposition: str | None = DISPOSITION,
        flow_chunk: int = 0,
        flow_delay: float = 0.0,
    ) -> None:
        self.payload = payload
        self.ranges = ranges
        self.drop_ranges = set(drop_ranges)  # type: ignore[arg-type]
        self.drop_whole = drop_whole
        self.refuse_ranges = refuse_ranges
        self.head_status = head_status
        self.disposition = disposition
        self.flow_chunk = flow_chunk
        self.flow_delay = flow_delay
        self.requests: list[tuple[str, str, str]] = []  # method, path, Range header
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread: threading.Thread | None = None

    # ── lifecycle ──

    def __enter__(self) -> FakeServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}/{FILE_NAME}"

    # ── what was asked ──

    def requests_of(self, method: str = "GET") -> list[str]:
        """The Range headers of every `method` request, in order ("" for none)."""
        with self._lock:
            return [header for name, _path, header in self.requests if name == method]

    def range_starts(self, method: str = "GET") -> list[int]:
        return [
            int(header.split("=")[1].split("-")[0]) for header in self.requests_of(method) if header
        ]

    def _record(self, handler: BaseHTTPRequestHandler) -> str | None:
        header = handler.headers.get("Range")
        with self._lock:
            self.requests.append((handler.command, handler.path, header or ""))
        return header

    # ── the handler ──

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # a test run does not need a log
                pass

            def do_HEAD(self) -> None:
                server._record(self)
                if server.head_status is not None:
                    self.send_response(server.head_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(server.payload)))
                if server.ranges:
                    self.send_header("Accept-Ranges", "bytes")
                if server.disposition:
                    self.send_header("Content-Disposition", server.disposition)
                self.end_headers()

            def do_GET(self) -> None:
                header = server._record(self)
                size = len(server.payload)
                if not header:
                    self._body(0, size, ranged=False)
                    return
                first, last = _parse_range(header)
                if not server.ranges:
                    self._body(0, size, ranged=False)
                    return
                if first == 0 and last == 0 and not server.refuse_ranges:
                    self._body(0, 1, ranged=True)  # the probe's own peek
                    return
                if server.refuse_ranges or first >= size:
                    self._refuse()
                    return
                self._body(first, min(last, size - 1) + 1, ranged=True)

            def _refuse(self) -> None:
                body = b"range not satisfiable"
                self.send_response(416)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes */{len(server.payload)}")
                self.end_headers()
                self.wfile.write(body)

            def _body(self, start: int, end: int, *, ranged: bool) -> None:
                length = end - start
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if ranged:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end - 1}/{len(server.payload)}"
                    )
                self.end_headers()
                body = server.payload[start:end]
                dying = (not ranged and server.drop_whole) or (
                    ranged and start in server.drop_ranges
                )
                if dying:
                    server.drop_ranges.discard(start)  # a retry from the frontier must succeed
                    self._write(body[: max(1, length // 2)])
                    self.close_connection = True  # EOF mid-body: the short read is the point
                    return
                self._write(body)

            def _write(self, body: bytes) -> None:
                if not server.flow_chunk:
                    self.wfile.write(body)
                    return
                for at in range(0, len(body), server.flow_chunk):
                    self.wfile.write(body[at : at + server.flow_chunk])
                    self.wfile.flush()
                    if server.flow_delay:
                        time.sleep(server.flow_delay)

        return Handler


def _parse_range(value: str) -> tuple[int, int]:
    spec = value.split("=", 1)[1].strip()
    first, _, last = spec.partition("-")
    return int(first or 0), int(last or 0)
