from __future__ import annotations

import json
from pathlib import Path

import pytest

from aimusic.accompaniment.trace_analysis import (
    analyze_runtime_trace,
    describe_live_run_artifacts,
    resolve_runtime_trace,
)


def test_resolve_runtime_trace_defaults_to_latest_live_run(tmp_path: Path) -> None:
    older = tmp_path / "runs" / "live-100" / "trace" / "runtime.jsonl"
    newer = tmp_path / "runs" / "live-200" / "trace" / "runtime.jsonl"
    non_live = tmp_path / "runs" / "alignment-audition-300" / "trace" / "runtime.jsonl"
    for path in (older, newer, non_live):
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
    older.touch()
    newer.touch()
    non_live.touch()

    assert resolve_runtime_trace(runs_dir=tmp_path / "runs") == newer.resolve()
    assert resolve_runtime_trace(run_id="live-100", runs_dir=tmp_path / "runs") == older.resolve()


def test_describe_live_run_artifacts_joins_trace_capture_and_cursor(tmp_path: Path) -> None:
    trace = tmp_path / "runs" / "live-123" / "trace" / "runtime.jsonl"
    cursor = trace.parent / "cursor.jsonl"
    capture = tmp_path / "processed" / "live-123"
    midi = capture / "solo.mid"
    marker = capture / ".rubato-scratch-performance.json"
    trace.parent.mkdir(parents=True)
    capture.mkdir(parents=True)
    trace.write_text("{}\n", encoding="utf-8")
    cursor.write_text("{}\n", encoding="utf-8")
    midi.write_bytes(b"MThd")
    marker.write_text(
        json.dumps({"kind": "scratch_performance", "recording_id": "live-123"}),
        encoding="utf-8",
    )

    artifacts = describe_live_run_artifacts(trace, processed_dir=tmp_path / "processed")

    assert artifacts["run_id"] == "live-123"
    assert artifacts["runtime_trace"] == str(trace.resolve())
    assert artifacts["cursor_trace"] == str(cursor.resolve())
    assert artifacts["captured_midi"] == str(midi.resolve())
    assert artifacts["scratch_marker"]["metadata"]["kind"] == "scratch_performance"


def test_trace_summary_reconstructs_jitter_tempo_policy_and_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "runtime.jsonl"
    rows = (
        {
            "type": "follower",
            "score_beat": 17.0,
            "raw_score_beat": 17.0,
            "position_action": "advance",
            "processing_latency_ms": 2.0,
        },
        {
            "type": "follower",
            "score_beat": 17.0,
            "raw_score_beat": 16.9,
            "position_action": "clamp_backward_jitter",
            "processing_latency_ms": 6.0,
        },
        {
            "type": "tempo",
            "tempo_bpm": 76.0,
            "reference_beat_period_seconds": 0.5,
            "observation_decision": "autonomous_lead",
        },
        {
            "type": "policy",
            "monotonic_time": 10.0,
            "score_beat": 84.0,
            "previous_section_mode": "FOLLOW",
            "section_mode": "LEAD",
            "section_id": "m22-orchestra-interlude",
            "transition_reason": "symbolic_position_entered_lead_section",
        },
        {
            "type": "scheduler",
            "panic_reason": None,
            "suppressed_event_ids": ["stale-before-relock"],
            "authority_generation": 4,
            "authority_reason": "mode:hold->follow",
            "dispatched_events": [
                {
                    "event_id": "a",
                    "score_beat": 84.0,
                    "section_mode": "LEAD",
                    "lateness_ms": 3.0,
                },
                {
                    "event_id": "b",
                    "score_beat": 85.0,
                    "section_mode": "LEAD",
                    "lateness_ms": 5.0,
                },
            ],
        },
        {
            "type": "control",
            "monotonic_time": 10.1,
            "control": "orchestra_volume",
            "requested_value": 0.4,
            "applied_value": 0.4,
            "section_mode": "FOLLOW",
        },
        {
            "type": "midi_output",
            "action": "note_on",
            "monotonic_time": 10.0,
            "channel": 1,
            "pitch": 60,
            "event_id": "a",
            "velocity": 72,
            "target_perf_time": 10.0,
            "committed_at": 9.92,
            "output_lateness_ms": 2.5,
            "send_call_duration_ms": 0.2,
        },
        {
            "type": "midi_output",
            "action": "note_on",
            "monotonic_time": 11.0,
            "channel": 1,
            "pitch": 62,
            "event_id": "b",
            "velocity": 72,
            "output_lateness_ms": 103.5,
            "send_call_duration_ms": 0.4,
        },
        {
            "type": "midi_output",
            "action": "note_off",
            "channel": 1,
            "pitch": 60,
            "event_id": "a",
            "output_lateness_ms": 4.0,
        },
        {
            "type": "midi_output",
            "action": "note_off",
            "channel": 1,
            "pitch": 62,
            "event_id": "b",
            "output_lateness_ms": 5.0,
        },
        {
            "type": "audio_worker",
            "action": "window",
            "zone_id": "room_center",
            "instrument_id": None,
            "blocks": 94,
            "render_ms_p95": 4.2,
            "render_utilization_p95": 0.39,
            "render_utilization_max": 0.61,
            "device_write_ms_p95": 10.8,
            "command_to_render_ms_p95": 42.0,
            "late_events": 1,
            "underruns": 0,
            "render_queue_high_watermark": 3,
            "rss_mb": 812.0,
        },
        {
            "type": "audio_worker",
            "action": "window",
            "zone_id": "room_center",
            "instrument_id": "violins",
            "blocks": 94,
            "render_ms_p95": 3.8,
            "render_ms_max": 5.1,
        },
        {
            "type": "mix_state",
            "monotonic_time": 10.15,
            "rendered_at": 10.1,
            "renderer": "live_vst",
            "mix_program_id": "main",
            "mix_program_revision": 3,
            "zone_id": "room_center",
            "instrument_id": "violins",
            "score_tick_start": 41_280,
            "score_tick_end": 41_344,
            "route_gain_start": 0.2,
            "route_gain_end": 0.25,
            "master_gain": 0.8,
            "effective_gain_start": 0.16,
            "effective_gain_end": 0.2,
            "output_rms": 0.15,
            "output_peak": 0.22,
        },
        {
            "type": "mix_state",
            "monotonic_time": 10.25,
            "rendered_at": 10.2,
            "renderer": "live_vst",
            "mix_program_id": "main",
            "mix_program_revision": 3,
            "zone_id": "room_center",
            "instrument_id": "violins",
            "score_tick_start": 41_344,
            "score_tick_end": 41_408,
            "route_gain_start": 0.25,
            "route_gain_end": 0.5,
            "master_gain": 0.8,
            "effective_gain_start": 0.2,
            "effective_gain_end": 0.4,
            "output_rms": 0.3,
            "output_peak": 0.45,
        },
    )
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    summary = analyze_runtime_trace(path)

    assert summary["follower"]["raw_backward_updates"] == 1
    assert summary["follower"]["clamped_backward_updates"] == 1
    assert summary["follower"]["processing_latency_ms"]["max"] == 6.0
    assert summary["tempo"]["decisions"] == {"autonomous_lead": 1}
    assert summary["tempo"]["autonomous_lead_canonical_bpm"]["median"] == 76
    assert summary["tempo"]["autonomous_lead_reference_bpm"]["median"] == 120
    assert summary["tempo"]["realized_lead_canonical_bpm_from_midi_output"]["median"] == 60
    assert summary["policy_transitions"][0]["section_id"] == "m22-orchestra-interlude"
    assert summary["scheduler"]["dispatch_lateness_ms"]["median"] == 4.0
    assert summary["midi_output"]["actions"] == {"note_off": 2, "note_on": 2}
    assert summary["controls"][0]["control"] == "orchestra_volume"
    assert summary["scheduler"]["expired_event_count"] == 1
    assert summary["scheduler"]["expired_event_ids"] == ["stale-before-relock"]
    assert summary["scheduler"]["authority_changes"][0]["generation"] == 4
    assert summary["midi_output"]["commit_lead_time_ms"]["median"] == pytest.approx(80)
    assert summary["midi_output"]["note_on_lateness_ms"]["max"] == 103.5
    assert summary["midi_output"]["late_note_on_counts"]["at_least_100_ms"] == 1
    assert summary["midi_output"]["adapter_call_duration_ms"]["max"] == 0.4
    assert summary["midi_output"]["note_off_lateness_ms"]["max"] == 5.0
    assert summary["midi_output"]["orchestra_velocity"]["median"] == 72
    assert summary["midi_output"]["notes_left_sounding"] == []
    room = summary["audio_workers"]["zones"]["room_center"]
    assert room["blocks"] == 94
    assert room["render_utilization_max"] == 0.61
    assert room["late_events"] == 1
    assert room["underruns"] == 0
    assert room["instruments"]["violins"]["render_ms_p95"]["max"] == 3.8
    mix = summary["mix_state"]["routes"]["room_center/violins"]
    assert mix["samples"] == 2
    assert mix["program_revisions"] == ["main@3"]
    assert mix["score_tick"] == {"min": 41_280, "max": 41_408}
    assert mix["effective_gain"]["min"] == 0.2
    assert mix["effective_gain"]["max"] == 0.4
    assert mix["gain_change"] == pytest.approx(0.2)
    assert mix["observer_delay_ms"]["max"] == pytest.approx(50)
