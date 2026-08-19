#!/usr/bin/env python3
"""Fit the cadenza handoff detector from recorded demonstrations.

All the expensive work is here, offline. What ships is 72 floats and one dot
product per note. Averaging several passes is what separates an invariant from
an ornament -- a single pass cannot, since a pitch that looks decisive once may
occur four times.

``--validate`` runs leave-one-out through the *shipped* CadenzaModel and
CadenzaDetector rather than through this script's own arrays, so the numbers it
prints are the runtime's behaviour and not a reimplementation that happens to
agree.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aimusic.accompaniment.cadenza_detector import (  # noqa: E402
    CHROMA_BINS,
    CadenzaDetector,
    CadenzaModel,
    DecayingChroma,
)

TAUS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
LABEL_LEAD_BEATS = 1.0  # "the last beat"; longer horizons fire early under rubato


def segment(notes: list[dict], gap: float = 3.0) -> list[list[dict]]:
    out: list[list[dict]] = [[notes[0]]]
    for prev, cur in zip(notes, notes[1:]):
        if cur["beat"] - prev["beat"] > gap:
            out.append([])
        out[-1].append(cur)
    return out


def rows(seq: list[tuple[float, int]]) -> np.ndarray:
    chroma = DecayingChroma(TAUS)
    return np.array([chroma.observe(p, b) for b, p in seq])


def load_negatives(path: Path | None) -> list[tuple[float, int]]:
    if path is None or not path.is_file():
        return []
    import mido

    midi = mido.MidiFile(path)
    ev: list[tuple[float, int]] = []
    for track in midi.tracks:
        t = 0
        for m in track:
            t += m.time
            if m.type == "note_on" and m.velocity > 0:
                ev.append((t / midi.ticks_per_beat, m.note))
    ev.sort()
    return ev


def fit(passes, negatives, lead):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    X, y = [], []
    for ps in passes:
        seq = [(n["beat"], n["pitch"]) for n in ps]
        origin, end = seq[0][0], seq[-1][0] - seq[0][0]
        X.append(rows(seq))
        y += [1 if (b - origin) >= end - lead else 0 for b, _ in seq]
    if negatives:
        X.append(rows(negatives))
        y += [0] * len(negatives)
    X = np.vstack(X)
    scaler = StandardScaler().fit(X)
    clf = LogisticRegression(max_iter=3000, C=2.0).fit(scaler.transform(X), np.array(y))
    return scaler, clf


def build(passes, negatives, args, lead) -> CadenzaModel:
    scaler, clf = fit(passes, negatives, lead)
    return CadenzaModel(
        taus=TAUS,
        coef=clf.coef_[0],
        intercept=float(clf.intercept_[0]),
        mean=scaler.mean_,
        scale=scaler.scale_,
        region_start_beat=args.region_start_beat,
        handoff_beat=args.handoff_beat,
        hand_back_beat=args.hand_back_beat,
        label_lead_beats=lead,
        landing_pitches=tuple(args.landing_pitches),
        source=Path(args.demos).name,
    )


def validate(passes, negatives, args, tmp: Path) -> int:
    """Leave-one-out, driven through the shipped runtime classes."""

    print(f"Leave-one-out over {len(passes)} demonstrations, via CadenzaDetector\n")
    warn, prem, miss, retracted = [], 0, 0, 0
    for held in range(len(passes)):
        train = [p for i, p in enumerate(passes) if i != held]
        model = build(train, negatives, args, LABEL_LEAD_BEATS)
        model.save(tmp)
        model = CadenzaModel.load(tmp)  # exercise the persistence round trip

        seq = [(n["beat"], n["pitch"]) for n in passes[held]]
        origin, end = seq[0][0], seq[-1][0] - seq[0][0]
        det = CadenzaDetector(model)
        det.maybe_arm(model.region_start_beat, seq[0][0])
        fired_at = None
        for b, p in seq:
            fire = det.observe(p, b)
            if fire is None:
                continue
            if fire.kind == "handoff" and fired_at is None:
                fired_at = b - origin
                if fired_at < end - LABEL_LEAD_BEATS - 0.5:
                    prem += 1
            elif fire.kind == "retracted":
                retracted += 1
        if fired_at is None:
            miss += 1
            print(f"  pass {held}: MISSED")
        else:
            warn.append(end - fired_at)
            print(f"  pass {held}: fired {end - fired_at:.3f} beats before the landing note")

    fp = 0
    if negatives:
        model = build(passes, negatives, args, LABEL_LEAD_BEATS)
        det = CadenzaDetector(model)
        det.maybe_arm(model.region_start_beat, negatives[0][0])
        fp = sum(1 for b, p in negatives if (f := det.observe(p, b)) and f.kind == "handoff")

    w = np.array(warn) if warn else np.array([np.nan])
    print(f"\n  warning before landing note: median {np.nanmedian(w):.3f} beats")
    print(f"  missed {miss}/{len(passes)} | premature {prem} | retracted {retracted}")
    print(f"  false positives on non-cadenza playing: {fp}/{len(negatives)}")
    ok = miss == 0 and prem == 0
    print(f"\n  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demos", required=True, help="recorded demonstrations JSON")
    ap.add_argument("--negatives", default=None, help="MIDI of non-cadenza playing")
    ap.add_argument("--out", default=None, help="where to write the model")
    ap.add_argument("--region-start-beat", type=float, required=True)
    ap.add_argument("--handoff-beat", type=float, required=True)
    ap.add_argument("--hand-back-beat", type=float, required=True)
    ap.add_argument("--landing-pitches", type=int, nargs="*", default=[56, 90],
                    help="MIDI pitches struck together at the cadenza's landing "
                         "chord; the handoff fires on these once confident")
    ap.add_argument("--drop-first-pass", action="store_true",
                    help="the performer entered on the wrong beat in take 1")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    notes = json.loads(Path(args.demos).read_text(encoding="utf-8"))["notes"]
    passes = segment(notes)
    if args.drop_first_pass:
        passes = passes[1:]
    negatives = load_negatives(Path(args.negatives) if args.negatives else None)
    print(f"{len(passes)} demonstrations, {len(negatives)} negative notes\n")

    if args.validate:
        rc = validate(passes, negatives, args, Path("/tmp/_cadenza_validate.json"))
        if rc:
            return rc
    if args.out:
        build(passes, negatives, args, LABEL_LEAD_BEATS).save(args.out)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
