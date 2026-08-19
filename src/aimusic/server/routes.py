"""API endpoint definitions for the local FastAPI server."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable, Literal
from uuid import uuid4

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi import (
    Path as PathParam,
)
from fastapi.responses import FileResponse
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick
from pydantic import ValidationError

from aimusic.accompaniment import beat_anchor, beat_geometry, free_regions
from aimusic.accompaniment.bundle_v2 import DisplayMappingDocument
from aimusic.accompaniment.following import PerformedNote
from aimusic.accompaniment.live_midi import play_alignment_audition
from aimusic.accompaniment.offline_alignment import extract_note_events
from aimusic.accompaniment.offline_render import render_offline_midi
from aimusic.accompaniment.oguri import OguriMovement, oguri_movement
from aimusic.accompaniment.oguri_extract import extract_oguri_movement
from aimusic.accompaniment.rehearsal_position import (
    SOURCE_TICKS_PER_SECOND,
    ProjectedPosition,
    rehearsal_cue_span,
    score_projection,
)
from aimusic.accompaniment.runtime_contracts import (
    OrchestraRendererStatus,
    RuntimeConfig,
    RuntimeStatus,
)
from aimusic.accompaniment.score_fusion import (
    HumanBeatCorrection,
    HumanCorrectionDocument,
    apply_human_corrections,
    load_human_corrections,
    load_performance_beat_map,
    source_midi_tick_at_seconds,
    write_human_corrections,
)
from aimusic.accompaniment.score_health import check_score_bundle
from aimusic.core import paths, recordings
from aimusic.core.event_journal import append_diagnostic
from aimusic.core.events import events
from aimusic.core.time import utc_now
from aimusic.mixing import store as mix_store
from aimusic.mixing.models import (
    MixProgram,
    MixProgramCreate,
    MixProgramList,
    MixRegionCreate,
    MixRegionUpdate,
    MixRevisionRequest,
    MixRoutesUpdate,
    ZoneConfigList,
)
from aimusic.mixing.policy import MixPolicy, compile_mix_policy
from aimusic.mixing.zones import room_zones
from aimusic.server.live_control import live_control
from aimusic.server.live_runtime import live_runtime
from aimusic.server.schemas import (
    AlignmentAuditionPlaybackRequest,
    AlignmentMetricsResponse,
    AlignmentWorklistMeasure,
    AlignmentWorklistResponse,
    AnchorCreateRequest,
    AnchorMoveRequest,
    AnchorRestoreRequest,
    BeatAuditionBeat,
    BeatAuditionCandidate,
    BeatAuditionNote,
    BeatAuditionOnsetGroup,
    BeatAuditionResponse,
    CursorTraceRequest,
    CursorTraceResponse,
    FreeRegion,
    FreeRegionRequest,
    FreeRegionsResponse,
    HardwareRecordStartRequest,
    HardwareRecordWithCueStartRequest,
    HardwareRecordWithCueStartResponse,
    LiveFollowStartRequest,
    LiveLatencyCalibrationRequest,
    LiveLatencyCalibrationResponse,
    LiveOutputAdvanceUpdateRequest,
    LivePerformancePlanResponse,
    LiveReplayStartRequest,
    LiveStatusResponse,
    LiveTempoUpdateRequest,
    LiveVolumeUpdateRequest,
    MeasureBoxesResponse,
    MidiDevicesResponse,
    MidiRecordingRequest,
    MixAuditionStartRequest,
    OfflineRenderRequest,
    OfflineRenderResponse,
    OguriPlaybackRequest,
    OrchestraRendererPreloadRequest,
    PanicRequest,
    PassageAnalysisRequest,
    PassageAnalysisResponse,
    PassageVariancePointResponse,
    PlaybackFile,
    ScoreAlignmentCorrectionRequest,
    ScoreAlignmentCorrectionResponse,
    ScoreBundleHealthResponse,
    ScoreHealthFindingResponse,
    ScorePositionResponse,
    ScoreSpanResponse,
    ScoreTransportAnchorResponse,
    ScoreTransportResponse,
    ServerShutdownResponse,
    SessionCreateRequest,
    SessionFiles,
    SessionMidiPlaybackRequest,
    SessionResponse,
    SessionStatusResponse,
    TakeAlignmentSummaryResponse,
    TakeCandidateResponse,
    TakeListResponse,
    TakeRecordingStarted,
    TakeRecordingStopped,
    TakeRecordStartRequest,
    TakeRecordStartResponse,
    TakeResolveRequest,
    TakeResponse,
    TakeReviewPlaybackRequest,
    TakeReviewRequest,
    TakeReviewStatusResponse,
)
from aimusic.takes import store as take_store
from aimusic.takes.aligner import alignment_worker, load_canonical_note_sequence
from aimusic.takes.coverage import (
    accompaniment_demand_by_measure,
    empty_coverage,
    get_cached_coverage,
    parse_bundle_timeline_measures,
)
from aimusic.takes.lifecycle import AlignedResultV2, AnalysisState
from aimusic.takes.materializer import materialization_worker
from aimusic.takes.models import (
    Anchor,
    AnchorSet,
    CoverageDoc,
    TakeCue,
    TakePlacementHint,
)
from aimusic.takes.passage_analysis import analyze_passage
from aimusic.takes.review import (
    review_midi_path,
    review_preroll_seconds,
    review_status,
    review_worker,
)

router = APIRouter(prefix="/api", tags=["sessions"])
LOGGER = logging.getLogger(__name__)

_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_oguri_extraction_lock = threading.Lock()

# Cue and localization metadata for scratch performances, keyed by recording
# ID. It remains pending after capture stops and is consumed only if the user
# promotes that recording to a durable rehearsal take.
_pending_take_cues: dict[str, dict[str, object]] = {}
_pending_take_placement_hints: dict[str, dict[str, object]] = {}
_pending_take_cues_lock = threading.Lock()
_ephemeral_performance_ids: set[str] = set()
_ephemeral_performance_ids_lock = threading.Lock()
_EPHEMERAL_PERFORMANCE_MARKER = ".rubato-scratch-performance.json"
_score_alignment_correction_lock = threading.Lock()

# Reclaim metadata abandoned by a crashed browser or server-side start that
# never reached explicit promotion. The timeout is deliberately much longer
# than a performance so an ordinary post-performance decision is never raced.
_PENDING_CUE_STALE_SECONDS = 24 * 60 * 60.0


def _prune_stale_pending_cues_locked() -> None:
    """Remove pending cues old enough to be abandoned, not in-flight.

    Caller must hold `_pending_take_cues_lock`. Age is measured from
    `created_at` (epoch seconds), stamped when the cue was registered.
    """

    now = time.time()
    stale_ids = [
        session_id
        for session_id, cue in _pending_take_cues.items()
        if now - cue.get("created_at", now) > _PENDING_CUE_STALE_SECONDS
    ]
    for session_id in stale_ids:
        del _pending_take_cues[session_id]
    stale_hint_ids = [
        session_id
        for session_id, hint in _pending_take_placement_hints.items()
        if now - hint.get("created_at", now) > _PENDING_CUE_STALE_SECONDS
    ]
    for session_id in stale_hint_ids:
        del _pending_take_placement_hints[session_id]


def _keep_recording_from_aborted_job(session_id: str | None) -> None:
    """Recover recordings made by the legacy durable-take endpoints.

    Public rehearsal and live-performance capture IDs are registered as
    ephemeral, so stop/panic/silence preserve their MIDI without silently
    adding it to the interpretation profile. The fallback below remains only
    for older API clients whose session IDs represented durable takes.
    ``session_id`` must be read from ``live_control.status()`` before stopping.
    """

    if not session_id:
        return
    if _is_ephemeral_performance(session_id):
        # Public performance capture is scratch evidence until promotion.
        # The marker is persisted beside the MIDI so a server restart or a
        # stale browser status cannot silently reinterpret it as a durable take.
        return
    recorded = paths.session_dir(session_id) / "solo.mid"
    if not recorded.exists() or not extract_note_events(recorded):
        # Nothing playable was captured (a lead-in cancelled before a note);
        # release the cue so it cannot leak across a long rehearsal session.
        with _pending_take_cues_lock:
            _pending_take_cues.pop(session_id, None)
            _pending_take_placement_hints.pop(session_id, None)
        return
    try:
        _finalize_recorded_take("chopin_op11", 2, session_id, recorded, aborted=True)
    except Exception as exc:
        # Never swallow this. The whole point of the abort path is that a
        # performance is not lost silently -- reporting success while the take
        # failed to register would recreate exactly the bug this fixes.
        LOGGER.exception("Could not keep the recording from aborted take %s", session_id)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Playing was recorded but could not be filed as take '{session_id}': {exc}. "
                f"The MIDI is still on disk at {recorded}."
            ),
        ) from exc


def _register_ephemeral_performance(recording_id: str) -> None:
    with _ephemeral_performance_ids_lock:
        _ephemeral_performance_ids.add(recording_id)
    session_dir = paths.session_dir(recording_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    marker = session_dir / _EPHEMERAL_PERFORMANCE_MARKER
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recording_id": recording_id,
                "kind": "scratch_performance",
                "created_at": utc_now().isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(marker)


def _is_ephemeral_performance(recording_id: str) -> bool:
    with _ephemeral_performance_ids_lock:
        if recording_id in _ephemeral_performance_ids:
            return True
    return (paths.session_dir(recording_id) / _EPHEMERAL_PERFORMANCE_MARKER).is_file()


def _forget_ephemeral_performance(recording_id: str) -> None:
    with _ephemeral_performance_ids_lock:
        _ephemeral_performance_ids.discard(recording_id)


def _finalize_recorded_take(
    piece_id: str,
    movement: int,
    take_id: str,
    recorded_midi_path: Path,
    *,
    aborted: bool = False,
    recording_id: str | None = None,
) -> bool:
    """Register a finished recording and start its alignment, exactly once.

    Both the normal stop endpoint and the abort paths call this. Ordering and
    single-execution are the store's job (`finalize_take` holds a per-take lock
    and consumes the cue inside it), so racing callers cannot clobber each
    other's take.json or double-enqueue alignment.
    """

    def consume_cue() -> tuple[TakeCue | None, TakePlacementHint | None]:
        metadata_id = recording_id or take_id
        with _pending_take_cues_lock:
            cue_data = _pending_take_cues.pop(metadata_id, None)
            hint_data = _pending_take_placement_hints.pop(metadata_id, None)
        return (
            TakeCue(**cue_data) if cue_data is not None else None,
            TakePlacementHint(**hint_data) if hint_data is not None else None,
        )

    take, finalized_now = take_store.finalize_take(
        piece_id,
        movement,
        take_id,
        recorded_midi_path,
        cue_provider=consume_cue,
        aborted=aborted,
    )
    if not finalized_now:
        return False
    events.publish(
        TakeRecordingStopped(
            type="take:recording_stopped",
            take_id=take.take_id,
            piece_id=take.piece_id,
            movement=take.movement,
            duration_seconds=take.duration_seconds,
        )
    )
    on_take_captured(piece_id, movement, take_id)
    return True


@router.post("/sessions", response_model=SessionResponse, operation_id="createSession")
def create_session(payload: SessionCreateRequest) -> SessionResponse:
    session_id = payload.session_id or _generate_session_id()
    _ensure_valid_session_id(session_id)
    paths.session_dir(session_id)
    return SessionResponse(session_id=session_id, label=payload.session_id or session_id)


@router.get("/sessions", response_model=list[SessionResponse], operation_id="listSessions")
def list_sessions() -> list[SessionResponse]:
    labels_by_id = {
        entry.recording_id: entry.original_name for entry in recordings.list_recordings()
    }
    processed_root = paths.processed_root()
    if not processed_root.exists():
        return []
    return [
        SessionResponse(session_id=child.name, label=labels_by_id.get(child.name, child.name))
        for child in sorted(processed_root.iterdir())
        if child.is_dir()
    ]


@router.get(
    "/sessions/{session_id}", response_model=SessionStatusResponse, operation_id="getSession"
)
def get_session(session_id: str) -> SessionStatusResponse:
    resolved = _ensure_valid_session_id(session_id)
    _require_session_dir(resolved)
    return _build_status(resolved)


@router.post(
    "/sessions/{session_id}/upload",
    response_model=SessionStatusResponse,
    operation_id="uploadMidi",
)
async def upload_midi(session_id: str, file: UploadFile = File(...)) -> SessionStatusResponse:
    resolved = _ensure_valid_session_id(session_id)
    if file.filename and not file.filename.lower().endswith(".mid"):
        raise HTTPException(status_code=400, detail="Only .mid files are supported")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    _persist_recording(resolved, contents, file.filename or resolved)
    return _build_status(resolved)


@router.post(
    "/sessions/{session_id}/recordings",
    response_model=SessionStatusResponse,
    operation_id="submitRecording",
)
def submit_recording(session_id: str, payload: MidiRecordingRequest) -> SessionStatusResponse:
    resolved = _ensure_valid_session_id(session_id)
    if not payload.events:
        raise HTTPException(status_code=400, detail="Recording must include at least one event")
    midi_bytes = _events_to_midi(payload)
    _persist_recording(resolved, midi_bytes, f"{session_id}.mid")
    return _build_status(resolved)


@router.get(
    "/sessions/{session_id}/midi/{variant}",
    response_class=FileResponse,
    operation_id="downloadMidi",
)
def download_midi(session_id: str, variant: str) -> FileResponse:
    resolved = _ensure_valid_session_id(session_id)
    path = _resolve_variant_path(resolved, variant)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Variant '{variant}' not available")
    return FileResponse(path, media_type="audio/midi", filename=path.name)


@router.post(
    "/sessions/{session_id}/render-offline",
    response_model=OfflineRenderResponse,
    operation_id="renderOfflineSession",
)
def render_offline_session(
    session_id: str,
    payload: OfflineRenderRequest,
) -> OfflineRenderResponse:
    resolved = _ensure_valid_session_id(session_id)
    raw_dir = _require_session_dir(resolved)
    performance_path = raw_dir / "solo.mid"
    if not performance_path.exists():
        raise HTTPException(status_code=400, detail="Session does not have solo.mid yet")

    movement = oguri_movement(payload.movement)
    _ensure_oguri_derived_files(movement)
    try:
        result = render_offline_midi(
            reference_path=movement.solo_reference_path,
            performance_path=performance_path,
            accompaniment_path=movement.orchestra_accompaniment_path,
            run_id=resolved,
            reference_track_marker=payload.reference_track_marker,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    status = _build_status(resolved)
    summary = result.alignment.summary
    return OfflineRenderResponse(
        session_id=status.session_id,
        files=status.files,
        playback=status.playback,
        metrics=AlignmentMetricsResponse(
            pitch_match_count=summary.pitch_match_count,
            pitch_mismatch_count=summary.pitch_mismatch_count,
            extra_performance_note_count=summary.extra_performance_note_count,
            missing_reference_note_count=summary.missing_reference_note_count,
            output_url=f"/api/sessions/{resolved}/midi/accompaniment",
        ),
    )


@router.get("/midi/devices", response_model=MidiDevicesResponse, operation_id="midiDevices")
def midi_devices() -> MidiDevicesResponse:
    ports = live_control.midi_devices()
    return MidiDevicesResponse(
        inputs=list(ports.inputs),
        outputs=list(ports.outputs),
        backend_available=ports.backend_available,
    )


@router.get("/hardware/status", response_model=LiveStatusResponse, operation_id="hardwareStatus")
def hardware_status() -> LiveStatusResponse:
    return _live_status_response()


@router.post(
    "/hardware/record/start",
    response_model=LiveStatusResponse,
    operation_id="startHardwareRecord",
)
def start_hardware_record(payload: HardwareRecordStartRequest) -> LiveStatusResponse:
    session_id = payload.session_id or _generate_session_id()
    _ensure_valid_session_id(session_id)
    _register_ephemeral_performance(session_id)
    try:
        status = live_control.start_record(
            input_name=payload.input_name,
            session_id=session_id,
            duration_seconds=payload.duration_seconds,
        )
    except RuntimeError as exc:
        _forget_ephemeral_performance(session_id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if payload.target_score_beat is not None:
        with _pending_take_cues_lock:
            _prune_stale_pending_cues_locked()
            _pending_take_placement_hints[session_id] = {
                "target_beat": payload.target_score_beat,
                "source": "selected_passage",
                "created_at": time.time(),
            }
    return _live_status_response(status)


@router.post(
    "/hardware/record-with-cue/start",
    response_model=HardwareRecordWithCueStartResponse,
    operation_id="startHardwareRecordWithCue",
)
def start_hardware_record_with_cue(
    payload: HardwareRecordWithCueStartRequest,
) -> HardwareRecordWithCueStartResponse:
    session_id = payload.session_id or _generate_session_id()
    _ensure_valid_session_id(session_id)
    source = oguri_movement(payload.movement)
    projection = score_projection("chopin_op11", payload.movement)
    target_score_tick = (
        None
        if payload.target_score_beat is None
        else round(payload.target_score_beat * projection.timeline.document.canonical_ppq)
    )
    if target_score_tick is not None and target_score_tick >= projection.timeline.end_tick:
        raise HTTPException(
            status_code=422,
            detail="target_score_beat is beyond the displayed score timeline",
        )
    cue_span = rehearsal_cue_span(
        projection,
        selected_score_tick=target_score_tick,
        solo_reference_path=source.solo_reference_path,
        first_solo_entry_seconds=source.first_solo_entry_seconds,
    )
    start_measure = cue_span.cue_start_measure_index + 1
    try:
        plan = live_runtime.performance_plan(
            bundle_id="chopin_op11_movement_2",
            revision=None,
            start_measure=start_measure,
        )
        cue_start_score_beat = (
            cue_span.cue_start_score_tick / projection.timeline.document.canonical_ppq
        )
        entry_score_beat = (
            cue_span.entry.entry_position.score_tick / projection.timeline.document.canonical_ppq
        )
        actual_cue_seconds = max(
            0.0,
            (entry_score_beat - cue_start_score_beat) * 60.0 / plan.initial_tempo_bpm,
        )
        LOGGER.info(
            "Resolved Cue & Record session_id=%s selected_score_tick=%s "
            "cue_measure=%d cue_score_tick=%d cue_source=%.6fs "
            "entry_measure=%s entry_score_tick=%d entry_beat=%.3f "
            "entry_source=%.6fs pitch=%s lead_in=%.6fs learned_tempo_bpm=%.1f "
            "tempo_source=%s follow_prior_take_count=%d",
            session_id,
            target_score_tick,
            start_measure,
            cue_span.cue_start_score_tick,
            cue_span.cue_start_source_seconds,
            cue_span.entry.entry_position.measure_label,
            cue_span.entry.entry_position.score_tick,
            cue_span.entry.entry_position.beat_in_measure,
            cue_span.entry.entry_source_seconds,
            cue_span.entry.entry_pitch,
            actual_cue_seconds,
            plan.initial_tempo_bpm,
            plan.tempo_source,
            plan.follow_prior_take_count,
        )
        _register_ephemeral_performance(session_id)
        live_runtime.start_follow(
            bundle_id="chopin_op11_movement_2",
            revision=None,
            input_name=payload.input_name,
            output_name=payload.output_name,
            config=RuntimeConfig(
                run_id=session_id,
                initial_tempo_bpm=plan.initial_tempo_bpm,
                orchestra_volume=payload.volume,
                output_advance_ms=payload.output_advance_ms,
            ),
            start_measure=start_measure,
            duration_seconds=payload.duration_seconds,
        )
    except ImportError as exc:
        _forget_ephemeral_performance(session_id)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (KeyError, FileNotFoundError, ValueError, ValidationError) as exc:
        _forget_ephemeral_performance(session_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        _forget_ephemeral_performance(session_id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # The cockpit passes a scratch performance ID. Cue metadata remains keyed
    # by that ID until explicit promotion, when it becomes an alignment hint
    # on the newly created durable take.
    with _pending_take_cues_lock:
        # Do not clear unrelated scratch sessions: a prior performance may be
        # waiting at the post-performance decision boundary.
        _prune_stale_pending_cues_locked()
        _pending_take_cues[session_id] = {
            "kind": "from_top" if target_score_tick is None else "from_position",
            "target_beat": (
                cue_span.entry.entry_position.score_tick
                / projection.timeline.document.canonical_ppq
            ),
            "cue_start_beat": (
                cue_span.cue_start_score_tick / projection.timeline.document.canonical_ppq
            ),
            "cue_seconds": actual_cue_seconds,
            "output_name": payload.output_name,
            "created_at": time.time(),
        }
        if plan.follow_start is not None:
            _pending_take_placement_hints[session_id] = {
                "target_beat": plan.follow_start.score_beat,
                "source": "selected_passage",
                "created_at": time.time(),
            }
    # `start_follow` is owned by LiveControl.start_managed, which already
    # published and retained the authoritative hardware lifecycle status.
    # Return that same state instead of manufacturing a second timestamp and
    # message that can disagree with `/hardware/status` and the websocket.
    live_status = _live_status_response()
    return HardwareRecordWithCueStartResponse(
        **live_status.model_dump(),
        actual_cue_seconds=actual_cue_seconds,
        cue_start_score_beat=cue_start_score_beat,
        entry_score_beat=entry_score_beat,
        cue_start_measure=start_measure,
        entry_measure=cue_span.entry.entry_position.measure_index + 1,
    )


@router.post(
    "/hardware/record/stop",
    response_model=LiveStatusResponse,
    operation_id="stopHardwareRecord",
)
def stop_hardware_record() -> LiveStatusResponse:
    session_id = live_control.status().session_id
    live_control.stop()
    live_control.wait_until_idle(timeout=10.0)
    _keep_recording_from_aborted_job(session_id)
    return _live_status_response()


@router.post("/hardware/play-oguri", response_model=LiveStatusResponse, operation_id="playOguri")
def play_oguri(payload: OguriPlaybackRequest) -> LiveStatusResponse:
    try:
        projection = score_projection("chopin_op11", payload.movement)
        tempo_scale = projection.tempo_scale_for_canonical_bpm(payload.tempo_bpm)
        LOGGER.info(
            "Starting fixed orchestra playback movement=%d tempo_bpm=%.1f "
            "reference_quarter_bpm=%.3f tempo_scale=%.6f",
            payload.movement,
            payload.tempo_bpm,
            projection.inferred_reference_quarter_bpm(),
            tempo_scale,
        )
        status = live_control.start_play_oguri(
            movement=payload.movement,
            output_name=payload.output_name,
            volume=payload.volume,
            tempo_scale=tempo_scale,
            duration_seconds=payload.duration_seconds,
            orchestra_only=payload.orchestra_only,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _live_status_response(status)


@router.post(
    "/sessions/{session_id}/midi/{variant}/play",
    response_model=LiveStatusResponse,
    operation_id="playSessionMidi",
)
def play_session_midi(
    session_id: str,
    variant: str,
    payload: SessionMidiPlaybackRequest,
) -> LiveStatusResponse:
    resolved = _ensure_valid_session_id(session_id)
    source_path = _resolve_variant_path(resolved, variant)
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail=f"Variant '{variant}' not available")
    try:
        status = live_control.start_play_midi_file(
            source_path=source_path,
            output_name=payload.output_name,
            volume=payload.volume,
            duration_seconds=payload.duration_seconds,
            session_id=resolved,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _live_status_response(status)


@router.post("/hardware/stop", response_model=LiveStatusResponse, operation_id="stopHardwareJob")
def stop_hardware_job() -> LiveStatusResponse:
    session_id = live_control.status().session_id
    live_control.stop()
    live_control.wait_until_idle(timeout=10.0)
    _keep_recording_from_aborted_job(session_id)
    return _live_status_response()


@router.post("/hardware/panic", response_model=LiveStatusResponse, operation_id="hardwarePanic")
def hardware_panic(payload: PanicRequest) -> LiveStatusResponse:
    # Silence must stop sound immediately -- but it must not throw away a take
    # that is already on disk.
    session_id = live_control.status().session_id
    status = live_control.panic(payload.output_name)
    live_control.wait_until_idle(timeout=10.0)
    _keep_recording_from_aborted_job(session_id)
    return _live_status_response(status)


@router.post(
    "/server/shutdown",
    response_model=ServerShutdownResponse,
    operation_id="shutdownServer",
)
async def shutdown_server(request: Request) -> ServerShutdownResponse:
    """Stop the local Rubato server from the UI (rubato#100).

    Only works when the app was started via `aimusic.server.app.main()`,
    which stashes the running `uvicorn.Server` on `app.state` -- `uv run
    python -m aimusic.server.app` (dev-server.sh's entrypoint) always does
    this. The actual `should_exit` flip is deferred a beat so this
    response can flush before the process goes down.
    """

    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(
            status_code=503,
            detail="This server process wasn't started with shutdown support",
        )

    async def _shut_down_soon() -> None:
        await asyncio.sleep(0.3)
        server.should_exit = True

    task = asyncio.create_task(_shut_down_soon())
    request.app.state.shutdown_task = task
    return ServerShutdownResponse(message="Shutting down")


@router.get(
    "/runtime/status",
    response_model=RuntimeStatus | None,
    operation_id="liveRuntimeStatus",
)
def live_runtime_status() -> RuntimeStatus | None:
    return live_runtime.status()


@router.get(
    "/runtime/renderer/status",
    response_model=OrchestraRendererStatus,
    operation_id="orchestraRendererStatus",
)
def orchestra_renderer_status() -> OrchestraRendererStatus:
    return live_runtime.renderer_status_for_client()


@router.post(
    "/runtime/renderer/preload",
    response_model=OrchestraRendererStatus,
    operation_id="preloadOrchestraRenderer",
)
def preload_orchestra_renderer(
    payload: OrchestraRendererPreloadRequest,
) -> OrchestraRendererStatus:
    try:
        return live_runtime.preload_renderer(
            program_id=payload.program_id,
            force=payload.force,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runtime/renderer/unload",
    response_model=OrchestraRendererStatus,
    operation_id="unloadOrchestraRenderer",
)
def unload_orchestra_renderer() -> OrchestraRendererStatus:
    try:
        return live_runtime.stop_preloaded_renderer()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/runtime/plan",
    response_model=LivePerformancePlanResponse,
    operation_id="getLivePerformancePlan",
)
def get_live_performance_plan(
    bundle_id: str = "chopin_op11_movement_2",
    revision: str | None = None,
    start_measure: int | None = Query(default=None, ge=1),
) -> LivePerformancePlanResponse:
    try:
        return live_runtime.performance_plan(
            bundle_id=bundle_id,
            revision=revision,
            start_measure=start_measure,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/runtime/replay/start",
    response_model=RuntimeStatus,
    operation_id="startLiveRuntimeReplay",
)
def start_live_runtime_replay(payload: LiveReplayStartRequest) -> RuntimeStatus:
    try:
        return live_runtime.start_replay(
            bundle_id=payload.bundle_id,
            revision=payload.revision,
            config=payload.config,
            start_measure=payload.start_measure,
            notes=tuple(
                PerformedNote(
                    perf_time=note.perf_time,
                    pitch=note.pitch,
                    velocity=note.velocity,
                    score_beat=note.score_beat,
                    event_id=note.event_id,
                )
                for note in payload.notes
            ),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runtime/follow/start",
    response_model=RuntimeStatus,
    operation_id="startLiveRuntimeFollow",
)
def start_live_runtime_follow(payload: LiveFollowStartRequest) -> RuntimeStatus:
    _register_ephemeral_performance(payload.config.run_id)
    started = False
    try:
        status = live_runtime.start_follow(
            bundle_id=payload.bundle_id,
            revision=payload.revision,
            input_name=payload.input_name,
            output_name=payload.output_name,
            config=payload.config,
            follower_method=payload.follower_method,
            start_measure=payload.start_measure,
        )
        started = True
        return status
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        # A failed start never created a managed performance. Successful runs
        # remain registered until explicitly promoted.
        if not started:
            _forget_ephemeral_performance(payload.config.run_id)


@router.post(
    "/mix/audition/start",
    response_model=RuntimeStatus,
    operation_id="startMixAudition",
)
def start_mix_audition(payload: MixAuditionStartRequest) -> RuntimeStatus:
    try:
        return live_runtime.start_mix_audition(
            piece_id=payload.piece_id,
            movement=payload.movement,
            program_id=payload.program_id,
            expected_revision=payload.expected_revision,
            bundle_id=payload.bundle_id,
            revision=payload.revision,
            output_name=payload.output_name,
            start_tick=payload.start_tick,
            end_tick=payload.end_tick,
            tempo_bpm=payload.tempo_bpm,
            volume=payload.volume,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, ImportError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/mix/audition/stop",
    response_model=RuntimeStatus | None,
    operation_id="stopMixAudition",
)
def stop_mix_audition() -> RuntimeStatus | None:
    try:
        return live_runtime.stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/runtime/tempo",
    response_model=RuntimeStatus,
    operation_id="updateLiveRuntimeTempo",
)
def update_live_runtime_tempo(payload: LiveTempoUpdateRequest) -> RuntimeStatus:
    try:
        return live_runtime.update_tempo(payload.tempo_bpm)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runtime/volume",
    response_model=RuntimeStatus,
    operation_id="updateLiveRuntimeVolume",
)
def update_live_runtime_volume(payload: LiveVolumeUpdateRequest) -> RuntimeStatus:
    try:
        return live_runtime.update_volume(payload.volume)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runtime/output-advance",
    response_model=RuntimeStatus,
    operation_id="updateLiveRuntimeOutputAdvance",
)
def update_live_runtime_output_advance(
    payload: LiveOutputAdvanceUpdateRequest,
) -> RuntimeStatus:
    try:
        return live_runtime.update_output_advance(payload.output_advance_ms)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runtime/calibrate/latency",
    response_model=LiveLatencyCalibrationResponse,
    operation_id="calibrateLiveRuntimeLatency",
)
def calibrate_live_runtime_latency(
    payload: LiveLatencyCalibrationRequest,
) -> LiveLatencyCalibrationResponse:
    try:
        result = live_runtime.calibrate_output_latency(
            input_name=payload.input_name,
            output_name=payload.output_name,
            tempo_bpm=payload.tempo_bpm,
            beats=payload.beats,
            metronome_volume=payload.metronome_volume,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return LiveLatencyCalibrationResponse(
        click_count=result.click_count,
        matched_count=result.matched_count,
        offsets_ms=list(result.offsets_ms),
        median_offset_ms=result.median_offset_ms,
        mad_ms=result.mad_ms,
        ci_half_width_ms=result.ci_half_width_ms,
        confident=result.confident,
        suggested_output_advance_ms=result.suggested_output_advance_ms,
        message=result.message,
    )


@router.post(
    "/runtime/stop",
    response_model=RuntimeStatus | None,
    operation_id="stopLiveRuntime",
)
def stop_live_runtime() -> RuntimeStatus | None:
    try:
        return live_runtime.stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/performances/{recording_id}/keep",
    response_model=TakeResponse,
    operation_id="keepPerformanceRecording",
)
def keep_performance_recording(
    recording_id: str,
    piece_id: str = "chopin_op11",
    movement: int = 2,
) -> TakeResponse:
    """Promote an ephemeral live capture into durable rehearsal evidence."""

    recording_id = _ensure_valid_session_id(recording_id)
    hardware_status = live_control.status()
    if hardware_status.running and hardware_status.session_id == recording_id:
        raise HTTPException(
            status_code=409,
            detail="Stop the live performance before keeping its recording",
        )
    recorded_midi_path = paths.session_dir(recording_id) / "solo.mid"
    if not recorded_midi_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No finished performance recording found for '{recording_id}'",
        )
    # Runtime IDs and take IDs are deliberately different namespaces. Keep the
    # promotion deterministic so retrying the request is idempotent, while
    # satisfying the take store's durable `t…` identity contract.
    take_id = recording_id if recording_id.startswith("t") else f"t-{recording_id}"
    _finalize_recorded_take(
        piece_id,
        movement,
        take_id,
        recorded_midi_path,
        recording_id=recording_id,
    )
    _forget_ephemeral_performance(recording_id)
    return _take_response(take_store.get_take(piece_id, movement, take_id))


@router.websocket("/events")
async def take_events_ws(websocket: WebSocket) -> None:
    """Push typed take transitions and live runtime status to the cockpit.

    One-way broadcast -- the client sends nothing we act on; incoming
    messages (if any) are ignored.
    """

    await websocket.accept()
    queue = events.subscribe()
    receive_task = asyncio.create_task(websocket.receive())
    event_task = asyncio.create_task(queue.get())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive_task, event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                inbound = receive_task.result()
                if inbound.get("type") == "websocket.disconnect":
                    break
                receive_task = asyncio.create_task(websocket.receive())
            if event_task in done:
                message = event_task.result()
                if message is None:
                    break
                await websocket.send_text(message)
                event_task = asyncio.create_task(queue.get())
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        # RuntimeError covers a send racing a connection that closed by some
        # path other than the clean disconnect Starlette translates to
        # WebSocketDisconnect -- either way this is just one client going
        # away, not a server-side failure worth propagating.
        pass
    finally:
        for task in (receive_task, event_task):
            if not task.done():
                task.cancel()
        try:
            await asyncio.gather(receive_task, event_task, return_exceptions=True)
        except asyncio.CancelledError:
            # Starlette/AnyIO may cancel the connection task during teardown;
            # both child tasks were already cancelled above, so cleanup can
            # still complete without leaking the subscription.
            pass
        events.unsubscribe(queue)


def _generate_session_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("session_%Y%m%d_%H%M%S")
    suffix = uuid4().hex[:6]
    return f"{timestamp}_{suffix}"


def _live_status_response(status=None) -> LiveStatusResponse:
    current = status or live_control.status()
    return LiveStatusResponse(
        phase=current.phase,
        kind=current.kind,
        running=current.running,
        message=current.message,
        started_at=current.started_at,
        session_id=current.session_id,
        score_transport=getattr(current, "score_transport", None),
    )


def _position_response(position: ProjectedPosition) -> ScorePositionResponse:
    return ScorePositionResponse(**position.__dict__)


def _score_span_response(
    piece_id: str,
    movement: int,
    aligned: AlignedResultV2,
) -> ScoreSpanResponse:
    projection = score_projection(piece_id, movement)
    # A take's observed span ends at its final matched musical onset.  Armed
    # time after that onset may be a held note or silence before Stop; tempo
    # extrapolation across that interval used to paint unplayed measures and
    # make the cursor crawl beyond the actual phrase ending.
    performed_end_tick = aligned.end_score_tick
    if aligned.coordinate_system == "canonical_score":
        return ScoreSpanResponse(
            start=_aligned_position(
                projection,
                aligned.start_score_tick,
                aligned.start_reference_tick,
            ),
            end=_aligned_position(
                projection,
                performed_end_tick,
                aligned.end_reference_tick,
            ),
            mapping_id=aligned.mapping_id or projection.mapping_id,
            mapping_review_state=projection.mapping_review_state.value,
            canonical_position=projection.canonical_position,
        )
    return ScoreSpanResponse(
        start=_position_response(projection.position_at_source_tick(aligned.start_score_tick)),
        end=_position_response(projection.position_at_source_tick(performed_end_tick)),
        mapping_id=projection.mapping_id,
        mapping_review_state=projection.mapping_review_state.value,
        canonical_position=projection.canonical_position,
    )


def _aligned_position(
    projection,
    canonical_tick: int,
    reference_tick: int | None,
) -> ScorePositionResponse:
    """Render a canonical take coordinate without projecting it a second time."""

    tick = max(0, min(canonical_tick, projection.timeline.end_tick - 1))
    position = projection.timeline.position_at(tick)
    confidence = (
        projection.position_at_source_tick(reference_tick).confidence
        if reference_tick is not None
        else 0.0
    )
    return ScorePositionResponse(
        score_tick=position.score_tick,
        score_beat=position.score_tick / projection.timeline.document.canonical_ppq,
        measure_index=position.measure_index,
        measure_label=position.measure_label,
        beat_in_measure=position.beat_in_measure,
        source_seconds=(reference_tick or 0) / SOURCE_TICKS_PER_SECOND,
        confidence=confidence,
    )


def _review_score_transport(
    piece_id: str,
    movement: int,
    take_id: str,
) -> ScoreTransportResponse:
    aligned = take_store.get_aligned_result_v2(piece_id, movement, take_id)
    if aligned is None:
        raise ValueError(f"take {take_id} has no authoritative alignment")
    projection = score_projection(piece_id, movement)
    take = take_store.get_take_v2(piece_id, movement, take_id)
    preroll_seconds = review_preroll_seconds(piece_id, movement, take_id)
    # Hold the cursor at the last matched onset through any trailing sustain
    # or silence.  No new score position has been observed after that anchor.
    performed_end_tick = aligned.end_score_tick

    # Chord notes can produce near-identical timing anchors whose raw aligned
    # ticks briefly regress.  A performer cursor cannot move backward within a
    # chord, so collapse equal-time points and retain a monotonic score path.
    if aligned.coordinate_system == "canonical_score":
        raw_points = [
            (0.0, aligned.start_score_tick, aligned.start_reference_tick),
            *(
                (
                    max(0.0, point.take_seconds - preroll_seconds),
                    point.score_tick,
                    point.reference_tick,
                )
                for point in aligned.timing_map
                if point.take_seconds + 1e-6 >= preroll_seconds
            ),
        ]
        performed_end_reference_tick = aligned.end_reference_tick
    else:
        raw_points = [
            (
                0.0,
                projection.position_at_source_tick(aligned.start_score_tick).score_tick,
                aligned.start_score_tick,
            ),
            *(
                (
                    max(0.0, point.take_seconds - preroll_seconds),
                    projection.position_at_source_tick(point.score_tick).score_tick,
                    point.score_tick,
                )
                for point in aligned.timing_map
                if point.take_seconds + 1e-6 >= preroll_seconds
            ),
        ]
        performed_end_tick = projection.position_at_source_tick(aligned.end_score_tick).score_tick
        performed_end_reference_tick = aligned.end_score_tick
    matched_elapsed = {
        max(0.0, point.take_seconds - preroll_seconds)
        for point in aligned.timing_map
        if point.take_seconds + 1e-6 >= preroll_seconds
    }
    anchors_by_elapsed: dict[float, tuple[int, int | None]] = {}
    furthest_tick = aligned.start_score_tick
    if aligned.coordinate_system != "canonical_score":
        furthest_tick = raw_points[0][1]
    for elapsed, score_tick, reference_tick in sorted(
        raw_points, key=lambda item: (item[0], item[1])
    ):
        furthest_tick = max(furthest_tick, score_tick)
        anchors_by_elapsed[elapsed] = (furthest_tick, reference_tick)
    review_duration = max(0.0, take.duration_seconds - preroll_seconds)
    anchors_by_elapsed[review_duration] = (
        max(furthest_tick, performed_end_tick),
        performed_end_reference_tick,
    )

    take_midi = paths.take_dir(piece_id, movement, take_id, create=False) / take.midi_path
    performed_notes = sorted(
        load_canonical_note_sequence(take_midi),
        key=lambda note: note.time_seconds,
    )
    note_times = [note.time_seconds for note in performed_notes]

    def played_pitches(elapsed: float) -> list[int]:
        # Aligned points originate in this same MIDI parser. A 30 ms window
        # also keeps rolled chord notes together for human-readable traces.
        # Binary-search the sorted onset index: this path has one lookup per
        # alignment anchor and must remain cheap for full-length takes.
        raw_elapsed = elapsed + preroll_seconds
        left = bisect_left(note_times, raw_elapsed - 0.03)
        right = bisect_right(note_times, raw_elapsed + 0.03)
        near = performed_notes[left:right]
        return sorted({note.pitch for note in near})

    anchors = [
        ScoreTransportAnchorResponse(
            elapsed_seconds=elapsed,
            position=_aligned_position(projection, score_tick, reference_tick),
            source_tick=reference_tick,
            played_pitches=played_pitches(elapsed),
            anchor_kind=(
                "stop"
                if elapsed == review_duration
                else "matched_onset"
                if elapsed in matched_elapsed
                else "transport"
            ),
        )
        for elapsed, (score_tick, reference_tick) in sorted(anchors_by_elapsed.items())
    ]
    return ScoreTransportResponse(
        piece_id=piece_id,
        movement=movement,
        mapping_id=projection.mapping_id,
        mapping_review_state=projection.mapping_review_state.value,
        canonical_position=projection.canonical_position,
        anchors=anchors,
    )


def _slice_score_transport(
    transport: ScoreTransportResponse,
    start_score_beat: float,
) -> tuple[float, ScoreTransportResponse]:
    """Start an aligned review at the selected displayed-score beat.

    The review MIDI and its cursor transport share take-relative seconds.
    Interpolating once at this boundary keeps hardware playback, browser
    preview, and the red score cursor on the same selected location.
    """

    target_tick = round(start_score_beat * take_store.CANONICAL_PPQ)
    anchors = transport.anchors
    first_tick = anchors[0].position.score_tick
    final_tick = anchors[-1].position.score_tick
    if target_tick < first_tick:
        return 0.0, transport
    if target_tick > final_tick:
        raise ValueError("selected score location is outside this take")

    left = anchors[0]
    right = anchors[0]
    for candidate in anchors:
        if candidate.position.score_tick == target_tick:
            left = right = candidate
            break
        if candidate.position.score_tick < target_tick:
            left = candidate
            continue
        right = candidate
        break

    tick_span = right.position.score_tick - left.position.score_tick
    ratio = 0.0 if tick_span <= 0 else (target_tick - left.position.score_tick) / tick_span
    start_elapsed = left.elapsed_seconds + ratio * (right.elapsed_seconds - left.elapsed_seconds)
    timeline = score_projection(transport.piece_id, transport.movement).timeline
    timeline_position = timeline.position_at(min(target_tick, timeline.end_tick - 1))
    start_position = ScorePositionResponse(
        score_tick=timeline_position.score_tick,
        score_beat=timeline_position.score_tick / timeline.document.canonical_ppq,
        measure_index=timeline_position.measure_index,
        measure_label=timeline_position.measure_label,
        beat_in_measure=timeline_position.beat_in_measure,
        source_seconds=left.position.source_seconds
        + ratio * (right.position.source_seconds - left.position.source_seconds),
        confidence=min(left.position.confidence, right.position.confidence),
    )
    exact_anchor = left if left.position.score_tick == target_tick else None
    sliced_anchors = [
        ScoreTransportAnchorResponse(
            elapsed_seconds=0.0,
            position=start_position,
            source_tick=exact_anchor.source_tick if exact_anchor else None,
            played_pitches=exact_anchor.played_pitches if exact_anchor else [],
            anchor_kind="transport",
        )
    ]
    sliced_anchors.extend(
        anchor.model_copy(update={"elapsed_seconds": anchor.elapsed_seconds - start_elapsed})
        for anchor in anchors
        if anchor.elapsed_seconds > start_elapsed
    )
    return start_elapsed, transport.model_copy(update={"anchors": sliced_anchors})


def _ensure_valid_session_id(session_id: str) -> str:
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(
            status_code=422,
            detail="Session ID must be alphanumeric, '-', or '_' only",
        )
    return session_id


def _ensure_valid_take_id(take_id: str) -> str:
    # take_id is joined directly into a filesystem path (paths.take_dir),
    # same class of risk as session_id above -- reject anything that isn't
    # a plain take_id before it reaches path construction.
    if not _SESSION_ID_PATTERN.fullmatch(take_id):
        raise HTTPException(
            status_code=422,
            detail="take_id must be alphanumeric, '-', or '_' only",
        )
    return take_id


def _persist_recording(recording_id: str, contents: bytes, original_name: str) -> None:
    raw_path = paths.recording_file_path(recording_id)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(contents)

    processed_dir = paths.session_dir(recording_id)
    (processed_dir / "solo.mid").write_bytes(contents)
    if recordings.get_recording(recording_id) is None:
        recordings.register_recording(recording_id, original_name)


def _build_status(session_id: str) -> SessionStatusResponse:
    raw_dir = _require_session_dir(session_id)
    accompaniment_path = paths.run_dir(session_id, create=False) / "output" / "accompaniment.mid"
    files = SessionFiles(
        solo=(raw_dir / "solo.mid").exists(),
        accompaniment=accompaniment_path.exists(),
    )
    playback = list(_playback_files(session_id, raw_dir))
    return SessionStatusResponse(session_id=session_id, files=files, playback=playback)


def _playback_files(session_id: str, raw_dir: Path) -> Iterable[PlaybackFile]:
    for variant, (label, path) in _variant_mapping(session_id, raw_dir).items():
        if path.exists():
            yield PlaybackFile(
                variant=variant,
                label=label,
                url=f"/api/sessions/{session_id}/midi/{variant}",
                size_bytes=path.stat().st_size,
            )


def _resolve_variant_path(session_id: str, variant: str) -> Path:
    raw_dir = _require_session_dir(session_id)
    variant_map = _variant_mapping(session_id, raw_dir)
    if variant not in variant_map:
        raise HTTPException(status_code=404, detail=f"Unknown variant '{variant}'")
    return variant_map[variant][1]


def _variant_mapping(session_id: str, raw_dir: Path) -> dict[str, tuple[str, Path]]:
    run_root = paths.run_dir(session_id, create=False)
    return {
        "solo": ("Solo recording", raw_dir / "solo.mid"),
        "accompaniment": ("Accompaniment", run_root / "output" / "accompaniment.mid"),
    }


def _events_to_midi(payload: MidiRecordingRequest) -> bytes:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    tempo = bpm2tempo(payload.tempo_bpm)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))

    events = sorted(payload.events, key=lambda evt: evt.time)
    current_time = 0.0
    for event in events:
        delta_seconds = max(event.time - current_time, 0.0)
        delta_ticks = int(round(second2tick(delta_seconds, midi.ticks_per_beat, tempo)))
        track.append(
            Message(
                event.type,
                note=event.note,
                velocity=event.velocity,
                channel=event.channel,
                time=delta_ticks,
            )
        )
        current_time = event.time

    midi.tracks.append(track)
    buffer = io.BytesIO()
    midi.save(file=buffer)
    buffer.seek(0)
    return buffer.read()


def _ensure_oguri_derived_files(movement: OguriMovement) -> None:
    if movement.solo_reference_path.exists() and movement.orchestra_accompaniment_path.exists():
        return
    with _oguri_extraction_lock:
        if movement.solo_reference_path.exists() and movement.orchestra_accompaniment_path.exists():
            return
        if not movement.local_path.exists():
            raise HTTPException(
                status_code=500,
                detail=f"Oguri source MIDI is missing: {movement.local_path}",
            )
        extract_oguri_movement(movement)


def _require_session_dir(session_id: str) -> Path:
    raw_dir = paths.processed_recording_dir(session_id)
    if not raw_dir.exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return raw_dir


ON_TAKE_CAPTURED_HOOKS: list[callable] = [
    lambda piece_id, movement, take_id: alignment_worker.enqueue(piece_id, movement, take_id)
]


def on_take_captured(piece_id: str, movement: int, take_id: str) -> None:
    for hook in ON_TAKE_CAPTURED_HOOKS:
        try:
            hook(piece_id, movement, take_id)
        except Exception:
            logging.exception("Take captured hook failed")


@router.post(
    "/takes/record", response_model=TakeRecordStartResponse, operation_id="startTakeRecord"
)
def start_take_record(payload: TakeRecordStartRequest) -> TakeRecordStartResponse:
    take_id = take_store.generate_take_id()
    if payload.target_score_beat is not None:
        projection = score_projection(payload.piece_id, payload.movement)
        target_score_tick = round(
            payload.target_score_beat * projection.timeline.document.canonical_ppq
        )
        if target_score_tick >= projection.timeline.end_tick:
            raise HTTPException(
                status_code=422,
                detail="target_score_beat is beyond the displayed score timeline",
            )
    try:
        status = live_control.start_record(
            input_name=payload.input_name,
            session_id=take_id,
            duration_seconds=payload.duration_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if payload.target_score_beat is not None:
        with _pending_take_cues_lock:
            _prune_stale_pending_cues_locked()
            _pending_take_placement_hints[take_id] = {
                "target_beat": payload.target_score_beat,
                "source": "selected_passage",
                "created_at": time.time(),
            }
    events.publish(
        TakeRecordingStarted(
            type="take:recording_started",
            take_id=take_id,
            piece_id=payload.piece_id,
            movement=payload.movement,
        )
    )
    return TakeRecordStartResponse(take_id=take_id, status=_live_status_response(status))


@router.post("/takes/{take_id}/stop", response_model=TakeResponse, operation_id="stopTakeRecord")
def stop_take_record(
    take_id: str,
    piece_id: str = "chopin_op11",
    movement: int = 2,
) -> TakeResponse:
    take_id = _ensure_valid_take_id(take_id)
    live_control.stop()
    status = live_control.wait_until_idle(timeout=10.0)
    if status.running:
        raise HTTPException(status_code=503, detail="Recording did not stop in time")

    recorded_midi_path = paths.session_dir(take_id) / "solo.mid"
    if not recorded_midi_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No finished recording found for take '{take_id}'",
        )

    _finalize_recorded_take(piece_id, movement, take_id, recorded_midi_path)
    return _take_response(take_store.get_take(piece_id, movement, take_id))


@router.get("/takes", response_model=TakeListResponse, operation_id="listTakes")
def list_takes(piece_id: str, movement: int) -> TakeListResponse:
    takes = take_store.list_takes(piece_id, movement)
    return TakeListResponse(takes=[_take_response(take) for take in takes])


@router.post(
    "/takes/passage-analysis",
    response_model=PassageAnalysisResponse,
    operation_id="analyzePassageTakes",
)
def analyze_passage_takes(payload: PassageAnalysisRequest) -> PassageAnalysisResponse:
    alignments = []
    for take_id in dict.fromkeys(payload.take_ids):
        _ensure_valid_take_id(take_id)
        try:
            take = take_store.get_take_v2(payload.piece_id, payload.movement, take_id)
        except take_store.TakeNotFoundError:
            continue
        if take.disposition.value != "kept" or take.analysis_state != AnalysisState.ALIGNED:
            continue
        aligned = take_store.get_aligned_result_v2(payload.piece_id, payload.movement, take_id)
        if aligned is not None:
            alignments.append(aligned)
    projection = score_projection(payload.piece_id, payload.movement)
    result = analyze_passage(alignments)
    variance_points = []
    for point in result.high_variance_points:
        position = projection.timeline.position_at(point.score_tick)
        variance_points.append(
            PassageVariancePointResponse(
                score_tick=point.score_tick,
                measure=position.measure_index + 1,
                beat=round(position.beat_in_measure + 1, 1),
                tempo_variation_percent=point.tempo_variation_percent,
            )
        )
    return PassageAnalysisResponse(
        take_count=result.take_count,
        common_cell_count=result.common_cell_count,
        mean_alignment_quality=result.mean_alignment_quality,
        learned_tempo_bpm=result.learned_tempo_bpm,
        typical_tempo_variation_percent=result.typical_tempo_variation_percent,
        typical_velocity_variation=result.typical_velocity_variation,
        confidence=result.confidence,
        one_pass_baseline=result.one_pass_baseline,
        evidence_multiplier=result.evidence_multiplier,
        high_variance_points=variance_points,
    )


@router.post(
    "/takes/{take_id}/review",
    response_model=TakeReviewStatusResponse,
    status_code=202,
    operation_id="prepareTakeReview",
)
def prepare_take_review(take_id: str, payload: TakeReviewRequest) -> TakeReviewStatusResponse:
    take_id = _ensure_valid_take_id(take_id)
    if not _SESSION_ID_PATTERN.fullmatch(payload.piece_id):
        raise HTTPException(status_code=422, detail="Invalid piece_id")
    try:
        review_worker.enqueue(payload.piece_id, payload.movement, take_id)
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current = review_status(payload.piece_id, payload.movement, take_id)
    if current is None:  # pragma: no cover - enqueue always persists first
        raise HTTPException(status_code=500, detail="Review job was not persisted")
    return current


@router.get(
    "/takes/{take_id}/review",
    response_model=TakeReviewStatusResponse,
    operation_id="getTakeReview",
)
def get_take_review(
    take_id: str, piece_id: str = "chopin_op11", movement: int = 2
) -> TakeReviewStatusResponse:
    take_id = _ensure_valid_take_id(take_id)
    current = review_status(piece_id, movement, take_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Take '{take_id}' has no review yet")
    return current


@router.get(
    "/takes/{take_id}/score-transport",
    response_model=ScoreTransportResponse,
    operation_id="getTakeScoreTransport",
)
def get_take_score_transport(
    take_id: str, piece_id: str = "chopin_op11", movement: int = 2
) -> ScoreTransportResponse:
    """Build the detailed cursor path only when a review consumer needs it.

    Take-list refreshes are intentionally metadata-only. Parsing every take's
    MIDI while serializing that list blocks the local UI as the take bank grows.
    """

    take_id = _ensure_valid_take_id(take_id)
    try:
        return _review_score_transport(piece_id, movement, take_id)
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/takes/{take_id}/review/midi",
    response_class=FileResponse,
    operation_id="downloadTakeReviewMidi",
)
def download_take_review_midi(
    take_id: str,
    piece_id: str = "chopin_op11",
    movement: int = 2,
    variant: Literal["solo", "ensemble"] = "ensemble",
) -> FileResponse:
    take_id = _ensure_valid_take_id(take_id)
    current = review_status(piece_id, movement, take_id)
    path = review_midi_path(take_id, variant)
    if current is None or current.state != "ready" or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Review for take '{take_id}' is not ready")
    suffix = "take-only" if variant == "solo" else "take-with-orchestra"
    return FileResponse(path, media_type="audio/midi", filename=f"{take_id}-{suffix}.mid")


@router.post(
    "/takes/{take_id}/review/play",
    response_model=LiveStatusResponse,
    operation_id="playTakeReview",
)
def play_take_review(
    take_id: str,
    payload: TakeReviewPlaybackRequest,
    piece_id: str = "chopin_op11",
    movement: int = 2,
) -> LiveStatusResponse:
    take_id = _ensure_valid_take_id(take_id)
    current = review_status(piece_id, movement, take_id)
    source_path = review_midi_path(take_id, payload.variant)
    if current is None or current.state != "ready" or not source_path.is_file():
        raise HTTPException(status_code=404, detail=f"Review for take '{take_id}' is not ready")
    try:
        attach_transport = getattr(live_control, "attach_score_transport", None)
        score_transport = None
        start_seconds = 0.0
        if payload.start_score_beat is not None or callable(attach_transport):
            score_transport = _review_score_transport(piece_id, movement, take_id)
            if payload.start_score_beat is not None:
                start_seconds, score_transport = _slice_score_transport(
                    score_transport, payload.start_score_beat
                )
        status = live_control.start_play_midi_file(
            source_path=source_path,
            output_name=payload.output_name,
            volume=payload.volume,
            duration_seconds=payload.duration_seconds,
            start_seconds=start_seconds,
            session_id=take_id,
        )
        if callable(attach_transport) and score_transport is not None:
            status = attach_transport(score_transport)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _live_status_response(status)


@router.delete("/takes/{take_id}", response_model=TakeResponse, operation_id="deleteTake")
def delete_take(
    take_id: str,
    piece_id: str,
    movement: int,
) -> TakeResponse:
    take_id = _ensure_valid_take_id(take_id)
    try:
        take = take_store.discard_take(piece_id, movement, take_id)
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    materialization_worker.enqueue(piece_id, movement, take_id)
    return _take_response(take)


@router.post("/takes/{take_id}/restore", response_model=TakeResponse, operation_id="restoreTake")
def restore_take(take_id: str, piece_id: str, movement: int) -> TakeResponse:
    take_id = _ensure_valid_take_id(take_id)
    try:
        take = take_store.restore_take(piece_id, movement, take_id)
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    materialization_worker.enqueue(piece_id, movement, take_id)
    return _take_response(take)


@router.post("/takes/{take_id}/resolve", response_model=TakeResponse, operation_id="resolveTake")
def resolve_take_route(take_id: str, payload: TakeResolveRequest) -> TakeResponse:
    """Resolve an `ambiguous` take by choosing a localization candidate
    (design doc §3.2/§4.3: "It was here / It was there",
    `POST /api/takes/{id}/resolve`).

    Validates synchronously (so a bad candidate_index or an already-resolved
    take gets an immediate 404/422) but performs the actual re-alignment on
    the background `AlignmentWorker`, same as the original alignment pass --
    it's exactly as CPU-bound, so it gets the same never-block-the-request-
    thread treatment (design doc §2.7). Returns the take with status
    `aligning`; the next take-list refresh picks up the final result.
    """

    take_id = _ensure_valid_take_id(take_id)
    if not _SESSION_ID_PATTERN.fullmatch(payload.piece_id):
        raise HTTPException(
            status_code=422,
            detail="piece_id must be alphanumeric, '-', or '_' only",
        )
    try:
        take = alignment_worker.enqueue_resolve(
            payload.piece_id,
            payload.movement,
            take_id,
            payload.candidate_id if payload.candidate_id is not None else payload.candidate_index,
        )
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _take_response(take)


@router.post(
    "/takes/{take_id}/alignment/retry",
    response_model=TakeResponse,
    status_code=202,
    operation_id="retryTakeAlignment",
)
def retry_take_alignment(take_id: str, piece_id: str, movement: int) -> TakeResponse:
    """Retry a retained take after a retryable analysis software failure."""

    take_id = _ensure_valid_take_id(take_id)
    try:
        lifecycle = take_store.get_take_v2(piece_id, movement, take_id)
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if lifecycle.analysis_state != AnalysisState.FAILED:
        raise HTTPException(status_code=409, detail="Take alignment is not in a failed state")
    if lifecycle.failure is None or not lifecycle.failure.retryable:
        raise HTTPException(status_code=409, detail="Take alignment failure is not retryable")
    alignment_worker.enqueue(piece_id, movement, take_id)
    return _take_response(take_store.get_take(piece_id, movement, take_id))


def _take_response(take: take_store.Take) -> TakeResponse:
    lifecycle = take_store.get_take_v2(take.piece_id, take.movement, take.take_id)
    candidates = None
    aligned = None
    if take.status in {take_store.ALIGNED, take_store.AMBIGUOUS}:
        aligned = take_store.get_aligned_result_v2(take.piece_id, take.movement, take.take_id)
    if take.status == take_store.AMBIGUOUS:
        # Only the ambiguous-take resolve card needs candidates -- skip the
        # aligned.json disk read `list_takes` would otherwise pay per take
        # for every other status. `store.get_aligned_result` already
        # validates `candidates` against the `AlignedResult` schema, so no
        # isinstance/key-presence filtering is needed here (design doc §3).
        if aligned is not None:
            candidates = [
                TakeCandidateResponse(
                    candidate_id=item.candidate_id,
                    start_score_tick=item.start_score_tick,
                    start_beat=item.start_score_tick / take_store.CANONICAL_PPQ,
                    score=item.score,
                    start_position=item.start_position,
                )
                for item in aligned.candidates
            ]
    return TakeResponse(
        take_id=take.take_id,
        piece_id=take.piece_id,
        movement=take.movement,
        recorded_at=take.recorded_at,
        duration_seconds=take.duration_seconds,
        note_on_count=take.note_on_count,
        input_name=take.input_name,
        cue=take.cue,
        placement_hint=take.placement_hint,
        status=take.status,
        analysis_state=lifecycle.analysis_state,
        disposition=lifecycle.disposition,
        profile_membership=lifecycle.profile_membership,
        lifecycle_revision=lifecycle.lifecycle_revision,
        failure=lifecycle.failure,
        midi_url=f"/api/takes/{take.take_id}/midi?piece_id={take.piece_id}&movement={take.movement}",
        candidates=candidates,
        review=review_status(take.piece_id, take.movement, take.take_id),
        alignment=TakeAlignmentSummaryResponse(
            rating=(
                "needs_review"
                if aligned.ambiguous or aligned.match_rate < 0.6
                else "strong"
                if aligned.match_rate >= 0.8
                else "usable"
            ),
            note_match_rate=aligned.match_rate,
            matched_notes=aligned.matched_notes,
            extra_notes=aligned.extra_notes,
            missing_notes=aligned.missing_notes,
            ambiguous=aligned.ambiguous,
        )
        if aligned is not None
        else None,
        score_span=_score_span_response(
            take.piece_id,
            take.movement,
            aligned,
        )
        if aligned is not None
        else None,
        # Dense cursor anchors are intentionally loaded through the dedicated
        # score-transport detail endpoint. List serialization must not parse a
        # MIDI file for every aligned take in the bank.
        score_transport=None,
    )


@router.get("/takes/{take_id}/midi", response_class=FileResponse, operation_id="downloadTakeMidi")
def download_take_midi(
    take_id: str,
    piece_id: str,
    movement: int,
) -> FileResponse:
    take_id = _ensure_valid_take_id(take_id)
    try:
        take = take_store.get_take(piece_id, movement, take_id)
    except take_store.TakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    abs_path = paths.take_dir(piece_id, movement, take_id, create=False) / take.midi_path
    return FileResponse(
        path=abs_path,
        filename=f"{take_id}.mid",
        media_type="audio/midi",
    )


@router.get("/coverage/{movement}", response_model=CoverageDoc, operation_id="getCoverage")
def get_coverage(
    movement: int,
    piece_id: str,
) -> CoverageDoc:
    try:
        return get_cached_coverage(piece_id, movement)
    except FileNotFoundError:
        # A new movement has no materialization yet. Return a read-only empty
        # view; GET still performs no filesystem writes.
        return empty_coverage(piece_id, movement)
    except Exception as exc:
        logging.exception("Failed to compute coverage for piece %s movement %d", piece_id, movement)
        raise HTTPException(
            status_code=500, detail="Internal server error during coverage computation"
        ) from exc


@router.get(
    "/scores/{movement}/health",
    response_model=ScoreBundleHealthResponse,
    operation_id="getScoreBundleHealth",
)
def get_score_bundle_health(movement: int, piece_id: str) -> ScoreBundleHealthResponse:
    """Report DVC-tracked score bundle artifacts that haven't been pulled yet.

    Read-only file-existence check, no subprocess execution -- this only
    tells the caller what's missing so the Ready face can show a warning
    instead of the user discovering it via a failed PDF load (rubato#101).
    """

    if not _SESSION_ID_PATTERN.fullmatch(piece_id):
        raise HTTPException(
            status_code=422,
            detail="piece_id must be alphanumeric, '-', or '_' only",
        )
    try:
        missing = paths.score_bundle_missing_artifacts(piece_id, movement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    findings: list[ScoreHealthFindingResponse] = []
    try:
        bundle_root = paths.score_bundle_dir(piece_id, movement)
    except ValueError:
        bundle_root = None
    if bundle_root is not None and not missing:
        # Only meaningful once the artifacts are actually present; a bundle
        # awaiting `dvc pull` would otherwise report every measure as missing
        # geometry, which is noise rather than a finding.
        findings = [
            ScoreHealthFindingResponse(
                code=finding.code,
                severity=finding.severity,
                message=finding.message,
                measures=list(finding.measures),
            )
            for finding in check_score_bundle(bundle_root)
        ]
    return ScoreBundleHealthResponse(
        piece_id=piece_id,
        movement=movement,
        missing_artifacts=missing,
        findings=findings,
    )


@router.get("/scores/{movement}/pdf", response_class=FileResponse, operation_id="getScorePdf")
def get_score_pdf(movement: int, piece_id: str) -> FileResponse:
    """Serve a score bundle's PDF for the pdf.js coverage overlay (design doc §3.2).

    Level 1/2 of the overlay render entirely client-side (pdf.js canvas + SVG
    overlay) -- the server's only job is handing back the bytes, no
    rasterization.
    """

    if not _SESSION_ID_PATTERN.fullmatch(piece_id):
        raise HTTPException(
            status_code=422,
            detail="piece_id must be alphanumeric, '-', or '_' only",
        )
    try:
        pdf_path = paths.score_display_pdf_path(piece_id, movement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not pdf_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Display score is registered but unavailable for {piece_id} "
                f"movement {movement}; pull its DVC artifact"
            ),
        )
    return FileResponse(path=pdf_path, media_type="application/pdf")


def _validate_piece_id(piece_id: str) -> None:
    if not _SESSION_ID_PATTERN.fullmatch(piece_id):
        raise HTTPException(
            status_code=422,
            detail="piece_id must be alphanumeric, '-', or '_' only",
        )


@router.get(
    "/scores/{movement}/anchors",
    response_model=AnchorSet,
    operation_id="listAnchors",
)
def list_anchors(movement: Annotated[int, PathParam(ge=1)], piece_id: str) -> AnchorSet:
    """List the piece/movement's structural beat anchors."""

    _validate_piece_id(piece_id)
    return take_store.load_anchor_set(piece_id, movement)


@router.post(
    "/scores/{movement}/anchors",
    response_model=AnchorSet,
    operation_id="addAnchor",
)
def add_anchor(
    movement: Annotated[int, PathParam(ge=1)],
    piece_id: str,
    payload: AnchorCreateRequest,
) -> AnchorSet:
    """Add (or replace) a structural beat anchor at a canonical score tick."""

    _validate_piece_id(piece_id)
    return take_store.add_anchor(
        piece_id,
        movement,
        score_tick=payload.score_tick,
        measure=payload.measure,
        label=payload.label,
    )


@router.patch(
    "/scores/{movement}/anchors/{score_tick}",
    response_model=AnchorSet,
    operation_id="moveAnchor",
)
def move_anchor(
    movement: Annotated[int, PathParam(ge=1)],
    score_tick: int,
    piece_id: str,
    payload: AnchorMoveRequest,
) -> AnchorSet:
    """Atomically move one structural beat anchor to a new canonical tick."""

    _validate_piece_id(piece_id)
    try:
        return take_store.move_anchor(
            piece_id,
            movement,
            score_tick=score_tick,
            new_score_tick=payload.new_score_tick,
            measure=payload.measure,
            label=payload.label,
        )
    except take_store.AnchorNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except take_store.AnchorConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete(
    "/scores/{movement}/anchors",
    response_model=AnchorSet,
    operation_id="clearAnchorsInMeasure",
)
def clear_anchors_in_measure(
    movement: Annotated[int, PathParam(ge=1)],
    piece_id: str,
    measure: Annotated[int, Query(ge=1)],
) -> AnchorSet:
    """Clear every structural beat anchor in one printed measure."""

    _validate_piece_id(piece_id)
    return take_store.remove_anchors_in_measure(
        piece_id,
        movement,
        measure=measure,
    )


@router.post(
    "/scores/{movement}/anchors/restore",
    response_model=AnchorSet,
    operation_id="restoreAnchors",
)
def restore_anchors(
    movement: Annotated[int, PathParam(ge=1)],
    piece_id: str,
    payload: AnchorRestoreRequest,
) -> AnchorSet:
    """Atomically restore anchors removed by a reversible score edit."""

    _validate_piece_id(piece_id)
    return take_store.restore_anchors(
        piece_id,
        movement,
        anchors=tuple(
            Anchor(
                score_tick=anchor.score_tick,
                measure=anchor.measure,
                label=anchor.label,
            )
            for anchor in payload.anchors
        ),
    )


@router.delete(
    "/scores/{movement}/anchors/{score_tick}",
    response_model=AnchorSet,
    operation_id="deleteAnchor",
)
def delete_anchor(
    movement: Annotated[int, PathParam(ge=1)],
    score_tick: int,
    piece_id: str,
) -> AnchorSet:
    """Delete the structural beat anchor at a canonical score tick."""

    _validate_piece_id(piece_id)
    return take_store.remove_anchor(piece_id, movement, score_tick=score_tick)


def _mix_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (mix_store.MixProgramNotFoundError, mix_store.MixRegionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, mix_store.MixRevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, mix_store.MixScoreIdentityError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get(
    "/mix/zones",
    response_model=ZoneConfigList,
    operation_id="listMixZones",
)
def list_mix_zones() -> ZoneConfigList:
    return ZoneConfigList(zones=room_zones())


@router.get(
    "/mix/programs",
    response_model=MixProgramList,
    operation_id="listMixPrograms",
)
def list_mix_programs(piece_id: str, movement: Annotated[int, Query(ge=1)]) -> MixProgramList:
    _validate_piece_id(piece_id)
    return MixProgramList(programs=mix_store.list_programs(piece_id, movement))


@router.post(
    "/mix/programs",
    response_model=MixProgram,
    operation_id="createMixProgram",
)
def create_mix_program(payload: MixProgramCreate) -> MixProgram:
    _validate_piece_id(payload.piece_id)
    try:
        return mix_store.create_program(payload)
    except (ValueError, ValidationError) as exc:
        raise _mix_error(exc) from exc


@router.get(
    "/mix/programs/{program_id}",
    response_model=MixProgram,
    operation_id="getMixProgram",
)
def get_mix_program(
    program_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
) -> MixProgram:
    _validate_piece_id(piece_id)
    _validate_piece_id(program_id)
    try:
        return mix_store.load_program(piece_id, movement, program_id)
    except mix_store.MixProgramNotFoundError as exc:
        raise _mix_error(exc) from exc


@router.post(
    "/mix/programs/{program_id}/rebind",
    response_model=MixProgram,
    operation_id="rebindMixProgram",
)
def rebind_mix_program(
    program_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
    payload: MixRevisionRequest,
) -> MixProgram:
    """Pin reviewed authored coordinates to the current score identity."""

    _validate_piece_id(piece_id)
    _validate_piece_id(program_id)
    try:
        return mix_store.rebind_score_identity(
            piece_id,
            movement,
            program_id,
            expected_revision=payload.expected_revision,
        )
    except (
        mix_store.MixProgramNotFoundError,
        mix_store.MixRevisionConflictError,
    ) as exc:
        raise _mix_error(exc) from exc


@router.put(
    "/mix/programs/{program_id}/default-routes",
    response_model=MixProgram,
    operation_id="updateMixDefaultRoutes",
)
def update_mix_default_routes(
    program_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
    payload: MixRoutesUpdate,
) -> MixProgram:
    _validate_piece_id(piece_id)
    _validate_piece_id(program_id)
    try:
        return mix_store.update_default_routes(
            piece_id,
            movement,
            program_id,
            expected_revision=payload.expected_revision,
            routes=payload.default_routes,
        )
    except (
        ValueError,
        mix_store.MixProgramNotFoundError,
        mix_store.MixRevisionConflictError,
    ) as exc:
        raise _mix_error(exc) from exc


def _validate_region_bounds(piece_id: str, movement: int, payload) -> None:
    projection = score_projection(piece_id, movement)
    final_tick = projection.timeline.document.measures[-1].end_tick
    if payload.region.end_tick > final_tick:
        raise HTTPException(status_code=422, detail="Mix region extends beyond the score timeline")


@router.post(
    "/mix/programs/{program_id}/regions",
    response_model=MixProgram,
    operation_id="createMixRegion",
)
def create_mix_region(
    program_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
    payload: MixRegionCreate,
) -> MixProgram:
    _validate_piece_id(piece_id)
    _validate_region_bounds(piece_id, movement, payload)
    try:
        return mix_store.add_region(
            piece_id,
            movement,
            program_id,
            expected_revision=payload.expected_revision,
            region=payload.region,
        )
    except Exception as exc:
        if not isinstance(exc, (ValueError, LookupError, RuntimeError)):
            raise
        raise _mix_error(exc) from exc


@router.put(
    "/mix/programs/{program_id}/regions/{region_id}",
    response_model=MixProgram,
    operation_id="updateMixRegion",
)
def update_mix_region(
    program_id: str,
    region_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
    payload: MixRegionUpdate,
) -> MixProgram:
    _validate_piece_id(piece_id)
    _validate_region_bounds(piece_id, movement, payload)
    try:
        return mix_store.update_region(
            piece_id,
            movement,
            program_id,
            region_id,
            expected_revision=payload.expected_revision,
            region=payload.region,
        )
    except Exception as exc:
        if not isinstance(exc, (ValueError, LookupError, RuntimeError)):
            raise
        raise _mix_error(exc) from exc


@router.delete(
    "/mix/programs/{program_id}/regions/{region_id}",
    response_model=MixProgram,
    operation_id="deleteMixRegion",
)
def delete_mix_region(
    program_id: str,
    region_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
    expected_revision: Annotated[int, Query(ge=1)],
) -> MixProgram:
    _validate_piece_id(piece_id)
    try:
        return mix_store.delete_region(
            piece_id,
            movement,
            program_id,
            region_id,
            expected_revision=expected_revision,
        )
    except Exception as exc:
        if not isinstance(exc, (ValueError, LookupError, RuntimeError)):
            raise
        raise _mix_error(exc) from exc


@router.post(
    "/mix/programs/{program_id}/undo",
    response_model=MixProgram,
    operation_id="undoMixProgram",
)
def undo_mix_program(
    program_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
    payload: MixRevisionRequest,
) -> MixProgram:
    _validate_piece_id(piece_id)
    try:
        return mix_store.undo(
            piece_id, movement, program_id, expected_revision=payload.expected_revision
        )
    except Exception as exc:
        if not isinstance(exc, (ValueError, LookupError, RuntimeError)):
            raise
        raise _mix_error(exc) from exc


@router.get(
    "/mix/programs/{program_id}/policy",
    response_model=MixPolicy,
    operation_id="getMixPolicy",
)
def get_mix_policy(
    program_id: str,
    piece_id: str,
    movement: Annotated[int, Query(ge=1)],
) -> MixPolicy:
    _validate_piece_id(piece_id)
    try:
        return compile_mix_policy(
            mix_store.load_program(piece_id, movement, program_id, require_current_score=True),
            room_zones(),
        )
    except Exception as exc:
        if not isinstance(exc, (ValueError, LookupError, RuntimeError)):
            raise
        raise _mix_error(exc) from exc


@router.get(
    "/scores/{movement}/measure_boxes",
    response_model=MeasureBoxesResponse,
    operation_id="getMeasureBoxes",
)
def get_measure_boxes(movement: int, piece_id: str) -> MeasureBoxesResponse:
    """Serve the pre-built measure-geometry map (design doc §3.3, roadmap item 7).

    `measure_boxes.json` is a build-time artifact (scripts/build_measure_boxes.py)
    checked into the bundle's `derived/` directory, not computed per-request.
    """

    if not _SESSION_ID_PATTERN.fullmatch(piece_id):
        raise HTTPException(
            status_code=422,
            detail="piece_id must be alphanumeric, '-', or '_' only",
        )
    try:
        boxes_path = paths.score_display_geometry_path(piece_id, movement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not boxes_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"display geometry not built yet for {piece_id} movement {movement}",
        )
    try:
        payload = json.loads(boxes_path.read_text(encoding="utf-8"))
        if payload.get("kind") == "display":
            display_map = DisplayMappingDocument.model_validate(payload)
            beat_map_path = paths.score_performance_beat_map_path(piece_id, movement)
            payload = _adapt_display_mapping(
                display_map,
                pdf_path=paths.score_display_pdf_path(piece_id, movement).name,
                beat_map_path=beat_map_path if beat_map_path.exists() else None,
            )
        return MeasureBoxesResponse.model_validate(payload)
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        logging.exception(
            "Failed to read display geometry for piece %s movement %d",
            piece_id,
            movement,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error reading measure geometry",
        ) from exc


@router.get(
    "/scores/{movement}/alignment-worklist",
    response_model=AlignmentWorklistResponse,
    operation_id="getAlignmentWorklist",
)
def get_alignment_worklist(
    movement: Annotated[int, PathParam(ge=1)],
    piece_id: str,
    tolerance: Annotated[float, Query(gt=0.0)] = 2.0,
    spine_step: Annotated[int, Query(ge=0)] = 15,
) -> AlignmentWorklistResponse:
    """The shortlist of measures to audition: bars both cross-check alignments reject, plus a spine.

    This validates MIDI *timing* (where each beat sounds in the Oguri), not PDF
    geometry. The two alignments have independent provenance from the beat map,
    so a bar both reject is a real defect; the spine catches a shared offset
    their intersection is blind to (score-localization Cardinal Rule).
    """

    _validate_piece_id(piece_id)
    try:
        beat_map_path = paths.score_performance_beat_map_path(piece_id, movement)
        align_a_path, align_b_path = paths.score_cross_check_alignment_paths(piece_id, movement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not beat_map_path.exists():
        return AlignmentWorklistResponse(
            piece_id=piece_id,
            movement=movement,
            available=False,
            detail="Performance beat map not available; pull or rebuild the bundle.",
        )
    if not (align_a_path.exists() and align_b_path.exists()):
        return AlignmentWorklistResponse(
            piece_id=piece_id,
            movement=movement,
            available=False,
            detail="Cross-check alignments not present in this checkout (local MuseScore-derived).",
        )
    beat_map = load_performance_beat_map(beat_map_path)
    corrections = load_human_corrections(paths.score_alignment_corrections_path(piece_id, movement))
    if corrections and corrections.corrections:
        beat_map = beat_anchor.rederive_beat_map(beat_map, corrections)

    def _downbeats(path: Path) -> dict[str, float]:
        raw = json.loads(path.read_text(encoding="utf-8"))["measure_downbeat_seconds"]
        return {str(k): float(v) for k, v in raw.items()}

    downbeats_a = _downbeats(align_a_path)
    downbeats_b = _downbeats(align_b_path)
    map_downbeats = {
        anchor.measure_label: anchor.source_seconds
        for anchor in beat_map.anchors
        if abs(anchor.beat_in_measure) < 1e-6
    }
    suspects = beat_anchor.suspect_measures(
        downbeats_a, downbeats_b, map_downbeats, tolerance=tolerance, spine_step=spine_step
    )
    # This endpoint drives an orchestra-only listening task. Keep cross-check
    # disagreements in the machine artifacts, but do not ask the performer to
    # audition a printed bar whose accompaniment is entirely rests: silence
    # cannot confirm or reject its orchestral timing. The canonical follower
    # still traverses those measures from the solo reference independently.
    bundle_root = paths.score_bundle_dir(piece_id, movement)
    bundle_measures = parse_bundle_timeline_measures(bundle_root)
    accompaniment_demand = accompaniment_demand_by_measure(bundle_root, bundle_measures)
    audible_measure_labels = {
        str(measure): required for measure, required in accompaniment_demand.items()
    }
    suspects = [
        suspect for suspect in suspects if audible_measure_labels.get(suspect.measure_label, False)
    ]
    return AlignmentWorklistResponse(
        piece_id=piece_id,
        movement=movement,
        available=True,
        measures=[
            AlignmentWorklistMeasure(
                measure_label=s.measure_label,
                reason=s.reason,
                hypothesis_downbeat_seconds=s.hypothesis_downbeat_seconds,
                disagreement_seconds=s.disagreement_seconds,
            )
            for s in suspects
        ],
    )


@router.get(
    "/scores/{movement}/beat-audition/{measure}",
    response_model=BeatAuditionResponse,
    operation_id="getBeatAudition",
)
def get_beat_audition(
    movement: Annotated[int, PathParam(ge=1)],
    measure: str,
    piece_id: str,
    window: Annotated[float, Query(gt=0.0)] = 1.5,
) -> BeatAuditionResponse:
    """Per-beat timing hypothesis + nearby reference onsets for one measure's audition.

    Returns, for each beat: the map's current time (the metronome click position),
    whether a real note anchors it, its page-x (so the beat under test can be
    marked), and the reference onsets within ``window`` seconds -- the candidate
    notes the performer picks among by ear for a precise (Mode B) correction. The
    UI persists a pick by POSTing its ``source_seconds`` to alignment-corrections.
    """

    _validate_piece_id(piece_id)
    try:
        beat_map_path = paths.score_performance_beat_map_path(piece_id, movement)
        solo_path = paths.score_solo_reference_path(piece_id, movement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not beat_map_path.exists():
        raise HTTPException(status_code=503, detail="Performance beat map not available.")
    beat_map = load_performance_beat_map(beat_map_path)
    corrections = load_human_corrections(paths.score_alignment_corrections_path(piece_id, movement))
    if corrections and corrections.corrections:
        beat_map = beat_anchor.rederive_beat_map(beat_map, corrections)
    hypotheses = beat_anchor.beat_hypotheses(beat_map, measure)
    if not hypotheses:
        raise HTTPException(status_code=404, detail=f"measure {measure!r} not in the beat map")

    onsets = beat_anchor.reference_onsets(solo_path) if solo_path.exists() else ()
    measure_anchors = [a for a in beat_map.anchors if a.measure_label == measure]
    pdf_x_by_beat = {a.beat_in_measure: a.pdf_x for a in measure_anchors}
    pdf_page = next((a.pdf_page for a in measure_anchors if a.pdf_page is not None), None)
    # The audible measure ends at the next measure's downbeat (or the last beat
    # plus one local period when this is the final measure).
    last_beat_seconds = hypotheses[-1].source_seconds
    downbeats = sorted(
        a.source_seconds for a in beat_map.anchors if a.source_seconds > last_beat_seconds
    )
    beats: list[BeatAuditionBeat] = []
    for hypothesis in hypotheses:
        candidates = beat_anchor.candidate_onsets(onsets, hypothesis.source_seconds, window)
        beats.append(
            BeatAuditionBeat(
                beat_in_measure=hypothesis.beat_in_measure,
                source_seconds=hypothesis.source_seconds,
                source_midi_tick=hypothesis.source_midi_tick,
                anchored=hypothesis.anchored,
                confidence=hypothesis.confidence,
                pdf_x=pdf_x_by_beat.get(hypothesis.beat_in_measure),
                candidates=[
                    BeatAuditionCandidate(
                        source_midi_tick=c.native_tick,
                        source_seconds=c.seconds,
                        pitch=c.pitch,
                        delta_seconds=c.delta_seconds,
                    )
                    for c in candidates[:12]
                ],
            )
        )
    measure_start = hypotheses[0].source_seconds
    if len(hypotheses) >= 2:
        beat_period = (last_beat_seconds - measure_start) / (len(hypotheses) - 1)
    elif downbeats:
        # A single-beat measure has no internal interval; use its whole span to
        # the next downbeat rather than a hardcoded 60 bpm count-in.
        beat_period = downbeats[0] - measure_start
    else:
        beat_period = 1.0
    beat_period = max(beat_period, 1e-3)
    if downbeats:
        measure_end = downbeats[0]
    else:
        # Final measure: no next downbeat, so extend past the last beat to cover
        # any solo notes still ringing out rather than truncating them.
        tail_onsets = [o.seconds for o in onsets if o.seconds >= measure_start]
        measure_end = max([last_beat_seconds + beat_period, *tail_onsets]) + (
            0.25 if tail_onsets else 0.0
        )
    solo_notes = [
        BeatAuditionNote(pitch=onset.pitch, source_seconds=onset.seconds)
        for onset in onsets
        if measure_start <= onset.seconds < measure_end
    ]
    orchestra_path = oguri_movement(movement).orchestra_accompaniment_path
    orchestra_groups: list[BeatAuditionOnsetGroup] = []
    if orchestra_path.exists():
        all_orchestra_onsets = beat_anchor.reference_onsets(orchestra_path, track_filter=None)
        context_before = max(3.0, 2 * beat_period)
        context_after = max(1.5, beat_period)
        orchestra_groups = [
            BeatAuditionOnsetGroup(
                source_midi_tick=group.native_tick,
                source_seconds=group.seconds,
                pitches=list(group.pitches),
                instruments=list(group.track_names),
                delta_seconds=group.seconds - measure_start,
            )
            for group in beat_anchor.group_onsets(all_orchestra_onsets)
            if measure_start - context_before <= group.seconds < measure_end + context_after
        ]
    return BeatAuditionResponse(
        piece_id=piece_id,
        movement=movement,
        measure_label=measure,
        measure_start_seconds=measure_start,
        measure_end_seconds=measure_end,
        beat_period_seconds=beat_period,
        pdf_page=pdf_page,
        mapping_id=beat_map.mapping_id,
        review_state=beat_map.review_state,
        beats=beats,
        solo_notes=solo_notes,
        orchestra_groups=orchestra_groups,
    )


@router.post(
    "/scores/{movement}/beat-audition/{measure}/play",
    response_model=LiveStatusResponse,
    operation_id="playAlignmentAudition",
)
def play_beat_alignment_audition(
    movement: Annotated[int, PathParam(ge=1)],
    measure: str,
    piece_id: str,
    payload: AlignmentAuditionPlaybackRequest,
) -> LiveStatusResponse:
    """Play four clicks, then one exact Oguri orchestral measure with beat clicks."""

    audition = get_beat_audition(movement, measure, piece_id)
    source = oguri_movement(movement)
    duration_seconds = audition.measure_end_seconds - audition.measure_start_seconds
    beat_offsets = tuple(
        beat.source_seconds - audition.measure_start_seconds for beat in audition.beats
    )

    def worker(stop_event: threading.Event) -> str:
        summary = play_alignment_audition(
            source.orchestra_accompaniment_path,
            payload.output_name,
            start_seconds=audition.measure_start_seconds,
            duration_seconds=duration_seconds,
            beat_period_seconds=audition.beat_period_seconds,
            beat_offsets_seconds=beat_offsets,
            volume=payload.volume,
            metronome_volume=payload.metronome_volume,
            stop_event=stop_event,
        )
        return (
            f"Auditioned Oguri m.{measure}: {summary.note_on_count} orchestral notes "
            "with a four-beat count-in"
        )

    try:
        status = live_control.start_managed(
            kind="alignment_audition",
            message=f"Auditioning Oguri measure {measure} through {payload.output_name}",
            target=worker,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _live_status_response(status)


@router.post(
    "/scores/{movement}/alignment-corrections",
    response_model=ScoreAlignmentCorrectionResponse,
    operation_id="createScoreAlignmentCorrection",
)
def create_score_alignment_correction(
    movement: int,
    piece_id: str,
    payload: ScoreAlignmentCorrectionRequest,
) -> ScoreAlignmentCorrectionResponse:
    """Persist the exceptional human fallback: “this sound is this beat.”"""

    if (piece_id, movement) != ("chopin_op11", 2):
        raise HTTPException(status_code=404, detail="score correction is not supported here")
    beat_map_path = paths.score_performance_beat_map_path(piece_id, movement)
    if not beat_map_path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "Performance beat map is not available. Pull or rebuild the score bundle artifacts."
            ),
        )
    beat_map = load_performance_beat_map(beat_map_path)
    timeline = score_projection(piece_id, movement).timeline
    measure_index = payload.measure - 1
    if measure_index >= len(timeline.document.measures):
        raise HTTPException(status_code=422, detail="measure is outside the score timeline")
    measure = timeline.document.measures[measure_index]
    measure_duration = measure.end_tick - measure.start_tick
    canonical_offset = round(payload.beat_in_measure * timeline.document.canonical_ppq)
    if canonical_offset >= measure_duration:
        raise HTTPException(status_code=422, detail="beat is outside the selected measure")
    score_tick = measure.start_tick + canonical_offset

    # Prefer the exact onset tick the performer selected (Mode B); fall back to
    # interpolating the audible time only when no onset was picked (Mode A / the
    # freeze fallback), which is exact for a constant-tempo reference and close
    # otherwise.
    if payload.source_midi_tick is not None:
        source_tick = payload.source_midi_tick
    else:
        source_tick = source_midi_tick_at_seconds(beat_map, payload.source_seconds)
    correction = HumanBeatCorrection(
        correction_id=str(uuid4()),
        source_midi_tick=source_tick,
        score_tick=score_tick,
        measure_label=measure.measure_label,
        beat_in_measure=payload.beat_in_measure,
        created_at=utc_now(),
        note=payload.note,
    )
    correction_path = paths.score_alignment_corrections_path(piece_id, movement)
    with _score_alignment_correction_lock:
        existing = load_human_corrections(correction_path)
        retained = tuple(
            item
            for item in (existing.corrections if existing else ())
            if item.score_tick != correction.score_tick
        )
        document = HumanCorrectionDocument(
            piece_id=piece_id,
            movement=movement,
            timeline_id=beat_map.timeline_id,
            corrections=retained + (correction,),
        )
        # Validate the complete candidate document before the atomic write.
        # Serializing the load/validate/write transaction prevents concurrent
        # corrections from losing one another or rolling back a newer write.
        try:
            apply_human_corrections(beat_map, document)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        write_human_corrections(document, correction_path)
        updated_projection = score_projection(piece_id, movement)

    queued = False
    if payload.take_id:
        try:
            take_store.get_take_v2(piece_id, movement, payload.take_id)
            alignment_worker.enqueue(piece_id, movement, payload.take_id)
            queued = True
        except take_store.TakeNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    diagnostic_payload = {
        "correction_id": correction.correction_id,
        "source_seconds": payload.source_seconds,
        "source_midi_tick": source_tick,
        "score_tick": score_tick,
        "measure": payload.measure,
        "measure_label": measure.measure_label,
        "beat_in_measure": payload.beat_in_measure,
        "mapping_id": updated_projection.mapping_id,
        "reanalysis_queued": queued,
    }
    append_diagnostic(
        "score:alignment_corrected",
        piece_id=piece_id,
        movement=movement,
        take_id=payload.take_id,
        payload=diagnostic_payload,
    )
    LOGGER.info("Applied score alignment correction %s", diagnostic_payload)
    return ScoreAlignmentCorrectionResponse(**diagnostic_payload)


def _adapt_display_mapping(
    display_map: DisplayMappingDocument,
    *,
    pdf_path: str,
    beat_map_path: Path | None,
) -> dict[str, object]:
    """Present canonical display boxes with their printed measure labels."""

    beats_by_index: dict[int, list[dict[str, float]]] = {}
    if beat_map_path is not None:
        beat_map = load_performance_beat_map(beat_map_path)
        if beat_map.timeline_id != display_map.timeline_id:
            raise ValueError("display and beat maps must share one canonical timeline")
        for anchor in beat_map.anchors:
            if anchor.pdf_x is None:
                continue
            beats_by_index.setdefault(anchor.measure_index, []).append(
                {
                    "beat_in_measure": anchor.beat_in_measure,
                    "x": anchor.pdf_x,
                    # The beat marker's own geometry confidence (how well the
                    # staff pinned it), not the timing-anchor confidence -- this
                    # response is about where to draw the cursor.
                    "confidence": (
                        anchor.pdf_x_confidence
                        if anchor.pdf_x_confidence is not None
                        else anchor.confidence
                    ),
                    "anchored": (
                        anchor.pdf_x_anchored if anchor.pdf_x_anchored is not None else True
                    ),
                    "staff": anchor.pdf_x_staff,
                }
            )

    grouped: dict[tuple[int, int], list[dict[str, object]]] = {}
    for box in display_map.boxes:
        try:
            display_measure = int(box.measure_label)
        except ValueError as exc:
            raise ValueError("measure-box API currently requires numeric labels") from exc
        beats = beats_by_index.get(box.measure_index, [])
        if not beats:
            # Closing tutti bars (m.114+ here) have a display box from the grid
            # pass but no notes in the Audiveris note pass, so nothing anchors
            # their beats. Show evenly-spaced markers so the measure is not blank
            # rather than leaving the reader wondering where the beats are.
            placed = beat_geometry.infer_measure_beats([], box_x0=box.x0, box_x1=box.x1)
            beats = [
                {
                    "beat_in_measure": float(beat),
                    "x": placed.beat_x[beat],
                    "confidence": 0.0,
                    "anchored": False,
                    "staff": None,
                }
                for beat in range(len(placed.beat_x))
            ]
        grouped.setdefault((box.page, box.system), []).append(
            {
                "measure": display_measure,
                "x0": box.x0,
                "x1": box.x1,
                "y0": box.y0,
                "y1": box.y1,
                "score_start_tick": box.score_start_tick,
                "score_end_tick": box.score_end_tick,
                "beats": beats,
                "beat_staff": next((b.get("staff") for b in beats if b.get("staff")), None),
            }
        )

    pages: dict[int, list[dict[str, object]]] = {}
    for (page, _system), measures in sorted(grouped.items()):
        numbers = [int(measure["measure"]) for measure in measures]
        pages.setdefault(page, []).append(
            {
                "y0": min(float(measure["y0"]) for measure in measures),
                "y1": max(float(measure["y1"]) for measure in measures),
                "first_measure": min(numbers),
                "last_measure": max(numbers),
                "measures": [
                    {
                        "measure": item["measure"],
                        "x0": item["x0"],
                        "x1": item["x1"],
                        "score_start_tick": item["score_start_tick"],
                        "score_end_tick": item["score_end_tick"],
                        "beats": item["beats"],
                        "beat_staff": item["beat_staff"],
                    }
                    for item in measures
                ],
            }
        )
    return {
        "pdf": pdf_path,
        "page_count": display_map.page_count,
        "level": 2,
        "method": "canonical-display-map",
        "review_state": "machine",
        "pages": [{"page": page, "systems": systems} for page, systems in sorted(pages.items())],
    }


__all__ = ["router", "ON_TAKE_CAPTURED_HOOKS", "on_take_captured"]


def _sections_path_or_404(piece_id: str, movement: int) -> Path:
    try:
        path = paths.score_sections_path(piece_id, movement)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail="Section map is not available. Pull or rebuild the score bundle artifacts.",
        )
    return path


def _as_free_region(section: dict[str, Any]) -> FreeRegion:
    block = section.get("free_region", {})
    return FreeRegion(
        from_measure=int(block["from_measure"]),
        to_measure=int(block["to_measure"]),
        label=str(block.get("label", "Free region")),
        hand_back_measure=int(block["hand_back_measure"]),
        detector_model=block.get("detector_model"),
        start_beat=float(section["start_beat"]),
        end_beat=float(section["end_beat"]),
    )


@router.get(
    "/scores/{movement}/free-regions",
    response_model=FreeRegionsResponse,
    operation_id="listFreeRegions",
)
def list_free_regions(movement: int, piece_id: str = "chopin_op11") -> FreeRegionsResponse:
    path = _sections_path_or_404(piece_id, movement)
    return FreeRegionsResponse(
        regions=[_as_free_region(s) for s in free_regions.declared_regions(path)]
    )


@router.post(
    "/scores/{movement}/free-regions",
    response_model=FreeRegionsResponse,
    operation_id="declareFreeRegion",
)
def declare_free_region(
    movement: int,
    payload: FreeRegionRequest,
    piece_id: str = "chopin_op11",
) -> FreeRegionsResponse:
    """Declare a span the follower cannot track.

    Splitting the section map is easy to get subtly wrong and fails far from
    the edit, so the invariants live in free_regions and a bad span is refused
    here rather than written and discovered at run time.
    """

    path = _sections_path_or_404(piece_id, movement)
    try:
        free_regions.declare(
            path,
            free_regions.FreeRegionSpec(
                from_measure=payload.from_measure,
                to_measure=payload.to_measure,
                label=payload.label,
                detector_model=payload.detector_model,
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return list_free_regions(movement, piece_id)


@router.delete(
    "/scores/{movement}/free-regions/{from_measure}",
    response_model=FreeRegionsResponse,
    operation_id="clearFreeRegion",
)
def clear_free_region(
    movement: int,
    from_measure: int,
    piece_id: str = "chopin_op11",
) -> FreeRegionsResponse:
    path = _sections_path_or_404(piece_id, movement)
    if not free_regions.clear(path, from_measure):
        raise HTTPException(status_code=404, detail="no free region starts at that measure")
    return list_free_regions(movement, piece_id)


@router.post(
    "/ui/cursor-trace/{run_id}",
    response_model=CursorTraceResponse,
    operation_id="recordCursorTrace",
)
def record_cursor_trace(run_id: str, payload: CursorTraceRequest) -> CursorTraceResponse:
    """Store what the UI actually drew, beside what the engine believed.

    The engine trace showed a stable position while the performer watched the
    cursor bounce. Both can be true -- three sources feed one cursor and the UI
    picks by precedence -- but nothing recorded the drawn value, so the
    disagreement could not be examined. Written next to runtime.jsonl so a run
    is one directory containing both accounts.
    """

    if not _SESSION_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=422, detail="invalid run id")
    directory = paths.run_trace_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cursor.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for sample in payload.samples:
            handle.write(sample.model_dump_json() + "\n")
    return CursorTraceResponse(recorded=len(payload.samples), path=str(path))
