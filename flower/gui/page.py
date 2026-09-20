"""Flower's whole interface: one page, four rows, one button that changes its name.

Minimal on purpose — no animation, no decoration, no second page. The button is
the state machine: 开始下载 → 暂停 → 继续, with 取消 beside it. Pause keeps the
part so the next run costs only the missing stretch; cancel deletes it, because
Flower does not resume across runs and a leftover part is then just litter.
"""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
    TitleLabel,
)

from ..config import MAX_STREAMS, MIN_STREAMS, Settings
from ..engine import Task, planned_streams
from ..gui.worker import DownloadWorker
from ..net import Client

SPEED_WINDOW_S = 5.0
BAR_STEPS = 1000
_UNITS = ("B", "KB", "MB", "GB", "TB")

IDLE, RUNNING, PAUSED, ENDING = "idle", "running", "paused", "ending"


def _row(parent: QWidget, label: str, *items: QWidget, fill: bool = False) -> QHBoxLayout:
    """One form row: a fixed-width name, then its controls.

    `fill` is for a row whose field should take the width (the URL, the folder);
    without it the controls stay packed at their own size and the slack goes to
    the end, which is what keeps the proxy row's fields and its switch together
    instead of drifting apart.
    """
    row = QHBoxLayout()
    row.setSpacing(10)
    name = StrongBodyLabel(label, parent)
    name.setFixedWidth(56)
    row.addWidget(name, 0, Qt.AlignVCenter)
    for item in items:
        row.addWidget(item, 0, Qt.AlignVCenter)
    if not fill:
        row.addStretch(1)
    return row


class MainPage(QWidget):
    """链接、保存到、代理、流数 —— 一个会改名的按钮管住全部状态。"""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mainPage")
        self._settings = settings
        self._worker: DownloadWorker | None = None
        self._task: Task | None = None
        self._state = IDLE
        self._samples: deque[tuple[float, int]] = deque()
        self._total = 0
        self._streams_live = 0
        self._streams_planned = 0
        self._degraded = False
        self._toast: InfoBar | None = None
        self._build()
        self._sync()

    # ── the page ──

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 26)
        root.setSpacing(12)
        root.addWidget(TitleLabel("Flower", self))
        root.addWidget(
            CaptionLabel("喂它一个链接，它把这一条流拆成几条，同时去取同一份文件。", self)
        )
        root.addSpacing(6)

        card = CardWidget(self)
        form = QVBoxLayout(card)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(14)

        self.url_edit = LineEdit(card)
        self.url_edit.setPlaceholderText("https://…")
        form.addLayout(_row(card, "链接", self.url_edit, fill=True))

        self.dir_edit = LineEdit(card)
        self.dir_edit.setText(str(self._settings.resolved_save_dir()))
        browse = PushButton("浏览", card, FluentIcon.FOLDER)
        browse.clicked.connect(self._pick_dir)
        open_dir = PushButton("打开", card)
        open_dir.setToolTip("在文件资源管理器里打开这个目录")
        open_dir.clicked.connect(self._open_dir)
        form.addLayout(_row(card, "保存到", self.dir_edit, browse, open_dir, fill=True))

        self.host_edit = LineEdit(card)
        self.host_edit.setText(self._settings.proxy_host)
        self.host_edit.setFixedWidth(150)
        self.port_edit = LineEdit(card)
        self.port_edit.setText(self._settings.proxy_port)
        self.port_edit.setFixedWidth(90)
        self.proxy_switch = SwitchButton(card)
        self.proxy_switch.setOnText("开")
        self.proxy_switch.setOffText("关")
        self.proxy_switch.setChecked(self._settings.use_proxy)
        form.addLayout(
            _row(
                card,
                "代理",
                self.host_edit,
                CaptionLabel(":", card),
                self.port_edit,
                self.proxy_switch,
            )
        )

        self.stream_box = ComboBox(card)
        self.stream_box.addItems([str(count) for count in range(MIN_STREAMS, MAX_STREAMS + 1)])
        self.stream_box.setCurrentIndex(self._settings.streams - MIN_STREAMS)
        self.stream_box.setFixedWidth(80)
        form.addLayout(
            _row(
                card,
                "流数",
                self.stream_box,
                CaptionLabel(f"{MIN_STREAMS}–{MAX_STREAMS} 条连接一起取同一份文件", card),
            )
        )
        root.addWidget(card)
        root.addSpacing(4)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.action_button = PrimaryPushButton("开始下载", self, FluentIcon.DOWNLOAD)
        self.action_button.clicked.connect(self._on_action)
        self.cancel_button = PushButton("取消", self)
        self.cancel_button.clicked.connect(self._on_cancel)
        actions.addWidget(self.action_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        root.addLayout(actions)

        self.progress = ProgressBar(self)
        self.progress.setRange(0, BAR_STEPS)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.status = BodyLabel("等待链接", self)
        self.status.setWordWrap(True)
        self.detail = CaptionLabel("", self)
        self.detail.setWordWrap(True)
        root.addWidget(self.status)
        root.addWidget(self.detail)
        root.addStretch(1)

    # ── what the buttons do ──

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择保存位置", str(self._current_dir()))
        if chosen:
            self.dir_edit.setText(chosen)

    def _open_dir(self) -> None:
        """Show the chosen folder in Explorer, whatever state the download is in.

        The folder is only opened, never created: a path the user is still typing
        should not put a directory tree on disk just because they looked at it.
        """
        target = self._current_dir()
        if not target.is_dir():
            self._warn("这个目录还不存在", str(target))
            return
        try:
            os.startfile(target)
        except OSError as error:
            self._warn("打开目录失败", str(error))

    def _on_action(self) -> None:
        if self._state == ENDING:
            return
        if self._state == RUNNING:
            self._stop_running(pause=True)
            return
        if self._task is not None:  # a paused task: the rest of the same download
            self._begin(resuming=True)
            return
        if not self.url_edit.text().strip().lower().startswith(("http://", "https://")):
            self._warn("链接看起来不对", "请填以 http:// 或 https:// 开头的地址")
            return
        self._remember()
        self._begin(resuming=False)

    def _on_cancel(self) -> None:
        if self._state == ENDING:
            return
        if self._state == RUNNING:
            self._stop_running(pause=False)
            return
        if self._task is not None:  # paused: cancel is where the part goes away
            self._task.discard()
            self._reset()
            self.status.setText("已取消")
            self.detail.setText("未完成的片段已经删掉了")

    def _stop_running(self, *, pause: bool) -> None:
        """Ask the worker to stop. It may still be probing, and it decides what that means."""
        worker = self._worker
        assert worker is not None
        self._state = ENDING
        self.status.setText("正在暂停…" if pause else "正在取消…")
        self._sync()
        if pause:
            worker.pause()
        else:
            worker.cancel()

    def _begin(self, resuming: bool) -> None:
        task = self._task
        if resuming and task is not None:
            worker = DownloadWorker(
                task.client, task.found.url, str(task.dest.parent), task.streams, task=task
            )
        else:
            url = self.url_edit.text().strip()
            worker = DownloadWorker(
                Client(self._settings.proxy_url()),
                url,
                str(self._current_dir()),
                self._stream_count(),
            )
        self._worker = worker
        self._reset_metrics()
        if resuming and task is not None:
            # The probe is not run again on a resume, so the stream count it found has
            # to be restored here rather than waited for.
            self._streams_planned = planned_streams(task.found, task.streams)
            self._streams_live = self._streams_planned
            self._degraded = not task.found.ranges
        self._state = RUNNING
        self.status.setText("正在继续…" if resuming else "正在探测链接…")
        self.detail.setText("")
        self._sync()
        worker.probed.connect(self._on_probed)
        worker.progressed.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.stopped.connect(self._on_stopped)
        worker.failed.connect(self._on_failed)
        worker.start()

    def shutdown(self) -> None:
        """The window is going away: stop the download and leave no part behind."""
        worker = self._worker
        if worker is not None:
            worker.cancel()  # a part filed under no task would never be resumed anyway
            worker.join(1.0)
        if self._task is not None:
            self._task.discard()

    # ── what the download says ──

    def _on_probed(self, filename: str, streams: int, ranges: bool) -> None:
        self._streams_planned = streams
        self._streams_live = streams
        self._degraded = not ranges
        self.status.setText(f"正在下载 {filename}")
        self.detail.setText(self._detail_line())

    def _on_progress(self, done: int, total: int, streams: int) -> None:
        if total:
            self._total = total
        if streams:
            self._streams_live = streams
        now = time.monotonic()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW_S:
            self._samples.popleft()
        if self.progress.maximum() == 0:
            self.progress.setRange(0, BAR_STEPS)
        rate = f" · {_rate(self._speed())}" if self._speed() > 0 else ""
        if self._total:
            self.progress.setValue(min(BAR_STEPS, int(done * BAR_STEPS / self._total)))
            self.status.setText(f"{_human(done)} / {_human(self._total)}{rate}")
        else:
            self.status.setText(f"{_human(done)}{rate}")
        self.detail.setText(self._detail_line())

    def _on_finished(self, path: str) -> None:
        self._reset()
        self.progress.setRange(0, BAR_STEPS)
        self.progress.setValue(BAR_STEPS)
        self.status.setText(f"已保存 {Path(path).name}")
        self.detail.setText(path)
        self._toast = InfoBar.success(
            "下载完成",
            Path(path).name,
            position=InfoBarPosition.TOP_RIGHT,
            duration=4000,
            parent=self,
        )

    def _on_stopped(self, kept: bool) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(1.0)
        # A fresh download's task lives inside the worker until here: pausing has to
        # take it over, or 继续 would start a second download from byte zero.
        task = worker.task if worker is not None else self._task
        self._task = task if kept else None
        if kept:
            self._state = PAUSED
            self.status.setText("已暂停")
            self.detail.setText("再次点击开始，会从断点接着下")
        else:
            self._reset()
            self.status.setText("已取消")
            self.detail.setText("未完成的片段已经删掉了")
        self._sync()

    def _on_failed(self, message: str) -> None:
        self._reset()
        self.status.setText("下载失败")
        self.detail.setText(message)
        self._toast = InfoBar.error(
            "下载失败", message, position=InfoBarPosition.TOP_RIGHT, duration=6000, parent=self
        )

    # ── the small print ──

    def _current_dir(self) -> Path:
        chosen = self.dir_edit.text().strip()
        return Path(chosen) if chosen else self._settings.resolved_save_dir()

    def _stream_count(self) -> int:
        return self.stream_box.currentIndex() + MIN_STREAMS

    def _remember(self) -> None:
        """Keep what the user chose, so the next run opens the way they left it."""
        self._settings.save_dir = str(self._current_dir())
        self._settings.use_proxy = self.proxy_switch.isChecked()
        self._settings.proxy_host = self.host_edit.text().strip() or self._settings.proxy_host
        self._settings.proxy_port = self.port_edit.text().strip() or self._settings.proxy_port
        self._settings.streams = self._stream_count()
        self._settings.save()

    def _reset(self) -> None:
        self._worker = None
        self._task = None
        self._state = IDLE
        self._sync()

    def _reset_metrics(self) -> None:
        self._samples.clear()
        self._total = 0
        self._streams_live = 0
        self._streams_planned = 0
        self._degraded = False
        self.progress.setRange(0, 0)  # busy until the size is known
        self.progress.setValue(0)

    def _sync(self) -> None:
        busy = self._state in (RUNNING, ENDING)
        self.action_button.setText(
            "暂停" if self._state == RUNNING else "继续" if self._state == PAUSED else "开始下载"
        )
        self.action_button.setEnabled(self._state != ENDING)
        self.cancel_button.setEnabled(busy or self._state == PAUSED)
        for widget in (self.url_edit, self.dir_edit, self.stream_box):
            widget.setEnabled(not busy and self._state != PAUSED)

    def _speed(self) -> float:
        """Bytes per second over the sliding window, not over the last two ticks."""
        if len(self._samples) < 2:
            return 0.0
        (first_t, first_bytes), (last_t, last_bytes) = self._samples[0], self._samples[-1]
        span = last_t - first_t
        return (last_bytes - first_bytes) / span if span > 0.05 else 0.0

    def _detail_line(self) -> str:
        parts: list[str] = []
        if self._degraded:
            parts.append("该站点不支持分段下载，单流取回")
        elif self._streams_live:
            if self._streams_live == self._streams_planned:
                parts.append(f"{self._streams_live} 条流")
            else:  # the pool added or dropped connections while it ran
                parts.append(f"{self._streams_live}/{self._streams_planned} 条流")
        speed = self._speed()
        if self._total and speed > 0 and self._samples:
            remaining = max(0, self._total - self._samples[-1][1])
            parts.append(f"约剩 {_duration(remaining / speed)}")
        return " · ".join(parts)

    def _warn(self, title: str, content: str) -> None:
        self._toast = InfoBar.warning(
            title, content, position=InfoBarPosition.TOP_RIGHT, duration=4000, parent=self
        )


def _human(count: float) -> str:
    value = float(count)
    for unit in _UNITS[:-1]:
        if value < 1024:
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} {_UNITS[-1]}"


def _rate(speed: float) -> str:
    return f"{_human(speed)}/s"


def _duration(seconds: float) -> str:
    if seconds < 1:
        return "不到 1 秒"
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds // 60:.0f} 分 {seconds % 60:.0f} 秒"
    return f"{seconds // 3600:.0f} 小时 {seconds % 3600 // 60:.0f} 分"
