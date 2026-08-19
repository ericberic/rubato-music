"""Pin the score's expectation against known bars of Movement II.

These tests exist because getting this wrong is *silent*. The reference MIDI is
at PPQ 240 and the canonical timeline at PPQ 960; bucketing one against the
other produces a confident-looking answer that is off by a factor of four. That
mistake was actually made during the Decision 0015 investigation -- it reported
that the piano plays from m.3, when the piano's first note of the movement is at
canonical beat 47.0 -- so the guard is a regression test, not a hypothetical.

Ground truth for Chopin Op.11 movement II, independently confirmed from three
places that agree exactly:
  - ``derived/solo_reference.mid``: first onset at reference tick 34619 (PPQ 240)
  - ``sections.json``: ``opening-orchestra-lead`` spans beats 0.0 -> 47.0
  - a recorded take's alignment: canonical tick 45120 <-> reference tick 138476
    (= 34619 x 4), i.e. canonical beat 47.0
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimusic.accompaniment.expectation import (
    CellRole,
    ExpectationModel,
    missing_ratio,
)
from aimusic.accompaniment.runtime_projection import (
    MOVEMENT_2_BUNDLE_ID,
    default_bundle_registry,
    project_bundle_v2_to_provisional_runtime,
)
from aimusic.accompaniment.following import OracleFollower, PerformedNote
from aimusic.accompaniment.live_engine import LiveEngine
from aimusic.accompaniment.runtime_contracts import LiveStateWord, RuntimeConfig
from aimusic.accompaniment.runtime_io import (
    CapturingOutput,
    ManualClock,
    MemoryTraceSink,
)
from aimusic.accompaniment.section_policy import AccompanimentMode

# The piano's first note of the movement: m.12 beat 4, Eric's B-natural pickup.
FIRST_SOLO_BEAT = 47.0


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


@pytest.fixture(scope="module")
def model() -> ExpectationModel:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    return ExpectationModel.from_bundle(projection.bundle)


def test_nothing_expected_means_nothing_missing() -> None:
    """The load-bearing guard, tested without any bundle at all.

    ``1 - observed / max(expected, 1)`` would return 1.0 here and report a total
    miss for a passage performed exactly as written.
    """

    assert missing_ratio(0, 0) == 0.0
    assert missing_ratio(0, 3) == 0.0  # extra notes are not a deficit either


def test_missing_ratio_is_relative_to_expectation() -> None:
    assert missing_ratio(4, 0) == 1.0
    assert missing_ratio(4, 2) == 0.5
    assert missing_ratio(4, 4) == 0.0
    assert missing_ratio(4, 9) == 0.0  # clamped, never negative


def test_opening_is_tacet_until_the_piano_enters(model: ExpectationModel) -> None:
    """m.1 through m.12 beat 4 is an orchestral introduction.

    This is the assertion that fails loudly if the PPQ mix-up returns.
    """

    assert model.is_tacet_through(0.0, FIRST_SOLO_BEAT)
    assert model.role_at(0.0) is CellRole.TACET
    assert model.role_at(FIRST_SOLO_BEAT - 0.5) is CellRole.TACET
    assert model.expected_onsets_in(0.0, FIRST_SOLO_BEAT) == 0


def test_first_solo_onset_is_canonical_beat_47(model: ExpectationModel) -> None:
    assert model.first_solo_beat() == pytest.approx(FIRST_SOLO_BEAT)
    entry = model.cell_at(FIRST_SOLO_BEAT)
    assert entry.expected_onsets > 0
    assert entry.role is not CellRole.TACET


def test_solo_material_after_the_entry_is_not_tacet(model: ExpectationModel) -> None:
    # m.13-m.17: the phrase Eric plays. Dense solo writing throughout.
    assert not model.is_tacet_through(48.0, 68.0)
    assert model.expected_onsets_in(48.0, 68.0) > 0


def test_authority_follows_the_section_map_not_the_evidence(
    model: ExpectationModel,
) -> None:
    """Authority is a statement about the music, so it is knowable a priori."""

    # opening-orchestra-lead: beats 0 -> 47
    assert model.authority_at(0.0) is AccompanimentMode.LEAD
    assert model.authority_at(40.0) is AccompanimentMode.LEAD
    # solo-follow-1: beats 47 -> 84
    assert model.authority_at(FIRST_SOLO_BEAT) is AccompanimentMode.FOLLOW
    assert model.authority_at(60.0) is AccompanimentMode.FOLLOW
    # m22-orchestra-interlude: beats 84 -> 88. The bar Eric expected to hear and
    # heard nothing, because a support count decided the orchestra should quit.
    assert model.authority_at(84.0) is AccompanimentMode.LEAD
    assert model.authority_at(87.5) is AccompanimentMode.LEAD
    # solo-follow-2 resumes
    assert model.authority_at(88.0) is AccompanimentMode.FOLLOW


def test_orchestral_interlude_asks_nothing_of_the_performer(
    model: ExpectationModel,
) -> None:
    """m.22 is orchestra-led; readiness must not demand takes for it (#149).

    The interlude is LEAD authority, which is the part that matters for who owns
    timing. Whether the solo part is literally silent for all four beats is a
    separate question -- assert only that the accompanist leads and that any
    expectation there is not treated as a rehearsal gap.
    """

    cells = [cell for cell in model.cells if 84.0 <= cell.start_beat < 88.0]
    assert cells
    assert all(cell.authority is AccompanimentMode.LEAD for cell in cells)
    for cell in cells:
        if cell.role is CellRole.TACET:
            assert missing_ratio(cell.expected_onsets, 0) == 0.0


def test_cells_tile_the_movement_without_gaps(model: ExpectationModel) -> None:
    cells = model.cells
    assert cells[0].start_beat == 0.0
    for previous, current in zip(cells, cells[1:], strict=False):
        assert current.start_beat == pytest.approx(previous.end_beat)


def test_sustained_notes_count_as_the_performer_playing(
    model: ExpectationModel,
) -> None:
    """A held chord is the performer playing, not the performer absent.

    Cells with no onset but a note sounding through must be SPARSE, never TACET,
    or the follower would hand authority back mid-phrase.
    """

    sustained = [
        cell
        for cell in model.cells
        if cell.expected_onsets == 0 and cell.sounding
    ]
    assert sustained, "movement has no sustained-through cells; check duration parsing"
    assert all(cell.role is CellRole.SPARSE for cell in sustained)


def test_follow_hands_back_at_a_tacet_interlude_without_coasting_first() -> None:
    """m.22: the performer stops because the score says to, and the orchestra leads.

    This is Eric's report ("I was expecting the orchestra to play that m.22
    interlude and I heard nothing") reproduced against the real bundle. Before
    Decision 0015, FOLLOW never re-checked the section map, so an authored LEAD
    interlude was reached only after `follower_coast_ms` elapsed -- announcing
    "Tracker uncertain -- coasting" and hesitating, for a passage where the
    performer had done exactly what was written.
    """

    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    bundle = projection.bundle
    model = ExpectationModel.from_bundle(bundle)
    # Sanity-check the premise before asserting on behaviour.
    assert model.authority_at(85.0) is AccompanimentMode.LEAD
    assert model.role_at(85.0) is CellRole.TACET

    clock = ManualClock(100.0)
    output = CapturingOutput()
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="m22-handback",
            initial_tempo_bpm=64,
            follower_coast_ms=1500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=output,
        trace_sink=trace,
    )
    assert engine.expectation is not None

    # The performer's last note of the phrase, just before the interlude at 84.0.
    engine.start(start_beat=83.5, entry_perf_time=100.0)
    engine.process_note(
        PerformedNote(perf_time=100.0, pitch=68, velocity=80, score_beat=83.5)
    )
    engine.tick()
    assert engine.status.state_word is LiveStateWord.FOLLOWING

    # The phrase ends at m.22 and the performer stops, exactly as written. Advance
    # 1.0s -- comfortably under follower_coast_ms (1.5s), so the old coast path
    # cannot be what fires -- which at the lead tempo carries the music past 84.0.
    clock.advance_to(101.0)
    engine.tick()

    reasons = [row.transition_reason for row in trace.records if row.type == "policy"]
    assert "reached_authored_tacet_handback" in reasons, (
        f"no anticipated handback; transitions were {reasons}"
    )
    assert not any(
        reason in {"temporary_follower_dropout_coast", "follower_dropout_coast_expired"}
        for reason in reasons
    ), "tacet silence was treated as a dropout"
    assert "uncertain" not in (engine.status.message or "").lower()


def test_silence_in_solo_material_is_still_a_dropout() -> None:
    """The handback must not swallow real dropouts.

    Same silence, different place in the score: inside solo-follow-1 the performer
    stopping is an anomaly and must still reach the coast/dropout contract.
    """

    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    bundle = projection.bundle
    clock = ManualClock(100.0)
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id="solo-dropout",
            initial_tempo_bpm=64,
            follower_coast_ms=400,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=CapturingOutput(),
        trace_sink=(trace := MemoryTraceSink()),
    )
    # Mid-phrase, well inside solo material and far from any interlude.
    engine.start(start_beat=60.0, entry_perf_time=100.0)
    engine.process_note(
        PerformedNote(perf_time=100.0, pitch=68, velocity=80, score_beat=60.0)
    )
    engine.tick()

    clock.advance_to(102.0)
    engine.tick()
    engine.tick()

    reasons = [row.transition_reason for row in trace.records if row.type == "policy"]
    assert "reached_authored_tacet_handback" not in reasons
    assert any(
        reason in {"temporary_follower_dropout_coast", "follower_dropout_coast_expired"}
        for reason in reasons
    ), f"real dropout was not detected; transitions were {reasons}"


# Recorded takes live in the performer's application-support directory, not the
# repo. `tests/conftest.py` deliberately redirects AIMUSIC_DATA_ROOT so that no
# test can touch or mutate that data, so these replays resolve the real location
# independently and read from it only. They run on the performer's machine, where
# the takes exist, and skip everywhere else -- the same shape as the DVC-gated
# tests that skip when an artifact has not been pulled.
def _recorded_take_dir(take_id: str):
    import os
    import sys

    override = os.environ.get("RUBATO_RECORDED_TAKES_ROOT")
    if override:
        root = Path(override).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Rubato" / "data"
    else:
        pytest.skip("recorded-take replay is only wired for macOS or an explicit root")
    return root / "takes" / "chopin_op11" / "2" / take_id


def _load_take_timing(take_id: str) -> list[tuple[float, float]]:
    import json

    payload = json.loads((_recorded_take_dir(take_id) / "aligned.v2.json").read_text())
    assert payload["coordinate_system"] == "canonical_score"
    rows = [
        (entry["score_tick"] / 960, entry["take_seconds"])
        for entry in payload["timing_map"]
    ]
    # Stored in score order; replay needs wall-clock order, and a chord's notes
    # share a timestamp.
    rows.sort(key=lambda row: (row[1], row[0]))
    monotonic: list[tuple[float, float]] = []
    for beat, seconds in rows:
        if monotonic and seconds < monotonic[-1][1]:
            seconds = monotonic[-1][1]
        monotonic.append((beat, seconds))
    return monotonic


def _replay_take(bundle, take_id: str, *, silence_seconds: float = 4.0):
    notes = _load_take_timing(take_id)
    start_beat, t0 = notes[0]
    clock = ManualClock(0.0)
    trace = MemoryTraceSink()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(
            run_id=f"replay-{take_id}",
            initial_tempo_bpm=64,
            follower_coast_ms=1500,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
        ),
        clock=clock,
        follower=OracleFollower(),
        output=CapturingOutput(),
        trace_sink=trace,
    )
    engine.start(start_beat=start_beat, entry_perf_time=0.0)
    for beat, seconds in notes:
        clock.advance_to(seconds - t0)
        engine.process_note(
            PerformedNote(perf_time=seconds - t0, pitch=60, velocity=80, score_beat=beat)
        )
        engine.tick()

    end = notes[-1][1] - t0
    steps = int(silence_seconds / 0.1)
    for step in range(1, steps + 1):
        clock.advance_to(end + step * 0.1)
        engine.tick()

    reasons = [row.transition_reason for row in trace.records if row.type == "policy"]
    return engine, notes[-1][0], reasons


@pytest.mark.parametrize(
    "take_id",
    [
        "t20260717T033751Z-22a6",
        "t20260729T141306Z-6612",
        "t20260719T224647Z-e131",
        "t20260729T040553Z-b91b",
    ],
)
def test_recorded_takes_ending_at_the_interlude_never_report_a_dropout(
    take_id: str,
) -> None:
    """Replay of real takes: stopping at m.22 is correct playing, not a dropout.

    Every one of Eric's takes over this passage ends at exactly beat 84.0 -- he
    plays to the interlude and stops, because the orchestra takes over there. The
    accompanist must end up leading, and must never classify that silence as the
    performer having dropped out.
    """

    if not (_recorded_take_dir(take_id) / "aligned.v2.json").is_file():
        pytest.skip(f"recorded take {take_id} not present in this environment")

    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    engine, last_beat, reasons = _replay_take(projection.bundle, take_id)

    assert last_beat == pytest.approx(84.0, abs=0.5), (
        "premise changed: this take no longer ends at the m.22 interlude"
    )
    assert not any(
        reason in {"temporary_follower_dropout_coast", "follower_dropout_coast_expired"}
        for reason in reasons
    ), f"tacet interlude treated as a dropout; transitions were {reasons}"
    # Leading the interlude, then waiting for the performer's re-entry at m.23,
    # are both correct resting places.
    assert engine._transport.authority.value in {"lead", "hold_await_entry"}


def test_a_recorded_take_stopped_mid_phrase_is_still_a_dropout() -> None:
    """The counter-case, on real data.

    This take (2026-07-29 19:38) was stopped by hand at m.17.4, in the middle of
    solo material. Identical silence to the interlude case, but here the score
    expected notes -- so it must reach the dropout contract.
    """

    take_id = "t20260729T193838Z-a74f"
    if not (_recorded_take_dir(take_id) / "aligned.v2.json").is_file():
        pytest.skip(f"recorded take {take_id} not present in this environment")

    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    engine, last_beat, reasons = _replay_take(projection.bundle, take_id)

    assert 60.0 < last_beat < 70.0, "premise changed: take no longer ends mid-phrase"
    assert any(
        reason in {"temporary_follower_dropout_coast", "follower_dropout_coast_expired"}
        for reason in reasons
    ), f"stopping mid-phrase was not treated as a dropout; got {reasons}"
    assert "reached_authored_tacet_handback" not in reasons
