from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from aimusic.accompaniment import runtime_io
from aimusic.accompaniment.following import FollowerUpdate, OracleFollower, PerformedNote
from aimusic.accompaniment.live_engine import LiveEngine
from aimusic.accompaniment.runtime_contracts import (
    LiveStateWord,
    RunPhase,
    RuntimeConfig,
    RuntimeTrace,
    require_run_phase_transition,
)
from aimusic.accompaniment.runtime_io import (
    CapturingOutput,
    JsonlTraceSink,
    ManualClock,
    MemoryTraceSink,
    RecordedNoteReplay,
)
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode, Section, SectionMap
from aimusic.accompaniment.tempo_model import OnlineTempoModel

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")


def make_engine(clock: ManualClock, output: CapturingOutput, trace) -> LiveEngine:
    bundle = ScoreBundle.load(FIXTURE)
    return LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="run-test",
            minimum_follower_confidence=0.5,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=50,
            output_advance_ms=10,
        ),
        clock=clock,
        follower=OracleFollower(),
        tempo_model=OnlineTempoModel(initial_tempo_bpm=120, smoothing_alpha=1),
        output=output,
        trace_sink=trace,
    )


def _identity_reference_bundle(*, sections: tuple[Section, ...] | None = None) -> ScoreBundle:
    source = ScoreBundle.load(FIXTURE)
    return replace(
        source,
        events=tuple(
            replace(
                event,
                source_refs={
                    **event.source_refs,
                    "source_performance_beat": event.beat,
                    "source_performance_duration_beats": event.duration_beats,
                },
            )
            for event in source.events
        ),
        section_map=(
            source.section_map
            if sections is None
            else SectionMap(piece_id=source.metadata.piece_id, sections=sections)
        ),
    )


def _engine_for_bundle(
    bundle: ScoreBundle,
    *,
    clock: ManualClock,
    output: CapturingOutput,
    trace: MemoryTraceSink,
) -> LiveEngine:
    return LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="measure-start",
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )


def test_nonzero_follow_start_seeds_score_reference_and_future_downbeat() -> None:
    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = _engine_for_bundle(bundle, clock=clock, output=output, trace=trace)

    status = engine.start(start_beat=3.0, entry_perf_time=11.0)

    assert status.phase is RunPhase.ACTIVE
    assert status.state_word is LiveStateWord.FOLLOWING
    assert status.score_beat == 3.0
    assert status.confidence == 0.0
    start = next(row for row in trace.records if row.type == "runtime_start")
    assert start.score_beat == 3.0
    assert start.reference_beat == 3.0
    assert start.entry_perf_time == 11.0
    assert start.section_mode is AccompanimentMode.FOLLOW
    warm_tempo = next(
        row
        for row in trace.records
        if row.type == "tempo" and row.observation_decision == "count_off_timing_seed"
    )
    assert warm_tempo.perf_time == 11.0
    assert warm_tempo.reference_beat == 3.0
    assert warm_tempo.confidence == 0.0

    clock.advance_to(10.91)
    engine.tick()
    event = next(item for _sent_at, item in output.sent if item.event.event_id == "accomp_001")
    assert event.perf_time == 11.0
    assert event.reference_beat == 3.0


def test_nonzero_follow_start_coasts_from_countoff_then_holds_without_piano() -> None:
    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="measure-start-missed-entry",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )

    engine.start(start_beat=3.0, entry_perf_time=11.0)
    clock.advance_to(10.5)
    assert engine.tick().score_beat == 3.0
    clock.advance_to(11.49)
    assert engine.tick().state_word is LiveStateWord.FOLLOWING
    clock.advance_to(11.51)
    held = engine.tick()

    assert held.state_word is LiveStateWord.WAITING
    assert held.confidence == 0.0
    assert output.panics[-1][1] == "section_hold"
    assert any(
        row.type == "policy" and row.transition_reason == "count_off_entry_grace_expired"
        for row in trace.records
    )


def test_orchestra_lead_in_keeps_playing_without_piano_evidence() -> None:
    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="orchestra-entry",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )

    started = engine.start(
        start_beat=3.0,
        entry_perf_time=10.0,
        orchestra_lead_in=True,
    )
    assert started.state_word is LiveStateWord.LEADING

    # This is deliberately well beyond the normal dropout coast. An explicit
    # orchestra-led entry owns the clock until piano evidence arrives.
    clock.advance_to(11.25)
    still_leading = engine.tick()

    assert still_leading.state_word is LiveStateWord.LEADING
    assert still_leading.score_beat == pytest.approx(5.5)
    assert not output.panics
    start = next(row for row in trace.records if row.type == "runtime_start")
    assert start.start_kind == "orchestra_lead_in"
    assert not any(
        row.type == "policy" and row.transition_reason == "count_off_entry_grace_expired"
        for row in trace.records
    )
    # The lead-in keeps the source-performance clock, uniformly scaled to the
    # requested global score tempo. Canonical position is only its projection.
    lead_tempo = next(
        row
        for row in trace.records
        if row.type == "tempo" and row.observation_decision == "orchestra_entry_timing_seed"
    )
    assert lead_tempo.tempo_bpm == pytest.approx(120.0)
    assert lead_tempo.reference_beat_period_seconds == pytest.approx(0.5)

    # One matched note localizes the pianist but spans zero beats: it must not
    # transfer timing authority. The orchestra begins acquiring pace and keeps
    # leading (frozen at the entry beat) rather than handing off on position.
    acquiring = engine.process_note(
        PerformedNote(
            perf_time=11.25,
            pitch=60,
            velocity=80,
            score_beat=5.5,
        )
    )
    assert acquiring.state_word is LiveStateWord.LEADING
    assert acquiring.score_beat == pytest.approx(5.5)
    assert not output.panics
    assert any(
        row.type == "follower" and row.position_action == "orchestra_entry_acquiring"
        for row in trace.records
    )
    assert not any(
        row.type == "policy" and row.transition_reason == "orchestra_entry_matched"
        for row in trace.records
    )


def test_orchestra_entry_keeps_post_pickup_attacks_while_piano_pace_is_acquired() -> None:
    """The opening LEAD boundary must not cancel attacks after the first pickup.

    Movement II's piano enters at beat 47, while two orchestral attacks follow
    around beat 47.68. Matchmaker needs several piano onsets to fit entry pace;
    ordinary LEAD used to pause exactly at beat 47 and cancel those attacks.
    """

    source = _identity_reference_bundle()
    start_marker = replace(
        source.solo_events[2],
        event_id="start_marker",
        beat=0.0,
        duration_beats=0.1,
        source_refs={
            **source.solo_events[2].source_refs,
            "source_performance_beat": 0.0,
        },
    )
    opening_solo = replace(
        source.solo_events[0],
        beat=1.0,
        duration_beats=0.25,
        source_refs={
            **source.solo_events[0].source_refs,
            "source_performance_beat": 1.0,
        },
    )
    next_solo = replace(
        source.solo_events[1],
        beat=2.0,
        duration_beats=0.25,
        source_refs={
            **source.solo_events[1].source_refs,
            "source_performance_beat": 2.0,
        },
    )
    post_pickup = replace(
        source.accompaniment_events[0],
        event_id="post_pickup_orchestra",
        beat=1.5,
        duration_beats=0.5,
        source_refs={
            **source.accompaniment_events[0].source_refs,
            "source_performance_beat": 1.5,
        },
    )
    bundle = replace(
        source,
        events=(start_marker, opening_solo, post_pickup, next_solo),
        section_map=SectionMap(
            piece_id=source.metadata.piece_id,
            sections=(
                Section(
                    id="opening",
                    start_beat=0.0,
                    end_beat=1.0,
                    mode=AccompanimentMode.LEAD,
                ),
                Section(
                    id="solo",
                    start_beat=1.0,
                    end_beat=8.0,
                    mode=AccompanimentMode.FOLLOW,
                ),
            ),
        ),
    )
    clock = ManualClock(0.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="post-pickup-entry",
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )

    engine.start(start_beat=0.0, entry_perf_time=0.0, orchestra_lead_in=True)
    clock.advance_to(0.5)
    engine.tick()
    acquiring = engine.process_note(
        PerformedNote(perf_time=0.5, pitch=60, velocity=80, score_beat=1.0)
    )
    assert acquiring.state_word is LiveStateWord.LEADING

    clock.advance_to(0.76)
    engine.tick()

    sent_ids = [item.event.event_id for _sent_at, item in output.sent]
    assert "post_pickup_orchestra" in sent_ids
    assert output.panics == []
    assert not any(
        row.type == "policy" and row.transition_reason == "lead_section_complete_waiting_for_solo"
        for row in trace.records
    )


def test_orchestra_entry_repositions_match_and_rejects_stale_cue_lock() -> None:
    class StaleFollower:
        repositioned: tuple[float, float | None] | None = None

        def reposition_for_entry(
            self,
            *,
            score_beat: float,
            reference_beat: float | None,
        ) -> None:
            self.repositioned = (score_beat, reference_beat)

        def observe(self, note: PerformedNote) -> FollowerUpdate:
            return FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=3.0,
                reference_beat=3.0,
                confidence=0.5,
                raw_state={
                    "follower": "matchmaker",
                    "stable_update_count": 2,
                },
            )

    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    follower = StaleFollower()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="orchestra-entry-stale-lock",
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=follower,
        output=output,
        trace_sink=trace,
    )

    engine.start(start_beat=3.0, entry_perf_time=10.0, orchestra_lead_in=True)
    clock.advance_to(12.0)
    engine.tick()
    status = engine.process_note(PerformedNote(perf_time=12.0, pitch=60, velocity=80))

    assert follower.repositioned == pytest.approx((7.0, 7.0))
    assert status.state_word is LiveStateWord.LEADING
    assert status.score_beat == pytest.approx(7.0)
    assert not output.panics
    mismatch = next(
        row
        for row in trace.records
        if row.type == "follower" and row.position_action == "orchestra_entry_position_mismatch"
    )
    assert mismatch.raw_score_beat == 3.0
    assert mismatch.score_beat == pytest.approx(7.0)
    assert not any(
        row.type == "scheduler" and row.reset_reason == "repeat" for row in trace.records
    )


def test_orchestra_entry_clamps_to_piano_pace_not_lead_in_seed() -> None:
    """Regression for live-1785118178612: the cue-in raced ahead at ~88 BPM.

    The orchestra led in at 120 BPM but the pianist enters at ~50 BPM. The old
    handoff seeded the FOLLOW clock from the lead-in pace and smoothed the
    pianist's slower pace into it. The clamp must instead adopt the pianist's
    pace, fit to piano onsets alone, and never step transport backward.
    """

    class SlowPacedFollower:
        def __init__(self, bpm: float) -> None:
            self._beats_per_second = bpm / 60.0
            self._entry_beat: float | None = None
            self._entry_time: float | None = None
            self.observations = 0

        def reposition_for_entry(
            self,
            *,
            score_beat: float,
            reference_beat: float | None,
        ) -> None:
            self._entry_beat = score_beat
            self._entry_time = None

        def observe(self, note: PerformedNote) -> FollowerUpdate:
            self.observations += 1
            if self._entry_beat is None:
                self._entry_beat = note.score_beat or 0.0
            if self._entry_time is None:
                self._entry_time = note.perf_time
            beat = self._entry_beat + (note.perf_time - self._entry_time) * self._beats_per_second
            return FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=beat,
                reference_beat=beat,
                confidence=0.5,
                raw_state={
                    "follower": "matchmaker",
                    "stable_update_count": self.observations,
                },
            )

    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    follower = SlowPacedFollower(bpm=50.0)
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="orchestra-entry-slow-piano",
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=follower,
        output=output,
        trace_sink=trace,
    )

    engine.start(start_beat=3.0, entry_perf_time=10.0, orchestra_lead_in=True)

    statuses = []
    # >=6 onsets over >=1.3 s so the acquisition window is satisfied.
    for step in range(8):
        perf_time = 11.0 + step * 0.2
        clock.advance_to(perf_time)
        engine.tick()
        statuses.append(
            engine.process_note(PerformedNote(perf_time=perf_time, pitch=60, velocity=80))
        )

    # Position was certified on the first note but authority is withheld while
    # too few onsets span too little to imply a pace.
    assert statuses[0].state_word is LiveStateWord.LEADING
    handed_off = statuses[-1]
    assert handed_off.state_word is LiveStateWord.FOLLOWING
    # The clamp adopts the pianist's ~50 BPM pace, not the 120 BPM lead-in seed.
    assert handed_off.tempo_bpm == pytest.approx(50.0, abs=3.0)
    assert handed_off.tempo_bpm < 90.0

    seed = next(
        row
        for row in trace.records
        if row.type == "tempo" and row.observation_decision == "orchestra_entry_piano_seed"
    )
    assert seed.tempo_bpm == pytest.approx(50.0, abs=3.0)

    # Transport never steps backward across acquisition and handoff.
    positions = [
        row.score_beat
        for row in trace.records
        if row.type == "follower" and row.score_beat is not None
    ]
    assert positions == sorted(positions)
    assert not output.panics
    assert not any(
        row.type == "scheduler" and row.reset_reason == "repeat" for row in trace.records
    )


def test_orchestra_lead_in_hands_off_when_follower_locks_after_the_entry() -> None:
    """Regression for live-1788038221669.

    The orchestra lead-in raced past the authored entry boundary on the source
    clock while the follower spent a few onsets locking. By the time the pianist
    had a confident position, it sat well behind the runaway orchestra, the
    entry proximity gate never certified, and the orchestra led autonomously for
    the entire take (policy stuck in LEAD, ``orchestra_entry_position_mismatch``
    on every follower row).

    The lead-in must hold at the entry boundary and certify the soloist's
    confident entry even when the follower locks a few beats late.
    """

    class LateLockSlowFollower:
        """Enters at the boundary, plays ~50 BPM, and only gains confidence
        after two warm-up onsets -- so the first confident position is a few
        beats past the boundary while the 120 BPM lead-in has raced ahead."""

        def __init__(self) -> None:
            self._entry_beat: float | None = None
            self._entry_time: float | None = None
            self.observations = 0

        def reposition_for_entry(
            self, *, score_beat: float, reference_beat: float | None
        ) -> None:
            self._entry_beat = score_beat
            self._entry_time = None

        def observe(self, note: PerformedNote) -> FollowerUpdate:
            self.observations += 1
            if self._entry_beat is None:
                self._entry_beat = note.score_beat or 0.0
            if self._entry_time is None:
                self._entry_time = note.perf_time
            beat = self._entry_beat + (note.perf_time - self._entry_time) * (50.0 / 60.0)
            return FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=beat,
                reference_beat=beat,
                confidence=0.0 if self.observations <= 2 else 0.5,
                raw_state={"follower": "matchmaker", "stable_update_count": self.observations},
            )

    bundle = _identity_reference_bundle(
        sections=(
            Section("opening", 0.0, 4.0, AccompanimentMode.LEAD),
            Section("solo", 4.0, 8.0, AccompanimentMode.FOLLOW),
        )
    )
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="orchestra-entry-late-lock",
            initial_tempo_bpm=120,
            minimum_follower_confidence=0.5,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=LateLockSlowFollower(),
        output=output,
        trace_sink=trace,
    )

    # The 120 BPM lead-in reaches the beat-4 entry boundary at t=12.0 s.
    engine.start(start_beat=0.0, entry_perf_time=10.0, orchestra_lead_in=True)

    statuses = []
    for step in range(10):
        perf_time = 12.0 + step * 0.3
        clock.advance_to(perf_time)
        engine.tick()
        statuses.append(
            engine.process_note(PerformedNote(perf_time=perf_time, pitch=60, velocity=80))
        )

    # The handoff certifies and reaches FOLLOW instead of leading autonomously.
    assert statuses[-1].state_word is LiveStateWord.FOLLOWING
    assert any(
        row.type == "policy" and row.section_mode is AccompanimentMode.FOLLOW
        for row in trace.records
    )
    # The regression signature -- never leaving LEAD on a permanent mismatch --
    # must be gone.
    assert not all(
        row.position_action == "orchestra_entry_position_mismatch"
        for row in trace.records
        if row.type == "follower" and row.confidence and row.confidence >= 0.5
    )
    # The pre-entry orchestra held at the boundary rather than racing past it.
    orchestra_beats = [
        row.score_beat
        for row in trace.records
        if row.type == "follower" and row.position_action == "orchestra_entry_position_mismatch"
    ]
    assert all(beat <= 4.5 for beat in orchestra_beats)
    assert not output.panics


def test_uncertain_measure_entry_remains_cue_led_until_grace_expires() -> None:
    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    trace = MemoryTraceSink()
    engine = _engine_for_bundle(
        bundle,
        clock=clock,
        output=CapturingOutput(),
        trace=trace,
    )
    engine.follower = ConfidenceFollower()

    engine.start(start_beat=3.0, entry_perf_time=11.0)
    clock.advance_to(11.1)
    engine.process_note(PerformedNote(perf_time=11.1, pitch=60, velocity=80, score_beat=3.1))

    follower_row = next(row for row in trace.records if row.type == "follower")
    policy_row = [
        row
        for row in trace.records
        if row.type == "policy" and row.transition_reason == "count_off_entry_not_yet_locked"
    ][-1]
    tempo_row = [
        row
        for row in trace.records
        if row.type == "tempo" and row.monotonic_time == pytest.approx(11.1)
    ][-1]
    assert follower_row.position_action == "count_off_entry_not_yet_locked"
    assert policy_row.section_mode is AccompanimentMode.FOLLOW
    assert tempo_row.observation_decision == "count_off_timing_seed"


def test_nonzero_lead_start_waits_for_countoff_entry_and_keeps_lead_authority() -> None:
    bundle = _identity_reference_bundle(
        sections=(
            Section(
                id="opening-follow",
                start_beat=0.0,
                end_beat=2.0,
                mode=AccompanimentMode.FOLLOW,
            ),
            Section(
                id="mid-lead",
                start_beat=2.0,
                end_beat=6.0,
                mode=AccompanimentMode.LEAD,
                tempo_bpm=120.0,
            ),
            Section(
                id="closing-follow",
                start_beat=6.0,
                end_beat=8.0,
                mode=AccompanimentMode.FOLLOW,
            ),
        )
    )
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = _engine_for_bundle(bundle, clock=clock, output=output, trace=trace)

    status = engine.start(start_beat=3.0, entry_perf_time=11.0)

    assert status.state_word is LiveStateWord.LEADING
    assert status.score_beat == 3.0
    clock.advance_to(10.5)
    assert engine.tick().score_beat == 3.0
    clock.advance_to(10.91)
    engine.tick()
    event = next(item for _sent_at, item in output.sent if item.event.event_id == "accomp_001")
    assert event.perf_time == 11.0
    clock.advance_to(11.5)
    assert engine.tick().score_beat > 3.0
    start = next(row for row in trace.records if row.type == "runtime_start")
    assert start.section_mode is AccompanimentMode.LEAD


def test_recorded_replay_uses_same_causal_engine_and_dispatches_once() -> None:
    # File timestamps need not share the process clock's epoch.
    clock = ManualClock(3.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = make_engine(clock, output, trace)
    engine.start()
    replay = RecordedNoteReplay(
        [
            PerformedNote(perf_time=10.0, pitch=60, velocity=64, score_beat=0.0),
            PerformedNote(perf_time=10.5, pitch=62, velocity=68, score_beat=1.0),
            PerformedNote(perf_time=11.0, pitch=64, velocity=72, score_beat=2.0),
        ]
    )

    replay.run(engine.process_note, clock=clock)
    clock.advance_to(4.49)
    engine.tick()

    assert engine.status.phase is RunPhase.ACTIVE
    assert engine.status.state_word is LiveStateWord.FOLLOWING
    assert [item[1].event.event_id for item in output.sent] == ["accomp_000", "accomp_001"]
    assert output.sent[-1][0] == pytest.approx(4.5)
    assert {record.type for record in trace.records} >= {
        "input",
        "follower",
        "tempo",
        "policy",
        "scheduler",
        "state",
    }

    stopped = engine.stop()
    assert stopped.phase is RunPhase.COMPLETED
    assert stopped.state_word is LiveStateWord.SILENT
    assert output.panics[-1][1] == "user_stop"


def test_current_score_beat_advances_between_follow_note_arrivals() -> None:
    clock = ManualClock(1.0)
    engine = make_engine(clock, CapturingOutput(), MemoryTraceSink())
    engine.start()
    engine.process_note(
        PerformedNote(perf_time=1.0, pitch=60, velocity=64, score_beat=0.0)
    )
    clock.advance_to(1.5)
    engine.process_note(
        PerformedNote(perf_time=1.5, pitch=62, velocity=64, score_beat=1.0)
    )

    clock.advance_to(1.6)

    assert engine.status.score_beat == 1.0
    assert engine.current_score_beat() == pytest.approx(1.2)


def test_jsonl_trace_sink_writes_valid_typed_rows(tmp_path: Path) -> None:
    clock = ManualClock(1.0)
    output = CapturingOutput()
    path = tmp_path / "runs" / "r1" / "trace" / "runtime.jsonl"
    trace_sink = JsonlTraceSink(path)
    engine = make_engine(clock, output, trace_sink)

    engine.start()
    engine.process_note(PerformedNote(perf_time=1.0, pitch=60, velocity=64, score_beat=0.0))
    engine.stop()
    trace_sink.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["type"] == "state"
    assert rows[0]["status"]["phase"] == "listening"
    assert any(row["type"] == "scheduler" for row in rows)
    assert rows[-1]["status"]["phase"] == "completed"


def test_jsonl_trace_sink_serializes_off_thread_and_drains_cleanly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "runtime.jsonl"
    serializer_threads: list[str] = []
    original_dumps = runtime_io.json.dumps

    def observed_dumps(*args, **kwargs):
        serializer_threads.append(threading.current_thread().name)
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(runtime_io.json, "dumps", observed_dumps)
    sink = JsonlTraceSink(path, max_queue_size=512)
    for index in range(500):
        sink.write(RuntimeConfig(run_id=f"trace-{index}"))
    sink.close()

    assert sink.dropped_rows == 0
    assert len(path.read_text(encoding="utf-8").splitlines()) == 500
    assert set(serializer_threads) == {"rubato-trace-writer"}


def test_run_lifecycle_rejects_invalid_transition() -> None:
    with pytest.raises(ValueError, match="Invalid run phase transition"):
        require_run_phase_transition(RunPhase.PREPARING, RunPhase.ACTIVE)


def test_runtime_trace_requires_wire_discriminator() -> None:
    adapter = TypeAdapter(RuntimeTrace)

    with pytest.raises(ValidationError, match="union_tag_not_found"):
        adapter.validate_python({"monotonic_time": 1.0, "perf_time": 1.0, "pitch": 60})


def test_runtime_config_rejects_dispatch_horizon_larger_than_plan() -> None:
    with pytest.raises(ValidationError, match="dispatch horizon cannot exceed"):
        RuntimeConfig(run_id="bad", planning_horizon_ms=20, dispatch_horizon_ms=50)


def test_failure_reason_and_panic_are_durable_in_trace() -> None:
    clock = ManualClock(1.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = make_engine(clock, output, trace)
    engine.start()

    failed = engine.fail("MIDI output disconnected")

    assert failed.phase is RunPhase.FAILED
    assert failed.message == "MIDI output disconnected"
    assert any(
        record.type == "scheduler" and record.panic_reason == "MIDI output disconnected"
        for record in trace.records
    )
    assert trace.records[-1].status.message == "MIDI output disconnected"


class ConfidenceFollower:
    def __init__(self) -> None:
        self.confidences = iter((0.2, 0.8))

    def observe(self, note: PerformedNote) -> FollowerUpdate:
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat or 4.0,
            confidence=next(self.confidences),
            raw_state={"fixture": "handoff"},
        )


def test_orchestra_led_opening_starts_without_piano_then_waits_for_follower() -> None:
    source = ScoreBundle.load(FIXTURE)
    events = tuple(
        sorted(
            (
                replace(source.accompaniment_events[0], beat=0.0),
                replace(source.accompaniment_events[1], beat=3.0),
                replace(source.solo_events[0], beat=4.0),
                replace(source.accompaniment_events[2], beat=5.0),
            ),
            key=lambda event: (event.beat, event.event_id),
        )
    )
    bundle = replace(
        source,
        events=events,
        section_map=SectionMap(
            piece_id="orchestra-led-fixture",
            sections=(
                Section("opening", 0.0, 4.0, AccompanimentMode.LEAD),
                Section("solo", 4.0, 8.0, AccompanimentMode.FOLLOW),
            ),
        ),
    )
    bundle.validate()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="orchestra-led",
            initial_tempo_bpm=120,
            minimum_follower_confidence=0.5,
            planning_horizon_ms=1500,
        ),
        clock=clock,
        follower=ConfidenceFollower(),
        output=output,
        trace_sink=trace,
    )

    started = engine.start()

    assert started.phase is RunPhase.ACTIVE
    assert started.state_word is LiveStateWord.LEADING
    assert started.score_beat == 0.0
    assert [item.event.event_id for _, item in output.sent] == ["accomp_000"]

    for tick_time in (10.5, 11.0, 11.5):
        clock.advance_to(tick_time)
        engine.tick()
    clock.advance_to(12.0)
    waiting = engine.tick()

    assert waiting.state_word is LiveStateWord.WAITING
    assert waiting.score_beat == 4.0
    assert [item.event.event_id for _, item in output.sent] == ["accomp_000", "accomp_001"]
    assert output.panics == []

    low_confidence = engine.process_note(
        PerformedNote(perf_time=12.1, pitch=60, velocity=70, score_beat=4.0)
    )
    assert low_confidence.state_word is LiveStateWord.WAITING

    following = engine.process_note(
        PerformedNote(perf_time=12.2, pitch=60, velocity=70, score_beat=4.0)
    )
    assert following.state_word is LiveStateWord.FOLLOWING
    assert any(
        record.type == "policy" and record.section_mode is AccompanimentMode.HOLD
        for record in trace.records
    )


def test_follow_hands_to_later_lead_interlude_without_more_piano_input() -> None:
    source = ScoreBundle.load(FIXTURE)
    events = tuple(
        sorted(
            (
                replace(source.solo_events[0], beat=0.0),
                replace(source.accompaniment_events[0], beat=1.0),
                replace(source.solo_events[1], beat=4.0),
                replace(source.accompaniment_events[1], beat=5.0),
                replace(source.accompaniment_events[2], beat=7.0),
            ),
            key=lambda event: (event.beat, event.event_id),
        )
    )
    bundle = replace(
        source,
        events=events,
        section_map=SectionMap(
            piece_id="later-lead-fixture",
            sections=(
                Section("solo-before", 0.0, 4.0, AccompanimentMode.FOLLOW),
                Section("interlude", 4.0, 8.0, AccompanimentMode.LEAD),
                Section("solo-after", 8.0, 12.0, AccompanimentMode.FOLLOW),
            ),
        ),
    )
    bundle.validate()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="later-lead",
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )

    assert engine.start().state_word is LiveStateWord.LISTENING
    engine.process_note(PerformedNote(perf_time=10.0, pitch=60, velocity=70, score_beat=0.0))
    clock.advance_to(12.0)
    leading = engine.process_note(
        PerformedNote(perf_time=12.0, pitch=62, velocity=70, score_beat=4.0)
    )

    assert leading.state_word is LiveStateWord.LEADING
    for tick_time in (12.5, 13.0, 13.5, 14.0):
        clock.advance_to(tick_time)
        engine.tick()

    assert engine.status.state_word is LiveStateWord.WAITING
    assert [item.event.event_id for _, item in output.sent][-2:] == [
        "accomp_001",
        "accomp_002",
    ]
    assert any(
        record.type == "policy"
        and record.section_id == "interlude"
        and record.transition_reason == "symbolic_position_entered_lead_section"
        for record in trace.records
    )
    dispatch_rows = [
        record
        for record in trace.records
        if record.type == "scheduler" and record.dispatched_events
    ]
    assert dispatch_rows[-1].dispatched_events[0].lateness_ms == pytest.approx(0.0)


def _orchestra_led_bundle() -> ScoreBundle:
    source = ScoreBundle.load(FIXTURE)
    events = tuple(
        sorted(
            (
                replace(source.accompaniment_events[0], beat=0.0),
                replace(source.accompaniment_events[1], beat=3.0),
                replace(source.solo_events[0], beat=4.0),
                replace(source.accompaniment_events[2], beat=5.0),
            ),
            key=lambda event: (event.beat, event.event_id),
        )
    )
    bundle = replace(
        source,
        events=events,
        section_map=SectionMap(
            piece_id="orchestra-led-fixture",
            sections=(
                Section("opening", 0.0, 4.0, AccompanimentMode.LEAD),
                Section("solo", 4.0, 8.0, AccompanimentMode.FOLLOW),
            ),
        ),
    )
    bundle.validate()
    return bundle


def _reference_clock_interlude_bundle() -> ScoreBundle:
    source = ScoreBundle.load(FIXTURE)
    events = (
        replace(
            source.solo_events[0],
            beat=0.0,
            source_refs={"source_performance_beat": 0.0},
        ),
        replace(
            source.accompaniment_events[0],
            beat=1.0,
            source_refs={"source_performance_beat": 1.0},
        ),
        replace(
            source.solo_events[1],
            beat=4.0,
            source_refs={"source_performance_beat": 10.0},
        ),
        replace(
            source.accompaniment_events[1],
            beat=5.0,
            source_refs={
                "source_performance_beat": 12.5,
                "source_performance_duration_beats": 2.5,
            },
        ),
        replace(
            source.accompaniment_events[2],
            beat=7.0,
            source_refs={"source_performance_beat": 17.5},
        ),
        replace(
            source.solo_events[2],
            beat=8.0,
            source_refs={"source_performance_beat": 20.0},
        ),
    )
    bundle = replace(
        source,
        events=tuple(sorted(events, key=lambda event: (event.beat, event.event_id))),
        section_map=SectionMap(
            piece_id="reference-clock-interlude",
            sections=(
                Section("solo-before", 0.0, 4.0, AccompanimentMode.FOLLOW),
                Section("interlude", 4.0, 8.0, AccompanimentMode.LEAD),
                Section("solo-after", 8.0, 12.0, AccompanimentMode.FOLLOW),
            ),
        ),
    )
    bundle.validate()
    return bundle


def test_lead_interlude_finishes_before_buffered_early_piano_reentry() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=_reference_clock_interlude_bundle(),
        config=RuntimeConfig(
            run_id="canonical-tempo-interlude",
            initial_tempo_bpm=90,
            planning_horizon_ms=2000,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )

    assert engine.start().state_word is LiveStateWord.LISTENING
    engine.process_note(PerformedNote(perf_time=10.0, pitch=60, velocity=70, score_beat=0.0))
    # FOLLOW lookahead must not pre-commit the event at score beat 5 using the
    # pianist's preceding timing state.
    assert all(
        item.score_beat < 4.0
        for record in trace.records
        if record.type == "scheduler"
        for item in record.planned_events
    )

    clock.advance_to(12.0)
    leading = engine.process_note(
        PerformedNote(perf_time=12.0, pitch=62, velocity=70, score_beat=4.0)
    )
    assert leading.state_word is LiveStateWord.LEADING
    assert leading.tempo_bpm == pytest.approx(90.0)

    # A plausible early estimate in the following solo is retained for the
    # handoff, but it cannot cut off the autonomous interlude.
    clock.advance_to(12.2)
    still_leading = engine.process_note(
        PerformedNote(perf_time=12.2, pitch=64, velocity=70, score_beat=8.2)
    )
    assert still_leading.state_word is LiveStateWord.LEADING
    assert any(
        record.type == "follower"
        and record.position_action == "buffered_for_lead_handoff"
        and record.raw_score_beat == pytest.approx(8.2)
        for record in trace.records
    )

    for tick_time in (12.67, 13.34, 14.0, 14.67):
        clock.advance_to(tick_time)
        engine.tick()

    assert engine.status.state_word is LiveStateWord.FOLLOWING
    interlude_ids = {
        event.event_id
        for event in _reference_clock_interlude_bundle().accompaniment_events
        if 4.0 <= event.beat < 8.0
    }
    assert {
        item.event.event_id for _, item in output.sent if item.event.beat >= 4.0
    } == interlude_ids
    assert output.panics == []
    assert any(
        record.type == "policy" and record.transition_reason == "buffered_early_solo_reentry"
        for record in trace.records
    )


def test_live_tempo_change_reanchors_current_lead_without_position_jump() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=_orchestra_led_bundle(),
        config=RuntimeConfig(run_id="tempo-change", initial_tempo_bpm=120),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start()
    clock.advance_to(10.5)
    assert engine.tick().score_beat == pytest.approx(1.0)

    changed = engine.set_lead_tempo(60)
    assert changed.score_beat == pytest.approx(1.0)
    clock.advance_to(11.5)
    assert engine.tick().score_beat == pytest.approx(2.0)
    assert any(
        record.type == "policy" and record.transition_reason == "performer_tempo_changed"
        for record in trace.records
    )
    assert changed.orchestra_tempo_bpm == 60
    assert any(
        record.type == "control"
        and record.control == "orchestra_tempo_bpm"
        and record.applied_value == 60
        for record in trace.records
    )


def test_live_tempo_change_calibrates_only_the_remaining_nonlinear_span() -> None:
    bundle = _reference_clock_interlude_bundle()
    reference_by_score_beat = {
        4.0: 10.0,
        5.0: 11.0,
        7.0: 17.0,
        8.0: 20.0,
    }
    events = tuple(
        replace(
            event,
            source_refs={
                **event.source_refs,
                "source_performance_beat": reference_by_score_beat.get(
                    event.beat,
                    event.source_refs.get("source_performance_beat"),
                ),
            },
        )
        for event in bundle.events
    )
    bundle = replace(bundle, events=events)
    bundle.validate()
    clock = ManualClock(10.0)
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(run_id="remaining-tempo-change", initial_tempo_bpm=90),
        clock=clock,
        follower=OracleFollower(),
        output=CapturingOutput(),
    )
    engine.start()
    engine.process_note(PerformedNote(perf_time=10.0, pitch=60, velocity=70, score_beat=0.0))
    clock.advance_to(12.0)
    engine.process_note(PerformedNote(perf_time=12.0, pitch=62, velocity=70, score_beat=4.0))
    # The first canonical quarter spans only one reference unit. Under the
    # section's initial 90 BPM average it completes in 4/10 * 4/90 minutes.
    clock.advance_to(12.0 + (4.0 * 60.0 / 90.0) / 10.0)
    assert engine.tick().score_beat == pytest.approx(5.0)

    changed = engine.set_lead_tempo(60)
    assert changed.score_beat == pytest.approx(5.0)
    clock.advance_to(clock.now() + 3.0)

    # The remaining three score quarters last exactly three seconds at ♩=60,
    # even though they span nine reference units. Averaging over the already
    # elapsed full section would incorrectly require another 3.6 seconds.
    assert engine.tick().state_word is LiveStateWord.WAITING
    assert engine.status.score_beat == pytest.approx(8.0)


def test_live_tempo_change_reanchors_orchestra_led_entry() -> None:
    clock = ManualClock(10.0)
    engine = LiveEngine.from_bundle(
        bundle=_identity_reference_bundle(),
        config=RuntimeConfig(run_id="entry-tempo-change", initial_tempo_bpm=120),
        clock=clock,
        follower=OracleFollower(),
        output=CapturingOutput(),
    )
    engine.start(start_beat=3.0, entry_perf_time=10.0, orchestra_lead_in=True)
    clock.advance_to(10.5)
    assert engine.tick().score_beat == pytest.approx(4.0)

    changed = engine.set_lead_tempo(60)
    assert changed.score_beat == pytest.approx(4.0)
    clock.advance_to(11.5)
    assert engine.tick().score_beat == pytest.approx(5.0)


def test_final_follower_overshoot_completes_instead_of_missing_section() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    engine = LiveEngine.from_bundle(
        bundle=_identity_reference_bundle(),
        config=RuntimeConfig(run_id="final-overshoot", initial_tempo_bpm=120),
        clock=clock,
        follower=OracleFollower(),
        output=output,
    )
    engine.start(start_beat=7.0, entry_perf_time=10.0)

    finished = engine.process_note(
        PerformedNote(perf_time=10.0, pitch=60, velocity=70, score_beat=8.0001)
    )

    assert finished.phase is RunPhase.COMPLETED
    assert finished.score_beat == pytest.approx(8.0)
    assert finished.message == "Reached the end of the score"
    assert output.panics[-1][1] == "score_end"


def test_live_volume_change_reaches_output_and_trace() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=_orchestra_led_bundle(),
        config=RuntimeConfig(run_id="volume-change", orchestra_volume=0.5),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start()

    changed = engine.set_orchestra_volume(0.2)

    assert changed.orchestra_volume == 0.2
    assert output.master_volumes == [(10.0, 0.2)]
    assert any(
        record.type == "control"
        and record.control == "orchestra_volume"
        and record.applied_value == 0.2
        for record in trace.records
    )


def test_live_output_advance_change_reaches_output_and_trace() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=_orchestra_led_bundle(),
        config=RuntimeConfig(
            run_id="advance-change",
            dispatch_horizon_ms=100.0,
            output_advance_ms=0.0,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start()

    changed = engine.set_output_advance(40.0)

    assert changed.orchestra_output_advance_ms == 40.0
    assert output.output_advance_changes == [40.0]
    assert any(
        record.type == "control"
        and record.control == "orchestra_output_advance_ms"
        and record.applied_value == 40.0
        for record in trace.records
    )


def test_live_output_advance_cannot_exceed_dispatch_horizon() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    engine = LiveEngine.from_bundle(
        bundle=_orchestra_led_bundle(),
        config=RuntimeConfig(run_id="advance-bound", dispatch_horizon_ms=100.0),
        clock=clock,
        follower=OracleFollower(),
        output=output,
    )
    engine.start()

    with pytest.raises(ValueError, match="dispatch horizon"):
        engine.set_output_advance(150.0)


def test_stale_position_inside_completed_lead_cannot_force_follow_or_jump_panic() -> None:
    clock = ManualClock(10.0)
    output = CapturingOutput()
    engine = LiveEngine.from_bundle(
        bundle=_orchestra_led_bundle(),
        config=RuntimeConfig(run_id="stale-reentry", initial_tempo_bpm=120),
        clock=clock,
        follower=OracleFollower(),
        output=output,
    )
    engine.start()
    clock.advance_to(12.0)
    assert engine.tick().state_word is LiveStateWord.WAITING
    assert output.panics == []

    stale = engine.process_note(
        PerformedNote(perf_time=12.0, pitch=60, velocity=70, score_beat=3.5)
    )
    assert stale.state_word is LiveStateWord.WAITING
    assert output.panics == []

    clock.advance_to(12.2)
    following = engine.process_note(
        PerformedNote(perf_time=12.2, pitch=62, velocity=70, score_beat=4.0)
    )
    assert following.state_word is LiveStateWord.FOLLOWING


class ExpectedEntryFollower:
    def __init__(self) -> None:
        self.count = 0

    def observe(self, note: PerformedNote) -> FollowerUpdate:
        assert note.score_beat is not None
        self.count += 1
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat,
            confidence=0.0,
            raw_state={
                "follower": "matchmaker",
                "stable_update_count": self.count,
                "locked": False,
            },
        )


@pytest.mark.parametrize("entry_offset", [-0.3, 0.0, 0.3])
def test_expected_entry_window_relocks_by_second_symbolic_estimate(
    entry_offset: float,
) -> None:
    clock = ManualClock(10.0)
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=_orchestra_led_bundle(),
        config=RuntimeConfig(run_id=f"entry-{entry_offset}", initial_tempo_bpm=120),
        clock=clock,
        follower=ExpectedEntryFollower(),
        output=CapturingOutput(),
        trace_sink=trace,
    )
    engine.start()
    entry_time = 12.0 + entry_offset
    if entry_offset >= 0:
        clock.advance_to(12.0)
        assert engine.tick().state_word is LiveStateWord.WAITING
        clock.advance_to(entry_time)
    else:
        clock.advance_to(entry_time)

    engine.process_note(
        PerformedNote(
            perf_time=entry_time,
            pitch=60,
            velocity=70,
            score_beat=4.0,
        )
    )
    clock.advance_to(entry_time + 0.01)
    status = engine.process_note(
        PerformedNote(
            perf_time=entry_time + 0.01,
            pitch=62,
            velocity=70,
            score_beat=4.1,
        )
    )
    if entry_offset < 0:
        clock.advance_to(12.0)
        status = engine.tick()

    assert status.state_word is LiveStateWord.FOLLOWING
    assert status.score_beat == pytest.approx(4.1)
    accepted_tempo = [
        record
        for record in trace.records
        if record.type == "tempo" and record.observation_decision == "initial_anchor"
    ][-1]
    assert accepted_tempo.perf_time == pytest.approx(entry_time + 0.01)


class DropoutFollower:
    def __init__(self) -> None:
        self.confidences = iter((1.0, 0.1, 1.0))

    def observe(self, note: PerformedNote) -> FollowerUpdate:
        assert note.score_beat is not None
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat,
            confidence=next(self.confidences),
            raw_state={"fixture": "dropout"},
        )


def test_follow_coasts_briefly_then_holds_and_recovers() -> None:
    clock = ManualClock(0.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    bundle = ScoreBundle.load(FIXTURE)
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="dropout-coast",
            initial_tempo_bpm=120,
            follower_coast_ms=1500,
            planning_horizon_ms=1500,
        ),
        clock=clock,
        follower=DropoutFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start()
    engine.process_note(PerformedNote(perf_time=0.0, pitch=60, velocity=70, score_beat=0.0))

    clock.advance_to(0.5)
    coasting = engine.process_note(
        PerformedNote(perf_time=0.5, pitch=99, velocity=70, score_beat=7.0)
    )
    assert coasting.state_word is LiveStateWord.FOLLOWING
    assert coasting.score_beat == pytest.approx(1.0)
    assert coasting.confidence == pytest.approx(0.1)
    assert output.panics == []
    assert any(
        record.type == "policy" and record.transition_reason == "temporary_follower_dropout_coast"
        for record in trace.records
    )

    clock.advance_to(1.0)
    assert engine.tick().score_beat == pytest.approx(2.0)
    # The old fixed 1.5 s window would hold here; the budget is now tempo-relative
    # and BOTH the confident anchor AND the last onset must run dry. The
    # low-confidence onset at 0.5 s keeps it coasting past 1.6 s instead of cutting
    # the orchestra on a single decelerating beat.
    clock.advance_to(1.6)
    assert engine.tick().state_word is LiveStateWord.FOLLOWING

    # Step forward until genuine silence past the tempo budget holds. Small steps
    # keep the coasted position inside the piano's ACTIVE cells (beats 0-5) so we
    # exercise the dropout hold rather than the tacet handback that follows them.
    now_t = 1.6
    while engine.status.state_word is LiveStateWord.FOLLOWING and now_t < 3.0:
        now_t += 0.2
        clock.advance_to(now_t)
        engine.tick()
    assert engine.status.state_word is LiveStateWord.WAITING
    # A dropout hold is gentle: it must never fire an all-notes-off panic, which
    # is exactly what chopped the m.45 downbeat in the live traces.
    assert not any(reason == "section_hold" for _at, reason in output.panics)
    assert any(
        record.type == "policy" and record.transition_reason == "follower_dropout_coast_expired"
        for record in trace.records
    )

    clock.advance_to(now_t + 0.1)
    recovered = engine.process_note(
        PerformedNote(perf_time=now_t + 0.1, pitch=64, velocity=70, score_beat=3.4)
    )
    assert recovered.state_word is LiveStateWord.FOLLOWING


def test_follow_coasts_on_pure_silence_without_any_further_note() -> None:
    """Silence is evidence: FOLLOW must honour follower_coast_ms with no input.

    Adversarial review finding: tick() had no FOLLOW branch, so FOLLOW was left
    only when a *low-confidence note* arrived. If the pianist simply stopped
    playing, no note ever came and the coast/dropout contract never ran.
    """

    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="follow-silence",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start(start_beat=0.0, entry_perf_time=10.0)
    engine.process_note(PerformedNote(perf_time=10.0, pitch=60, velocity=80, score_beat=0.0))
    assert engine.status.state_word is LiveStateWord.FOLLOWING

    # The pianist stops. No further notes arrive at all -- only time passes.
    clock.advance_to(10.2)
    engine.tick()
    assert engine.status.state_word is LiveStateWord.FOLLOWING  # still inside the grace

    # Silence alone must still drive the coast/dropout path (the tick-loop
    # invariant this test guards). Step in small increments so the coasted
    # position stays inside the piano's ACTIVE cells (beats 0-5) and reaches a
    # dropout hold before the tacet handback at beat 6.
    now_t = 10.2
    while engine.status.state_word is LiveStateWord.FOLLOWING and now_t < 12.6:
        now_t += 0.3
        clock.advance_to(now_t)
        engine.tick()

    assert engine.status.state_word is LiveStateWord.WAITING
    assert any(
        row.type == "policy"
        and row.transition_reason
        in {"temporary_follower_dropout_coast", "follower_dropout_coast_expired"}
        for row in trace.records
    ), "silence never triggered the coast/dropout path"
    # The dropout hold is gentle now: it stops scheduling but never fires the
    # blunt all-notes-off. Sustained orchestra notes release on their own beat.
    assert not any(reason == "section_hold" for _at, reason in output.panics)


def test_expected_sustain_carries_orchestra_cue_before_dropout_hold() -> None:
    """Written sustain is not a dropout; the orchestra may supply the next cue."""

    source = _identity_reference_bundle()
    sustained_solo = replace(
        source.solo_events[0],
        event_id="sustained_solo",
        beat=0.0,
        duration_beats=2.75,
        source_refs={
            **source.solo_events[0].source_refs,
            "source_performance_beat": 0.0,
            "source_performance_duration_beats": 2.75,
        },
    )
    next_piano_attack = replace(
        source.solo_events[1],
        event_id="next_piano_attack",
        beat=3.0,
        duration_beats=0.25,
        source_refs={
            **source.solo_events[1].source_refs,
            "source_performance_beat": 3.0,
            "source_performance_duration_beats": 0.25,
        },
    )
    orchestra_cue = replace(
        source.accompaniment_events[0],
        event_id="orchestra_before_pickup",
        beat=1.5,
        duration_beats=0.25,
        source_refs={
            **source.accompaniment_events[0].source_refs,
            "source_performance_beat": 1.5,
            "source_performance_duration_beats": 0.25,
        },
    )
    bundle = replace(
        source,
        events=(sustained_solo, orchestra_cue, next_piano_attack),
    )
    clock = ManualClock(0.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="sustain-before-pickup",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start()
    engine.process_note(
        PerformedNote(perf_time=0.0, pitch=60, velocity=80, score_beat=0.0)
    )

    clock.advance_to(0.65)
    carrying = engine.tick()
    assert carrying.state_word is LiveStateWord.FOLLOWING
    assert output.panics == []

    clock.advance_to(0.76)
    engine.tick()
    assert "orchestra_before_pickup" in [
        item.event.event_id for _sent_at, item in output.sent
    ]

    # Once prediction reaches the score's next actual piano attack, continued
    # silence becomes anomalous and HOLD is again appropriate. The orchestra
    # first eases in (graceful slowdown), so the hold lands a little later than
    # the un-slowed projection would, and it is gentle -- no all-notes-off panic.
    clock.advance_to(2.2)
    held = engine.tick()
    assert held.state_word is LiveStateWord.WAITING
    assert not any(reason == "section_hold" for _at, reason in output.panics)


class _LockThenLowConfidenceFollower:
    """Locks on the first onsets, then rates every later onset below threshold.

    Reproduces the m.53-63 and m.105 chromatic runs, where the pianist keeps
    playing but the matcher's confidence sits just under its 0.5 lock the whole
    passage. The first two onsets are confident so the transport has both an
    anchor and a pace to coast on; every onset after is uncertain.
    """

    def __init__(self) -> None:
        self._confident_left = 2

    def observe(self, note: PerformedNote) -> FollowerUpdate:
        assert note.score_beat is not None
        confident = self._confident_left > 0
        self._confident_left = max(0, self._confident_left - 1)
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat,
            confidence=1.0 if confident else 0.4,
            raw_state={"fixture": "lock_then_low"},
        )


def test_arriving_low_confidence_notes_never_cut_the_orchestra() -> None:
    """m.53-63 / m.105 regression: unsure != stopped, so never panic while playing.

    The old coast timer keyed only on the last *confident* position, so a dense
    or chromatic passage that kept the matcher below its lock threshold expired
    into a HOLD -- an all-notes-off -- even though the pianist never stopped. The
    dropout is now gated on genuine input silence, so continuously arriving onsets
    keep the orchestra coasting and it is never cut.
    """

    bundle = _identity_reference_bundle()
    clock = ManualClock(0.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="low-confidence-run",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=_LockThenLowConfidenceFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start(start_beat=0.0, entry_perf_time=0.0)

    # A dense stream of onsets, each rated below the lock threshold after the
    # first, spanning far longer than the coast budget. Beats stay inside the
    # piano's ACTIVE cells so the only reason to hold would be a false dropout.
    t = 0.0
    beat = 0.0
    for _ in range(10):
        engine.process_note(
            PerformedNote(perf_time=t, pitch=60, velocity=70, score_beat=beat)
        )
        assert engine.status.state_word is not LiveStateWord.WAITING
        t += 0.25
        beat += 0.25
        clock.advance_to(t)
        engine.tick()
        assert engine.status.state_word is not LiveStateWord.WAITING

    # The uncertain-coast path was exercised, yet no dropout hold and no panic.
    assert any(
        row.type == "follower" and row.position_action == "coast_from_confident_anchor"
        for row in trace.records
    )
    assert not any(
        row.type == "policy" and row.transition_reason == "follower_dropout_coast_expired"
        for row in trace.records
    )
    assert not any(reason == "section_hold" for _at, reason in output.panics)


def test_ritardando_gap_shorter_than_a_slow_beat_is_not_a_dropout() -> None:
    """m.45 regression: a slow beat's worth of space must not cut the orchestra.

    Live into the m.45 climax the pianist decelerated toward ~30 BPM, so a ~2 s
    gap before the downbeat was less than a single beat -- yet the old fixed
    1.5 s coast declared a dropout and panicked the orchestra on the very
    downbeat it was expanding toward. With a tempo-relative budget the same gap
    keeps coasting and the downbeat is never cut.
    """

    bundle = _identity_reference_bundle()
    clock = ManualClock(0.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="ritardando",
            initial_tempo_bpm=40,
            follower_coast_ms=1500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start(start_beat=0.0, entry_perf_time=0.0)

    # Establish a slow ~40 BPM pace: consecutive beats spaced 1.5 s apart.
    for i in range(3):
        clock.advance_to(i * 1.5)
        engine.process_note(
            PerformedNote(perf_time=i * 1.5, pitch=60, velocity=70, score_beat=float(i))
        )
    assert engine.status.state_word is LiveStateWord.FOLLOWING

    # A gap longer than the OLD fixed 1.5 s coast but within the tempo-relative
    # budget (the LTE clock's slow beat period makes the budget ~1.9 s here) must
    # NOT hold or panic -- the whole point of judging silence in beats, not a
    # fixed wall-clock. now - anchor is ~1.75 s: past 1.5 s, inside the budget.
    clock.advance_to(4.6)  # last onset was at t=3.0
    engine.tick()
    assert engine.status.state_word is LiveStateWord.FOLLOWING
    assert not any(reason == "section_hold" for _at, reason in output.panics)
    assert not any(
        row.type == "policy" and row.transition_reason == "follower_dropout_coast_expired"
        for row in trace.records
    )

    # The pianist arrives on the downbeat; still following, never cut.
    clock.advance_to(4.8)
    resumed = engine.process_note(
        PerformedNote(perf_time=4.8, pitch=62, velocity=80, score_beat=4.0)
    )
    assert resumed.state_word is LiveStateWord.FOLLOWING


def test_coast_slowdown_never_steps_the_score_position_backward() -> None:
    """The graceful slowdown must engage continuously, never jump the cursor back.

    Adversarial-review finding: scaling the whole elapsed time by 1/(1+slowdown)
    the instant the slowdown turns on stepped the projected beat backward
    (~0.16 beat), breaking monotonicity and jittering the cursor. The slowdown is
    now piecewise -- full rate up to the engage instant, slower after -- so the
    coasted position is non-decreasing across the engage boundary.
    """

    bundle = _identity_reference_bundle()
    clock = ManualClock(0.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="slowdown-monotonic",
            initial_tempo_bpm=120,
            follower_coast_ms=1500,
            follower_coast_slowdown=0.2,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start(start_beat=0.0, entry_perf_time=0.0)
    engine.process_note(PerformedNote(perf_time=0.0, pitch=60, velocity=80, score_beat=0.0))

    # Sample the coasted position densely across the moment the slowdown engages
    # (about one beat after the last onset). It must never decrease.
    previous = -1.0
    t = 0.05
    while t < 1.4:
        clock.advance_to(t)
        beat = engine.tick().score_beat
        if beat is not None:
            assert beat >= previous - 1e-9, f"score_beat stepped backward at t={t}: {previous}->{beat}"
            previous = beat
        t += 0.01


def test_a_position_arriving_after_its_note_is_applied_on_the_next_tick() -> None:
    """An out-of-process follower answers late; tick must still consume it.

    Adversarial review finding: observe() hands the note off without waiting, so
    the answer lands in the response queue a moment later. Nothing drained it
    except the *next* note, which strands the last position before any silence.
    """

    class LateFollower:
        """Answers one note behind, like a real IPC round trip."""

        def __init__(self) -> None:
            self._pending: FollowerUpdate | None = None

        def observe(self, note: PerformedNote) -> FollowerUpdate | None:
            self._pending = FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=note.score_beat or 0.0,
                reference_beat=note.score_beat or 0.0,
                confidence=1.0,
            )
            return None  # this note's answer is not ready yet

        def poll_update(self) -> FollowerUpdate | None:
            update, self._pending = self._pending, None
            return update

    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="late-follower",
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=LateFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start(start_beat=3.0, entry_perf_time=10.0)

    # One note, then silence forever.
    engine.process_note(PerformedNote(perf_time=10.0, pitch=60, velocity=80, score_beat=4.0))
    assert engine.status.score_beat == pytest.approx(3.0), "position applied too early"

    clock.advance_to(10.05)
    engine.tick()

    assert engine.status.score_beat == pytest.approx(4.0), (
        "the follower's answer stayed trapped when no further note arrived"
    )


def test_lead_in_preserves_source_timing_and_ignores_rehearsal_pace_shape() -> None:
    """Without piano input, Oguri passes through under one global speed scalar."""

    from aimusic.accompaniment.predictive_follow import PaceProfile

    # A deliberately conflicting rehearsal profile must not reshape autonomous
    # source playback beat by beat.
    profile = PaceProfile(
        smoothed_period_at=lambda _b: 0.75,
        support_at=lambda beat: 3 if beat < 6.0 else 0,
    )
    source = _identity_reference_bundle()
    reference_by_score_beat = {
        0.0: 0.0,
        1.0: 1.0,
        2.0: 2.0,
        3.0: 3.0,
        4.0: 3.5,
        5.0: 4.0,
    }
    warped_events = []
    for event in source.events:
        # Explicit section-end knot for this deliberately tiny fixture.
        score_beat = 8.0 if event.event_id == "solo_005" else event.beat
        reference_beat = (
            5.0
            if event.event_id == "solo_005"
            else reference_by_score_beat[event.beat]
        )
        warped_events.append(
            replace(
                event,
                beat=score_beat,
                source_refs={
                    **event.source_refs,
                    "source_performance_beat": reference_beat,
                },
            )
        )
    bundle = replace(source, events=tuple(warped_events))
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="lead-in-shape",
            initial_tempo_bpm=120,
            planning_horizon_ms=3000,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
        pace_profile=profile,
    )
    engine.start(start_beat=3.0, entry_perf_time=10.0, orchestra_lead_in=True)

    clock.advance_to(10.625)
    source_clock = engine.tick()
    assert source_clock.score_beat == pytest.approx(4.0)
    assert source_clock.tempo_bpm == pytest.approx(96.0)
    assert source_clock.confidence == 0.0

    planned = {
        item.event_id: item
        for record in trace.records
        if record.type == "scheduler"
        for item in record.planned_events
    }
    assert planned["accomp_002"].target_perf_time == pytest.approx(11.25)
    assert planned["accomp_002"].reference_beat == pytest.approx(4.0)
    assert not output.panics


def test_stalled_cue_in_recovers_instead_of_freezing_the_orchestra() -> None:
    """A cue-in whose pace never fits must not strand the orchestra.

    Reproduces live take ``performance-20260731T024857Z-c79f``: the pianist
    entered a measure away from where the orchestra armed the entry, the
    follower stayed pinned near its warm-start prior, and every pace fit came
    out around 13.8 BPM -- below ``min_tempo_bpm``. ``estimate()`` therefore
    returned ``None`` forever and the runtime read that as "keep waiting", so
    77 piano onsets over 21 s produced a single dispatched orchestra note.
    """

    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="stalled-cue-in",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    engine.start(start_beat=3.0, entry_perf_time=10.0, orchestra_lead_in=True)

    # The follower crawls at ~0.2 canonical beats/s while the pianist really
    # plays at ~2 beats/s: every fit lands near 12 BPM and is rejected.
    entry_time, entry_beat, crawl = 11.25, 5.5, 0.2
    statuses = []
    for step in range(9):
        perf_time = entry_time + step * 0.85
        clock.advance_to(perf_time)
        statuses.append(
            engine.process_note(
                PerformedNote(
                    perf_time=perf_time,
                    pitch=60,
                    velocity=80,
                    score_beat=entry_beat + crawl * (perf_time - entry_time),
                )
            )
        )

    acquiring = [
        row
        for row in trace.records
        if row.type == "follower" and row.position_action == "orchestra_entry_acquiring"
    ]
    abandoned = [
        row
        for row in trace.records
        if row.type == "follower" and row.position_action == "orchestra_entry_abandoned"
    ]
    assert acquiring, "the cue-in should still collect onsets before giving up"
    assert len(abandoned) == 1, "recovery must fire exactly once, not every note"

    state = abandoned[0].follower_state
    assert state["abandon_reason"] == "no_fittable_pace_within_ceiling"
    assert state["abandon_after_seconds"] >= 6.0
    # No rehearsal profile is wired here, so recovery falls back to the
    # orchestra's own lead-in pulse rather than inventing a tempo.
    assert state["recovered_pace_source"] == "orchestra_lead_in"
    assert state["recovered_tempo_bpm"] == pytest.approx(120.0)

    # The orchestra is following again, and the transport moved past the beat
    # it had been frozen at.
    assert statuses[-1].state_word is LiveStateWord.FOLLOWING
    assert statuses[-1].score_beat > entry_beat
    assert any(
        row.type == "policy" and row.transition_reason == "orchestra_entry_matched"
        for row in trace.records
    )
    assert not output.panics


def test_rehearsed_pace_rejects_an_implausible_cue_in_fit() -> None:
    """A fit that passes the tempo bounds can still be wrong for this passage.

    Live take ``performance-20260731T024748Z-1dfb`` handed off at 23 BPM in a
    ~50 BPM passage: the follower was crawling, so the fit was noise that
    cleared ``min_tempo_bpm``. Where prior takes cover the passage the runtime
    has an honest expectation and must not adopt the noise. The pianist's
    measured *position* is still respected -- only the pace is replaced.
    """

    from aimusic.accompaniment.predictive_follow import PaceProfile

    bundle = _identity_reference_bundle()
    clock = ManualClock(10.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    rehearsed_period = 0.5  # 120 BPM, matching this fixture's rehearsed pace
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="implausible-fit",
            initial_tempo_bpm=120,
            follower_coast_ms=500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
        pace_profile=PaceProfile(
            smoothed_period_at=lambda _beat: rehearsed_period,
            support_at=lambda _beat: 4,
        ),
    )
    engine.start(start_beat=3.0, entry_perf_time=10.0, orchestra_lead_in=True)

    # Onsets crawling at ~0.35 beats/s -> ~21 BPM: above min_tempo_bpm, so the
    # fit is accepted today, but less than half the rehearsed pace.
    entry_time, entry_beat, crawl = 11.25, 5.5, 0.35
    for step in range(8):
        perf_time = entry_time + step * 0.4
        clock.advance_to(perf_time)
        engine.process_note(
            PerformedNote(
                perf_time=perf_time,
                pitch=60,
                velocity=80,
                score_beat=entry_beat + crawl * (perf_time - entry_time),
            )
        )

    handoff = next(
        row
        for row in trace.records
        if row.type == "follower" and row.position_action == "orchestra_entry_handoff"
    )
    state = handoff.follower_state
    assert state["entry_pace_source"] == "rehearsal_profile"
    assert state["entry_pace_rejected_bpm"] < 40.0
    assert state["entry_pace_substituted_bpm"] == pytest.approx(120.0)
    seeded = next(
        row
        for row in trace.records
        if row.type == "tempo" and row.observation_decision == "orchestra_entry_piano_seed"
    )
    assert seeded.tempo_bpm == pytest.approx(120.0, abs=1.0)
