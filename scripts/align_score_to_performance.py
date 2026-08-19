#!/usr/bin/env python3
"""Align a symbolic score to a performance recording with Parangonar.

Independent of any existing beat map by construction: nothing here reads the
warp it is meant to check, so it can contradict it.

Validate the matcher before trusting it. `--validate` aligns the score against
a MIDI exported from that same score, where the correspondence is exact, and
refuses to proceed below a match threshold. Without that step a broken matcher
and a broken beat map are indistinguishable.

Piece-agnostic: pass any MusicXML and any performance MIDI.

Matcher choice matters on noisy (OMR-derived) input: `--matcher dualdtw`
(the default) held 0.04s median disagreement with a known-good beat map on
an Audiveris export where `automatic` drifted 60-85s by accumulating small
local errors with no global constraint to catch it -- see
docs/sources/parangonar.md, "Matcher Choice Matters on OMR Input". Prefer
`automatic` only if you've specifically confirmed it does better on your
input; don't switch it back by default.

See .agents/skills/score-localization/SKILL.md.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np, partitura, parangonar

MIN_VALIDATION_MATCH = 0.95

_MATCHERS = {
    "dualdtw": parangonar.DualDTWNoteMatcher,
    "automatic": parangonar.AutomaticNoteMatcher,
}


def _note_array(parts) -> np.ndarray:
    """Concatenate per-part note arrays instead of asking partitura to combine them.

    partitura's Score.note_array()/note_array_from_part_list rejects parts with
    differing <divisions> ("Note array from parts with multiple divisions is
    not supported") -- legal MusicXML that Audiveris emits routinely (a new
    divisions value per measure/system to keep triplet durations integral).
    Per-part note_array() handles intra-part divisions changes fine, so build
    each part's array separately and concatenate. include_grace_notes=True
    because DualDTWNoteMatcher requires an is_grace field.
    """
    arrays = [p.note_array(include_grace_notes=True) for p in parts]
    combined = np.concatenate(arrays)
    combined["id"] = [
        f"{p.id}-{note_id}" for p, arr in zip(parts, arrays) for note_id in arr["id"]
    ]
    return combined


def align(score_path: Path, perf_path: Path, matcher_name: str = "dualdtw"):
    score = partitura.load_score(str(score_path))
    perf = partitura.load_performance_midi(str(perf_path))
    sna = _note_array(score.parts)
    pna = perf.note_array(include_grace_notes=True)
    matcher_cls = _MATCHERS[matcher_name]
    if matcher_name == "automatic":
        alignment = matcher_cls()(sna, pna, verbose_time=False)
    else:
        alignment = matcher_cls()(sna, pna)
    sid = {n["id"]: n for n in sna}
    pid = {n["id"]: n for n in pna}
    pairs = sorted(
        (float(sid[a["score_id"]]["onset_beat"]), float(pid[a["performance_id"]]["onset_sec"]))
        for a in alignment
        if a["label"] == "match" and a["score_id"] in sid and a["performance_id"] in pid
    )
    return score, sna, pna, alignment, pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("score", type=Path, help="MusicXML (.mxl/.musicxml) supplying measures and beats")
    ap.add_argument("performance", type=Path, nargs="?",
                    help="performance MIDI; omitted when --validate derives it from the score")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--validate", action="store_true",
                    help="align the score against its own exported MIDI (exact by construction); "
                         "the MIDI is expected beside the score with a .mid suffix")
    ap.add_argument("--min-match", type=float, default=MIN_VALIDATION_MATCH)
    ap.add_argument("--matcher", choices=sorted(_MATCHERS), default="dualdtw",
                     help="parangonar matcher (default: dualdtw -- see module docstring)")
    args = ap.parse_args()
    perf = args.score.with_suffix(".mid") if args.validate else args.performance
    if perf is None:
        raise SystemExit("a performance MIDI is required unless --validate is given")
    score, sna, pna, alignment, pairs = align(args.score, perf, args.matcher)
    matched = sum(1 for a in alignment if a["label"] == "match")
    print(f"score notes {len(sna)}  performance notes {len(pna)}  matched {matched} "
          f"({100 * matched / max(1, len(sna)):.1f}% of score)")
    if args.validate and matched / max(1, len(sna)) < args.min_match:
        print("VALIDATION FAILED: matcher cannot align a score to its own MIDI")
        return 1
    part = score.parts[0]
    qb = np.array([b for b, _ in pairs]); ts = np.array([t for _, t in pairs])
    downbeats = {
        m.number: float(np.interp(part.beat_map(m.start.t), qb, ts))
        for m in part.iter_all(partitura.score.Measure)
    }
    if args.out:
        args.out.write_text(json.dumps({
            "source": f"parangonar_{args.matcher}_note_matcher",
            "score": args.score.name, "performance": perf.name,
            "matched_notes": len(pairs), "measure_downbeat_seconds": downbeats,
        }, indent=1), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
