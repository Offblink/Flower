"""The one place a download runs off the GUI thread.

A plain thread behind Qt signals: the engine knows nothing about Qt, and the page
never reaches into a task's state. Speed and remaining time are not measured
here — they come from the same progress stream the bar uses, so the two can never
disagree, and the throttling is here rather than in the engine because it is an
interface concern (a 1 GB download reports a million fragments otherwise).
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal

from ..engine import Task, planned_streams
from ..net import Client
from ..probe import probe

PROGRESS_INTERVAL_S = 0.2
PROGRESS_STEP_BYTES = 1024 * 1024


class DownloadWorker(QObject):
    """Runs one probe-and-fetch — or the rest of a paused one — and reports back."""

    probed = Signal(str, int, bool)  # file name, connections it will use, ranges supported
    progressed = Signal(int, int)  # bytes on disk, total (0 when the server never said)
    finished = Signal(str)  # where the file landed
    stopped = Signal(bool)  # True: paused, part kept. False: cancelled, part gone
    failed = Signal(str)

    def __init__(
        self,
        client: Client,
        url: str,
        dest_dir: str,
        streams: int,
        task: Task | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._url = url
        self._dest_dir = dest_dir
        self._streams = streams
        self._task = task
        self._wanted: str | None = None  # asked for before the task existed (probing)
        self._thread: threading.Thread | None = None
        self._last_emit = 0.0
        self._last_done = 0

    @property
    def task(self) -> Task | None:
        return self._task

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pause(self) -> None:
        """Pause, whether or not the probe has finished building the task yet."""
        self._wanted = "pause"
        if self._task is not None:
            self._task.pause()

    def cancel(self) -> None:
        self._wanted = "cancel"
        if self._task is not None:
            self._task.cancel()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="flower-download", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            if self._task is None:
                found = probe(self._client, self._url)
                self.probed.emit(
                    found.filename, planned_streams(found, self._streams), found.ranges
                )
                task = Task(self._client, found, self._dest_dir, self._streams)
                self._task = task
                if self._wanted == "pause":
                    task.pause()
                elif self._wanted == "cancel":
                    task.cancel()
            self._task.watch(self._report)
            landed = self._task.run()
        except BaseException as exc:  # a download that dies is shown, not swallowed
            self.failed.emit(str(exc) or type(exc).__name__)
            return
        if landed is None:
            self.stopped.emit(self._task.keep_part)
        else:
            self.finished.emit(str(landed))

    def _report(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        quiet = now - self._last_emit < PROGRESS_INTERVAL_S
        if quiet and done - self._last_done < PROGRESS_STEP_BYTES:
            return
        self._last_emit = now
        self._last_done = done
        self.progressed.emit(done, total or 0)
