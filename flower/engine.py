"""One download, from probe to landed file: windows, retries, and the stop switch.

Same shape as Fungi's windowed delivery (spec §50), minus the hub. The file is
cut into windows, one thread each, every window retried from where the disk
actually got to rather than from where its request started — that is the whole
of "a window that dies costs a window, not the file". A server that ignores
ranges answers the first request with the whole file from byte zero, and that
response *is* the single stream; nothing is torn down and started again.

Pause and cancel are the same interrupt with one difference: pause keeps the
part (its spans too, so resuming costs only the missing stretch), cancel deletes
it. Neither one is allowed to leave a half file under the real name.
"""

from __future__ import annotations

import http.client
import secrets
import threading
import time
import urllib.error
from collections.abc import Callable
from pathlib import Path

from .landing import Landing
from .net import Client, NetError, as_int
from .probe import Probe, probe

WINDOW_ATTEMPTS = 3
WINDOW_RETRY_WAIT_S = 0.5
MIN_WINDOW_BYTES = 4 * 1024 * 1024  # a window smaller than this is not worth a connection
STREAM_CHUNK = 256 * 1024
MAX_STREAMS = 16

Progress = Callable[[int, int | None], None]


class RangeRefusedError(NetError):
    """The server answered 416: the range was past the end, so the probe is stale."""


def windows(size: int, streams: int, min_bytes: int = MIN_WINDOW_BYTES) -> list[tuple[int, int]]:
    """`[0, size)` cut into at most `streams` ranges, as `(start, end)` pairs."""
    count = max(1, min(int(streams), size // max(1, min_bytes)))
    if count <= 1:
        return [(0, size)]
    step = size // count
    cuts: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        end = size if index == count - 1 else start + step
        cuts.append((start, end))
        start = end
    return cuts


def planned_streams(found: Probe, streams: int) -> int:
    """How many connections this download will actually use — what the interface says."""
    if not found.ranges or not found.size:
        return 1
    return len(windows(found.size, streams))


class Task:
    """One download that can be paused and resumed: the part, the spans, the switch.

    The task outlives its worker, because pausing ends the thread and resuming
    starts another one over the same part file and the same record of what is on
    disk. That record is the reason a resumed download does not start over.
    """

    def __init__(
        self,
        client: Client,
        found: Probe,
        dest_dir: Path | str,
        streams: int = 4,
        tag: str | None = None,
        min_window_bytes: int = MIN_WINDOW_BYTES,
    ) -> None:
        self.client = client
        self.found = found
        self.size = found.size
        self.streams = max(1, min(int(streams), MAX_STREAMS))
        self.min_window_bytes = min_window_bytes
        self.dest = Path(dest_dir) / found.filename
        self.tag = tag or secrets.token_hex(4)
        self.stop = threading.Event()
        self.keep_part = False
        self._started = False
        self._land: Landing | None = None
        self._progress: Progress = lambda _done, _total: None

    def watch(self, callback: Progress) -> None:
        self._progress = callback

    def pause(self) -> None:
        """Stop with the part intact: the next run continues from the frontier."""
        self.keep_part = True
        self.stop.set()

    def cancel(self) -> None:
        """Stop and throw the part away."""
        self.keep_part = False
        self.stop.set()

    def discard(self) -> None:
        """Throw a paused task's part away without fetching anything else."""
        self.keep_part = False
        self.stop.set()
        if self._land is not None:
            self._land.discard()
            self._land = None

    def run(self) -> Path | None:
        """Fetch the file; None means stopped (part kept if paused, gone if cancelled).

        The stop switch is cleared for a resume but not for the first attempt, so a
        pause or cancel pressed while the probe was still running is still honoured
        instead of being wiped out by the run that follows it.
        """
        if self._started:
            self.stop.clear()
        else:
            self._started = True
            if self.stop.is_set():
                return None
        for attempt in (0, 1):
            try:
                return self._attempt()
            except RangeRefusedError:
                if attempt:  # a second 416 means the server is not going to cooperate
                    raise
                self._reprobe()
        raise AssertionError("unreachable")

    def _attempt(self) -> Path | None:
        land = self._land
        if land is None:
            land = self._land = Landing(self.dest, self.size, self.tag)
            land.start(fresh=True)
        else:
            land.start(fresh=False)
        try:
            self._fetch(land)
        except BaseException:
            if not self.keep_part:
                land.discard()
            raise
        if self.stop.is_set():
            if not self.keep_part:
                land.discard()
            return None
        return land.commit()

    def _reprobe(self) -> None:
        """The file moved under us: ask again, and start the part from scratch."""
        found = probe(self.client, self.found.url)
        self.found = found
        self.size = found.size
        self.dest = self.dest.with_name(found.filename)
        assert self._land is not None
        self._land.discard()
        self._land = None

    def _fetch(self, land: Landing) -> None:
        size = self.size
        if not self.found.ranges or not size:
            self._stream(land)
            return
        cuts = windows(size, self.streams, self.min_window_bytes)
        first, last = cuts[0]
        at = land.spans.end_of_run(first)
        if at < last:  # a resumed first window has nothing to ask about when it is done
            with self._open_range(at, last - 1) as response:
                if not _is_ranged(response, at):
                    # No ranges on this server: the answer is the whole file from
                    # byte zero, so it plays the part of the single stream.
                    self._pump(response, land, 0, size)
                    return
        errors: list[BaseException] = []
        workers = [
            threading.Thread(
                target=self._window, args=(land, cut, errors), name=f"flower-window-{cut[0]}"
            )
            for cut in cuts[1:]
        ]
        for worker in workers:
            worker.start()
        self._window(land, cuts[0], errors)
        for worker in workers:
            worker.join()
        if errors:
            raise errors[0]

    def _window(self, land: Landing, cut: tuple[int, int], errors: list[BaseException]) -> None:
        """One window, retried in place: a drop costs this window's tail, not the file."""
        start, end = cut
        at = land.spans.end_of_run(start)
        failure: BaseException | None = None
        for attempt in range(WINDOW_ATTEMPTS):
            if self.stop.is_set():
                return
            if attempt:
                time.sleep(WINDOW_RETRY_WAIT_S * attempt)
            failure = None
            try:
                self._fetch_window(land, at, end)
            except RangeRefusedError:
                raise  # a stale probe is not something a retry can fix
            except (OSError, http.client.HTTPException) as exc:
                failure = exc
            # Where the disk really got to, not where the request started.
            at = land.spans.end_of_run(at)
            if at >= end:
                return
            failure = failure or NetError(f"the server ended the range at {at} of {end}")
        errors.append(failure or NetError(f"window {start}-{end} failed"))
        self.stop.set()

    def _fetch_window(self, land: Landing, start: int, end: int) -> None:
        """One ranged GET into the landing, writing `[start, end)` of the file."""
        response = self._open_range(start, end - 1)
        with response:
            if not _is_ranged(response, start):
                # Mid-file a whole-file answer is useless: it starts at byte zero
                # and this window is not there.
                raise NetError(f"the server ignored the range from {start}")
            self._pump(response, land, start, end)

    def _stream(self, land: Landing) -> None:
        """One GET from byte zero: the shape every server understands."""
        with self.client.open(self.found.url) as response:
            announced = as_int(response.headers.get("Content-Length"))
            if land.expect is None:
                land.expect = announced
            self._pump(response, land, 0, announced)

    def _open_range(self, first: int, last: int):
        """Open one ranged GET. The range is always built by an f-string: a `.replace`
        on a placeholder sends the literal text, which servers answer with an empty
        response and a silent 416, leaving the threads to die one by one."""
        try:
            return self.client.open(self.found.url, {"Range": f"bytes={first}-{last}"})
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                raise RangeRefusedError(f"the server refused bytes {first}-{last}") from exc
            raise

    def _pump(self, response, land: Landing, start: int, end: int | None) -> None:
        """Copy the response body into the landing from `start`.

        `end` is exclusive; None means "to the end of the body". Returning early
        means the server ended the body early or the task was stopped — for a
        window that is the signal to resume, and the resume point is read off the
        landing (what is really on disk) rather than off this call.
        """
        at = start
        with land.writer(start) as handle:
            while end is None or at < end:
                if self.stop.is_set():
                    break
                want = STREAM_CHUNK if end is None else min(STREAM_CHUNK, end - at)
                chunk = response.read(want)
                if not chunk:
                    break
                began = at
                handle.write(chunk)
                at += len(chunk)
                land.written(began, at)
                self._progress(land.spans.bytes, self.size)


def _is_ranged(response, first: int) -> bool:
    """True when the answer is a 206 starting exactly where we asked.

    A 206 whose `Content-Range` starts elsewhere counts as no: bytes would land at
    the wrong offset, which is worse than fetching the window again.
    """
    if getattr(response, "status", 200) != 206:
        return False
    return (response.headers.get("Content-Range") or "").startswith(f"bytes {first}-")
