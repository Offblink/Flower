"""Probing: the three answers, from the three shapes hosts actually answer with."""

from __future__ import annotations

from flower.net import Client
from flower.probe import probe, safe_name
from tests.fake_server import FakeServer

PAYLOAD = b"x" * 300_000


def test_a_cooperative_host_is_understood_from_head_alone():
    with FakeServer(PAYLOAD) as server:
        found = probe(Client(), server.url)
        assert (found.size, found.ranges) == (len(PAYLOAD), True)
        assert found.filename == "blob.bin"
    assert server.requests_of("HEAD") == [""]  # no second request was needed


def test_a_host_that_ignores_ranges_is_told_so():
    with FakeServer(PAYLOAD, ranges=False) as server:
        found = probe(Client(), server.url)
        assert found.ranges is False
        assert found.size == len(PAYLOAD)  # read off the 200 the peek got
    # HEAD said nothing about ranges, so the one-byte GET had to answer
    assert server.requests_of("GET") == ["bytes=0-0"]


def test_a_host_that_will_not_answer_head_is_still_split(tmp_path):
    with FakeServer(PAYLOAD, head_status=405) as server:
        found = probe(Client(), server.url)
        assert (found.size, found.ranges) == (len(PAYLOAD), True)


def test_the_name_comes_from_the_header_then_the_url():
    with FakeServer(
        PAYLOAD, disposition="attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95.bin"
    ) as server:
        assert probe(Client(), server.url).filename == "测试.bin"
    with FakeServer(PAYLOAD, disposition=None) as server:
        assert probe(Client(), server.url).filename == "blob.bin"  # the URL's last segment
    with FakeServer(PAYLOAD, disposition='attachment; filename="../../etc/passwd"') as server:
        assert probe(Client(), server.url).filename == "passwd"  # never a path


def test_names_windows_refuses_are_cleaned_up():
    assert safe_name('a<b>c:d"e|f?g*h') == "abcdefgh"
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "ghij"  # only ever the last segment
    assert safe_name("   ") == ""
    assert safe_name("report.pdf") == "report.pdf"
