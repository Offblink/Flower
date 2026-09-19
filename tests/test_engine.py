"""The engine's promises: windows, a drop that costs only its own window, resume.

Everything here runs against a real HTTP server on loopback — a fake host that
can drop a body, ignore ranges, or refuse them — because the things worth
asserting are what the client *asks for* after something goes wrong.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from flower.engine import RangeRefusedError, Task, planned_streams, windows
from flower.landing import part_path
from flower.net import Client
from flower.probe import Probe, probe
from tests.fake_server import FakeServer

WINDOW = 64 * 1024


def _fetch(server: FakeServer, dest, streams: int = 4) -> Task:
    found = probe(Client(), server.url)
    return Task(Client(), found, dest, streams, min_window_bytes=WINDOW)


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the download to get there")


def test_windows_never_make_a_window_smaller_than_it_is_worth():
    assert windows(100, 4) == [(0, 100)]  # below the minimum: one stream
    assert windows(8 * WINDOW, 4, WINDOW) == [
        (0, 2 * WINDOW),
        (2 * WINDOW, 4 * WINDOW),
        (4 * WINDOW, 6 * WINDOW),
        (6 * WINDOW, 8 * WINDOW),
    ]
    assert windows(8 * WINDOW, 16, WINDOW) == windows(8 * WINDOW, 8, WINDOW)  # capped by size


def test_a_dropped_piece_resumes_from_its_frontier_not_from_zero(tmp_path):
    payload = bytes(range(256)) * (2 * 1024)  # 512 KiB, cut into 64 KiB pieces
    dropped = 2 * WINDOW  # a piece starts here; half of it will be written
    with FakeServer(payload, drop_ranges={dropped}) as server:
        landed = _fetch(server, tmp_path).run()
        assert landed.read_bytes() == payload
        starts = server.range_starts()
    # The dropped window's body stopped half way, so the retry asked for the byte
    # the disk had actually reached — this is the whole of "a drop costs a window".
    assert dropped in starts
    assert dropped + WINDOW // 2 in starts  # half of that 64 KiB piece reached the disk
    assert not part_path(tmp_path / "blob.bin").exists()


def test_a_server_that_ignores_the_range_is_used_as_a_single_stream(tmp_path):
    """The probe can be right and the range still refused: that answer *is* the file."""
    payload = b"z" * (256 * WINDOW)
    with FakeServer(payload, ranges=False) as server:
        stale = Probe(server.url, len(payload), True, "blob.bin")  # as a cooperative host looks
        landed = Task(Client(), stale, tmp_path, 4, min_window_bytes=WINDOW).run()
        assert landed.read_bytes() == payload
        assert server.range_starts() == [0]  # asked once, then the whole body was taken


def _start_and_pause(server: FakeServer, dest, streams: int = 2) -> tuple[Task, Path]:
    """Start a download and pause it mid-flight; gives back the task and its part file."""
    task = _fetch(server, dest, streams=streams)
    seen: list[int] = []
    task.watch(lambda done, _total, _streams: seen.append(done))
    runner = threading.Thread(target=task.run, daemon=True)
    runner.start()
    _wait_for(lambda: seen and seen[-1] >= 200 * 1024)
    task.pause()
    runner.join(10)
    assert not runner.is_alive()
    return task, part_path(dest / "blob.bin", task.tag)


def test_pause_keeps_the_part_and_resume_fetches_only_the_rest(tmp_path):
    payload = bytes(range(256)) * (8 * 1024)  # 2 MiB
    with FakeServer(payload, flow_chunk=8192, flow_delay=0.004) as server:
        task, part = _start_and_pause(server, tmp_path)
        assert part.exists()  # a paused download keeps what it has
        assert not (tmp_path / "blob.bin").exists()  # and never shows a half file

        server.flow_chunk = 0
        server.flow_delay = 0.0
        before = len(server.range_starts())
        landed = task.run()
        assert landed.read_bytes() == payload
        resumed = server.range_starts()[before:]

    assert 0 not in resumed, "the resumed download asked for byte zero again"
    assert min(resumed) > 0


def test_cancelling_a_paused_download_deletes_its_part(tmp_path):
    payload = bytes(range(256)) * (8 * 1024)
    with FakeServer(payload, flow_chunk=8192, flow_delay=0.004) as server:
        task, part = _start_and_pause(server, tmp_path)
        assert part.exists()
        task.discard()  # 取消 pressed while it sits paused
        assert not part.exists()
        assert not (tmp_path / "blob.bin").exists()


def test_a_stalling_link_makes_the_pool_retire_connections(tmp_path):
    """Stalls are the evidence the count should come down — and it stays down."""
    payload = bytes(range(256)) * (8 * 1024)  # 2 MiB, cut into 128 KiB pieces
    stalled = {128 * 1024, 384 * 1024, 640 * 1024}  # three pieces lose their body half way
    with FakeServer(payload, drop_ranges=stalled, flow_chunk=32768, flow_delay=0.005) as server:
        task = _fetch(server, tmp_path, streams=4)
        task.adapt_interval_s = 0.05
        task.adapt_settle_s = 0.1
        landed = task.run()
        assert landed.read_bytes() == payload
    assert task.stats.stalls > 0, "the dropped bodies were never counted as stalls"
    assert task.stats.retirements >= 1, "a stalling link kept all its connections"
    assert not part_path(tmp_path / "blob.bin").exists()


def test_a_healthy_link_lets_the_pool_grow_toward_the_cap(tmp_path):
    """No stalls: the pool is free to add connections, up to what the user asked for."""
    payload = bytes(range(256)) * (32 * 1024)  # 8 MiB, cut into 64 KiB pieces
    with FakeServer(payload, flow_chunk=65536, flow_delay=0.02) as server:
        task = _fetch(server, tmp_path, streams=4)
        task.min_window_bytes = 64 * 1024
        task.adapt_interval_s = 0.05
        task.adapt_settle_s = 0.1
        landed = task.run()
        assert landed.read_bytes() == payload
    assert task.stats.additions >= 1, "a healthy link never grew the pool"
    assert task.stats.peak_streams > 2, f"the pool peaked at {task.stats.peak_streams}"
    assert task.stats.retirements == 0


def test_a_pause_pressed_during_the_probe_is_not_swallowed(tmp_path):
    """The probe can take seconds; a pause pressed then must still be honoured."""
    with FakeServer(b"q" * (2 * WINDOW)) as server:
        task = _fetch(server, tmp_path, streams=1)
        task.pause()  # arrives before run(), exactly as the interface can
        assert task.run() is None
        assert task.keep_part is True
        assert not part_path(tmp_path / "blob.bin", task.tag).exists()  # nothing was fetched
        assert server.requests_of("GET") == []


def test_a_refused_range_is_reprobed_once_and_then_given_up_on(tmp_path):
    with FakeServer(b"y" * (64 * WINDOW), refuse_ranges=True) as server:
        task = _fetch(server, tmp_path)
        with pytest.raises(RangeRefusedError):
            task.run()
    assert len(server.requests_of("HEAD")) == 2  # the original probe, then one re-probe
    assert not task.dest.exists()
    assert not part_path(task.dest, task.tag).exists()


def test_planned_streams_reports_what_the_download_will_do():
    cooperative = Probe("http://x/blob.bin", 32 * 1024 * 1024, True, "blob.bin")
    assert planned_streams(cooperative, 4) == 4
    assert planned_streams(cooperative, 1) == 1
    assert planned_streams(cooperative, 16) == 8  # never more windows than 4 MiB each
    assert planned_streams(Probe("http://x/b", 8 * 1024 * 1024, True, "b"), 4) == 2
    assert planned_streams(Probe("http://x/b", 8 * 1024 * 1024, False, "b"), 4) == 1
    assert planned_streams(Probe("http://x/b", None, True, "b"), 4) == 1
