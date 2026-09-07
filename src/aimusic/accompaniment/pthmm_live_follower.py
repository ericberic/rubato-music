"""Narrow live-only adapter for Matchmaker's pitch HMM.

Matchmaker's public convenience class imports its audio processors, plotting
helpers, and Partitura's complete score I/O surface before it can construct a
MIDI PTHMM. None of that belongs on the performance startup path. This module
loads the PTHMM implementation without executing those package initializers,
parses the already-prepared MIDI reference directly, and preserves the same
pitch observation and follower-update contract.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import queue
import sys
import threading
import types
from pathlib import Path
from typing import Any

import mido
import numpy as np

from aimusic.accompaniment.following import FollowerUpdate, PerformedNote
from aimusic.accompaniment.matchmaker_follower import (
    _broaden_pthmm_prior,
    _seed_pthmm_prior,
)


class PthmmLiveFollower:
    """Pitch-only Matchmaker follower without offline/audio dependencies."""

    def __init__(
        self,
        score_file: Path | str,
        *,
        provisional_confidence: float = 0.5,
        minimum_lock_updates: int = 3,
        initial_reference_beat: float | None = None,
        max_wait_seconds: float = 0.005,
        startup_observer: Any | None = None,
        **_: Any,
    ) -> None:
        progress = startup_observer or (lambda stage: None)
        progress("import_pitch_hmm")
        pitch_hmm, stream_end = _load_pitch_hmm()
        progress("parse_reference_midi")
        reference = _reference_note_array(Path(score_file))
        self._input: queue.Queue[Any] = queue.Queue()
        progress("build_pitch_hmm")
        self.score_follower = pitch_hmm(
            reference_features=reference,
            queue=self._input,
            has_insertions=True,
            piano_range=True,
        )
        self._stream_end = stream_end
        self._updates: queue.Queue[float] = queue.Queue()
        self._confidence = provisional_confidence
        self._minimum_lock_updates = minimum_lock_updates
        self._max_wait_seconds = max_wait_seconds
        self._stable_updates = 0
        self._warm_start_reference_beat = (
            _seed_pthmm_prior(self.score_follower, initial_reference_beat)
            if initial_reference_beat is not None
            else None
        )
        self._last_score_beat: float | None = self._warm_start_reference_beat
        self._closed = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="rubato-pthmm",
            daemon=True,
        )
        progress("start_tracking_thread")
        self._thread.start()

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        if self._closed:
            raise RuntimeError("PTHMM live follower is closed")
        if self._error is not None:
            raise RuntimeError("PTHMM live follower failed") from self._error
        if not 21 <= note.pitch <= 108:
            return None
        pitch = np.zeros(88, dtype=np.float32)
        pitch[note.pitch - 21] = 1
        self._input.put((pitch, note.perf_time))
        try:
            score_beat = self._updates.get(timeout=self._max_wait_seconds)
        except queue.Empty:
            return None
        return self._latest_update(note.perf_time, score_beat)

    def poll_update(self) -> FollowerUpdate | None:
        return self._latest_update(None)

    def _latest_update(
        self,
        perf_time: float | None,
        score_beat: float | None = None,
    ) -> FollowerUpdate | None:
        while True:
            try:
                score_beat = self._updates.get_nowait()
            except queue.Empty:
                break
        if score_beat is None:
            return None
        if self._last_score_beat is None or -0.25 <= score_beat - self._last_score_beat <= 8.0:
            self._stable_updates += 1
        else:
            self._stable_updates = 1
        self._last_score_beat = score_beat
        locked = self._stable_updates >= self._minimum_lock_updates
        return FollowerUpdate(
            perf_time=0.0 if perf_time is None else perf_time,
            score_beat=score_beat,
            confidence=self._confidence if locked else 0.0,
            raw_state={
                "follower": "matchmaker",
                "method": "pthmm",
                "input_type": "midi_stream",
                "dependency_scope": "live_pitch_only",
                "confidence_kind": "uncalibrated_policy_value",
                "locked": locked,
                "lock_kind": "observation_count_heuristic",
                "stable_update_count": self._stable_updates,
                "warm_start_reference_beat": self._warm_start_reference_beat,
            },
        )

    def reposition_for_entry(
        self,
        *,
        score_beat: float,
        reference_beat: float | None,
    ) -> None:
        target = score_beat if reference_beat is None else reference_beat
        self._warm_start_reference_beat = _seed_pthmm_prior(self.score_follower, target)
        self._last_score_beat = self._warm_start_reference_beat
        self._stable_updates = 0

    def relocalize(self) -> None:
        _broaden_pthmm_prior(self.score_follower, from_beat=self._last_score_beat)
        self._stable_updates = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._input.put(self._stream_end)
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        try:
            for position in self.score_follower.run(verbose=False):
                self._updates.put(float(position))
        except BaseException as exc:
            self._error = exc


def _reference_note_array(path: Path) -> np.ndarray:
    midi = mido.MidiFile(path)
    events: list[tuple[float, int]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                events.append((tick / midi.ticks_per_beat, message.note))
    if not events:
        raise ValueError(f"Follower reference has no note-on events: {path}")
    events.sort()
    result = np.empty(len(events), dtype=[("onset_beat", "f8"), ("pitch", "i4")])
    result["onset_beat"] = [event[0] for event in events]
    result["pitch"] = [event[1] for event in events]
    return result


def _load_pitch_hmm() -> tuple[Any, Any]:
    """Load only Matchmaker modules required by the live pitch HMM.

    The worker is a fresh spawned process, so installing namespace parents here
    cannot affect the server or offline score tooling. The tiny misc module
    supplies the three numerical helpers imported by Matchmaker's HMM module;
    its upstream misc module eagerly imports plotting and audio packages.
    """
    distribution = importlib.metadata.distribution("pymatchmaker")
    if distribution.version != "0.3.0":
        raise RuntimeError(
            "The narrow live PTHMM adapter requires pymatchmaker==0.3.0; "
            f"found {distribution.version}"
        )
    root = Path(distribution.locate_file("matchmaker"))
    for name, directory in {
        "matchmaker": root,
        "matchmaker.io": root / "io",
        "matchmaker.prob": root / "prob",
        "matchmaker.utils": root / "utils",
    }.items():
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__path__ = [str(directory)]
        module.__package__ = name
        sys.modules[name] = module

    misc_name = "matchmaker.utils.misc"
    if misc_name not in sys.modules:
        misc = types.ModuleType(misc_name)

        def get_window_indices(indices: np.ndarray, context: int) -> np.ndarray:
            return (indices[:, np.newaxis] + np.arange(-context, context + 1)).astype(int)

        def interleave_with_constant(array: np.ndarray, constant_row: float = 0) -> np.ndarray:
            result = np.zeros((array.shape[0] * 2, array.shape[1]), dtype=array.dtype)
            result[0::2] = array
            result[1::2] = constant_row
            return result

        def set_latency_stats(
            latency: float,
            latency_stats: dict[str, float],
            count: int,
        ) -> dict[str, float]:
            latency_stats["total_latency"] += latency
            latency_stats["total_frames"] = count
            latency_stats["max_latency"] = max(latency_stats["max_latency"], latency)
            latency_stats["min_latency"] = min(latency_stats["min_latency"], latency)
            return latency_stats

        misc.get_window_indices = get_window_indices
        misc.interleave_with_constant = interleave_with_constant
        misc.set_latency_stats = set_latency_stats
        sys.modules[misc_name] = misc

    hmm = importlib.import_module("matchmaker.prob.hmm")
    stream = importlib.import_module("matchmaker.io.stream")
    return hmm.PitchHMM, stream.STREAM_END


__all__ = ["PthmmLiveFollower"]
