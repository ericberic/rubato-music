#!/usr/bin/env python3
"""Replay a recorded run's follower updates through candidate tempo settings.

The performer reported the cursor "bouncing around, never finding where I am"
for over a minute. The follower was not the culprit: in the run of 2026-08-02 it
moved monotonically with zero backward jumps in 1020 updates. The transport was.
It extrapolates the cursor between updates as

    cursor = anchor_beat + (now - anchor_time) / beat_period

so an unstable period makes the cursor race ahead and snap back. That run's
tempo estimate ranged 15-203 BPM in a Larghetto, with single-update jumps of
87 BPM.

This replays the *recorded* follower updates -- the real evidence the tempo
model saw -- through different settings, so candidates are compared on identical
input rather than on separate performances.

The headline metric is prediction error at each update: how far the extrapolated
cursor had drifted from where the follower says the performer actually is. That
error is exactly the jump the eye sees when the cursor corrects.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aimusic.accompaniment.following import FollowerUpdate  # noqa: E402
from aimusic.accompaniment.tempo_model import OnlineTempoModel  # noqa: E402


@dataclass(frozen=True)
class Candidate:
    name: str
    rationale: str
    settings: dict


def load_follower_updates(trace: Path) -> list[tuple[float, float]]:
    """(monotonic_time, score_beat) for every follower update in the run."""

    updates: list[tuple[float, float]] = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        if '"follower"' not in line:
            continue
        row = json.loads(line)
        if row.get("type") != "follower":
            continue
        updates.append((float(row["monotonic_time"]), float(row["score_beat"])))
    return updates


def extrapolate(rule: str, elapsed: float, period: float) -> float:
    """Beats the cursor advances after `elapsed` seconds of no new evidence.

    Tempo settings turned out not to be the lever: across pauses the cursor
    drifted a median 0.6 beats whichever tempo the model held, because the
    transport keeps running at full rate on no evidence at all. These rules
    change what it does with silence instead.
    """

    beats = elapsed / period
    if rule == "free":
        return beats                       # what ships: run forever
    if rule == "freeze-1s":
        # Move for a beat's worth of silence, then wait to be told.
        return min(beats, 1.0 / period * min(elapsed, 1.0)) if elapsed > 1.0 else beats
    if rule == "cap-1beat":
        return min(beats, 1.0)
    if rule == "cap-2beats":
        return min(beats, 2.0)
    if rule == "decay":
        # Full rate briefly, then asymptotically slower: the longer the silence,
        # the less the estimate deserves to be trusted.
        import math

        return 2.0 * (1.0 - math.exp(-beats / 2.0))
    raise ValueError(rule)


def run_candidate(updates: list[tuple[float, float]], settings: dict) -> dict:
    """Feed the recorded evidence to one configuration and measure the result."""

    rule = settings.pop("_rule", "free") if isinstance(settings, dict) else "free"
    model = OnlineTempoModel(**settings)
    periods: list[float] = []
    errors: list[float] = []
    anchor: tuple[float, float] | None = None
    period = 0.0

    for now, beat in updates:
        if anchor is not None and period > 0:
            elapsed = now - anchor[0]
            predicted = anchor[1] + extrapolate(rule, elapsed, period)
            # Record the gap too. Extrapolation only matters across silence:
            # between two onsets 150 ms apart any tempo gives the same answer,
            # so averaging over every update hides the effect entirely.
            errors.append((elapsed, predicted - beat))
        state = model.update(
            FollowerUpdate(perf_time=now, score_beat=beat, confidence=0.5)
        )
        period = state.beat_period_seconds
        periods.append(period)
        anchor = (now, beat)

    bpm = [60.0 / p for p in periods if p > 0]
    swings = [abs(b - a) for a, b in zip(bpm, bpm[1:])]
    absolute = [abs(e) for _, e in errors]
    # Gaps of half a second or more: the performer is between gestures and the
    # cursor is running on the tempo estimate alone. This is where a wrong
    # tempo becomes visible movement.
    gapped = [abs(e) for g, e in errors if g >= 0.5]
    overshoot = [e for _, e in errors if e > 0]
    return {
        "bpm_p10": sorted(bpm)[len(bpm) // 10],
        "bpm_median": statistics.median(bpm),
        "bpm_p90": sorted(bpm)[9 * len(bpm) // 10],
        "bpm_range": max(bpm) - min(bpm),
        "swing_p90": sorted(swings)[int(0.9 * len(swings))] if swings else 0.0,
        "swing_max": max(swings) if swings else 0.0,
        "err_median": statistics.median(absolute),
        "err_p90": sorted(absolute)[int(0.9 * len(absolute))],
        "err_max": max(absolute),
        "gap_n": len(gapped),
        "gap_median": statistics.median(gapped) if gapped else 0.0,
        "gap_p90": sorted(gapped)[int(0.9 * len(gapped))] if gapped else 0.0,
        "gap_max": max(gapped) if gapped else 0.0,
        "overshoot_over_1_beat": sum(1 for e in overshoot if e > 1.0),
        "updates": len(updates),
    }


def candidates(lead_bpm: float) -> list[Candidate]:
    """Each isolates one hypothesis, plus the combination of whatever helps."""

    base = dict(initial_tempo_bpm=lead_bpm)
    return [
        Candidate(
            "current",
            "what shipped: 20-400 BPM, alpha 0.25, 0.5-beat baseline",
            dict(base),
        ),
        Candidate(
            "narrow-band",
            "reject tempi a Larghetto cannot be: 0.5x-2x the rehearsed pace",
            dict(base, min_tempo_bpm=lead_bpm * 0.5, max_tempo_bpm=lead_bpm * 2.0),
        ),
        Candidate(
            "slow-response",
            "same evidence, less weight per update (alpha 0.25 -> 0.08)",
            dict(base, smoothing_alpha=0.08),
        ),
        Candidate(
            "longer-baseline",
            "no tempo from a fragment: need 2 beats and 0.5 s, not 0.5 and 0.12",
            dict(base, min_progress_beats=2.0, min_elapsed_seconds=0.5),
        ),
        Candidate(
            "narrow+baseline",
            "both structural fixes, response left alone",
            dict(
                base,
                min_tempo_bpm=lead_bpm * 0.5,
                max_tempo_bpm=lead_bpm * 2.0,
                min_progress_beats=2.0,
                min_elapsed_seconds=0.5,
            ),
        ),
        Candidate(
            "all-three",
            "narrow band, long baseline, slow response",
            dict(
                base,
                min_tempo_bpm=lead_bpm * 0.5,
                max_tempo_bpm=lead_bpm * 2.0,
                min_progress_beats=2.0,
                min_elapsed_seconds=0.5,
                smoothing_alpha=0.08,
            ),
        ),
        Candidate("cap-1beat", "never run more than 1 beat past the last note",
                  dict(base, _rule="cap-1beat")),
        Candidate("cap-2beats", "never run more than 2 beats past the last note",
                  dict(base, _rule="cap-2beats")),
        Candidate("decay", "slow asymptotically the longer the silence lasts",
                  dict(base, _rule="decay")),
        Candidate("cap1+all-three", "the best tempo settings plus a 1-beat cap",
                  dict(base, _rule="cap-1beat", min_tempo_bpm=lead_bpm * 0.5,
                       max_tempo_bpm=lead_bpm * 2.0, min_progress_beats=2.0,
                       min_elapsed_seconds=0.5, smoothing_alpha=0.08)),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--lead-bpm", type=float, default=62.77,
                    help="the orchestra's rehearsed pace for this run")
    args = ap.parse_args()

    updates = load_follower_updates(Path(args.trace))
    if len(updates) < 50:
        print(f"only {len(updates)} follower updates; need a real run")
        return 1
    span = updates[-1][0] - updates[0][0]
    print(f"{len(updates)} follower updates over {span:.0f}s, "
          f"beats {updates[0][1]:.0f} to {updates[-1][1]:.0f}\n")

    rows = [(c, run_candidate(updates, c.settings)) for c in candidates(args.lead_bpm)]

    head = (f"{'candidate':18s}{'bpm range':>11s}{'swing p90':>11s}"
            f"{'gap>0.5s: med':>15s}{'p90':>8s}{'max':>8s}{'  |  all: med':>14s}{'p90':>8s}")
    print(head)
    print("-" * len(head))
    for c, m in rows:
        print(f"{c.name:18s}{m['bpm_range']:11.0f}{m['swing_p90']:11.1f}"
              f"{m['gap_median']:15.3f}{m['gap_p90']:8.3f}{m['gap_max']:8.2f}"
              f"{m['err_median']:14.3f}{m['err_p90']:8.3f}")

    print("\nrationale:")
    for c, _ in rows:
        print(f"  {c.name:18s} {c.rationale}")
    print("\nerr = beats between where the cursor had drifted to and where the")
    print("performer actually was, at each follower update. That gap is the jump")
    print("the eye sees when the cursor corrects. Lower is steadier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
