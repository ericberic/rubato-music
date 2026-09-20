"""The PTHMM rewrites must not change what the follower reports.

Rubato always replaces Matchmaker's dense transition matvec with a banded
sparse one and its Bernoulli observation with a factorized equivalent. Both are
exact, so there is no slower path to fall back to -- which means equivalence is
pinned here against Matchmaker's *own* implementation, built directly, rather
than via a toggle on the follower.
"""

from __future__ import annotations

from pathlib import Path

import mido
import numpy as np
import pytest

pytest.importorskip("matchmaker", reason="Install with `uv sync --extra live`")

from aimusic.accompaniment.following import PerformedNote
from aimusic.accompaniment.matchmaker_follower import (
    DEFAULT_TRANSITION_BACKWARD_STATES,
    MatchmakerStreamFollower,
    _band_pthmm_transition,
)

pytestmark = pytest.mark.matchmaker

TICKS_PER_BEAT = 480
PITCHES = [60, 62, 64, 65, 67, 69, 71, 72, 71, 69, 67, 65, 64, 62, 60, 59]


@pytest.fixture(scope="module")
def score_midi(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("rewrites") / "score.mid"
    midi = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    for pitch in PITCHES:
        track.append(mido.Message("note_on", note=pitch, velocity=64, time=0))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=TICKS_PER_BEAT))
    midi.save(path)
    return path


def _raw_pthmm(score: Path):
    """A stock Matchmaker PTHMM, untouched by our rewrites."""

    import partitura as pt
    from matchmaker.prob.hmm import PitchHMM

    reference = pt.load_score_midi(str(score)).note_array()
    hmm = PitchHMM(reference_features=reference, has_insertions=True, piano_range=True)
    hmm.transition_model.use_log_probabilities = False
    return hmm


def _observations(hmm) -> list[np.ndarray]:
    vectors = []
    for pitch in PITCHES:
        vector = np.zeros(88)
        vector[max(0, min(87, pitch - 21))] = 1.0
        vectors.append(vector)
    return vectors


def _drive(hmm, observations) -> list[int]:
    return [hmm.forward_algorithm_step(observation=o, log_probabilities=False)
            for o in observations]


def test_banded_transition_reproduces_the_dense_forward_path(score_midi: Path) -> None:
    """Two independent PTHMMs, one banded, driven by the same observations."""

    stock, rewritten = _raw_pthmm(score_midi), _raw_pthmm(score_midi)
    _band_pthmm_transition(rewritten, backward_states=DEFAULT_TRANSITION_BACKWARD_STATES)
    observations = _observations(stock)
    assert _drive(rewritten, observations) == _drive(stock, observations)


def test_banding_reports_what_it_replaced(score_midi: Path) -> None:
    follower = MatchmakerStreamFollower(score_midi)
    try:
        info = follower._transition_banding
    finally:
        follower.close()
    assert info is not None
    assert info["backward_states"] == DEFAULT_TRANSITION_BACKWARD_STATES
    # The matrix's own forward reach is preserved exactly, never truncated.
    assert info["forward_states"] > 0
    assert info["banded_bytes"] < info["dense_bytes"]
    # Truncating only a negligible tail is the whole safety argument.
    assert info["mean_row_mass_dropped"] < 1e-6


def test_factorization_reports_what_it_precomputed(score_midi: Path) -> None:
    follower = MatchmakerStreamFollower(score_midi)
    try:
        info = follower._observation_factorization
    finally:
        follower.close()
    assert info is not None
    assert info["pitch_dimensions"] == 88
    assert info["precomputed_bytes"] > 0


def test_banding_rejects_a_nonpositive_band(score_midi: Path) -> None:
    with pytest.raises(ValueError):
        MatchmakerStreamFollower(score_midi, transition_backward_states=0)


def test_rewritten_follower_matches_a_stock_pthmm_beat_for_beat(score_midi: Path) -> None:
    """End to end: the shipped follower against stock Matchmaker's own path."""

    stock = _raw_pthmm(score_midi)
    expected = [
        float(stock.state_space[index]) for index in _drive(stock, _observations(stock))
    ]
    follower = MatchmakerStreamFollower(score_midi, max_wait_seconds=2.0)
    try:
        got = [
            follower.observe(PerformedNote(perf_time=i * 0.5, pitch=p, velocity=64)).score_beat
            for i, p in enumerate(PITCHES)
        ]
    finally:
        follower.close()
    assert got == expected


def test_rewrites_survive_reposition_and_relocalize(score_midi: Path) -> None:
    """Both rewrites derive from static data, so a prior reset invalidates
    neither -- unlike a windowed observation model, which would."""

    follower = MatchmakerStreamFollower(score_midi, max_wait_seconds=2.0)
    try:
        follower.reposition_for_entry(score_beat=2.0, reference_beat=None)
        assert follower.observe(PerformedNote(perf_time=0.0, pitch=64, velocity=64)) is not None
        follower.relocalize()
        assert follower.observe(PerformedNote(perf_time=0.5, pitch=65, velocity=64)) is not None
    finally:
        follower.close()


def test_banded_recovery_matches_dense_after_a_broad_reseed(score_midi: Path) -> None:
    """Banding must not weaken `relocalize()` relative to Matchmaker's dense reach.

    Previously only factorized-vs-direct observation was compared with the band
    held constant, which left this untested: after a broad re-seed the dense
    matrix can step back up to 744 states in one note and the banded one only
    64, so recovery had to be compared directly.
    """

    from aimusic.accompaniment.matchmaker_follower import _broaden_pthmm_prior

    stock, rewritten = _raw_pthmm(score_midi), _raw_pthmm(score_midi)
    _band_pthmm_transition(rewritten, backward_states=DEFAULT_TRANSITION_BACKWARD_STATES)
    for hmm in (stock, rewritten):
        _broaden_pthmm_prior(hmm, from_beat=None)  # fully uniform: the worst case
    observations = _observations(stock)
    assert _drive(rewritten, observations) == _drive(stock, observations)


def test_underflowing_profiles_are_rejected_not_silently_zeroed() -> None:
    """Dense, high-confidence profiles underflow `prod(1 - P)` where the direct
    expression stays finite. That must raise, not return zeros."""

    from aimusic.accompaniment.matchmaker_follower import _FactorizedPitchObservationModel

    dense_profiles = np.full((4, 110), 0.999)
    with pytest.raises(ValueError, match="underflows"):
        _FactorizedPitchObservationModel(dense_profiles)


def test_rewrites_refuse_to_degrade_silently() -> None:
    """A follower missing the internals must fail loudly, never run 50x slower."""

    import types

    from aimusic.accompaniment.matchmaker_follower import _supports_rewrites

    with pytest.raises(RuntimeError, match="50x slower"):
        _supports_rewrites(types.SimpleNamespace(transition_model=None))


def test_released_dense_matrix_reports_why_it_is_gone(score_midi: Path) -> None:
    """Freed internals must explain themselves rather than surface as None."""

    hmm = _raw_pthmm(score_midi)
    _band_pthmm_transition(hmm, backward_states=DEFAULT_TRANSITION_BACKWARD_STATES)
    with pytest.raises(RuntimeError, match="released when the transition was banded"):
        hmm.transition_model().T
