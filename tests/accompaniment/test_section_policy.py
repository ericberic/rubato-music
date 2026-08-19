from __future__ import annotations

import pytest

from aimusic.accompaniment.section_policy import AccompanimentMode, SectionMap


def test_section_map_returns_expected_modes() -> None:
    section_map = SectionMap.from_dict(
        {
            "piece_id": "chopin_op11_excerpt",
            "sections": [
                {
                    "id": "tutti",
                    "start_beat": 0,
                    "end_beat": 8,
                    "mode": "LEAD",
                    "tempo_bpm": 88,
                },
                {
                    "id": "solo",
                    "start_beat": 8,
                    "end_beat": 24,
                    "mode": "FOLLOW",
                },
                {
                    "id": "cadence",
                    "start_beat": 24,
                    "end_beat": 28,
                    "mode": "HOLD",
                },
            ],
        }
    )

    assert section_map.mode_at(0) == AccompanimentMode.LEAD
    assert section_map.mode_at(7.999) == AccompanimentMode.LEAD
    assert section_map.mode_at(8) == AccompanimentMode.FOLLOW
    assert section_map.mode_at(24) == AccompanimentMode.HOLD


def test_section_map_rejects_overlaps() -> None:
    with pytest.raises(ValueError, match="overlaps"):
        SectionMap.from_dict(
            {
                "piece_id": "bad",
                "sections": [
                    {"id": "a", "start_beat": 0, "end_beat": 8, "mode": "LEAD"},
                    {"id": "b", "start_beat": 7, "end_beat": 10, "mode": "FOLLOW"},
                ],
            }
        )


def test_section_map_sorts_unordered_non_overlapping_sections() -> None:
    section_map = SectionMap.from_dict(
        {
            "piece_id": "unordered",
            "sections": [
                {"id": "solo", "start_beat": 8, "end_beat": 16, "mode": "FOLLOW"},
                {"id": "tutti", "start_beat": 0, "end_beat": 8, "mode": "LEAD"},
                {"id": "hold", "start_beat": 16, "end_beat": 20, "mode": "HOLD"},
            ],
        }
    )

    assert [section.id for section in section_map.sections] == ["tutti", "solo", "hold"]
    assert section_map.mode_at(0) == AccompanimentMode.LEAD
    assert section_map.mode_at(8) == AccompanimentMode.FOLLOW
    assert section_map.mode_at(16) == AccompanimentMode.HOLD


def test_section_map_requires_covered_beat() -> None:
    section_map = SectionMap.from_dict(
        {
            "piece_id": "gap",
            "sections": [
                {"id": "a", "start_beat": 0, "end_beat": 8, "mode": "LEAD"},
            ],
        }
    )

    with pytest.raises(ValueError, match="No section covers beat"):
        section_map.mode_at(8)
