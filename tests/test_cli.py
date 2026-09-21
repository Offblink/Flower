"""The command line's contract: what it prints, what it exits with, what it leaves behind.

Everything runs against the same fake host the engine's tests use, because the
questions worth asking here are the same ones — does the file land, does a bad
link still land it, and does a failure leave nothing behind. On top of that the
CLI has promises of its own: a verdict in the exit code, `--json` that a script
can parse, `--sha256` that is actually checked, and a signal handler that is put
back where it was found.
"""

from __future__ import annotations

import hashlib
import json
import signal
from pathlib import Path

import pytest

from flower.cli import EXIT_FAILED, EXIT_HASH, EXIT_OK, main
from tests.fake_server import FakeServer

PAYLOAD = bytes(range(256)) * 1024  # 256 KiB: one window, and quick in a test run


def _lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def _events(capsys) -> list[dict]:
    """Every stdout line the run wrote, parsed — the `--json` contract."""
    return [json.loads(line) for line in _lines(capsys)]


def _download(server: FakeServer, dest: Path, *extra: str) -> int:
    return main([server.url, "-d", str(dest), *extra])


def test_the_file_lands_and_the_report_says_where(tmp_path, capsys):
    with FakeServer(PAYLOAD) as server:
        code = _download(server, tmp_path)
        out = capsys.readouterr().out
    landed = tmp_path / "blob.bin"
    assert code == EXIT_OK
    assert landed.read_bytes() == PAYLOAD
    assert str(landed) in out
    assert "256.0 KiB" in out  # the size it reports is the size it got
    assert not list(tmp_path.glob("*.part"))


def test_a_host_that_ignores_ranges_still_lands_the_file(tmp_path, capsys):
    with FakeServer(PAYLOAD, ranges=False) as server:
        code = _download(server, tmp_path)
        out = capsys.readouterr().out
    assert code == EXIT_OK
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD
    assert "不支持分段" in out  # the report admits the降级 instead of pretending to split


def test_a_body_that_dies_half_way_does_not_cost_the_whole_download(tmp_path):
    with FakeServer(PAYLOAD, drop_ranges={0}) as server:
        code = _download(server, tmp_path)
    assert code == EXIT_OK
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_a_refused_range_fails_loudly_and_leaves_nothing_behind(tmp_path, capsys):
    with FakeServer(PAYLOAD, refuse_ranges=True) as server:
        code = _download(server, tmp_path)
        out = capsys.readouterr().out
    assert code == EXIT_FAILED
    assert "失败" in out
    assert not (tmp_path / "blob.bin").exists()  # never a half file under the real name
    assert not list(tmp_path.glob("*.part"))


def test_an_unreachable_host_is_a_failed_event_not_a_traceback(capsys):
    code = main(["http://127.0.0.1:1/blob.bin", "-d", ".", "--json"])
    assert code == EXIT_FAILED
    events = _events(capsys)
    assert events[-1]["event"] == "failed"
    assert events[-1]["error"]


def test_the_sha256_it_was_given_is_checked(tmp_path, capsys):
    good = hashlib.sha256(PAYLOAD).hexdigest()
    with FakeServer(PAYLOAD) as server:
        code = _download(server, tmp_path, "--sha256", f"sha256:{good.upper()}", "--json")
        events = _events(capsys)
    assert code == EXIT_OK
    assert events[-1]["event"] == "done"
    assert events[-1]["sha256"] == good


def test_a_hash_mismatch_is_its_own_verdict_and_names_both_digests(tmp_path, capsys):
    with FakeServer(PAYLOAD) as server:
        code = _download(server, tmp_path, "--sha256", "0" * 64, "--json")
        captured = capsys.readouterr().out
    landed = tmp_path / "blob.bin"
    assert code == EXIT_HASH
    assert landed.exists(), "a download that landed is not deleted because a hash disagreed"
    assert "0" * 64 in captured
    assert hashlib.sha256(PAYLOAD).hexdigest() in captured


def test_json_mode_writes_events_and_nothing_else(tmp_path, capsys):
    with FakeServer(PAYLOAD) as server:
        code = _download(server, tmp_path, "--json", "--interval", "0")
        events = _events(capsys)  # json.loads on every line: a human line would fail here
    assert code == EXIT_OK
    kinds = [event["event"] for event in events]
    assert kinds[0] == "probe"
    assert "progress" in kinds
    assert kinds[-1] == "done"
    probe_event = events[0]
    assert probe_event["file"] == "blob.bin"
    assert probe_event["size"] == len(PAYLOAD)
    assert probe_event["ranges"] is True
    assert events[-1]["bytes"] == len(PAYLOAD)


def test_the_directory_it_was_given_is_created(tmp_path, capsys):
    nested = tmp_path / "deep" / "er"
    with FakeServer(PAYLOAD) as server:
        code = _download(server, nested)
    assert code == EXIT_OK
    assert (nested / "blob.bin").read_bytes() == PAYLOAD


@pytest.mark.parametrize("streams", ["0", "99"])
def test_an_out_of_range_stream_count_is_survivable(tmp_path, streams):
    """The clamp itself is 配置 的事 (config.clamp_streams); here it only has to not blow up."""
    with FakeServer(PAYLOAD) as server:
        assert _download(server, tmp_path, "-n", streams) == EXIT_OK
    assert (tmp_path / "blob.bin").read_bytes() == PAYLOAD


def test_the_signal_handler_is_put_back(tmp_path):
    before = signal.getsignal(signal.SIGINT)
    with FakeServer(PAYLOAD) as server:
        _download(server, tmp_path)
    assert signal.getsignal(signal.SIGINT) is before
