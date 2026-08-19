from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import mido
import pytest

from aimusic.accompaniment.beat_anchor import (
    OnsetEvent,
    SnapCandidate,
    beat_hypotheses,
    beat_hypothesis,
    candidate_onsets,
    confirmed_defects,
    correction_from_selection,
    diff_beat_maps,
    group_onsets,
    merge_corrections,
    rederive_beat_map,
    reference_onsets,
    snap_to_pitch,
    suspect_measures,
)
from aimusic.accompaniment.score_fusion import (
    BeatEvidence,
    EvidenceKind,
    HumanCorrectionDocument,
    PerformanceBeatAnchor,
    PerformanceBeatMapDocument,
    apply_human_corrections,
)


def _beat_map() -> PerformanceBeatMapDocument:
    """Two measures, four beats each; beat 2 of m.1 is interpolated (no match)."""

    anchored = (
        BeatEvidence(kind=EvidenceKind.AUDIVERIS_SOLO_MATCH, confidence=0.8, matched_notes=5),
    )
    interpolated = (
        BeatEvidence(kind=EvidenceKind.STRUCTURAL_INTERPOLATION, confidence=0.3, matched_notes=0),
    )
    specs = [
        # (measure_label, beat, source_tick, source_seconds, evidence)
        ("1", 0.0, 100, 1.0, anchored),
        ("1", 1.0, 220, 1.5, anchored),
        ("1", 2.0, 500, 2.5, interpolated),
        ("1", 3.0, 620, 3.0, anchored),
        ("2", 0.0, 800, 4.0, anchored),
        ("2", 1.0, 900, 4.5, anchored),
    ]
    anchors = tuple(
        PerformanceBeatAnchor(
            score_tick=index * 960,
            measure_index=0 if label == "1" else 1,
            measure_label=label,
            beat_in_measure=beat,
            source_midi_tick=tick,
            source_seconds=seconds,
            confidence=0.8,
            evidence=evidence,
        )
        for index, (label, beat, tick, seconds, evidence) in enumerate(specs)
    )
    return PerformanceBeatMapDocument(
        mapping_id="test-map",
        timeline_id="test-timeline",
        source_id="reference-midi",
        source_midi_ppq=240,
        canonical_ppq=960,
        anchors=anchors,
    )


def _write_midi(path: Path, notes: list[tuple[int, int]], ppq: int = 240) -> None:
    """notes = [(absolute_tick, pitch)] on a single named PIANO SOLO track."""

    midi = mido.MidiFile(ticks_per_beat=ppq)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="PIANO SOLO", time=0))
    last = 0
    for tick, pitch in sorted(notes):
        track.append(mido.Message("note_on", note=pitch, velocity=64, time=tick - last))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=1))
        last = tick + 1
    midi.tracks.append(track)
    midi.save(str(path))


# ------------------------------------------------------------------- hypotheses


def test_beat_hypotheses_orders_by_beat_and_flags_interpolation() -> None:
    hypotheses = beat_hypotheses(_beat_map(), "1")
    assert [h.beat_in_measure for h in hypotheses] == [0.0, 1.0, 2.0, 3.0]
    anchored = {h.beat_in_measure: h.anchored for h in hypotheses}
    assert anchored[0.0] is True
    assert anchored[2.0] is False  # the interpolated beat: nothing to audition


def test_beat_hypothesis_lookup_and_miss() -> None:
    beat_map = _beat_map()
    assert beat_hypothesis(beat_map, "1", 3.0).source_seconds == 3.0
    assert beat_hypothesis(beat_map, "1", 9.0) is None


def test_human_verified_beat_counts_as_anchored_without_matched_notes() -> None:
    # A reviewed downbeat / human correction has confidence but no matcher count;
    # it is a real position, not an interpolation, so it must audition.
    for kind in (EvidenceKind.REVIEWED_DOWNBEAT, EvidenceKind.HUMAN_CORRECTION):
        anchor = PerformanceBeatAnchor(
            score_tick=0,
            measure_index=0,
            measure_label="1",
            beat_in_measure=0.0,
            source_midi_tick=10,
            source_seconds=1.0,
            confidence=1.0,
            evidence=(BeatEvidence(kind=kind, confidence=1.0, matched_notes=0),),
        )
        doc = PerformanceBeatMapDocument(
            mapping_id="m",
            timeline_id="t",
            source_id="s",
            source_midi_ppq=240,
            canonical_ppq=960,
            anchors=(anchor, anchor.model_copy(update={"score_tick": 960, "source_midi_tick": 20})),
        )
        assert beat_hypotheses(doc, "1")[0].anchored is True


# ------------------------------------------------------------------- candidates


def test_candidate_onsets_are_windowed_and_nearest_first() -> None:
    onsets = (
        OnsetEvent(native_tick=1, seconds=0.10, pitch=60),
        OnsetEvent(native_tick=2, seconds=2.40, pitch=59),  # just before beat
        OnsetEvent(native_tick=3, seconds=2.55, pitch=47),  # just after beat
        OnsetEvent(native_tick=4, seconds=9.00, pitch=60),  # far away
    )
    candidates = candidate_onsets(onsets, near_seconds=2.5, window_seconds=1.5)
    assert [c.native_tick for c in candidates] == [3, 2]  # 0.05 then 0.10 away
    assert all(abs(c.delta_seconds) <= 1.5 for c in candidates)


def test_group_onsets_turns_a_rolled_orchestral_chord_into_one_choice() -> None:
    onsets = (
        OnsetEvent(100, 1.00, 68, "Corni"),
        OnsetEvent(105, 1.08, 63, "Violini II"),
        OnsetEvent(110, 1.19, 66, "Viole"),
        OnsetEvent(300, 2.00, 46, "Violoncelli"),
    )

    groups = group_onsets(onsets)

    assert len(groups) == 2
    assert groups[0].native_tick == 100
    assert groups[0].pitches == (63, 66, 68)
    assert groups[0].track_names == ("Corni", "Viole", "Violini II")
    assert groups[1].pitches == (46,)


# ------------------------------------------------------------------ snap-to-pitch


def test_snap_prefers_exact_pitch_nearest_the_hypothesis() -> None:
    candidates = (
        SnapCandidate(native_tick=2, seconds=2.40, pitch=59, delta_seconds=-0.10),
        SnapCandidate(native_tick=3, seconds=2.55, pitch=47, delta_seconds=+0.05),
        SnapCandidate(native_tick=5, seconds=2.70, pitch=47, delta_seconds=+0.20),
    )
    result = snap_to_pitch(candidates, target_pitch=47)
    assert result.chosen.native_tick == 3
    assert not result.ambiguous


def test_snap_falls_back_to_pitch_class_across_octaves() -> None:
    candidates = (SnapCandidate(native_tick=7, seconds=2.6, pitch=35, delta_seconds=0.1),)
    result = snap_to_pitch(candidates, target_pitch=47)  # 47 % 12 == 35 % 12 == B
    assert result.chosen.native_tick == 7


def test_snap_flags_ambiguity_when_two_matches_are_equally_close() -> None:
    candidates = (
        SnapCandidate(native_tick=3, seconds=2.55, pitch=47, delta_seconds=+0.05),
        SnapCandidate(native_tick=2, seconds=2.44, pitch=47, delta_seconds=-0.06),
    )
    result = snap_to_pitch(candidates, target_pitch=47)
    assert result.ambiguous
    assert result.runner_up is not None


def test_snap_reports_no_match_without_guessing() -> None:
    candidates = (SnapCandidate(native_tick=1, seconds=2.5, pitch=60, delta_seconds=0.0),)
    result = snap_to_pitch(candidates, target_pitch=47)
    assert result.chosen is None and not result.ambiguous


# ------------------------------------------------------------------ reference MIDI


def test_reference_onsets_read_tick_and_seconds_from_solo_track(tmp_path: Path) -> None:
    path = tmp_path / "solo.mid"
    _write_midi(path, [(0, 60), (240, 62), (480, 64)], ppq=240)  # 120bpm default
    onsets = reference_onsets(path, track_filter="PIANO SOLO")
    assert [o.pitch for o in onsets] == [60, 62, 64]
    assert onsets[0].native_tick == 0
    assert onsets[1].native_tick == 240
    assert onsets[1].seconds == pytest.approx(0.5, abs=1e-3)  # one beat at 120bpm


# ------------------------------------------------------------------- suspect list


def test_confirmed_defects_only_where_both_alignments_disagree() -> None:
    map_downbeats = {"1": 10.0, "2": 20.0, "3": 30.0}
    a = {"1": 10.0, "2": 25.0, "3": 30.1}  # disagrees at 2 (and trivially at nothing else)
    b = {"1": 10.0, "2": 24.0, "3": 30.0}  # disagrees at 2
    defects = confirmed_defects(a, b, map_downbeats, tolerance=2.0)
    assert [d.measure_label for d in defects] == ["2"]
    assert defects[0].disagreement_seconds == pytest.approx(5.0)


def test_suspect_measures_adds_a_sparse_spine_for_shared_offsets() -> None:
    map_downbeats = {str(n): float(n) for n in range(1, 31)}
    identical = dict(map_downbeats)  # both alignments agree with the map everywhere
    suspects = suspect_measures(identical, identical, map_downbeats, tolerance=2.0, spine_step=15)
    labels = [s.measure_label for s in suspects]
    assert labels == ["1", "16"]  # every 15th bar, even with zero disagreement
    assert all(s.reason == "spine" for s in suspects)


# ------------------------------------------------------------------- persistence


def test_correction_from_selection_reuses_map_score_tick() -> None:
    beat_map = _beat_map()
    when = datetime(2026, 8, 7, tzinfo=timezone.utc)
    correction = correction_from_selection(
        beat_map, "2", 0.0, source_midi_tick=805, created_at=when, note="picked by ear"
    )
    assert correction.score_tick == 4 * 960  # the m.2 downbeat anchor's score_tick
    assert correction.source_midi_tick == 805
    assert correction.note == "picked by ear"


def test_correction_from_selection_rejects_unknown_beat() -> None:
    with pytest.raises(ValueError):
        correction_from_selection(
            _beat_map(), "1", 7.0, source_midi_tick=1, created_at=datetime.now(timezone.utc)
        )


def _corrections(beat_map, *specs):
    when = datetime(2026, 8, 7, tzinfo=timezone.utc)
    items = tuple(
        correction_from_selection(beat_map, m, b, source_midi_tick=t, created_at=when)
        for m, b, t in specs
    )
    return HumanCorrectionDocument(
        piece_id="chopin_op11", movement=2, timeline_id=beat_map.timeline_id, corrections=items
    )


def test_rederive_respaces_only_beats_adjacent_to_a_correction() -> None:
    beat_map = _beat_map()  # m.1 b2 (tick 500) is the only interpolated beat
    # Correct m.1 b3 (anchored knot at 620) to 640; b2 sits between b1(220) and b3.
    rederived = rederive_beat_map(beat_map, _corrections(beat_map, ("1", 3.0, 640)))
    by = {(a.measure_label, a.beat_in_measure): a.source_midi_tick for a in rederived.anchors}
    assert by[("1", 3.0)] == 640  # the human correction is a root, applied exactly
    # b2 re-interpolated between b1 (960->220) and b3 (2880->640): midpoint 430.
    assert by[("1", 2.0)] == 430
    # A beat in an untouched span keeps its original tick.
    assert by[("2", 1.0)] == 900


def test_rederive_without_corrections_is_identity() -> None:
    beat_map = _beat_map()
    rederived = rederive_beat_map(beat_map, None)
    assert [a.source_midi_tick for a in rederived.anchors] == [
        a.source_midi_tick for a in beat_map.anchors
    ]


def test_diff_beat_maps_reports_only_moved_beats() -> None:
    beat_map = _beat_map()
    rederived = rederive_beat_map(beat_map, _corrections(beat_map, ("1", 3.0, 640)))
    changes = diff_beat_maps(beat_map, rederived)
    moved = {(c.measure_label, c.beat_in_measure) for c in changes}
    assert ("1", 2.0) in moved and ("1", 3.0) in moved  # the correction + its cascade
    assert ("2", 0.0) not in moved and ("2", 1.0) not in moved  # untouched region


def test_merge_corrections_replaces_same_beat_and_stays_applyable() -> None:
    beat_map = _beat_map()
    when = datetime(2026, 8, 7, tzinfo=timezone.utc)
    first = correction_from_selection(beat_map, "1", 3.0, source_midi_tick=610, created_at=when)
    # Same beat, redone with a different onset.
    second = correction_from_selection(beat_map, "1", 3.0, source_midi_tick=615, created_at=when)
    merged = merge_corrections((first,), (second,))
    assert len(merged) == 1 and merged[0].source_midi_tick == 615
    # And the merged set stays monotonic against the map, so projection won't reject it.
    apply_human_corrections(
        beat_map,
        HumanCorrectionDocument(
            piece_id="chopin_op11", movement=2, timeline_id=beat_map.timeline_id, corrections=merged
        ),
    )
