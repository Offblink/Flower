"""Where the bytes land, and the one moment the real name is allowed to appear.

Ported from Fungi's landing layer (spec §49, §50) with one difference: the part
file's tag. Fungi tags it with the hub's staged transfer id; Flower mints a
random short token per task, so two downloads of the same name never write into
each other's part.

The rules being kept: every window writes its own stretch of one part file in
place (no second copy to concatenate), the union of the ranges is the proof of
what is really on disk, and the rename happens only when the announced length
*and* the coverage are both right. Anything else — a window that never
finished, a dropped connection, a cancelled task — leaves no half file under the
real name, because the only thing that notices a truncated download is the
user, hours later.

The tag is minted per task, which used to mean a part could not outlive its
process: nothing could name it again. `PartRecord` is that name, written next to
the part and keyed by the destination, so a run killed mid-download (a crash, a
killed process — not a cancel, which deletes on purpose) leaves something a later
run can prove and continue from. Two things make it safe to believe: a range is
only recorded once its bytes have left the writer's buffer, and the record is
adopted only when the link, the length and (when both sides know one) the digest
still match. `PartLock` covers the remaining hole: two live runs of the same
destination must not both think the part is theirs.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

PART_SUFFIX = ".part"
RECORD_SUFFIX = ".part.json"
LOCK_SUFFIX = ".part.lock"
RECORD_VERSION = 1
PERSIST_INTERVAL_S = 2.0  # how often the record is rewritten while bytes keep arriving


if os.name == "nt":
    import msvcrt

    def _claim(handle: BinaryIO, *, take: bool) -> None:
        """Windows: lock or unlock one byte of the file, without waiting for it."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _claim(handle: BinaryIO, *, take: bool) -> None:
        """POSIX: an advisory whole-file lock, without waiting for it."""
        flags = (fcntl.LOCK_EX | fcntl.LOCK_NB) if take else fcntl.LOCK_UN
        fcntl.flock(handle.fileno(), flags)


class TransferTruncatedError(OSError):
    """A download that is not all there: short, or with a hole no window filled."""

    def __init__(self, written: int, expected: int) -> None:
        super().__init__(f"download ended early: {written} of {expected} bytes")
        self.written = written
        self.expected = expected


class Spans:
    """Which bytes of a download are really here, as a union of ranges.

    A running total cannot answer that once the download is split into windows:
    they arrive out of order, a window that dies reports its range again when it
    resumes, and a server that ignores ranges rewrites from zero. Union length
    only ever grows, which is exactly what a progress count and a "is it whole?"
    check need. One thread per window reports here, so the lock is the point.
    """

    def __init__(self) -> None:
        self._parts: list[tuple[int, int]] = []  # sorted, disjoint, [start, end)
        self._lock = threading.Lock()

    def add(self, start: int, end: int) -> None:
        if end <= start:
            return
        lo, hi = int(start), int(end)
        with self._lock:
            kept: list[tuple[int, int]] = []
            for first, last in self._parts:
                if last < lo or first > hi:  # a gap: touching spans merge
                    kept.append((first, last))
                    continue
                lo, hi = min(lo, first), max(hi, last)
            kept.append((lo, hi))
            kept.sort()
            self._parts = kept

    @property
    def bytes(self) -> int:
        with self._lock:
            return sum(last - first for first, last in self._parts)

    def covers(self, start: int, end: int) -> bool:
        """True when every byte of `[start, end)` has been reported."""
        at = int(start)
        with self._lock:
            for first, last in self._parts:  # sorted
                if last <= at:
                    continue
                if first > at:
                    return False
                at = last
                if at >= end:
                    return True
        return at >= end

    def end_of_run(self, start: int) -> int:
        """How far the covered stretch beginning at `start` reaches (>= start).

        This is where a download resumes from: not where the request started, but
        where the bytes actually are.
        """
        at = int(start)
        with self._lock:
            for first, last in self._parts:  # sorted
                if last <= at:
                    continue
                if first > at:
                    break
                at = last
        return at

    def ranges(self) -> list[tuple[int, int]]:
        """A snapshot of the union, for writing down somewhere the next run can read it."""
        with self._lock:
            return list(self._parts)


def part_path(dest: Path | str, tag: str = "") -> Path:
    """The part file a download writes before it earns the real name."""
    target = Path(dest)
    if not tag:
        return target.with_name(target.name + PART_SUFFIX)
    return target.with_name(f"{target.name}.{tag}{PART_SUFFIX}")


def record_path(dest: Path | str) -> Path:
    """Where the note about a part lives. Named after the destination, not the tag.

    It has to be findable by a run that never saw the tag — that is the whole point of
    it, since the tag is minted per task and dies with the process that made it.
    """
    target = Path(dest)
    return target.with_name(target.name + RECORD_SUFFIX)


def lock_path(dest: Path | str) -> Path:
    """The claim on a destination, one per destination rather than one per part."""
    target = Path(dest)
    return target.with_name(target.name + LOCK_SUFFIX)


@dataclass
class PartRecord:
    """What an earlier run finished writing, in the shape the next run can act on.

    Only ranges whose writer has been closed are in here — `Landing.persist` is called
    after the piece's handle is gone — so a record that survived a killed process
    describes bytes that are really in the file rather than bytes still in Python's
    buffer. `source` is the link the user asked for, not the URL the probe ended up at:
    signed redirect targets change on every run, so comparing those would refuse to
    resume on exactly the hosts where it matters.
    """

    source: str
    size: int | None
    sha256: str
    part: str
    spans: list[list[int]]

    def matches(self, source: str, size: int | None, sha256: str) -> bool:
        """Same link, same length, and — when both sides know one — the same digest."""
        if not self.part or self.source != source:
            return False
        if size is not None and self.size is not None and self.size != size:
            return False
        return not (sha256 and self.sha256 and self.sha256 != sha256)

    @classmethod
    def read(cls, dest: Path | str) -> PartRecord | None:
        """The record for this destination, or None when there is nothing trustworthy."""
        try:
            raw = json.loads(record_path(dest).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != RECORD_VERSION:
            return None
        try:
            spans = [[int(a), int(b)] for a, b in raw.get("spans") or []]
        except (TypeError, ValueError):
            return None
        spans = [span for span in spans if span[1] > span[0] >= 0]
        if not spans:
            return None
        size = raw.get("size")
        return cls(
            str(raw.get("source") or ""),
            int(size) if size is not None else None,
            str(raw.get("sha256") or ""),
            str(raw.get("part") or ""),
            spans,
        )

    def write(self, dest: Path | str) -> None:
        path = record_path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": RECORD_VERSION,
                    "source": self.source,
                    "size": self.size,
                    "sha256": self.sha256,
                    "part": self.part,
                    "spans": self.spans,
                    "saved": round(time.time(), 3),
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def clear(dest: Path | str) -> None:
        with contextlib.suppress(OSError):
            record_path(dest).unlink()


class PartLock:
    """An exclusive claim on one destination, held by the process that is writing it.

    The kernel releases it when the process dies, which is the property that makes it
    usable here: a part left by a killed run cannot leave a lock behind that blocks its
    own resume, so no stale-lock cleanup (and no pid guessing) is needed. Refusing to
    take it means somebody else is writing this destination right now, and then nothing
    on disk may be adopted.
    """

    def __init__(self, dest: Path | str) -> None:
        self.path = lock_path(dest)
        self._handle: BinaryIO | None = None

    @property
    def held(self) -> bool:
        """True while this process is the one writing the destination."""
        return self._handle is not None

    def take(self) -> bool:
        """True when this process now owns the destination."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
        except OSError:
            return False
        try:
            _claim(handle, take=True)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self, remove: bool = False) -> None:
        if self._handle is None:
            return
        with contextlib.suppress(OSError, ValueError):
            _claim(self._handle, take=False)
        with contextlib.suppress(OSError):
            self._handle.close()
        self._handle = None
        if remove:
            with contextlib.suppress(OSError):
                self.path.unlink()


def free_name(dest: Path) -> Path:
    """`dest` itself, or `name (2).ext` when that name is already taken.

    A downloader that silently overwrites an existing file is a downloader that
    loses somebody's data once, so an occupied name steps aside instead. The part
    file is unaffected: it is named after the intended destination, not after the
    name the commit eventually picks.
    """
    if not dest.exists():
        return dest
    index = 2
    while True:  # the first name that is not taken wins; the loop always returns
        candidate = dest.with_name(f"{dest.stem} ({index}){dest.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


class Landing:
    """One download in flight: a part file that one or more writers fill in place.

    `start(fresh=False)` adopts whatever an earlier attempt left behind, which is
    what makes pause/resume cost the missing stretch instead of the whole file —
    the spans object handed back in is the record of what is already on disk.
    `adopt()` is the same thing for an earlier *process*: it takes the lock, believes
    the record on disk only when it matches this download, and hands back the spans.
    """

    def __init__(
        self,
        dest: Path | str,
        expect: int | None = None,
        tag: str = "",
        source: str = "",
        sha256: str = "",
        resumable: bool = True,
    ) -> None:
        self.plan = Path(dest)  # what the record and the lock are keyed on, renames aside
        self.dest = Path(dest)
        self.expect = expect
        self.tag = tag
        self.part = part_path(self.plan, tag)
        self.spans = Spans()
        self.source = source
        self.sha256 = sha256
        self.resumable = resumable
        self.lock = PartLock(self.plan)
        self._saved = 0.0
        self._done = False

    def adopt(self) -> bool:
        """Continue the part an earlier run left, when it is provably the same download.

        False means there is nothing to continue: no note, another live run owns this
        destination, or `resumable` is off (`--fresh`), or the link / length / digest does
        not match. A note that cannot be used is cleared here together with the part it
        names — no run will look for that tag again, so keeping it is exactly the litter
        that "Flower does not resume across runs" was avoiding. When another live run
        holds the claim, nothing on disk is touched at all.
        """
        if not self.lock.take():
            return False
        record = PartRecord.read(self.plan)
        if record is None:
            return False
        part = self.plan.with_name(record.part)
        size = part.stat().st_size if part.exists() else 0
        spans = [(first, min(last, size)) for first, last in record.spans if first < size]
        usable = (
            self.resumable
            and record.matches(self.source, self.expect, self.sha256)
            and any(last > first for first, last in spans)
        )
        if not usable:
            self._drop_stale(part)
            return False
        self.part = part  # keep writing into the file the bytes are already in
        for first, last in spans:
            self.spans.add(first, last)
        return True

    def _drop_stale(self, part: Path) -> None:
        """A record nothing can use, and the part it pointed at: both go."""
        with contextlib.suppress(OSError):
            part.unlink()
        PartRecord.clear(self.plan)

    def start(self, fresh: bool) -> None:
        """Make the part ready: `fresh` throws away what an earlier attempt left."""
        self.part.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            with contextlib.suppress(OSError):
                self.part.unlink()
        self.part.touch()

    def persist(self, force: bool = False) -> None:
        """Write down what is provably on disk, so a later run can continue from it.

        Called after a piece's handle has been closed — never mid-piece — because a
        record may not claim bytes that only exist in Python's buffer. Rewritten at most
        every `PERSIST_INTERVAL_S`; a later run re-fetches whatever the note missed.
        """
        if not self.resumable or not self.spans.bytes:
            return
        now = time.monotonic()
        if not force and now - self._saved < PERSIST_INTERVAL_S:
            return
        self._saved = now
        PartRecord(
            source=self.source,
            size=self.expect,
            sha256=self.sha256,
            part=self.part.name,
            spans=[list(span) for span in self.spans.ranges()],
        ).write(self.plan)

    @contextlib.contextmanager
    def writer(self, offset: int) -> Iterator[BinaryIO]:
        """A handle positioned at `offset`: one of these per window."""
        with self.part.open("r+b") as handle:
            handle.seek(offset)
            yield handle

    def written(self, start: int, end: int) -> None:
        """Bytes `[start, end)` are on disk now (a retry reports the same range again).

        On disk means "out of the handle that wrote them" — the engine flushes before
        reporting, which is what lets `persist` trust this list.
        """
        self.spans.add(start, end)

    def commit(self) -> Path:
        """Put the part in place — the only way the real name appears. Returns it."""
        size = self.part.stat().st_size
        if self.expect is not None:
            if size != self.expect:
                raise TransferTruncatedError(size, self.expect)
            # Length alone is not proof: a window that never ran leaves a hole, and
            # a later window writing past it makes the file the right size anyway.
            if not self.spans.covers(0, self.expect):
                raise TransferTruncatedError(self.spans.bytes, self.expect)
        target = free_name(self.dest)
        self.part.replace(target)
        self.dest = target
        self._done = True
        self.forget()
        return target

    def discard(self) -> None:
        """Remove the part: what a cancelled or failed download leaves behind (nothing)."""
        with contextlib.suppress(OSError):
            self.part.unlink()
        self.forget()

    def forget(self) -> None:
        """Drop the note and the claim — and the part the note named, when it is not ours.

        Only while this process holds the claim: a run that never owned the destination
        (another live run does) must not touch what is on disk.
        """
        if self.lock.held:
            record = PartRecord.read(self.plan)
            if record is not None and record.part != self.part.name:
                with contextlib.suppress(OSError):
                    self.plan.with_name(record.part).unlink()
            PartRecord.clear(self.plan)
        self.lock.release(remove=True)
