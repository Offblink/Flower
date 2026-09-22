"""The same download, without a window: one link in, one file out.

Nothing here is a second engine. The probe, the pool, the landing and the
adaptive stream count are the ones the window drives; this module only supplies
what the window supplies — a route (proxy or not), a stream budget, a progress
stream, and a place to say where the file landed.

Three things are deliberately the command line's own:

* The report is line-based and log-safe (no cursor tricks, no `\\r`), because the
  interesting use of a CLI is inside a script whose output goes somewhere.
  `--json` emits the same events as one object per line for a machine to read.
* The exit code is the verdict: 0 landed, 1 failed, 2 hash mismatch, 130 stopped.
  A downloader that always exits 0 teaches the caller to ignore it.
* Ctrl-C stops the download and keeps what is on disk: the part and the note beside it
  stay, so starting the same link again continues from there instead of from zero. A
  second Ctrl-C leaves at once, and the part is still there. 取消 is the window's
  button — that is the one that deletes — and `--fresh` is how the command line says
  the same thing. A download that dies any other way (a crash, a killed process)
  leaves exactly that same pair behind.

Speed is the same five-second sliding-window difference the window shows, so a
script and the interface can never disagree about how fast the link is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path

from .config import MAX_STREAMS, MIN_STREAMS, Settings, clamp_streams
from .engine import Task, planned_streams
from .net import Client, NetError
from .probe import probe

RATE_WINDOW_S = 5.0  # the same window the interface averages over
REPORT_INTERVAL_S = 5.0  # how often a line is written (unless the pool changes size)
HASH_CHUNK = 8 * 1024 * 1024

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_HASH = 2
EXIT_STOPPED = 130  # the shell's convention for "killed by SIGINT"


def human_bytes(count: float | None) -> str:
    """`1536` -> `1.5 KiB`. Nothing here rounds a size into a lie."""
    if count is None:
        return "未知"
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def human_seconds(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "未知"
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分"
    return f"{seconds / 3600:.1f} 小时"


def sha256_of(path: Path) -> str:
    """The file's digest, read in chunks: a 16 GB weight file is not a memory test."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def wanted_digest(text: str | None) -> str:
    """`sha256:ABC…`, `ABC…` and any case are the same answer."""
    if not text:
        return ""
    return text.strip().lower().removeprefix("sha256:").strip()


class Reporter:
    """Turns the task's progress stream into lines somebody can read or parse."""

    def __init__(self, *, as_json: bool, quiet: bool, interval: float = REPORT_INTERVAL_S) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.interval = max(0.0, interval)
        self.done = 0
        self.total: int | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._last_line = 0.0
        self._last_streams = 0

    def say(self, line: str) -> None:
        """A human line. In JSON mode stdout carries events only, so this is text-mode only."""
        if not self.as_json:
            print(line, flush=True)

    def event(self, kind: str, **fields: object) -> None:
        if self.as_json:
            print(json.dumps({"event": kind, **fields}, ensure_ascii=False), flush=True)

    def rate(self, done: int) -> float:
        """Bytes per second over the last few seconds — the window the interface uses."""
        now = time.monotonic()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > RATE_WINDOW_S:
            self._samples.popleft()
        if len(self._samples) < 2:
            return 0.0
        (first_t, first_bytes), (last_t, last_bytes) = self._samples[0], self._samples[-1]
        span = last_t - first_t
        return (last_bytes - first_bytes) / span if span > 0.05 else 0.0

    def progress(self, done: int, total: int | None, streams: int) -> None:
        self.done, self.total = done, total
        rate = self.rate(done)
        now = time.monotonic()
        # A line on every tick would drown a log; a pool that changed size is news.
        if streams == self._last_streams and now - self._last_line < self.interval:
            return
        self._last_line = now
        self._last_streams = streams
        remaining = (total - done) / rate if total and rate > 0 else None
        if self.as_json:
            self.event(
                "progress",
                done=done,
                total=total,
                streams=streams,
                rate_bps=round(rate),
                eta_s=None if remaining is None else round(remaining, 1),
            )
            return
        if self.quiet:
            return
        share = f"{done * 100 / total:3.0f}%" if total else "  ?"
        print(
            f"{human_bytes(done):>10} / {human_bytes(total):<10} {share} "
            f"{human_bytes(rate)}/s  约剩 {human_seconds(remaining)}  {streams} 条流",
            flush=True,
        )


class StopSwitch:
    """The first Ctrl-C stops the download and keeps it; a second one leaves at once.

    The engine's own stop switch is used rather than letting the exception unwind
    through the pool: that way the landing is handled by the same code path the
    window's 暂停 uses, and the workers get joined instead of being killed with
    the process mid-write. What is left behind is the part plus the note beside it,
    so running the same link again continues instead of starting over.
    """

    def __init__(self, reporter: Reporter) -> None:
        self.reporter = reporter
        self.task: Task | None = None
        self.asked = False
        self._previous: object | None = None

    def arm(self, task: Task) -> None:
        """Called once the task exists: before the probe there is nothing to stop."""
        self.task = task
        if self.asked:
            task.cancel()

    def install(self) -> None:
        self._previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._on_signal)

    def restore(self) -> None:
        """Put the handler back: a library call installs no signal handler of its own forever."""
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)
            self._previous = None

    def _on_signal(self, *_args: object) -> None:
        if self.asked or self.task is None:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        self.asked = True
        self.reporter.say("已中断，part 保留：下次下同一个链接会接着下（要扔掉它用 --fresh）")
        self.task.pause()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flower",
        description="把一条流分成几条，同时去取同一份文件。",
        epilog="退出码：0 落地，1 失败，2 校验值不符，130 被中断。",
    )
    parser.add_argument("url", help="要下载的链接")
    parser.add_argument(
        "-d", "--dir", dest="dest_dir", help="保存到哪个目录（默认取界面里存的那个）"
    )
    parser.add_argument(
        "-n",
        "--streams",
        type=int,
        default=None,
        help=f"最多几条连接一起取（{MIN_STREAMS}–{MAX_STREAMS}，默认取界面里存的那个）",
    )
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--proxy", metavar="HOST:PORT", help="走这个代理（例如 127.0.0.1:7897）")
    route.add_argument("--no-proxy", action="store_true", help="直连，不管界面里的代理开着没有")
    parser.add_argument("--sha256", metavar="HEX", help="取完核对这个 sha256，不符则退出码 2")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="不要接着上次下：删掉留下的 part，从头开始（默认会接着下）",
    )
    parser.add_argument("--json", action="store_true", help="每行一个 JSON 事件，给脚本读")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印进度，只打印结论")
    parser.add_argument(
        "--interval",
        type=float,
        default=REPORT_INTERVAL_S,
        metavar="秒",
        help=f"进度行之间的最小间隔（默认 {REPORT_INTERVAL_S:g}）",
    )
    return parser


def route_of(args: argparse.Namespace, settings: Settings) -> str | None:
    """The route this run will take, as the interface would have set it.

    Never a fallback: `--no-proxy` means direct even when the stored setting says
    otherwise, and a chosen proxy either works or the run fails saying so.
    """
    if args.proxy:
        text = args.proxy.strip()
        return text if "://" in text else f"http://{text}"
    if args.no_proxy:
        return None
    return settings.proxy_url()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    streams = clamp_streams(args.streams if args.streams is not None else settings.streams)
    chosen = args.dest_dir.strip() if args.dest_dir else ""
    dest_dir = Path(chosen) if chosen else settings.resolved_save_dir()
    proxy = route_of(args, settings)
    expect = wanted_digest(args.sha256)

    reporter = Reporter(as_json=args.json, quiet=args.quiet, interval=args.interval)
    stopper = StopSwitch(reporter)
    client = Client(proxy)
    reporter.say(f"链接    {args.url}")
    reporter.say(f"保存到  {dest_dir}")
    reporter.say(f"代理    {proxy or '直连'}")

    started = time.monotonic()
    task: Task | None = None
    try:
        found = probe(client, args.url)
        planned = planned_streams(found, streams)
        reporter.say(
            f"文件    {found.filename}（{human_bytes(found.size)}，"
            f"{'支持分段' if found.ranges else '不支持分段，单流取'}，计划 {planned} 条流）"
        )
        reporter.event(
            "probe",
            url=found.url,
            file=found.filename,
            size=found.size,
            ranges=found.ranges,
            streams=planned,
            dir=str(dest_dir),
            proxy=proxy,
        )
        task = Task(
            client, found, dest_dir, streams, source=args.url, sha256=expect, resume=not args.fresh
        )

        def _picked_up(picked: int) -> None:
            reporter.say(f"续传    上次留下 {human_bytes(picked)}，从这里接着下")
            reporter.event("resumed", done=picked, total=found.size)

        task.on_resume = _picked_up
        task.watch(reporter.progress)
        stopper.install()
        try:
            stopper.arm(task)
            landed = task.run()
        finally:
            stopper.restore()
    except KeyboardInterrupt:
        reporter.say("已中断")
        reporter.event("stopped", interrupted=True, done=reporter.done, total=reporter.total)
        return EXIT_STOPPED
    except (NetError, OSError) as exc:
        reporter.say(f"失败    {exc}")
        reporter.event("failed", error=str(exc))
        return EXIT_FAILED

    elapsed = time.monotonic() - started
    if landed is None:
        reporter.say("已中断，part 保留着（用 --fresh 从头下）" if stopper.asked else "已停止")
        reporter.event(
            "stopped",
            interrupted=stopper.asked,
            done=reporter.done,
            total=reporter.total,
            seconds=round(elapsed, 1),
        )
        return EXIT_STOPPED

    stats = task.stats if task else None
    digest = ""
    if expect:
        try:
            digest = sha256_of(landed)
        except KeyboardInterrupt:  # hashing a 16 GB file is long enough to be interrupted
            reporter.say(f"校验被打断，文件已经在 {landed}")
            reporter.event(
                "stopped",
                interrupted=True,
                done=reporter.done,
                total=reporter.total,
                file=str(landed),
            )
            return EXIT_STOPPED
        if digest != expect:
            reporter.say(f"校验不符  期望 {expect}\n          实际 {digest}")
            reporter.event(
                "failed", error="sha256 mismatch", file=str(landed), expected=expect, actual=digest
            )
            return EXIT_HASH
    reporter.say(
        f"完成    {landed}（{human_bytes(landed.stat().st_size)}，{human_seconds(elapsed)}，"
        f"峰值 {stats.peak_streams if stats else 1} 条流，停滞 {stats.stalls if stats else 0} 次）"
    )
    reporter.event(
        "done",
        file=str(landed),
        bytes=landed.stat().st_size,
        sha256=digest or None,
        seconds=round(elapsed, 1),
        streams_peak=stats.peak_streams if stats else 1,
        additions=stats.additions if stats else 0,
        retirements=stats.retirements if stats else 0,
        stalls=stats.stalls if stats else 0,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
