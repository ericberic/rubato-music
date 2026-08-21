"""Tests for the real profile fold (roadmap item 4).

Verifies `cell_samples` are resampled from a take's actual MIDI (velocity,
timing, pedal) rather than the old constant-64/zero-spread placeholders, and
that `fit_interpretation_for` fuses multiple takes with a quality-weighted recency
median, per docs/design/REHEARSAL_TAKE_COVERAGE_DESIGN.md §2.3/§2.5.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick

from aimusic.takes import aligner, profile, store

REPO_ROOT = Path(__file__).resolve().parents[2]
SOLO_REFERENCE_PATH = (
    REPO_ROOT / "data/scores/chopin_op11_movement_2/derived/solo_reference.mid"
)


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))


@pytest.fixture(scope="module")
def reference() -> tuple[aligner.ChordNote, ...]:
    return aligner.load_canonical_note_sequence(
        SOLO_REFERENCE_PATH, track_name_contains=("PIANO SOLO",)
    )


def _write_take_midi(
    path: Path,
    notes: list[aligner.ChordNote],
    *,
    velocity: int = 100,
    pedal_depth: float | None = 0.8,
    ticks_per_beat: int = 480,
    tempo_bpm: int = 120,
) -> None:
    """Write a synthetic take with a distinct (non-default) velocity and a
    pedal held down throughout, so the fold has real, checkable data.
    """

    midi = MidiFile(ticks_per_beat=ticks_per_beat)
    track = MidiTrack()
    midi.tracks.append(track)
    tempo = bpm2tempo(tempo_bpm)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    if pedal_depth is not None:
        track.append(
            Message("control_change", control=64, value=round(pedal_depth * 127), time=0)
        )

    start_seconds = notes[0].time_seconds
    last_time = 0.0
    for note in notes:
        elapsed = note.time_seconds - start_seconds
        delta = max(0.0, elapsed - last_time)
        delta_ticks = int(round(second2tick(delta, ticks_per_beat, tempo)))
        track.append(Message("note_on", note=note.pitch, velocity=velocity, time=delta_ticks))
        track.append(Message("note_off", note=note.pitch, velocity=0, time=ticks_per_beat // 8))
        last_time = elapsed

    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def _write_two_track_take_midi(
    path: Path,
    notes: list[aligner.ChordNote],
    *,
    velocity: int = 100,
    pedal_depth: float = 0.8,
    ticks_per_beat: int = 480,
    tempo_bpm: int = 120,
) -> None:
    """Genuine Type 1 layout: tempo lives on its own track (track 0), notes
    and pedal on a separate track (track 1) -- the shape that broke naive
    per-track tempo tracking.
    """

    midi = MidiFile(ticks_per_beat=ticks_per_beat, type=1)
    tempo_track = MidiTrack()
    midi.tracks.append(tempo_track)
    tempo = bpm2tempo(tempo_bpm)
    tempo_track.append(MetaMessage("set_tempo", tempo=tempo, time=0))

    note_track = MidiTrack()
    midi.tracks.append(note_track)
    note_track.append(
        Message("control_change", control=64, value=round(pedal_depth * 127), time=0)
    )

    start_seconds = notes[0].time_seconds
    last_time = 0.0
    for note in notes:
        elapsed = note.time_seconds - start_seconds
        delta = max(0.0, elapsed - last_time)
        delta_ticks = int(round(second2tick(delta, ticks_per_beat, tempo)))
        note_track.append(
            Message("note_on", note=note.pitch, velocity=velocity, time=delta_ticks)
        )
        note_track.append(
            Message("note_off", note=note.pitch, velocity=0, time=ticks_per_beat // 8)
        )
        last_time = elapsed

    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def test_cell_samples_correct_when_tempo_is_on_a_separate_track(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Regression for the Gemini-flagged bug: tracking `tempo` locally per
    track silently ignores tempo set on a different track, mis-timing every
    note. A two-track take (tempo on track 0, notes on track 1) must produce
    the same cell_samples as the equivalent single-track take.
    """

    start_position = 550
    notes = list(reference[start_position : start_position + 25])
    # 120bpm (500000us/beat) is mido's own fallback default -- a bug that
    # never updates `tempo` from its initial value would coincidentally look
    # correct there. Use a tempo that isn't the fallback.
    non_default_tempo_bpm = 76

    single_track_path = tmp_path / "single.mid"
    _write_take_midi(
        single_track_path, notes, velocity=95, pedal_depth=0.5, tempo_bpm=non_default_tempo_bpm
    )
    two_track_path = tmp_path / "two.mid"
    _write_two_track_take_midi(
        two_track_path, notes, velocity=95, pedal_depth=0.5, tempo_bpm=non_default_tempo_bpm
    )

    single = aligner.align_take("single-track", single_track_path, SOLO_REFERENCE_PATH)
    two = aligner.align_take("two-track", two_track_path, SOLO_REFERENCE_PATH)

    assert two.cell_samples
    assert len(two.cell_samples) == len(single.cell_samples)
    for a, b in zip(single.cell_samples, two.cell_samples):
        assert a.beat == pytest.approx(b.beat)
        assert a.velocity == pytest.approx(b.velocity)
        assert a.pedal == pytest.approx(b.pedal, abs=0.01)
        assert a.period_s == pytest.approx(b.period_s, abs=1e-6)


def test_compute_cell_samples_uses_real_velocity_and_pedal(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    rng = random.Random(1)
    start_position = 500
    notes = [reference[start_position]] + [
        n for n in reference[start_position + 1 : start_position + 30] if rng.random() >= 0.1
    ]

    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes, velocity=101, pedal_depth=0.6)

    aligned = aligner.align_take("real-take", take_midi_path, SOLO_REFERENCE_PATH)

    assert aligned.cell_samples, "expected real cell_samples, got none"
    velocities = [s.velocity for s in aligned.cell_samples]
    pedals = [s.pedal for s in aligned.cell_samples]
    periods = [s.period_s for s in aligned.cell_samples]

    # Real note velocity (101), not the old hard-coded 64.0 placeholder.
    assert all(v == pytest.approx(101.0) for v in velocities)
    # Real pedal depth (0.6 held throughout), not the old hard-coded 0.0.
    # abs tolerance covers CC64's 7-bit (0-127) quantization of 0.6.
    assert all(p == pytest.approx(0.6, abs=0.01) for p in pedals)
    # Real local tempo derived from the timing map, not a flat default.
    assert all(p > 0.0 for p in periods)
    assert aligned.edge_trim_beats == (
        profile.EDGE_TRIM_HEAD_BEATS,
        profile.EDGE_TRIM_TAIL_BEATS,
    )


def test_edge_cells_are_quality_scaled_not_excluded(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    rng = random.Random(2)
    start_position = 600
    notes = [reference[start_position]] + [
        n for n in reference[start_position + 1 : start_position + 40] if rng.random() >= 0.1
    ]
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes, velocity=90)

    aligned = aligner.align_take("edge-take", take_midi_path, SOLO_REFERENCE_PATH)
    assert aligned.cell_samples

    head_cutoff = aligned.score_start_beat + profile.EDGE_TRIM_HEAD_BEATS
    tail_cutoff = aligned.score_end_beat - profile.EDGE_TRIM_TAIL_BEATS
    interior = [
        s for s in aligned.cell_samples if head_cutoff <= s.beat < tail_cutoff
    ]
    edge = [s for s in aligned.cell_samples if s.beat < head_cutoff]
    assert interior and edge
    assert max(s.quality for s in edge) < min(s.quality for s in interior)
    assert all(s.quality > 0.0 for s in edge)  # scaled down, not zeroed out


def test_cell_grid_is_absolute_not_relative_to_each_takes_start(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Two takes of an overlapping span that start on *different* notes must
    still land on the same absolute 0.5-beat grid cells in the overlap --
    otherwise their cell_samples can never be fused by `fit_interpretation_for`.
    """

    start_position = 500
    take_a_notes = list(reference[start_position : start_position + 30])
    take_b_notes = list(reference[start_position + 10 : start_position + 40])

    take_a_path = tmp_path / "a.mid"
    _write_take_midi(take_a_path, take_a_notes, velocity=70, pedal_depth=None)
    take_b_path = tmp_path / "b.mid"
    _write_take_midi(take_b_path, take_b_notes, velocity=70, pedal_depth=None)

    aligned_a = aligner.align_take("grid-a", take_a_path, SOLO_REFERENCE_PATH)
    aligned_b = aligner.align_take("grid-b", take_b_path, SOLO_REFERENCE_PATH)

    assert aligned_a.score_start_beat != aligned_b.score_start_beat

    beats_a = {round(s.beat / 0.5) * 0.5 for s in aligned_a.cell_samples}
    beats_b = {round(s.beat / 0.5) * 0.5 for s in aligned_b.cell_samples}
    # Every emitted beat should already be exactly on the canonical grid.
    assert all(
        s.beat == pytest.approx(round(s.beat / 0.5) * 0.5) for s in aligned_a.cell_samples
    )
    shared = beats_a & beats_b
    assert shared, "overlapping takes starting on different notes must share grid cells"


def test_last_grid_cell_is_not_dropped(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Regression for the Gemini-flagged off-by-one: `while cell < grid_end`
    silently discarded the cell exactly at the take's grid-rounded end beat.
    """

    start_position = 620
    notes = list(reference[start_position : start_position + 20])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes, velocity=88, pedal_depth=None)

    aligned = aligner.align_take("last-cell-take", take_midi_path, SOLO_REFERENCE_PATH)
    assert aligned.cell_samples

    grid_end = round(aligned.score_end_beat / profile.GRID_BEATS) * profile.GRID_BEATS
    sample_beats = {s.beat for s in aligned.cell_samples}
    assert any(abs(beat - grid_end) < 1e-9 for beat in sample_beats), (
        f"expected a sample at the final grid cell {grid_end}, got {sorted(sample_beats)}"
    )


def test_fit_interpretation_for_medians_across_overlapping_takes(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Three takes of the same passage at velocities 40/60/80 should fold to
    a median of 60 per cell (design doc §2.5: outvoted, not averaged in) --
    not the old flat 64.0 placeholder, and not a naive mean either.
    """

    rng = random.Random(3)
    start_position = 700
    notes = [reference[start_position]] + [
        n for n in reference[start_position + 1 : start_position + 30] if rng.random() >= 0.1
    ]

    take_ids = []
    for suffix, velocity, recorded_at in (
        ("a", 40, "2026-07-09T20:00:00Z"),
        ("b", 60, "2026-07-09T20:05:00Z"),
        ("c", 80, "2026-07-09T20:10:00Z"),
    ):
        take_path = tmp_path / f"{suffix}.mid"
        _write_take_midi(take_path, notes, velocity=velocity)
        take = store.save_take("chopin_op11", 2, take_path, recorded_at=recorded_at)
        aligner.align_and_store("chopin_op11", 2, take.take_id)
        assert store.get_take("chopin_op11", 2, take.take_id).status == store.ALIGNED
        take_ids.append(take.take_id)

    folded = profile.fit_interpretation_for("chopin_op11", 2)
    assert folded.take_count == 3
    assert folded.schema_version == 2
    assert folded.coordinate_system == "canonical_score"
    assert folded.grid_step_ticks == 480
    assert folded.cells, "expected fused canonical performance cells"

    cells_with_all_three = [c for c in folded.cells if c.support == 3]
    assert cells_with_all_three, "expected at least one cell covered by all three takes"
    for cell in cells_with_all_three:
        assert cell.velocity == pytest.approx(60.0)
        assert cell.velocity_mad > 0.0  # real spread, not the old flat 0.0
