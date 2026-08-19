"""Hardware-independent causal live accompaniment engine."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from aimusic.accompaniment.anchors import AnchorFirer
from aimusic.accompaniment.cadenza_detector import (
    CadenzaDetector,
    CadenzaFire,
    CadenzaModel,
    CadenzaState,
)
from aimusic.accompaniment.expectation import CellRole, ExpectationModel
from aimusic.accompaniment.following import (
    EntryPositionableScoreFollower,
    FollowerUpdate,
    PerformedNote,
    ScoreFollower,
)
from aimusic.accompaniment.predictive_follow import (
    LteTempoModel,
    PaceProfile,
    RehearsalAnchoredTempoModel,
)
from aimusic.accompaniment.runtime_contracts import (
    ControlTrace,
    FollowerTrace,
    FreeRegionTrace,
    InputTrace,
    PolicyTrace,
    RunPhase,
    RuntimeConfig,
    RuntimeStartTrace,
    RuntimeStatus,
    ScheduledEventTrace,
    SchedulerTrace,
    StateTrace,
    TempoTrace,
    require_run_phase_transition,
    state_word_for,
)
from aimusic.accompaniment.runtime_io import MonotonicClock, NullTraceSink, TraceSink
from aimusic.accompaniment.scheduler import (
    AccompanimentOutput,
    AccompanimentScheduler,
    ArrivalTimeCurve,
)
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode, Section, SectionMap
from aimusic.accompaniment.tempo_model import (
    EntryPaceAcquisition,
    EntryPaceEstimate,
    OnlineTempoModel,
    TempoModel,
    TempoObservation,
    TempoState,
    TimingSeed,
)
from aimusic.accompaniment.transport import Inert, TransportEvent, TransportTable


def _free_region_id(section_map) -> str:
    for section in section_map.sections:
        if getattr(section, "free_region", None):
            return section.id
    return "free-region"


def _cadenza_detector_for(bundle) -> "CadenzaDetector | None":
    """Build a detector for the first FREE section that names a trained model."""

    for section in bundle.section_map.sections:
        block = getattr(section, "free_region", None)
        if not block or block.get("policy") != "handoff_detector":
            continue
        name = block.get("detector_model")
        if not name:
            logging.getLogger(__name__).warning(
                "Free region %s declares no detector model; leaving it unarmed", section.id
            )
            continue
        path = Path(bundle.root) / "derived" / name if hasattr(bundle, "root") else Path(name)
        if not path.is_file():
            logging.getLogger(__name__).warning(
                "Free region %s names a missing detector model %s", section.id, path
            )
            continue
        return CadenzaDetector(CadenzaModel.load(path))
    return None

# A cue-in pace fit may be at most 2x slower or faster than the pace this
# passage is known to run at (rehearsed where takes cover it, else the
# orchestra's own pulse) before it is treated as follower noise rather than
# performer evidence. Passing ``min_tempo_bpm`` is not the same as being
# plausible *here*.
_FREE_REGION_MISSED_BEATS = 4.0
_PLAUSIBLE_ENTRY_PACE_RATIO = (0.5, 2.0)


class TransportAuthority(str, Enum):
    """The component currently allowed to advance musical transport."""

    FOLLOW = "follow"
    LEAD = "lead"
    ORCHESTRA_ENTRY = "orchestra_entry"
    CUE_ENTRY = "cue_entry"
    COAST = "coast"
    HOLD_AWAIT_ENTRY = "hold_await_entry"
    HOLD_DROPOUT = "hold_dropout"
    STOP = "stop"


@dataclass
class _TransportState:
    """One explicit authority/clock state instead of interacting booleans."""

    authority: TransportAuthority | None = None
    position: TempoState | None = None
    clock_anchor: TempoState | None = None
    section: Section | None = None
    section_end_reference_beat: float | None = None
    expected_entry_beat: float | None = None
    reentry_candidate: FollowerUpdate | None = None
    entry_matching_started: bool = False
    # Cue-in tempo/phase acquisition: while set, the orchestra holds at
    # ``entry_freeze`` and collects piano onsets instead of leading or following.
    entry_acquisition: EntryPaceAcquisition | None = None
    entry_freeze: TempoState | None = None

    @property
    def mode(self) -> AccompanimentMode | None:
        if self.authority in {
            TransportAuthority.FOLLOW,
            TransportAuthority.CUE_ENTRY,
            TransportAuthority.COAST,
        }:
            return AccompanimentMode.FOLLOW
        if self.authority in {
            TransportAuthority.LEAD,
            TransportAuthority.ORCHESTRA_ENTRY,
        }:
            return AccompanimentMode.LEAD
        if self.authority in {
            TransportAuthority.HOLD_AWAIT_ENTRY,
            TransportAuthority.HOLD_DROPOUT,
        }:
            return AccompanimentMode.HOLD
        if self.authority is TransportAuthority.STOP:
            return AccompanimentMode.STOP
        return None


class LiveEngine:
    """Wire follower -> tempo -> policy -> scheduler -> output causally.

    Bundle-v2 integration will replace ``score_beat`` at the boundary with a
    canonical score position. The internal compatibility coordinate remains
    explicit here so the runtime does not invent a second score mapping.
    """

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        clock: MonotonicClock,
        follower: ScoreFollower,
        tempo_model: TempoModel,
        section_map: SectionMap,
        scheduler: AccompanimentScheduler,
        output: AccompanimentOutput,
        trace_sink: TraceSink | None = None,
        anchor: AnchorFirer | None = None,
        cadenza: CadenzaDetector | None = None,
        pace_profile: PaceProfile | None = None,
        expectation: ExpectationModel | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.follower = follower
        self.tempo_model = tempo_model
        self.section_map = section_map
        self.scheduler = scheduler
        self.output = output
        self.trace_sink = trace_sink or NullTraceSink()
        # What the score asks of the performer, per cell (Decision 0015). Lets
        # the engine tell intentional silence apart from a dropout instead of
        # discovering the difference after a coast timeout. Optional so existing
        # callers and synthetic bundles keep working unchanged.
        self.expectation = expectation
        self._anchor = anchor
        # Handles passages the follower cannot track. Consulted only inside a
        # FREE section; see .agents/skills/free-region-handoff/SKILL.md.
        self._cadenza = cadenza
        self._cadenza_region_id = _free_region_id(section_map)
        self._cadenza_notes = 0
        self._cadenza_missed = False
        self._pace_profile = pace_profile
        self._phase = RunPhase.PREPARING
        self._transport = _TransportState()
        self._lead_tempo_bpm = config.initial_tempo_bpm
        self._orchestra_volume = config.orchestra_volume
        self._output_advance_ms = config.output_advance_ms
        self._last_planned_event_ids: tuple[str, ...] = ()
        self._message: str | None = None
        # Wall-clock of the most recent performer onset, distinct from the last
        # *confident* follower position. A dense or chromatic passage keeps the
        # matcher below its lock threshold while notes keep arriving; keying the
        # dropout on confident positions alone then mistakes "the tracker is
        # unsure" for "the pianist has stopped" and cuts the orchestra mid-phrase.
        # Genuine silence is the only thing that may expire into a dropout hold.
        self._last_input_at: float | None = None
        self._transport_table = self._build_transport_table()

    def _build_transport_table(self) -> TransportTable[RuntimeStatus]:
        """Declare every (authority, event) cell -- including the inert ones.

        Construction fails if a cell is missing, so a state can no longer lose a
        behaviour by omission. ``Inert`` records *why* nothing happens, which is
        the information silent fall-through used to destroy.
        """

        A, E = TransportAuthority, TransportEvent
        return TransportTable(
            TransportAuthority,
            {
                (A.FOLLOW, E.TICK): self._tick_follow,
                (A.FOLLOW, E.UPDATE): self._update_default,
                (A.LEAD, E.TICK): self._tick_lead,
                (A.LEAD, E.UPDATE): self._update_during_lead,
                (A.ORCHESTRA_ENTRY, E.TICK): self._tick_orchestra_entry,
                (A.ORCHESTRA_ENTRY, E.UPDATE): self._update_during_orchestra_entry,
                (A.CUE_ENTRY, E.TICK): self._tick_coasting,
                (A.CUE_ENTRY, E.UPDATE): self._update_default,
                (A.COAST, E.TICK): self._tick_coasting,
                (A.COAST, E.UPDATE): self._update_default,
                (A.HOLD_AWAIT_ENTRY, E.TICK): Inert(
                    "waiting for the pianist's re-entry; nothing may be scheduled "
                    "until a confident position arrives"
                ),
                (A.HOLD_AWAIT_ENTRY, E.UPDATE): self._update_default,
                (A.HOLD_DROPOUT, E.TICK): self._tick_dispatch_only,
                (A.HOLD_DROPOUT, E.UPDATE): self._update_default,
                (A.STOP, E.TICK): self._tick_dispatch_only,
                (A.STOP, E.UPDATE): Inert(
                    "the authored stop is terminal; later positions cannot restart it"
                ),
            },
        )

    @classmethod
    def from_bundle(
        cls,
        *,
        bundle: ScoreBundle,
        config: RuntimeConfig,
        clock: MonotonicClock,
        follower: ScoreFollower,
        output: AccompanimentOutput,
        trace_sink: TraceSink | None = None,
        tempo_model: TempoModel | None = None,
        anchor_ticks: Sequence[int] = (),
        arrival_curve: ArrivalTimeCurve | None = None,
        pace_profile: PaceProfile | None = None,
    ) -> "LiveEngine":
        """Build the standard runtime with all timing knobs from one config."""

        anchor = AnchorFirer(anchor_ticks, bundle) if len(anchor_ticks) else None
        scheduler = AccompanimentScheduler(
            bundle,
            planning_horizon_seconds=config.planning_horizon_ms / 1000,
            dispatch_horizon_seconds=config.dispatch_horizon_ms / 1000,
            output_advance_ms=config.output_advance_ms,
            arrival_curve=arrival_curve,
            reactive_event_ids=anchor.owned_event_ids if anchor is not None else (),
        )
        base_tempo = tempo_model or OnlineTempoModel(initial_tempo_bpm=config.initial_tempo_bpm)
        selected_tempo: TempoModel = base_tempo
        if config.follow_clock == "lte" and isinstance(base_tempo, OnlineTempoModel):
            # LTE keeps the reactive pace estimate and adds a leading phase lock
            # against the scheduler's shared reference timeline.
            selected_tempo = LteTempoModel(
                base=base_tempo,
                project_reference_beat=scheduler.reference_beat_at_score_beat,
            )
        if pace_profile is not None:
            # Anchor the pace to the rehearsal plan where the takes support it;
            # live evidence then only scales it. Wraps whatever pace/phase model
            # was selected above so position tracking and phase lead are kept.
            selected_tempo = RehearsalAnchoredTempoModel(
                base=selected_tempo,
                profile=pace_profile,
            )
        # Built here, like the anchor firer, so every engine picks up a declared
        # free region without each call site remembering to. A FREE section with
        # no trained model is left alone rather than half-armed: the detector is
        # the only thing that ends the region, so arming without one would strand
        # the orchestra behind a passage nothing can hand back from.
        cadenza = _cadenza_detector_for(bundle)
        # Derived here rather than passed in, so that *every* engine gets the
        # score's expectation without each call site having to remember. A bundle
        # with no solo events (synthetic fixtures) simply has nothing to expect,
        # and the engine falls back to its prior silence handling.
        try:
            expectation = ExpectationModel.from_bundle(bundle)
        except ValueError:
            expectation = None
        return cls(
            config=config,
            clock=clock,
            follower=follower,
            tempo_model=selected_tempo,
            section_map=bundle.section_map,
            scheduler=scheduler,
            output=output,
            trace_sink=trace_sink,
            anchor=anchor if anchor is not None and not anchor.is_empty else None,
            cadenza=cadenza,
            pace_profile=pace_profile,
            expectation=expectation,
        )

    @property
    def status(self) -> RuntimeStatus:
        tempo = self._transport.position
        mode = self._transport.mode
        return RuntimeStatus(
            run_id=self.config.run_id,
            run_mode=self.config.run_mode,
            phase=self._phase,
            state_word=state_word_for(mode, self._phase),
            monotonic_time=self.clock.now(),
            score_beat=tempo.score_beat if tempo else None,
            confidence=tempo.confidence if tempo else None,
            tempo_bpm=tempo.tempo_bpm if tempo else None,
            orchestra_tempo_bpm=self._lead_tempo_bpm,
            orchestra_volume=self._orchestra_volume,
            orchestra_output_advance_ms=self._output_advance_ms,
            section_mode=mode,
            message=self._message,
        )

    def current_score_beat(self, *, at: float | None = None) -> float | None:
        """Return the transport clock position between follower note arrivals.

        Status intentionally reports the last evidence-backed FOLLOW position.
        Continuous score automation needs the transport's forward estimate
        instead, while LEAD and COAST already own explicit clocks.
        """

        position = self._transport.position
        authority = self._transport.authority
        if position is None or authority is None:
            return None
        now = self.clock.now() if at is None else at
        if authority is TransportAuthority.FOLLOW:
            return self._predicted_follow_beat(now, position)
        if authority is TransportAuthority.LEAD:
            return self._lead_state_at(now).score_beat
        if authority is TransportAuthority.ORCHESTRA_ENTRY:
            if self._transport.entry_acquisition is not None:
                freeze = self._transport.entry_freeze
                return freeze.score_beat if freeze is not None else position.score_beat
            return self._orchestra_lead_in_state_at(now).score_beat
        if authority in {TransportAuthority.COAST, TransportAuthority.CUE_ENTRY}:
            return self._follow_coast_state_at(now).score_beat
        return position.score_beat

    def start(
        self,
        *,
        start_beat: float | None = None,
        entry_perf_time: float | None = None,
        orchestra_lead_in: bool = False,
    ) -> RuntimeStatus:
        """Start at the authored opening or at an explicit canonical entry.

        ``start_beat`` is canonical score identity. The scheduler owns the
        one canonical->reference projection; both coordinates are then seeded
        at ``entry_perf_time``. ``orchestra_lead_in`` grants the orchestra an
        explicit monotonic clock until real piano evidence transfers authority
        to FOLLOW; it is not follower dropout coasting.
        """

        if start_beat is None and entry_perf_time is not None:
            raise ValueError("entry_perf_time requires start_beat")
        if start_beat is not None and start_beat < 0:
            raise ValueError("start_beat must be non-negative")
        if entry_perf_time is not None and entry_perf_time < self.clock.now():
            raise ValueError("entry_perf_time cannot be in the past")
        self._transition(RunPhase.LISTENING)
        opening_beat = self.scheduler.first_accompaniment_beat if start_beat is None else start_beat
        opening_section = self.section_map.section_at(opening_beat)
        if start_beat is not None:
            return self._start_from_beat(
                start_beat,
                opening_section,
                self.clock.now() if entry_perf_time is None else entry_perf_time,
                orchestra_lead_in=orchestra_lead_in,
            )
        if opening_section.mode is AccompanimentMode.LEAD:
            now = self.clock.now()
            tempo_bpm = opening_section.tempo_bpm or self.config.initial_tempo_bpm
            reference_beat = self.scheduler.reference_beat_at_score_beat(opening_beat)
            reference_period = self.scheduler.reference_period_for_score_tempo(
                start_score_beat=opening_section.start_beat,
                end_score_beat=opening_section.end_beat,
                score_tempo_bpm=tempo_bpm,
            )
            tempo = self._state_on_reference_clock(
                perf_time=now,
                score_beat=opening_beat,
                confidence=1.0,
                reference_beat=reference_beat,
                reference_beat_period_seconds=reference_period,
                fallback_beat_period=60.0 / tempo_bpm,
            )
            self._transition(RunPhase.ACTIVE)
            self._begin_lead(opening_section, tempo, now, reason="run_started_in_lead")
        return self.status

    def _start_from_beat(
        self,
        score_beat: float,
        section: Section,
        entry_perf_time: float,
        *,
        orchestra_lead_in: bool = False,
    ) -> RuntimeStatus:
        if section.mode is AccompanimentMode.STOP:
            raise ValueError("cannot start inside a STOP section")
        now = self.clock.now()
        tempo_bpm = section.tempo_bpm or self.config.initial_tempo_bpm
        reference_beat = self.scheduler.reference_beat_at_score_beat(score_beat)
        # An orchestra-led opening is source playback with one global speed
        # scalar. Calibrating over one beat magnifies local projection noise.
        calibration_end = (
            section.end_beat
            if orchestra_lead_in
            else min(section.end_beat, score_beat + 1.0)
        )
        reference_period = self.scheduler.reference_period_for_score_tempo(
            start_score_beat=score_beat,
            end_score_beat=calibration_end,
            score_tempo_bpm=tempo_bpm,
        )
        tempo = self._state_on_reference_clock(
            perf_time=entry_perf_time,
            score_beat=score_beat,
            confidence=0.0,
            reference_beat=reference_beat,
            reference_beat_period_seconds=reference_period,
            coasting=True,
            fallback_beat_period=60.0 / tempo_bpm,
        )
        self.trace_sink.write(
            RuntimeStartTrace(
                type="runtime_start",
                monotonic_time=now,
                entry_perf_time=entry_perf_time,
                score_beat=score_beat,
                reference_beat=reference_beat,
                section_mode=section.mode,
                start_kind=("orchestra_lead_in" if orchestra_lead_in else "count_off_measure"),
            )
        )
        self._transition(RunPhase.ACTIVE)
        if orchestra_lead_in:
            self.tempo_model.seed_timing(
                TimingSeed(
                    perf_time=tempo.perf_time,
                    score_beat=tempo.score_beat,
                    beat_period_seconds=tempo.beat_period_seconds,
                    reference_beat=tempo.reference_beat,
                    reference_beat_period_seconds=tempo.reference_beat_period_seconds,
                )
            )
            self._transport = _TransportState(
                authority=TransportAuthority.ORCHESTRA_ENTRY,
                position=tempo,
                clock_anchor=tempo,
            )
            lead_state = self._orchestra_lead_in_state_at(now)
            self._transport.position = lead_state
            self._message = "Orchestra leading — join when ready"
            self._write_tempo_policy(
                lead_state,
                AccompanimentMode.LEAD,
                now,
                transition_reason="orchestra_lead_in_started",
                observation_decision="orchestra_entry_timing_seed",
            )
            self._run_scheduler(lead_state, AccompanimentMode.LEAD, now)
            return self.status
        if section.mode is AccompanimentMode.LEAD:
            self._begin_lead(
                section,
                tempo,
                now,
                reason="run_started_from_measure_in_lead",
                anchor_perf_time=entry_perf_time,
            )
            return self.status
        # The count-off supplies a clock origin, not piano evidence. Seed the
        # selected reactive/LTE model through its explicit timing seam so the
        # first real follower update owns confidence and phase correction.
        self.tempo_model.seed_timing(
            TimingSeed(
                perf_time=tempo.perf_time,
                score_beat=tempo.score_beat,
                beat_period_seconds=tempo.beat_period_seconds,
                reference_beat=tempo.reference_beat,
                reference_beat_period_seconds=tempo.reference_beat_period_seconds,
            )
        )
        if section.mode is AccompanimentMode.HOLD:
            self._transport = _TransportState(
                authority=TransportAuthority.HOLD_AWAIT_ENTRY,
                position=tempo,
                expected_entry_beat=section.end_beat,
            )
            self._write_tempo_policy(
                tempo,
                AccompanimentMode.HOLD,
                now,
                transition_reason="run_started_from_measure_in_hold",
                observation_decision="count_off_timing_seed",
            )
            self._run_scheduler(tempo, AccompanimentMode.HOLD, now)
            return self.status

        self._transport = _TransportState(
            authority=TransportAuthority.CUE_ENTRY,
            position=tempo,
            clock_anchor=tempo,
        )
        self._message = "Following the count-off cue — waiting for score follower lock"
        self._write_tempo_policy(
            tempo,
            AccompanimentMode.FOLLOW,
            now,
            transition_reason="run_started_from_measure_in_follow",
            observation_decision="count_off_timing_seed",
        )
        self._run_scheduler(tempo, AccompanimentMode.FOLLOW, now)
        return self.status

    def _apply_free_region_fire(self, fire: CadenzaFire, now: float) -> None:
        """Hand transport back to the score at the end of a free region.

        Moving the position is not enough, which is the bug this exists to fix.
        Throughout the free region the section is not FOLLOW, so the scheduler
        dispatch set authority to STOP on every note -- correct while the
        performer plays alone. Snapping the position to the hand-back beat left
        that STOP in place, so the orchestra never entered the LEAD interlude the
        handoff exists to trigger. The hand-back must actively begin the section
        it lands in, exactly as a symbolic position crossing into LEAD would.
        """

        self.trace_sink.write(
            FreeRegionTrace(
                type="free_region",
                monotonic_time=now,
                region_id=self._cadenza_region_id,
                event=fire.kind,
                score_beat=fire.canonical_beat,
                hand_back_beat=fire.canonical_beat,
                probability=fire.probability,
                notes_observed=self._cadenza_notes,
            )
        )
        position = self._transport.position
        period = (
            position.beat_period_seconds
            if position is not None
            else 60.0 / self._lead_tempo_bpm
        )
        handback = TempoState(
            perf_time=now,
            score_beat=fire.canonical_beat,
            beat_period_seconds=period,
            tempo_bpm=60.0 / period,
            confidence=1.0,
        )
        self._transport.position = handback
        self._transport.clock_anchor = handback
        self._message = (
            f"Cadenza {fire.kind} at beat {fire.canonical_beat:.0f} — orchestra entering"
        )
        if self._phase is RunPhase.LISTENING:
            self._transition(RunPhase.ACTIVE)
        section = self.section_map.section_at(fire.canonical_beat)
        if section.mode is AccompanimentMode.LEAD:
            self._begin_lead(
                section,
                handback,
                now,
                reason="free_region_handoff_entered_lead_section",
            )
        else:
            # The interlude that follows is not orchestra-led (unusual, but the
            # section map is authoritative). Rejoin as a normal follower rather
            # than leaving the STOP the free region left behind.
            self._transport.authority = TransportAuthority.FOLLOW
            self.scheduler.resume()
            self._run_scheduler(handback, section.mode, now)

    def process_note(self, note: PerformedNote) -> RuntimeStatus:
        if self._phase not in {RunPhase.LISTENING, RunPhase.ACTIVE}:
            raise RuntimeError(f"Cannot process input while run is {self._phase.value}")
        input_at = self.clock.now()
        # Every performer onset, however the follower later rates it, is proof the
        # pianist is still playing. Dropout coasting is judged against this, so a
        # low-confidence stretch never masquerades as silence.
        self._last_input_at = input_at
        if self._anchor is not None:
            # Reactive beat anchors run before follower/tempo work so a marked
            # chord lands with the pianist's bass, not a prediction behind it.
            position = self._transport.position
            self._anchor.maybe_fire(
                note.pitch,
                input_at,
                position.score_beat if position else None,
                self.scheduler,
                self.output,
                position.beat_period_seconds if position else None,
                position.reference_beat_period_seconds if position else None,
            )
        if self._cadenza is not None:
            # A free region (cadenza, fermata, improvised lead-in). Position is
            # read once to arm; after that the detector runs on the performer's
            # notes alone, because the estimate it exists to correct is exactly
            # the one that has stopped moving.
            position = self._transport.position
            if position is not None and self._cadenza.maybe_arm(position.score_beat, input_at):
                self._cadenza_notes = 0
                self.trace_sink.write(
                    FreeRegionTrace(
                        type="free_region",
                        monotonic_time=input_at,
                        region_id=self._cadenza_region_id,
                        event="armed",
                        score_beat=position.score_beat,
                    )
                )
            if (
                position is not None
                and self._cadenza.state is CadenzaState.IDLE
                and not self._cadenza_missed
                and position.score_beat
                > self._cadenza._model.region_start_beat + _FREE_REGION_MISSED_BEATS
            ):
                # Walked past the region without ever arming. Silent to the ear
                # and identical to "armed but never fired", so it has to be said
                # explicitly or the trace cannot tell the two apart.
                self._cadenza_missed = True
                self.trace_sink.write(
                    FreeRegionTrace(
                        type="free_region",
                        monotonic_time=input_at,
                        region_id=self._cadenza_region_id,
                        event="not_armed",
                        score_beat=position.score_beat,
                        detail=(
                            "position passed the region without arming; the follower was "
                            "not within arming distance when the region began"
                        ),
                    )
                )
            if self._cadenza.state is CadenzaState.ARMED:
                self._cadenza_notes += 1
            fire = self._cadenza.observe(note.pitch, input_at)
            if fire is not None:
                self._apply_free_region_fire(fire, input_at)
                # The region has handed control back. This very note must not
                # continue to the follower: the follower is still down in the
                # cadenza, and its update would re-enter the FREE section and
                # reset the transport to STOP -- which is exactly what made the
                # interlude play one chord and stall. Once handed off, the LEAD
                # clock is authoritative, not the follower.
                self.trace_sink.write(
                    InputTrace(
                        type="input",
                        monotonic_time=input_at,
                        perf_time=note.perf_time,
                        pitch=note.pitch,
                        velocity=note.velocity,
                    )
                )
                return self.status
        self.trace_sink.write(
            InputTrace(
                type="input",
                monotonic_time=input_at,
                perf_time=note.perf_time,
                pitch=note.pitch,
                velocity=note.velocity,
            )
        )
        if (
            self._transport.authority is TransportAuthority.ORCHESTRA_ENTRY
            and not self._transport.entry_matching_started
        ):
            entry_position = self._orchestra_lead_in_state_at(note.perf_time)
            self._transport.position = entry_position
            self._transport.entry_matching_started = True
            if isinstance(self.follower, EntryPositionableScoreFollower):
                # The lead-in state drops reference to keep event spacing
                # uniform, but Matchmaker's prior is a reference beat: recover it
                # from the warp at the orchestra's current canonical position.
                entry_reference = self.scheduler.reference_beat_at_score_beat(
                    entry_position.score_beat
                )
                self.follower.reposition_for_entry(
                    score_beat=entry_position.score_beat,
                    reference_beat=entry_reference,
                )
        update = self.follower.observe(note)
        return self.process_update(update, input_at=input_at)

    def process_update(
        self,
        update: FollowerUpdate | None,
        *,
        input_at: float | None = None,
    ) -> RuntimeStatus:
        """Apply one follower position, whoever produced it.

        Split out of :meth:`process_note` so a position that arrives *after* its
        note -- the normal case with an out-of-process follower -- can still be
        applied from ``tick`` when no further note follows it. Dispatch goes
        through the total transport table, so no authority can silently lack a
        response to new evidence.
        """

        now = self.clock.now()
        if input_at is None:
            input_at = now
        if update is None:
            return self.status
        authority = self._transport.authority
        if authority is None:
            # Evidence can arrive before transport has an authority (a note
            # played while still LISTENING). The ordinary path owns that case.
            return self._update_default(update, now, input_at)
        handler = self._transport_table.handler(authority, TransportEvent.UPDATE)
        if isinstance(handler, Inert):
            return self.status
        return handler(update, now, input_at)

    def _update_during_lead(
        self, update: FollowerUpdate, now: float, input_at: float
    ) -> RuntimeStatus:
        if self._transport.authority is TransportAuthority.LEAD:
            # An authored orchestral passage owns its clock until its canonical
            # endpoint. Piano observations remain diagnostic, but an early
            # re-entry or follower jump must not cut off the interlude.
            near_handoff = self._is_expected_entry_candidate(
                update,
                (self._transport.section.end_beat if self._transport.section is not None else None),
            )
            if near_handoff:
                self._transport.reentry_candidate = update
            self.trace_sink.write(
                FollowerTrace(
                    type="follower",
                    monotonic_time=now,
                    perf_time=update.perf_time,
                    score_beat=self._transport.position.score_beat,
                    raw_score_beat=update.score_beat,
                    reference_beat=update.reference_beat,
                    confidence=update.confidence,
                    position_action=(
                        "buffered_for_lead_handoff" if near_handoff else "ignored_during_lead"
                    ),
                    processing_latency_ms=(now - input_at) * 1000,
                    follower_state=update.raw_state,
                )
            )
            return self.status


    def _update_during_orchestra_entry(
        self, update: FollowerUpdate, now: float, input_at: float
    ) -> RuntimeStatus:
        if (
            self._transport.authority is TransportAuthority.ORCHESTRA_ENTRY
            and update.confidence < self.config.minimum_follower_confidence
        ):
            self.trace_sink.write(
                FollowerTrace(
                    type="follower",
                    monotonic_time=now,
                    perf_time=update.perf_time,
                    score_beat=self._transport.position.score_beat,
                    raw_score_beat=update.score_beat,
                    reference_beat=update.reference_beat,
                    confidence=update.confidence,
                    position_action="orchestra_entry_not_yet_locked",
                    processing_latency_ms=(now - input_at) * 1000,
                    follower_state=update.raw_state,
                )
            )
            return self.status

        if self._transport.authority is TransportAuthority.ORCHESTRA_ENTRY:
            acquisition = self._transport.entry_acquisition
            if acquisition is None:
                # First confident match: certify *where* the pianist entered
                # relative to the still-moving orchestra. This proximity gate
                # only guards the start of acquisition; once certified, later
                # onsets are gathered even as the pianist plays past this beat.
                orchestra_position = self._orchestra_lead_in_state_at(now)
                section = self.section_map.section_at(orchestra_position.score_beat)
                position_matches = (
                    section.mode is AccompanimentMode.FOLLOW
                    and self._is_expected_entry_candidate(
                        update,
                        orchestra_position.score_beat,
                    )
                )
                if not position_matches:
                    self.trace_sink.write(
                        FollowerTrace(
                            type="follower",
                            monotonic_time=now,
                            perf_time=update.perf_time,
                            score_beat=orchestra_position.score_beat,
                            raw_score_beat=update.score_beat,
                            reference_beat=update.reference_beat,
                            confidence=update.confidence,
                            position_action="orchestra_entry_position_mismatch",
                            processing_latency_ms=(now - input_at) * 1000,
                            follower_state={
                                **update.raw_state,
                                "orchestra_score_beat": orchestra_position.score_beat,
                                "orchestra_reference_beat": orchestra_position.reference_beat,
                            },
                        )
                    )
                    return self.status
                acquisition = EntryPaceAcquisition(
                    min_onsets=self.config.entry_tempo_min_onsets,
                    min_canonical_span_beats=self.config.entry_tempo_min_span_beats,
                    min_elapsed_seconds=self.config.entry_tempo_min_seconds,
                    max_elapsed_seconds=self.config.entry_tempo_max_seconds,
                )
                self._transport.entry_acquisition = acquisition
                self._transport.entry_freeze = orchestra_position

            # Position is certified; pace is not. Matching two notes spans ~0
            # beats, so seeding tempo now would hand the clock the orchestra's
            # warp-inflated lead-in pace and then smooth the pianist's real pace
            # into that artifact. Freeze the orchestra here and collect piano
            # onsets until a pace fit to the pianist alone is trustworthy.
            freeze = self._transport.entry_freeze
            assert freeze is not None
            acquisition.observe(
                perf_time=update.perf_time,
                canonical_beat=update.score_beat,
                reference_beat=update.reference_beat,
            )
            estimate = acquisition.estimate(force=acquisition.should_force(now))
            if estimate is None and acquisition.should_abandon(
                now, self.config.entry_tempo_abandon_seconds
            ):
                # The pianist is audibly playing but no pace can be fit to the
                # follower's estimates. Recover on the rehearsed pace here (or
                # the orchestra's own pulse when no takes cover this passage)
                # rather than leaving the orchestra frozen. Position comes from
                # the follower's latest estimate, floored at the freeze so
                # transport stays monotonic.
                return self._abandon_entry_acquisition(
                    acquisition, freeze, update, now, input_at
                )
            if estimate is None:
                self.trace_sink.write(
                    FollowerTrace(
                        type="follower",
                        monotonic_time=now,
                        perf_time=update.perf_time,
                        score_beat=freeze.score_beat,
                        raw_score_beat=update.score_beat,
                        reference_beat=update.reference_beat,
                        confidence=update.confidence,
                        position_action="orchestra_entry_acquiring",
                        processing_latency_ms=(now - input_at) * 1000,
                        follower_state={
                            **update.raw_state,
                            "entry_onset_count": acquisition.onset_count,
                            "orchestra_score_beat": freeze.score_beat,
                        },
                    )
                )
                self._transport.position = freeze
                self._run_scheduler(freeze, AccompanimentMode.LEAD, now)
                return self.status
            return self._clamp_entry_to_follow(estimate, update, now, input_at)


    def _update_default(
        self, update: FollowerUpdate, now: float, input_at: float
    ) -> RuntimeStatus:
        """Follow/coast/hold: the ordinary path for a confident position."""

        if (
            update.confidence < self.config.minimum_follower_confidence
            and self._transport.authority is TransportAuthority.HOLD_AWAIT_ENTRY
            and self._is_expected_entry_candidate(
                update,
                self._transport.expected_entry_beat,
            )
        ):
            update = replace(
                update,
                confidence=self.config.minimum_follower_confidence,
                raw_state={
                    **update.raw_state,
                    "lock_policy": "expected_entry_window",
                },
            )
        if update.confidence < self.config.minimum_follower_confidence:
            return self._handle_uncertain_follow_update(update, now, input_at)

        tempo = self.tempo_model.update(update)
        observation = self.tempo_model.last_observation
        self.trace_sink.write(
            FollowerTrace(
                type="follower",
                monotonic_time=now,
                perf_time=update.perf_time,
                score_beat=tempo.score_beat,
                raw_score_beat=update.score_beat,
                reference_beat=update.reference_beat,
                confidence=update.confidence,
                position_action=observation.position_action,
                processing_latency_ms=(now - input_at) * 1000,
                follower_state=update.raw_state,
            )
        )
        if self._complete_at_score_end(tempo):
            return self.status
        section = self.section_map.section_at(tempo.score_beat)
        reentry_ready = (
            self._transport.authority is TransportAuthority.HOLD_AWAIT_ENTRY
            and tempo.confidence >= self.config.minimum_follower_confidence
            and self._transport.expected_entry_beat is not None
            and tempo.score_beat >= self._transport.expected_entry_beat - 0.25
            and section.mode is AccompanimentMode.FOLLOW
        )
        if self._transport.authority is TransportAuthority.HOLD_AWAIT_ENTRY and not reentry_ready:
            self._write_tempo_policy(
                tempo,
                AccompanimentMode.HOLD,
                now,
                observation=observation,
                transition_reason="awaiting_confident_solo_reentry",
            )
            return self.status

        mode = section.mode
        if reentry_ready:
            self._transport.authority = TransportAuthority.FOLLOW
            self._transport.expected_entry_beat = None
            self.scheduler.resume()
        if self._transport.authority is TransportAuthority.HOLD_DROPOUT:
            self._transport.authority = TransportAuthority.FOLLOW
            self.scheduler.resume()
        self._transport.clock_anchor = None
        self._message = None
        if mode is AccompanimentMode.LEAD:
            self._begin_lead(
                section,
                tempo,
                now,
                reason="symbolic_position_entered_lead_section",
            )
            return self.status
        previous_mode = self._transport.mode
        self._transport.position = tempo
        self._transport.authority = (
            TransportAuthority.FOLLOW
            if mode is AccompanimentMode.FOLLOW
            else TransportAuthority.STOP
        )
        self._write_tempo_policy(
            tempo,
            mode,
            now,
            observation=observation,
            previous_mode=previous_mode,
            transition_reason=(
                "confident_solo_reentry"
                if previous_mode is AccompanimentMode.HOLD
                else "score_position_update"
            ),
        )
        if (
            self._phase is RunPhase.LISTENING
            and tempo.confidence >= self.config.minimum_follower_confidence
        ):
            self._transition(RunPhase.ACTIVE)
        if self._phase is RunPhase.ACTIVE:
            self._run_scheduler(tempo, mode, now)
        return self.status

    def tick(self) -> RuntimeStatus:
        """Dispatch a due plan even when no new solo note has arrived."""

        if self._phase is not RunPhase.ACTIVE or self._transport.position is None:
            return self.status
        # An out-of-process follower answers a note slightly after it was handed
        # over, so the newest position can arrive with no further input. Drain it
        # here or the last note before any silence -- a phrase ending, a fermata,
        # the final note of an entry -- would stay unconsumed until the pianist
        # happened to play again.
        poll = getattr(self.follower, "poll_update", None)
        if callable(poll):
            pending = poll()
            if pending is not None:
                return self.process_update(pending)
        return self._dispatch_tick(self.clock.now())

    def _dispatch_tick(self, now: float) -> RuntimeStatus:
        """Route this tick through the explicit transport table.

        The table is total, so a state can never silently lack a tick handler --
        which is exactly how FOLLOW stopped honouring its coast contract.
        """

        authority = self._transport.authority
        assert authority is not None
        handler = self._transport_table.handler(authority, TransportEvent.TICK)
        if isinstance(handler, Inert):
            return self.status
        return handler(now)

    def _tick_lead(self, now: float) -> RuntimeStatus:
        if self._transport.section is None:
            return self._tick_dispatch_only(now)
        tempo = self._lead_state_at(now)
        score_beat = tempo.score_beat
        self._transport.position = tempo
        self._run_scheduler(tempo, AccompanimentMode.LEAD, now)
        reference_complete = (
            self._transport.section_end_reference_beat is not None
            and tempo.reference_beat is not None
            and tempo.reference_beat >= self._transport.section_end_reference_beat
        )
        if score_beat >= self._transport.section.end_beat or reference_complete:
            self._finish_lead(now, await_follow=True)
        return self.status

    def _tick_orchestra_entry(self, now: float) -> RuntimeStatus:
        if self._transport.entry_acquisition is not None:
            # Piano detected: hold at the entry beat and keep dispatching the
            # events already due there while onsets accumulate. The pianist,
            # not the lead-in clock, will advance transport at the clamp. A
            # timeout clamp lives in process_note, where a fresh onset proves
            # the pianist is still playing; a stalled entrance simply waits.
            freeze = self._transport.entry_freeze
            assert freeze is not None
            self._transport.position = freeze
            self._run_scheduler(freeze, AccompanimentMode.LEAD, now)
            return self.status
        orchestra = self._orchestra_lead_in_state_at(now)
        if self._complete_at_score_end(orchestra):
            return self.status
        section = self.section_map.section_at(orchestra.score_beat)
        if section.mode is AccompanimentMode.STOP:
            previous_mode = self._transport.mode
            self._transport.position = orchestra
            self._transport.authority = TransportAuthority.STOP
            self._message = "Reached the authored stop"
            self._write_tempo_policy(
                orchestra,
                AccompanimentMode.STOP,
                now,
                previous_mode=previous_mode,
                transition_reason="orchestra_entry_reached_authored_stop",
                observation_decision="orchestra_entry_clock",
            )
            self._run_scheduler(orchestra, AccompanimentMode.STOP, now)
            return self.status
        if section.mode is AccompanimentMode.HOLD:
            previous_mode = self._transport.mode
            self._transport = _TransportState(
                authority=TransportAuthority.HOLD_AWAIT_ENTRY,
                position=orchestra,
                expected_entry_beat=section.end_beat,
            )
            self._message = "Waiting at the authored hold"
            self._write_tempo_policy(
                orchestra,
                AccompanimentMode.HOLD,
                now,
                previous_mode=previous_mode,
                transition_reason="orchestra_entry_reached_authored_hold",
                observation_decision="orchestra_entry_clock",
            )
            self._run_scheduler(orchestra, AccompanimentMode.HOLD, now)
            return self.status
        if section.mode is AccompanimentMode.LEAD:
            # The initial orchestral introduction is still the cue-in
            # acquisition path. Converting it to ordinary LEAD here used to
            # pause the scheduler exactly at the solo boundary, cancelling
            # orchestral attacks just after the pianist's first pickup while
            # Matchmaker gathered enough onsets to lock. Retain ORCHESTRA_ENTRY
            # authority across the boundary until piano position and pace are
            # both trustworthy.
            self._transport.position = orchestra
            self._run_scheduler(orchestra, AccompanimentMode.LEAD, now)
            return self.status
        self._transport.position = orchestra
        self._run_scheduler(orchestra, AccompanimentMode.LEAD, now)
        return self.status

    def _predicted_follow_beat(self, now: float, position: TempoState) -> float:
        """Where the score has moved to since the last correction.

        FOLLOW tracks the performer, so `position` only advances when a note
        arrives. Between notes the music still moves, and the engine needs that
        forward estimate to see a tacet passage coming.
        """

        elapsed = now - position.perf_time
        if elapsed <= 0:
            return position.score_beat
        # Prefer the shared reference timeline, so the projection respects the
        # piece's own tempo shape rather than extrapolating a flat pulse.
        if position.reference_beat is not None and position.reference_beat_period_seconds:
            reference_beat = (
                position.reference_beat + elapsed / position.reference_beat_period_seconds
            )
            projected = self.scheduler.score_beat_at_reference_beat(reference_beat)
            if projected is not None:
                return max(position.score_beat, projected)
        if position.beat_period_seconds:
            return position.score_beat + elapsed / position.beat_period_seconds
        return position.score_beat

    def _tacet_handback_section(self, score_beat: float) -> Section | None:
        """The LEAD section to hand back to, when the score is silent here.

        Returns None whenever the performer is expected to be playing, so the
        ordinary follow/coast/dropout contract is untouched. Requires *both* the
        authored section map to say LEAD and the derived expectation to say the
        solo part is silent: the section map is coarse and human-authored, and a
        sustained solo note crossing a section edge must not hand authority away
        mid-phrase.
        """

        if self.expectation is None:
            return None
        try:
            section = self.section_map.section_at(score_beat)
        except ValueError:
            return None
        if section.mode is not AccompanimentMode.LEAD:
            return None
        # Judge the *region*, not the single cell. An authored interlude can hold
        # a couple of solo notes -- m.22 has two -- and requiring literal silence
        # would leave the accompanist trying to follow two notes a bar. Requiring
        # low density instead also protects against a coarse section edge cutting
        # into a genuinely solo passage.
        if not self.expectation.is_orchestra_led_region(
            section.start_beat, section.end_beat
        ):
            return None
        return section

    def _tick_follow(self, now: float) -> RuntimeStatus:
        """Silence is evidence: FOLLOW must honour follower_coast_ms unprompted.

        FOLLOW used to be left only when a *low-confidence note* arrived, so a
        pianist who simply stopped -- a fermata, a memory slip, walking away --
        produced no note, and the coast contract never ran.

        But silence only *means* something where the score asked for sound. When
        FOLLOW runs into a passage the solo part leaves silent -- an orchestral
        interlude such as m.22 -- the performer has done nothing wrong, and the
        accompanist must simply take the lead. Previously this region was reached
        only by way of a coast timeout, so the interlude opened with
        "Tracker uncertain" and roughly a beat of hesitation before LEAD engaged
        (Decision 0015: a handback is anticipated, not discovered).
        """

        position = self._transport.position
        assert position is not None
        # Check the *predicted* beat, not the last corrected one. The performer's
        # last note lands before the tacet passage begins -- that is what makes it
        # tacet -- so a check against `position.score_beat` would never fire and
        # the interlude would still be reached only by coast timeout. Prediction
        # never stops (Decision 0015), so ask where the music is now.
        predicted = self._predicted_follow_beat(now, position)
        handback = self._tacet_handback_section(predicted)
        if handback is not None:
            self._begin_lead(
                handback,
                position,
                now,
                reason="reached_authored_tacet_handback",
            )
            self._message = "Orchestra leading this passage"
            return self.status
        if now - position.perf_time > self.config.follower_coast_ms / 1000.0:
            self._transport.clock_anchor = position
            self._transport.authority = TransportAuthority.COAST
            self.tempo_model.reset_timing_reference()
            self._message = "Tracker uncertain — coasting from the last confident beat"
            return self._tick_coasting(now)
        return self._tick_dispatch_only(now)

    def _tick_coasting(self, now: float) -> RuntimeStatus:
        cue_entry = self._transport.authority is TransportAuthority.CUE_ENTRY
        coast = self._follow_coast_state_at(now)
        assert self._transport.clock_anchor is not None
        if self._complete_at_score_end(coast):
            return self.status
        section = self.section_map.section_at(coast.score_beat)
        if section.mode is AccompanimentMode.LEAD:
            self._begin_lead(
                section,
                coast,
                now,
                reason=(
                    "count_off_cue_reached_authored_lead_section"
                    if cue_entry
                    else "coast_reached_authored_lead_section"
                ),
            )
            return self.status
        anchor = self._transport.clock_anchor
        if cue_entry:
            if now - anchor.perf_time > self.config.follower_coast_ms / 1000.0:
                self._enter_entry_hold(coast, now)
                return self.status
        elif self._score_expects_fresh_evidence(coast.score_beat) and self._coast_should_hold(
            now, anchor
        ):
            self._enter_dropout_hold(coast, now)
            return self.status
        else:
            # Either the score expects no fresh attack here (a notated rest or
            # sustain), or the budget has not run dry, or the pianist is still
            # playing but hard to localize. In every case keep predicting and
            # dispatching so an orchestral cue the pianist is waiting to hear --
            # or the downbeat a ritardando is expanding toward -- still sounds.
            self._message = "Carrying the score until the next piano entrance"
        self._transport.position = coast
        self._run_scheduler(coast, AccompanimentMode.FOLLOW, now)
        return self.status

    def _complete_at_score_end(self, tempo: TempoState) -> bool:
        """Turn follower/coast overshoot at the final barline into completion."""

        score_end = self.section_map.sections[-1].end_beat
        if tempo.score_beat < score_end:
            return False
        self._transport.position = replace(tempo, score_beat=score_end)
        self._message = "Reached the end of the score"
        self.stop("score_end")
        return True

    def _score_expects_fresh_evidence(self, score_beat: float) -> bool:
        """Whether silence here may legitimately expire into dropout HOLD.

        A sustained piano note or written rest supplies no new localization
        evidence. Holding merely because wall time elapsed cuts off orchestral
        cues that the pianist is waiting to hear. Only an ACTIVE expectation
        cell can make continued silence anomalous; callers without symbolic
        expectation retain the conservative legacy behavior.
        """

        return (
            self.expectation is None
            or self.expectation.role_at(score_beat) is CellRole.ACTIVE
        )

    def _coast_budget_seconds(self, anchor: TempoState) -> float:
        """Tempo-relative silence budget before a coast may expire into dropout.

        A fixed wall-clock window is wrong at the tempo extremes. Live at the
        m.45 climax the pianist decelerated to 27 BPM, where one beat lasts
        ~2.2 s, so the old fixed 1.5 s coast declared a dropout on a single beat
        of notated rubato and panicked the orchestra at the very downbeat the
        ritardando was expanding toward. The budget is ``follower_coast_beats``
        at the current beat period, floored by ``follower_coast_ms`` so fast
        passages still react quickly and capped by ``follower_coast_max_ms`` so a
        genuine stop still resolves promptly.
        """

        period = anchor.beat_period_seconds
        if period is None or period <= 0:
            period = 60.0 / max(self._lead_tempo_bpm, 1e-6)
        budget = self.config.follower_coast_beats * period
        floor = self.config.follower_coast_ms / 1000.0
        cap = self.config.follower_coast_max_ms / 1000.0
        return min(cap, max(floor, budget))

    def _coast_should_hold(self, now: float, anchor: TempoState) -> bool:
        """Only *genuine* silence past the tempo budget becomes a dropout hold.

        Two clocks must both run dry: the last confident follower position and
        the last performer onset. While notes keep arriving the matcher is merely
        unsure -- dense or chromatic writing sits below the lock threshold -- and
        that is FOLLOW's reactive case, not a dropout. Cutting the orchestra there
        (observed live through m.53-63 and the m.105 chromatics) is exactly the
        overreaction Decision 0015 forbids, so keep coasting instead.
        """

        budget = self._coast_budget_seconds(anchor)
        if now - anchor.perf_time <= budget:
            return False
        if self._last_input_at is not None and now - self._last_input_at <= budget:
            return False
        return True

    def _coast_slowdown_engaged_at(self, anchor: TempoState) -> float | None:
        """Wall-clock instant the graceful slowdown engages this coast, or None.

        Slowdown eases the orchestra into a real gap where an attack is due -- a
        ritardando or a breath into a live note -- never during an authored rest
        or sustain, where the orchestra leads at its own pace. It is gated on the
        fixed *anchor* cell (the last confident position, constant for the whole
        silence), not the moving projected beat: a stable engage instant lets the
        coast tempo turn down CONTINUOUSLY instead of stepping the score position
        backward the moment it turns on. It ignores ordinary note-to-note
        spacing -- only silence longer than one beat counts.
        """

        if self._last_input_at is None:
            return None
        if not self._score_expects_fresh_evidence(anchor.score_beat):
            return None
        period = anchor.beat_period_seconds
        if period is None or period <= 0:
            period = 60.0 / max(self._lead_tempo_bpm, 1e-6)
        engaged_at = self._last_input_at + period
        return engaged_at if engaged_at >= anchor.perf_time else None

    def _project_coast_beat(
        self, anchor: TempoState, elapsed: float
    ) -> tuple[float, float | None]:
        """Project the coast position ``elapsed`` seconds past a confident anchor.

        Returns ``(score_beat, reference_beat)`` on the shared reference clock
        when the anchor carries one, else a plain canonical extrapolation. Pulled
        out so the graceful slowdown can price its own un-slowed position before
        deciding whether to expand the tempo.
        """

        reference_beat = anchor.reference_beat
        if reference_beat is not None and anchor.reference_beat_period_seconds is not None:
            reference_beat = reference_beat + elapsed / anchor.reference_beat_period_seconds
            projected = self.scheduler.score_beat_at_reference_beat(reference_beat)
            score_beat = projected if projected is not None else anchor.score_beat
        else:
            score_beat = anchor.score_beat + elapsed / anchor.beat_period_seconds
        return score_beat, reference_beat

    def _tick_dispatch_only(self, now: float) -> RuntimeStatus:
        """Keep the committed plan flowing without changing transport state."""

        self._run_scheduler(self._transport.position, self._transport.mode, now)
        return self.status

    def set_lead_tempo(self, tempo_bpm: float) -> RuntimeStatus:
        """Apply a performer tempo change without restarting the live run.

        FOLLOW tempo remains evidence-driven from the pianist. During LEAD the
        current shared-reference location is re-anchored at ``now`` so future
        orchestral events retime immediately without replaying or cutting the
        note that is already sounding.
        """

        if tempo_bpm <= 0:
            raise ValueError("tempo_bpm must be positive")
        now = self.clock.now()
        self._lead_tempo_bpm = tempo_bpm
        self.trace_sink.write(
            ControlTrace(
                type="control",
                monotonic_time=now,
                control="orchestra_tempo_bpm",
                requested_value=tempo_bpm,
                applied_value=self._lead_tempo_bpm,
                section_mode=self._transport.mode,
            )
        )
        if self._transport.authority in {
            TransportAuthority.LEAD,
            TransportAuthority.ORCHESTRA_ENTRY,
        }:
            orchestra_entry = self._transport.authority is TransportAuthority.ORCHESTRA_ENTRY
            current = (
                self._orchestra_lead_in_state_at(now)
                if orchestra_entry
                else self._lead_state_at(now)
            )
            section = self._transport.section or self.section_map.section_at(current.score_beat)
            remaining_start = min(
                current.score_beat,
                section.end_beat - 1e-9,
            )
            reference_period = self.scheduler.reference_period_for_score_tempo(
                start_score_beat=remaining_start,
                end_score_beat=section.end_beat,
                score_tempo_bpm=tempo_bpm,
            )
            cancelled = self.scheduler.reanchor("performer_tempo_changed")
            self._last_planned_event_ids = ()
            self._transport.clock_anchor = self._state_on_reference_clock(
                perf_time=now,
                score_beat=current.score_beat,
                confidence=1.0,
                reference_beat=current.reference_beat,
                reference_beat_period_seconds=reference_period,
                fallback_beat_period=60.0 / tempo_bpm,
            )
            self._transport.position = self._transport.clock_anchor
            if cancelled:
                self.trace_sink.write(
                    SchedulerTrace(
                        type="scheduler",
                        monotonic_time=now,
                        planned_event_ids=[],
                        dispatched_event_ids=[],
                        cancelled_event_ids=list(cancelled),
                        authority_generation=self.scheduler.authority_generation,
                        authority_reason="performer_tempo_changed",
                    )
                )
            self._write_tempo_policy(
                self._transport.clock_anchor,
                AccompanimentMode.LEAD,
                now,
                previous_mode=AccompanimentMode.LEAD,
                transition_reason="performer_tempo_changed",
            )
            self._run_scheduler(
                self._transport.clock_anchor,
                AccompanimentMode.LEAD,
                now,
            )
        return self.status

    def set_orchestra_volume(self, volume: float) -> RuntimeStatus:
        """Apply the global orchestra/piano balance on the output boundary."""

        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        now = self.clock.now()
        self.output.set_master_volume(volume, sent_at=now)
        self._orchestra_volume = volume
        self.trace_sink.write(
            ControlTrace(
                type="control",
                monotonic_time=now,
                control="orchestra_volume",
                requested_value=volume,
                applied_value=self._orchestra_volume,
                section_mode=self._transport.mode,
            )
        )
        return self.status

    def set_output_advance(self, output_advance_ms: float) -> RuntimeStatus:
        """Retune the repeatable Yamaha output-advance mid-run (by-ear calibration).

        Bounded by the scheduler's dispatch horizon so a committed event is
        always transferred to the deadline worker before its advanced deadline;
        beyond that the scheduler would have to widen its freeze window too. It
        compensates a stable output-path delay and cannot repair tracking drift.
        """

        if output_advance_ms < 0:
            raise ValueError("output_advance_ms must be non-negative")
        if output_advance_ms > self.config.dispatch_horizon_ms:
            raise ValueError(
                "output_advance_ms cannot exceed the dispatch horizon "
                f"({self.config.dispatch_horizon_ms} ms)"
            )
        now = self.clock.now()
        self.output.set_output_advance(output_advance_ms)
        self._output_advance_ms = output_advance_ms
        self.trace_sink.write(
            ControlTrace(
                type="control",
                monotonic_time=now,
                control="orchestra_output_advance_ms",
                requested_value=output_advance_ms,
                applied_value=self._output_advance_ms,
                section_mode=self._transport.mode,
            )
        )
        return self.status

    def stop(self, reason: str = "user_stop") -> RuntimeStatus:
        if self._cadenza is not None:
            # Every run that had a region says what became of it. Without this a
            # follower that never got near the cadenza looks exactly like a run
            # with no region declared, and the trace cannot tell them apart.
            state = self._cadenza.state
            if state is CadenzaState.IDLE or state is CadenzaState.ARMED:
                position = self._transport.position
                self.trace_sink.write(
                    FreeRegionTrace(
                        type="free_region",
                        monotonic_time=self.clock.now(),
                        region_id=self._cadenza_region_id,
                        event="not_armed" if state is CadenzaState.IDLE else "gave_up",
                        score_beat=position.score_beat if position else None,
                        notes_observed=self._cadenza_notes,
                        detail=(
                            f"run ended with the region {state.value}; last followed beat "
                            f"{position.score_beat:.1f}" if position else
                            f"run ended with the region {state.value}; follower never locked"
                        ),
                    )
                )
        if self._phase in {RunPhase.COMPLETED, RunPhase.FAILED}:
            return self.status
        self._transition(RunPhase.STOPPING)
        now = self.clock.now()
        cancelled = self.scheduler.cancel(now=now, output=self.output, reason=reason)
        self._transport.authority = TransportAuthority.STOP
        self._last_planned_event_ids = ()
        self.trace_sink.write(
            SchedulerTrace(
                type="scheduler",
                monotonic_time=now,
                planned_event_ids=[],
                dispatched_event_ids=[],
                cancelled_event_ids=list(cancelled),
                authority_generation=self.scheduler.authority_generation,
                authority_reason=f"cancel:{reason}",
                panic_reason=reason,
            )
        )
        self._transition(RunPhase.COMPLETED)
        return self.status

    def fail(self, reason: str) -> RuntimeStatus:
        if self._phase not in {RunPhase.COMPLETED, RunPhase.FAILED}:
            now = self.clock.now()
            cancelled = self.scheduler.cancel(now=now, output=self.output, reason=reason)
            self._transport.authority = TransportAuthority.STOP
            self._last_planned_event_ids = ()
            self._message = reason
            self.trace_sink.write(
                SchedulerTrace(
                    type="scheduler",
                    monotonic_time=now,
                    planned_event_ids=[],
                    dispatched_event_ids=[],
                    cancelled_event_ids=list(cancelled),
                    authority_generation=self.scheduler.authority_generation,
                    authority_reason=f"cancel:{reason}",
                    panic_reason=reason,
                )
            )
            self._transition(RunPhase.FAILED)
        return self.status

    def _run_scheduler(
        self,
        tempo: TempoState,
        mode: AccompanimentMode | None,
        now: float,
        *,
        hold_panic: bool = True,
    ) -> None:
        result = self.scheduler.update(
            tempo,
            now=now,
            output=self.output,
            mode=mode,
            planning_end_beat=(
                self._transport.section.end_beat
                if mode is AccompanimentMode.LEAD and self._transport.section is not None
                else None
            ),
            hold_panic=hold_panic,
        )
        planned_ids = tuple(item.event.event_id for item in result.planned)
        if not (
            planned_ids != self._last_planned_event_ids
            or result.dispatched
            or result.cancelled_event_ids
            or result.expired_event_ids
            or result.authority_reason
            or result.reset_reason
            or result.panic_reason
        ):
            return
        self._last_planned_event_ids = planned_ids
        self.trace_sink.write(
            SchedulerTrace(
                type="scheduler",
                monotonic_time=now,
                planned_event_ids=list(planned_ids),
                dispatched_event_ids=[item.event.event_id for item in result.dispatched],
                cancelled_event_ids=list(result.cancelled_event_ids),
                expired_event_ids=list(result.expired_event_ids),
                authority_generation=result.authority_generation,
                arrival_curve_id=self.scheduler.arrival_curve_id,
                authority_reason=result.authority_reason,
                reset_reason=result.reset_reason,
                planned_events=[self._scheduled_trace(item) for item in result.planned],
                dispatched_events=[
                    self._scheduled_trace(item, sent_at=now) for item in result.dispatched
                ],
                panic_reason=result.panic_reason,
            )
        )

    def _write_tempo_policy(
        self,
        tempo: TempoState,
        mode: AccompanimentMode,
        now: float,
        *,
        observation: TempoObservation | None = None,
        previous_mode: AccompanimentMode | None = None,
        transition_reason: str | None = None,
        observation_decision: str | None = None,
    ) -> None:
        self.trace_sink.write(
            TempoTrace(
                type="tempo",
                monotonic_time=now,
                perf_time=tempo.perf_time,
                score_beat=tempo.score_beat,
                reference_beat=tempo.reference_beat,
                reference_beat_period_seconds=tempo.reference_beat_period_seconds,
                beat_period_seconds=tempo.beat_period_seconds,
                tempo_bpm=tempo.tempo_bpm,
                confidence=tempo.confidence,
                observation_accepted=(observation.accepted if observation else None),
                observation_decision=(
                    observation.decision
                    if observation
                    else observation_decision or "autonomous_lead"
                ),
                beat_delta=observation.beat_delta if observation else None,
                time_delta=observation.time_delta if observation else None,
                candidate_tempo_bpm=(observation.candidate_tempo_bpm if observation else None),
            )
        )
        try:
            section_id = self.section_map.section_at(tempo.score_beat).id
        except ValueError:
            section_id = None
        self.trace_sink.write(
            PolicyTrace(
                type="policy",
                monotonic_time=now,
                score_beat=tempo.score_beat,
                section_mode=mode,
                section_id=section_id,
                previous_section_mode=previous_mode,
                transition_reason=transition_reason,
            )
        )

    def _begin_lead(
        self,
        section: Section,
        anchor: TempoState,
        now: float,
        *,
        reason: str,
        anchor_perf_time: float | None = None,
    ) -> None:
        tempo_bpm = section.tempo_bpm or self._lead_tempo_bpm
        score_beat = max(section.start_beat, anchor.score_beat)
        reference_beat = self.scheduler.reference_beat_at_score_beat(score_beat)
        if reference_beat is None:
            reference_beat = anchor.reference_beat
        reference_period = self.scheduler.reference_period_for_score_tempo(
            start_score_beat=score_beat,
            end_score_beat=section.end_beat,
            score_tempo_bpm=tempo_bpm,
        )
        lead = self._state_on_reference_clock(
            perf_time=now if anchor_perf_time is None else anchor_perf_time,
            score_beat=score_beat,
            confidence=1.0,
            reference_beat=reference_beat,
            reference_beat_period_seconds=reference_period,
            fallback_beat_period=60.0 / tempo_bpm,
        )
        previous_mode = self._transport.mode
        self._transport = _TransportState(
            authority=TransportAuthority.LEAD,
            position=lead,
            clock_anchor=lead,
            section=section,
            section_end_reference_beat=self.scheduler.reference_beat_at_score_beat(
                section.end_beat
            ),
        )
        self._message = None
        self.tempo_model.reset_timing_reference()
        cancelled = self.scheduler.resume()
        if cancelled:
            self._last_planned_event_ids = ()
            self.trace_sink.write(
                SchedulerTrace(
                    type="scheduler",
                    monotonic_time=now,
                    planned_event_ids=[],
                    dispatched_event_ids=[],
                    cancelled_event_ids=list(cancelled),
                    authority_generation=self.scheduler.authority_generation,
                    authority_reason="resume",
                )
            )
        self._write_tempo_policy(
            lead,
            AccompanimentMode.LEAD,
            now,
            previous_mode=previous_mode,
            transition_reason=reason,
        )
        self._run_scheduler(lead, AccompanimentMode.LEAD, now)

    def _finish_lead(self, now: float, *, await_follow: bool) -> None:
        assert self._transport.section is not None
        end_beat = self._transport.section.end_beat
        reentry = self._transport.reentry_candidate
        previous_mode = self._transport.mode
        final_position = self._transport.position
        cancelled = self.scheduler.pause(score_beat=end_beat)
        self._last_planned_event_ids = ()
        self._transport = _TransportState(
            authority=(
                TransportAuthority.HOLD_AWAIT_ENTRY if await_follow else TransportAuthority.FOLLOW
            ),
            position=final_position,
            expected_entry_beat=end_beat if await_follow else None,
        )
        self.tempo_model.reset_timing_reference()
        if await_follow:
            self.trace_sink.write(
                PolicyTrace(
                    type="policy",
                    monotonic_time=now,
                    score_beat=end_beat,
                    section_mode=AccompanimentMode.HOLD,
                    previous_section_mode=previous_mode,
                    transition_reason="lead_section_complete_waiting_for_solo",
                )
            )
        else:
            # ``pause`` currently only clears future plans, but making the
            # immediate handoff symmetric keeps this transition correct if the
            # scheduler later gains an explicit pause latch.
            self.scheduler.resume()
        self.trace_sink.write(
            SchedulerTrace(
                type="scheduler",
                monotonic_time=now,
                planned_event_ids=[],
                dispatched_event_ids=[],
                cancelled_event_ids=list(cancelled),
                authority_generation=self.scheduler.authority_generation,
                authority_reason="pause",
            )
        )
        if await_follow and reentry is not None:
            reference_beat = reentry.reference_beat
            if reference_beat is None:
                reference_beat = self.scheduler.reference_beat_at_score_beat(reentry.score_beat)
            buffered = FollowerUpdate(
                perf_time=reentry.perf_time,
                score_beat=reentry.score_beat,
                confidence=max(
                    reentry.confidence,
                    self.config.minimum_follower_confidence,
                ),
                reference_beat=reference_beat,
                raw_state={
                    **reentry.raw_state,
                    "handoff": "buffered_early_reentry",
                    "observed_score_beat": reentry.score_beat,
                    "observed_perf_time": reentry.perf_time,
                    "consumed_at": now,
                },
            )
            tempo = self.tempo_model.update(buffered)
            self._transport.authority = TransportAuthority.FOLLOW
            self._transport.expected_entry_beat = None
            self.scheduler.resume()
            self._transport.position = tempo
            self._write_tempo_policy(
                tempo,
                AccompanimentMode.FOLLOW,
                now,
                observation=self.tempo_model.last_observation,
                previous_mode=AccompanimentMode.HOLD,
                transition_reason="buffered_early_solo_reentry",
            )
            self._run_scheduler(tempo, AccompanimentMode.FOLLOW, now)

    def _is_expected_entry_candidate(
        self,
        update: FollowerUpdate,
        expected_beat: float | None,
    ) -> bool:
        if expected_beat is None or not (
            expected_beat - 0.5 <= update.score_beat <= expected_beat + 1.0
        ):
            return False
        if update.confidence >= self.config.minimum_follower_confidence:
            return True
        # Matchmaker confidence is deliberately uncalibrated. Inside a narrow,
        # authored entry window, two monotonic symbolic estimates are enough
        # to seed the handoff instead of paying its normal three-update lock
        # delay. Outside this window the ordinary follower threshold remains.
        return (
            update.raw_state.get("follower") == "matchmaker"
            and int(update.raw_state.get("stable_update_count", 0)) >= 2
        )

    def _lead_state_at(self, now: float) -> TempoState:
        assert self._transport.clock_anchor is not None
        assert self._transport.section is not None
        lead = self._transport.clock_anchor
        if now <= lead.perf_time:
            # A selected LEAD entry may be seeded during the final count-off
            # beat. Keep that future downbeat as the timing origin: publishing
            # status before entry must not retime its score events backward to
            # each control-loop tick.
            return lead
        reference_beat = lead.reference_beat
        score_beat: float
        elapsed = now - lead.perf_time
        if reference_beat is not None and lead.reference_beat_period_seconds is not None:
            reference_beat += elapsed / lead.reference_beat_period_seconds
            if self._transport.section_end_reference_beat is not None:
                reference_beat = min(
                    reference_beat,
                    self._transport.section_end_reference_beat,
                )
            projected = self.scheduler.score_beat_at_reference_beat(reference_beat)
            score_beat = projected if projected is not None else lead.score_beat
        else:
            score_beat = lead.score_beat + (elapsed / lead.beat_period_seconds)
        score_beat = min(score_beat, self._transport.section.end_beat)
        return self._state_on_reference_clock(
            perf_time=now,
            score_beat=score_beat,
            confidence=1.0,
            reference_beat=reference_beat,
            reference_beat_period_seconds=lead.reference_beat_period_seconds,
        )

    def _abandon_entry_acquisition(
        self,
        acquisition: EntryPaceAcquisition,
        freeze: TempoState,
        update: FollowerUpdate,
        now: float,
        input_at: float,
    ) -> RuntimeStatus:
        """Leave a stalled cue-in on a rehearsed pace instead of holding forever.

        This is a recovery, not a measurement: the pace comes from the
        rehearsal profile at this score position when takes cover it (so a
        pianist who has rehearsed this passage rejoins at *their* tempo, not a
        generic pulse), and from the orchestra's own lead-in period otherwise.
        """

        period = self._lead_in_period_at(freeze.score_beat, freeze.beat_period_seconds)
        rehearsed = (
            self._pace_profile is not None
            and self._pace_profile.trusted_period(freeze.score_beat) is not None
        )
        target_beat = max(update.score_beat, freeze.score_beat)
        slope = 1.0 / period
        reference_slope: float | None = None
        reference_intercept: float | None = None
        if freeze.reference_beat is not None and freeze.reference_beat_period_seconds:
            reference_slope = 1.0 / freeze.reference_beat_period_seconds
            reference_target = max(
                update.reference_beat
                if update.reference_beat is not None
                else freeze.reference_beat,
                freeze.reference_beat,
            )
            reference_intercept = reference_target - reference_slope * now
        estimate = EntryPaceEstimate(
            canonical_beats_per_second=slope,
            canonical_intercept_beats=target_beat - slope * now,
            reference_beats_per_second=reference_slope,
            reference_intercept_beats=reference_intercept,
            onset_count=acquisition.onset_count,
            canonical_span_beats=0.0,
            elapsed_seconds=acquisition.elapsed_since_first(now) or 0.0,
        )
        return self._clamp_entry_to_follow(
            estimate,
            update,
            now,
            input_at,
            position_action="orchestra_entry_abandoned",
            extra_state={
                "abandon_reason": "no_fittable_pace_within_ceiling",
                "abandon_after_seconds": acquisition.elapsed_since_first(now),
                "recovered_pace_source": "rehearsal_profile" if rehearsed else "orchestra_lead_in",
                "recovered_tempo_bpm": 60.0 / period,
            },
        )

    def _clamp_entry_to_follow(
        self,
        estimate: EntryPaceEstimate,
        update: FollowerUpdate,
        now: float,
        input_at: float,
        *,
        position_action: str = "orchestra_entry_handoff",
        extra_state: dict[str, object] | None = None,
    ) -> RuntimeStatus:
        """Hand the cue-in to FOLLOW using a pace fit to the pianist alone.

        This is a hard clamp, not a smoothed update: ``seed_timing`` overwrites
        the tempo model's period and anchor with the piano-derived estimate so
        the pre-entry orchestra seed leaves no residue. Phase comes from the fit
        evaluated at ``now`` (floored at the frozen orchestra beat so transport
        stays monotonic), so the orchestra joins at the soloist's current
        position and pace in a single step rather than chasing it.
        """

        freeze = self._transport.entry_freeze
        pace_state: dict[str, object] = {}
        rehearsed_period = (
            None
            if freeze is None or self._pace_profile is None
            else self._pace_profile.trusted_period(freeze.score_beat)
        )
        if rehearsed_period is not None and position_action == "orchestra_entry_handoff":
            # A fit inside ``min_tempo_bpm``/``max_tempo_bpm`` can still be
            # nonsense: live take ``performance-20260731T024748Z-1dfb`` handed
            # off at 23 BPM against a ~50 BPM passage because the follower was
            # crawling, and the whole take then ran on that pace.
            #
            # The comparison is *only* against a rehearsed pace. The orchestra's
            # lead-in pulse is deliberately not a reference here: it is the
            # warp-inflated seed this handoff exists to escape, and a pianist may
            # legitimately enter far from it at cold start. Prior takes are the
            # one honest expectation of how fast this passage actually goes.
            assert freeze is not None
            expected_period = rehearsed_period
            ratio = estimate.canonical_beat_period_seconds / expected_period
            if not _PLAUSIBLE_ENTRY_PACE_RATIO[0] <= ratio <= _PLAUSIBLE_ENTRY_PACE_RATIO[1]:
                held_beat = estimate.canonical_beat_at(now)
                slope = 1.0 / expected_period
                pace_state = {
                    "entry_pace_rejected_bpm": estimate.tempo_bpm,
                    "entry_pace_substituted_bpm": 60.0 / expected_period,
                    "entry_pace_source": "rehearsal_profile",
                }
                # Keep the pianist's measured *position*; replace only the pace.
                estimate = replace(
                    estimate,
                    canonical_beats_per_second=slope,
                    canonical_intercept_beats=held_beat - slope * now,
                )
        canonical_beat = estimate.canonical_beat_at(now)
        reference_beat = estimate.reference_beat_at(now)
        if freeze is not None:
            canonical_beat = max(canonical_beat, freeze.score_beat)
            if reference_beat is not None and freeze.reference_beat is not None:
                reference_beat = max(reference_beat, freeze.reference_beat)
        seeded = self.tempo_model.seed_timing(
            TimingSeed(
                perf_time=now,
                score_beat=canonical_beat,
                beat_period_seconds=estimate.canonical_beat_period_seconds,
                reference_beat=reference_beat,
                reference_beat_period_seconds=estimate.reference_beat_period_seconds,
            )
        )
        tempo = replace(seeded, confidence=update.confidence, coasting=False)
        self.trace_sink.write(
            FollowerTrace(
                type="follower",
                monotonic_time=now,
                perf_time=update.perf_time,
                score_beat=tempo.score_beat,
                raw_score_beat=update.score_beat,
                reference_beat=update.reference_beat,
                confidence=update.confidence,
                position_action=position_action,
                processing_latency_ms=(now - input_at) * 1000,
                follower_state={
                    **update.raw_state,
                    "entry_onset_count": estimate.onset_count,
                    "entry_span_beats": estimate.canonical_span_beats,
                    "entry_pace_bpm": estimate.tempo_bpm,
                    **pace_state,
                    **(extra_state or {}),
                },
            )
        )
        self._transport = _TransportState(
            authority=TransportAuthority.FOLLOW,
            position=tempo,
        )
        self._message = None
        self._write_tempo_policy(
            tempo,
            AccompanimentMode.FOLLOW,
            now,
            previous_mode=AccompanimentMode.LEAD,
            transition_reason="orchestra_entry_matched",
            observation_decision="orchestra_entry_piano_seed",
        )
        self._run_scheduler(tempo, AccompanimentMode.FOLLOW, now)
        return self.status

    def _handle_uncertain_follow_update(
        self, update: FollowerUpdate, now: float, input_at: float
    ) -> RuntimeStatus:
        """Coast briefly from the last accepted clock; never learn from noise."""

        anchor = self._transport.clock_anchor
        if anchor is None and self._transport.position is not None:
            if self._transport.authority is TransportAuthority.FOLLOW:
                anchor = self._transport.position
                self._transport.authority = TransportAuthority.COAST
                self._transport.clock_anchor = anchor
                self.tempo_model.reset_timing_reference()
        if anchor is None:
            self.trace_sink.write(
                FollowerTrace(
                    type="follower",
                    monotonic_time=now,
                    perf_time=update.perf_time,
                    score_beat=(
                        self._transport.position.score_beat
                        if self._transport.position is not None
                        else update.score_beat
                    ),
                    raw_score_beat=update.score_beat,
                    reference_beat=update.reference_beat,
                    confidence=update.confidence,
                    position_action="dropout_hold",
                    processing_latency_ms=(now - input_at) * 1000,
                    follower_state=update.raw_state,
                )
            )
            return self.status

        cue_entry = self._transport.authority is TransportAuthority.CUE_ENTRY
        coast = self._follow_coast_state_at(now, confidence=update.confidence)
        self.trace_sink.write(
            FollowerTrace(
                type="follower",
                monotonic_time=now,
                perf_time=update.perf_time,
                score_beat=coast.score_beat,
                raw_score_beat=update.score_beat,
                reference_beat=update.reference_beat,
                confidence=update.confidence,
                position_action=(
                    "count_off_entry_not_yet_locked" if cue_entry else "coast_from_confident_anchor"
                ),
                processing_latency_ms=(now - input_at) * 1000,
                follower_state=update.raw_state,
            )
        )
        if cue_entry:
            if now - anchor.perf_time > self.config.follower_coast_ms / 1000.0:
                self._enter_entry_hold(coast, now)
                return self.status
        elif self._score_expects_fresh_evidence(coast.score_beat) and self._coast_should_hold(
            now, anchor
        ):
            # A low-confidence note *did* arrive, so ``_last_input_at`` is fresh
            # and this normally keeps coasting: an uncertain onset is the pianist
            # playing, not a dropout. Only genuine silence past the tempo budget
            # (reached via the tick path with no notes at all) lands here.
            self._enter_dropout_hold(coast, now)
            return self.status
        section = self.section_map.section_at(coast.score_beat)
        if section.mode is AccompanimentMode.LEAD:
            self._begin_lead(
                section,
                coast,
                now,
                reason=(
                    "count_off_cue_reached_authored_lead_section"
                    if cue_entry
                    else "coast_reached_authored_lead_section"
                ),
            )
            return self.status
        previous_mode = self._transport.mode
        self._transport.position = coast
        self._transport.authority = (
            TransportAuthority.CUE_ENTRY if cue_entry else TransportAuthority.COAST
        )
        self._message = (
            "Following the count-off cue — waiting for score follower lock"
            if cue_entry
            else "Tracker uncertain — coasting from the last confident beat"
        )
        self._write_tempo_policy(
            coast,
            AccompanimentMode.FOLLOW,
            now,
            previous_mode=previous_mode,
            transition_reason=(
                "count_off_entry_not_yet_locked"
                if cue_entry
                else "temporary_follower_dropout_coast"
            ),
            observation_decision=("count_off_timing_seed" if cue_entry else "follower_coast"),
        )
        if self._phase is RunPhase.ACTIVE:
            self._run_scheduler(coast, AccompanimentMode.FOLLOW, now)
        return self.status

    def _orchestra_lead_in_state_at(self, now: float) -> TempoState:
        """Play the pre-entry orchestra on one uniformly scaled source clock.

        With no pianist yet, Oguri is the performance. Canonical position is
        projected for display and entry matching; it does not retime the audio
        beat by beat, and rehearsal pace does not reshape autonomous playback.
        """

        anchor = self._transport.clock_anchor
        assert anchor is not None
        if now <= anchor.perf_time:
            return replace(anchor, confidence=0.0, coasting=True)
        reference_beat = anchor.reference_beat
        if reference_beat is not None and anchor.reference_beat_period_seconds is not None:
            reference_beat += (now - anchor.perf_time) / anchor.reference_beat_period_seconds
            projected = self.scheduler.score_beat_at_reference_beat(reference_beat)
            score_beat = projected if projected is not None else anchor.score_beat
        else:
            score_beat = anchor.score_beat + (
                (now - anchor.perf_time) / anchor.beat_period_seconds
            )
        return self._state_on_reference_clock(
            perf_time=now,
            score_beat=score_beat,
            confidence=0.0,
            reference_beat=reference_beat,
            reference_beat_period_seconds=anchor.reference_beat_period_seconds,
            coasting=True,
            fallback_beat_period=anchor.beat_period_seconds,
        )

    def _lead_in_period_at(self, score_beat: float, nominal_period: float) -> float:
        """Rehearsed seconds-per-beat here, or the nominal pulse at cold start."""

        if self._pace_profile is None:
            return nominal_period
        period = self._pace_profile.trusted_period(score_beat)
        return period if period is not None else nominal_period

    def _follow_coast_state_at(self, now: float, *, confidence: float = 0.0) -> TempoState:
        assert self._transport.clock_anchor is not None
        anchor = self._transport.clock_anchor
        if now <= anchor.perf_time:
            # A selected FOLLOW entry is seeded during the last count-off
            # quarter. Preserve the future downbeat exactly so control ticks
            # cannot walk the score position backward before entry.
            return replace(anchor, confidence=confidence, coasting=True)
        # Graceful degradation: once the performer has left a real gap where an
        # attack is due, expand the coast tempo so the orchestra eases into the
        # silence instead of marching ahead. Applied PIECEWISE -- full rate up to
        # the engage instant, a slower rate after -- so the projected position is
        # continuous and monotonic and never steps backward the moment slowdown
        # turns on (a naive retroactive scale of the whole elapsed did, jumping
        # the cursor back ~0.16 beat; caught in adversarial review).
        elapsed = now - anchor.perf_time
        engaged_at = self._coast_slowdown_engaged_at(anchor)
        if engaged_at is not None and now > engaged_at:
            elapsed = (engaged_at - anchor.perf_time) + (now - engaged_at) / (
                1.0 + self.config.follower_coast_slowdown
            )
        score_beat, reference_beat = self._project_coast_beat(anchor, elapsed)
        return self._state_on_reference_clock(
            perf_time=now,
            score_beat=score_beat,
            confidence=confidence,
            reference_beat=reference_beat,
            reference_beat_period_seconds=anchor.reference_beat_period_seconds,
            coasting=True,
            fallback_beat_period=anchor.beat_period_seconds,
        )

    def _enter_dropout_hold(self, coast: TempoState, now: float) -> None:
        if self._transport.authority is TransportAuthority.HOLD_DROPOUT:
            return
        previous_mode = self._transport.mode
        self._transport = _TransportState(
            authority=TransportAuthority.HOLD_DROPOUT,
            position=coast,
        )
        self._message = "Waiting for the score follower to relock"
        self._write_tempo_policy(
            coast,
            AccompanimentMode.HOLD,
            now,
            previous_mode=previous_mode,
            transition_reason="follower_dropout_coast_expired",
            observation_decision="follower_coast",
        )
        # A dropout hold is a *gentle* stop: the pianist fell silent where sound
        # was due, but any orchestra chord still ringing should release on its own
        # scheduled note-off, not be slammed off by a blunt all-notes-off. Only a
        # true STOP panics. This is the cut Eric heard chop the m.45 downbeat.
        self._run_scheduler(coast, AccompanimentMode.HOLD, now, hold_panic=False)

    def _enter_entry_hold(self, cue_state: TempoState, now: float) -> None:
        """Stop a count-off-led entry that never acquired piano evidence."""

        previous_mode = self._transport.mode
        self._transport = _TransportState(
            authority=TransportAuthority.HOLD_DROPOUT,
            position=cue_state,
        )
        self._message = "Waiting for the pianist to enter and the score follower to lock"
        self._write_tempo_policy(
            cue_state,
            AccompanimentMode.HOLD,
            now,
            previous_mode=previous_mode,
            transition_reason="count_off_entry_grace_expired",
            observation_decision="count_off_timing_seed",
        )
        self._run_scheduler(cue_state, AccompanimentMode.HOLD, now)

    def _state_on_reference_clock(
        self,
        *,
        perf_time: float,
        score_beat: float,
        confidence: float,
        reference_beat: float | None,
        reference_beat_period_seconds: float | None,
        coasting: bool = False,
        fallback_beat_period: float | None = None,
    ) -> TempoState:
        beat_period = fallback_beat_period or 60.0 / self._lead_tempo_bpm
        if reference_beat is not None and reference_beat_period_seconds is not None:
            beat_period = self.scheduler.score_beat_period_at_reference_beat(
                reference_beat, reference_beat_period_seconds
            )
        return TempoState(
            perf_time=perf_time,
            score_beat=score_beat,
            beat_period_seconds=beat_period,
            tempo_bpm=60.0 / beat_period,
            confidence=confidence,
            reference_beat=reference_beat,
            reference_beat_period_seconds=reference_beat_period_seconds,
            coasting=coasting,
        )

    @staticmethod
    def _scheduled_trace(item, *, sent_at: float | None = None) -> ScheduledEventTrace:
        return ScheduledEventTrace(
            event_id=item.event.event_id,
            score_beat=item.event.beat,
            pitch=item.event.pitch,
            part_id=item.event.part_id,
            duration_beats=item.event.duration_beats,
            target_perf_time=item.perf_time,
            reference_beat=item.reference_beat,
            duration_seconds=item.duration_seconds,
            section_mode=item.section_mode,
            authority_generation=item.authority_generation,
            committed_at=(sent_at if sent_at is not None else item.committed_at),
            sent_at=sent_at,
            lateness_ms=((sent_at - item.perf_time) * 1000 if sent_at is not None else None),
        )

    def _transition(self, target: RunPhase) -> None:
        require_run_phase_transition(self._phase, target)
        self._phase = target
        self.trace_sink.write(StateTrace(type="state", status=self.status))
