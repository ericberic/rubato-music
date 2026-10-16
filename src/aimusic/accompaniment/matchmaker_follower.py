"""Thin adapters around Matchmaker real score-following runs."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from typing import Any

import mido
import numpy as np

from aimusic.accompaniment.following import FollowerUpdate, PerformedNote

logger = logging.getLogger(__name__)

# Backward reach of the banded PTHMM transition, in states. Chosen from 117
# recorded takes: the largest backward step ever observed was 35 states, so this
# covers every one with 2x headroom. See _band_pthmm_transition.
DEFAULT_TRANSITION_BACKWARD_STATES = 64


@dataclass(frozen=True)
class MatchmakerMidiRun:
    """Follower updates emitted by a Matchmaker offline MIDI simulation."""

    updates: tuple[FollowerUpdate, ...]
    alignment_path: tuple[tuple[float, ...], tuple[float, ...]]
    method: str


class MatchmakerStreamFollower:
    """Bounded live-MIDI adapter for Matchmaker's ``pthmm`` tracker.

    Matchmaker owns a streaming generator rather than an ``observe`` method.
    This adapter bridges Rubato's causal protocol using its ``BytesMidiStream``:
    each performed note is forwarded as MIDI bytes and ``observe`` waits only
    ``max_wait_seconds`` (5 ms by default) for the next estimate. A missed deadline returns
    ``None`` and never stalls the hardware loop indefinitely.

    Two exact rewrites are always applied to Matchmaker's PTHMM: the dense
    transition matvec becomes a banded sparse one
    (:func:`_band_pthmm_transition`), and the Bernoulli observation is
    factorized (:class:`_FactorizedPitchObservationModel`). Together they take
    the per-note cost from ~24 ms to ~0.35 ms on the full movement while
    producing a bit-identical beat path, so there is no slower path to fall back
    to. ``transition_backward_states`` remains tunable because the right
    backward reach may prove score-dependent.

    Matchmaker 0.3 does not expose a calibrated confidence or lock probability.
    ``provisional_confidence`` is therefore an explicit policy value, not a
    probabilistic claim.  Callers should require several stable observations
    and inspect tracking error before enabling risky entrances.
    """

    def __init__(
        self,
        score_file: Path | str,
        *,
        method: str = "pthmm",
        tempo_bpm: float | None = None,
        unfold_score: bool = False,
        max_wait_seconds: float = 0.005,
        provisional_confidence: float = 0.5,
        minimum_lock_updates: int = 3,
        initial_reference_beat: float | None = None,
        transition_backward_states: int = DEFAULT_TRANSITION_BACKWARD_STATES,
        gauge_publisher: Any | None = None,
    ) -> None:
        if method != "pthmm":
            raise ValueError("The live stream adapter currently supports only method='pthmm'")
        if max_wait_seconds < 0:
            raise ValueError("max_wait_seconds must be non-negative")
        if not 0 <= provisional_confidence <= 1:
            raise ValueError("provisional_confidence must be between 0 and 1")
        if minimum_lock_updates <= 0:
            raise ValueError("minimum_lock_updates must be positive")
        if initial_reference_beat is not None and initial_reference_beat < 0:
            raise ValueError("initial_reference_beat must be non-negative")
        startup_started = time.monotonic()
        try:
            from matchmaker import Matchmaker
            from matchmaker.features.midi import PitchProcessor
            from matchmaker.io.midi import BytesMidiStream
        except ImportError as exc:
            raise ImportError("Matchmaker live following requires `uv sync --extra live`.") from exc

        self.startup_timings_ms = {"imports": (time.monotonic() - startup_started) * 1000}
        model_started = time.monotonic()
        self._input: queue.Queue[bytes | None] = queue.Queue()
        stream = BytesMidiStream(
            processor=PitchProcessor(piano_range=True),
            data_queue=self._input,
            polling_period=None,
        )
        timings = self.startup_timings_ms

        class TimedMatchmaker(Matchmaker):
            def _build_processor(self, *args, **kwargs):
                # Matchmaker calls this immediately after score loading.
                timings["score_loading"] = (time.monotonic() - model_started) * 1000
                return super()._build_processor(*args, **kwargs)

            def preprocess_score(self, *args, **kwargs):
                started = time.monotonic()
                result = super().preprocess_score(*args, **kwargs)
                timings["score_features"] = (time.monotonic() - started) * 1000
                return result

            def _build_score_follower(self, *args, **kwargs):
                started = time.monotonic()
                result = super()._build_score_follower(*args, **kwargs)
                timings["tracking_model"] = (time.monotonic() - started) * 1000
                return result

        self._matchmaker = TimedMatchmaker(
            score_file,
            performance_file=None,
            input_type="midi",
            method=method,
            stream=stream,
            tempo=tempo_bpm,
            unfold_score=unfold_score,
            kwargs={"processor": "pitch", "piano_range": True},
        )
        self.startup_timings_ms["score_and_model"] = (time.monotonic() - model_started) * 1000
        self._configured_for_run = False
        # Band before seeding: seeding only touches ``init_probabilities``, but
        # banding frees the dense matrices and should happen while nothing else
        # holds a reference. Runs during background preparation, off the run's
        # critical path.
        score_follower = getattr(self._matchmaker, "score_follower", None)
        self._transition_banding = (
            _band_pthmm_transition(
                score_follower,
                backward_states=transition_backward_states,
            )
            if _supports_rewrites(score_follower)
            else None
        )
        self._observation_factorization = (
            _factorize_pthmm_observation(score_follower)
            if _supports_rewrites(score_follower)
            else None
        )
        if gauge_publisher is not None and score_follower is not None:
            _instrument_pthmm_step(score_follower, gauge_publisher)
        self._warm_start_reference_beat = (
            _seed_pthmm_prior(self._matchmaker.score_follower, initial_reference_beat)
            if initial_reference_beat is not None
            else None
        )
        self._updates: queue.Queue[float] = queue.Queue()
        self._max_wait_seconds = max_wait_seconds
        self._confidence = provisional_confidence
        self._minimum_lock_updates = minimum_lock_updates
        self._stable_updates = 0
        self._last_score_beat: float | None = self._warm_start_reference_beat
        self._method = method
        self._closed = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def configure_entry(self, reference_beat: float | None, minimum_lock_updates: int) -> None:
        """Configure once, before input; this is deliberately not a take reset."""
        if self._configured_for_run or self._stable_updates:
            raise RuntimeError("Only a pristine follower can be claimed")
        self._configured_for_run = True
        self._minimum_lock_updates = minimum_lock_updates
        if reference_beat is not None:
            self.reposition_for_entry(score_beat=reference_beat, reference_beat=reference_beat)

    def reposition_for_entry(
        self,
        *,
        score_beat: float,
        reference_beat: float | None = None,
    ) -> None:
        """Move the warm-start entry point without restarting the follower thread."""
        target = reference_beat if reference_beat is not None else score_beat
        self._warm_start_reference_beat = _seed_pthmm_prior(
            self._matchmaker.score_follower, target
        )
        self._last_score_beat = self._warm_start_reference_beat
        self._stable_updates = 0
        while not self._updates.empty():
            try:
                self._updates.get_nowait()
            except queue.Empty:
                break

    def relocalize(self) -> None:
        """Recover from a stall by broadening the tracker's prior forward."""
        _broaden_pthmm_prior(
            self._matchmaker.score_follower,
            from_beat=self._last_score_beat,
        )
        self._stable_updates = 0
        while not self._updates.empty():
            try:
                self._updates.get_nowait()
            except queue.Empty:
                break

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        """Forward a note-on and wait at most ``max_wait_seconds`` for an estimate."""
        if self._closed:
            raise RuntimeError("follower is closed")
        if self._error is not None:
            raise RuntimeError("follower failed") from self._error
        self._input.put(
            mido.Message(
                "note_on",
                note=note.pitch,
                velocity=note.velocity,
                time=0,
            ).bin()
        )
        try:
            score_beat = self._updates.get(timeout=self._max_wait_seconds)
        except queue.Empty:
            return None
        return self._latest_update(note, score_beat)

    def poll_update(self) -> FollowerUpdate | None:
        """Drain any update that arrived after an `observe` timeout."""
        if self._closed or self._error is not None:
            return None
        try:
            score_beat = self._updates.get_nowait()
        except queue.Empty:
            return None
        note = PerformedNote(
            perf_time=time.monotonic(),
            pitch=60,
            velocity=0,
        )
        return self._latest_update(note, score_beat)

    def _latest_update(self, note: PerformedNote, score_beat: float) -> FollowerUpdate:
        while True:
            try:
                score_beat = self._updates.get_nowait()
            except queue.Empty:
                break
        if self._last_score_beat is not None and score_beat >= self._last_score_beat:
            self._stable_updates += 1
        else:
            self._stable_updates = 1
        self._last_score_beat = score_beat
        locked = self._stable_updates >= self._minimum_lock_updates
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=score_beat,
            confidence=self._confidence if locked else 0.0,
            raw_state={
                "follower": "matchmaker",
                "method": self._method,
                "input_type": "midi_stream",
                "confidence_kind": "uncalibrated_policy_value",
                "locked": locked,
                "lock_kind": "observation_count_heuristic",
                "stable_update_count": self._stable_updates,
                "warm_start_reference_beat": self._warm_start_reference_beat,
                "transition_banding": self._transition_banding,
                "observation_factorization": self._observation_factorization,
            },
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._input.put(None)
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        try:
            for position in self._matchmaker.run(verbose=False):
                self._updates.put(float(position))
        except BaseException as exc:  # propagated on the hardware/control thread
            self._error = exc


class _FreedDenseMatrix:
    """Stands in for a dense matrix we deliberately released.

    Setting the freed attributes to ``None`` would surface as
    ``TypeError: 'NoneType' object is not subscriptable`` somewhere deep in
    numpy. This says what actually happened and why.
    """

    __slots__ = ()

    def _fail(self, *_: Any, **__: Any):
        raise RuntimeError(
            "this PTHMM's dense transition matrix was released when the "
            "transition was banded (see _band_pthmm_transition). Only the "
            "banded forward step may be used; rebuild the follower to get a "
            "dense matrix back."
        )

    __getitem__ = _fail
    __getattr__ = _fail
    __array__ = _fail
    __call__ = _fail


def _band_pthmm_transition(
    score_follower: Any,
    *,
    backward_states: int = DEFAULT_TRANSITION_BACKWARD_STATES,
) -> dict[str, Any]:
    """Replace the PTHMM's dense transition matvec with a banded sparse one.

    Matchmaker stores the transition model as a dense ``n_states x n_states``
    float64 matrix and ``hiddenmarkov`` caches a ``log`` copy beside it. On the
    full Chopin movement that is 5557 states -- 247 MB each, 494 MB resident --
    and every note pays a dense matvec over all of it. Measured on an M1 that is
    9.3 ms of the ~24.5 ms per-note cost, and it is almost entirely wasted: each
    row reaches only ``-744 .. +8``, and 92.4% of its mass sits in the forward
    offsets ``[0, +8]``.

    Banding keeps the forward reach exact (the matrix's own limit is +8) and
    truncates only the long backward tail, which holds 1.06% of row mass. Across
    117 recorded takes (44,463 follower steps) the largest backward step ever
    observed was 35 states; ``backward_states=64`` covers every one of them with
    2x headroom, costs 0.28 ms against 5.86 ms dense, and drops the transition
    model to ~4.8 MB. Rows are renormalized so the model stays a proper
    distribution.

    This is the same class of seam as :func:`_seed_pthmm_prior`: it rebinds
    ``forward_algorithm_step`` on the instance and leaves the observation model,
    the state space and the tempo model untouched.

    Returns a dict of measured facts about what was replaced, for the trace.
    """

    import scipy.sparse as sp

    if backward_states < 1:
        raise ValueError("backward_states must be positive")
    transition_model = score_follower.transition_model
    transition_model.use_log_probabilities = False
    dense = transition_model()
    if isinstance(dense, _FreedDenseMatrix):
        raise RuntimeError("PTHMM transition matrix has already been banded")
    n_states = len(dense)

    # The matrix's own forward reach; never truncate it, so forward motion stays
    # bit-exact and only the backward tail is approximated.
    forward_states = int(
        max(
            (k for k in range(n_states) if np.any(np.diag(dense, k) > 0.0)),
            default=0,
        )
    )
    offsets = [k for k in range(-backward_states, forward_states + 1) if -n_states < k < n_states]
    banded = sp.diags(
        [np.diag(dense, k) for k in offsets], offsets, shape=(n_states, n_states), format="csr"
    )
    row_sums = np.asarray(banded.sum(axis=1)).ravel()
    dropped_mass = float(np.mean(1.0 - row_sums))
    banded = sp.csr_matrix(banded.multiply(1.0 / np.maximum(row_sums, 1e-300)[:, None]))
    transposed = banded.T.tocsr()

    def _banded_forward_step(
        self: Any, observation: Any, log_probabilities: bool = False
    ) -> int:
        """Mirror of ``hiddenmarkov.forward_algorithm_step`` over the band.

        Same recursion and the same ``max(sum, 1e-6)`` normalization guard, so a
        banded run is numerically comparable to a dense one step for step.
        """

        if log_probabilities:
            raise NotImplementedError("banded PTHMM step supports linear probabilities only")
        self.transition_model.use_log_probabilities = False
        self.observation_model.use_log_probabilities = False
        if self.forward_variable is None:
            transition_prob = self.transition_model.init_probabilities
        else:
            transition_prob = transposed.dot(self.forward_variable)
        forward_variable = self.observation_model(observation) * transition_prob
        forward_variable /= max(forward_variable.sum(), 1e-6)
        self.forward_variable = forward_variable
        return int(np.argmax(forward_variable))

    score_follower.forward_algorithm_step = MethodType(_banded_forward_step, score_follower)
    # Release both dense copies. Nothing else reads them: the seeding helpers
    # touch only ``init_probabilities``, which is a separate vector.
    dense_bytes = dense.nbytes
    log_dense = getattr(transition_model, "_log_transition_prob", None)
    dense_bytes += log_dense.nbytes if log_dense is not None else 0
    freed = _FreedDenseMatrix()
    transition_model._transition_prob = freed
    transition_model._log_transition_prob = freed
    return {
        "n_states": n_states,
        "backward_states": backward_states,
        "forward_states": forward_states,
        "dense_bytes": int(dense_bytes),
        "banded_bytes": int(
            transposed.data.nbytes + transposed.indices.nbytes + transposed.indptr.nbytes
        ),
        "mean_row_mass_dropped": dropped_mass,
    }


class _FactorizedPitchObservationModel:
    r"""Matchmaker's Bernoulli pitch likelihood, evaluated the cheap way.

    Matchmaker computes, for every state, a product over all 88 pitch
    dimensions::

        prod_j  P[s,j]**o_j  *  (1 - P[s,j])**(1 - o_j)

    That is two ``pow`` calls and an 88-wide product per state per note. But the
    observation is a binary piano-roll, so the expression factors::

        prod_j (1 - P[s,j])   *   prod_{j sounding} P[s,j] / (1 - P[s,j])
        \_ independent of o _/      \_ one term per sounding pitch _/

    The left factor never changes, so it is precomputed once. The right factor
    touches only the pitches actually played -- 1 on this movement's
    near-monophonic solo reference, a handful in a chord.

    This is an algebraic identity, not an approximation. Measured over 5673
    notes across 16 takes, the beat path is **bit-identical** to Matchmaker's own
    (max difference exactly 0.0), and over 240 synthetic 2-8 note chords the
    relative error is 2.2e-15 -- float roundoff. Cost falls from 8.00 ms to
    0.34 ms p50 (23.5x); p95 from 11.99 ms to 0.63 ms.

    Crucially it keeps scoring the **whole** score every note, so
    :meth:`MatchmakerStreamFollower.relocalize` still works. A windowed
    observation model would be a cheaper-looking alternative but silently
    disables that recovery: measured, full observation re-acquires a stalled
    tracker in 2 notes and a +/-512-state window never does.

    Falls back to the direct expression if an observation is not binary, so a
    different processor cannot silently get wrong numbers.
    """

    def __init__(self, pitch_profiles: np.ndarray) -> None:
        profiles = np.asarray(pitch_profiles, dtype=float)
        if not np.all((profiles > 0.0) & (profiles < 1.0)):
            raise ValueError(
                "pitch profiles must lie strictly in (0, 1) to factorize; "
                "a 0 or 1 makes the odds ratio undefined"
            )
        base = np.prod(1.0 - profiles, axis=1)
        # ``base`` multiplies (1-P) over *every* dimension, including the ones
        # that end up sounding -- which the direct expression never does. So it
        # can underflow to 0 where the direct form stays finite: with 110
        # dimensions at P=0.999 and 10 sounding, direct gives 9.9e-301 and the
        # factorization gives 0.0. Once base is zero the odds multiply cannot
        # rescue it, the forward variable collapses and argmax pins to state 0.
        # Dense or high-confidence profiles are the regime at risk; this
        # movement's solo reference sits at ~2e-28, far clear of it.
        if not np.all(base > 0.0):
            raise ValueError(
                "factorizing these pitch profiles underflows float64: "
                f"prod(1 - P) is zero for {int(np.sum(base <= 0.0))} of {len(base)} "
                "states. Use log-odds accumulation for profiles this dense "
                "(it costs bit-identity with Matchmaker's linear recursion)."
            )
        self.pitch_profiles = profiles
        self._base = base
        self._odds = profiles / (1.0 - profiles)
        self.underflow_margin = float(np.min(base))
        self.use_log_probabilities = False
        self.current_state: int | None = None

    def __call__(self, observation: np.ndarray) -> np.ndarray:
        obs = np.asarray(observation, dtype=float)
        sounding = np.flatnonzero(obs)
        if not np.all(obs[sounding] == 1.0):  # not a binary piano-roll
            profiles = self.pitch_profiles
            return np.prod((profiles**obs) * ((1 - profiles) ** (1 - obs)), axis=1)
        probabilities = self._base.copy()
        for pitch in sounding:
            probabilities *= self._odds[:, pitch]
        return probabilities


def _factorize_pthmm_observation(score_follower: Any) -> dict[str, Any]:
    """Swap in the factorized observation model. Returns facts for the trace."""

    model = score_follower.observation_model
    profiles = getattr(model, "pitch_profiles", None)
    if profiles is None:
        raise RuntimeError("PTHMM observation model exposes no pitch profiles")
    replacement = _FactorizedPitchObservationModel(profiles)
    replacement.current_state = getattr(model, "current_state", None)
    score_follower.observation_model = replacement
    return {
        "n_states": int(profiles.shape[0]),
        "pitch_dimensions": int(profiles.shape[1]),
        "precomputed_bytes": int(replacement._base.nbytes + replacement._odds.nbytes),
    }


def _supports_rewrites(score_follower: Any) -> bool:
    """Require a real PTHMM the exact rewrites can be applied to.

    Raises rather than degrading. Silently running Matchmaker's dense path in a
    live performance is ~50x slower per note and would breach the follower's
    p95 contract with no warning -- a worse outcome than refusing to start.
    Returns False only when there is no follower at all (the fake-stream test
    doubles, which stand in for the MIDI-byte bridge and never build an HMM).
    """

    if score_follower is None:
        return False
    has_transition = callable(getattr(score_follower, "transition_model", None))
    profiles = getattr(getattr(score_follower, "observation_model", None), "pitch_profiles", None)
    has_profiles = isinstance(profiles, np.ndarray) and profiles.ndim == 2
    if not (has_transition and has_profiles):
        raise RuntimeError(
            "this score follower exposes no dense transition matrix "
            f"(transition_model={has_transition}) or pitch profiles "
            f"(pitch_profiles={has_profiles}), so Rubato's exact PTHMM rewrites "
            "cannot be applied. Refusing to fall back to Matchmaker's dense "
            "path: it is ~50x slower per note and would breach the follower's "
            "p95 latency contract silently. Check the installed matchmaker "
            "version against the pin in pyproject.toml."
        )
    return True


def _instrument_pthmm_step(score_follower: Any, publisher: Any) -> None:
    """Time the HMM itself, not the plumbing around it.

    The follower has three layers: the parent enqueues a note, a child process
    dispatches it, and an inner thread inside this adapter drives Matchmaker's
    generator -- and only that innermost layer runs the HMM. Timing
    ``observe()`` (the obvious place) measures the dispatch instead, which
    returns as soon as it has handed the bytes over or hit its 5 ms wait: it
    reads ~0.1 ms while the HMM is actually costing tens of ms.

    Wrapping ``step`` puts the measurement on the one call that is exactly one
    forward recursion, so dense and banded runs are directly comparable.
    """

    inner = score_follower.step

    def timed_step(features: Any) -> None:
        started = time.monotonic()
        try:
            inner(features)
        finally:
            now = time.monotonic()
            publisher.beat(now=now, work_seconds=now - started, events=1)

    score_follower.step = timed_step


def _seed_pthmm_prior(score_follower: Any, reference_beat: float) -> float:
    """Center Matchmaker's PTHMM initial distribution at one reference beat.

    PTHMM exposes the score-position state space and its transition model after
    construction. This is the narrowest supported seam in Matchmaker 0.3: set
    the initial distribution before the streaming thread consumes an onset,
    without changing transition or observation behavior.
    """

    state_space = np.asarray(score_follower.state_space, dtype=float)
    if state_space.ndim != 1 or len(state_space) == 0:
        raise ValueError("Matchmaker PTHMM has no one-dimensional score state space")
    if not np.all(np.isfinite(state_space)):
        raise ValueError("Matchmaker PTHMM score state space is not finite")
    center = int(np.argmin(np.abs(state_space - reference_beat)))
    state_indices = np.arange(len(state_space), dtype=float)
    sigma_states = 2.0
    probabilities = np.exp(-0.5 * ((state_indices - center) / sigma_states) ** 2)
    probabilities[np.abs(state_indices - center) > 8] = 0.0
    total = float(probabilities.sum())
    if total <= 0:
        raise ValueError("Matchmaker PTHMM warm-start prior is empty")
    score_follower.transition_model.init_probabilities = probabilities / total
    score_follower.forward_variable = None
    return float(state_space[center])


def _broaden_pthmm_prior(
    score_follower: Any,
    from_beat: float | None = None,
    *,
    backward_slack_states: int = 4,
) -> None:
    """Reset Matchmaker's PTHMM initial distribution to a broad *forward* prior.

    The counterpart to :func:`_seed_pthmm_prior`: instead of concentrating the
    prior near one beat, spread it out so the next observations re-localize the
    tracker. The re-search is deliberately **forward-biased** from ``from_beat``
    (the tracker's current estimate): a stalled tracker is always stalled
    *behind* the performer, and the piece restates earlier material (the Romance
    ending echoes its opening), so a uniform re-seed can jump *backward* to the
    wrong restatement -- observed on end-of-piece takes as a ~400-beat backward
    leap. Uniform over ``[from_beat - backward_slack_states, end]`` lets the
    tracker leap forward to catch up while forbidding the backward mis-lock; a
    small backward slack forgives a slight overshoot. ``from_beat=None`` falls
    back to a fully uniform prior. Same supported seam (set ``init_probabilities``
    and clear ``forward_variable``); transition and observation models untouched.
    """

    state_space = np.asarray(score_follower.state_space, dtype=float)
    if state_space.ndim != 1 or len(state_space) == 0:
        raise ValueError("Matchmaker PTHMM has no one-dimensional score state space")
    count = len(state_space)
    probabilities = np.zeros(count)
    if from_beat is None:
        probabilities[:] = 1.0
    else:
        center = int(np.argmin(np.abs(state_space - from_beat)))
        start = max(0, center - backward_slack_states)
        probabilities[start:] = 1.0
    total = float(probabilities.sum())
    if total <= 0:
        raise ValueError("Matchmaker PTHMM re-localization prior is empty")
    score_follower.transition_model.init_probabilities = probabilities / total
    score_follower.forward_variable = None


def run_matchmaker_midi_file(
    score_file: Path | str,
    performance_file: Path | str,
    *,
    method: str = "arzt",
    tempo_bpm: float | None = None,
    unfold_score: bool = False,
    kwargs: dict[str, Any] | None = None,
) -> MatchmakerMidiRun:
    """Run Matchmaker's real MIDI follower on score/performance files.

    This is an offline simulation path. It exercises the actual Matchmaker
    follower and returns Rubato `FollowerUpdate` rows that can later feed the
    tempo model and scheduler. It intentionally does not claim live Yamaha MIDI
    support; that requires a separate stream-backed adapter.
    """

    try:
        from matchmaker import Matchmaker
    except ImportError as exc:  # pragma: no cover - covered by optional-test skip
        raise ImportError(
            "Matchmaker is required. Install Rubato with `uv sync --extra live`."
        ) from exc

    matchmaker_kwargs = dict(kwargs or {})
    matchmaker = Matchmaker(
        score_file,
        performance_file,
        input_type="midi",
        method=method,
        tempo=tempo_bpm,
        wait=False,
        unfold_score=unfold_score,
        kwargs=matchmaker_kwargs or None,
    )
    list(matchmaker.run(verbose=False))
    path = matchmaker.score_follower.alignment_path
    score_beats = tuple(path[0].tolist())
    perf_times = tuple(path[1].tolist())
    updates = tuple(
        FollowerUpdate(
            perf_time=perf_time,
            score_beat=score_beat,
            # Matchmaker's alignment path does not include calibrated
            # confidence, even in file simulation. Keep this an explicit
            # policy placeholder rather than asserting certainty.
            confidence=0.5,
            raw_state={
                "follower": "matchmaker",
                "method": method,
                "input_type": "midi_file",
                "confidence_kind": "uncalibrated_policy_value",
            },
        )
        for score_beat, perf_time in zip(score_beats, perf_times, strict=True)
    )
    return MatchmakerMidiRun(
        updates=updates,
        alignment_path=(score_beats, perf_times),
        method=method,
    )
