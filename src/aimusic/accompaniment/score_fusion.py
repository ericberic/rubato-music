"""Audiveris/MIDI evidence fusion for the canonical rehearsal timeline.

Audiveris supplies semantic score positions and engraving coordinates; it does
not align performances.  This module keeps that boundary explicit and fuses
recognized score notes, a reference MIDI performance, reviewed anchors, and
optional human corrections into beat-level source correspondences.

The runtime consumes the resulting :class:`PerformanceBeatMapDocument`; it
never reruns OMR or sequence alignment while the performer is playing.
"""

from __future__ import annotations

import statistics
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Literal

import mido
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aimusic.accompaniment.offline_alignment import MidiNoteEvent, align_note_events


class FusionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceKind(StrEnum):
    REVIEWED_DOWNBEAT = "reviewed_downbeat"
    HUMAN_CORRECTION = "human_correction"
    AUDIVERIS_SOLO_MATCH = "audiveris_solo_match"
    AUDIVERIS_REDUCTION_MATCH = "audiveris_reduction_match"
    STRUCTURAL_INTERPOLATION = "structural_interpolation"


class BeatEvidence(FusionModel):
    kind: EvidenceKind
    confidence: float = Field(ge=0.0, le=1.0)
    matched_notes: int = Field(default=0, ge=0)
    details: str | None = None


class PerformanceBeatAnchor(FusionModel):
    """One reference-MIDI position joined to one canonical score beat."""

    score_tick: int = Field(ge=0)
    measure_index: int = Field(ge=0)
    measure_label: str = Field(min_length=1)
    beat_in_measure: float = Field(ge=0.0)
    source_midi_tick: int = Field(ge=0)
    source_seconds: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[BeatEvidence, ...] = ()
    pdf_page: int | None = Field(default=None, ge=1)
    pdf_system: int | None = Field(default=None, ge=1)
    # PAGE-NORMALIZED horizontal position of this beat: a fraction of the page
    # width in [0, 1], origin at the page's left edge. Produced by
    # beat_geometry.infer_measure_beats, which picks the clearest staff and
    # projects its note fractions into the display box's page span. Display-only;
    # timing uses score_tick / source_midi_tick. See
    # docs/concepts/score-coordinate-systems.md.
    pdf_x: float | None = Field(default=None, ge=0.0, le=1.0)
    # How well the beat's page-x was pinned by note geometry, in [0, 1]. 1.0 is a
    # clean staff with a note on every beat; low values fell back to even
    # spacing. Lets weak placements be flagged rather than silently trusted.
    pdf_x_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # Which staff's notes located this measure's beats ("<part>:<staff>"), or
    # None when no staff was clean enough and even spacing was used.
    pdf_x_staff: str | None = Field(default=None)
    # True if a real note sat on this beat; False if it was interpolated.
    pdf_x_anchored: bool | None = Field(default=None)


class PerformanceBeatMapDocument(FusionModel):
    """Offline-built beat correspondence used by projection and diagnostics."""

    schema_version: Literal[1] = 1
    mapping_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_midi_ppq: int = Field(gt=0)
    canonical_ppq: int = Field(gt=0)
    review_state: Literal["machine", "reviewed"] = "machine"
    anchors: tuple[PerformanceBeatAnchor, ...] = Field(min_length=2)
    unmapped_measure_labels: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_monotonic_anchors(self) -> "PerformanceBeatMapDocument":
        for left, right in zip(self.anchors, self.anchors[1:]):
            if right.score_tick <= left.score_tick:
                raise ValueError("beat-map score ticks must increase strictly")
            if right.source_midi_tick <= left.source_midi_tick:
                raise ValueError("beat-map source MIDI ticks must increase strictly")
        return self


class HumanBeatCorrection(FusionModel):
    """Sparse performer correction captured by the in-app workflow."""

    correction_id: str = Field(min_length=1)
    source_midi_tick: int = Field(ge=0)
    score_tick: int = Field(ge=0)
    measure_label: str = Field(min_length=1)
    beat_in_measure: float = Field(ge=0.0)
    created_at: datetime
    note: str | None = None


class HumanCorrectionDocument(FusionModel):
    schema_version: Literal[1] = 1
    piece_id: str = Field(min_length=1)
    movement: int = Field(ge=1)
    timeline_id: str = Field(min_length=1)
    corrections: tuple[HumanBeatCorrection, ...] = ()


@dataclass(frozen=True)
class RecognizedNote:
    """A note head from the Audiveris *note* pass (the recognition MusicXML).

    Coordinate frame -- see docs/concepts/score-coordinate-systems.md:

    - ``default_x``/``measure_width`` are MEASURE-LOCAL, in MusicXML *tenths*
      (staff-space units, ~10 per interline gap), with the origin at the
      measure's left barline. ``default_x`` is therefore in ``[0, measure_width]``
      and their ratio is a unit-free fraction across the measure. This is a
      different frame from the display boxes, which are page-normalized; the two
      are composed in ``beat_pdf_x``.
    - ``onset_beats`` is rhythmic position within the measure (0-based), derived
      from note durations, NOT from geometry.
    """

    part_id: str
    measure: int
    onset_beats: float
    pitch: int
    default_x: float | None
    measure_width: float | None


@dataclass(frozen=True)
class ReferenceNote:
    source_tick: int
    seconds: float
    pitch: int


@dataclass(frozen=True)
class DownbeatEstimate:
    source_tick: int
    confidence: float
    evidence: BeatEvidence


def load_performance_beat_map(path: Path | str) -> PerformanceBeatMapDocument:
    return PerformanceBeatMapDocument.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def write_performance_beat_map(
    document: PerformanceBeatMapDocument, path: Path | str
) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(output, document.model_dump_json(indent=2) + "\n")
    return output


def read_audiveris_musicxml(path: Path | str) -> dict[str, tuple[RecognizedNote, ...]]:
    """Parse note onset, pitch, and horizontal geometry from MXL/MusicXML.

    The parser intentionally implements only the deterministic MusicXML cursor
    rules needed here (``backup``, ``forward``, ``chord`` and ``divisions``).
    It does not attempt to repair Audiveris rhythm; inconsistent measures are
    retained as lower-confidence evidence for the fusion stage.
    """

    root = _musicxml_root(Path(path))
    result: dict[str, tuple[RecognizedNote, ...]] = {}
    for part in root.findall("{*}part"):
        part_id = part.attrib.get("id", "")
        divisions = 1
        notes: list[RecognizedNote] = []
        for ordinal, measure in enumerate(part.findall("{*}measure"), start=1):
            try:
                measure_number = int(measure.attrib.get("number", ordinal))
            except ValueError:
                measure_number = ordinal
            measure_width = _optional_float(measure.attrib.get("width"))
            cursor = 0
            previous_onset = 0
            for child in measure:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "attributes":
                    value = child.findtext("{*}divisions")
                    if value:
                        divisions = max(1, int(value))
                elif tag == "backup":
                    cursor -= int(child.findtext("{*}duration") or 0)
                elif tag == "forward":
                    cursor += int(child.findtext("{*}duration") or 0)
                elif tag == "note":
                    duration = int(child.findtext("{*}duration") or 0)
                    is_chord = child.find("{*}chord") is not None
                    onset = previous_onset if is_chord else cursor
                    if not is_chord:
                        previous_onset = onset
                    pitch_element = child.find("{*}pitch")
                    if pitch_element is not None:
                        notes.append(
                            RecognizedNote(
                                part_id=part_id,
                                measure=measure_number,
                                onset_beats=onset / divisions,
                                pitch=_musicxml_pitch(pitch_element),
                                default_x=_optional_float(child.attrib.get("default-x")),
                                measure_width=measure_width,
                            )
                        )
                    if not is_chord:
                        cursor += duration
        result[part_id] = tuple(
            sorted(notes, key=lambda item: (item.measure, item.onset_beats, item.pitch))
        )
    return result


def read_reference_midi(
    path: Path | str,
    *,
    include_track_markers: tuple[str, ...] = (),
    exclude_track_markers: tuple[str, ...] = (),
) -> tuple[int, tuple[ReferenceNote, ...]]:
    """Read absolute source ticks/seconds from selected MIDI tracks."""

    midi = mido.MidiFile(path, clip=True)
    include = tuple(item.upper() for item in include_track_markers)
    exclude = tuple(item.upper() for item in exclude_track_markers)
    selected: list[tuple[int, int]] = []
    tempo_changes: list[tuple[int, int]] = [(0, 500_000)]
    for track in midi.tracks:
        name = next((msg.name for msg in track if msg.type == "track_name"), "")
        upper_name = name.upper()
        absolute_tick = 0
        for message in track:
            absolute_tick += message.time
            if message.type == "set_tempo":
                tempo_changes.append((absolute_tick, message.tempo))
            if message.type != "note_on" or message.velocity <= 0:
                continue
            if include and not any(marker in upper_name for marker in include):
                continue
            if exclude and any(marker in upper_name for marker in exclude):
                continue
            selected.append((absolute_tick, message.note))
    tempo_changes.sort()
    notes = tuple(
        ReferenceNote(tick, _seconds_at_tick(tick, midi.ticks_per_beat, tempo_changes), pitch)
        for tick, pitch in sorted(selected, key=lambda item: (item[0], item[1]))
    )
    return midi.ticks_per_beat, notes


def infer_symbolic_downbeat_candidates(
    recognized: Iterable[RecognizedNote],
    reference: tuple[ReferenceNote, ...],
    *,
    constraints: dict[int, DownbeatEstimate] | None = None,
    pitch_class: bool = False,
    evidence_kind: EvidenceKind = EvidenceKind.AUDIVERIS_SOLO_MATCH,
) -> dict[int, DownbeatEstimate]:
    """Infer downbeats from constraint-bounded symbolic sequence matches.

    Ordered pitches establish musical identity. Reference MIDI time remains
    only the coordinate attached to a matched note; it is never converted into
    an assumed number of measures. Sparse verified constraints split the
    alignment into monotonic windows so repeated passages cannot displace an
    entire later region.
    """

    score = tuple(
        sorted(
            recognized,
            key=lambda note: (note.measure, note.onset_beats, note.pitch),
        )
    )
    reference = tuple(sorted(reference, key=lambda note: (note.source_tick, note.pitch)))
    if not score or not reference:
        return {}
    ticks: dict[int, list[int]] = defaultdict(list)
    for score_window, reference_window in _symbolic_alignment_windows(
        score,
        reference,
        constraints or {},
    ):
        score_events = tuple(
            MidiNoteEvent(
                index=index,
                time_seconds=(note.measure - score_window[0].measure) * 4 + note.onset_beats,
                pitch=note.pitch % 12 if pitch_class else note.pitch,
                velocity=64,
                channel=0,
                track_index=0,
            )
            for index, note in enumerate(score_window)
        )
        reference_events = tuple(
            MidiNoteEvent(
                index=index,
                time_seconds=note.seconds,
                pitch=note.pitch % 12 if pitch_class else note.pitch,
                velocity=64,
                channel=0,
                track_index=0,
            )
            for index, note in enumerate(reference_window)
        )
        alignment = align_note_events(
            score_events,
            reference_events,
            mismatch_score=-3.0 if pitch_class else -2.0,
            gap_score=-1.2 if pitch_class else -1.5,
        )
        for pair in alignment.matched_pairs:
            note = score_window[pair.reference_index]
            if abs(note.onset_beats) <= 0.05:
                ticks[note.measure].append(reference_window[pair.performance_index].source_tick)
    candidates: dict[int, DownbeatEstimate] = {}
    for measure, values in ticks.items():
        source_tick = round(statistics.median(values))
        support = len(values)
        confidence = min(0.84, 0.48 + 0.08 * support)
        candidates[measure] = DownbeatEstimate(
            source_tick=source_tick,
            confidence=confidence,
            evidence=BeatEvidence(
                kind=evidence_kind,
                confidence=confidence,
                matched_notes=support,
                details=(
                    "median of exact-pitch downbeat matches on a monotonic, "
                    "constraint-bounded symbolic sequence path"
                ),
            ),
        )
    return candidates


def resolve_downbeat_path(
    *,
    first_measure: int,
    last_measure: int,
    verified_constraints: dict[int, DownbeatEstimate],
    symbolic_candidates: Iterable[dict[int, DownbeatEstimate]],
) -> dict[int, DownbeatEstimate]:
    """Resolve a monotonic symbolic path, then interpolate explicit gaps.

    Verified performer/score landmarks are hard constraints. Symbolic
    candidates own every other musical location. Source-time distance is not
    used to decide how many measures elapsed: a candidate is rejected only if
    it violates the monotonic order established by the constraints.
    """

    merged = dict(verified_constraints)
    _validate_constraint_order(merged)
    candidates_by_measure: dict[int, DownbeatEstimate] = {}
    for source in symbolic_candidates:
        for measure, candidate in source.items():
            if not first_measure <= measure <= last_measure:
                continue
            existing = candidates_by_measure.get(measure)
            if existing is None or (
                candidate.confidence,
                candidate.evidence.matched_notes,
            ) > (existing.confidence, existing.evidence.matched_notes):
                candidates_by_measure[measure] = candidate

    for measure, candidate in sorted(candidates_by_measure.items()):
        if measure in merged:
            continue
        accepted_measures = sorted(merged)
        left = max((item for item in accepted_measures if item < measure), default=None)
        right = min((item for item in accepted_measures if item > measure), default=None)
        if left is not None and candidate.source_tick <= merged[left].source_tick:
            continue
        if right is not None and candidate.source_tick >= merged[right].source_tick:
            continue
        merged[measure] = candidate

    known = sorted(merged)
    for measure in range(first_measure, last_measure + 1):
        if measure in merged:
            continue
        left = max((item for item in known if item < measure), default=None)
        right = min((item for item in known if item > measure), default=None)
        if left is None or right is None:
            continue
        ratio = (measure - left) / (right - left)
        source_tick = round(
            merged[left].source_tick
            + ratio * (merged[right].source_tick - merged[left].source_tick)
        )
        confidence = max(0.25, min(merged[left].confidence, merged[right].confidence) * 0.65)
        merged[measure] = DownbeatEstimate(
            source_tick=source_tick,
            confidence=confidence,
            evidence=BeatEvidence(
                kind=EvidenceKind.STRUCTURAL_INTERPOLATION,
                confidence=confidence,
                details=f"interpolated between measures {left} and {right}",
            ),
        )
        known.append(measure)
        known.sort()
    return dict(sorted(merged.items()))


def _symbolic_alignment_windows(
    score: tuple[RecognizedNote, ...],
    reference: tuple[ReferenceNote, ...],
    constraints: dict[int, DownbeatEstimate],
) -> tuple[tuple[tuple[RecognizedNote, ...], tuple[ReferenceNote, ...]], ...]:
    first_measure = min(note.measure for note in score)
    final_measure = max(note.measure for note in score)
    first_source_tick = min(note.source_tick for note in reference)
    final_source_tick = max(note.source_tick for note in reference) + 1
    bounds = sorted(
        (measure, estimate.source_tick)
        for measure, estimate in constraints.items()
        if first_measure <= measure <= final_measure + 1
    )
    if not bounds or bounds[0][0] > first_measure:
        bounds.insert(0, (first_measure, first_source_tick))
    if bounds[-1][0] <= final_measure:
        bounds.append((final_measure + 1, final_source_tick))
    for left, right in zip(bounds, bounds[1:]):
        if right[0] <= left[0] or right[1] <= left[1]:
            raise ValueError("symbolic alignment constraints must increase monotonically")

    windows = []
    for (start_measure, start_tick), (end_measure, end_tick) in zip(bounds, bounds[1:]):
        score_window = tuple(note for note in score if start_measure <= note.measure < end_measure)
        reference_window = tuple(
            note for note in reference if start_tick <= note.source_tick < end_tick
        )
        if score_window and reference_window:
            windows.append((score_window, reference_window))
    return tuple(windows)


def _validate_constraint_order(constraints: dict[int, DownbeatEstimate]) -> None:
    ordered = sorted(constraints.items())
    for (left_measure, left), (right_measure, right) in zip(ordered, ordered[1:]):
        if right_measure <= left_measure or right.source_tick <= left.source_tick:
            raise ValueError("verified downbeat constraints must increase monotonically")


def build_measure_beat_ticks(
    recognized: Iterable[RecognizedNote],
    reference: tuple[ReferenceNote, ...],
    *,
    start_tick: int,
    end_tick: int,
    evidence_kind: EvidenceKind,
    pitch_class: bool = False,
) -> tuple[dict[int, int], tuple[BeatEvidence, ...]]:
    """Estimate source MIDI ticks for beats 0..4 inside one measure."""

    score = tuple(note for note in recognized if -0.05 <= note.onset_beats <= 4.05)
    performance = tuple(note for note in reference if start_tick <= note.source_tick < end_tick)
    if not score or not performance:
        return _linear_measure_ticks(start_tick, end_tick), (
            BeatEvidence(
                kind=EvidenceKind.STRUCTURAL_INTERPOLATION,
                confidence=0.3,
                details="no usable symbolic matches in this measure",
            ),
        )

    score_events = tuple(
        MidiNoteEvent(
            index=index,
            time_seconds=note.onset_beats,
            pitch=note.pitch % 12 if pitch_class else note.pitch,
            velocity=64,
            channel=0,
            track_index=0,
        )
        for index, note in enumerate(score)
    )
    performance_events = tuple(
        MidiNoteEvent(
            index=index,
            time_seconds=note.seconds,
            pitch=note.pitch % 12 if pitch_class else note.pitch,
            velocity=64,
            channel=0,
            track_index=0,
        )
        for index, note in enumerate(performance)
    )
    alignment = align_note_events(
        score_events,
        performance_events,
        mismatch_score=-3.0 if pitch_class else -2.0,
        gap_score=-1.2 if pitch_class else -1.5,
    )
    matched_ticks: dict[float, list[int]] = defaultdict(list)
    for pair in alignment.matched_pairs:
        matched_ticks[score[pair.reference_index].onset_beats].append(
            performance[pair.performance_index].source_tick
        )
    knots = [(0.0, start_tick)]
    knots.extend(
        (onset, round(statistics.median(values)))
        for onset, values in sorted(matched_ticks.items())
        if 0.0 < onset < 4.0
    )
    knots.append((4.0, end_tick))
    knots = _monotonic_knots(knots)
    ticks = {beat: round(_interpolate_knots(float(beat), knots)) for beat in range(5)}
    support = len(alignment.matched_pairs)
    confidence = min(0.92, 0.45 + 0.04 * support)
    intervals = [ticks[beat + 1] - ticks[beat] for beat in range(4)]
    measure_span = end_tick - start_tick
    # Exact-pitch dynamic programming can still pair several score beats with
    # one dense ornament or rolled chord. Such a match is useful evidence for
    # the measure, but not a credible beat warp: a near-zero beat followed by
    # almost the entire bar would make the cursor race and then stall. Keep
    # broad expressive timing (m.17's longest beat is about 55% of its bar),
    # while rejecting collapsed paths and falling back to trusted downbeats.
    if measure_span <= 0 or any(
        interval / measure_span < 0.05 or interval / measure_span > 0.65
        for interval in intervals
    ):
        return _linear_measure_ticks(start_tick, end_tick), (
            BeatEvidence(
                kind=EvidenceKind.STRUCTURAL_INTERPOLATION,
                confidence=0.35,
                matched_notes=support,
                details=(
                    "rejected an implausible collapsed symbolic beat warp; "
                    "interpolated between trusted measure downbeats"
                ),
            ),
        )
    return ticks, (
        BeatEvidence(
            kind=evidence_kind,
            confidence=confidence,
            matched_notes=support,
            details=f"{support} exact symbolic note matches inside the downbeat window",
        ),
    )


def beat_pdf_x(
    notes: Iterable[RecognizedNote],
    beat: int,
    *,
    box_x0: float,
    box_x1: float,
) -> float:
    """Project a canonical beat to a page-normalized x using note geometry.

    Composes two coordinate frames (docs/concepts/score-coordinate-systems.md):
    each note's measure-local fraction ``default_x / measure_width`` in [0, 1] is
    mapped into the display box's page-normalized span ``[box_x0, box_x1]``.
    Notes are grouped by rhythmic onset and a beat's x is the median x of the
    notes that sound on it; beats with no note are interpolated between those
    that do. The output is page-normalized and clamped to ``[box_x0, box_x1]``.

    Correctness depends on ``notes`` being the SAME measure as ``box_x0/box_x1``.
    They are joined by measure number upstream; a mismatch (e.g. a stale display
    map) stretches the right note fraction across the wrong box, which reads as
    the cursor bouncing. The renderer defends against it, but the join must hold.
    """

    points: dict[float, list[float]] = defaultdict(list)
    for note in notes:
        if (
            note.default_x is None
            or note.measure_width is None
            or note.measure_width <= 0
            or not -0.05 <= note.onset_beats <= 4.05
        ):
            continue
        normalized = box_x0 + (note.default_x / note.measure_width) * (box_x1 - box_x0)
        points[note.onset_beats].append(max(box_x0, min(box_x1, normalized)))
    knots = [(0.0, box_x0)]
    knots.extend(
        (onset, statistics.median(values))
        for onset, values in sorted(points.items())
        if 0.0 <= onset < 4.0
    )
    knots.append((4.0, box_x1))
    knots = _monotonic_knots(knots)
    return max(box_x0, min(box_x1, _interpolate_knots(float(beat), knots)))


def apply_human_corrections(
    document: PerformanceBeatMapDocument,
    corrections: HumanCorrectionDocument | None,
) -> tuple[tuple[int, int, float], ...]:
    """Return compatibility projection anchors with corrections overriding beats.

    Source ticks are scaled to canonical PPQ because the current take aligner
    persists reference MIDI positions in that compatibility unit.  Canonical
    score ticks remain the target coordinate.
    """

    scale = document.canonical_ppq / document.source_midi_ppq
    by_score_tick = {
        anchor.score_tick: (
            round(anchor.source_midi_tick * scale),
            anchor.score_tick,
            anchor.confidence,
        )
        for anchor in document.anchors
    }
    if corrections and corrections.timeline_id == document.timeline_id:
        for correction in corrections.corrections:
            by_score_tick[correction.score_tick] = (
                round(correction.source_midi_tick * scale),
                correction.score_tick,
                1.0,
            )
    anchors = tuple(by_score_tick[key] for key in sorted(by_score_tick))
    for left, right in zip(anchors, anchors[1:]):
        if right[0] <= left[0]:
            raise ValueError("human correction makes the source beat map non-monotonic")
    return anchors


def source_midi_tick_at_seconds(
    document: PerformanceBeatMapDocument, source_seconds: float
) -> int:
    """Interpolate source MIDI ticks on the map's tempo-aware second axis."""

    anchors = document.anchors
    if source_seconds <= anchors[0].source_seconds:
        return anchors[0].source_midi_tick
    for left, right in zip(anchors, anchors[1:]):
        if source_seconds > right.source_seconds:
            continue
        span = right.source_seconds - left.source_seconds
        ratio = 0.0 if span <= 0 else (source_seconds - left.source_seconds) / span
        return round(
            left.source_midi_tick
            + ratio * (right.source_midi_tick - left.source_midi_tick)
        )
    return anchors[-1].source_midi_tick


def load_human_corrections(path: Path | str) -> HumanCorrectionDocument | None:
    source = Path(path)
    if not source.exists():
        return None
    return HumanCorrectionDocument.model_validate_json(source.read_text(encoding="utf-8"))


def write_human_corrections(document: HumanCorrectionDocument, path: Path | str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(output, document.model_dump_json(indent=2) + "\n")
    return output


def _linear_measure_ticks(start_tick: int, end_tick: int) -> dict[int, int]:
    return {beat: round(start_tick + beat / 4 * (end_tick - start_tick)) for beat in range(5)}


def _monotonic_knots(knots: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    grouped: dict[float, list[float]] = defaultdict(list)
    for x, y in knots:
        grouped[float(x)].append(float(y))
    result: list[tuple[float, float]] = []
    last_y = float("-inf")
    for x, values in sorted(grouped.items()):
        y = max(last_y, statistics.median(values))
        result.append((x, y))
        last_y = y
    return result


def _interpolate_knots(value: float, knots: list[tuple[float, float]]) -> float:
    if value <= knots[0][0]:
        return knots[0][1]
    for left, right in zip(knots, knots[1:]):
        if value <= right[0]:
            span = right[0] - left[0]
            if span <= 0:
                return right[1]
            ratio = (value - left[0]) / span
            return left[1] + ratio * (right[1] - left[1])
    return knots[-1][1]


def _musicxml_root(path: Path) -> ET.Element:
    if path.suffix.lower() in {".mxl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            member = next(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".xml") and not name.startswith("META-INF/")
                ),
                None,
            )
            if member is None:
                raise ValueError(f"No MusicXML document found in {path}")
            return ET.fromstring(archive.read(member))
    return ET.parse(path).getroot()


def _musicxml_pitch(pitch: ET.Element) -> int:
    step = pitch.findtext("{*}step")
    octave = pitch.findtext("{*}octave")
    if step is None or octave is None:
        raise ValueError("MusicXML pitch requires step and octave")
    semitones = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    return 12 * (int(octave) + 1) + semitones[step] + int(pitch.findtext("{*}alter") or 0)


def _seconds_at_tick(
    target_tick: int, ppq: int, tempo_changes: list[tuple[int, int]]
) -> float:
    seconds = 0.0
    previous_tick = 0
    tempo = tempo_changes[0][1]
    for tick, next_tempo in tempo_changes[1:]:
        if tick > target_tick:
            break
        seconds += mido.tick2second(tick - previous_tick, ppq, tempo)
        previous_tick = tick
        tempo = next_tempo
    return seconds + mido.tick2second(target_tick - previous_tick, ppq, tempo)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _atomic_text(path: Path, contents: str) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(contents)
        temp_path.replace(path)
    except BaseException:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise


__all__ = [
    "BeatEvidence",
    "DownbeatEstimate",
    "EvidenceKind",
    "HumanBeatCorrection",
    "HumanCorrectionDocument",
    "PerformanceBeatAnchor",
    "PerformanceBeatMapDocument",
    "RecognizedNote",
    "ReferenceNote",
    "apply_human_corrections",
    "beat_pdf_x",
    "build_measure_beat_ticks",
    "infer_symbolic_downbeat_candidates",
    "load_human_corrections",
    "load_performance_beat_map",
    "read_audiveris_musicxml",
    "read_reference_midi",
    "resolve_downbeat_path",
    "source_midi_tick_at_seconds",
    "write_human_corrections",
    "write_performance_beat_map",
]
