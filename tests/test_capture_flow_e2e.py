"""Browser-driven test of the take-capture toast/auto-arm loop (roadmap item
5, design doc §2.7/§3.1): `WS /api/events` pushing `take:recording_stopped`
then `take:alignment_done` must (a) resolve the after-take toast in place and
(b) never gate the Record button on alignment finishing.

Hardware recording itself can't run in CI (no Yamaha attached), so this fakes
`live_control`/`alignment_worker` the same way tests/test_takes_api.py does
for the plain API tests -- but here the fakes back a *real* uvicorn server a
real browser drives end-to-end, so the WebSocket wire, the Svelte event
client, and the toast/button reactivity are all exercised for real.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from mido import Message, MidiFile, MidiTrack
from playwright.sync_api import Page, expect
from pypdf import PdfWriter

import aimusic.server.routes as routes
from aimusic.accompaniment.rehearsal_position import score_projection
from aimusic.accompaniment.runtime_contracts import (
    LiveStateWord,
    OrchestraRendererStatus,
    RunPhase,
    RuntimeConfig,
    RuntimeScorePosition,
    RuntimeStatus,
)
from aimusic.core import paths
from aimusic.core.events import events as shared_events
from aimusic.core.time import utc_now
from aimusic.server.app import create_app
from aimusic.server.live_control import LiveJobStatus
from aimusic.server.schemas import (
    CoverageMaterialized,
    HardwareJobPhase,
    HardwareStatusEvent,
    LivePerformancePlanResponse,
    LiveRuntimeStatusEvent,
    LiveStatusResponse,
    OrchestraRendererStatusEvent,
    ScoreTransportAnchorResponse,
    ScoreTransportResponse,
    TakeAlignmentDone,
    TakeReviewStatusEvent,
)
from aimusic.takes import coverage as take_coverage
from aimusic.takes import store as take_store
from aimusic.takes.lifecycle import JobKind, JobState
from aimusic.takes.models import AlignedResult, TakePlacementHint, TimingMapPoint
from aimusic.takes.review import review_input_revision, review_midi_path
from tests.fake_hardware import (
    FakeLiveControl,
    movement_2_follow_beat,
    write_fixture_midi,
)
from tests.ui_cursor_audit import capture_cursor_audit

# How long the fake alignment worker waits before resolving, long enough
# that a test asserting the *unresolved* toast/auto-armed button first is
# not racing the resolution.
FAKE_ALIGNMENT_DELAY_SECONDS = 0.6


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


class FakeAlignmentWorker:
    """Resolves every enqueued take to `aligned` after a short delay on a
    background thread -- standing in for the real background pipeline
    (tested for real, without a browser, in tests/takes/test_aligner_events.py
    and tests/test_events_ws.py) so this test can focus on what the UI does
    with the events, not on real alignment.
    """

    def enqueue(self, piece_id: str, movement: int, take_id: str) -> None:
        timer = threading.Timer(
            FAKE_ALIGNMENT_DELAY_SECONDS,
            self._resolve,
            args=(piece_id, movement, take_id),
        )
        timer.daemon = True
        timer.start()

    def _resolve(self, piece_id: str, movement: int, take_id: str) -> None:
        try:
            updated = take_store.update_take_status(piece_id, movement, take_id, take_store.ALIGNED)
        except take_store.TakeNotFoundError:
            # A later browser test may already be using a fresh temporary
            # data root when this prior fixture's delayed timer fires.
            return
        # Persist the same authoritative score span the production aligner
        # writes.  The score-first cockpit reads this span back after refresh
        # to paint the latest take and compute observed rehearsal coverage;
        # publishing an event without the durable artifact would make this
        # browser fake less truthful than production.
        take_store.write_aligned_result(
            piece_id,
            movement,
            take_id,
            AlignedResult(
                take_id=take_id,
                aligner="browser-fixture",
                score_start_beat=96.0,
                score_end_beat=152.5,
                match_rate=0.9,
                ambiguous=False,
                matched_notes=2,
                extra_notes=0,
                missing_notes=0,
                timing_map=(
                    TimingMapPoint(score_beat=96.0, take_seconds=0.0),
                    TimingMapPoint(score_beat=152.5, take_seconds=4.0),
                ),
                candidates=(),
                cell_samples=(),
                edge_trim_beats=(0.0, 0.0),
            ),
        )
        coverage = take_coverage.compute_coverage(piece_id, movement)
        shared_events.publish(
            TakeAlignmentDone(
                type="take:alignment_done",
                take_id=updated.take_id,
                piece_id=updated.piece_id,
                movement=updated.movement,
                status=updated.status,
                score_start_beat=96.0,
                score_end_beat=152.5,
            )
        )
        shared_events.publish(
            CoverageMaterialized(
                type="coverage:materialized",
                piece_id=piece_id,
                movement=movement,
                revision=coverage.revision,
            )
        )


class FakeReviewWorker:
    """Commits a playable ensemble artifact through the durable job store."""

    def enqueue(self, piece_id: str, movement: int, take_id: str):
        job = take_store.create_job(
            piece_id,
            movement,
            take_id,
            JobKind.RENDER_REVIEW,
            input_revision=review_input_revision(piece_id, movement, take_id),
            deduplicate=True,
        )
        if job.state == JobState.SUCCEEDED:
            return job
        if job.state == JobState.QUEUED:
            job = take_store.transition_stored_job(piece_id, movement, job.job_id, JobState.RUNNING)
            midi = MidiFile()
            track = MidiTrack()
            track.append(Message("note_on", note=60, velocity=80, time=0))
            # Four seconds at mido's default 120 BPM gives the cockpit enough
            # transport time for the score cursor to visibly advance in E2E.
            track.append(Message("note_off", note=60, velocity=0, time=3840))
            midi.tracks.append(track)
            ensemble_path = review_midi_path(take_id, "ensemble")
            solo_path = review_midi_path(take_id, "solo")
            ensemble_path.parent.mkdir(parents=True, exist_ok=True)
            midi.save(ensemble_path)
            midi.save(solo_path)
            job = take_store.transition_stored_job(
                piece_id, movement, job.job_id, JobState.SUCCEEDED
            )
            shared_events.publish(
                TakeReviewStatusEvent(
                    type="take:review_status",
                    take_id=take_id,
                    piece_id=piece_id,
                    movement=movement,
                    state="ready",
                    job_id=job.job_id,
                    midi_url=(
                        f"/api/takes/{take_id}/review/midi?piece_id={piece_id}&movement={movement}"
                    ),
                )
            )
        return job


class ScoreAwareFakeLiveControl(FakeLiveControl):
    """Software hardware double with the production score-time contract.

    The shared fake predates score transports and remains intentionally small
    for API tests that do not care about performer position. This browser fake
    adds wall-clock anchors so the UI exercises real cursor interpolation.
    """

    def __init__(self) -> None:
        super().__init__()
        self.runtime: OrchestraLedFakeLiveRuntime | None = None

    def stop(self) -> LiveJobStatus:
        if self.runtime is not None:
            self.runtime.stop()
        return super().stop()

    def _set_status(self, status: LiveJobStatus) -> LiveJobStatus:
        self.current = status
        shared_events.publish(
            HardwareStatusEvent(
                type="hardware:status",
                status=LiveStatusResponse(
                    phase=status.phase,
                    kind=status.kind,
                    running=status.running,
                    message=status.message,
                    started_at=status.started_at,
                    session_id=status.session_id,
                    score_transport=status.score_transport,
                ),
            )
        )
        return status

    def start_play_midi_file(self, **kwargs: object) -> LiveJobStatus:
        status = super().start_play_midi_file(**kwargs)
        return self._set_status(
            LiveJobStatus(
                kind=status.kind,
                running=status.running,
                message=status.message,
                started_at=utc_now(),
                session_id=status.session_id,
            )
        )

    def attach_score_transport(self, score_transport) -> LiveJobStatus:
        status = self.current
        return self._set_status(
            LiveJobStatus(
                kind=status.kind,
                running=status.running,
                message=status.message,
                started_at=status.started_at,
                session_id=status.session_id,
                score_transport=score_transport,
            )
        )


class OrchestraLedFakeLiveRuntime:
    """Browser boundary double for the live plan and runtime event stream."""

    def __init__(self) -> None:
        self.current: RuntimeStatus | None = None
        self.start_args: dict[str, object] | None = None
        self.projection = score_projection("chopin_op11", 2)
        self.renderer = OrchestraRendererStatus(
            state="ready",
            preload_id="browser-fixture",
            zone_id="room_center",
            device_name="LG TV SSCR2",
            loaded_instruments=4,
            total_instruments=4,
            updated_at_monotonic=time.monotonic(),
            message="BBCSO ready on LG TV SSCR2",
        )

    def _position(self, *, source_tick: int) -> RuntimeScorePosition:
        position = self.projection.position_at_source_tick(source_tick)
        return RuntimeScorePosition(
            **position.__dict__,
            mapping_id=self.projection.mapping_id,
            mapping_review_state=self.projection.mapping_review_state.value,
            canonical_position=self.projection.canonical_position,
        )

    def _position_at_score_beat(self, score_beat: float) -> RuntimeScorePosition:
        score_tick = round(score_beat * self.projection.timeline.document.canonical_ppq)
        return self._position(source_tick=self.projection.source_tick_at_score_tick(score_tick))

    def performance_plan(self, **kwargs: object) -> LivePerformancePlanResponse:
        start_measure = kwargs.get("start_measure")
        explicit_start = isinstance(start_measure, int)
        if explicit_start:
            start_score_tick = self.projection.timeline.tick_at(start_measure - 1)
            start_score_beat = start_score_tick / self.projection.timeline.document.canonical_ppq
            orchestra_start = self._position_at_score_beat(start_score_beat)
        else:
            orchestra_start = self._position(source_tick=8_068)
            start_score_beat = orchestra_start.score_beat
        follow_beat = movement_2_follow_beat(start_score_beat)
        return LivePerformancePlanResponse(
            bundle_id="chopin_op11_movement_2",
            orchestra_starts_automatically=(
                explicit_start or (follow_beat is not None and follow_beat > start_score_beat)
            ),
            orchestra_start=orchestra_start,
            first_solo_entry=self._position_at_score_beat(47.0),
            follow_start=(
                None if follow_beat is None else self._position_at_score_beat(follow_beat)
            ),
            follow_prior_take_count=6,
            initial_tempo_bpm=64.0,
            tempo_source="performance_profile",
            rehearsal_take_count=8,
        )

    def status(self) -> RuntimeStatus | None:
        return self.current

    def renderer_status(self) -> OrchestraRendererStatus:
        return self.renderer

    def preload_renderer(self, **_: object) -> OrchestraRendererStatus:
        return self.renderer

    def stop_preloaded_renderer(self) -> OrchestraRendererStatus:
        self.renderer = OrchestraRendererStatus(
            state="not_loaded",
            updated_at_monotonic=time.monotonic(),
            message="BBCSO preload is off",
        )
        return self.renderer

    def _publish_position(self, score_beat: float) -> None:
        if self.current is None or self.current.phase is not RunPhase.ACTIVE:
            return
        self.current = self.current.model_copy(
            update={
                "monotonic_time": self.current.monotonic_time + 0.25,
                "score_beat": score_beat,
                "score_position": self._position_at_score_beat(score_beat),
            }
        )
        shared_events.publish(
            LiveRuntimeStatusEvent(
                type="runtime:status",
                status=self.current,
            )
        )

    def start_follow(self, **kwargs: object) -> RuntimeStatus:
        self.start_args = kwargs
        config = kwargs["config"]
        assert isinstance(config, RuntimeConfig)
        write_fixture_midi(paths.session_dir(config.run_id) / "solo.mid")
        start_measure = kwargs.get("start_measure")
        start_score_beat = 0.0
        if isinstance(start_measure, int):
            start_score_beat = (
                self.projection.timeline.tick_at(start_measure - 1)
                / self.projection.timeline.document.canonical_ppq
            )
        self.current = RuntimeStatus(
            run_id=config.run_id,
            run_mode=config.run_mode,
            phase="active",
            state_word="Leading",
            monotonic_time=10.0,
            score_beat=start_score_beat,
            confidence=1.0,
            tempo_bpm=config.initial_tempo_bpm,
            orchestra_tempo_bpm=config.initial_tempo_bpm,
            orchestra_volume=config.orchestra_volume,
            orchestra_renderer_state="ready",
            orchestra_renderer_zone_id="room_center",
            orchestra_renderer_loaded_instruments=4,
            orchestra_renderer_total_instruments=4,
            section_mode="LEAD",
            coordinate_system="midi_performance_provisional",
            score_position=self._position_at_score_beat(start_score_beat),
        )
        shared_events.publish(
            LiveRuntimeStatusEvent(
                type="runtime:status",
                status=self.current,
            )
        )
        shared_events.publish(
            HardwareStatusEvent(
                type="hardware:status",
                status=LiveStatusResponse(
                    phase=HardwareJobPhase.RUNNING,
                    kind="live_follow",
                    running=True,
                    message="Following Fake CLP-795GP USB",
                    started_at=utc_now(),
                    session_id=config.run_id,
                ),
            )
        )
        # Exercise the causal cursor contract with runtime positions rather
        # than resurrecting the fixed hardware transport. Several spaced
        # updates keep the assertion deterministic across browser scheduling.
        for index, delay in enumerate((0.25, 0.5, 0.75, 1.0), start=1):
            timer = threading.Timer(
                delay,
                self._publish_position,
                args=(start_score_beat + index * 0.25,),
            )
            timer.daemon = True
            timer.start()
        return self.current

    def stop(self) -> RuntimeStatus | None:
        if self.current is None:
            return None
        self.current = self.current.model_copy(
            update={"phase": RunPhase.COMPLETED, "state_word": LiveStateWord.SILENT}
        )
        shared_events.publish(
            LiveRuntimeStatusEvent(
                type="runtime:status",
                status=self.current,
            )
        )
        shared_events.publish(
            HardwareStatusEvent(
                type="hardware:status",
                status=LiveStatusResponse(
                    phase=HardwareJobPhase.COMPLETED,
                    kind="live_follow",
                    running=False,
                    message="Live follow stopped",
                    session_id=self.current.run_id,
                ),
            )
        )
        return self.current

    def update_tempo(self, tempo_bpm: float) -> RuntimeStatus:
        if self.current is None:
            raise RuntimeError("No live performance is running")
        if tempo_bpm == 199:
            raise RuntimeError("Simulated Yamaha tempo update failure")
        self.current = self.current.model_copy(
            update={"tempo_bpm": tempo_bpm, "orchestra_tempo_bpm": tempo_bpm}
        )
        return self.current

    def update_volume(self, volume: float) -> RuntimeStatus:
        if self.current is None:
            raise RuntimeError("No live performance is running")
        if volume == 0.99:
            raise RuntimeError("Simulated Yamaha volume update failure")
        self.current = self.current.model_copy(update={"orchestra_volume": volume})
        return self.current


@pytest.fixture()
def live_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project_root = Path(__file__).resolve().parents[1]
    static_index = project_root / "src/aimusic/server/static/index.html"
    if not static_index.exists():
        pytest.fail("Web assets missing. Run 'cd webapp && npm run build' before tests.")

    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    # Browser CI deliberately does not pull the large DVC score PDF. Serve
    # enough blank pages to cover the real measure-box page identities so
    # pdf.js still exercises page turns and overlay geometry. A two-page fake
    # silently made every late-movement cursor check an invalid-page request.
    # Production uses the Joseffy reduction at the same endpoint.
    score_fixture = tmp_path / "performer-score.pdf"
    writer = PdfWriter()
    for _page in range(12):
        writer.add_blank_page(width=612, height=792)
    with score_fixture.open("wb") as stream:
        writer.write(stream)
    monkeypatch.setattr(routes.paths, "score_display_pdf_path", lambda *_: score_fixture)
    monkeypatch.setattr(routes.paths, "score_bundle_missing_artifacts", lambda *_: [])
    fake_live_control = ScoreAwareFakeLiveControl()
    fake_live_runtime = OrchestraLedFakeLiveRuntime()
    fake_live_control.runtime = fake_live_runtime
    monkeypatch.setattr(routes, "live_control", fake_live_control)
    monkeypatch.setattr(routes, "live_runtime", fake_live_runtime)
    monkeypatch.setattr(routes, "alignment_worker", FakeAlignmentWorker())
    monkeypatch.setattr(routes, "review_worker", FakeReviewWorker())

    app = create_app()
    port = _find_open_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for(lambda: _server_ready(port), timeout=5)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        with shared_events._lock:
            shared_events._queues.clear()
            shared_events._loop = None


def _find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_ready(port: int) -> bool:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/", timeout=0.2)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


def _wait_for(predicate, timeout: float) -> None:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError("Server did not start in time")


def _assert_intersects_viewport(page: Page, test_id: str) -> None:
    """Assert a performer surface remains on-screen without scrolling.

    The score shell is intentionally taller than some desktop viewports, so
    intersection is the correct contract: the score and its position overlay
    must remain visible while the adjacent rehearsal controls are operated.
    """

    locator = page.get_by_test_id(test_id)
    expect(locator).to_be_visible()
    assert locator.evaluate(
        """element => {
          const box = element.getBoundingClientRect();
          return box.bottom > 0 && box.top < window.innerHeight &&
            box.right > 0 && box.left < window.innerWidth;
        }"""
    )


def _record_selected_passage(page: Page, *, with_orchestra_cue: bool) -> None:
    """Use the single passage action with its explicit per-pass cue switch."""

    cue = page.get_by_role("checkbox", name=re.compile(r"Orchestra cue"))
    expect(cue).to_be_visible()
    if with_orchestra_cue:
        cue.check()
    else:
        cue.uncheck()
    page.get_by_role("button", name="Record pass").click()


def _stop_and_keep_performance(page: Page) -> None:
    """Cross the explicit scratch -> rehearsal-evidence boundary."""

    page.get_by_role("button", name="Stop Take").click()
    decision = page.get_by_test_id("after-performance-recording")
    expect(decision).to_contain_text("Performance recorded automatically")
    decision.get_by_role("button", name=re.compile(r"Keep for .*review")).click()
    expect(decision).not_to_be_visible()
    _open_inspector(page)


def _open_inspector(page: Page) -> None:
    """Reveal the inspection panel.

    Takes, device setup, and detailed rehearsal controls live behind a collapsed
    panel so the score owns the screen (issue #153). Tests that drive those
    controls have to open it first, exactly as the performer does.
    """

    toggle = page.get_by_test_id("inspector-toggle")
    toggle.wait_for(state="visible")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
        page.get_by_test_id("inspector").wait_for(state="visible")
    rehearsal_details = page.locator("details.rehearsal-details")
    if rehearsal_details.count() and rehearsal_details.get_attribute("open") is None:
        rehearsal_details.evaluate("element => { element.open = true; }")


def _close_inspector(page: Page) -> None:
    toggle = page.get_by_test_id("inspector-toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
        expect(page.get_by_test_id("inspector")).to_have_count(0)


def test_go_live_starts_orchestra_opening_and_exposes_the_solo_handoff(
    page: Page, live_server: str
) -> None:
    follow_requests: list[dict[str, object]] = []
    tempo_requests: list[dict[str, object]] = []
    volume_requests: list[dict[str, object]] = []
    nonexistent_session_requests: list[str] = []

    def remember_follow_request(request) -> None:
        if request.url.endswith("/api/runtime/follow/start"):
            follow_requests.append(request.post_data_json)
        if request.url.endswith("/api/runtime/tempo"):
            tempo_requests.append(request.post_data_json)
        if request.url.endswith("/api/runtime/volume"):
            volume_requests.append(request.post_data_json)
        if re.search(r"/api/sessions/live-[^/]+$", request.url):
            nonexistent_session_requests.append(request.url)

    page.on("request", remember_follow_request)
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)

    action_rail = page.get_by_test_id("perform-action-rail")
    expect(action_rail).to_contain_text("♩ =")
    live_intent = page.get_by_test_id("perform-live")
    expect(live_intent).to_contain_text("Go live")

    tempo = page.get_by_role("spinbutton", name="Quarter-note tempo in beats per minute")
    tempo.fill("88")
    volume = page.get_by_role("slider", name="Orchestra volume")
    volume.fill("42")
    page.get_by_text("Advanced Yamaha timing", exact=True).click()
    output_advance = page.get_by_role("slider", name="Yamaha output advance in milliseconds")
    # The output-advance control is coarse (10 ms steps) for by-ear calibration.
    output_advance.fill("20")
    page.reload(wait_until="networkidle")
    _open_inspector(page)
    expect(tempo).to_have_value("88")
    expect(action_rail).to_contain_text("♩ = 88")
    page.get_by_text("Advanced Yamaha timing", exact=True).click()
    expect(output_advance).to_have_value("20")

    live_intent.click()
    _wait_for(lambda: len(follow_requests) == 1, timeout=5)

    config = follow_requests[0]["config"]
    assert isinstance(config, dict)
    assert config["initial_tempo_bpm"] == 88
    assert config["orchestra_volume"] == 0.42
    assert config["output_advance_ms"] == 20
    assert config["mix_enabled"] is False
    assert follow_requests[0]["output_name"] == "Fake Synth"
    run_id = str(config["run_id"])
    assert follow_requests[0]["start_measure"] is None
    assert "keep_recording" not in follow_requests[0]
    expect(page.locator(".state-word")).to_have_text("Leading")
    expect(live_intent).to_contain_text("Stop live")
    expect(page.get_by_test_id("score-position-cursor")).to_contain_text("measure 1")

    tempo.fill("72")
    page.wait_for_timeout(250)
    _wait_for(lambda: len(tempo_requests) == 1, timeout=5)
    assert tempo_requests[0] == {"tempo_bpm": 72}

    volume.fill("25")
    page.wait_for_timeout(200)
    _wait_for(lambda: len(volume_requests) == 1, timeout=5)
    assert volume_requests[0] == {"volume": 0.25}
    expect(volume).to_have_value("25")

    volume.fill("99")
    page.wait_for_timeout(200)
    expect(page.get_by_text(re.compile("Simulated Yamaha volume update failure"))).to_be_visible()
    expect(volume).to_have_value("25")

    # A rejected live edit returns both controls to the last server-confirmed
    # tempo; the UI must not silently display a value that was never applied.
    tempo.fill("199")
    page.wait_for_timeout(250)
    expect(page.get_by_text(re.compile("Simulated Yamaha tempo update failure"))).to_be_visible()
    expect(tempo).to_have_value("72")

    projection = score_projection("chopin_op11", 2)
    measure_two_source_tick = projection.source_tick_at_score_tick(4 * 960)
    measure_two = projection.position_at_source_tick(measure_two_source_tick)
    shared_events.publish(
        LiveRuntimeStatusEvent(
            type="runtime:status",
            status=RuntimeStatus(
                run_id=run_id,
                run_mode="performance",
                phase="active",
                state_word="Leading",
                monotonic_time=12.0,
                score_beat=measure_two_source_tick / 960,
                confidence=1.0,
                tempo_bpm=64.0,
                section_mode="LEAD",
                coordinate_system="midi_performance_provisional",
                score_position=RuntimeScorePosition(
                    **measure_two.__dict__,
                    mapping_id=projection.mapping_id,
                    mapping_review_state=projection.mapping_review_state.value,
                    canonical_position=projection.canonical_position,
                ),
            ),
        )
    )
    expect(page.get_by_test_id("score-position-cursor")).to_contain_text("measure 2")
    live_intent.click()
    expect(live_intent).to_contain_text("Go live")
    live_recording = page.get_by_test_id("after-performance-recording")
    expect(live_recording).to_contain_text("Performance recorded automatically")
    expect(live_recording).to_contain_text(run_id)
    expect(live_recording.get_by_role("button", name="Hear piano on Yamaha")).to_be_visible()
    expect(live_recording.get_by_role("button", name="Preview on Mac")).to_be_visible()
    live_recording.get_by_role("button", name=re.compile(r"Keep for .*review")).click()
    expect(live_recording).not_to_be_visible()
    expect(page.get_by_text("Performance kept as a rehearsal take.")).to_be_visible()
    assert nonexistent_session_requests == []


def test_score_context_starts_orchestra_led_live_entry_without_remote_control(
    page: Page, live_server: str
) -> None:
    follow_requests: list[dict[str, object]] = []

    def remember_follow_request(request) -> None:
        if request.url.endswith("/api/runtime/follow/start"):
            follow_requests.append(request.post_data_json)

    page.on("request", remember_follow_request)
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    _close_inspector(page)

    printed_measure = page.locator(
        'svg.pdf-overlay-svg rect[role="button"][aria-label^="Measure 12"]'
    )
    expect(printed_measure).to_be_visible()
    printed_measure.click(button="right", position={"x": 24, "y": 32})

    menu = page.get_by_test_id("score-context-menu")
    expect(menu).to_be_visible()
    start_here = menu.get_by_role("menuitem", name="Start live with orchestra here")
    expect(start_here).to_be_visible()
    expect(menu).to_contain_text("Starts immediately")
    expect(menu).to_contain_text("Join when you have the pulse")
    start_here.click()

    _wait_for(lambda: len(follow_requests) == 1, timeout=5)
    assert follow_requests[0]["start_measure"] == 12
    live_intent = page.get_by_test_id("perform-live")
    expect(live_intent).to_contain_text("Stop live")
    live_intent.click()
    expect(page.get_by_test_id("perform-action-rail")).to_contain_text("Selected m. 12")


def test_failed_live_start_is_explained_and_never_offered_as_a_performance(
    page: Page, live_server: str
) -> None:
    follow_requests: list[dict[str, object]] = []

    page.on(
        "request",
        lambda request: follow_requests.append(request.post_data_json)
        if request.url.endswith("/api/runtime/follow/start")
        else None,
    )
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )
    page.get_by_test_id("perform-live").click()
    _wait_for(lambda: len(follow_requests) == 1, timeout=5)
    expect(page.get_by_test_id("perform-live")).to_contain_text("Stop live")
    config = follow_requests[0]["config"]
    assert isinstance(config, dict)
    run_id = str(config["run_id"])

    shared_events.publish(
        HardwareStatusEvent(
            type="hardware:status",
            status=LiveStatusResponse(
                phase=HardwareJobPhase.FAILED,
                kind="live_follow",
                running=False,
                message="Live MIDI job failed: VST worker room_center did not become ready",
                session_id=run_id,
            ),
        )
    )

    expect(page.get_by_role("alert")).to_contain_text(
        "Orchestra did not start: VST worker room_center did not become ready"
    )
    expect(page.get_by_role("alert")).to_contain_text("No performance was recorded")
    expect(page.get_by_test_id("after-performance-recording")).to_have_count(0)
    # Tear down the fake runtime too; production transitions both runtime and
    # hardware state when its worker exits.
    page.get_by_test_id("perform-live").click()
    expect(page.get_by_test_id("perform-live")).to_contain_text("Go live")


def test_idle_stage_keeps_live_record_and_sound_together_without_diagnostics(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/app/", wait_until="networkidle")

    # The score is the whole idle surface. The former prose-heavy intent cards
    # are gone; live, record, and the nearby sound drawer remain in one line.
    expect(page.get_by_test_id("intent-launcher")).to_have_count(0)
    rail = page.get_by_test_id("perform-action-rail")
    expect(rail).to_be_visible()
    expect(page.get_by_test_id("perform-live")).to_contain_text("Go live")
    expect(page.get_by_test_id("orchestra-readiness")).to_contain_text("Orchestra Ready")
    expect(page.get_by_test_id("orchestra-readiness")).to_contain_text(
        "Yamaha MIDI output is ready"
    )
    expect(rail.get_by_role("button", name=re.compile(r"^Record m\. \d+$"))).to_be_visible()
    sound = page.get_by_test_id("inspector-toggle")
    expect(sound).to_contain_text("Sound · 75%")
    assert page.get_by_role("button", name=re.compile(r"Start orchestra \+ go live")).count() == 0
    assert page.get_by_text("Play for", exact=True).count() == 0

    sound.click()
    drawer = page.get_by_test_id("inspector")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Piano input")
    expect(drawer).to_contain_text("Orchestra output")
    expect(drawer).to_contain_text("Local Control on the piano itself")
    expect(page.get_by_test_id("intent-launcher")).to_have_count(0)


def test_orchestra_readiness_reports_loading_progress_then_real_audio_ready(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.get_by_test_id("inspector-toggle").click()
    page.get_by_role("combobox", name="Orchestra output").select_option(
        label="LG soundbar · BBCSO"
    )
    page.get_by_role("button", name="Close sound controls").click()
    readiness = page.get_by_test_id("orchestra-readiness")
    expect(readiness).to_contain_text("Orchestra Ready")

    shared_events.publish(
        OrchestraRendererStatusEvent(
            type="runtime:renderer_status",
            status=OrchestraRendererStatus(
                state="loading",
                preload_id="browser-readiness",
                zone_id="room_center",
                device_name="LG TV SSCR2",
                instrument_id="low_strings",
                loaded_instruments=1,
                total_instruments=4,
                updated_at_monotonic=10.0,
                message="Loading BBCSO 2 of 4 · low strings",
            ),
        )
    )

    expect(readiness).to_contain_text("Orchestra Loading")
    expect(readiness).to_contain_text("Loading BBCSO 2 of 4 · low strings")
    expect(readiness.get_by_role("progressbar", name="BBCSO ensembles loaded")).to_have_attribute(
        "value", "1"
    )
    expect(page.get_by_test_id("perform-live")).to_be_disabled()

    shared_events.publish(
        OrchestraRendererStatusEvent(
            type="runtime:renderer_status",
            status=OrchestraRendererStatus(
                state="opening_audio",
                preload_id="browser-readiness",
                zone_id="room_center",
                device_name="LG TV SSCR2",
                loaded_instruments=4,
                total_instruments=4,
                updated_at_monotonic=11.0,
                message="BBCSO loaded · opening the LG soundbar audio stream",
            ),
        )
    )
    expect(readiness).to_contain_text("opening the LG soundbar audio stream")

    shared_events.publish(
        OrchestraRendererStatusEvent(
            type="runtime:renderer_status",
            status=OrchestraRendererStatus(
                state="ready",
                preload_id="browser-readiness",
                zone_id="room_center",
                device_name="LG TV SSCR2",
                loaded_instruments=4,
                total_instruments=4,
                updated_at_monotonic=12.0,
                message="BBCSO orchestra ready on the LG soundbar",
            ),
        )
    )
    expect(readiness).to_contain_text("Orchestra Ready")
    expect(readiness).to_contain_text("BBCSO streaming to LG soundbar")
    expect(page.get_by_test_id("perform-live")).to_be_enabled()


def test_output_picker_persists_and_selects_exactly_one_orchestra_renderer(
    page: Page, live_server: str
) -> None:
    follow_requests: list[dict[str, object]] = []
    page.on(
        "request",
        lambda request: follow_requests.append(request.post_data_json)
        if request.url.endswith("/api/runtime/follow/start")
        else None,
    )
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.get_by_test_id("inspector-toggle").click()
    output = page.get_by_role("combobox", name="Orchestra output")
    output.select_option(label="LG soundbar · BBCSO")
    page.reload(wait_until="networkidle")
    page.get_by_test_id("inspector-toggle").click()
    expect(output).to_have_value("none")
    page.get_by_role("button", name="Close sound controls").click()

    page.get_by_test_id("perform-live").click()
    _wait_for(lambda: len(follow_requests) == 1, timeout=5)
    config = follow_requests[0]["config"]
    assert isinstance(config, dict)
    assert follow_requests[0]["output_name"] == ""
    assert config["mix_enabled"] is True
    page.get_by_test_id("perform-live").click()


def test_capture_toast_resolves_in_place_and_button_rearms_before_alignment(
    page: Page, live_server: str
) -> None:
    base = f"{live_server}/app/"
    page.goto(base, wait_until="networkidle")
    _open_inspector(page)

    # The take-events socket connects on mount (design doc §2.7).
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )

    # The score has already selected the next passage. This choice starts
    # immediately while preserving the displayed score location as intent.
    cue_switch = page.get_by_role("checkbox", name=re.compile(r"Orchestra cue"))
    cue_switch.uncheck()
    record_button = page.get_by_role("button", name="Record pass")
    expect(record_button).to_be_enabled()
    record_button.click()

    stop_button = page.get_by_role("button", name="Stop Take")
    expect(stop_button).to_be_visible()
    stop_button.click()
    decision = page.get_by_test_id("after-performance-recording")
    expect(decision).to_be_visible()
    expect(page.locator(".toast.capture-toast")).not_to_be_visible()
    decision.get_by_role("button", name=re.compile(r"Keep for .*review")).click()
    expect(decision).not_to_be_visible()
    _open_inspector(page)
    page.get_by_role("button", name="Silence").click()
    # Terminal runtime status is history, not an active recording owner.
    expect(decision).not_to_be_visible()

    # The take lifecycle begins only after the performer explicitly keeps it.
    toast = page.locator(".toast.capture-toast")
    expect(toast).to_contain_text("placing it in the score")
    expect(toast).not_to_have_class(re.compile(r"\bresolved\b"))

    # ...and the button re-arms right away -- it must not wait for the
    # background alignment worker (still pending: FAKE_ALIGNMENT_DELAY_SECONDS
    # hasn't elapsed yet). This is the auto-arm contract from design doc
    # §3.1: "non-playing time between takes < 3 seconds."
    expect(page.get_by_role("button", name="Record pass")).to_be_enabled()

    # take:alignment_done resolves the *same* toast in place once the fake
    # worker finishes (well within Playwright's default 5s assertion timeout).
    expect(toast).to_contain_text("kept")
    expect(toast).to_have_class(re.compile(r"\bresolved\b"))


def test_capture_flows_into_one_press_accompanied_review_and_record_again(
    page: Page, live_server: str
) -> None:
    score_transport_requests: list[str] = []
    page.on(
        "request",
        lambda request: score_transport_requests.append(request.url)
        if "/score-transport" in request.url
        else None,
    )
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )

    _record_selected_passage(page, with_orchestra_cue=False)
    _stop_and_keep_performance(page)

    after = page.get_by_test_id("after-take-card")
    expect(after).to_be_visible()
    expect(
        after.get_by_role("button", name=re.compile(r"Hear .* with orchestra on Yamaha"))
    ).to_be_enabled()
    hear = after.get_by_role("button", name=re.compile(r"Hear .* with orchestra on Yamaha"))
    expect(hear).to_be_enabled()
    hear.click()
    expect(page.locator(".state-word")).to_have_text("Playing")

    page.get_by_role("button", name="Stop current playback", exact=True).click()
    page.get_by_role("button", name="Preview here").click()
    _wait_for(lambda: len(score_transport_requests) == 1, timeout=5)
    expect(page.get_by_test_id("score-position-cursor")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"Record another pass"))).to_be_enabled()


def test_take_bank_reviews_one_selected_passage_solo_or_with_orchestra(
    page: Page, live_server: str
) -> None:
    """The score location, take choice, and sound choice form one workflow."""

    hardware_review_requests: list[dict[str, object]] = []
    review_downloads: list[str] = []

    def remember_review_request(request) -> None:
        if "/review/play" in request.url:
            hardware_review_requests.append(request.post_data_json)
        if "/review/midi" in request.url:
            review_downloads.append(request.url)

    page.on("request", remember_review_request)
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )

    _record_selected_passage(page, with_orchestra_cue=False)
    _stop_and_keep_performance(page)
    expect(page.get_by_role("button", name=re.compile(r"Show measure \d+ on score"))).to_be_visible(
        timeout=5000
    )

    # Re-enter with the recording already persisted. The score location, not
    # a global take counter, restores the relevant passage review in context.
    page.reload(wait_until="networkidle")
    _open_inspector(page)
    expect(page.get_by_test_id("passage-review-card")).to_contain_text(
        re.compile(r"1\s+aligned pass")
    )
    expect(
        page.get_by_role("button", name=re.compile(r"Show measure \d+ on score"))
    ).to_be_visible()

    bank = page.locator(".takes-list-container")
    expect(bank).to_contain_text("Recordings are filed by where you entered in the score")
    expect(bank).to_contain_text("Repeated passes from the same location stay together")
    expect(bank).to_contain_text("From measure")
    expect(bank).to_contain_text(re.compile(r"mm\.\s*\d+–\d+"))
    compact_row = bank.locator(".compact-recording-row")
    expect(compact_row).to_be_visible()
    expect(compact_row.get_by_role("combobox")).to_be_visible()

    bank.get_by_role("button", name=re.compile(r"Show measure \d+ on score")).click()
    review = page.get_by_test_id("passage-review-card")
    expect(review).to_contain_text("Passage recordings")
    expect(review).to_contain_text("Playback begins at m.")
    expect(review).to_contain_text("On Yamaha")
    expect(review.get_by_role("combobox", name="Recording at selected passage")).to_be_visible()

    take_only = review.get_by_role(
        "button", name=re.compile(r"Hear pass from measure \d+ alone on Yamaha from measure")
    )
    expect(take_only).to_be_visible(timeout=5000)
    take_only.click()
    _wait_for(lambda: len(hardware_review_requests) == 1, timeout=5)
    assert hardware_review_requests[0]["variant"] == "solo"
    takes_response = httpx.get(
        f"{live_server}/api/takes",
        params={"piece_id": "chopin_op11", "movement": 2},
    ).json()
    recorded_take = takes_response["takes"][0]
    intended_entry = (
        recorded_take.get("cue", {}).get("target_beat")
        if recorded_take.get("cue")
        else recorded_take["placement_hint"]["target_beat"]
    )
    assert hardware_review_requests[0]["start_score_beat"] == pytest.approx(intended_entry)

    page.get_by_role("button", name="Stop current playback", exact=True).click()
    review.get_by_text("Recording options", exact=True).click()
    review.get_by_role(
        "button", name=re.compile(r"Preview pass from measure \d+ with orchestra in this browser")
    ).click()
    expect(page.locator(".toast:not(.capture-toast)")).to_contain_text("Loaded Take + orchestra")
    assert any("variant=ensemble" in url for url in review_downloads)
    expect(page.get_by_test_id("score-position-cursor")).to_be_visible()


def test_repeated_recordings_are_grouped_by_score_location_not_global_take_number(
    page: Page, live_server: str
) -> None:
    """The PDF location owns the mental model; chronology is local to it."""

    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )

    _record_selected_passage(page, with_orchestra_cue=False)
    _stop_and_keep_performance(page)

    library = page.locator(".takes-list-container")
    group = library.get_by_test_id("passage-take-group")
    expect(group).to_have_count(1, timeout=5000)
    expect(group).to_contain_text("1 aligned pass")
    compact_picker = group.locator(".compact-recording-row").get_by_role("combobox")
    expect(compact_picker.locator("option")).to_have_count(1)

    entry_measure = group.get_attribute("data-entry-measure")
    assert entry_measure
    group.get_by_role("button", name=f"Show measure {entry_measure} on score").click()

    _record_selected_passage(page, with_orchestra_cue=False)
    _stop_and_keep_performance(page)

    expect(group).to_contain_text("2 aligned passes", timeout=5000)
    group.get_by_role("button", name=f"Show measure {entry_measure} on score").click()
    picker = page.get_by_role("combobox", name="Recording at selected passage")
    expect(picker.locator("option")).to_have_count(2)
    expect(compact_picker.locator("option")).to_have_count(2)
    expect(compact_picker.locator("option").first).to_contain_text(
        "Strong alignment (90% score match)"
    )
    expect(compact_picker.locator("option").first).to_contain_text(
        re.compile(r"\d{1,2}:\d{2}\s(?:AM|PM)")
    )
    expect(library).not_to_contain_text("Take 1")
    page.get_by_test_id("mode-data").click()
    page.get_by_role("tab", name="Geometry").click()
    expect(page.get_by_test_id(f"score-measure-label-{entry_measure}")).to_contain_text("2 passes")
    guide = page.get_by_test_id("score-guide")
    expect(guide).to_contain_text("Green · ready")
    expect(guide).to_contain_text("another green pass is optional")

    page.get_by_test_id("mode-perform").click()
    _open_inspector(page)
    group.get_by_role("button", name=f"Show measure {entry_measure} on score").click()
    _record_selected_passage(page, with_orchestra_cue=False)
    _stop_and_keep_performance(page)
    expect(group).to_contain_text("3 aligned passes", timeout=5000)

    takes_response = httpx.get(
        f"{live_server}/api/takes",
        params={"piece_id": "chopin_op11", "movement": 2},
    ).json()
    covered_measure = takes_response["takes"][-1]["score_span"]["start"]["measure_index"] + 1
    score_page = page.get_by_test_id("performer-score-page")
    _close_inspector(page)
    score_page.locator(f'.measure-hit-target[aria-label^="Measure {covered_measure}"]').click(
        button="right"
    )

    menu = page.get_by_test_id("score-context-menu")
    expect(menu).to_be_visible()
    # The menu heading now shows the beat label; the measure it targets is on the
    # accessible label rather than printed as "Measure N".
    expect(menu).to_have_attribute("aria-label", f"Actions for measure {covered_measure}")
    expect(menu.get_by_role("menuitem", name="Start orchestra here & record")).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Hear latest take")).to_be_visible()
    menu.get_by_role("menuitem", name="Open passage details").click()
    expect(menu).not_to_be_visible()
    expect(page.get_by_test_id("alignment-quality")).to_contain_text(
        "Strong alignment · 90% score-note match"
    )


def test_anchor_edits_stay_on_the_score_and_every_destructive_action_is_undoable(
    page: Page,
    live_server: str,
) -> None:
    """The performer can repair an accidental anchor cluster without leaving the notes."""

    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.get_by_test_id("mode-data").click()
    page.get_by_role("tab", name="Geometry").click()
    # Strip-button title is "Measure {n} — page {p} ({state})".
    page.locator('.measure-strip button[title^="Measure 44 "][title*="(uncovered)"]').click()
    score_page = page.get_by_test_id("performer-score-page")
    target = score_page.locator('.measure-hit-target[aria-label^="Measure 44"]')
    expect(target).to_be_visible(timeout=15000)

    target.click(button="right", position={"x": 20, "y": 40})
    menu = page.get_by_test_id("score-context-menu")
    expect(menu).to_be_visible()
    # Menu heading now reads "m. {n} · beat {label}".
    expect(menu).to_contain_text("m. 44 · beat")
    menu.get_by_test_id("menu-assertion-anchor.add").click()
    notice = page.get_by_test_id("anchor-notice")
    expect(notice).to_contain_text("Anchor added at m. 44")
    expect(notice.get_by_role("button", name="Undo")).to_be_visible()

    anchors = httpx.get(
        f"{live_server}/api/scores/2/anchors",
        params={"piece_id": "chopin_op11"},
    ).json()["anchors"]
    assert len(anchors) == 1

    anchor_marker = score_page.locator(
        '.anchor-marker[aria-label^="Reactive anchor in measure 44"]'
    )
    expect(anchor_marker).to_have_count(1)
    anchor_marker.click(button="right")
    menu.get_by_test_id("menu-assertion-anchor.remove").click()
    expect(notice).to_contain_text("Anchor removed from m. 44")
    assert (
        httpx.get(
            f"{live_server}/api/scores/2/anchors",
            params={"piece_id": "chopin_op11"},
        ).json()["anchors"]
        == []
    )
    notice.get_by_role("button", name="Undo").click()
    expect(notice).to_contain_text("Anchor change undone")
    expect(anchor_marker).to_have_count(1)

    # Exercise the actual 36px direct-manipulation target, not only the move API.
    marker_box = anchor_marker.bounding_box()
    assert marker_box is not None
    drag_x = marker_box["x"] + marker_box["width"] / 2
    drag_y = marker_box["y"] + marker_box["height"] / 2
    hit_class = page.evaluate(
        """([x, y]) => {
            const element = document.elementFromPoint(x, y);
            return element
                ? `${element.tagName}:${element.getAttribute('class') ?? ''}`
                : 'no element';
        }""",
        [drag_x, drag_y],
    )
    assert "anchor-marker" in hit_class, (
        f"{hit_class}; drag=({drag_x}, {drag_y}); marker={marker_box}"
    )
    page.mouse.move(drag_x, drag_y)
    page.mouse.down()
    page.mouse.move(drag_x + 35, drag_y, steps=5)
    page.mouse.up()
    expect(notice).to_contain_text("Anchor moved within m. 44")
    moved_anchors = httpx.get(
        f"{live_server}/api/scores/2/anchors",
        params={"piece_id": "chopin_op11"},
    ).json()["anchors"]
    assert moved_anchors[0]["score_tick"] != anchors[0]["score_tick"]
    notice.get_by_role("button", name="Undo").click()
    expect(notice).to_contain_text("Anchor change undone")

    target.click(button="right", position={"x": 90, "y": 40})
    menu.get_by_test_id("menu-assertion-anchor.add").click()
    expect(anchor_marker).to_have_count(2)
    notice.get_by_role("button", name="Dismiss anchor message").click()

    target.click(button="right", position={"x": 120, "y": 40})
    menu.get_by_test_id("menu-assertion-anchor.clearSpan").click()
    expect(anchor_marker).to_have_count(0)
    expect(notice).to_contain_text("2 anchors removed from m. 44")
    notice.get_by_role("button", name="Undo").click()
    expect(anchor_marker).to_have_count(2)


def test_incomplete_attempt_stays_at_its_passage_with_cued_retry(
    page: Page, live_server: str, tmp_path: Path
) -> None:
    """A zero-note stop remains a local, recoverable attempt instead of advancing."""

    empty_midi = tmp_path / "empty.mid"
    MidiFile().save(empty_midi)
    take_store.save_take(
        "chopin_op11",
        2,
        empty_midi,
        take_id="t20260719T220105Z-a359",
        placement_hint=TakePlacementHint(target_beat=44.0),
        status=take_store.UNALIGNABLE,
    )

    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)

    # Stays at m.12 because this take was recorded *at* m.12 -- a pinned passage,
    # not the suggested next target.
    target = page.get_by_test_id("next-take-target")
    expect(target).to_contain_text("Record from m. 12")
    cue = page.get_by_role("checkbox", name=re.compile(r"Orchestra cue"))
    expect(cue).to_be_checked()
    expect(target).to_contain_text("orchestra starts at m. 12 and keeps playing")
    expect(target).to_contain_text("come in whenever you are ready")
    expect(page.get_by_test_id("after-take-card")).to_contain_text("No piano notes were recorded")

    review = page.get_by_test_id("passage-review-card")
    expect(review.get_by_role("combobox", name="Recording at selected passage")).to_have_value(
        "t20260719T220105Z-a359"
    )
    expect(review).to_contain_text("Incomplete attempt")
    expect(review).to_contain_text("Nothing musical was captured")
    expect(review.get_by_text("Recording options", exact=True)).to_be_visible()


def test_score_stays_visible_and_tracks_cue_take_coverage_and_review(
    page: Page, live_server: str
) -> None:
    """Lock the at-piano score-first workflow against the reported regression.

    This is deliberately one journey instead of isolated DOM assertions: the
    failure was architectural.  A performer must be able to keep reading the
    score while starting a cue, recording, stopping, seeing the placed span
    and changed coverage, and reviewing the take with orchestra.
    """

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    tempo = page.get_by_role("spinbutton", name="Quarter-note tempo in beats per minute")
    tempo.fill("120")
    tempo.press("Enter")
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )

    # The score owns the viewport (issue #153). It was previously required to
    # share the screen with the take controls; that side-by-side workstation is
    # what made the page too busy to read while playing, so the controls now
    # live in the opt-in inspection panel below. The invariant that still
    # matters -- and the one that actually protects the performer -- is that the
    # score itself is never scrolled off while a take is running.
    # Operating a panel control scrolls to the panel, which is below the score --
    # that detour is now deliberate rather than forbidden, so viewport
    # intersection is no longer the contract here. What must still hold is that
    # the score stays rendered and reachable, and that the app pulls the active
    # system back into view as the cursor advances (PdfCoverageOverlay's
    # `scrollActiveSystemIntoView`, which runs in every mode).
    score_shell = page.get_by_test_id("performer-score-shell")
    expect(score_shell).to_be_visible()
    score_shell.scroll_into_view_if_needed()
    _assert_intersects_viewport(page, "performer-score-shell")
    _assert_intersects_viewport(page, "performer-score-page")
    expect(page.get_by_test_id("rehearsal-controls")).to_be_visible()
    page.get_by_test_id("mode-data").click()
    page.get_by_role("tab", name="Geometry").click()
    # m.16 contains no orchestral note. Readiness describes whether Rubato can
    # execute its own part, so an unrehearsed piano-only bar is green, not amber.
    expect(page.get_by_test_id("score-measure-16")).to_have_attribute("fill", "var(--ready)")
    expect(page.get_by_test_id("score-measure-16")).to_have_attribute(
        "aria-label", re.compile(r"orchestra has no notes")
    )
    # Strip-button title is "Measure {n} — page {p} ({state})"; match by measure
    # and state without pinning the page number.
    expect(
        page.locator('.measure-strip button[title^="Measure 16 "][title*="(ready)"]')
    ).to_have_count(1)
    page.get_by_test_id("mode-perform").click()
    _open_inspector(page)
    cue_switch = page.get_by_role("checkbox", name=re.compile(r"Orchestra cue"))
    expect(cue_switch).to_be_checked()
    expect(page.get_by_role("button", name="Record pass")).to_be_visible()

    # m.13: the suggested target skips m.12, which holds only the pickup while
    # the orchestra finishes the introduction (Decision 0015).
    expect(
        page.get_by_text(re.compile(r"On · orchestra starts at m\. 13 and keeps playing"))
    ).to_be_visible()
    page.get_by_role("button", name="Record pass").click()

    # A request-in-flight state closes the double-submit window before the
    # backend response arrives. The secondary row is hidden once capture
    # starts, while the persistent selected-target action is disabled.
    expect(page.get_by_role("button", name="Record pass")).to_be_disabled()
    expect(cue_switch).to_be_disabled()

    # Starting a take must bring the score back on screen by itself. The button
    # that started it lives in the panel below, so without this the take begins
    # with score, cursor and beat readout all scrolled away from the performer.
    # Sample the cursor as early as possible. The fixture's cue spans only a few
    # seconds, so any settle-wait taken *before* the baseline reading eats the
    # window and the position legitimately clamps at its final anchor.
    cursor = page.get_by_test_id("score-position-cursor")
    expect(cursor).to_be_visible()
    expect(cursor).to_have_attribute("data-score-beat", re.compile(r"\d"))
    cue_beat_before = float(cursor.get_attribute("data-score-beat") or "nan")
    cursor_line = page.locator(".position-cursor-line")
    expect(page.get_by_test_id("score-now-badge")).to_have_text("Now")
    cursor_x_before = float(cursor_line.get_attribute("x1") or "nan")
    page.wait_for_timeout(500)
    cue_beat_after = float(cursor.get_attribute("data-score-beat") or "nan")
    cursor_x_after = float(cursor_line.get_attribute("x1") or "nan")
    assert cue_beat_after > cue_beat_before
    assert cursor_x_after != cursor_x_before

    # Starting a take must bring the score back on screen by itself.
    page.wait_for_timeout(700)  # smooth scroll settles
    _assert_intersects_viewport(page, "performer-score-shell")
    _assert_intersects_viewport(page, "performer-score-page")

    # The fake cue switches into recording after the score-derived lead-in.
    expect(page.get_by_role("button", name=re.compile(r"Stop Take"))).to_be_visible(timeout=20000)
    _assert_intersects_viewport(page, "performer-score-shell")
    _assert_intersects_viewport(page, "performer-score-page")
    _stop_and_keep_performance(page)

    after = page.get_by_test_id("after-take-card")
    expect(after).to_be_visible()
    expect(after).not_to_contain_text("safe", ignore_case=True)
    expect(after).to_contain_text(re.compile(r"filed|library|aligned", re.IGNORECASE))

    # Alignment must become visible in both spatial representations: the
    # latest take is marked on the score and observed coverage is non-zero.
    page.get_by_test_id("mode-data").click()
    latest_span = page.get_by_test_id("latest-take-span")
    expect(latest_span).to_be_visible(timeout=5000)
    expect(latest_span).to_have_attribute("aria-label", re.compile(r"newest recording", re.I))
    span_start = float(latest_span.get_attribute("data-score-start") or "nan")
    span_end = float(latest_span.get_attribute("data-score-end") or "nan")
    assert span_end > span_start
    expect(latest_span).to_contain_text(re.compile(r"m\.\s*\d+"))
    expect(page.get_by_test_id("score-newest-take-badge")).to_have_text("Newest recording")
    assert page.locator(".latest-take-box").count() >= 1
    coverage_percent = page.get_by_test_id("coverage-percent")
    expect(coverage_percent).to_be_visible()
    expect(coverage_percent).not_to_have_text(re.compile(r"^\s*0(?:\.0+)?%"))

    page.get_by_test_id("mode-perform").click()
    _open_inspector(page)

    # The automatic next-passage suggestion and the PDF must be one state:
    # whatever m. N the card names is visibly labeled and badged on this page.
    next_target = page.get_by_test_id("next-take-target")
    target_copy = next_target.locator(".passage-target-copy strong").inner_text()
    target_match = re.search(r"m\.\s*(\d+)", target_copy)
    assert target_match is not None
    target_measure = target_match.group(1)
    expect(page.get_by_test_id(f"score-measure-label-{target_measure}")).to_be_visible()
    expect(page.get_by_test_id("score-target-badge")).to_have_text("Selected passage")

    hear = after.get_by_role("button", name=re.compile(r"Hear .* with orchestra on Yamaha"))
    expect(hear).to_be_enabled()
    # Sample the pre-playback position BEFORE starting it. This review transport
    # steps once, ~160ms in, then holds that position for the rest of its ~3.6s
    # span. And before playback the cursor has no position at all -- the
    # attribute is empty, not a number -- so "advanced past the previous beat"
    # cannot be the contract. What review playback must do is give the cursor a
    # real score position at the reviewed passage.
    review_beat_before = cursor.get_attribute("data-score-beat") or ""
    hear.click()

    def _review_cursor_positioned() -> bool:
        raw = cursor.get_attribute("data-score-beat") or ""
        if not raw:
            return False
        return float(raw) > (float(review_beat_before) if review_beat_before else 0)

    _wait_for(_review_cursor_positioned, timeout=5)

    # Review playback moves the cursor, so the score comes back on screen the
    # same way a take does -- the performer follows the playback on the page,
    # not the panel button they just pressed.
    page.get_by_test_id("performer-score-shell").scroll_into_view_if_needed()
    _assert_intersects_viewport(page, "performer-score-shell")
    _assert_intersects_viewport(page, "performer-score-page")


def test_real_take_note_landmarks_drive_timestamped_score_screenshots(
    page: Page, live_server: str, tmp_path: Path
) -> None:
    """Reproduce the reported G# -> F# -> final-chord cursor timeline.

    The screenshots are generated at the three performed-note timestamps so a
    browser failure leaves visual evidence alongside the DOM assertions. The
    fake hardware event never opens a MIDI output.
    """

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )
    projection = score_projection("chopin_op11", 2)

    def landmark(elapsed: float, native_tick: int, pitches: list[int]):
        return ScoreTransportAnchorResponse(
            elapsed_seconds=elapsed,
            position=routes._position_response(projection.position_at_source_tick(native_tick * 4)),
            source_tick=native_tick * 4,
            played_pitches=pitches,
            anchor_kind="matched_onset",
        )

    transport = ScoreTransportResponse(
        piece_id="chopin_op11",
        movement=2,
        mapping_id=projection.mapping_id,
        mapping_review_state=projection.mapping_review_state.value,
        canonical_position=False,
        anchors=[
            landmark(10.014, 35_404, [80]),  # G#5, measure 13
            landmark(13.992, 37_493, [78]),  # F#5, measure 14
            landmark(49.253, 55_772, [59, 68, 76]),  # B3-G#4-E5, measure 22
            ScoreTransportAnchorResponse(
                elapsed_seconds=51.632,
                position=routes._position_response(projection.position_at_source_tick(56_928 * 4)),
                source_tick=56_928 * 4,
                anchor_kind="stop",
            ),
        ],
    )

    cursor = page.get_by_test_id("score-position-cursor")
    for label, elapsed, expected_measure in (
        ("g-sharp", 10.014, 13),
        ("f-sharp", 13.992, 14),
        ("final-chord", 49.253, 22),
    ):
        shared_events.publish(
            HardwareStatusEvent(
                type="hardware:status",
                status=LiveStatusResponse(
                    phase=HardwareJobPhase.RUNNING,
                    kind="playback",
                    running=True,
                    message=f"Timeline checkpoint: {label}",
                    started_at=utc_now() - timedelta(seconds=elapsed),
                    session_id="timeline-fixture",
                    score_transport=transport,
                ),
            )
        )
        expect(cursor).to_contain_text(f"measure {expected_measure}")
        expect(page.get_by_test_id("score-now-badge")).to_have_text("Now")
        screenshot = tmp_path / f"review-{elapsed:06.3f}s-{label}.png"
        audit = capture_cursor_audit(
            page,
            expected_measure=expected_measure,
            screenshot_path=screenshot,
        )
        assert audit.score_beat >= 0

    expect(page.get_by_test_id("performer-score-page").locator("canvas")).to_have_attribute(
        "data-rendered-page", "2"
    )


def test_late_live_cursor_measure_page_and_geometry_stay_coherent(
    page: Page, live_server: str, tmp_path: Path
) -> None:
    """Audit the late-movement path where a visual measure drift was reported."""

    page.on(
        "console",
        lambda message: (
            print(f"browser console: {message.type}: {message.text}")
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: print(f"browser page error: {error}"))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )
    projection = score_projection("chopin_op11", 2)

    for measure in (50, 70, 79, 86, 94, 100):
        score_tick = projection.timeline.tick_at(measure - 1)
        source_tick = projection.source_tick_at_score_tick(score_tick)
        position = projection.position_at_source_tick(source_tick)
        shared_events.publish(
            LiveRuntimeStatusEvent(
                type="runtime:status",
                status=RuntimeStatus(
                    run_id="late-cursor-audit",
                    run_mode="performance",
                    phase="active",
                    state_word="Following",
                    monotonic_time=float(measure),
                    score_beat=score_tick / 960,
                    confidence=0.95,
                    tempo_bpm=64.0,
                    section_mode="FOLLOW",
                    coordinate_system="canonical_score",
                    score_position=RuntimeScorePosition(
                        **position.__dict__,
                        mapping_id=projection.mapping_id,
                        mapping_review_state=projection.mapping_review_state.value,
                        canonical_position=projection.canonical_position,
                    ),
                ),
            )
        )
        expect(page.get_by_test_id("score-position-cursor")).to_contain_text(f"measure {measure}")
        capture_cursor_audit(
            page,
            expected_measure=measure,
            screenshot_path=tmp_path / f"live-cursor-m{measure}.png",
        )


def test_major_cursor_failure_can_be_corrected_in_app_at_one_score_beat(
    page: Page, live_server: str
) -> None:
    correction_requests: list[dict[str, object]] = []

    def remember_correction(request) -> None:
        if "/alignment-corrections" in request.url:
            correction_requests.append(request.post_data_json)

    page.on("request", remember_correction)
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    _record_selected_passage(page, with_orchestra_cue=True)
    expect(page.get_by_test_id("score-position-cursor")).to_have_attribute(
        "data-score-beat", re.compile(r"\d")
    )
    page.get_by_test_id("mode-data").click()

    correction = page.get_by_test_id("alignment-correction")
    correction.get_by_text("Cursor not on the sound you heard?", exact=True).click()
    expect(correction).to_contain_text("That sound belongs at m.")
    selected_beat = correction.locator('button[aria-pressed="true"]')
    expect(selected_beat).to_have_count(1)
    correction.get_by_role("button", name="Anchor this beat").click()

    _wait_for(lambda: len(correction_requests) == 1, timeout=5)
    assert correction_requests[0]["beat_in_measure"] in {0, 1, 2, 3}
    assert correction_requests[0]["source_seconds"] >= 0
    expect(page.locator(".toast:not(.capture-toast)")).to_contain_text("Anchor saved")
    page.get_by_role("button", name="Silence").click()


def test_data_timing_score_click_directly_owns_the_audition_target(
    page: Page, live_server: str
) -> None:
    audition_attempts: list[int] = []

    def audition_with_one_teardown_conflict(route) -> None:
        audition_attempts.append(len(audition_attempts) + 1)
        if len(audition_attempts) == 1:
            route.fulfill(
                status=409,
                content_type="application/json",
                body='{"detail":"Live MIDI job already running: alignment_audition"}',
            )
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route(
        re.compile(r".*/api/scores/2/beat-audition/17/play\?.*"),
        audition_with_one_teardown_conflict,
    )
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    page.get_by_test_id("mode-data").click()

    # The score and its global strip are the only measure navigator. There is
    # no duplicate text-entry form or manual refresh button in the side panel.
    expect(page.get_by_label("Go to measure")).to_have_count(0)
    expect(page.get_by_role("button", name="↻")).to_have_count(0)
    # Strip cells and printed measure boxes both call PdfCoverageOverlay's
    # selectMeasure handler; the live-PDF path is also exercised in the visual
    # browser QA and in the existing printed-measure selection regression.
    strip_measure = page.get_by_role("button", name=re.compile(r"^Measure 109 — page 13"))
    strip_measure.click()

    timing_panel = page.locator(".data-facet-panel:not([hidden])")
    expect(timing_panel.get_by_role("heading", name="m.109", exact=True)).to_be_visible()
    expect(timing_panel.get_by_role("button", name="Play m.109 with count-in")).to_have_text(
        "▶ Play"
    )
    expect(timing_panel.get_by_text(re.compile(r"Count in \+ play"))).to_have_count(0)
    expect(strip_measure).to_have_class(re.compile(r"strip-selected"))

    # Review priorities are both explicit by measure number and visually loud
    # in the movement strip. The panel no longer makes the performer decode a
    # count with no destination.
    # M.17 is orchestra-tacet, so its old source disagreement is not an audible
    # accompaniment defect and no longer appears as a required listening target.
    expect(timing_panel.locator(".review-target.needs-listening")).to_have_count(0)
    expect(timing_panel.locator(".review-target.spot-check")).to_have_count(8)
    needs_listening = page.get_by_role("button", name=re.compile(r"^Measure 17 — page"))
    expect(needs_listening).not_to_have_class(re.compile(r"strip-timing-needs-listening"))

    # Right-click is a compact Play shortcut in Timing mode, and it must clear
    # native selection just like left-click. Previously the right-button
    # pointerdown could paint the entire canvas blue before contextmenu fired.
    # M.109 already proved the strip's physical click path above. Invoke the
    # same handler directly here because the grouped horizontal strip's
    # auto-scroll animation can leave another page group over this off-screen
    # cell while Playwright is trying to scroll it back into view.
    needs_listening.dispatch_event("pointerdown", {"button": 0})
    needs_listening.dispatch_event("pointerup", {"button": 0})
    expect(timing_panel.get_by_role("heading", name="m.17", exact=True)).to_be_visible()
    printed_measure = page.get_by_test_id("score-measure-17")
    canvas = page.get_by_test_id("performer-score-page").locator("canvas")
    page.evaluate(
        """canvas => {
          const selection = window.getSelection();
          const range = document.createRange();
          range.selectNode(canvas);
          selection?.removeAllRanges();
          selection?.addRange(range);
        }""",
        canvas.element_handle(),
    )
    printed_measure.click(button="right")
    menu = page.get_by_test_id("score-context-menu")
    expect(menu.get_by_role("menuitem", name="Play")).to_be_visible()
    expect(menu.locator('[data-testid^="menu-assertion-"]')).to_have_count(0)
    selection = page.evaluate(
        """() => ({
          collapsed: window.getSelection()?.isCollapsed ?? true,
          text: window.getSelection()?.toString() ?? '',
        })"""
    )
    assert selection == {"collapsed": True, "text": ""}
    menu.get_by_role("menuitem", name="Play").click()
    page.wait_for_timeout(800)
    assert len(audition_attempts) == 2
    expect(timing_panel.get_by_text(re.compile(r"409|already running"))).to_have_count(0)
    timing_panel.get_by_role("button", name="Stop").click()
    strip_measure.click()
    expect(timing_panel.get_by_role("heading", name="m.109", exact=True)).to_be_visible()

    timing_panel.get_by_text("Clicks don’t line up with the orchestra?", exact=True).click()
    expect(timing_panel).to_contain_text("Put the four numbered clicks on the four printed beats")
    expect(timing_panel).not_to_contain_text("wrong note?")
    expect(timing_panel.locator(".beat-marker")).to_have_count(4)


def test_clicking_printed_measure_starts_a_position_targeted_take(
    page: Page, live_server: str
) -> None:
    requests: list[dict[str, object]] = []

    def remember_target(request) -> None:
        if request.url.endswith("/api/hardware/record-with-cue/start"):
            requests.append(request.post_data_json)

    page.on("request", remember_target)
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    tempo = page.get_by_role("spinbutton", name="Quarter-note tempo in beats per minute")
    tempo.fill("88")
    tempo.press("Enter")

    # The machine-projected solo reference marks the first printed piano
    # entrance as an uncovered target. The PDF itself is the control surface,
    # and explains its visual language where the performer looks.
    page.get_by_test_id("mode-data").click()
    guide = page.get_by_test_id("score-guide")
    expect(guide).to_contain_text("Listen")
    expect(guide).to_contain_text("Spot check")
    expect(guide).to_contain_text("Confirmed")
    expect(page.get_by_text(re.compile(r"machine-detected", re.I))).to_have_count(0)

    page.get_by_test_id("mode-perform").click()
    _open_inspector(page)

    # The first *solo-led* measure is m.13; m.12 carries only the pickup.
    target = page.get_by_test_id("next-take-target")
    expect(target).to_contain_text("m. 13")
    expect(target).to_contain_text("This score location stays attached to the recording")
    expect(target).to_contain_text("No pass recorded here yet")
    expect(page.get_by_test_id("score-measure-label-12")).to_have_text("m. 12")
    expect(page.get_by_test_id("score-target-badge")).to_have_text("Selected passage")
    _close_inspector(page)
    printed_measure = page.locator(
        'svg.pdf-overlay-svg rect[role="button"][aria-label^="Measure 12"]'
    )
    expect(printed_measure).to_be_visible()
    score_page = page.get_by_test_id("performer-score-page")
    canvas = score_page.locator("canvas")
    expect(canvas).to_have_css("user-select", "none")
    expect(canvas).to_have_attribute("draggable", "false")

    # Exercise the human failure mode, not Playwright's ideal zero-motion
    # click: a small hand movement while pressing a highlighted measure used
    # to select the replaced canvas element and cover the PDF in native blue.
    box = printed_measure.bounding_box()
    assert box is not None
    x = box["x"] + box["width"] * 0.5
    y = box["y"] + box["height"] * 0.5
    page.evaluate("() => window.getSelection()?.removeAllRanges()")
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 5, y + 2, steps=3)
    page.mouse.up()

    selection = page.evaluate(
        """() => {
          const value = window.getSelection();
          return {
            rangeCount: value?.rangeCount ?? 0,
            collapsed: value?.isCollapsed ?? true,
            text: value?.toString() ?? '',
            anchor: value?.anchorNode?.nodeName ?? null,
            focus: value?.focusNode?.nodeName ?? null,
          };
        }"""
    )
    assert selection["collapsed"] is True
    assert selection["text"] == ""
    expect(canvas).to_have_attribute("data-rendered-page", "1")

    # Also recover if Chrome already created a native range before the current
    # pointer event (the visible symptom is a solid blue, apparently blank PDF
    # page). Selecting a measure must clear that stale range.
    page.evaluate(
        """() => {
          const canvas = document.querySelector('[data-testid="performer-score-page"] canvas');
          if (!canvas) throw new Error('score canvas missing');
          const range = document.createRange();
          range.selectNode(canvas);
          const selection = window.getSelection();
          selection?.removeAllRanges();
          selection?.addRange(range);
        }"""
    )
    assert page.evaluate("() => window.getSelection()?.isCollapsed") is False

    # A drag intentionally does not activate a target; the ordinary click
    # still must select the measure after native selection is suppressed.
    printed_measure.click()
    assert page.evaluate("() => window.getSelection()?.isCollapsed") is True

    _open_inspector(page)

    expect(target).to_contain_text("m. 12")
    expect(target).to_contain_text("This score location stays attached to the recording")
    expect(target).to_contain_text("No pass recorded here yet")
    recording_plan = page.get_by_test_id("recording-plan")
    expect(recording_plan).to_contain_text("Orchestra leads m. 12.")
    expect(recording_plan).to_contain_text(
        "Follows you from m. 12, beat 4 · 6 prior takes · ♩ = 64 learned."
    )
    target.get_by_role("button", name="Record pass").click()

    expect(page.get_by_text(re.compile(r"entry at measure 12"))).to_be_visible()
    assert requests
    assert requests[-1]["target_score_beat"] == 44.0
    assert "tempo_bpm" not in requests[-1]
    assert "orchestra_only" not in requests[-1]
    assert "output_advance_ms" in requests[-1]
    assert "cue_seconds" not in requests[-1]
    page.get_by_role("button", name="Cancel").click()


def test_recording_plan_follows_immediately_when_selection_is_inside_follow(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    _close_inspector(page)

    printed_measure = page.locator(
        'svg.pdf-overlay-svg rect[role="button"][aria-label^="Measure 17"]'
    )
    expect(printed_measure).to_be_visible()
    printed_measure.click()

    _open_inspector(page)

    recording_plan = page.get_by_test_id("recording-plan")
    expect(recording_plan).to_contain_text("Orchestra cues from m. 17 until you join.")
    expect(recording_plan).to_contain_text(
        "Follows you from m. 17, beat 1 · 6 prior takes · ♩ = 64 learned."
    )


def test_take_events_socket_reconnects_after_a_dropped_connection(
    page: Page, live_server: str
) -> None:
    hardware_status_requests: list[str] = []
    page.on(
        "request",
        lambda request: hardware_status_requests.append(request.url)
        if request.url.endswith("/api/hardware/status")
        else None,
    )
    base = f"{live_server}/app/"
    page.goto(base, wait_until="networkidle")

    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )
    requests_before_reconnect = len(hardware_status_requests)
    assert requests_before_reconnect >= 1

    page.evaluate("() => window.__rubatoTakeEvents.forceDisconnect()")
    page.wait_for_function("() => window.__rubatoTakeEvents.status === 'closed'")
    page.wait_for_function(
        "() => window.__rubatoTakeEvents.status === 'open' "
        "&& window.__rubatoTakeEvents.reconnectCount >= 1",
        timeout=10000,
    )
    _wait_for(
        lambda: len(hardware_status_requests) > requests_before_reconnect,
        timeout=5,
    )


def test_running_hardware_state_is_driven_by_events_without_status_polling(
    page: Page, live_server: str
) -> None:
    hardware_status_requests: list[str] = []
    page.on(
        "request",
        lambda request: hardware_status_requests.append(request.url)
        if request.url.endswith("/api/hardware/status")
        else None,
    )
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )

    # Let the coalesced mount/socket-open snapshot settle, then enter a
    # running lifecycle phase. The old implementation issued one status GET
    # per second for as long as this state lasted; lifecycle events now own
    # the transition, so no additional GET should appear.
    time.sleep(0.2)
    _record_selected_passage(page, with_orchestra_cue=False)
    expect(page.get_by_role("button", name="Stop Take")).to_be_visible()
    requests_while_running = len(hardware_status_requests)
    time.sleep(1.25)
    assert len(hardware_status_requests) == requests_while_running

    page.get_by_role("button", name="Stop Take").click()


def test_lifecycle_events_update_hardware_and_refresh_materialized_coverage(
    page: Page, live_server: str
) -> None:
    coverage_requests: list[str] = []
    page.on(
        "request",
        lambda request: coverage_requests.append(request.url)
        if "/api/coverage/2" in request.url
        else None,
    )
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    _open_inspector(page)
    page.wait_for_function(
        "() => window.__rubatoTakeEvents && window.__rubatoTakeEvents.status === 'open'"
    )
    initial_coverage_requests = len(coverage_requests)
    assert initial_coverage_requests >= 1

    shared_events.publish(
        HardwareStatusEvent(
            type="hardware:status",
            status=LiveStatusResponse(
                phase=HardwareJobPhase.RUNNING,
                kind="playback",
                running=True,
                message="Playing lifecycle event",
            ),
        )
    )
    expect(page.locator(".state-word")).to_have_text("Playing")
    expect(page.locator(".state-detail")).to_have_text("Playing lifecycle event")

    shared_events.publish(
        HardwareStatusEvent(
            type="hardware:status",
            status=LiveStatusResponse(
                phase=HardwareJobPhase.COMPLETED,
                kind="idle",
                running=False,
                message="Playback completed",
            ),
        )
    )
    expect(page.locator(".state-word")).to_have_text("Orchestra Ready")
    expect(page.locator(".state-detail")).to_have_text(
        "Yamaha MIDI output is ready · orchestra 75%"
    )

    with page.expect_request(lambda request: "/api/coverage/2" in request.url):
        shared_events.publish(
            CoverageMaterialized(
                type="coverage:materialized",
                piece_id="chopin_op11",
                movement=2,
                revision=1,
            )
        )
    assert len(coverage_requests) > initial_coverage_requests
