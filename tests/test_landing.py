"""The part file's rules: coverage is what proves a download, not its length."""

from __future__ import annotations

import pytest

from flower.landing import Landing, Spans, TransferTruncatedError, free_name, part_path


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
