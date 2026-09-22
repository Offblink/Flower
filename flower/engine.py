"""One download, from probe to landed file: a pool of connections, and the stop switch.

Same shape as Fungi's windowed delivery (spec §50), minus the hub, plus one thing Fungi
did not have: the number of connections adapts while the download runs.

The file is cut into pieces that sit in a queue, and workers pull them. Pieces are small
(a few MiB) because that is what makes adapting cheap — adding a connection is starting
another worker, retiring one is letting it finish the piece in hand, or shrinking that
piece to the frontier so the rest goes back to the queue. Retries are unchanged and are
the part that matters on a bad link: a piece that dies is re-requested from where the
disk actually got to, never from zero.

What adaptation can see from inside the process is limited. The bytes that really go to
waste — a peer still writing into a socket we stopped reading, retransmissions on a
stalling link — are counted by the NIC, not by us. So the signals are the two that are
honestly measurable here: a piece whose body ended before its range did (a stall), and
whether the aggregate rate moved when the last connection was added. Stalls bring the
count down and it stays down; an addition that buys nothing stops the growth.
"""

from __future__ import annotations

import http.client
import secrets
import threading
import time
import urllib.error
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .landing import Landing
from .net import Client, NetError, as_int
from .probe import Probe, probe

WINDOW_ATTEMPTS = 3
WINDOW_RETRY_WAIT_S = 0.5
MIN_WINDOW_BYTES = 4 * 1024 * 1024  # a piece smaller than this is not worth a connection
STREAM_CHUNK = 256 * 1024
MAX_STREAMS = 16
CHUNKS_PER_STREAM = 4  # pieces per connection: what makes adding one mid-flight useful
START_STREAMS = 2  # one connection rarely fills a link; four can be punished for trying

ADAPT_INTERVAL_S = 1.0  # how often the supervisor re-decides
ADAPT_SETTLE_S = 5.0  # that long after an addition, judge whether it paid
ADAPT_GAIN = 0.05  # a 5% rate gain is "it paid"
RATE_WINDOW_S = 5.0
POLL_S = 0.1  # how often the supervisor notices the pool is done

Progress = Callable[[int, int | None, int], None]
Resumed = Callable[[int], None]  # how many bytes a run picked up from an earlier one


class RangeRefusedError(NetError):
    """The server answered 416: the range was past the end, so the probe is stale."""


@dataclass(frozen=True)
class Stats:
    """What the pool did, for the interface and for tests to look at."""

    peak_streams: int
    additions: int
    retirements: int
    stalls: int


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


def pieces(size: int, streams: int, min_bytes: int = MIN_WINDOW_BYTES) -> list[tuple[int, int]]:
    """The queue a pool of `streams` connections pulls from.

    Several pieces per connection: with exactly one each, adding a connection would have
    nothing to fetch, and retiring one would leave a whole piece unwritten.
    """
    return windows(size, max(1, int(streams)) * CHUNKS_PER_STREAM, min_bytes)


def planned_streams(found: Probe, streams: int) -> int:
    """How many connections this download can use at most — what the interface says."""
    if not found.ranges or not found.size:
        return 1
    return min(max(1, int(streams)), len(pieces(found.size, streams)))


class _Lease:
    """A stretch of the file one worker may still take.

    `end` is mutable so a worker being retired stops at the frontier instead of
    finishing its piece; the rest goes back to the queue. `None` means "to the end of
    the body", which is what an unknown-length single stream has.
    """

    __slots__ = ("end", "start")

    def __init__(self, start: int, end: int | None) -> None:
        self.start = start
        self.end = end


class _Worker(threading.Thread):
    """Takes pieces off the queue until it is retired or the queue runs dry."""

    def __init__(self, pool: _Pool, index: int) -> None:
        super().__init__(name=f"flower-stream-{index}", daemon=True)
        self.pool = pool
        self.lease: _Lease | None = None
        self.piece: tuple[int, int] | None = None
        self.stalls = 0
        self._retire = threading.Event()

    @property
    def retiring(self) -> bool:
        return self._retire.is_set()

    def retire(self) -> None:
        """Stop after the current piece: shrink it to what is already on disk."""
        self._retire.set()
        lease = self.lease
        if lease is not None and lease.end is not None:
            lease.end = min(lease.end, self.pool.land.spans.end_of_run(lease.start))

    def run(self) -> None:
        task, pool = self.pool.task, self.pool
        while not self.retiring and not task.stop.is_set():
            piece = pool.take()
            if piece is None:
                return
            self.piece = piece
            lease = self.lease = _Lease(*piece)
            try:
                task.fetch_piece(pool.land, lease)
            except RangeRefusedError as exc:
                pool.fail(exc)
                return
            except (OSError, http.client.HTTPException) as exc:
                pool.fail(exc)
                return
            # Hand back against the piece as it was taken, not against the lease: a worker
            # retired before it wrote a byte has a lease ending where it starts, and that
            # piece still has to be fetched by somebody.
            pool.give_back(pool.land.spans.end_of_run(piece[0]), piece[1])


class _Pool:
    """The workers fetching a file, and the queue they pull from."""

    def __init__(self, task: Task, land: Landing, queue: list[tuple[int, int]]) -> None:
        self.task = task
        self.land = land
        self.queue = queue
        self.workers: list[_Worker] = []
        self.lock = threading.Lock()
        self.failure: BaseException | None = None
        self.spawned = 0

    def spawn(self) -> None:
        with self.lock:
            self.spawned += 1
            worker = _Worker(self, self.spawned)
            self.workers.append(worker)
        worker.start()

    def take(self) -> tuple[int, int] | None:
        with self.lock:
            return self.queue.pop(0) if self.queue else None

    def give_back(self, start: int, end: int | None) -> None:
        if end is None or end > start:
            with self.lock:
                self.queue.insert(0, (start, end))

    def alive(self) -> list[_Worker]:
        return [worker for worker in self.workers if worker.is_alive()]

    def retire_one(self) -> None:
        alive = self.alive()
        if len(alive) > 1:
            alive[-1].retire()

    def retire_all(self) -> None:
        for worker in self.alive():
            worker.retire()

    def fail(self, exc: BaseException) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = exc
        self.task.stop.set()

    def outstanding(self) -> bool:
        return bool(self.queue) or bool(self.alive())


class Task:
    """One download that can be paused and resumed: the part, the spans, the switch.

    The task outlives its worker, because pausing ends the threads and resuming starts
    new ones over the same part file and the same record of what is on disk. That record
    is the reason a resumed download does not start over.
    """

    def __init__(
        self,
        client: Client,
        found: Probe,
        dest_dir: Path | str,
        streams: int = 4,
        tag: str | None = None,
        min_window_bytes: int = MIN_WINDOW_BYTES,
        adapt_interval_s: float = ADAPT_INTERVAL_S,
        adapt_settle_s: float = ADAPT_SETTLE_S,
        start_streams: int = START_STREAMS,
        source: str = "",
        sha256: str = "",
        resume: bool = True,
    ) -> None:
        self.client = client
        self.found = found
        self.size = found.size
        self.streams = max(1, min(int(streams), MAX_STREAMS))
        self.min_window_bytes = min_window_bytes
        self.adapt_interval_s = adapt_interval_s
        self.adapt_settle_s = adapt_settle_s
        self.start_streams = max(1, start_streams)
        self.dest = Path(dest_dir) / found.filename
        self.tag = tag or secrets.token_hex(4)
        self.source = source or found.url  # what the record is matched on across runs
        self.sha256 = sha256
        self.resume = resume
        self.resumed = 0
        self.on_resume: Resumed | None = None  # told how many bytes were picked up, once
        self.stop = threading.Event()
        self.keep_part = False
        self.live_streams = 1
        self.additions = 0
        self.retirements = 0
        self.stalls = 0
        self._peak = 1
        self._started = False
        self._backed_off = False
        self._growth_cap = self.streams
        self._land: Landing | None = None
        self._progress: Progress = lambda _done, _total, _streams: None
        self._samples: deque[tuple[float, int]] = deque()
        self._rate_at_add = 0.0

    @property
    def stats(self) -> Stats:
        return Stats(self._peak, self.additions, self.retirements, self.stalls)

    def watch(self, callback: Progress) -> None:
        self._progress = callback

    def pause(self) -> None:
        """Stop with the part intact: the next run continues from the frontier."""
        self.keep_part = True
        if self._land is not None:
            self._land.persist(force=True)  # the note has to be on disk before the stop lands
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

        The stop switch is cleared for a resume but not for the first attempt, so a pause
        or cancel pressed while the probe was still running is still honoured instead of
        being wiped out by the run that follows it.
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
            land = self._land = Landing(
                self.dest,
                self.size,
                self.tag,
                source=self.source,
                sha256=self.sha256,
                resumable=self.resume,
            )
            if land.adopt():
                self.resumed = land.spans.bytes
                if self.on_resume is not None:
                    self.on_resume(self.resumed)
            else:
                land.start(fresh=True)
        else:
            land.start(fresh=False)
        try:
            self._fetch(land)
        except BaseException:
            if self.keep_part or self._paid_for(land):
                # A failure that already cost bytes keeps them, with the note that says what
                # they are: the next run of this link continues instead of starting over.
                land.persist(force=True)
            else:
                land.discard()
            raise
        if self.stop.is_set():
            if not self.keep_part:
                land.discard()
            return None
        return land.commit()

    @staticmethod
    def _paid_for(land: Landing) -> bool:
        """True when the part holds bytes worth keeping; a failure that lost nothing leaves nothing."""
        return land.spans.bytes > 0

    def _reprobe(self) -> None:
        """The file moved under us: ask again, and start the part from scratch."""
        found = probe(self.client, self.found.url)
        self.found = found
        self.size = found.size
        self.dest = self.dest.with_name(found.filename)
        assert self._land is not None
        self._land.discard()
        self._land = None

    # ── fetching ──

    def _fetch(self, land: Landing) -> None:
        self.live_streams = 1
        size = self.size
        if not self.found.ranges or not size:
            self._stream(land)
            return
        queue = pieces(size, self.streams, self.min_window_bytes)
        first_start, first_end = queue[0]
        at = land.spans.end_of_run(first_start)
        if at < first_end:  # a resumed first piece has nothing to ask about when it is done
            with self._open_range(at, first_end - 1) as response:
                if not _is_ranged(response, at):
                    # No ranges on this server: the answer is the whole file from byte
                    # zero, so it plays the part of the single stream.
                    self._pump(response, land, _Lease(0, size))
                    return
        self._run_pool(land, queue)

    def _run_pool(self, land: Landing, queue: list[tuple[int, int]]) -> None:
        """Feed the pieces to a pool of connections, and decide how big that pool is."""
        pool = _Pool(self, land, queue)
        for _ in range(min(self.start_streams, len(queue))):
            pool.spawn()
        self._note_pool(pool)
        stalls_seen = 0
        since_add = 0.0
        while not self.stop.is_set() and pool.failure is None and pool.outstanding():
            time.sleep(POLL_S)
            if self.stop.is_set() or pool.failure is not None:
                break
            if pool.queue and not pool.alive():
                pool.spawn()  # a piece came back after every worker had quit
                self._note_pool(pool)
            since_add += POLL_S
            if since_add < self.adapt_interval_s:
                continue
            since_add = 0.0
            rate = self._sample(land)
            self._note_pool(pool)
            stalled = self.stalls > stalls_seen
            stalls_seen = self.stalls
            if stalled and len(pool.alive()) > 1:
                before = len(pool.alive())
                pool.retire_one()
                self.retirements += 1
                self._backed_off = True  # a link that stalls does not get tried again
                if len(pool.alive()) < before:
                    continue
            elif not self._backed_off and len(pool.alive()) < self._growth_cap:
                if self.additions and rate <= self._rate_at_add * (1 + ADAPT_GAIN):
                    self._growth_cap = len(pool.alive())  # the last addition bought nothing
                else:
                    pool.spawn()
                    self.additions += 1
                    self._rate_at_add = rate
                    since_add = 0.0
                    self._note_pool(pool)
        pool.retire_all()
        for worker in pool.workers:
            worker.join(10.0)
        if pool.failure is not None:
            raise pool.failure

    def _note_pool(self, pool: _Pool) -> None:
        """Publish the live connection count, and let the interface see it move."""
        self.live_streams = len(pool.alive())
        self._peak = max(self._peak, self.live_streams)
        self._progress(self._land.spans.bytes if self._land else 0, self.size, self.live_streams)

    def _sample(self, land: Landing) -> float:
        """The aggregate rate over the last few seconds."""
        now = time.monotonic()
        self._samples.append((now, land.spans.bytes))
        while len(self._samples) > 2 and now - self._samples[0][0] > RATE_WINDOW_S:
            self._samples.popleft()
        if len(self._samples) < 2:
            return 0.0
        (first_t, first_bytes), (last_t, last_bytes) = self._samples[0], self._samples[-1]
        span = last_t - first_t
        return (last_bytes - first_bytes) / span if span > 0.05 else 0.0

    def _stream(self, land: Landing) -> None:
        """One GET from byte zero: the shape every server understands."""
        with self.client.open(self.found.url) as response:
            announced = as_int(response.headers.get("Content-Length"))
            if land.expect is None:
                land.expect = announced
            self._pump(response, land, _Lease(0, announced))

    def fetch_piece(self, land: Landing, lease: _Lease) -> None:
        """Fetch one piece, retried from the frontier the disk actually reached.

        A body that ends before the range did is a stall: it costs a retry from the
        frontier — never from the start — and it is counted, because a link that stalls
        more as connections are added is the evidence that the count should come down.
        """
        attempt = 0
        failure: BaseException | None = None
        while not self.stop.is_set():
            at = land.spans.end_of_run(lease.start)
            if lease.end is not None and at >= lease.end:
                return
            if attempt >= WINDOW_ATTEMPTS:
                raise failure or NetError(f"stalled at {at} of {lease.end}")
            if attempt:
                time.sleep(WINDOW_RETRY_WAIT_S * attempt)
            attempt += 1
            failure = None
            try:
                self._fetch_window(land, at, lease)
            except RangeRefusedError:
                raise  # a stale probe is not something a retry can fix
            except (OSError, http.client.HTTPException) as exc:
                failure = exc
            if not self.stop.is_set() and _short(land, at, lease):
                self.stalls += 1

    def _fetch_window(self, land: Landing, start: int, lease: _Lease) -> None:
        """One ranged GET into the landing, writing from `start` up to the lease's end."""
        assert lease.end is not None
        response = self._open_range(start, lease.end - 1)
        with response:
            if not _is_ranged(response, start):
                # Mid-file a whole-file answer is useless: it starts at byte zero and
                # this piece is not there.
                raise NetError(f"the server ignored the range from {start}")
            self._pump(response, land, _Lease(start, lease.end))

    def _open_range(self, first: int, last: int):
        """Open one ranged GET. The range is always built by an f-string: a `.replace` on
        a placeholder sends the literal text, which servers answer with an empty response
        and a silent 416, leaving the workers to die one by one."""
        try:
            return self.client.open(self.found.url, {"Range": f"bytes={first}-{last}"})
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                raise RangeRefusedError(f"the server refused bytes {first}-{last}") from exc
            raise

    def _pump(self, response, land: Landing, lease: _Lease) -> None:
        """Copy the response body into the landing from `lease.start`.

        The lease's end is read every round, so a worker being retired stops within one
        chunk rather than finishing a piece nobody needs. Returning early means the
        server ended the body early or the task was stopped — for a piece that is the
        signal to resume, and the resume point is read off the landing (what is really
        on disk) rather than off this call.
        """
        at = lease.start
        with land.writer(lease.start) as handle:
            while lease.end is None or at < lease.end:
                if self.stop.is_set():
                    break
                if lease.end is None:
                    want = STREAM_CHUNK
                else:
                    want = min(STREAM_CHUNK, lease.end - at)
                chunk = response.read(want)
                if not chunk:
                    break
                began = at
                handle.write(chunk)
                handle.flush()  # reported means "out of this handle": the record relies on it
                at += len(chunk)
                land.written(began, at)
                self._progress(land.spans.bytes, self.size, self.live_streams)
        land.persist()  # note what on disk, so a process that dies here does not cost the piece


def _short(land: Landing, start: int, lease: _Lease) -> bool:
    """True when the piece did not reach its end: the body stopped early."""
    if lease.end is None:
        return False
    return land.spans.end_of_run(start) < lease.end


def _is_ranged(response, first: int) -> bool:
    """True when the answer is a 206 starting exactly where we asked.

    A 206 whose `Content-Range` starts elsewhere counts as no: bytes would land at the
    wrong offset, which is worse than re-fetching the piece.
    """
    if getattr(response, "status", 200) != 206:
        return False
    return (response.headers.get("Content-Range") or "").startswith(f"bytes {first}-")
