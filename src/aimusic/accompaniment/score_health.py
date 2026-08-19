"""Cross-artifact consistency checks for a score bundle's derived coordinates.

A bundle carries several independently-derived views of the same music: the
canonical timeline (measure grid), the display map (PDF geometry joined to
canonical ticks), the performance beat map (canonical <-> reference warp), and
the OMR's own note-level MusicXML. Each is internally consistent, and each is
validated on load. Nothing checked that they agreed *with each other*.

That gap is not hypothetical. In the Chopin Op. 11 mvt II bundle the OMR's
note pass reports 113 measures while the layout pass and timeline report 126 --
a 13-measure disagreement concentrated in the mvt II cadenza, where the solo is
engraved unbarred and only the accompaniment staff carries barlines. The
performer saw correct-looking measure labels on a correctly-drawn page while the
runtime worked in a measure grid that had drifted from it by two bars, and no
surface could report the disagreement.

These checks are read-only and cheap. They do not decide which artifact is
right -- that needs a human reading the score -- they only refuse to let two
derived views disagree silently.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

# A measure whose warp anchors are all interpolated or weakly matched cannot
# support a position claim; the runtime may still be right there, but it is
# right by inheritance from its neighbours rather than by evidence.
WEAK_ANCHOR_CONFIDENCE = 0.5

# Fraction of page width a system's boxes may fall short before it counts as
# un-boxed music rather than ordinary engraving slack.
SYSTEM_COVERAGE_TOLERANCE = 0.03

# The check compares each system against the median of the others, so it needs
# enough systems for that median to mean anything.
MIN_SYSTEMS_FOR_COVERAGE = 6


@dataclass(frozen=True)
class ScoreHealthFinding:
    """One disagreement between derived views of the same bundle."""

    code: str
    severity: str  # "error" | "warning"
    message: str
    measures: tuple[str, ...] = ()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def musicxml_measure_count(path: Path) -> int | None:
    """Measure count from the OMR's note-level export, or ``None`` if absent.

    Reads the first part only: MusicXML parts are barred together, so any part
    answers the question and the first is cheapest.
    """

    if not path.is_file():
        return None
    try:
        if path.suffix == ".mxl":
            with zipfile.ZipFile(path) as archive:
                names = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".xml") and "META-INF" not in name
                ]
                if not names:
                    return None
                root = ElementTree.fromstring(archive.read(names[0]))
        else:
            root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, zipfile.BadZipFile, KeyError):
        return None
    part = root.find("part")
    if part is None:
        return None
    return len(part.findall("measure"))


def check_score_bundle(root: Path) -> list[ScoreHealthFinding]:
    """Run every cross-artifact check available for this bundle."""

    derived = root / "derived"
    findings: list[ScoreHealthFinding] = []
    timeline_path = derived / "timeline.machine.json"
    if not timeline_path.is_file():
        return findings
    timeline = _read_json(timeline_path)
    measures = timeline.get("measures", [])
    by_label = {str(m["measure_label"]): m for m in measures}

    findings.extend(_check_note_pass_agreement(root, len(measures)))
    findings.extend(_check_display_map(derived, by_label))
    findings.extend(_check_system_coverage(derived))
    findings.extend(_check_beat_map(derived, by_label))
    findings.extend(_check_beat_geometry(derived))
    return findings


def _check_beat_geometry(derived: Path) -> list[ScoreHealthFinding]:
    """Every beat's page-x must lie inside its own measure box.

    The beat map's ``pdf_x`` is computed against the display boxes; if the beat
    map is not rebuilt after the display map changes, its geometry goes stale and
    the beats land over neighbouring bars. That makes the score cursor jump even
    though the follower is smooth. This flags exactly that -- the check that would
    have caught a whole run of mispositioned measures before a rehearsal did.
    See docs/concepts/score-coordinate-systems.md.
    """

    beat_path = derived / "performance_beat_map.machine.json"
    display_path = derived / "display_map.machine.json"
    if not beat_path.is_file() or not display_path.is_file():
        return []
    boxes = {
        box["measure_index"]: box
        for box in _read_json(display_path).get("boxes", [])
        if box.get("measure_index") is not None and "x0" in box and "x1" in box
    }
    misplaced: list[str] = []
    by_index: dict[int, list[float]] = {}
    for anchor in _read_json(beat_path).get("anchors", []):
        x = anchor.get("pdf_x")
        index = anchor.get("measure_index")
        if (
            x is not None
            and index is not None
            and 0.0 < float(anchor.get("beat_in_measure", 0.0)) < 4.0
        ):
            by_index.setdefault(int(index), []).append(float(x))
    for measure_index, xs in by_index.items():
        box = boxes.get(measure_index)
        if box is None:
            continue
        if min(xs) < box["x0"] - 1e-6 or max(xs) > box["x1"] + 1e-6:
            misplaced.append(str(box.get("measure_label", measure_index + 1)))
    if not misplaced:
        return []
    return [
        ScoreHealthFinding(
            code="beat_map_geometry_out_of_box",
            severity="error",
            message=(
                "Beat positions fall outside their own measure boxes, so the score "
                "cursor is misplaced on these bars. The beat map's page geometry is "
                "stale relative to the display map -- rebuild it (scripts/"
                "build_movement2_beat_map.py) so it picks up the corrected boxes."
            ),
            measures=tuple(sorted(misplaced, key=_label_key)),
        )
    ]


def _check_note_pass_agreement(root: Path, timeline_measures: int) -> list[ScoreHealthFinding]:
    """The OMR's note pass and layout pass must count the same bars."""

    sources = sorted((root / "source").glob("*.mxl")) + sorted(
        (root / "source").glob("*.musicxml")
    )
    for source in sources:
        count = musicxml_measure_count(source)
        if count is None or count == timeline_measures:
            continue
        return [
            ScoreHealthFinding(
                code="omr_pass_measure_count_disagreement",
                severity="error",
                message=(
                    f"{source.name} reports {count} measures but the canonical timeline "
                    f"reports {timeline_measures}. The two OMR passes disagree about the "
                    "measure grid, so displayed measure numbers and runtime score "
                    "positions can refer to different bars. A human must decide which "
                    "matches the printed score."
                ),
            )
        ]
    return []


def _check_display_map(derived: Path, by_label: dict[str, dict]) -> list[ScoreHealthFinding]:
    """Display geometry must be joined to the same ticks the timeline declares."""

    path = derived / "display_map.machine.json"
    if not path.is_file():
        return []
    boxes = _read_json(path).get("boxes", [])
    mismatched: list[str] = []
    missing: list[str] = []
    for box in boxes:
        label = str(box["measure_label"])
        measure = by_label.get(label)
        if measure is None:
            missing.append(label)
            continue
        if (
            box.get("score_start_tick") != measure["start_tick"]
            or box.get("score_end_tick") != measure["end_tick"]
        ):
            mismatched.append(label)
    findings: list[ScoreHealthFinding] = []
    if mismatched:
        findings.append(
            ScoreHealthFinding(
                code="display_map_tick_mismatch",
                severity="error",
                message=(
                    "Display boxes claim canonical ticks the timeline assigns to a "
                    "different measure. The cursor would be drawn in the wrong bar."
                ),
                measures=tuple(mismatched),
            )
        )
    if missing:
        findings.append(
            ScoreHealthFinding(
                code="display_map_unknown_measure",
                severity="error",
                message="Display boxes reference measures absent from the canonical timeline.",
                measures=tuple(missing),
            )
        )
    undrawn = sorted(set(by_label) - {str(b["measure_label"]) for b in boxes}, key=_label_key)
    if undrawn:
        findings.append(
            ScoreHealthFinding(
                code="display_map_missing_measure",
                severity="warning",
                message=(
                    "Canonical measures have no display box, so the cursor cannot be "
                    "drawn for them."
                ),
                measures=tuple(undrawn),
            )
        )
    return findings


def _check_system_coverage(derived: Path) -> list[ScoreHealthFinding]:
    """Every system's boxes must span it; un-boxed music means a missed barline.

    A system whose boxes stop short of the typical right edge has music no box
    covers -- the cursor can never be drawn there, and the measure count is
    silently one low for the rest of the piece. This is the cheapest possible
    check and it caught a bar that survived every content-based comparison:
    Joseffy p8 system 3 boxed only 3 of its 4 bars, ending at x=0.776 where
    every other system reaches 0.931.
    """

    path = derived / "display_map.machine.json"
    if not path.is_file():
        return []
    boxes = _read_json(path).get("boxes", [])
    if not boxes:
        return []
    by_system: dict[tuple[int, int], list[dict]] = {}
    for box in boxes:
        # Geometry is optional in the contract; a box without it simply cannot
        # be checked for coverage.
        if not all(k in box for k in ("page", "system", "x0", "x1")):
            continue
        by_system.setdefault((box["page"], box["system"]), []).append(box)
    if len(by_system) < MIN_SYSTEMS_FOR_COVERAGE:
        # Too few systems to establish what a full-width system looks like.
        return []
    rights = sorted(max(b["x1"] for b in bs) for bs in by_system.values())
    typical = rights[len(rights) // 2]
    short: list[str] = []
    for (page, system), bs in sorted(by_system.items()):
        ordered = sorted(bs, key=lambda b: b["x0"])
        if typical - max(b["x1"] for b in ordered) > SYSTEM_COVERAGE_TOLERANCE:
            short.append(f"p{page}s{system} (after m.{ordered[-1]['measure_label']})")
        for left, right in zip(ordered, ordered[1:]):
            if right["x0"] - left["x1"] > SYSTEM_COVERAGE_TOLERANCE:
                short.append(f"p{page}s{system} (gap after m.{left['measure_label']})")
    if not short:
        return []
    return [
        ScoreHealthFinding(
            code="display_map_unboxed_music",
            severity="error",
            message=(
                "Systems contain music no measure box covers, which means a barline "
                "was missed. The cursor cannot be drawn there and every later measure "
                "number is one low."
            ),
            measures=tuple(short),
        )
    ]

def _check_beat_map(derived: Path, by_label: dict[str, dict]) -> list[ScoreHealthFinding]:
    """Surface measures whose warp position rests on interpolation, not evidence."""

    path = derived / "performance_beat_map.machine.json"
    if not path.is_file():
        return []
    payload = _read_json(path)
    findings: list[ScoreHealthFinding] = []
    unmapped = [str(label) for label in payload.get("unmapped_measure_labels", [])]
    if unmapped:
        findings.append(
            ScoreHealthFinding(
                code="beat_map_unmapped_measures",
                severity="warning",
                message=(
                    "Measures have no reference-performance mapping, so the runtime has "
                    "no warp there and cannot follow against the reference."
                ),
                measures=tuple(unmapped),
            )
        )
    weak: list[str] = []
    by_measure: dict[str, list[dict]] = {}
    for anchor in payload.get("anchors", []):
        by_measure.setdefault(str(anchor["measure_label"]), []).append(anchor)
    for label, anchors in by_measure.items():
        if label not in by_label:
            continue
        interpolated = all(
            any(e.get("kind") == "structural_interpolation" for e in a.get("evidence", []))
            for a in anchors
        )
        below = max((a.get("confidence", 0.0) for a in anchors), default=0.0)
        if interpolated or below < WEAK_ANCHOR_CONFIDENCE:
            weak.append(label)
    if weak:
        findings.append(
            ScoreHealthFinding(
                code="beat_map_weak_measures",
                severity="warning",
                message=(
                    "Measures are positioned by interpolation or weak matches rather "
                    "than by matched notes. Prefer human review over re-running the "
                    "matcher for these bars."
                ),
                measures=tuple(sorted(weak, key=_label_key)),
            )
        )
    return findings


def _label_key(label: str) -> tuple[int, str]:
    """Sort measure labels numerically where possible, lexically otherwise."""

    try:
        return (int(label), "")
    except ValueError:
        return (10**9, label)
