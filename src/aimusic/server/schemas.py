"""Pydantic models for the FastAPI server."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from aimusic.accompaniment.runtime_contracts import (
    OrchestraRendererStatus,
    RuntimeConfig,
    RuntimeScorePosition,
    RuntimeStatus,
)
from aimusic.takes.lifecycle import (
    AnalysisState,
    LifecycleFailure,
    ProfileMembership,
    UserDisposition,
)
from aimusic.takes.models import TakeCue, TakePlacementHint, TakeStatus


class SessionCreateRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        description="Explicit session identifier. Generated automatically when omitted.",
    )


class SessionResponse(BaseModel):
    session_id: str
    label: str | None = None


class SessionFiles(BaseModel):
    solo: bool = False
    accompaniment: bool = False


class PlaybackFile(BaseModel):
    variant: str
    label: str
    url: str
    size_bytes: int | None = None


class SessionStatusResponse(BaseModel):
    session_id: str
    files: SessionFiles
    playback: list[PlaybackFile]


class AlignmentMetricsResponse(BaseModel):
    pitch_match_count: int
    pitch_mismatch_count: int
    extra_performance_note_count: int
    missing_reference_note_count: int
    output_url: str


class OfflineRenderRequest(BaseModel):
    movement: Literal[2] = 2
    reference_track_marker: str | None = "PIANO SOLO"


class OfflineRenderResponse(SessionStatusResponse):
    metrics: AlignmentMetricsResponse


class MidiEvent(BaseModel):
    type: Literal["note_on", "note_off"]
    note: int = Field(ge=0, le=127)
    velocity: int = Field(ge=0, le=127)
    channel: int = Field(default=0, ge=0, le=15)
    time: float = Field(ge=0.0, description="Seconds since the recording started")


class MidiRecordingRequest(BaseModel):
    tempo_bpm: int = Field(default=120, ge=20, le=400)
    events: list[MidiEvent]


class MidiDevicesResponse(BaseModel):
    inputs: list[str]
    outputs: list[str]
    backend_available: bool


class ScoreBundleHealthResponse(BaseModel):
    """Which DVC-tracked score bundle artifacts haven't been pulled yet.

    Surfaced on the Ready face so a missing `dvc pull` shows up as a warning
    instead of a silent PDF load failure discovered mid-session (rubato#101).
    """

    piece_id: str
    movement: int
    missing_artifacts: list[str]
    findings: list["ScoreHealthFindingResponse"] = []


class ScoreHealthFindingResponse(BaseModel):
    """One disagreement between independently-derived views of the bundle.

    Each derived artifact validates on load, so a bundle can be entirely
    "valid" while its measure grid, PDF geometry and reference warp describe
    different music. These findings exist so that disagreement is visible
    instead of surfacing as a cursor in the wrong bar.
    """

    code: str
    severity: Literal["error", "warning"]
    message: str
    measures: list[str] = []


class ServerShutdownResponse(BaseModel):
    message: str


class ScorePositionResponse(BaseModel):
    """One performer-facing location on the displayed score.

    ``canonical_position`` lives on the containing span/transport because every
    point in one projection has the same coordinate ownership. Mapping review
    state and confidence independently describe evidence quality.
    """

    score_tick: int = Field(ge=0)
    score_beat: float = Field(ge=0.0)
    measure_index: int = Field(ge=0, description="Zero-based display measure index")
    measure_label: str
    beat_in_measure: float = Field(ge=0.0)
    source_seconds: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)


class ScoreSpanResponse(BaseModel):
    start: ScorePositionResponse
    end: ScorePositionResponse
    mapping_id: str
    mapping_review_state: Literal["machine", "reviewed"]
    canonical_position: bool


class ScoreTransportAnchorResponse(BaseModel):
    elapsed_seconds: float = Field(ge=0.0)
    position: ScorePositionResponse
    # Review transports populate these diagnostics so the durable event
    # journal can reconstruct which onset drove a visible cursor landmark.
    source_tick: int | None = Field(default=None, ge=0)
    played_pitches: list[int] = Field(default_factory=list)
    anchor_kind: Literal["transport", "matched_onset", "stop"] = "transport"


class ScoreTransportResponse(BaseModel):
    piece_id: str
    movement: int
    mapping_id: str
    mapping_review_state: Literal["machine", "reviewed"]
    canonical_position: bool
    anchors: list[ScoreTransportAnchorResponse] = Field(min_length=1)


class HardwareJobPhase(str, Enum):
    """Lifecycle phase of the process-wide hardware job slot."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


class LiveStatusResponse(BaseModel):
    phase: HardwareJobPhase
    kind: str
    running: bool
    message: str
    started_at: datetime | None = None
    session_id: str | None = None
    score_transport: ScoreTransportResponse | None = None

    @model_validator(mode="after")
    def validate_phase_running_invariant(self) -> "LiveStatusResponse":
        expected_running = self.phase in {
            HardwareJobPhase.RUNNING,
            HardwareJobPhase.STOPPING,
        }
        if self.running is not expected_running:
            raise ValueError(
                f"Hardware phase {self.phase.value} requires running={expected_running}"
            )
        return self


class HardwareRecordStartRequest(BaseModel):
    input_name: str
    session_id: str | None = None
    target_score_beat: float | None = Field(
        default=None,
        ge=0,
        description="Optional displayed-score entry hint for later take promotion.",
    )
    duration_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)


class HardwareRecordWithCueStartRequest(BaseModel):
    input_name: str
    output_name: str
    session_id: str | None = None
    movement: Literal[2] = 2
    target_score_beat: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Displayed-score quarter beat where the pianist enters. Omit for the "
            "movement's first solo entry."
        ),
    )
    volume: float = Field(default=0.75, ge=0.0, le=1.0)
    output_advance_ms: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Measured Yamaha output advance applied by the live scheduler.",
    )
    duration_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)


class HardwareRecordWithCueStartResponse(LiveStatusResponse):
    """Live status plus the resolved score-anchored cue span."""

    actual_cue_seconds: float
    cue_start_score_beat: float
    entry_score_beat: float
    cue_start_measure: int = Field(ge=1)
    entry_measure: int = Field(ge=1)


class OguriPlaybackRequest(BaseModel):
    output_name: str
    movement: Literal[2] = 2
    volume: float = Field(default=0.75, ge=0.0, le=1.5)
    tempo_bpm: float = Field(
        default=120.0,
        ge=40.0,
        le=200.0,
        description="Exact quarter-note metronome tempo for orchestra playback.",
    )
    duration_seconds: float | None = Field(default=75.0, gt=0.0, le=3600.0)
    orchestra_only: bool = True


class SessionMidiPlaybackRequest(BaseModel):
    output_name: str
    volume: float = Field(default=0.75, ge=0.0, le=1.5)
    duration_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)


class TakeReviewPlaybackRequest(SessionMidiPlaybackRequest):
    """Audition one rendered take variant from a selected score location."""

    variant: Literal["solo", "ensemble"] = "ensemble"
    start_score_beat: float | None = Field(default=None, ge=0.0)


class PanicRequest(BaseModel):
    output_name: str


class AnchorCreateRequest(BaseModel):
    """Add a structural beat anchor at a canonical score tick."""

    score_tick: int = Field(ge=0)
    measure: int = Field(ge=1)
    label: str = ""


class AnchorMoveRequest(BaseModel):
    """Move one structural beat anchor without an add/delete race."""

    new_score_tick: int = Field(ge=0)
    measure: int = Field(ge=1)
    label: str = ""


class AnchorRestoreRequest(BaseModel):
    """Restore one or more anchors as a single undo transaction."""

    anchors: list[AnchorCreateRequest] = Field(min_length=1)


class LiveReplayNote(BaseModel):
    perf_time: float = Field(ge=0)
    pitch: int = Field(ge=0, le=127)
    velocity: int = Field(default=64, ge=0, le=127)
    score_beat: float | None = Field(default=None, ge=0)
    event_id: str | None = None


class LiveReplayStartRequest(BaseModel):
    bundle_id: str = Field(min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    config: RuntimeConfig
    notes: list[LiveReplayNote]
    start_measure: int | None = Field(
        default=None,
        ge=1,
        description="Printed measure whose canonical barline seeds the replay.",
    )


class LiveFollowStartRequest(BaseModel):
    bundle_id: str = Field(min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    input_name: str
    # None/empty means no MIDI output: the orchestra sounds only through live
    # audio zones (e.g. BBCSO to a room speaker), with no MIDI copy to double it.
    output_name: str | None = None
    config: RuntimeConfig
    follower_method: Literal["pthmm"] = "pthmm"
    start_measure: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Printed measure where the orchestra starts immediately and leads "
            "until the score follower locks to live piano evidence."
        ),
    )


class MixAuditionStartRequest(BaseModel):
    """Stream one authored score region through the real live output graph."""

    piece_id: str = Field(default="chopin_op11", pattern=r"^[A-Za-z0-9_-]+$")
    movement: int = Field(default=2, ge=1)
    program_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=1)
    bundle_id: str = Field(default="chopin_op11_movement_2", min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    # Empty means VST/REAPER-only, matching live FOLLOW. The Yamaha continues
    # sounding the pianist locally but receives no orchestral MIDI copy.
    output_name: str = ""
    start_tick: int = Field(ge=0)
    end_tick: int = Field(gt=0)
    tempo_bpm: float = Field(default=76.0, gt=0, le=300)
    volume: float = Field(default=0.20, ge=0, le=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "MixAuditionStartRequest":
        if self.end_tick <= self.start_tick:
            raise ValueError("audition end_tick must be greater than start_tick")
        return self


class LiveTempoUpdateRequest(BaseModel):
    tempo_bpm: float = Field(gt=0, le=300)


class LiveVolumeUpdateRequest(BaseModel):
    volume: float = Field(ge=0, le=1)


class LiveOutputAdvanceUpdateRequest(BaseModel):
    output_advance_ms: float = Field(ge=0, le=100)


class LiveLatencyCalibrationRequest(BaseModel):
    input_name: str = Field(min_length=1)
    output_name: str = Field(min_length=1)
    tempo_bpm: float = Field(default=90.0, ge=30, le=120)
    # Fixed standard protocol: a 4-beat count-in plus 12 measured beats. The
    # engine discards the first 4 as warm-up, so beats must exceed that count-in.
    beats: int = Field(default=16, ge=8, le=32)
    metronome_volume: float = Field(default=0.3, ge=0.0, le=1.0)


class LiveLatencyCalibrationResponse(BaseModel):
    click_count: int
    matched_count: int
    offsets_ms: list[float] = Field(default_factory=list)
    median_offset_ms: float | None = None
    mad_ms: float | None = None
    ci_half_width_ms: float | None = None
    confident: bool = False
    suggested_output_advance_ms: int | None = Field(default=None, ge=0, le=100)
    message: str = ""


class LivePerformancePlanResponse(BaseModel):
    """Performer-facing plan resolved before opening the live MIDI ports."""

    bundle_id: str
    orchestra_starts_automatically: bool
    orchestra_start: RuntimeScorePosition | None = None
    first_solo_entry: RuntimeScorePosition | None = None
    follow_start: RuntimeScorePosition | None = None
    follow_prior_take_count: int = Field(default=0, ge=0)
    initial_tempo_bpm: float = Field(gt=0)
    tempo_source: Literal["performance_profile", "movement_default"]
    rehearsal_take_count: int = Field(ge=0)


class TakeRecordStartRequest(BaseModel):
    input_name: str
    piece_id: str
    movement: int
    duration_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)
    target_score_beat: float | None = Field(
        default=None,
        ge=0.0,
        description="Selected displayed-score entry retained as an alignment hint.",
    )


class TakeRecordStartResponse(BaseModel):
    take_id: str
    status: LiveStatusResponse


# Reused as-is: the resolve card needs exactly what stage-1 localization
# already persists (design doc §2.1 -- "instead of parallel re-declarations").
class TakeCandidateResponse(BaseModel):
    candidate_id: str
    start_score_tick: int
    start_beat: float
    score: float
    start_position: int | None = None


ReviewState = Literal["queued", "running", "ready", "failed"]


class TakeReviewRequest(BaseModel):
    piece_id: str = "chopin_op11"
    movement: Literal[2] = 2


class TakeReviewStatusResponse(BaseModel):
    take_id: str
    state: ReviewState
    job_id: str
    error: str | None = None
    midi_url: str | None = None


class TakeAlignmentSummaryResponse(BaseModel):
    """Performer-facing evidence that a take was actually placed in the score.

    ``note_match_rate`` is an observable fit statistic, not a calibrated
    probability.  ``rating`` translates that statistic plus ambiguity into
    rehearsal language without pretending the aligner knows more than it does.
    """

    rating: Literal["strong", "usable", "needs_review"]
    note_match_rate: float = Field(ge=0.0, le=1.0)
    matched_notes: int = Field(ge=0)
    extra_notes: int = Field(ge=0)
    missing_notes: int = Field(ge=0)
    ambiguous: bool


class TakeResponse(BaseModel):
    take_id: str
    piece_id: str
    movement: int
    recorded_at: datetime
    duration_seconds: float
    note_on_count: int
    input_name: str | None = None
    cue: TakeCue | None = None
    placement_hint: TakePlacementHint | None = None
    status: TakeStatus
    analysis_state: AnalysisState = AnalysisState.CAPTURED
    disposition: UserDisposition = UserDisposition.KEPT
    profile_membership: ProfileMembership
    lifecycle_revision: int
    failure: LifecycleFailure | None = None
    midi_url: str
    candidates: list[TakeCandidateResponse] | None = None
    review: TakeReviewStatusResponse | None = None
    alignment: TakeAlignmentSummaryResponse | None = None
    score_span: ScoreSpanResponse | None = None
    score_transport: ScoreTransportResponse | None = None


class TakeListResponse(BaseModel):
    takes: list[TakeResponse]


class PassageAnalysisRequest(BaseModel):
    piece_id: str = "chopin_op11"
    movement: Literal[2] = 2
    take_ids: list[str] = Field(min_length=1, max_length=20)


class PassageVariancePointResponse(BaseModel):
    score_tick: int = Field(ge=0)
    measure: int = Field(ge=1)
    beat: float = Field(ge=1.0)
    tempo_variation_percent: float = Field(ge=0.0)


class PassageAnalysisResponse(BaseModel):
    take_count: int = Field(ge=0)
    common_cell_count: int = Field(ge=0)
    mean_alignment_quality: float = Field(ge=0.0, le=1.0)
    learned_tempo_bpm: float | None = Field(default=None, gt=0.0)
    typical_tempo_variation_percent: float | None = Field(default=None, ge=0.0)
    typical_velocity_variation: float | None = Field(default=None, ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    one_pass_baseline: float = Field(ge=0.0, le=1.0)
    evidence_multiplier: float = Field(ge=0.0)
    high_variance_points: list[PassageVariancePointResponse]


class TakeResolveRequest(BaseModel):
    piece_id: str
    movement: int
    candidate_id: str | None = None
    candidate_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_candidate(self):
        if self.candidate_id is None and self.candidate_index is None:
            raise ValueError("candidate_id or candidate_index is required")
        return self


# `GET /api/coverage/{movement}` serves `CoverageDoc` (aimusic.takes.models)
# directly as its `response_model` -- design doc §2.1 "coverage response <-
# CoverageDoc shapes" -- instead of a parallel re-declaration here.


class MeasureBoxBeatResponse(BaseModel):
    """A beat's horizontal position on the page, for cursor placement.

    ``x`` is PAGE-NORMALIZED: a fraction of the page width in [0, 1], the same
    frame as the enclosing box's x0/x1, so a well-formed beat satisfies
    x0 <= x <= x1. The client interpolates the cursor between these within a
    measure. See docs/concepts/score-coordinate-systems.md.
    """

    beat_in_measure: float = Field(ge=0.0)
    x: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    # True if a real note sat on this beat; False if interpolated / evenly spaced.
    anchored: bool = True


class MeasureBoxMeasureResponse(BaseModel):
    # x0/x1 are PAGE-NORMALIZED (fraction of page width, [0, 1]); nested beats
    # share that frame and lie within [x0, x1] when the geometry is correct.
    measure: int = Field(ge=1)
    x0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    score_start_tick: int | None = Field(default=None, ge=0)
    score_end_tick: int | None = Field(default=None, gt=0)
    beats: list[MeasureBoxBeatResponse] = Field(default_factory=list)
    # Which staff located these beats ("<part>:<staff>"), or None when no notes
    # were available and even spacing was used.
    beat_staff: str | None = None


class MeasureBoxSystemResponse(BaseModel):
    y0: float
    y1: float
    first_measure: int
    last_measure: int
    measures: list[MeasureBoxMeasureResponse] = Field(default_factory=list)


class MeasureBoxPageResponse(BaseModel):
    page: int
    systems: list[MeasureBoxSystemResponse]


class MeasureBoxesResponse(BaseModel):
    pdf: str
    page_count: int
    level: int
    method: str
    review_state: Literal["machine", "reviewed"] = "reviewed"
    pages: list[MeasureBoxPageResponse]


class ScoreAlignmentCorrectionRequest(BaseModel):
    """Human fallback: join the currently audible source time to one score beat.

    When the performer picked a specific reference onset (Mode B), pass its exact
    ``source_midi_tick`` so the correction *selects* that onset instead of
    re-interpolating it from ``source_seconds`` -- the latter only matches when
    the reference tempo is constant, and silently drifts when it is not.
    """

    source_seconds: float = Field(ge=0.0)
    source_midi_tick: int | None = Field(default=None, ge=0)
    measure: int = Field(ge=1)
    beat_in_measure: float = Field(ge=0.0)
    take_id: str | None = None
    note: str | None = Field(default=None, max_length=500)


class ScoreAlignmentCorrectionResponse(BaseModel):
    correction_id: str
    source_seconds: float = Field(ge=0.0)
    source_midi_tick: int = Field(ge=0)
    score_tick: int = Field(ge=0)
    measure: int = Field(ge=1)
    measure_label: str
    beat_in_measure: float = Field(ge=0.0)
    mapping_id: str
    reanalysis_queued: bool = False


class AlignmentWorklistMeasure(BaseModel):
    """One measure the timing audition should visit, and why."""

    measure_label: str
    reason: Literal["both_disagree", "spine"]
    hypothesis_downbeat_seconds: float | None = None
    disagreement_seconds: float | None = None


class AlignmentWorklistResponse(BaseModel):
    """The shortlist of measures worth listening to, from the cross-check.

    ``available`` is false (with a ``detail``) when the two independent
    cross-check alignments are not present, e.g. a fresh checkout without the
    local MuseScore-derived artifacts; the UI shows that instead of an empty
    list that would read as "nothing to check".
    """

    piece_id: str
    movement: int = Field(ge=1)
    available: bool
    detail: str | None = None
    measures: list[AlignmentWorklistMeasure] = []


class BeatAuditionCandidate(BaseModel):
    """A real reference onset near a beat -- a Mode B pick, not an estimate."""

    source_midi_tick: int = Field(ge=0)
    source_seconds: float = Field(ge=0.0)
    pitch: int = Field(ge=0, le=127)
    delta_seconds: float  # candidate minus the beat's current hypothesis


class BeatAuditionBeat(BaseModel):
    beat_in_measure: float = Field(ge=0.0)
    source_seconds: float = Field(ge=0.0)  # the map's current hypothesis for this beat
    source_midi_tick: int = Field(ge=0)
    # A real matched reference note underlies this beat. False = interpolated:
    # nothing audible to judge and nothing to snap to.
    anchored: bool
    confidence: float = Field(ge=0.0, le=1.0)
    pdf_x: float | None = Field(default=None, ge=0.0, le=1.0)  # page-normalized, geometry only
    candidates: list[BeatAuditionCandidate] = []


class BeatAuditionNote(BaseModel):
    """One reference-solo onset in the audible measure window, for browser playback."""

    pitch: int = Field(ge=0, le=127)
    source_seconds: float = Field(ge=0.0)


class BeatAuditionOnsetGroup(BaseModel):
    """One musician-readable orchestral attack near a disputed boundary."""

    source_midi_tick: int = Field(ge=0)
    source_seconds: float = Field(ge=0.0)
    pitches: list[int] = Field(min_length=1)
    instruments: list[str] = []
    delta_seconds: float  # attack minus the current measure-start hypothesis


class BeatAuditionResponse(BaseModel):
    """Everything the audition needs for one measure of the MIDI-timing check.

    This describes MIDI *timing* (``source_seconds`` / ``source_midi_tick``); the
    only geometry here is ``pdf_x`` per beat and ``pdf_page``, carried through so
    the page can be shown and the beat under test marked. Correcting timing never
    moves geometry.
    """

    piece_id: str
    movement: int = Field(ge=1)
    measure_label: str
    measure_start_seconds: float = Field(ge=0.0)
    measure_end_seconds: float = Field(ge=0.0)  # next downbeat; bounds the audible measure
    beat_period_seconds: float = Field(gt=0.0)  # local tempo, for the count-in metronome
    pdf_page: int | None = Field(default=None, ge=1)  # geometry only: which page shows this bar
    mapping_id: str
    review_state: Literal["machine", "reviewed"]
    beats: list[BeatAuditionBeat]
    # The reference solo notes sounding in this measure, so the browser can play
    # the bar under a beat-click track without parsing MIDI itself.
    solo_notes: list[BeatAuditionNote] = []
    # Rolled/simultaneous orchestral attacks around the boundary let the human
    # answer "which sound starts this measure?" instead of estimating early/late.
    orchestra_groups: list[BeatAuditionOnsetGroup] = []


class AlignmentAuditionPlaybackRequest(BaseModel):
    """Play one mapped Oguri measure and its metronome through MIDI hardware."""

    output_name: str
    volume: float = Field(default=0.75, ge=0.0, le=1.5)
    metronome_volume: float = Field(default=0.3, ge=0.0, le=1.0)


# `WS /api/events` payloads (design doc §2.1/§2.2, issue #85). These were
# hand-built dict literals at four publish sites (`routes.py` x3,
# `aligner.py`'s `_publish_alignment_done`); the only written contract was
# the hand-maintained TS union in `takeEvents.ts`. `TakeEvent`'s
# discriminated union is injected into the OpenAPI document
# (`aimusic.server.app`) so the generated TS/Zod client (#83/#84) covers it.
#
# `type` is deliberately a *required* `Literal` with no default (found by
# issue #83/#84's generated client, not by inspection): a Pydantic field
# with a default is `optional` in the emitted JSON Schema, and hey-api's Zod
# plugin mirrors that as `.optional().default(...)`. Zod v4's
# `discriminatedUnion` can't extract a literal discriminator value through
# that `optional` wrapper, so it read every event's `type` key as
# `undefined` and threw "Duplicate discriminator value" on the very first
# WS message parsed. A discriminator must always be present on the wire
# anyway, so requiring it (and passing it explicitly at every construction
# site) is the correct fix, not a workaround.
class TakeRecordingStarted(BaseModel):
    type: Literal["take:recording_started"]
    take_id: str
    piece_id: str
    movement: int


class TakeRecordingStopped(BaseModel):
    type: Literal["take:recording_stopped"]
    take_id: str
    piece_id: str
    movement: int
    duration_seconds: float


class TakeAlignmentDone(BaseModel):
    type: Literal["take:alignment_done"]
    take_id: str
    piece_id: str
    movement: int
    status: TakeStatus
    analysis_state: AnalysisState = AnalysisState.CAPTURED
    disposition: UserDisposition = UserDisposition.KEPT
    score_start_beat: float | None = None
    score_end_beat: float | None = None
    placement_basis: Literal["selected_passage", "pitch_localization"] | None = None
    target_score_tick: int | None = Field(default=None, ge=0)
    failure_code: str | None = None
    failure_message: str | None = None
    aligner: str | None = None
    match_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    matched_notes: int | None = Field(default=None, ge=0)
    extra_notes: int | None = Field(default=None, ge=0)
    missing_notes: int | None = Field(default=None, ge=0)


class TakeReviewStatusEvent(BaseModel):
    type: Literal["take:review_status"]
    take_id: str
    piece_id: str
    movement: int
    state: ReviewState
    job_id: str
    error: str | None = None
    midi_url: str | None = None


class LiveRuntimeStatusEvent(BaseModel):
    type: Literal["runtime:status"]
    status: RuntimeStatus


class OrchestraRendererStatusEvent(BaseModel):
    type: Literal["runtime:renderer_status"]
    status: OrchestraRendererStatus


class OrchestraRendererPreloadRequest(BaseModel):
    program_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]+$")
    force: bool = False


class HardwareStatusEvent(BaseModel):
    type: Literal["hardware:status"]
    status: LiveStatusResponse


class CoverageMaterialized(BaseModel):
    type: Literal["coverage:materialized"]
    piece_id: str
    movement: int
    revision: int = Field(ge=1)


TakeEvent = Annotated[
    Union[
        TakeRecordingStarted,
        TakeRecordingStopped,
        TakeAlignmentDone,
        TakeReviewStatusEvent,
        LiveRuntimeStatusEvent,
        OrchestraRendererStatusEvent,
        HardwareStatusEvent,
        CoverageMaterialized,
    ],
    Field(discriminator="type"),
]


__all__ = [
    "AlignmentAuditionPlaybackRequest",
    "HardwareRecordStartRequest",
    "HardwareRecordWithCueStartRequest",
    "HardwareJobPhase",
    "HardwareStatusEvent",
    "CoverageMaterialized",
    "LiveStatusResponse",
    "ScorePositionResponse",
    "ScoreSpanResponse",
    "ScoreTransportAnchorResponse",
    "ScoreTransportResponse",
    "MidiDevicesResponse",
    "SessionCreateRequest",
    "SessionResponse",
    "SessionFiles",
    "PlaybackFile",
    "SessionStatusResponse",
    "AlignmentMetricsResponse",
    "OfflineRenderRequest",
    "OfflineRenderResponse",
    "MidiEvent",
    "MidiRecordingRequest",
    "OguriPlaybackRequest",
    "SessionMidiPlaybackRequest",
    "TakeReviewPlaybackRequest",
    "PanicRequest",
    "LiveReplayNote",
    "LiveReplayStartRequest",
    "LiveFollowStartRequest",
    "MixAuditionStartRequest",
    "LiveTempoUpdateRequest",
    "LiveVolumeUpdateRequest",
    "LivePerformancePlanResponse",
    "TakeRecordStartRequest",
    "TakeRecordStartResponse",
    "TakeCandidateResponse",
    "TakeResponse",
    "TakeListResponse",
    "TakeResolveRequest",
    "TakeReviewRequest",
    "TakeReviewStatusResponse",
    "TakeReviewStatusEvent",
    "MeasureBoxBeatResponse",
    "MeasureBoxSystemResponse",
    "MeasureBoxPageResponse",
    "MeasureBoxesResponse",
    "ScoreAlignmentCorrectionRequest",
    "ScoreAlignmentCorrectionResponse",
    "TakeRecordingStarted",
    "TakeRecordingStopped",
    "TakeAlignmentDone",
    "LiveRuntimeStatusEvent",
    "OrchestraRendererPreloadRequest",
    "OrchestraRendererStatusEvent",
    "TakeEvent",
]


class FreeRegionRequest(BaseModel):
    """Mark a span the follower cannot track: cadenza, fermata, improvised lead-in."""

    from_measure: int = Field(ge=1)
    to_measure: int = Field(ge=1)
    label: str = Field(default="Free region", max_length=120)
    detector_model: str | None = None


class FreeRegion(BaseModel):
    from_measure: int
    to_measure: int
    label: str
    hand_back_measure: int
    detector_model: str | None = None
    start_beat: float
    end_beat: float


class FreeRegionsResponse(BaseModel):
    regions: list[FreeRegion]


class CursorSample(BaseModel):
    """One frame in which the rendered cursor changed."""

    t: float
    epoch: str = ""
    wall: float = 0.0
    beat: float | None = None
    measure: int | None = None
    source: str
    projected: bool = False
    page: int | None = None


class CursorTraceRequest(BaseModel):
    samples: list[CursorSample]


class CursorTraceResponse(BaseModel):
    recorded: int
    path: str
