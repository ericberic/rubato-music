"""Coordinate conversions, pinned against the mistakes that motivated them.

A partitura beat compared directly to a runtime score_beat produces an offset of
exactly one measure. That is indistinguishable by eye from a follower running a
bar behind, and it manufactured exactly that false conclusion twice in one
session. These tests exist so the seam fails loudly instead.
"""

from __future__ import annotations

import pytest

from aimusic.accompaniment.score_coordinates import (
    BeatOrigin,
    beat_origin_from_measure_starts,
    canonical_beat_from_measure_beat,
    canonical_beat_of_measure,
    canonical_beat_to_tick,
    measure_and_beat,
    tick_to_canonical_beat,
)

# The real Chopin Op. 11 mvt II orchestral MusicXML: partitura treats bar 1 as a
# pickup, so measure 1 begins at beat -4 and measure 2 at beat 0.
CHOPIN_MVT2_MEASURE_STARTS = {1: -4.0, 2: 0.0, 3: 4.0, 4: 8.0}

# A score with no pickup: partitura and canonical already agree.
ALIGNED_MEASURE_STARTS = {1: 0.0, 2: 4.0, 3: 8.0}


def test_pickup_score_yields_a_one_measure_offset() -> None:
    origin = beat_origin_from_measure_starts(CHOPIN_MVT2_MEASURE_STARTS)
    assert origin.offset == pytest.approx(4.0)
    # partitura measure 1 downbeat -> canonical 0.0, not -4.0
    assert origin.to_canonical(-4.0) == pytest.approx(0.0)
    assert origin.to_canonical(0.0) == pytest.approx(4.0)


def test_score_without_a_pickup_needs_no_correction() -> None:
    origin = beat_origin_from_measure_starts(ALIGNED_MEASURE_STARTS)
    assert origin.offset == pytest.approx(0.0)
    assert origin.to_canonical(12.0) == pytest.approx(12.0)


def test_conversion_round_trips() -> None:
    origin = beat_origin_from_measure_starts(CHOPIN_MVT2_MEASURE_STARTS)
    for beat in (-4.0, 0.0, 17.5, 403.25):
        assert origin.to_partitura(origin.to_canonical(beat)) == pytest.approx(beat)


def test_the_false_alarm_this_prevents() -> None:
    """Uncorrected comparison looks like a follower a full bar ahead.

    A follower reporting canonical 400.0 against a ground truth taken raw from
    partitura appears 4 beats fast. Through the origin it is exact.
    """

    origin = beat_origin_from_measure_starts(CHOPIN_MVT2_MEASURE_STARTS)
    follower_says = 400.0  # canonical
    truth_partitura = 396.0  # same musical instant, partitura axis
    assert follower_says - truth_partitura == pytest.approx(4.0)  # the false alarm
    assert follower_says - origin.to_canonical(truth_partitura) == pytest.approx(0.0)


def test_measure_starts_use_the_lowest_numbered_measure() -> None:
    """A pickup places measure 1 below measure 2; min(beat) would pick the wrong one."""

    origin = beat_origin_from_measure_starts({2: 0.0, 1: -4.0, 3: 4.0})
    assert origin.offset == pytest.approx(4.0)


def test_empty_measure_starts_is_an_error() -> None:
    with pytest.raises(ValueError):
        beat_origin_from_measure_starts({})


@pytest.mark.parametrize(
    "measure,expected",
    [(1, 0.0), (2, 4.0), (17, 64.0), (96, 380.0), (104, 412.0), (126, 500.0)],
)
def test_canonical_beat_of_measure(measure: int, expected: float) -> None:
    assert canonical_beat_of_measure(measure) == pytest.approx(expected)


def test_measure_numbers_are_one_based() -> None:
    with pytest.raises(ValueError):
        canonical_beat_of_measure(0)


@pytest.mark.parametrize(
    "beat,measure,beat_in_measure",
    [
        (0.0, 1, 1.0),
        (3.0, 1, 4.0),
        (380.0, 96, 1.0),
        (407.0, 102, 4.0),  # the orchestra re-entry the performer names as m.102 b4
        (412.0, 104, 1.0),
    ],
)
def test_measure_and_beat_round_trip(beat, measure, beat_in_measure) -> None:
    assert measure_and_beat(beat) == (measure, pytest.approx(beat_in_measure))
    assert canonical_beat_from_measure_beat(measure, beat_in_measure) == pytest.approx(beat)


def test_beat_in_measure_is_one_based() -> None:
    """Beat 1 is the downbeat. A 0-based value is a coordinate bug, not a pickup."""

    with pytest.raises(ValueError):
        canonical_beat_from_measure_beat(104, 0.0)


def test_negative_canonical_beats_are_rejected() -> None:
    with pytest.raises(ValueError):
        measure_and_beat(-4.0)


def test_tick_conversions_use_the_canonical_ppq() -> None:
    assert canonical_beat_to_tick(407.0) == 390720
    assert tick_to_canonical_beat(390720) == pytest.approx(407.0)
    assert tick_to_canonical_beat(canonical_beat_to_tick(96.5)) == pytest.approx(96.5)


def test_origin_is_immutable() -> None:
    origin = BeatOrigin(offset=4.0)
    with pytest.raises(Exception):
        origin.offset = 0.0  # type: ignore[misc]
