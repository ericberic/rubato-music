"""Deterministic summaries for reconstructing a live runtime trace."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any


def resolve_runtime_trace(
    trace: Path | str | None = None,
    *,
    run_id: str | None = None,
    runs_dir: Path | str,
) -> Path:
    """Resolve an explicit, named, or latest live runtime trace."""

    if trace is not None and run_id is not None:
        raise ValueError("Pass either a trace path or run_id, not both")

    root = Path(runs_dir)
    if trace is not None:
        candidate = Path(trace).expanduser()
    elif run_id is not None:
        candidate = root / run_id / "trace" / "runtime.jsonl"
    else:
        candidates = [path for path in root.glob("live-*/trace/runtime.jsonl") if path.is_file()]
        if not candidates:
            raise FileNotFoundError(f"No live runtime traces found under {root}")
        candidate = max(
            candidates,
            key=lambda path: (path.stat().st_mtime_ns, path.parent.parent.name),
        )

    if not candidate.is_file():
        raise FileNotFoundError(f"Runtime trace not found: {candidate}")
    return candidate.resolve()


def describe_live_run_artifacts(
    runtime_trace: Path | str,
    *,
    processed_dir: Path | str,
) -> dict[str, Any]:
    """Describe the files that jointly represent one captured live take."""

    trace = Path(runtime_trace).resolve()
    run_id = trace.parent.parent.name
    capture_dir = Path(processed_dir) / run_id
    cursor_trace = trace.parent / "cursor.jsonl"
    captured_midi = capture_dir / "solo.mid"
    scratch_marker = capture_dir / ".rubato-scratch-performance.json"
    marker_payload: dict[str, Any] | None = None
    if scratch_marker.is_file():
        try:
            marker_payload = json.loads(scratch_marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            marker_payload = {"unreadable": True}

    return {
        "run_id": run_id,
        "runtime_trace": str(trace),
        "startup_diagnostics": {
            name: str(trace.parent / name)
            for name in (
                "startup-request.jsonl",
                "follower-startup.jsonl",
                "follower-startup-stacks.txt",
            )
            if (trace.parent / name).is_file()
        },
        "cursor_trace": str(cursor_trace.resolve()) if cursor_trace.is_file() else None,
        "captured_midi": str(captured_midi.resolve()) if captured_midi.is_file() else None,
        "scratch_marker": (
            {
                "path": str(scratch_marker.resolve()),
                "metadata": marker_payload,
            }
            if scratch_marker.is_file()
            else None
        ),
    }


def analyze_runtime_trace(path: Path | str) -> dict[str, Any]:
    """Summarize follower, tempo, policy, and MIDI dispatch behavior."""

    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    request_trace = Path(path).parent / "startup-request.jsonl"
    if request_trace.is_file():
        rows.extend(json.loads(line) for line in request_trace.read_text().splitlines())

    type_counts = Counter(str(row.get("type", "unknown")) for row in rows)
    follower_rows = [row for row in rows if row.get("type") == "follower"]
    tempo_rows = [row for row in rows if row.get("type") == "tempo"]
    policy_rows = [row for row in rows if row.get("type") == "policy"]
    scheduler_rows = [row for row in rows if row.get("type") == "scheduler"]
    control_rows = [row for row in rows if row.get("type") == "control"]
    midi_output_rows = [row for row in rows if row.get("type") == "midi_output"]
    audio_worker_rows = [
        row for row in rows if row.get("type") == "audio_worker" and row.get("action") == "window"
    ]
    mix_state_rows = [row for row in rows if row.get("type") == "mix_state"]

    raw_positions = [float(row.get("raw_score_beat", row["score_beat"])) for row in follower_rows]
    backward_raw_updates = sum(
        current < previous
        for previous, current in zip(raw_positions, raw_positions[1:], strict=False)
    )
    follower_latencies = [
        float(row["processing_latency_ms"])
        for row in follower_rows
        if row.get("processing_latency_ms") is not None
    ]
    tempo_bpms = [float(row["tempo_bpm"]) for row in tempo_rows]
    tempo_decisions = Counter(
        str(row["observation_decision"])
        for row in tempo_rows
        if row.get("observation_decision") is not None
    )
    autonomous_lead_bpms = [
        float(row["tempo_bpm"])
        for row in tempo_rows
        if row.get("observation_decision") == "autonomous_lead" and row.get("tempo_bpm") is not None
    ]
    autonomous_reference_bpms = [
        60.0 / float(row["reference_beat_period_seconds"])
        for row in tempo_rows
        if row.get("observation_decision") == "autonomous_lead"
        and row.get("reference_beat_period_seconds") is not None
    ]
    dispatched = [event for row in scheduler_rows for event in row.get("dispatched_events", [])]
    lateness = [
        float(event["lateness_ms"]) for event in dispatched if event.get("lateness_ms") is not None
    ]
    transitions: list[dict[str, Any]] = []
    prior_mode: str | None = None
    for row in policy_rows:
        current_mode = row.get("section_mode")
        explicit_previous = row.get("previous_section_mode")
        previous_mode = explicit_previous if explicit_previous is not None else prior_mode
        if previous_mode != current_mode:
            transitions.append(
                {
                    "monotonic_time": row.get("monotonic_time"),
                    "score_beat": row.get("score_beat"),
                    "from": previous_mode,
                    "to": current_mode,
                    "section_id": row.get("section_id"),
                    "reason": row.get("transition_reason"),
                }
            )
        prior_mode = current_mode
    panics = [row["panic_reason"] for row in scheduler_rows if row.get("panic_reason")]
    output_actions = Counter(str(row.get("action")) for row in midi_output_rows)
    note_on_rows = [row for row in midi_output_rows if row.get("action") == "note_on"]
    note_off_rows = [row for row in midi_output_rows if row.get("action") == "note_off"]
    note_on_lateness = [
        float(row["output_lateness_ms"])
        for row in note_on_rows
        if row.get("output_lateness_ms") is not None
    ]
    adapter_call_durations = [
        float(row["send_call_duration_ms"])
        for row in midi_output_rows
        if row.get("send_call_duration_ms") is not None
    ]
    commit_lead_times = [
        (float(row["target_perf_time"]) - float(row["committed_at"])) * 1000
        for row in note_on_rows
        if row.get("target_perf_time") is not None and row.get("committed_at") is not None
    ]
    expired_event_ids = [
        str(event_id)
        for row in scheduler_rows
        for event_id in (
            row.get("expired_event_ids", [])
            # Traces before the authority-generation migration used a
            # canonical-range suppression heuristic.
            or row.get("suppressed_event_ids", [])
        )
    ]
    authority_changes = [
        {
            "monotonic_time": row.get("monotonic_time"),
            "generation": row.get("authority_generation"),
            "reason": row.get("authority_reason"),
            "cancelled_event_ids": row.get("cancelled_event_ids", []),
            "expired_event_ids": row.get("expired_event_ids", []),
        }
        for row in scheduler_rows
        if row.get("authority_reason")
    ]
    scheduled_releases = {
        str(row["event_id"]): float(row["scheduled_note_off_time"])
        for row in note_on_rows
        if row.get("event_id") is not None and row.get("scheduled_note_off_time") is not None
    }
    note_off_lateness: list[float] = []
    for row in note_off_rows:
        if row.get("output_lateness_ms") is not None:
            note_off_lateness.append(float(row["output_lateness_ms"]))
            continue
        # Backward-compatible analysis for traces written before note-off rows
        # carried their own deadline/lateness fields.
        deadline = scheduled_releases.get(str(row.get("event_id")))
        if deadline is not None and row.get("monotonic_time") is not None:
            note_off_lateness.append((float(row["monotonic_time"]) - deadline) * 1000)
    emitted_velocities = [
        float(row["velocity"]) for row in note_on_rows if row.get("velocity") is not None
    ]
    input_velocities = [float(row["velocity"]) for row in rows if row.get("type") == "input"]
    note_on_times = {
        str(row["event_id"]): float(row["monotonic_time"])
        for row in note_on_rows
        if row.get("event_id") is not None and row.get("monotonic_time") is not None
    }
    emitted_lead_events = sorted(
        (
            (
                float(event["score_beat"]),
                note_on_times[str(event["event_id"])],
            )
            for event in dispatched
            if event.get("section_mode") == "LEAD"
            and event.get("score_beat") is not None
            and str(event.get("event_id")) in note_on_times
        ),
        key=lambda item: item[1],
    )
    realized_lead_bpms: list[float] = []
    prior_lead_event: tuple[float, float] | None = None
    for score_beat, emitted_at in emitted_lead_events:
        if prior_lead_event is not None:
            beat_delta = score_beat - prior_lead_event[0]
            time_delta = emitted_at - prior_lead_event[1]
            # Ignore notes within one chord/onset cluster and gaps between
            # separate LEAD sections. The remaining symbolic interval divided
            # by actual adapter emission time is a directly measured canonical
            # tempo sample.
            if 0.25 <= beat_delta <= 8.0 and 0 < time_delta <= 4.0:
                realized_lead_bpms.append(60.0 * beat_delta / time_delta)
        prior_lead_event = (score_beat, emitted_at)
    sounding: dict[tuple[int, int], str | None] = {}
    for row in midi_output_rows:
        action = row.get("action")
        channel = row.get("channel")
        pitch = row.get("pitch")
        if action == "panic":
            sounding.clear()
        elif channel is not None and pitch is not None:
            key = (int(channel), int(pitch))
            if action == "note_on":
                sounding[key] = row.get("event_id")
            elif action in {"note_off", "retrigger_note_off"}:
                sounding.pop(key, None)

    audio_zones: dict[str, dict[str, Any]] = {}
    for zone_id in sorted({str(row.get("zone_id")) for row in audio_worker_rows}):
        zone_rows = [row for row in audio_worker_rows if str(row.get("zone_id")) == zone_id]
        total_rows = [row for row in zone_rows if row.get("instrument_id") is None]
        instruments: dict[str, Any] = {}
        for instrument_id in sorted(
            {str(row["instrument_id"]) for row in zone_rows if row.get("instrument_id") is not None}
        ):
            instrument_rows = [
                row for row in zone_rows if str(row.get("instrument_id")) == instrument_id
            ]
            instruments[instrument_id] = {
                "windows": len(instrument_rows),
                "render_ms_p95": _distribution(
                    [
                        float(row["render_ms_p95"])
                        for row in instrument_rows
                        if row.get("render_ms_p95") is not None
                    ]
                ),
                "render_ms_max": max(
                    (
                        float(row["render_ms_max"])
                        for row in instrument_rows
                        if row.get("render_ms_max") is not None
                    ),
                    default=None,
                ),
            }
        audio_zones[zone_id] = {
            "windows": len(total_rows),
            "blocks": sum(int(row.get("blocks", 0)) for row in total_rows),
            "render_ms_p95": _distribution(
                [
                    float(row["render_ms_p95"])
                    for row in total_rows
                    if row.get("render_ms_p95") is not None
                ]
            ),
            "render_utilization_p95": _distribution(
                [
                    float(row["render_utilization_p95"])
                    for row in total_rows
                    if row.get("render_utilization_p95") is not None
                ]
            ),
            "render_utilization_max": max(
                (
                    float(row["render_utilization_max"])
                    for row in total_rows
                    if row.get("render_utilization_max") is not None
                ),
                default=None,
            ),
            "device_write_ms_p95": _distribution(
                [
                    float(row["device_write_ms_p95"])
                    for row in total_rows
                    if row.get("device_write_ms_p95") is not None
                ]
            ),
            "command_to_render_ms_p95": _distribution(
                [
                    float(row["command_to_render_ms_p95"])
                    for row in total_rows
                    if row.get("command_to_render_ms_p95") is not None
                ]
            ),
            "late_events": sum(int(row.get("late_events", 0)) for row in total_rows),
            "underruns": sum(int(row.get("underruns", 0)) for row in total_rows),
            "queue_high_watermark": max(
                (int(row.get("render_queue_high_watermark", 0)) for row in total_rows),
                default=0,
            ),
            "rss_mb_max": max(
                (float(row["rss_mb"]) for row in total_rows if row.get("rss_mb") is not None),
                default=None,
            ),
            "instruments": instruments,
        }

    mix_routes: dict[str, dict[str, Any]] = {}
    for zone_id, instrument_id in sorted(
        {(str(row.get("zone_id")), str(row.get("instrument_id"))) for row in mix_state_rows}
    ):
        route_rows = [
            row
            for row in mix_state_rows
            if str(row.get("zone_id")) == zone_id and str(row.get("instrument_id")) == instrument_id
        ]
        route_gains = [float(row["route_gain_end"]) for row in route_rows]
        effective_gains = [float(row["effective_gain_end"]) for row in route_rows]
        observer_delays = [
            max(0.0, (float(row["monotonic_time"]) - float(row["rendered_at"])) * 1000)
            for row in route_rows
        ]
        mix_routes[f"{zone_id}/{instrument_id}"] = {
            "samples": len(route_rows),
            "renderer": sorted({str(row.get("renderer")) for row in route_rows}),
            "program_revisions": sorted(
                {
                    f"{row.get('mix_program_id')}@{row.get('mix_program_revision')}"
                    for row in route_rows
                }
            ),
            "score_tick": {
                "min": min(int(row["score_tick_start"]) for row in route_rows),
                "max": max(int(row["score_tick_end"]) for row in route_rows),
            },
            "route_gain": _distribution(route_gains),
            "effective_gain": _distribution(effective_gains),
            "gain_change": max(effective_gains) - min(effective_gains),
            "master_gain": _distribution([float(row["master_gain"]) for row in route_rows]),
            "output_rms": _distribution([float(row["output_rms"]) for row in route_rows]),
            "output_peak": _distribution([float(row["output_peak"]) for row in route_rows]),
            "observer_delay_ms": _distribution(observer_delays),
        }

    return {
        "path": str(Path(path)),
        "row_counts": dict(sorted(type_counts.items())),
        "startup": sorted(
            (row for row in rows if row.get("type") in ("runtime_startup", "follower_startup")),
            key=lambda row: row["monotonic_time"],
        ),
        "follower": {
            "updates": len(follower_rows),
            "raw_backward_updates": backward_raw_updates,
            "clamped_backward_updates": sum(
                row.get("position_action") == "clamp_backward_jitter" for row in follower_rows
            ),
            "processing_latency_ms": _distribution(follower_latencies),
        },
        "tempo": {
            "decisions": dict(sorted(tempo_decisions.items())),
            "bpm": _distribution(tempo_bpms),
            "autonomous_lead_canonical_bpm": _distribution(autonomous_lead_bpms),
            "autonomous_lead_reference_bpm": _distribution(autonomous_reference_bpms),
            "realized_lead_canonical_bpm_from_midi_output": _distribution(realized_lead_bpms),
        },
        "controls": [
            {
                "monotonic_time": row.get("monotonic_time"),
                "control": row.get("control"),
                "requested_value": row.get("requested_value"),
                "applied_value": row.get("applied_value"),
                "section_mode": row.get("section_mode"),
            }
            for row in control_rows
        ],
        "policy_transitions": transitions,
        "scheduler": {
            "dispatched_note_ons": sum(
                len(row.get("dispatched_events") or row.get("dispatched_event_ids", []))
                for row in scheduler_rows
            ),
            "dispatch_lateness_ms": _distribution(lateness),
            "expired_event_count": len(expired_event_ids),
            "expired_event_ids": expired_event_ids,
            "authority_changes": authority_changes,
            "panics": panics,
        },
        "midi_output": {
            "actions": dict(sorted(output_actions.items())),
            "note_on_lateness_ms": _distribution(note_on_lateness),
            "commit_lead_time_ms": _distribution(commit_lead_times),
            "late_note_on_counts": {
                "at_least_50_ms": sum(value >= 50 for value in note_on_lateness),
                "at_least_100_ms": sum(value >= 100 for value in note_on_lateness),
                "at_least_250_ms": sum(value >= 250 for value in note_on_lateness),
            },
            "note_off_lateness_ms": _distribution(note_off_lateness),
            "adapter_call_duration_ms": _distribution(adapter_call_durations),
            "input_velocity": _distribution(input_velocities),
            "orchestra_velocity": _distribution(emitted_velocities),
            "notes_left_sounding": [
                {"channel": key[0], "pitch": key[1], "event_id": event_id}
                for key, event_id in sorted(sounding.items())
            ],
        },
        "audio_workers": {"zones": audio_zones},
        "mix_state": {"routes": mix_routes},
    }


def _distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))
    return {
        "min": ordered[0],
        "median": median(ordered),
        "p95": ordered[p95_index],
        "max": ordered[-1],
    }
