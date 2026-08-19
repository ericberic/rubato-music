"""Thin adapters around Matchmaker real score-following runs."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mido
import numpy as np

from aimusic.accompaniment.following import FollowerUpdate, PerformedNote


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
        try:
            from matchmaker import Matchmaker
            from matchmaker.features.midi import PitchProcessor
            from matchmaker.io.midi import BytesMidiStream
        except ImportError as exc:
            raise ImportError("Matchmaker live following requires `uv sync --extra live`.") from exc

        self._input: queue.Queue[bytes | None] = queue.Queue()
        stream = BytesMidiStream(
            processor=PitchProcessor(piano_range=True),
            data_queue=self._input,
            polling_period=None,
        )
        self._matchmaker = Matchmaker(
            score_file,
            performance_file=None,
            input_type="midi",
            method=method,
            stream=stream,
            tempo=tempo_bpm,
            unfold_score=unfold_score,
            kwargs={"processor": "pitch", "piano_range": True},
        )
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

    def reposition_for_entry(
        self,
        *,
        score_beat: float,
        reference_beat: float | None,
    ) -> None:
        """Move the untouched live PTHMM prior to the orchestra's current beat.

        Rubato calls this immediately before forwarding the first piano note of
        an orchestra-led cue-in. Matchmaker's coordinate is the reference
        performance beat when one exists; legacy identity bundles fall back to
        their score beat.
        """

        if self._closed:
            raise RuntimeError("Matchmaker stream follower is closed")
        target_beat = score_beat if reference_beat is None else reference_beat
        if target_beat < 0:
            raise ValueError("entry follower position must be non-negative")
        self._warm_start_reference_beat = _seed_pthmm_prior(
            self._matchmaker.score_follower,
            target_beat,
        )
        self._stable_updates = 0
        self._last_score_beat = self._warm_start_reference_beat

    def relocalize(self) -> None:
        """Reset the PTHMM to a broad prior for a global position re-search.

        Unlike :meth:`reposition_for_entry`, which centers a narrow prior on a
        known beat, this spreads the initial distribution across the whole score
        so the tracker can re-acquire its absolute position from the pitch stream
        after it has fallen behind and cannot climb back. Rubato calls it when a
        watchdog sees the position stall while notes keep arriving -- an early
        mis-lock that would otherwise persist for the rest of the take (observed
        on a real take whose opening never locked, leaving the tracker ~70 beats
        behind for the entire piece; one re-search recovered it to ~4). Same
        mid-stream seam as ``reposition_for_entry``; it does not touch the
        transition or observation model.
        """

        if self._closed:
            raise RuntimeError("Matchmaker stream follower is closed")
        _broaden_pthmm_prior(
            self._matchmaker.score_follower,
            from_beat=self._last_score_beat,
        )
        self._stable_updates = 0

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        if self._closed:
            raise RuntimeError("Matchmaker stream follower is closed")
        if self._error is not None:
            raise RuntimeError("Matchmaker stream follower failed") from self._error
        message = mido.Message(
            "note_on", note=note.pitch, velocity=max(1, note.velocity), channel=0
        )
        self._input.put(bytes(message.bytes()))
        try:
            score_beat = self._updates.get(timeout=self._max_wait_seconds)
        except queue.Empty:
            return None
        # Collapse any backlog to the freshest estimate for this control tick.
        while True:
            try:
                score_beat = self._updates.get_nowait()
            except queue.Empty:
                break
        if self._last_score_beat is None or -0.25 <= score_beat - self._last_score_beat <= 8.0:
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
