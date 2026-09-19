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
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

PART_SUFFIX = ".part"


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


def part_path(dest: Path | str, tag: str = "") -> Path:
    """The part file a download writes before it earns the real name."""
    target = Path(dest)
    if not tag:
        return target.with_name(target.name + PART_SUFFIX)
    return target.with_name(f"{target.name}.{tag}{PART_SUFFIX}")


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
    """

    def __init__(self, dest: Path | str, expect: int | None = None, tag: str = "") -> None:
        self.dest = Path(dest)
        self.expect = expect
        self.part = part_path(self.dest, tag)
        self.spans = Spans()
        self._done = False

    def start(self, fresh: bool) -> None:
        """Make the part ready: `fresh` throws away what an earlier attempt left."""
        self.part.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            with contextlib.suppress(OSError):
                self.part.unlink()
        self.part.touch()

    @contextlib.contextmanager
    def writer(self, offset: int) -> Iterator[BinaryIO]:
        """A handle positioned at `offset`: one of these per window."""
        with self.part.open("r+b") as handle:
            handle.seek(offset)
            yield handle

    def written(self, start: int, end: int) -> None:
        """Bytes `[start, end)` are on disk now (a retry reports the same range again)."""
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
        return target

    def discard(self) -> None:
        """Remove the part: what a cancelled or failed download leaves behind (nothing)."""
        with contextlib.suppress(OSError):
            self.part.unlink()
