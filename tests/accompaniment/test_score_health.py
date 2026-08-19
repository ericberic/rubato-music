"""Cross-artifact consistency checks catch silent coordinate disagreement."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from aimusic.accompaniment.score_health import check_score_bundle, musicxml_measure_count

CANONICAL_PPQ = 960
BAR = 4 * CANONICAL_PPQ


def _bundle(tmp_path: Path, *, measures: int = 4) -> Path:
    root = tmp_path / "bundle"
    (root / "derived").mkdir(parents=True)
    (root / "source").mkdir(parents=True)
    (root / "derived" / "timeline.machine.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "canonical_ppq": CANONICAL_PPQ,
                "measures": [
                    {
                        "measure_index": i,
                        "measure_label": str(i + 1),
                        "start_tick": i * BAR,
                        "end_tick": (i + 1) * BAR,
                    }
                    for i in range(measures)
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def _write_display_map(root: Path, boxes: list[dict]) -> None:
    (root / "derived" / "display_map.machine.json").write_text(
        json.dumps({"boxes": boxes}), encoding="utf-8"
    )


def _box(label: str, start: int, end: int) -> dict:
    return {"measure_label": label, "score_start_tick": start, "score_end_tick": end}


def _write_mxl(root: Path, measures: int) -> None:
    part = "".join(f'<measure number="{i + 1}"/>' for i in range(measures))
    xml = f'<?xml version="1.0"?><score-partwise><part id="P1">{part}</part></score-partwise>'
    with zipfile.ZipFile(root / "source" / "omr.mxl", "w") as archive:
        archive.writestr("score.xml", xml)


def test_agreeing_bundle_reports_nothing(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write_display_map(root, [_box(str(i + 1), i * BAR, (i + 1) * BAR) for i in range(4)])
    _write_mxl(root, 4)

    assert check_score_bundle(root) == []


def test_note_pass_measure_count_disagreement_is_an_error(tmp_path: Path) -> None:
    """The Chopin mvt II failure: 113 measures in one pass, 126 in the other."""

    root = _bundle(tmp_path, measures=4)
    _write_display_map(root, [_box(str(i + 1), i * BAR, (i + 1) * BAR) for i in range(4)])
    _write_mxl(root, 3)

    findings = check_score_bundle(root)
    finding = next(f for f in findings if f.code == "omr_pass_measure_count_disagreement")
    assert finding.severity == "error"
    assert "3 measures" in finding.message and "4" in finding.message


def test_display_box_pointing_at_another_measure_is_an_error(tmp_path: Path) -> None:
    """Geometry joined to the wrong ticks draws the cursor in the wrong bar."""

    root = _bundle(tmp_path)
    boxes = [_box(str(i + 1), i * BAR, (i + 1) * BAR) for i in range(4)]
    boxes[2] = _box("3", 3 * BAR, 4 * BAR)  # m.3's box claims m.4's ticks
    _write_display_map(root, boxes)
    _write_mxl(root, 4)

    finding = next(f for f in check_score_bundle(root) if f.code == "display_map_tick_mismatch")
    assert finding.severity == "error"
    assert finding.measures == ("3",)


def test_undrawn_measure_is_a_warning(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write_display_map(root, [_box(str(i + 1), i * BAR, (i + 1) * BAR) for i in range(3)])
    _write_mxl(root, 4)

    finding = next(f for f in check_score_bundle(root) if f.code == "display_map_missing_measure")
    assert finding.severity == "warning"
    assert finding.measures == ("4",)


def test_interpolated_measures_are_flagged_for_human_review(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write_display_map(root, [_box(str(i + 1), i * BAR, (i + 1) * BAR) for i in range(4)])
    (root / "derived" / "performance_beat_map.machine.json").write_text(
        json.dumps(
            {
                "unmapped_measure_labels": ["4"],
                "anchors": [
                    {
                        "measure_label": "1",
                        "confidence": 0.92,
                        "evidence": [{"kind": "audiveris_solo_match", "matched_notes": 20}],
                    },
                    {
                        "measure_label": "2",
                        "confidence": 0.36,
                        "evidence": [{"kind": "structural_interpolation"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    findings = {f.code: f for f in check_score_bundle(root)}
    assert findings["beat_map_weak_measures"].measures == ("2",)
    assert findings["beat_map_unmapped_measures"].measures == ("4",)
    assert findings["beat_map_weak_measures"].severity == "warning"


def test_missing_artifacts_are_not_an_error(tmp_path: Path) -> None:
    """A bundle without optional derived artifacts is silent, not failing."""

    root = _bundle(tmp_path)
    assert check_score_bundle(root) == []


def test_musicxml_measure_count_tolerates_unreadable_input(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mxl"
    broken.write_bytes(b"not a zip")
    assert musicxml_measure_count(broken) is None
    assert musicxml_measure_count(tmp_path / "absent.mxl") is None


def _geo_box(label: str, page: int, system: int, x0: float, x1: float) -> dict:
    return {
        "measure_label": label,
        "page": page,
        "system": system,
        "x0": x0,
        "x1": x1,
        "score_start_tick": (int(label) - 1) * BAR,
        "score_end_tick": int(label) * BAR,
    }


def test_unboxed_music_at_a_system_end_is_an_error(tmp_path: Path) -> None:
    """A missed end-of-system barline drops a bar and silently shifts the rest.

    Joseffy p8 system 3: Audiveris found the barline that *opens* the fourth bar
    (x=0.775) but not the one closing the system (x=0.931), so the bar had a
    start and no end and was never boxed. Every later measure number was one
    low, and no artifact-vs-artifact comparison could see it -- the display map
    faithfully reproduced the wrong answer. Only the geometry shows it: that
    system's boxes stop at 0.776 where every other system reaches 0.931.
    """

    root = _bundle(tmp_path, measures=14)
    boxes = []
    label = 1
    for system in range(1, 8):  # enough systems to establish a typical width
        for i in range(2):
            x0 = 0.09 + i * 0.42
            boxes.append(_geo_box(str(label), 1, system, x0, x0 + 0.42))
            label += 1
    # the last system loses its final bar: boxes stop well short of the edge
    boxes = [b for b in boxes if not (b["system"] == 7 and b["measure_label"] == "14")]
    _write_display_map(root, boxes)

    finding = next(
        f for f in check_score_bundle(root) if f.code == "display_map_unboxed_music"
    )
    assert finding.severity == "error"
    assert any("p1s7" in m for m in finding.measures)


def test_full_width_systems_report_nothing(tmp_path: Path) -> None:
    root = _bundle(tmp_path, measures=14)
    boxes = []
    label = 1
    for system in range(1, 8):
        for i in range(2):
            x0 = 0.09 + i * 0.42
            boxes.append(_geo_box(str(label), 1, system, x0, x0 + 0.42))
            label += 1
    _write_display_map(root, boxes)

    assert not [
        f for f in check_score_bundle(root) if f.code == "display_map_unboxed_music"
    ]


def test_beat_geometry_out_of_box_is_flagged(tmp_path: Path) -> None:
    """A beat whose page-x escapes its measure box means a stale beat map.

    This is the check that would have caught the mispositioned cursor before a
    rehearsal: the beat map's pdf_x went stale relative to a corrected display
    map, so beats landed over neighbouring bars.
    """

    root = _bundle(tmp_path)
    # three boxes across the page, each carrying measure_index + normalized x.
    (root / "derived" / "display_map.machine.json").write_text(
        json.dumps(
            {
                "boxes": [
                    {"measure_index": 0, "measure_label": "1", "x0": 0.0, "x1": 0.33},
                    {"measure_index": 1, "measure_label": "2", "x0": 0.33, "x1": 0.66},
                    {"measure_index": 2, "measure_label": "3", "x0": 0.66, "x1": 1.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "derived" / "performance_beat_map.machine.json").write_text(
        json.dumps(
            {
                "anchors": [
                    # measure 1's box is [0, 0.33]; this beat sits in measure 3's span.
                    {"measure_index": 0, "measure_label": "1",
                     "beat_in_measure": 1.0, "pdf_x": 0.80, "confidence": 0.9},
                    # measure 2's beat correctly inside [0.33, 0.66].
                    {"measure_index": 1, "measure_label": "2",
                     "beat_in_measure": 1.0, "pdf_x": 0.50, "confidence": 0.9},
                ]
            }
        ),
        encoding="utf-8",
    )
    findings = {f.code: f for f in check_score_bundle(root)}
    assert "beat_map_geometry_out_of_box" in findings
    assert findings["beat_map_geometry_out_of_box"].severity == "error"
    assert "1" in findings["beat_map_geometry_out_of_box"].measures
    assert "2" not in findings["beat_map_geometry_out_of_box"].measures
