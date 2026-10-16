"""The factorized Bernoulli observation must be exact, not merely close.

These test the algebra directly against the expression Matchmaker evaluates, so
they need neither the `live` extra nor a built HMM and run in milliseconds.
"""

from __future__ import annotations

import numpy as np
import pytest

from aimusic.accompaniment.matchmaker_follower import _FactorizedPitchObservationModel


def direct(profiles: np.ndarray, obs: np.ndarray) -> np.ndarray:
    """Matchmaker's own expression, verbatim, as the reference."""

    return np.prod((profiles**obs) * ((1 - profiles) ** (1 - obs)), axis=1)


@pytest.fixture
def profiles() -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.uniform(0.002, 0.52, size=(200, 88))


def one_hot(*pitches: int) -> np.ndarray:
    obs = np.zeros(88)
    obs[list(pitches)] = 1.0
    return obs


def test_single_pitch_matches_the_direct_expression(profiles: np.ndarray) -> None:
    model = _FactorizedPitchObservationModel(profiles)
    for pitch in (0, 40, 87):
        obs = one_hot(pitch)
        assert np.allclose(model(obs), direct(profiles, obs), rtol=1e-12, atol=0.0)


def test_chords_match_the_direct_expression(profiles: np.ndarray) -> None:
    model = _FactorizedPitchObservationModel(profiles)
    rng = np.random.default_rng(7)
    for size in (2, 3, 5, 8):
        obs = one_hot(*rng.choice(88, size=size, replace=False))
        assert np.allclose(model(obs), direct(profiles, obs), rtol=1e-12, atol=0.0)


def test_silence_is_the_precomputed_base(profiles: np.ndarray) -> None:
    model = _FactorizedPitchObservationModel(profiles)
    obs = np.zeros(88)
    assert np.allclose(model(obs), direct(profiles, obs), rtol=1e-12, atol=0.0)


def test_non_binary_observation_falls_back_instead_of_going_wrong(
    profiles: np.ndarray,
) -> None:
    """The factorization is only valid for a 0/1 piano-roll.

    A different processor (velocities, a sustained roll) must not silently get
    wrong numbers -- it gets the slow-but-correct path.
    """

    model = _FactorizedPitchObservationModel(profiles)
    obs = np.zeros(88)
    obs[40] = 0.5
    assert np.allclose(model(obs), direct(profiles, obs), rtol=1e-12, atol=0.0)


@pytest.mark.parametrize("bad", [0.0, 1.0])
def test_degenerate_profiles_are_rejected(profiles: np.ndarray, bad: float) -> None:
    """P/(1-P) is undefined at 0 and 1, so refuse rather than emit inf/nan."""

    broken = profiles.copy()
    broken[3, 7] = bad
    with pytest.raises(ValueError, match="strictly in"):
        _FactorizedPitchObservationModel(broken)


def test_precomputation_does_not_depend_on_position(profiles: np.ndarray) -> None:
    """Why `relocalize()` survives this optimization but a window would not.

    The tables derive only from the static pitch profiles, so moving the
    tracker's position invalidates nothing.
    """

    model = _FactorizedPitchObservationModel(profiles)
    obs = one_hot(40)
    first = model(obs)
    model.current_state = 4321  # as BaseHMM.step does every note
    assert np.array_equal(model(obs), first)
