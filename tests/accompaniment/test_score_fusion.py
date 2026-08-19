from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from aimusic.accompaniment.bundle_v2 import DisplayMappingDocument
from aimusic.accompaniment.score_fusion import (
    BeatEvidence,
    DownbeatEstimate,
    EvidenceKind,
    HumanBeatCorrection,
    HumanCorrectionDocument,
    PerformanceBeatAnchor,
    PerformanceBeatMapDocument,
    RecognizedNote,
    ReferenceNote,
    apply_human_corrections,
    build_measure_beat_ticks,
    infer_symbolic_downbeat_candidates,
    load_performance_beat_map,
    read_audiveris_musicxml,
    read_reference_midi,
    resolve_downbeat_path,
    source_midi_tick_at_seconds,
)


def _document() -> PerformanceBeatMapDocument:
    evidence = (BeatEvidence(kind=EvidenceKind.AUDIVERIS_SOLO_MATCH, confidence=0.8),)
    return PerformanceBeatMapDocument(
        mapping_id="test-map",
        timeline_id="test-timeline",
        source_id="reference-midi",
        source_midi_ppq=240,
        canonical_ppq=960,
        anchors=tuple(
            PerformanceBeatAnchor(
                score_tick=index * 960,
                measure_index=0,
                measure_label="1",
                beat_in_measure=float(index),
                source_midi_tick=source_tick,
                source_seconds=source_seconds,
                confidence=0.8,
                evidence=evidence,
            )
            for index, (source_tick, source_seconds) in enumerate(
                ((100, 1.0), (220, 1.5), (500, 2.5), (620, 3.0))
            )
        ),
    )


def test_audiveris_parser_preserves_backup_chord_and_engraving_x(tmp_path: Path) -> None:
    musicxml = tmp_path / "score.musicxml"
    musicxml.write_text(
        """<?xml version="1.0"?>
<score-partwise version="4.0"><part-list><score-part id="P1">
<part-name>Piano</part-name></score-part></part-list>
<part id="P1"><measure number="1" width="200"><attributes><divisions>4</divisions></attributes>
<note default-x="20"><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration></note>
<note default-x="20"><chord/><pitch><step>E</step><octave>4</octave></pitch>
<duration>4</duration></note>
<backup><duration>4</duration></backup>
<note default-x="70"><pitch><step>G</step><alter>1</alter><octave>3</octave></pitch>
<duration>4</duration></note>
</measure></part></score-partwise>""",
        encoding="utf-8",
    )

    notes = read_audiveris_musicxml(musicxml)["P1"]

    assert [(note.onset_beats, note.pitch) for note in notes] == [
        (0.0, 56),
        (0.0, 60),
        (0.0, 64),
    ]
    assert notes[0].default_x == 70.0
    assert notes[0].measure_width == 200.0


def test_symbolic_matches_recover_expressive_beats_not_uniform_quarters() -> None:
    recognized = tuple(
        RecognizedNote("P1", 17, float(beat), 60 + beat, None, None)
        for beat in range(4)
    )
    reference = tuple(
        ReferenceNote(tick, seconds, 60 + beat)
        for beat, (tick, seconds) in enumerate(
            ((42_703, 0.0), (44_653, 4.0), (45_109, 5.0), (45_550, 6.0))
        )
    )

    ticks, evidence = build_measure_beat_ticks(
        recognized,
        reference,
        start_tick=42_703,
        end_tick=46_242,
        evidence_kind=EvidenceKind.AUDIVERIS_SOLO_MATCH,
    )

    assert ticks == {0: 42_703, 1: 44_653, 2: 45_109, 3: 45_550, 4: 46_242}
    assert evidence[0].matched_notes == 4


def test_symbolic_matches_reject_collapsed_beat_warp() -> None:
    recognized = tuple(
        RecognizedNote("P1", 1, float(beat), 60 + beat, None, None)
        for beat in range(4)
    )
    reference = tuple(
        ReferenceNote(tick, seconds, 60 + beat)
        for beat, (tick, seconds) in enumerate(
            ((0, 0.0), (2, 0.01), (4, 0.02), (400, 2.0))
        )
    )

    ticks, evidence = build_measure_beat_ticks(
        recognized,
        reference,
        start_tick=0,
        end_tick=1_000,
        evidence_kind=EvidenceKind.AUDIVERIS_SOLO_MATCH,
    )

    assert ticks == {0: 0, 1: 250, 2: 500, 3: 750, 4: 1_000}
    assert evidence[0].kind == EvidenceKind.STRUCTURAL_INTERPOLATION
    assert "collapsed" in (evidence[0].details or "")


def test_symbolic_downbeat_accepts_rubato_without_duration_assumption() -> None:
    reviewed_evidence = BeatEvidence(
        kind=EvidenceKind.REVIEWED_DOWNBEAT,
        confidence=1.0,
    )
    candidate_evidence = BeatEvidence(
        kind=EvidenceKind.AUDIVERIS_REDUCTION_MATCH,
        confidence=0.8,
    )
    reviewed = {
        0: DownbeatEstimate(0, 1.0, reviewed_evidence),
        2: DownbeatEstimate(2_000, 1.0, reviewed_evidence),
    }
    # The candidate is close to the right constraint, but musical identity is
    # supplied by its symbolic match. Rubato is allowed to make the two
    # neighboring measure durations radically different.
    candidate = {1: DownbeatEstimate(1_800, 0.8, candidate_evidence)}

    completed = resolve_downbeat_path(
        first_measure=0,
        last_measure=2,
        verified_constraints=reviewed,
        symbolic_candidates=(candidate,),
    )

    assert completed[1] == candidate[1]


def test_symbolic_downbeat_cannot_cross_verified_constraint() -> None:
    evidence = BeatEvidence(kind=EvidenceKind.REVIEWED_DOWNBEAT, confidence=1.0)
    candidate_evidence = BeatEvidence(
        kind=EvidenceKind.AUDIVERIS_SOLO_MATCH,
        confidence=0.8,
    )

    completed = resolve_downbeat_path(
        first_measure=0,
        last_measure=2,
        verified_constraints={
            0: DownbeatEstimate(0, 1.0, evidence),
            2: DownbeatEstimate(2_000, 1.0, evidence),
        },
        symbolic_candidates=({1: DownbeatEstimate(2_200, 0.8, candidate_evidence)},),
    )

    assert completed[1].source_tick == 1_000
    assert completed[1].evidence.kind == EvidenceKind.STRUCTURAL_INTERPOLATION


def test_human_override_is_sparse_monotonic_and_revisionable() -> None:
    document = _document()
    correction = HumanBeatCorrection(
        correction_id="correction-1",
        source_midi_tick=240,
        score_tick=960,
        measure_label="1",
        beat_in_measure=1.0,
        created_at=datetime.now(timezone.utc),
    )
    corrections = HumanCorrectionDocument(
        piece_id="piece",
        movement=1,
        timeline_id=document.timeline_id,
        corrections=(correction,),
    )

    anchors = apply_human_corrections(document, corrections)

    assert anchors[1] == (960, 960, 1.0)
    assert anchors[2] == (2_000, 1_920, 0.8)

    invalid = corrections.model_copy(
        update={"corrections": (correction.model_copy(update={"source_midi_tick": 600}),)}
    )
    with pytest.raises(ValueError, match="non-monotonic"):
        apply_human_corrections(document, invalid)


def test_source_second_conversion_uses_artifact_knots_not_fixed_tempo() -> None:
    document = _document()

    assert source_midi_tick_at_seconds(document, 2.0) == 360
    assert source_midi_tick_at_seconds(document, 0.0) == 100
    assert source_midi_tick_at_seconds(document, 10.0) == 620


def test_movement2_pdf_geometry_uses_canonical_display_map_index() -> None:
    root = Path(__file__).resolve().parents[2]
    bundle = root / "data" / "scores" / "chopin_op11_movement_2"
    beat_map = load_performance_beat_map(
        bundle / "derived" / "performance_beat_map.machine.json"
    )
    display_map = DisplayMappingDocument.model_validate_json(
        (bundle / "derived" / "display_map.machine.json").read_text(encoding="utf-8")
    )
    first_box = display_map.boxes[0]
    first_beat = next(
        anchor
        for anchor in beat_map.anchors
        if anchor.measure_label == "1" and anchor.beat_in_measure == 0.0
    )

    assert first_beat.measure_index == 0
    assert first_box.measure_index == 0
    assert first_box.measure_label == "1"
    assert first_beat.pdf_page == first_box.page
    assert first_beat.pdf_system == first_box.system
    assert first_box.x0 <= first_beat.pdf_x <= first_box.x1


def test_movement2_symbolic_path_identifies_measure_46_opening_chord() -> None:
    root = Path(__file__).resolve().parents[2]
    bundle = root / "data" / "scores" / "chopin_op11_movement_2"
    mxl = bundle / "source" / "joseffy_reduction_movement2.audiveris.mxl"
    oguri = bundle / "source" / "oguri_concerto_11_2.mid"
    # Integration check over the real OMR recognition and the Oguri performance
    # recording; both are DVC-managed source and absent in CI (the local DVC
    # remote is unreachable there). Skip rather than commit heavy source data.
    if not (mxl.exists() and oguri.exists()):
        pytest.skip("Movement 2 DVC source artifacts (OMR MusicXML / Oguri MIDI) not pulled")
    parts = read_audiveris_musicxml(mxl)
    _ppq, solo = read_reference_midi(oguri, include_track_markers=("PIANO SOLO",))

    candidates = infer_symbolic_downbeat_candidates(
        tuple(note for note in parts["P1"] if 23 <= note.measure <= 52),
        solo,
    )

    # Audiveris recognizes the m.46 downbeat as pitches 47, 63, and 71. The
    # ordered Oguri sequence places those attacks at 109_900..109_915; their
    # median is 109_912. The former time-led boundary 107_926 is a solitary
    # pitch 54 inside m.45 and has no authority to advance the measure.
    assert candidates[46].source_tick == 109_912
    assert candidates[46].evidence.matched_notes == 3
    assert candidates[47].source_tick == 112_160
