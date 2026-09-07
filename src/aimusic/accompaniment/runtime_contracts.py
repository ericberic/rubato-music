"""Typed lifecycle, status, configuration, and trace contracts for live runs."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aimusic.accompaniment.section_policy import AccompanimentMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunMode(StrEnum):
    REHEARSAL = "rehearsal"
    PERFORMANCE = "performance"


class RunPhase(StrEnum):
    PREPARING = "preparing"
    LISTENING = "listening"
    ACTIVE = "active"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


class LiveStateWord(StrEnum):
    LISTENING = "Listening"
    FOLLOWING = "Following"
    LEADING = "Leading"
    WAITING = "Waiting"
    SILENT = "Silent"


class TelemetryLevel(StrEnum):
    """Cost envelope for optional runtime observation."""

    OFF = "off"
    COUNTERS = "counters"
    TRACE = "trace"


class RuntimeConfig(StrictModel):
    run_id: str = Field(min_length=1)
    run_mode: RunMode = RunMode.REHEARSAL
    initial_tempo_bpm: float = Field(default=120.0, gt=0)
    orchestra_volume: float = Field(default=0.75, ge=0, le=1)
    # A single, mode-independent switch for the spatial mix. Rehearse-from-a-bar
    # and perform-from-the-top are one runtime that differ only by start measure;
    # the mix must not silently depend on which entry point launched the run (a
    # rehearsal take once skipped the mix while a live run applied it, so the same
    # passage sounded different). ``mix_program_id`` selects *which* program when
    # more than one exists; ``None`` means the piece's default. Turning the mix
    # off is this flag, never the absence of an id.
    mix_enabled: bool = True
    mix_program_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    mix_program_revision: int | None = Field(default=None, ge=1)
    # A mix program decides balance and routing, never whether the audible
    # orchestra exists at all. This floor is applied to the anchor/Yamaha output
    # gain so a routing gap or an uncalibrated-zone fallback (which once muted the
    # Clavinova on the m.44 trigger beats) can never silence it. Intentional
    # silence is the separate global orchestra mute (``orchestra_volume=0``),
    # applied after this, so the floor never traps the performer.
    min_audible_orchestra_gain: float = Field(default=0.2, ge=0, le=1)
    telemetry_level: TelemetryLevel = TelemetryLevel.COUNTERS
    planning_horizon_ms: float = Field(default=500.0, ge=0)
    dispatch_horizon_ms: float = Field(default=100.0, ge=0)
    output_advance_ms: float = Field(default=0.0, ge=0, le=250)
    minimum_follower_confidence: float = Field(default=0.5, ge=0, le=1)
    # Follower dropout is judged on a TEMPO-RELATIVE silence budget, not a fixed
    # wall-clock. A ritardando into a climax (observed live at 51->27 BPM into the
    # m.45 downbeat) makes a single beat last >2 s, so a fixed 1.5 s coast would
    # declare a dropout on perfectly-played rubato and panic the orchestra at the
    # very downbeat it is expanding toward. The budget is
    # ``follower_coast_beats`` beats at the current beat period, clamped to
    # [``follower_coast_ms``, ``follower_coast_max_ms``]. ``follower_coast_ms``
    # therefore acts as the fast-tempo floor and stays the knob older callers set.
    follower_coast_ms: float = Field(default=1500.0, ge=0, le=5000)
    follower_coast_beats: float = Field(default=2.0, ge=0, le=8)
    follower_coast_max_ms: float = Field(default=4000.0, ge=0, le=10000)
    # When the pianist is genuinely silent where the score expects sound, the
    # orchestra decelerates by this fraction while it coasts, giving the performer
    # room to re-enter instead of marching ahead or being cut off. 0.2 == play the
    # waiting stretch ~20% slower. Silence that outlasts the coast budget hands off
    # to a *gentle* dropout hold that stops scheduling new notes but never fires an
    # all-notes-off panic, so sustaining orchestra chords ring on until relock.
    follower_coast_slowdown: float = Field(default=0.2, ge=0, le=0.9)
    count_off_channel: int = Field(
        default=9,
        ge=0,
        le=15,
        description="Explicit MIDI channel for the rehearsal count-off cue.",
    )
    # FOLLOW clock: "lte" (default) keeps a reactive pace estimate and adds a
    # bounded leading phase so the orchestra anticipates arrival beats instead of
    # waiting to detect them; validated to roughly halve the climax lag on a real
    # Yamaha take (docs/decisions/0008-predictive-follow-clock.md). "reactive" is
    # the plain last-detected-note baseline, kept for A/B.
    follow_clock: Literal["reactive", "lte"] = "lte"
    # Orchestra cue-in tempo/phase acquisition. At an orchestra-led entry the
    # follower localizes the pianist in ~2 notes, but that spans ~0 beats and
    # carries no pace. The orchestra therefore holds at the entry beat while
    # these thresholds' worth of piano onsets accumulate, then clamps FOLLOW onto
    # a pace fit to the piano alone (never the pre-entry warp seed). Trace-derived
    # starting points, not searched
    # (docs/decisions/0010-human-beat-anchors.md).
    entry_tempo_min_onsets: int = Field(default=6, ge=2)
    entry_tempo_min_span_beats: float = Field(default=0.5, gt=0)
    entry_tempo_min_seconds: float = Field(default=1.3, gt=0)
    entry_tempo_max_seconds: float = Field(default=3.0, gt=0)
    # Hard ceiling on the whole acquisition. ``entry_tempo_max_seconds`` only
    # relaxes the *fit* gates; a forced fit can still be rejected outright (slope
    # <= 0, or a tempo outside the plausible band) and that rejection is
    # indistinguishable from "keep waiting". Without this ceiling the orchestra
    # freezes for the rest of the take -- observed live as 77 piano onsets
    # against a single dispatched orchestra note.
    entry_tempo_abandon_seconds: float = Field(default=6.0, gt=0)
    # Follower re-localization watchdog. A note-by-note tracker that mis-locks
    # early -- e.g. an opening it never locks -- keeps drifting behind and cannot
    # climb back, so it stays tens of beats off for the whole take even though
    # the later material tracks fine (observed offline: a take stuck ~70 beats
    # behind that a single global PTHMM re-search recovered to ~4). The watchdog
    # fires a re-search when the reported position advances less than
    # ``follower_relock_min_advance_beats`` over ``follower_relock_window_seconds``
    # *despite* ``follower_relock_min_updates`` note updates in that span. The
    # update gate is the safety: rests, holds, and ritardandos emit too few
    # updates to trip it, so only sustained playing that makes no score progress
    # does. Validated to recover the stuck take with zero re-locks on a cleanly
    # tracked one.
    follower_relock_enabled: bool = True
    follower_relock_window_seconds: float = Field(default=8.0, gt=0)
    follower_relock_min_updates: int = Field(default=25, ge=2)
    follower_relock_min_advance_beats: float = Field(default=4.0, ge=0)
    follower_relock_cooldown_seconds: float = Field(default=6.0, ge=0)

    @model_validator(mode="after")
    def validate_horizons(self) -> "RuntimeConfig":
        if self.dispatch_horizon_ms > self.planning_horizon_ms:
            raise ValueError("dispatch horizon cannot exceed planning horizon")
        if self.entry_tempo_max_seconds < self.entry_tempo_min_seconds:
            raise ValueError("entry_tempo_max_seconds cannot be below entry_tempo_min_seconds")
        if self.entry_tempo_abandon_seconds < self.entry_tempo_max_seconds:
            raise ValueError("entry_tempo_abandon_seconds cannot be below entry_tempo_max_seconds")
        if self.follower_coast_max_ms < self.follower_coast_ms:
            raise ValueError("follower_coast_max_ms cannot be below follower_coast_ms")
        return self


class RuntimeScorePosition(StrictModel):
    """Projected performer-facing score location for the live cockpit."""

    score_tick: int = Field(ge=0)
    score_beat: float = Field(ge=0)
    measure_index: int = Field(ge=0)
    measure_label: str
    beat_in_measure: float = Field(ge=0)
    source_seconds: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    mapping_id: str
    mapping_review_state: Literal["machine", "reviewed"]
    canonical_position: bool


class RuntimeStatus(StrictModel):
    run_id: str
    run_mode: RunMode
    phase: RunPhase
    state_word: LiveStateWord
    monotonic_time: float = Field(ge=0)
    score_beat: float | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    tempo_bpm: float | None = Field(default=None, gt=0)
    orchestra_tempo_bpm: float = Field(default=120.0, gt=0)
    orchestra_volume: float = Field(default=0.75, ge=0, le=1)
    orchestra_output_advance_ms: float = Field(default=0.0, ge=0, le=250)
    orchestra_renderer_state: Literal[
        "not_loaded", "loading", "opening_audio", "ready", "failed"
    ] = "not_loaded"
    orchestra_renderer_zone_id: str | None = None
    orchestra_renderer_instrument_id: str | None = None
    orchestra_renderer_loaded_instruments: int = Field(default=0, ge=0)
    orchestra_renderer_total_instruments: int = Field(default=0, ge=0)
    section_mode: AccompanimentMode | None = None
    message: str | None = None
    coordinate_system: Literal[
        "legacy_score_beat", "midi_performance_provisional", "canonical_score"
    ] = "legacy_score_beat"
    canonical_position: bool = False
    performance_ready: bool = False
    score_position: RuntimeScorePosition | None = None


class OrchestraRendererStatus(StrictModel):
    """Lifecycle of the resident BBCSO/CoreAudio renderer, independent of a run."""

    state: Literal["not_loaded", "unavailable", "loading", "opening_audio", "ready", "failed"] = (
        "not_loaded"
    )
    preload_id: str | None = None
    zone_id: str | None = None
    device_name: str | None = None
    instrument_id: str | None = None
    loaded_instruments: int = Field(default=0, ge=0)
    total_instruments: int = Field(default=0, ge=0)
    started_at_monotonic: float | None = Field(default=None, ge=0)
    updated_at_monotonic: float = Field(default=0, ge=0)
    message: str = "BBCSO is not loaded"
    error_type: str | None = None
    error_detail: str | None = None
    worker_exit_code: int | None = None
    trace_path: str | None = None


class RuntimeStartTrace(StrictModel):
    """Explicit canonical/reference seed for a non-zero runtime entry."""

    type: Literal["runtime_start"]
    monotonic_time: float
    entry_perf_time: float
    score_beat: float = Field(ge=0)
    reference_beat: float | None = None
    section_mode: AccompanimentMode
    start_kind: Literal["count_off_measure", "orchestra_lead_in"]


class CountOffTrace(StrictModel):
    """One audible count-off click leading to a selected score entry."""

    type: Literal["count_off"]
    monotonic_time: float
    target_perf_time: float
    beat_index: int = Field(ge=1)
    beat_count: int = Field(ge=1)
    tempo_bpm: float = Field(gt=0)
    entry_perf_time: float
    entry_score_beat: float = Field(ge=0)
    entry_reference_beat: float | None = None
    entry_section_mode: AccompanimentMode


class InputTrace(StrictModel):
    type: Literal["input"]
    monotonic_time: float
    perf_time: float
    pitch: int = Field(ge=0, le=127)
    velocity: int = Field(ge=0, le=127)


class FollowerTrace(StrictModel):
    type: Literal["follower"]
    monotonic_time: float
    perf_time: float
    score_beat: float
    raw_score_beat: float | None = None
    reference_beat: float | None = None
    confidence: float = Field(ge=0, le=1)
    position_action: (
        Literal[
            "initial",
            "advance",
            "clamp_backward_jitter",
            "repeat_reset",
            "ignored_during_lead",
            "buffered_for_lead_handoff",
            "orchestra_entry_not_yet_locked",
            "orchestra_entry_position_mismatch",
            "orchestra_entry_acquiring",
            "orchestra_entry_handoff",
            "orchestra_entry_abandoned",
            "coast_from_confident_anchor",
            "count_off_entry_not_yet_locked",
            "dropout_hold",
        ]
        | None
    ) = None
    processing_latency_ms: float | None = Field(default=None, ge=0)
    follower_state: dict[str, Any] = Field(default_factory=dict)


class TempoTrace(StrictModel):
    type: Literal["tempo"]
    monotonic_time: float
    perf_time: float
    score_beat: float
    reference_beat: float | None = None
    reference_beat_period_seconds: float | None = Field(default=None, gt=0)
    beat_period_seconds: float = Field(gt=0)
    tempo_bpm: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    observation_accepted: bool | None = None
    observation_decision: (
        Literal[
            "initial_anchor",
            "accepted",
            "insufficient_progress",
            "nonpositive_time",
            "implausible_tempo",
            "repeat_reset",
            "autonomous_lead",
            "warm_start",
            "count_off_timing_seed",
            "orchestra_entry_timing_seed",
            "orchestra_entry_clock",
            "orchestra_entry_acquiring",
            "orchestra_entry_handoff_seed",
            "orchestra_entry_piano_seed",
            "follower_coast",
        ]
        | None
    ) = None
    beat_delta: float | None = None
    time_delta: float | None = None
    candidate_tempo_bpm: float | None = None


class PolicyTrace(StrictModel):
    type: Literal["policy"]
    monotonic_time: float
    score_beat: float
    section_mode: AccompanimentMode
    section_id: str | None = None
    previous_section_mode: AccompanimentMode | None = None
    transition_reason: str | None = None


class ScheduledEventTrace(StrictModel):
    """A reconstructable note-on scheduling or dispatch decision."""

    event_id: str
    score_beat: float
    pitch: int | None = Field(default=None, ge=0, le=127)
    part_id: str
    duration_beats: float = Field(gt=0)
    target_perf_time: float
    reference_beat: float | None = None
    duration_seconds: float | None = Field(default=None, gt=0)
    section_mode: AccompanimentMode
    authority_generation: int = Field(default=0, ge=0)
    committed_at: float | None = None
    sent_at: float | None = None
    lateness_ms: float | None = None


class SchedulerTrace(StrictModel):
    type: Literal["scheduler"]
    monotonic_time: float
    planned_event_ids: list[str]
    dispatched_event_ids: list[str]
    cancelled_event_ids: list[str]
    expired_event_ids: list[str] = Field(default_factory=list)
    authority_generation: int = Field(default=0, ge=0)
    arrival_curve_id: str | None = None
    authority_reason: str | None = None
    reset_reason: Literal["skip", "repeat"] | None = None
    planned_events: list[ScheduledEventTrace] = Field(default_factory=list)
    dispatched_events: list[ScheduledEventTrace] = Field(default_factory=list)
    panic_reason: str | None = None


class ControlTrace(StrictModel):
    """A performer control value accepted by the live engine."""

    type: Literal["control"]
    monotonic_time: float
    control: Literal["orchestra_tempo_bpm", "orchestra_volume", "orchestra_output_advance_ms"]
    requested_value: float
    applied_value: float
    section_mode: AccompanimentMode | None = None


class FreeRegionTrace(StrictModel):
    """Everything that happens to a free region, so a run explains itself.

    Written for arming and for every outcome, including the ones that produce
    no sound. A silent cadenza has several possible causes -- never armed, armed
    but never fired, fired and retracted, gave up -- and they are
    indistinguishable to the listener. Without this the only witness is the
    performer's memory of a status line.
    """

    type: Literal["free_region"]
    monotonic_time: float
    region_id: str
    event: Literal["armed", "not_armed", "handoff", "gave_up"]
    score_beat: float | None = None
    hand_back_beat: float | None = None
    probability: float | None = None
    notes_observed: int | None = None
    detail: str | None = None


class MidiOutputTrace(StrictModel):
    """What the MIDI adapter actually emitted, including note lifetimes."""

    type: Literal["midi_output"]
    monotonic_time: float
    action: Literal[
        "note_on",
        "note_off",
        "release_retime",
        "retrigger_note_off",
        "count_off_note_on",
        "count_off_note_off",
        "channel_volume",
        "master_volume",
        "panic",
    ]
    renderer: Literal["midi", "live_vst"] = "midi"
    zone_id: str | None = None
    instrument_id: str | None = None
    event_id: str | None = None
    channel: int | None = Field(default=None, ge=0, le=15)
    pitch: int | None = Field(default=None, ge=0, le=127)
    velocity: int | None = Field(default=None, ge=0, le=127)
    control: int | None = Field(default=None, ge=0, le=127)
    value: int | None = Field(default=None, ge=0, le=127)
    target_perf_time: float | None = None
    committed_at: float | None = None
    requested_send_time: float | None = None
    scheduled_note_off_time: float | None = None
    output_lateness_ms: float | None = None
    send_call_duration_ms: float | None = Field(default=None, ge=0)
    master_volume: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None


class StateTrace(StrictModel):
    type: Literal["state"]
    status: RuntimeStatus


class StartupTrace(StrictModel):
    """Startup-only diagnostics; elapsed times share the monotonic clock."""

    type: Literal["follower_startup", "runtime_startup"]
    stage: str
    monotonic_time: float
    elapsed_seconds: float = 0.0
    event: str | None = None
    timestamp: str | None = None
    pid: int | None = None
    child_monotonic_time: float | None = None
    child_elapsed_seconds: float | None = None
    stage_elapsed_seconds: float | None = None
    stage_timeout_seconds: float | None = None
    total_timeout_seconds: float | None = None
    score_file: str | None = None
    error_type: str | None = None
    error: str | None = None
    traceback: str | None = None


class LoopTimingTrace(StrictModel):
    """Periodic cadence/work stats for one real-time loop or stage.

    Each concurrent stage (MIDI-input thread, processing loop, per-note
    processing, UI publish) emits one of these per window. ``interval_ms_*``
    describes the gap between iterations (a spike means the loop stalled);
    ``work_ms_*`` describes how long each iteration's work took (a spike is a hot
    spot). Together they show whether load or compute-time variance is starving a
    stage.
    """

    type: Literal["loop_timing"]
    monotonic_time: float
    loop: str
    window_seconds: float = Field(gt=0)
    iterations: int = Field(ge=0)
    interval_ms_median: float | None = Field(default=None, ge=0)
    interval_ms_p95: float | None = Field(default=None, ge=0)
    interval_ms_max: float | None = Field(default=None, ge=0)
    work_ms_median: float | None = Field(default=None, ge=0)
    work_ms_p95: float | None = Field(default=None, ge=0)
    work_ms_max: float | None = Field(default=None, ge=0)


class AudioWorkerTrace(StrictModel):
    """One periodic, cross-process health/performance sample from a VST zone.

    Audio blocks are too frequent to journal individually.  The worker therefore
    emits one bounded summary per window, retaining both total-zone timing and
    per-instrument render cost so a hot plug-in/patch can be localized without
    adding file I/O to the render path.
    """

    type: Literal["audio_worker"]
    monotonic_time: float
    zone_id: str
    action: Literal["startup", "ready", "window", "stopped", "error"]
    instrument_id: str | None = None
    window_seconds: float | None = Field(default=None, gt=0)
    blocks: int = Field(default=0, ge=0)
    block_frames: int = Field(ge=1)
    sample_rate: float = Field(gt=0)
    render_queue_depth: int = Field(default=0, ge=0)
    render_queue_high_watermark: int = Field(default=0, ge=0)
    render_ms_median: float | None = Field(default=None, ge=0)
    render_ms_p95: float | None = Field(default=None, ge=0)
    render_ms_max: float | None = Field(default=None, ge=0)
    render_utilization_p95: float | None = Field(default=None, ge=0)
    render_utilization_max: float | None = Field(default=None, ge=0)
    device_write_ms_p95: float | None = Field(default=None, ge=0)
    command_to_render_ms_p95: float | None = Field(default=None, ge=0)
    late_events: int = Field(default=0, ge=0)
    underruns: int = Field(default=0, ge=0)
    rss_mb: float | None = Field(default=None, ge=0)
    detail: str | None = None
    startup_stage: str | None = None
    error_type: str | None = None
    traceback: str | None = None
    worker_exit_code: int | None = None


class MixStateTrace(StrictModel):
    """An off-path observation of the gain actually applied to an audio block.

    The VST render process only writes numeric gauges. A sampler outside that
    process turns the latest gauge values into these trace rows when detailed
    tracing is enabled, so JSON and file I/O never enter the audio loop.
    """

    type: Literal["mix_state"]
    monotonic_time: float
    rendered_at: float
    renderer: Literal["live_vst", "unit_signal_probe"]
    mix_program_id: str
    mix_program_revision: int = Field(ge=1)
    zone_id: str
    instrument_id: str
    score_tick_start: int = Field(ge=0)
    score_tick_end: int = Field(ge=0)
    route_gain_start: float = Field(ge=0, le=1)
    route_gain_end: float = Field(ge=0, le=1)
    master_gain: float = Field(ge=0, le=1)
    effective_gain_start: float = Field(ge=0, le=1)
    effective_gain_end: float = Field(ge=0, le=1)
    output_rms: float = Field(ge=0)
    output_peak: float = Field(ge=0)


RuntimeTrace = Annotated[
    RuntimeStartTrace
    | CountOffTrace
    | InputTrace
    | FollowerTrace
    | TempoTrace
    | PolicyTrace
    | SchedulerTrace
    | ControlTrace
    | MidiOutputTrace
    | FreeRegionTrace
    | StateTrace
    | LoopTimingTrace
    | StartupTrace
    | AudioWorkerTrace
    | MixStateTrace,
    Field(discriminator="type"),
]


_ALLOWED_PHASE_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.PREPARING: frozenset({RunPhase.LISTENING, RunPhase.FAILED}),
    RunPhase.LISTENING: frozenset({RunPhase.ACTIVE, RunPhase.STOPPING, RunPhase.FAILED}),
    RunPhase.ACTIVE: frozenset({RunPhase.STOPPING, RunPhase.FAILED}),
    RunPhase.STOPPING: frozenset({RunPhase.COMPLETED, RunPhase.FAILED}),
    RunPhase.COMPLETED: frozenset(),
    RunPhase.FAILED: frozenset(),
}


def require_run_phase_transition(current: RunPhase, target: RunPhase) -> None:
    """Reject lifecycle transitions that would make run history ambiguous."""

    if target not in _ALLOWED_PHASE_TRANSITIONS[current]:
        raise ValueError(f"Invalid run phase transition: {current.value} -> {target.value}")


def state_word_for(mode: AccompanimentMode | None, phase: RunPhase) -> LiveStateWord:
    if phase in {RunPhase.PREPARING, RunPhase.LISTENING}:
        return LiveStateWord.LISTENING
    if phase in {RunPhase.STOPPING, RunPhase.COMPLETED, RunPhase.FAILED}:
        return LiveStateWord.SILENT
    return {
        AccompanimentMode.FOLLOW: LiveStateWord.FOLLOWING,
        AccompanimentMode.LEAD: LiveStateWord.LEADING,
        AccompanimentMode.HOLD: LiveStateWord.WAITING,
        AccompanimentMode.STOP: LiveStateWord.SILENT,
        None: LiveStateWord.LISTENING,
    }[mode]
