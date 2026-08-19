from __future__ import annotations

from dataclasses import replace

import pytest

from aimusic.accompaniment.oguri import oguri_movement
from aimusic.accompaniment.rehearsal_position import (
    MOVEMENT_2_VERIFIED_DOWNBEATS,
    estimate_performed_end_source_tick,
    rehearsal_cue_span,
    rehearsal_entry_for_measure,
    score_projection,
)

from tests.oguri_guard import requires_oguri_derived


def test_checked_oguri_anchors_project_to_joseffy_measures() -> None:
    projection = score_projection("chopin_op11", 2)

    first_solo = projection.position_at_source_tick(34_619 * 4)
    first_full_measure = projection.position_at_source_tick(35_374 * 4)
    measure_14_f_sharp = projection.position_at_source_tick(37_493 * 4)
    next_system = projection.position_at_source_tick(39_341 * 4)
    take_ending_chord = projection.position_at_source_tick(55_772 * 4)
    piano_resume = projection.position_at_source_tick(58_155 * 4)
    long_take_phrase_end = projection.position_at_source_tick(495_744)

    assert first_solo.measure_label == "12"
    assert first_full_measure.measure_label == "13"
    assert first_full_measure.beat_in_measure == 0.0
    assert measure_14_f_sharp.measure_label == "14"
    assert measure_14_f_sharp.beat_in_measure < 0.1
    assert next_system.measure_label == "15"
    assert next_system.beat_in_measure == 0.0
    assert take_ending_chord.measure_label == "22"
    assert take_ending_chord.beat_in_measure == 0.0
    assert piano_resume.measure_label == "23"
    assert long_take_phrase_end.measure_label == "52"
    # The rolled treble attack follows the bass downbeat by 30 native MIDI
    # ticks, but remains on beat one rather than two bars early.
    assert long_take_phrase_end.beat_in_measure < 0.15
    assert projection.canonical_position is True
    assert projection.mapping_review_state.value == "machine"


def test_first_sounding_orchestra_note_is_measure_one_downbeat() -> None:
    projection = score_projection("chopin_op11", 2)

    first_orchestra_e = projection.position_at_source_tick(2_017 * 4)
    technical_preroll = projection.position_at_source_tick(0)

    assert first_orchestra_e.measure_label == "1"
    assert first_orchestra_e.beat_in_measure == 0.0
    assert technical_preroll.measure_label == "1"
    assert technical_preroll.beat_in_measure == 0.0
    assert projection.source_tick_at_score_tick(0) == 2_017 * 4


def test_metronome_tempo_is_inferred_from_score_to_midi_correspondence() -> None:
    projection = score_projection("chopin_op11", 2)

    reference_bpm = projection.inferred_reference_quarter_bpm()

    # The source declares 120 BPM merely to convert its ticks into seconds.
    # Symbolic correspondence shows that its notated-quarter pulse is near 51.
    assert reference_bpm == pytest.approx(50.7042254)
    assert projection.tempo_scale_for_canonical_bpm(88) == pytest.approx(88 / reference_bpm)
    assert projection.tempo_scale_for_canonical_bpm(88) != pytest.approx(88 / 120)


def test_real_take_landmarks_do_not_hold_cursor_one_bar_behind() -> None:
    projection = score_projection("chopin_op11", 2)

    just_before_full_measure = projection.position_at_source_tick((35_374 * 4) - 1)
    first_full_measure = projection.position_at_source_tick(35_374 * 4)
    aligned_top_note = projection.position_at_source_tick(35_404 * 4)
    measure_14_f_sharp = projection.position_at_source_tick(37_493 * 4)
    measure_22_chord = projection.position_at_source_tick(55_772 * 4)

    # The dense beat map resolves the old one-tick gap at the m.13 boundary.
    assert just_before_full_measure.measure_label == "13"
    assert first_full_measure.measure_label == "13"
    assert aligned_top_note.measure_label == "13"
    assert 0.0 < aligned_top_note.beat_in_measure < 0.1
    assert measure_14_f_sharp.measure_label == "14"
    assert measure_14_f_sharp.beat_in_measure < 0.1
    assert measure_22_chord.measure_label == "22"
    assert measure_22_chord.beat_in_measure == 0.0


def test_sparse_verified_landmarks_constrain_symbolic_beat_map() -> None:
    projection = score_projection("chopin_op11", 2)

    assert [anchor.display_measure for anchor in MOVEMENT_2_VERIFIED_DOWNBEATS] == [
        1,
        13,
        14,
        17,
        18,
        22,
        23,
        52,
        94,
    ]
    for anchor in MOVEMENT_2_VERIFIED_DOWNBEATS:
        position = projection.position_at_source_tick(anchor.source_tick)
        assert position.measure_label == str(anchor.display_measure)
        assert position.beat_in_measure == 0.0


@pytest.mark.parametrize(
    ("source_tick", "measure", "beat"),
    (
        (84_874 * 4, 35, 0.0),
        (85_399 * 4, 35, 1.0),
        (85_931 * 4, 35, 2.0),
        (86_357 * 4, 35, 3.0),
        (206_422 * 4, 87, 0.0),
        (207_066 * 4, 87, 1.0),
        (207_589 * 4, 87, 2.0),
        (208_305 * 4, 87, 3.0),
        (220_944 * 4, 93, 0.0),
        (221_551 * 4, 93, 1.0),
        (222_086 * 4, 93, 2.0),
        (222_884 * 4, 93, 3.0),
        (223_861 * 4, 94, 0.0),
    ),
)
def test_rehearsal_verified_quarter_pulses_map_to_printed_beats(
    source_tick: int,
    measure: int,
    beat: float,
) -> None:
    position = score_projection("chopin_op11", 2).position_at_source_tick(source_tick)

    assert position.measure_label == str(measure)
    assert position.beat_in_measure == pytest.approx(beat)


def test_measure_45_to_47_follows_symbolic_chords_not_legacy_clock() -> None:
    projection = score_projection("chopin_op11", 2)

    # The old dense table labeled the solitary source onset at native tick
    # 107_926 as m.46. Audiveris/Oguri sequence matching places the notated
    # m.46 opening chord (47, 63, 71) at 109_900..109_915 instead.
    inside_measure_45 = projection.position_at_source_tick(107_926 * 4)
    measure_46_chord = projection.position_at_source_tick(109_912 * 4)
    measure_47_chord = projection.position_at_source_tick(112_160 * 4)

    assert inside_measure_45.measure_label == "45"
    assert inside_measure_45.beat_in_measure > 1.0
    assert measure_46_chord.measure_label == "46"
    assert measure_46_chord.beat_in_measure == 0.0
    assert measure_47_chord.measure_label == "47"
    assert measure_47_chord.beat_in_measure == 0.0


def test_measure_17_huge_rubato_uses_observed_beat_anchors() -> None:
    projection = score_projection("chopin_op11", 2)

    expected = ((42_703, 0.0), (44_653, 1.0), (45_109, 2.0), (45_550, 3.0))
    for native_tick, beat in expected:
        position = projection.position_at_source_tick(native_tick * 4)
        assert position.measure_label == "17"
        assert position.beat_in_measure == beat
    assert projection.position_at_source_tick(46_242 * 4).measure_label == "18"
    assert projection.position_at_source_tick(46_242 * 4).beat_in_measure == 0.0


@requires_oguri_derived
def test_measure_53_cue_targets_the_late_piano_pickup() -> None:
    projection = score_projection("chopin_op11", 2)
    measure_53_tick = projection.timeline.tick_at(52)

    entry = rehearsal_entry_for_measure(
        projection,
        measure_53_tick,
        oguri_movement(2).solo_reference_path,
    )

    assert entry.selected_measure == 53
    assert entry.entry_pitch == 71  # B-natural pickup
    assert entry.entry_source_seconds == 270.29375
    assert entry.entry_position.measure_label == "53"
    assert entry.entry_position.beat_in_measure == 3.0


@requires_oguri_derived
def test_measure_53_cue_starts_on_53_and_still_resolves_the_pickup() -> None:
    projection = score_projection("chopin_op11", 2)
    movement = oguri_movement(2)

    cue = rehearsal_cue_span(
        projection,
        selected_score_tick=projection.timeline.tick_at(52),
        solo_reference_path=movement.solo_reference_path,
        first_solo_entry_seconds=movement.first_solo_entry_seconds,
    )

    # The entry within the bar is still resolved -- m.53 opens with an
    # orchestral melody and the soloist's pickup arrives on beat 3 -- because it
    # remains a localization hint for the aligner. It no longer decides where the
    # orchestra starts: that is the measure the performer selected.
    assert cue.entry.entry_position.measure_label == "53"
    assert cue.entry.entry_position.beat_in_measure == 3.0
    assert cue.cue_start_measure_index == 52
    assert cue.cue_start_score_tick == projection.timeline.tick_at(52)
    assert cue.cue_start_source_seconds == projection.source_seconds_at_score_tick(
        projection.timeline.tick_at(52)
    )


@requires_oguri_derived
def test_cue_starts_at_the_selected_measure() -> None:
    projection = score_projection("chopin_op11", 2)
    movement = oguri_movement(2)

    first_entry = rehearsal_cue_span(
        projection,
        selected_score_tick=None,
        solo_reference_path=movement.solo_reference_path,
        first_solo_entry_seconds=movement.first_solo_entry_seconds,
    )
    opening = rehearsal_cue_span(
        projection,
        selected_score_tick=projection.timeline.tick_at(0),
        solo_reference_path=movement.solo_reference_path,
        first_solo_entry_seconds=movement.first_solo_entry_seconds,
    )

    assert first_entry.entry.entry_position.measure_label == "12"
    # The orchestra now starts on the measure the performer picked; they enter
    # whenever ready after it, so no lead-in measures are prepended.
    assert first_entry.cue_start_measure_index == 11
    assert opening.cue_start_measure_index == 0
    assert opening.cue_start_score_tick == 0


def test_real_take_span_projects_to_checked_opening_score_region() -> None:
    projection = score_projection("chopin_op11", 2)

    start = projection.position_at_source_tick(34_619 * 4)
    end = projection.position_at_source_tick(55_772 * 4)

    assert start.measure_label == "12"
    assert end.measure_label == "22"
    assert start.confidence >= 0.6
    assert end.confidence >= 0.5


def test_projection_without_anchors_degrades_to_identity() -> None:
    projection = replace(score_projection("chopin_op11", 2), anchors=())

    position = projection.position_at_source_tick(960)

    assert position.score_tick == 960
    assert position.confidence == 0.0


def test_duplicate_source_anchors_do_not_divide_by_zero() -> None:
    projection = replace(
        score_projection("chopin_op11", 2),
        anchors=((0, 960, 0.8), (0, 1920, 0.6)),
    )

    assert projection._project_tick(0) == (960, 0.6)


def test_projection_inverse_round_trips_score_targets_for_from_here_cues() -> None:
    projection = score_projection("chopin_op11", 2)

    for source_tick in (34_619 * 4, 37_493 * 4, 55_772 * 4, 58_155 * 4):
        displayed = projection.position_at_source_tick(source_tick)
        restored = projection.source_tick_at_score_tick(displayed.score_tick)
        assert abs(restored - source_tick) <= 1


def test_legacy_performed_span_estimator_is_bounded() -> None:
    end_tick = estimate_performed_end_source_tick(
        aligned_end_tick=223_092,
        timing_points=((220_660, 47.895), (223_092, 49.253)),
        take_duration_seconds=51.632,
    )

    # Retained for offline diagnostics. Performer-facing score spans no longer
    # use this estimate because silence is not evidence of a new score onset.
    assert end_tick > 225_368
    projection = score_projection("chopin_op11", 2)
    assert projection.position_at_source_tick(end_tick).measure_label == "22"
