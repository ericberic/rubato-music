"""Declaring a free region must leave the section map tiling the piece."""

from __future__ import annotations

import json

import pytest

from aimusic.accompaniment.free_regions import (
    FreeRegionSpec,
    clear,
    declare,
    declared_regions,
)
from aimusic.accompaniment.section_policy import SectionMap

BASE = {
    "piece_id": "test",
    "sections": [
        {"id": "lead-in", "start_beat": 0, "end_beat": 40, "mode": "LEAD"},
        {"id": "solo", "start_beat": 40, "end_beat": 412, "mode": "FOLLOW"},
        {"id": "interlude", "start_beat": 412, "end_beat": 417, "mode": "LEAD"},
        {"id": "solo-2", "start_beat": 417, "end_beat": 505, "mode": "FOLLOW"},
    ],
}


@pytest.fixture()
def sections(tmp_path):
    path = tmp_path / "sections.json"
    path.write_text(json.dumps(BASE), encoding="utf-8")
    return path


def _beats(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [(s["id"], s["start_beat"], s["end_beat"], s["mode"]) for s in payload["sections"]]


def test_declaring_splits_the_enclosing_follow(sections) -> None:
    """A region in the middle of a FOLLOW leaves a section on either side."""

    declare(sections, FreeRegionSpec(from_measure=60, to_measure=62, label="Fermata"))
    ids = [row[0] for row in _beats(sections)]
    assert "m60-free" in ids
    assert "solo" in ids and "solo-after" in ids


def test_a_region_flush_with_the_end_leaves_no_empty_remainder(sections) -> None:
    """m.101-103 ends at beat 412, exactly where the enclosing FOLLOW ended.

    Emitting a zero-length trailing section would tile arithmetically while
    being meaningless, and section_at would never return it.
    """

    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    rows = _beats(sections)
    assert all(end > start for _, start, end, _ in rows)
    assert [row[0] for row in rows] == ["lead-in", "solo", "m101-free", "interlude", "solo-2"]


def test_the_map_still_tiles_without_gaps(sections) -> None:
    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    rows = _beats(sections)
    for (_, _, prev_end, _), (_, start, _, _) in zip(rows, rows[1:]):
        assert prev_end == start


def test_the_result_still_loads_as_a_section_map(sections) -> None:
    """The real consumer. A file that parses here but not in the runtime is useless."""

    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    loaded = SectionMap.from_json_file(sections)
    assert str(loaded.section_at(404.0).mode) == "FREE"
    assert str(loaded.section_at(399.0).mode) == "FOLLOW"
    assert str(loaded.section_at(412.0).mode) == "LEAD"


def test_boundaries_land_on_the_measure_the_performer_named(sections) -> None:
    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    region = declared_regions(sections)[0]
    assert region["start_beat"] == 400.0  # m.101 beat 1
    assert region["end_beat"] == 412.0  # m.104 beat 1, the hand-back
    assert region["free_region"]["hand_back_measure"] == 104


def test_annotations_are_stored_in_measures_not_ticks(sections) -> None:
    """Ticks are derived and relocate when the beat map is rebuilt."""

    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    block = declared_regions(sections)[0]["free_region"]
    assert block["from_measure"] == 101 and block["to_measure"] == 103
    assert not any("tick" in key for key in block)


def test_a_region_may_not_overlap_a_lead_passage(sections) -> None:
    """The orchestra already owns the timing there."""

    with pytest.raises(ValueError, match="LEAD"):
        declare(sections, FreeRegionSpec(from_measure=101, to_measure=104))


def test_regions_may_not_overlap_each_other(sections) -> None:
    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    with pytest.raises(ValueError, match="already overlaps"):
        declare(sections, FreeRegionSpec(from_measure=102, to_measure=103))


def test_a_span_outside_the_score_is_rejected(sections) -> None:
    with pytest.raises(ValueError, match="outside the score"):
        declare(sections, FreeRegionSpec(from_measure=900, to_measure=902))


def test_clearing_closes_the_hole(sections) -> None:
    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    assert clear(sections, from_measure=101) is True
    assert declared_regions(sections) == []
    rows = _beats(sections)
    for (_, _, prev_end, _), (_, start, _, _) in zip(rows, rows[1:]):
        assert prev_end == start
    assert str(SectionMap.from_json_file(sections).section_at(404.0).mode) == "FOLLOW"


def test_clearing_something_undeclared_reports_rather_than_raises(sections) -> None:
    assert clear(sections, from_measure=77) is False


def test_backwards_and_zero_spans_are_rejected() -> None:
    with pytest.raises(ValueError):
        FreeRegionSpec(from_measure=103, to_measure=101)
    with pytest.raises(ValueError):
        FreeRegionSpec(from_measure=0, to_measure=2)


def test_declare_then_clear_restores_the_original_map(sections) -> None:
    """A round trip must be a no-op, not leave adjacent fragments behind.

    Without coalescing, clearing left two touching FOLLOW sections where there
    had been one. Behaviour was identical so nothing failed, but repeating the
    cycle shreds the map and makes every later diff unreadable.
    """

    before = _beats(sections)
    declare(sections, FreeRegionSpec(from_measure=60, to_measure=62))
    clear(sections, from_measure=60)
    assert _beats(sections) == before


def test_coalescing_does_not_merge_across_a_declared_region(sections) -> None:
    declare(sections, FreeRegionSpec(from_measure=60, to_measure=62))
    declare(sections, FreeRegionSpec(from_measure=101, to_measure=103))
    clear(sections, from_measure=60)
    modes = [row[3] for row in _beats(sections)]
    assert modes.count("FREE") == 1
