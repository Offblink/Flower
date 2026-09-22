"""The part file's rules: coverage is what proves a download, not its length."""

from __future__ import annotations

import pytest

from flower.landing import (
    Landing,
    Spans,
    TransferTruncatedError,
    free_name,
    lock_path,
    part_path,
    record_path,
)


def test_spans_merge_neighbours_and_report_the_frontier():
    spans = Spans()
    spans.add(10, 20)
    spans.add(20, 30)  # touching spans become one
    spans.add(0, 5)
    assert spans.bytes == 25
    assert spans.covers(0, 30) is False  # 5..10 is a hole
    assert spans.covers(10, 30) is True
    assert spans.end_of_run(0) == 5  # where a resumed window would restart
    assert spans.end_of_run(10) == 30


def test_a_right_sized_file_with_a_hole_is_not_committed(tmp_path):
    land = Landing(tmp_path / "blob.bin", expect=64, tag="t")
    land.start(fresh=True)
    with land.writer(0) as handle:
        handle.write(b"a" * 32)
    land.written(0, 32)
    with land.writer(32) as handle:
        handle.write(b"b" * 32)
    land.written(40, 64)  # 32..40 was never reported, but the file is the announced size
    with pytest.raises(TransferTruncatedError):
        land.commit()
    assert not (tmp_path / "blob.bin").exists()


def test_commit_renames_and_discard_leaves_nothing(tmp_path):
    doomed = Landing(tmp_path / "gone.bin", expect=4, tag="t")
    doomed.start(fresh=True)
    with doomed.writer(0) as handle:
        handle.write(b"half")
    doomed.discard()
    assert not doomed.part.exists()

    kept = Landing(tmp_path / "kept.bin", expect=4, tag="t")
    kept.start(fresh=True)
    with kept.writer(0) as handle:
        handle.write(b"full")
    kept.written(0, 4)
    assert kept.commit() == tmp_path / "kept.bin"
    assert kept.part.exists() is False


def test_a_second_start_adopts_the_part_an_earlier_attempt_left(tmp_path):
    dest = tmp_path / "blob.bin"
    land = Landing(dest, expect=8, tag="t")
    land.start(fresh=True)
    with land.writer(0) as handle:
        handle.write(b"1234")
    land.written(0, 4)

    again = Landing(dest, expect=8, tag="t")
    again.start(fresh=False)  # a resume, not a new download
    assert again.part.read_bytes() == b"1234"
    assert part_path(dest, "t").name == "blob.bin.t.part"


def test_an_occupied_name_steps_aside(tmp_path):
    taken = tmp_path / "report.pdf"
    taken.write_bytes(b"the user's own file")
    for index in (2, 3):
        fresh = free_name(taken)
        assert fresh.name == f"report ({index}).pdf"
        fresh.write_bytes(b"")
    assert free_name(tmp_path / "free.pdf") == tmp_path / "free.pdf"  # nothing taken, no suffix


def _dead_run(
    dest,
    written: int,
    *,
    source: str = "http://x/blob.bin",
    size: int = 64,
    tag: str = "aaa",
    sha256: str = "",
    release: bool = True,
) -> Landing:
    """What a run that dies leaves behind: a part with bytes in it, and the note beside it."""
    land = Landing(dest, expect=size, tag=tag, source=source, sha256=sha256)
    assert land.lock.take(), "the helper builds a run that owns the destination"
    land.start(fresh=True)
    with land.writer(0) as handle:
        handle.write(b"a" * written)
    land.written(0, written)
    land.persist(force=True)
    if release:  # the process is gone, so the kernel has dropped the claim with it
        land.lock.release()
    return land


def test_a_note_lets_a_later_run_keep_writing_where_the_bytes_are(tmp_path):
    dest = tmp_path / "blob.bin"
    _dead_run(dest, 24)

    land = Landing(dest, expect=64, tag="bbb", source="http://x/blob.bin")
    assert land.adopt() is True
    assert land.spans.bytes == 24
    assert land.spans.end_of_run(0) == 24  # where the next window starts, not zero
    assert land.part == part_path(dest, "aaa"), "the part with the bytes, not a new one"


def test_a_note_from_another_link_is_dropped_with_the_part_it_names(tmp_path):
    dest = tmp_path / "blob.bin"
    stale = _dead_run(dest, 24, source="http://x/other.bin")

    land = Landing(dest, expect=64, tag="bbb", source="http://x/blob.bin")
    assert land.adopt() is False
    assert land.spans.bytes == 0
    assert not stale.part.exists(), "no run will ever look for that tag again"
    assert not record_path(dest).exists()


def test_a_note_of_another_length_or_digest_is_not_believed(tmp_path):
    dest = tmp_path / "blob.bin"
    _dead_run(dest, 24, size=64)
    other_length = Landing(dest, expect=99, tag="b", source="http://x/blob.bin")
    assert other_length.adopt() is False
    other_length.lock.release()  # that run ends, so the next one may claim the destination

    _dead_run(dest, 24, sha256="a" * 64)
    wrong = Landing(dest, expect=64, tag="c", source="http://x/blob.bin", sha256="b" * 64)
    assert wrong.adopt() is False, "a digest that disagrees is a different file"
    wrong.lock.release()

    _dead_run(dest, 24, sha256="a" * 64)
    right = Landing(dest, expect=64, tag="d", source="http://x/blob.bin", sha256="a" * 64)
    assert right.adopt() is True


def test_a_note_that_reaches_past_the_part_is_clipped_to_what_is_there(tmp_path):
    dest = tmp_path / "blob.bin"
    land = _dead_run(dest, 24)
    land.part.write_bytes(b"a" * 16)  # the file is shorter than the note claims

    again = Landing(dest, expect=64, tag="bbb", source="http://x/blob.bin")
    assert again.adopt() is True
    assert again.spans.bytes == 16, "bytes the file does not have are not adopted"


def test_a_second_live_run_may_not_touch_the_first_ones_part(tmp_path):
    dest = tmp_path / "blob.bin"
    alive = _dead_run(dest, 24, release=False)
    assert alive.lock.held  # the first run is still going, so it still holds the claim

    other = Landing(dest, expect=64, tag="bbb", source="http://x/blob.bin")
    assert other.adopt() is False
    assert alive.part.exists(), "a run that does not own the destination deletes nothing"
    assert record_path(dest).exists()


def test_fresh_throws_away_what_an_earlier_run_left(tmp_path):
    dest = tmp_path / "blob.bin"
    stale = _dead_run(dest, 24)

    land = Landing(dest, expect=64, tag="bbb", source="http://x/blob.bin", resumable=False)
    assert land.adopt() is False
    assert not stale.part.exists()
    assert not record_path(dest).exists()


def test_the_note_and_the_claim_go_when_the_download_lands(tmp_path):
    dest = tmp_path / "blob.bin"
    land = Landing(dest, expect=4, tag="t", source="http://x/blob.bin")
    assert land.adopt() is False  # nothing to continue: no note yet
    land.start(fresh=True)
    with land.writer(0) as handle:
        handle.write(b"full")
    land.written(0, 4)
    land.persist(force=True)
    assert record_path(dest).exists() and lock_path(dest).exists()

    assert land.commit() == dest
    assert not record_path(dest).exists()
    assert not lock_path(dest).exists()
