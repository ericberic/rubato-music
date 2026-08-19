"""Detect the handoff at the end of a free passage, so the orchestra can enter.

A cadenza defeats a score follower: the performer and the notation share no
reliable note correspondence, and the reference recording reaches only 0.32
pitch-sequence similarity with the printed part. Position-gated anchors deadlock
here -- a frozen estimate is far from every future anchor, so nothing fires and
it stays frozen. That was observed live for 16.4 seconds while twenty gestures
went past.

The reduction that makes this tractable: nothing plays *during* the cadenza, so
tracking the beat through it serves no consumer. The orchestra needs two
instants -- did we enter, and are we at the handoff. That collapses a 169-class
position problem to a binary one against the same training data.

Three properties, each measured rather than assumed (see
``scripts/probe_handoff_pernote.py`` and ``docs/LOG.md``):

- **Per-note, never clustered.** Chord clustering was tried, to exploit the
  performer's observation that strike sizes run 2222-33 across the beat. The
  periodicity is real but no threshold segments it stably: inter-note gaps are
  bimodal (268 below 20 ms, 244 above 80 ms) yet their tails cross under tempo
  change, and every window from 10 to 50 ms was unstable on 7 of 8 passes.
  Decaying pitch-class histograms need no segmentation at all.
- **Tempo cancels.** Features are ratios within a histogram, so 0.7x, 1.4x, a
  ritardando and sinusoidal push-pull all give 0 premature fires and 0 misses.

There is deliberately no mechanism to reverse a handoff. One existed and was
removed: no premature fire was ever observed from real playing -- every case it
"caught" had been forced by setting detector state directly -- and its only
firing on a live run was a false positive. It also could not have worked, since
reversing set a message and never touched transport. The structure already
provides the protection the veto was imagined to give: the hand-back beat is the
downbeat of the interlude, so the orchestra begins there however early the
passage was recognised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np

CHROMA_BINS = 12

# The detector fires ~0.3 beats BEFORE the landing note, so the cadenza's own
# closing notes still arrive after it. The veto must not count those: it opens
# only once the passage should be over. Without this grace every correct fire
# retracts itself on the landing dyad -- which is exactly what happened, and
# which the probe scripts could not show because they measured silence after the
# true landing rather than after the fire.


class CadenzaState(Enum):
    IDLE = "idle"
    ARMED = "armed"
    FIRED = "fired"
    GAVE_UP = "gave_up"


@dataclass(frozen=True)
class CadenzaModel:
    """A trained detector. Twelve floats per time constant, one linear layer.

    Built offline by ``scripts/train_cadenza_detector.py``. Deliberately a
    plain dataclass of arrays: the online path must not import scikit-learn.
    """

    taus: tuple[float, ...]
    coef: np.ndarray
    intercept: float
    mean: np.ndarray
    scale: np.ndarray
    region_start_beat: float
    handoff_beat: float
    hand_back_beat: float
    label_lead_beats: float
    # Pitches struck together that mark the true handoff instant -- the cadenza's
    # landing chord. The classifier says "near the end"; these say "now". Empty
    # falls back to firing on confidence alone.
    landing_pitches: tuple[int, ...] = ()
    landing_window_seconds: float = 0.18
    source: str = ""

    def __post_init__(self) -> None:
        n = len(self.taus) * CHROMA_BINS
        for name in ("coef", "mean", "scale"):
            if getattr(self, name).shape != (n,):
                raise ValueError(
                    f"{name} must have {n} entries for {len(self.taus)} time constants"
                )
        if self.handoff_beat <= self.region_start_beat:
            raise ValueError("handoff must fall after the region start")

    @classmethod
    def load(cls, path: str | Path) -> "CadenzaModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            taus=tuple(payload["taus"]),
            coef=np.asarray(payload["coef"], dtype=float),
            intercept=float(payload["intercept"]),
            mean=np.asarray(payload["mean"], dtype=float),
            scale=np.asarray(payload["scale"], dtype=float),
            region_start_beat=float(payload["region_start_beat"]),
            handoff_beat=float(payload["handoff_beat"]),
            hand_back_beat=float(payload["hand_back_beat"]),
            label_lead_beats=float(payload["label_lead_beats"]),
            landing_pitches=tuple(payload.get("landing_pitches", ())),
            landing_window_seconds=float(payload.get("landing_window_seconds", 0.18)),
            source=payload.get("source", ""),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "taus": list(self.taus),
                    "coef": self.coef.tolist(),
                    "intercept": self.intercept,
                    "mean": self.mean.tolist(),
                    "scale": self.scale.tolist(),
                    "region_start_beat": self.region_start_beat,
                    "handoff_beat": self.handoff_beat,
                    "hand_back_beat": self.hand_back_beat,
                    "label_lead_beats": self.label_lead_beats,
                    "landing_pitches": list(self.landing_pitches),
                    "landing_window_seconds": self.landing_window_seconds,
                    "source": self.source,
                },
                indent=1,
            ),
            encoding="utf-8",
        )


@dataclass(frozen=True)
class CadenzaFire:
    kind: str  # "handoff" | "gave_up"
    canonical_beat: float
    perf_time: float
    probability: float


class DecayingChroma:
    """Multi-scale pitch-class memory, updated per note.

    Six time constants from a quarter beat to eight span the range from "this
    gesture" to "this whole passage". Each scale is L1-normalised, which is what
    makes the representation tempo-invariant: a ratio within a histogram does not
    care how fast the histogram was filled.
    """

    def __init__(self, taus: tuple[float, ...]) -> None:
        self._taus = np.asarray(taus, dtype=float)
        self._v = np.zeros((len(taus), CHROMA_BINS))
        self._last: float | None = None

    def reset(self) -> None:
        self._v[:] = 0.0
        self._last = None

    def observe(self, pitch: int, now: float) -> np.ndarray:
        if self._last is not None:
            dt = now - self._last
            if dt > 0:
                self._v *= np.exp(-dt / self._taus)[:, None]
        self._v[:, pitch % CHROMA_BINS] += 1.0
        self._last = now
        totals = self._v.sum(axis=1, keepdims=True)
        return (self._v / np.maximum(totals, 1e-9)).ravel()


class CadenzaDetector:
    """Arms on position once, then runs on the performer's notes alone.

    Position is consulted only to arm. Once armed the detector never reads the
    follower again, which is the whole point: the estimate it exists to correct
    is exactly the one that has stopped moving.
    """

    def __init__(
        self,
        model: CadenzaModel,
        arm_within_beats: float = 2.0,
        threshold: float = 0.5,
        give_up_after_beats: float = 24.0,
        beat_period_seconds: float = 1.0,
    ) -> None:
        self._model = model
        self._arm_within = arm_within_beats
        self._threshold = threshold
        self._give_up_after = give_up_after_beats
        # Observation times arrive in SECONDS while every threshold here is in
        # beats. The demonstrations were recorded at 60 BPM, where the two
        # coincide, so the difference stayed invisible until a live run at a
        # Larghetto: a 0.75-beat veto grace became 0.5 of a beat and the
        # cadenza's own closing notes retracted every correct handoff.
        self._beat_period = beat_period_seconds
        self._chroma = DecayingChroma(model.taus)
        self._state = CadenzaState.IDLE
        # Set once the classifier is confident the passage is ending. From then
        # the actual landing chord -- not the classifier crossing threshold --
        # fires the handoff, so the orchestra enters WITH the performer's G#/F#
        # rather than a beat early.
        self._committed = False
        self._recent: list[tuple[float, int]] = []
        self._last_probability = 0.0
        self._armed_at: float | None = None
        self._fired_at: float | None = None

    @property
    def state(self) -> CadenzaState:
        return self._state


    def maybe_arm(self, score_beat: float, now: float) -> bool:
        """The single use of follower position."""

        if self._state is not CadenzaState.IDLE:
            return False
        if abs(score_beat - self._model.region_start_beat) > self._arm_within:
            return False
        self._chroma.reset()
        self._state = CadenzaState.ARMED
        self._armed_at = now
        return True

    def observe(self, pitch: int, now: float) -> CadenzaFire | None:
        if self._state is CadenzaState.ARMED:
            return self._while_armed(pitch, now)
        return None

    def _while_armed(self, pitch: int, now: float) -> CadenzaFire | None:
        x = self._chroma.observe(pitch, now)
        z = float(self._model.coef @ ((x - self._model.mean) / self._model.scale))
        p = 1.0 / (1.0 + np.exp(-(z + self._model.intercept)))
        if p > self._threshold:
            self._committed = True
        self._last_probability = p

        landing = self._model.landing_pitches
        if landing:
            # Watch for the landing chord. Once committed, the moment all its
            # pitches have sounded together IS the handoff -- the orchestra
            # enters exactly with the performer, not a classifier-beat early.
            self._recent.append((now, pitch))
            window = self._model.landing_window_seconds
            self._recent = [(t, q) for t, q in self._recent if now - t <= window]
            struck = {q for _, q in self._recent}
            if self._committed and all(pc in struck for pc in landing):
                return self._fire(now, p)
        elif p > self._threshold:
            # No landing chord configured: fall back to firing on confidence.
            return self._fire(now, p)

        if (
            self._armed_at is not None
            and now - self._armed_at > self._give_up_after * self._beat_period
        ):
            # A missed cue must not strand the orchestra. If we were confident
            # the passage was ending but never saw a clean landing (the
            # performer fluffed it), hand off anyway; otherwise report a miss.
            self._state = CadenzaState.GAVE_UP
            kind = "handoff" if self._committed else "gave_up"
            return CadenzaFire(kind, self._model.hand_back_beat, now,
                               self._last_probability)
        return None

    def _fire(self, now: float, probability: float) -> CadenzaFire:
        self._state = CadenzaState.FIRED
        self._fired_at = now
        # Hand back where the ORCHESTRA resumes (hand_back_beat), not where the
        # cadenza was recognised: the recognition beat is inside the free region
        # and snapping there leaves the transport in FREE.
        return CadenzaFire("handoff", self._model.hand_back_beat, now, probability)

