"""Tests for the per-job timing breakdown."""

from __future__ import annotations

import pytest

from hoplink.core.timing import PHASES, Timings, human_bytes


def filled(**phases: float) -> Timings:
    """A Timings with one measurement recorded per named phase."""
    timings = Timings()
    for phase, seconds in phases.items():
        timings.record(phase, seconds)
    return timings


def test_repeated_measurements_of_a_phase_add_up():
    timings = Timings()
    timings.record("write", 1.5)
    timings.record("write", 0.5)
    assert timings.seconds["write"] == 2.0
    assert timings.calls["write"] == 2


def test_a_negative_measurement_is_clamped_rather_than_credited_back():
    timings = filled(write=1.0)
    timings.record("write", -5.0)
    assert timings.seconds["write"] == 1.0
    assert timings.calls["write"] == 2


def test_measure_records_the_block_it_wraps():
    timings = Timings()
    with timings.measure("hash"):
        pass
    assert timings.calls["hash"] == 1
    assert timings.seconds["hash"] >= 0.0


def test_measure_still_records_when_the_block_raises():
    timings = Timings()
    with pytest.raises(RuntimeError):
        with timings.measure("write"):
            raise RuntimeError("disk full")
    assert timings.calls["write"] == 1


def test_total_sums_every_phase():
    assert filled(harvest=2.0, write=0.5).total == 2.5


def test_an_untouched_timings_is_empty():
    timings = Timings()
    assert timings.total == 0.0
    assert timings.lines() == []
    assert timings.to_dict() == {"seconds": {}, "calls": {}, "downloaded_bytes": 0}


def test_rows_follow_pipeline_order_not_insertion_order():
    timings = filled(manifest=1.0, write=2.0, harvest=3.0)
    rows = [line.split()[0] for line in timings.lines()[1:]]
    assert rows == ["harvest", "write", "manifest"]


def test_a_phase_with_no_time_is_left_out():
    timings = filled(harvest=1.0)
    assert not any("hash" in line for line in timings.lines())


def test_the_wall_clock_gives_each_phase_a_share():
    timings = filled(harvest=5.0)
    header, row = timings.lines(wall=10.0)
    assert "wall 10.0s" in header
    assert "50.0%" in row


def test_shares_are_omitted_without_a_wall_clock():
    assert "%" not in "".join(filled(harvest=5.0).lines())


def test_a_zero_wall_clock_does_not_divide_by_zero():
    assert filled(harvest=5.0).lines(wall=0.0)


def test_overlapping_downloads_are_flagged_so_shares_are_not_read_as_a_partition():
    timings = filled(fetch_body=30.0)
    assert "overlap" in timings.lines(wall=10.0, overlapped=True)[0]
    assert "overlap" not in timings.lines(wall=10.0)[0]


def test_an_average_is_shown_only_once_a_phase_has_repeated():
    timings = Timings()
    timings.record("fetch_body", 2.0)
    assert "avg" not in timings.lines()[1]
    timings.record("fetch_body", 4.0)
    assert "avg   3.00s" in timings.lines()[1]


def test_the_transfer_rate_is_reported_against_the_body_time_only():
    # 20 MB over 2s of body transfer is 10 MB/s, whatever the waiting cost.
    timings = filled(fetch_wait=8.0, fetch_body=2.0)
    timings.downloaded_bytes = 20_000_000
    assert "at 10.0 MB/s" in timings.lines()[-1]


def test_no_rate_is_claimed_when_nothing_was_transferred():
    assert "MB/s" not in "".join(filled(fetch_wait=8.0).lines())


def test_every_phase_has_a_label():
    timings = Timings()
    for phase in PHASES:
        timings.record(phase, 1.0)
    for line in timings.lines()[1:]:
        assert len(line.split("x1")[-1].strip()) > 0


def test_to_dict_rounds_to_milliseconds_and_keeps_pipeline_order():
    timings = filled(write=0.0004999, harvest=1.23456)
    document = timings.to_dict()
    assert list(document["seconds"]) == ["harvest", "write"]
    assert document["seconds"] == {"harvest": 1.235, "write": 0.0}
    assert document["calls"] == {"harvest": 1, "write": 1}


@pytest.mark.parametrize(
    "count, expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1024 * 1024, "1.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2048 * 1024**3, "2048.0 GB"),  # no unit above GB
    ],
)
def test_byte_counts_are_formatted_for_a_person(count, expected):
    assert human_bytes(count) == expected
