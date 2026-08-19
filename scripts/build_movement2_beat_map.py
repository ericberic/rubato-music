#!/usr/bin/env python3
"""Build Movement II's Audiveris/reference-MIDI beat evidence artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

import mido

from aimusic.accompaniment.beat_geometry import infer_measure_beats, read_beat_glyphs
from aimusic.accompaniment.bundle_v2 import DisplayMappingDocument, TimelineDocument
from aimusic.accompaniment.rehearsal_position import (
    MOVEMENT_2_MEASURE_END_SOURCE_TICKS,
    MOVEMENT_2_VERIFIED_DOWNBEATS,
)
from aimusic.accompaniment.score_fusion import (
    BeatEvidence,
    DownbeatEstimate,
    EvidenceKind,
    PerformanceBeatAnchor,
    PerformanceBeatMapDocument,
    build_measure_beat_ticks,
    infer_symbolic_downbeat_candidates,
    read_audiveris_musicxml,
    read_reference_midi,
    resolve_downbeat_path,
    write_performance_beat_map,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("musicxml", type=Path)
    parser.add_argument("reference_midi", type=Path)
    parser.add_argument("timeline", type=Path)
    parser.add_argument("display_map", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    timeline = TimelineDocument.model_validate_json(args.timeline.read_text(encoding="utf-8"))
    display_map = DisplayMappingDocument.model_validate_json(
        args.display_map.read_text(encoding="utf-8")
    )
    if display_map.timeline_id != timeline.timeline_id:
        raise ValueError("display map and canonical timeline IDs must match")
    boxes = {box.measure_index: box for box in display_map.boxes}
    beat_glyphs = read_beat_glyphs(args.musicxml)
    parts = read_audiveris_musicxml(args.musicxml)
    piano_i = parts.get("P1", ())
    piano_ii = parts.get("P2", ())
    if not piano_i or not piano_ii:
        raise ValueError("Movement II recognition must contain Piano I (P1) and Piano II (P2)")

    source_ppq, solo = read_reference_midi(
        args.reference_midi, include_track_markers=("PIANO SOLO",)
    )
    orchestra_ppq, orchestra = read_reference_midi(
        args.reference_midi, exclude_track_markers=("PIANO SOLO",)
    )
    if source_ppq != orchestra_ppq:
        raise ValueError("solo and orchestra evidence must share one MIDI PPQ")

    verified: dict[int, DownbeatEstimate] = {}
    for anchor in MOVEMENT_2_VERIFIED_DOWNBEATS:
        source_tick = round(anchor.source_tick * source_ppq / timeline.canonical_ppq)
        verified[anchor.display_measure] = DownbeatEstimate(
            source_tick=source_tick,
            confidence=max(0.9, anchor.confidence),
            evidence=BeatEvidence(
                kind=EvidenceKind.REVIEWED_DOWNBEAT,
                confidence=max(0.9, anchor.confidence),
                details="performer-verified audible downbeat in the Joseffy engraving",
            ),
        )
    # The existing cue regression identifies m.54's downbeat independently of
    # the late solo pickup in m.53.
    verified[54] = DownbeatEstimate(
        source_tick=round(
            MOVEMENT_2_MEASURE_END_SOURCE_TICKS[53] * source_ppq / timeline.canonical_ppq
        ),
        confidence=0.9,
        evidence=BeatEvidence(
            kind=EvidenceKind.REVIEWED_DOWNBEAT,
            confidence=0.9,
            details="checked next-bar boundary used by the m.53 pickup cue",
        ),
    )

    opening_all = infer_symbolic_downbeat_candidates(
        tuple(note for note in piano_ii if note.measure <= 12),
        orchestra,
        constraints=verified,
        pitch_class=True,
        evidence_kind=EvidenceKind.AUDIVERIS_REDUCTION_MATCH,
    )
    later_all = infer_symbolic_downbeat_candidates(
        tuple(note for note in piano_i if note.measure >= 13),
        solo,
        constraints=verified,
        evidence_kind=EvidenceKind.AUDIVERIS_SOLO_MATCH,
    )
    later_reduction_all = infer_symbolic_downbeat_candidates(
        tuple(note for note in piano_ii if note.measure >= 13),
        orchestra,
        constraints=verified,
        pitch_class=True,
        evidence_kind=EvidenceKind.AUDIVERIS_REDUCTION_MATCH,
    )
    # Reduction/orchestra pitch classes are deliberately only an opening
    # bridge. Later solo measures have a much stronger exact-part signal and
    # must not be displaced by a coincidental orchestral chord-class match.
    opening = {measure: item for measure, item in opening_all.items() if measure <= 12}
    later = {measure: item for measure, item in later_all.items() if measure >= 13}
    # The solo path remains authoritative wherever it has a downbeat match. In
    # orchestra-led bars such as mm.104-105, however, Piano I is sparse or
    # silent and interpolating across the gap shifts the barline by a beat.
    # Piano II -> orchestra supplies an independent symbolic fallback only for
    # measures the stronger exact-part solo path could not identify.
    later_reduction = {
        measure: item
        for measure, item in later_reduction_all.items()
        if measure >= 13 and measure not in later
    }
    recognized_last_measure = min(
        max(note.measure for note in piano_i),
        len(timeline.measures),
    )
    downbeats = resolve_downbeat_path(
        first_measure=1,
        last_measure=recognized_last_measure,
        verified_constraints=verified,
        symbolic_candidates=(opening, later, later_reduction),
    )
    reduction_owned_measures = {
        measure
        for measure, estimate in downbeats.items()
        if measure >= 13 and estimate.evidence.kind == EvidenceKind.AUDIVERIS_REDUCTION_MATCH
    }
    # Eric verified that mm.35, 87, 93, 107, and 109 have the right musical
    # pulse but the wrong solo-derived interior clicks. Piano II spells the
    # orchestral quarter-note pulse he hears; in m.93 the separately reviewed
    # m.94 boundary also constrains the final interval. Promote these reviewed measures only after
    # independently checking that the reduction and resolved downbeats agree
    # within half a source quarter. Do not silently generalize the choice to
    # every coincident reduction match; those measures have not yet received
    # the same listening verdict, and m.54 demonstrates that displaced
    # reduction candidates exist.
    reviewed_reduction_rhythm_measures = {35, 87, 93, 107, 109}
    reduction_owned_measures.update(
        measure
        for measure, candidate in later_reduction_all.items()
        if measure in reviewed_reduction_rhythm_measures
        and measure in downbeats
        and abs(candidate.source_tick - downbeats[measure].source_tick) <= source_ppq // 2
    )

    notes_by_part_measure = {
        (part_id, measure): tuple(note for note in notes if note.measure == measure)
        for part_id, notes in (("P1", piano_i), ("P2", piano_ii))
        for measure in range(1, recognized_last_measure + 1)
    }
    anchors: list[PerformanceBeatAnchor] = []
    for measure_number in range(1, recognized_last_measure):
        start = downbeats.get(measure_number)
        end = downbeats.get(measure_number + 1)
        if start is None or end is None or end.source_tick <= start.source_tick:
            continue
        if measure_number <= 12 or measure_number in reduction_owned_measures:
            part_id = "P2"
            reference = orchestra
            evidence_kind = EvidenceKind.AUDIVERIS_REDUCTION_MATCH
            pitch_class = True
        else:
            part_id = "P1"
            reference = solo
            evidence_kind = EvidenceKind.AUDIVERIS_SOLO_MATCH
            pitch_class = False
        measure_notes = notes_by_part_measure[(part_id, measure_number)]
        beat_ticks, match_evidence = build_measure_beat_ticks(
            measure_notes,
            reference,
            start_tick=start.source_tick,
            end_tick=end.source_tick,
            evidence_kind=evidence_kind,
            pitch_class=pitch_class,
        )

        # Sparse, high-SNR musical landmarks. These are piece annotations, not
        # generalized rules: the builder can accept analogous corrections for
        # another movement without learning that every B-natural is a pickup.
        if measure_number == 12:
            beat_ticks[3] = 34_619
        if measure_number == 53:
            beat_ticks[3] = round(mido.second2tick(270.29375, source_ppq, 500_000))

        measure = timeline.measures[measure_number - 1]
        box = boxes.get(measure.measure_index)
        # Beat pixel positions, best-staff and engraving-aware, computed once per
        # measure. See aimusic.accompaniment.beat_geometry.
        beat_positions = None
        if box is not None:
            measure_glyphs = beat_glyphs.get(measure_number)
            if measure_glyphs is not None:
                beat_positions = infer_measure_beats(
                    list(measure_glyphs.glyphs),
                    box_x0=box.x0,
                    box_x1=box.x1,
                    new_system=measure_glyphs.new_system,
                )
        for beat in range(4):
            source_tick = beat_ticks[beat]
            if anchors and source_tick <= anchors[-1].source_midi_tick:
                # Recognition can collapse a weak beat onto its predecessor.
                # Retain strict runtime monotonicity without pretending the
                # adjustment is strong evidence.
                source_tick = anchors[-1].source_midi_tick + 1
            evidence = list(match_evidence)
            if beat == 0:
                evidence.insert(0, start.evidence)
            if (measure_number, beat) in {(12, 3), (53, 3)}:
                evidence.insert(
                    0,
                    BeatEvidence(
                        kind=EvidenceKind.HUMAN_CORRECTION,
                        confidence=0.98,
                        matched_notes=1,
                        details="distinctive B-natural solo pickup confirmed by the performer",
                    ),
                )
            confidence = max(item.confidence for item in evidence)
            anchors.append(
                PerformanceBeatAnchor(
                    score_tick=measure.start_tick + beat * timeline.canonical_ppq,
                    measure_index=measure.measure_index,
                    measure_label=measure.measure_label,
                    beat_in_measure=float(beat),
                    source_midi_tick=source_tick,
                    source_seconds=mido.tick2second(source_tick, source_ppq, 500_000),
                    confidence=confidence,
                    evidence=tuple(evidence),
                    pdf_page=box.page if box else None,
                    pdf_system=box.system if box else None,
                    pdf_x=(beat_positions.beat_x[beat] if beat_positions else None),
                    pdf_x_confidence=(beat_positions.confidence if beat_positions else None),
                    pdf_x_staff=(beat_positions.staff if beat_positions else None),
                    pdf_x_anchored=(beat_positions.anchored[beat] if beat_positions else None),
                )
            )

    mapped_labels = {anchor.measure_label for anchor in anchors}
    document = PerformanceBeatMapDocument(
        mapping_id="audiveris-oguri-symbolic-path-v2",
        timeline_id=timeline.timeline_id,
        source_id="oguri_performance",
        source_midi_ppq=source_ppq,
        canonical_ppq=timeline.canonical_ppq,
        review_state="machine",
        anchors=tuple(anchors),
        unmapped_measure_labels=tuple(
            measure.measure_label
            for measure in timeline.measures
            if measure.measure_label not in mapped_labels
        ),
    )
    write_performance_beat_map(document, args.output)


if __name__ == "__main__":
    main()
