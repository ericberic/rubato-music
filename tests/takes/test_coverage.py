"""Tests for the coverage computation and measure rollup (roadmap item 4/5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mido import Message, MetaMessage, MidiFile, MidiTrack

from aimusic.core import paths
from aimusic.server.app import create_app
from aimusic.takes import coverage, store
from aimusic.takes.models import AlignedResult, CellSample
from tests.oguri_guard import requires_oguri_derived


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))


def _write_dummy_midi(path: Path) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=240))
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def _write_span_aligned_result(
    piece_id: str,
    movement: int,
    take_id: str,
    *,
    score_start_beat: float,
    score_end_beat: float,
    match_rate: float,
    cell_samples: tuple[CellSample, ...] = (),
) -> None:
    """A minimal, schema-valid `aligned.json` covering just the span/quality
    fields these coverage tests exercise -- the rest are required fields on
    `AlignedResult` with no bearing on the rollup being tested.
    """

    store.write_aligned_result(
        piece_id,
        movement,
        take_id,
        AlignedResult(
            take_id=take_id,
            aligner="seeded-local-v1",
            score_start_beat=score_start_beat,
            score_end_beat=score_end_beat,
            match_rate=match_rate,
            ambiguous=False,
            matched_notes=0,
            extra_notes=0,
            missing_notes=0,
            timing_map=(),
            candidates=(),
            cell_samples=cell_samples,
            edge_trim_beats=(1.0, 0.5),
        ),
    )


def test_coverage_computes_from_aligned_takes(tmp_path: Path) -> None:
    piece_id = "chopin_op11"
    movement = 1

    # Save three dummy takes
    dummy_midi = tmp_path / "dummy.mid"
    _write_dummy_midi(dummy_midi)

    # Take 1: span 417.0 to 421.0
    t1 = store.save_take(piece_id, movement, dummy_midi, recorded_at="2026-07-09T20:00:00Z")
    store.update_take_status(piece_id, movement, t1.take_id, store.ALIGNED)
    _write_span_aligned_result(
        piece_id, movement, t1.take_id, score_start_beat=417.0, score_end_beat=421.0, match_rate=0.9
    )

    # Take 2: span 420.0 to 424.0 (overlaps with Take 1)
    t2 = store.save_take(piece_id, movement, dummy_midi, recorded_at="2026-07-09T20:01:00Z")
    store.update_take_status(piece_id, movement, t2.take_id, store.ALIGNED)
    _write_span_aligned_result(
        piece_id,
        movement,
        t2.take_id,
        score_start_beat=420.0,
        score_end_beat=424.0,
        match_rate=0.85,
    )

    # Take 3: span 430.0 to 433.0 (disjoint)
    t3 = store.save_take(piece_id, movement, dummy_midi, recorded_at="2026-07-09T20:02:00Z")
    store.update_take_status(piece_id, movement, t3.take_id, store.ALIGNED)
    _write_span_aligned_result(
        piece_id,
        movement,
        t3.take_id,
        score_start_beat=430.0,
        score_end_beat=433.0,
        match_rate=0.95,
    )

    # Compute coverage
    cov = coverage.compute_coverage(piece_id, movement, n_target=2)

    assert cov.piece_id == piece_id
    assert cov.movement == movement
    assert cov.n_target == 2
    assert cov.computed_at is not None

    # Verify that data files are cached
    profile_dir = paths.data_root() / "profiles" / piece_id / str(movement)
    assert (profile_dir / "profile.json").exists()
    assert (profile_dir / "coverage.json").exists()

    measures = cov.measures
    assert len(measures) == 690  # from score.mxl

    # Find measure 139 (first solo measure, beats 417.0 to 420.0)
    # Take 1 covers beats 417.0, 417.5, 418.0, 418.5, 419.0, 419.5, 420.0, 420.5, 421.0
    # So every grid beat in [417.0, 420.0) has n=1 (from Take 1).
    # Since n_target=2, it should be "touched" (since n=1 >= 1).
    m139 = next(m for m in measures if m.measure == 139)
    assert m139.solo is True
    assert m139.min_n == 1
    assert m139.state == "touched"

    # Measure 140: beats 420.0 to 423.0.
    # Grid beats: 420.0 (T1, T2), 420.5 (T1, T2), 421.0 (T1, T2), 421.5 (T2), 422.0 (T2), 422.5 (T2)
    # So beats 420.0, 420.5, 421.0 have n=2. Beats 421.5, 422.0, 422.5 have n=1.
    # Therefore, min_n should be 1. State should be "touched".
    m140 = next(m for m in measures if m.measure == 140)
    assert m140.min_n == 1
    assert m140.state == "touched"

    # Now let's add Take 4 covering 420.0 to 423.0 to make Measure 140 "covered" (min_n >= 2)
    t4 = store.save_take(piece_id, movement, dummy_midi)
    store.update_take_status(piece_id, movement, t4.take_id, store.ALIGNED)
    _write_span_aligned_result(
        piece_id, movement, t4.take_id, score_start_beat=420.0, score_end_beat=423.0, match_rate=0.9
    )

    cov2 = coverage.compute_coverage(piece_id, movement, n_target=2)
    m140_new = next(m for m in cov2.measures if m.measure == 140)
    assert m140_new.min_n >= 2
    assert m140_new.state == "covered"

    summary = cov2.summary
    assert summary.solo_measures == 459
    assert summary.percent_covered > 0.0


def test_parse_midi_measures_raises_when_no_track_matches_marker(tmp_path: Path) -> None:
    """A marker that matches no track would otherwise silently produce every
    measure as solo=False (all tutti, a confusing "everything's grayed out"
    UI) -- must fail loudly instead.
    """

    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("track_name", name="Orchestra", time=0))
    track.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=480))

    midi_path = tmp_path / "no_match.mid"
    midi.save(midi_path)

    with pytest.raises(ValueError, match="No track name matched"):
        coverage.parse_midi_measures(midi_path, track_name_contains=("PIANO SOLO",))


def test_parse_midi_measures_rejects_non_positive_ticks_per_beat(tmp_path: Path) -> None:
    """SMPTE-timecode-divided MIDI files can carry a negative ticks_per_beat,
    and a corrupt file could carry zero; every elapsed_ticks/ticks_per_beat
    computation in this module assumes a positive quarter-note tick rate.
    Zero would raise ZeroDivisionError deep in the parse; must fail with a
    clear error up front instead.
    """

    midi = MidiFile(ticks_per_beat=0)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("track_name", name="PIANO SOLO", time=0))
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=1))

    midi_path = tmp_path / "zero_ticks_per_beat.mid"
    midi.save(midi_path)

    with pytest.raises(ValueError, match="ticks_per_beat"):
        coverage.parse_midi_measures(midi_path)


def test_parse_midi_measures_defaults_to_four_four_without_time_signature(
    tmp_path: Path,
) -> None:
    """MIDI spec: absence of a time_signature meta message means 4/4 -- not
    an error, and not some other arbitrary assumption.
    """

    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("track_name", name="PIANO SOLO", time=0))
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=480 * 8))

    midi_path = tmp_path / "no_time_sig.mid"
    midi.save(midi_path)

    measures = coverage.parse_midi_measures(midi_path)
    assert measures[0]["end_beat"] - measures[0]["start_beat"] == 4.0


def test_beats_per_measure_guards_against_zero_numerator_or_denominator() -> None:
    """A malformed time_signature with numerator=0 (or denominator=0) would
    make beats_per_measure exactly 0.0, which hangs `parse_midi_measures`'
    walk loop forever *and* unboundedly grows `measures_data` in memory --
    confirmed by actually triggering it once (killed the runaway process at
    ~900MB and climbing) before extracting this guard into a directly
    testable pure function. That's deliberate: a bug this destructive isn't
    safe to reproduce via execution even under a timeout, since the memory
    growth happens regardless of how the test waits for it.
    """

    assert coverage._beats_per_measure(4, 4) == 4.0
    assert coverage._beats_per_measure(3, 4) == 3.0
    assert coverage._beats_per_measure(0, 4) == 4.0
    assert coverage._beats_per_measure(4, 0) == 4.0
    assert coverage._beats_per_measure(0, 0) == 4.0


def test_beats_per_measure_guards_against_underflow_to_near_zero() -> None:
    """A merely huge (but positive) denominator -- not just a non-positive
    one -- can underflow beats_per_measure to a vanishingly small positive
    value, which is just as much a memory-exhaustion DoS as exactly 0.0: the
    measure-walk loop would still run for a practically unbounded number of
    iterations. A denominator of 2**20 with numerator=1 is a legal (if
    absurd) MIDI byte-derived value that would produce ~4e-6 beats/measure
    without this guard.
    """

    assert coverage._beats_per_measure(1, 2**20) == 4.0
    # A genuinely small-but-legitimate subdivision must still pass through.
    assert coverage._beats_per_measure(1, 64) == pytest.approx(4.0 / 64)


def test_parse_midi_measures_defaults_leading_region_to_four_four(tmp_path: Path) -> None:
    """If the first explicit time_signature change occurs after beat 0 (here,
    3/4 starting at beat 8), the leading region before it must default to
    4/4 rather than retroactively inheriting that later signature.
    """

    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("track_name", name="PIANO SOLO", time=0))
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=480))
    track.append(MetaMessage("time_signature", numerator=3, denominator=4, time=480 * 7))
    track.append(Message("note_on", note=61, velocity=64, time=0))
    track.append(Message("note_off", note=61, velocity=0, time=480 * 4))

    midi_path = tmp_path / "late_time_sig.mid"
    midi.save(midi_path)

    measures = coverage.parse_midi_measures(midi_path)
    assert measures[0]["start_beat"] == 0.0
    assert measures[0]["end_beat"] == 4.0
    assert measures[1]["end_beat"] == 8.0
    m_at_8 = next(m for m in measures if m["start_beat"] == 8.0)
    assert m_at_8["end_beat"] - m_at_8["start_beat"] == 3.0  # the real 3/4 change


def test_rollup_measure_rounds_non_grid_aligned_boundaries() -> None:
    """Regression for a Gemini-flagged gap: a measure boundary that isn't an
    exact 0.5-beat multiple (e.g. from an unusual time signature like 7/16)
    must still round to the nearest 0.5 grid point to find real cell_map
    entries, rather than silently missing them.
    """

    m_data = {"measure": 1, "start_beat": 10.24, "end_beat": 11.76, "solo": True}
    cell_map = {
        10.0: {"n": 3, "qualities": [0.9, 0.9, 0.9]},
        10.5: {"n": 3, "qualities": [0.9, 0.9, 0.9]},
        11.0: {"n": 3, "qualities": [0.9, 0.9, 0.9]},
        11.5: {"n": 3, "qualities": [0.9, 0.9, 0.9]},
    }
    result = coverage._rollup_measure(m_data, cell_map, n_target=3)
    assert result.min_n == 3
    assert result.min_quality == 0.9
    assert result.state == "covered"


def test_rollup_measure_explains_quality_shortfall_after_count_target() -> None:
    """Amber can mean weak alignment evidence even after three passes.

    Preserve the weakest half-beat quality so the UI can name the actual
    blocker instead of incorrectly asking for a fourth pass because it thinks
    the observation count is below target.
    """

    m_data = {"measure": 23, "start_beat": 88.0, "end_beat": 89.0, "solo": True}
    cell_map = {
        88.0: {"n": 3, "qualities": [0.9, 0.9, 0.9]},
        88.5: {"n": 3, "qualities": [0.4, 0.4, 0.4]},
    }

    result = coverage._rollup_measure(m_data, cell_map, n_target=3)

    assert result.min_n == 3
    assert result.min_quality == 0.4
    assert result.mean_quality == 0.65
    assert result.state == "touched"


def test_rollup_measure_short_measure_still_finds_a_cell() -> None:
    """Rounding both m_start and m_end to the *nearest* 0.5 grid point could
    put them on the same value for a short measure (or one that doesn't
    cross a 0.25/0.75 boundary) even though it genuinely overlaps a real
    grid cell -- an empty cell_beats, not just a coarser lookup. Flooring
    m_start instead finds the real overlapping cell.
    """

    m_data = {"measure": 1, "start_beat": 10.3, "end_beat": 10.4, "solo": True}
    cell_map = {10.0: {"n": 5, "qualities": [0.9, 0.9, 0.9, 0.9, 0.9]}}
    result = coverage._rollup_measure(m_data, cell_map, n_target=3)
    assert result.min_n == 5
    assert result.state == "covered"


def test_rollup_measure_empty_cell_list_is_uncovered_not_covered() -> None:
    """all([]) is vacuously True in Python -- without an explicit guard, a
    measure with zero cell_stats (min_n=0, no data at all) would compute
    all_covered=True and report state="covered", directly contradicting its
    own min_n=0. A zero-duration measure sitting exactly on a grid point
    (start_beat == end_beat == 10.5) is the case that still produces a
    genuinely empty cell_beats even with the floor-based walk above --
    isolates this guard from the short-measure fix.
    """

    m_data = {"measure": 1, "start_beat": 10.5, "end_beat": 10.5, "solo": True}
    result = coverage._rollup_measure(m_data, cell_map={}, n_target=3)
    assert result.min_n == 0
    assert result.state == "uncovered"


@requires_oguri_derived
def test_parse_midi_measures_derives_real_time_signature_and_solo_status() -> None:
    """The Oguri movement-2 MIDI is a single 4/4 movement of 1332 beats
    (=333 source-grid groups of 4 MIDI beats), with an orchestral intro/interludes
    where the piano is tacet -- not the old hard-coded "119 measures of 2
    beats, always solo" fallback.
    """

    reference_path = (
        Path(__file__).resolve().parents[2]
        / "assets/scores/chopin_op11_ii_larghetto/derived/solo_reference.mid"
    )
    measures = coverage.parse_midi_measures(reference_path)

    assert len(measures) == 333
    assert measures[0]["start_beat"] == 0.0
    assert measures[0]["end_beat"] == 4.0  # 4/4 time -> 4 beats/measure
    assert measures[-1]["end_beat"] == 1332.0

    # Measure numbers are contiguous and every measure is exactly 4 beats
    # (a single time signature covers the whole movement).
    for i, m in enumerate(measures, start=1):
        assert m["measure"] == i
        assert m["end_beat"] - m["start_beat"] == 4.0

    # The piece opens with an orchestral introduction before the piano
    # enters (first solo note at beat ~144.25, i.e. measure 37) -- these
    # early measures must be tutti, not solo.
    assert all(not m["solo"] for m in measures[:36])
    assert measures[36]["solo"] is True  # measure 37, piano's entrance

    solo_measures = [m for m in measures if m["solo"]]
    tutti_measures = [m for m in measures if not m["solo"]]
    assert len(solo_measures) == 284
    assert len(tutti_measures) == 49


@requires_oguri_derived
def test_coverage_movement_2_uses_real_measure_map() -> None:
    piece_id = "chopin_op11"
    movement = 2

    cov = coverage.compute_coverage(piece_id, movement)
    assert len(cov.measures) == 126
    # Beat fusion substantiates measures 1--125. The solo reference is active
    # from m.12; only the still-unmapped final m.126 remains unknown rather than
    # becoming a fabricated rehearsal target.
    #
    # 109, not 114: solo-led means a solo *line*, not merely the presence of a
    # solo note (Decision 0015). Five bars hold a note or two while the orchestra
    # leads, and they can never be "covered" by rehearsing -- so demanding it kept
    # them permanently uncertain:
    #     m.12  (1 onset)  the solo pickup at the end of the introduction
    #     m.22  (2)        orchestral interlude
    #     m.52  (2), m.53 (1)  orchestral continuation
    #     m.104 (2)        orchestral interlude
    # These match `sections.json`'s authored LEAD regions exactly, from a wholly
    # independent derivation.
    assert cov.summary.solo_measures == 109
    assert cov.measures[51].scope == "tutti", "m.52 is orchestra-led"
    assert cov.measures[21].scope == "tutti", "m.22 interlude is orchestra-led"
    assert all(cov.measures[index - 1].solo for index in range(54, 58))
    assert all(cov.measures[index - 1].scope == "solo" for index in range(113, 126))
    assert cov.measures[125].scope == "unknown"
    assert cov.summary.percent_covered == 0.0
    assert cov.summary.observable_measures == 109
    assert cov.summary.observed_measures == 0
    assert cov.summary.percent_observed == 0.0
    assert cov.mapping_review_state == "machine"
    assert cov.canonical_positions is True

    # The PDF provides exact page/measure geometry while the declared Oguri
    # solo-reference track independently marks machine-projected piano
    # activity. The coordinates are canonical while evidence review remains
    # machine-labelled.
    m1 = next(m for m in cov.measures if m.measure == 1)
    assert m1.solo is False
    assert m1.state == "tutti"
    assert m1.scope == "tutti"
    assert m1.observed is False
    # m.12 holds exactly one solo onset -- the pickup into m.13 -- while the
    # orchestra finishes the introduction. Solo-led means a solo *line*, so this
    # bar is orchestra-led and asks nothing of the performer (Decision 0015).
    # Marking it solo demanded coverage that no amount of rehearsing could give.
    m12 = next(m for m in cov.measures if m.measure == 12)
    assert m12.solo is False
    assert m12.scope == "tutti"
    m13 = next(m for m in cov.measures if m.measure == 13)
    assert m13.solo is True, "the phrase proper begins at m.13"
    assert m13.scope == "solo"
    assert m13.state == "uncovered"
    m16 = next(m for m in cov.measures if m.measure == 16)
    assert m16.accompaniment_required is False
    m34 = next(m for m in cov.measures if m.measure == 34)
    assert m34.accompaniment_required is True


@requires_oguri_derived
def test_movement_2_projects_one_take_as_observed_not_canonically_covered(
    tmp_path: Path,
) -> None:
    piece_id = "chopin_op11"
    movement = 2
    dummy_midi = tmp_path / "real-span.mid"
    _write_dummy_midi(dummy_midi)
    take = store.save_take(piece_id, movement, dummy_midi)
    store.update_take_status(piece_id, movement, take.take_id, store.ALIGNED)
    _write_span_aligned_result(
        piece_id,
        movement,
        take.take_id,
        score_start_beat=144.245,
        score_end_beat=232.387,
        match_rate=0.86,
        cell_samples=tuple(
            CellSample(
                beat=beat / 2,
                period_s=0.5,
                velocity=64,
                pedal=0.5,
                quality=0.86,
            )
            for beat in range(288, 466)
        ),
    )

    cov = coverage.compute_coverage(piece_id, movement)

    assert cov.summary.covered == 0
    assert cov.summary.percent_covered == 0.0
    assert cov.summary.observed_measures > 0
    assert cov.summary.percent_observed > 0.0
    observed = [measure for measure in cov.measures if measure.observed]
    assert observed
    assert observed[0].measure == 12
    assert observed[-1].measure == 22
    # The take's two endpoint bars are precisely the orchestra-led ones: m.12
    # carries only the pickup, m.22 is the interlude. Observing playing there is
    # real, but neither bar is a rehearsal target.
    assert {measure.measure for measure in observed if measure.scope == "tutti"} == {12, 22}
    assert all(measure.state in {"touched", "uncovered", "tutti"} for measure in observed)
    assert any(measure.state == "touched" for measure in observed)


def test_coverage_api_route(tmp_path: Path) -> None:
    client = TestClient(create_app())

    # Mock isolated path in app instance if routes read paths
    import os

    os.environ["AIMUSIC_DATA_ROOT"] = str(tmp_path / "data")
    os.environ["AIMUSIC_RUNS_ROOT"] = str(tmp_path / "runs")

    response = client.get("/api/coverage/1?piece_id=chopin_op11")
    assert response.status_code == 200
    body = response.json()
    assert body["piece_id"] == "chopin_op11"
    assert body["movement"] == 1
    assert body["measures"] != []
    assert body["summary"]["solo_measures"] == 459
