"""Run the Matchmaker score follower in its own process.

Why a process and not a thread: measured on a live take, the Matchmaker PTHMM
holds the GIL for tens of milliseconds per note and that cost *grows* through a
performance (7 ms -> 180 ms). In-process, that starves every other real-time
loop — the MIDI input thread stalled up to 80 ms and the processing loop spiked
to 1.8 s, even though their own work is sub-millisecond. Isolating this one
CPU-bound stage puts it on another core where it cannot steal anyone's deadline.

The wire protocol is deliberately tiny (a timestamp plus three MIDI bytes in, a
position triple out), so ``mp.Queue``'s ~50 us round trip is immaterial against
a 5-20 ms budget. See ``docs/decisions/0011-realtime-multiprocess-architecture.md``.

``ProcessFollower`` keeps the same ``observe`` / ``reposition_for_entry`` /
``close`` contract as the in-process follower, so the engine cannot tell the
difference and every existing test double still applies.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import time
from dataclasses import dataclass
from typing import Any

from aimusic.accompaniment.following import FollowerUpdate, PerformedNote
from aimusic.realtime.gauges import GaugeBlock

_SENTINEL = None


@dataclass(frozen=True)
class FollowerSpec:
    """Everything the child needs to build the follower; must stay picklable."""

    score_file: str
    method: str = "pthmm"
    tempo_bpm: float | None = None
    unfold_score: bool = False
    provisional_confidence: float = 0.5
    minimum_lock_updates: int = 3
    initial_reference_beat: float | None = None


def _child_main(
    spec: FollowerSpec,
    requests: Any,
    responses: Any,
    gauges: GaugeBlock | None,
) -> None:
    """Child entry point: own the HMM, answer note/reposition requests."""

    from aimusic.accompaniment.matchmaker_follower import MatchmakerStreamFollower

    publisher = gauges.publisher("follower") if gauges is not None else None
    try:
        follower = MatchmakerStreamFollower(
            spec.score_file,
            method=spec.method,
            tempo_bpm=spec.tempo_bpm,
            unfold_score=spec.unfold_score,
            provisional_confidence=spec.provisional_confidence,
            minimum_lock_updates=spec.minimum_lock_updates,
            initial_reference_beat=spec.initial_reference_beat,
        )
    except BaseException as exc:  # surface construction failure to the parent
        responses.put(("error", repr(exc)))
        return
    responses.put(("ready", None))

    while True:
        message = requests.get()
        if message is _SENTINEL:
            break
        kind, payload = message
        started = time.monotonic()
        try:
            if kind == "note":
                perf_time, pitch, velocity = payload
                update = follower.observe(
                    PerformedNote(perf_time=perf_time, pitch=pitch, velocity=velocity)
                )
                responses.put(
                    (
                        "update",
                        None
                        if update is None
                        else (
                            update.perf_time,
                            update.score_beat,
                            update.reference_beat,
                            update.confidence,
                            update.raw_state,
                        ),
                    )
                )
            elif kind == "reposition":
                score_beat, reference_beat = payload
                follower.reposition_for_entry(
                    score_beat=score_beat, reference_beat=reference_beat
                )
            elif kind == "relocalize":
                follower.relocalize()
        except BaseException as exc:
            responses.put(("error", repr(exc)))
        if publisher is not None:
            now = time.monotonic()
            publisher.beat(now=now, work_seconds=now - started, events=1)
            publisher.set_queue_depth(_qsize(requests))
    follower.close()


def _qsize(q: Any) -> int:
    try:
        return q.qsize()
    except (NotImplementedError, OSError):  # qsize is unsupported on some platforms
        return 0


class ProcessFollower:
    """Parent-side handle: same follower contract, HMM isolated in a subprocess.

    ``observe`` is synchronous by design — the engine's causal contract expects a
    position (or ``None``) per note — but it waits only ``max_wait_seconds``. A
    slow or wedged child therefore degrades to "no update", which the engine
    already handles by coasting, instead of blocking the conductor.
    """

    def __init__(
        self,
        spec: FollowerSpec,
        *,
        gauges: GaugeBlock | None = None,
        max_wait_seconds: float = 0.25,
        start_timeout_seconds: float = 60.0,
        context: Any | None = None,
    ) -> None:
        self._spec = spec
        self._gauges = gauges
        self._max_wait_seconds = max_wait_seconds
        # "spawn" keeps the child free of inherited threads/handles from the
        # parent's audio and web stack; "fork" would clone them.
        self._ctx = context or mp.get_context("spawn")
        self._requests: Any = self._ctx.Queue()
        self._responses: Any = self._ctx.Queue()
        self._process = self._ctx.Process(
            target=_child_main,
            args=(spec, self._requests, self._responses, gauges),
            name="rubato-follower",
            daemon=True,
        )
        self._closed = False
        self._process.start()
        kind, payload = self._responses.get(timeout=start_timeout_seconds)
        if kind == "error":
            self._process.terminate()
            raise RuntimeError(f"follower process failed to start: {payload}")

    @property
    def is_alive(self) -> bool:
        return self._process.is_alive()

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        """Hand the note off and return the freshest position already available.

        Deliberately **does not wait** for this note's own answer. Blocking here
        would put the HMM back on the critical path — measured, that is worse
        than in-process, because the caller pays the compute *and* the IPC. The
        conductor instead consumes positions as they arrive (conflated
        latest-value; stale estimates are dropped), so a slow follower costs
        freshness rather than the conductor's deadline.
        """

        if self._closed:
            raise RuntimeError("follower process is closed")
        self._requests.put(("note", (note.perf_time, note.pitch, note.velocity)))
        return self._latest_update()

    def poll_update(self) -> FollowerUpdate | None:
        """Collect a position that arrived since the last note, if any.

        ``observe`` hands a note off without waiting, so the answer for the most
        recent note lands in the response queue a moment later. Without this the
        last note before a silence -- a phrase ending, a fermata, the final note
        of an entry -- would sit unconsumed until the pianist happened to play
        again. The conductor calls this every tick so position keeps flowing on
        silence as well as on input.
        """

        if self._closed:
            return None
        return self._latest_update()

    def _latest_update(self) -> FollowerUpdate | None:
        """Drain everything pending and keep only the newest position."""

        payload = None
        while True:
            try:
                kind, item = self._responses.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                raise RuntimeError(f"follower process error: {item}")
            if kind == "update" and item is not None:
                payload = item
        if payload is None:
            return None
        perf_time, score_beat, reference_beat, confidence, raw_state = payload
        return FollowerUpdate(
            perf_time=perf_time,
            score_beat=score_beat,
            reference_beat=reference_beat,
            confidence=confidence,
            raw_state=raw_state,
        )

    def reposition_for_entry(
        self, *, score_beat: float, reference_beat: float | None
    ) -> None:
        if self._closed:
            raise RuntimeError("follower process is closed")
        # Fire-and-forget: the request queue is FIFO, so the child applies this
        # before any later note. Waiting would only add latency at the cue-in.
        self._requests.put(("reposition", (score_beat, reference_beat)))

    def relocalize(self) -> None:
        if self._closed:
            raise RuntimeError("follower process is closed")
        # Fire-and-forget, FIFO-ordered before later notes, same as reposition.
        self._requests.put(("relocalize", None))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._requests.put(_SENTINEL)
        except (OSError, ValueError):
            pass
        self._process.join(timeout=2.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)


__all__ = ["FollowerSpec", "ProcessFollower"]
