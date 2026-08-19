from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from aimusic.accompaniment.conversion import convert_mxl_excerpt_to_bundle
from aimusic.accompaniment.evaluation import evaluate_scheduled_events
from aimusic.accompaniment.following import ReferencePitchFollower
from aimusic.accompaniment.scheduler import AccompanimentScheduler
from aimusic.accompaniment.simulation import (
    generate_synthetic_performance,
    hide_score_beats,
    run_simulated_online,
)
from aimusic.accompaniment.tempo_model import OnlineTempoModel

SOURCE_MXL = Path("assets/scores/chopin_op11_i_allegro_maestoso/source/score.mxl")


def _write_mxl(path: Path, root_name: str, xml_text: str) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(root_name, xml_text)
    return path


def _simple_musicxml(
    score_parts: list[tuple[str, str]],
    part_bodies: dict[str, str],
) -> str:
    score_part_rows = "\n".join(
        f'<score-part id="{part_id}"><part-name>{name}</part-name></score-part>'
        for part_id, name in score_parts
    )
    part_rows = "\n".join(
        f'<part id="{part_id}"><measure number="1">{body}</measure></part>'
        for part_id, _name in score_parts
        for body in [part_bodies[part_id]]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>{score_part_rows}</part-list>
  {part_rows}
</score-partwise>
"""


def _pitched_note(
    step: str,
    octave: str = "4",
    *,
    duration: str = "1",
) -> str:
    return f"""
<note>
  <pitch><step>{step}</step><octave>{octave}</octave></pitch>
  <duration>{duration}</duration>
</note>
"""


def test_convert_real_chopin_musicxml_excerpt_to_score_bundle(tmp_path: Path) -> None:
    bundle = convert_mxl_excerpt_to_bundle(
        SOURCE_MXL,
        tmp_path / "bundle",
        start_measure=139,
        end_measure=141,
        version="test-m139-141",
    )

    assert bundle.metadata.piece_id == "chopin_op11_i_m139_141"
    assert len(bundle.solo_events) >= 10
    assert len(bundle.accompaniment_events) >= 5
    assert {event.role for event in bundle.solo_events} == {"solo"}
    assert {event.role for event in bundle.accompaniment_events} == {"accompaniment"}
    assert min(event.measure for event in bundle.events) == 139
    assert max(event.measure for event in bundle.events) == 141
    assert all(event.beat >= 0 for event in bundle.events)


def test_converter_accepts_musicxml_root_inside_mxl(tmp_path: Path) -> None:
    source = _write_mxl(
        tmp_path / "source.mxl",
        "score.musicxml",
        _simple_musicxml(
            [("P1", "Piano solo"), ("P2", "Violin")],
            {
                "P1": "<attributes><divisions>1</divisions></attributes>" + _pitched_note("C"),
                "P2": "<attributes><divisions>1</divisions></attributes>" + _pitched_note("G"),
            },
        ),
    )

    bundle = convert_mxl_excerpt_to_bundle(
        source,
        tmp_path / "bundle",
        start_measure=1,
        end_measure=1,
    )

    assert len(bundle.solo_events) == 1
    assert bundle.solo_events[0].pitch == 60


def test_converter_tolerates_malformed_musicxml_pitch_and_duration(
    tmp_path: Path,
) -> None:
    source = _write_mxl(
        tmp_path / "source.mxl",
        "score.xml",
        _simple_musicxml(
            [("P1", "Piano solo"), ("P2", "Violin"), ("P3", "Viola")],
            {
                "P1": (
                    "<attributes><divisions>0</divisions></attributes>"
                    + _pitched_note("C", duration="bad")
                ),
                "P2": (
                    "<attributes><divisions>not-an-int</divisions></attributes>"
                    + _pitched_note("H")
                ),
                "P3": "<attributes><divisions>1</divisions></attributes>" + _pitched_note("G"),
            },
        ),
    )

    bundle = convert_mxl_excerpt_to_bundle(
        source,
        tmp_path / "bundle",
        start_measure=1,
        end_measure=1,
    )

    assert len(bundle.solo_events) == 1
    assert bundle.solo_events[0].duration_beats == pytest.approx(0.000001)
    assert [event.part_id for event in bundle.accompaniment_events] == ["P3"]


def test_converter_skips_general_midi_percussion_channel_for_accompaniment(
    tmp_path: Path,
) -> None:
    accompaniment_parts = [(f"P{index:02d}", f"Part {index:02d}") for index in range(2, 13)]
    source = _write_mxl(
        tmp_path / "source.mxl",
        "score.xml",
        _simple_musicxml(
            [("P1", "Piano solo"), *accompaniment_parts],
            {
                "P1": "<attributes><divisions>1</divisions></attributes>" + _pitched_note("C"),
                **{
                    part_id: (
                        "<attributes><divisions>1</divisions></attributes>"
                        + _pitched_note("G")
                    )
                    for part_id, _name in accompaniment_parts
                },
            },
        ),
    )

    bundle = convert_mxl_excerpt_to_bundle(
        source,
        tmp_path / "bundle",
        start_measure=1,
        end_measure=1,
    )

    channels = [entry.channel for entry in bundle.instrument_map]
    assert len(channels) == len(accompaniment_parts)
    assert 9 not in channels


def test_real_chopin_excerpt_hidden_scorebeat_playback_schedules_accompaniment(
    tmp_path: Path,
) -> None:
    bundle = convert_mxl_excerpt_to_bundle(
        SOURCE_MXL,
        tmp_path / "bundle",
        start_measure=139,
        end_measure=141,
        version="test-m139-141",
    )
    performed_with_truth = generate_synthetic_performance(
        bundle.solo_events,
        start_perf_time=1.0,
        beat_period_fn=lambda _beat: 0.5,
    )
    performed_without_truth = hide_score_beats(performed_with_truth)

    trace = run_simulated_online(
        bundle=bundle,
        performed_notes=performed_without_truth,
        follower=ReferencePitchFollower(bundle.solo_events, search_window_events=32),
        tempo_model=OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0),
        scheduler=AccompanimentScheduler(bundle, lookahead_beats=1.0),
    )

    assert len(trace.performed_notes) == len(performed_with_truth)
    assert all(note.score_beat is None for note in trace.performed_notes)
    assert len(trace.follower_updates) == len(trace.performed_notes)
    assert [update.score_beat for update in trace.follower_updates] == pytest.approx(
        [note.score_beat for note in performed_with_truth]
    )
    assert trace.scheduled_events

    expected_times = {
        event.event_id: 1.0 + event.beat * 0.5 for event in bundle.accompaniment_events
    }
    evaluation = evaluate_scheduled_events(
        trace.scheduled_events,
        expected_times,
        tolerance_seconds=0.001,
    )

    assert evaluation.passed
    assert evaluation.max_abs_error_seconds == pytest.approx(0.0)
