#!/usr/bin/env python3
"""Re-derive take -> canonical-measure alignments directly from the score.

Independent of any beat map: each take is matched against the score's piano
part with Parangonar, so a take recorded while the beat map was wrong is still
recoverable. Nothing here reads `performance_beat_map.machine.json`.

Aligns against the PIANO PART ONLY. Matching a solo recording against a full
orchestral score produces thousands of spurious "deletions" for parts the
performer never plays, which buries the real divergences.
"""
from __future__ import annotations
import argparse, collections, glob, json
from pathlib import Path
import numpy as np, partitura, parangonar


def piano_part(score, name_hint: str):
    for part in score.parts:
        if name_hint.lower() in (part.part_name or "").lower():
            return part
    return max(score.parts, key=lambda p: len(p.note_array()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("score")
    ap.add_argument("takes_glob", help="e.g. '~/.../processed/**/solo.mid'")
    ap.add_argument("--out", required=True)
    ap.add_argument("--part-name", default="piano")
    args = ap.parse_args()

    score = partitura.load_score(str(args.score))
    part = piano_part(score, args.part_name)
    sna = part.note_array()
    measures = sorted((part.beat_map(m.start.t), m.number) for m in part.iter_all(partitura.score.Measure))
    quarters = np.array([q for q, _ in measures])
    numbers = [n for _, n in measures]

    results = {}
    paths = sorted(glob.glob(str(Path(args.takes_glob).expanduser()), recursive=True))
    print(f"{len(paths)} take(s); score piano part '{part.part_name}' has {len(sna)} notes")
    for path in paths:
        take = Path(path).parent.name
        try:
            perf = partitura.load_performance_midi(path)
            pna = perf.note_array()
        except Exception as exc:  # noqa: BLE001
            print(f"  {take}: unreadable ({exc})")
            continue
        if len(pna) < 12:
            print(f"  {take}: {len(pna)} notes, too short to align")
            continue
        alignment = parangonar.AutomaticNoteMatcher()(sna, pna, verbose_time=False)
        sid = {n["id"]: n for n in sna}
        pid = {n["id"]: n for n in pna}
        pairs = sorted(
            (float(sid[a["score_id"]]["onset_beat"]), float(pid[a["performance_id"]]["onset_sec"]))
            for a in alignment
            if a["label"] == "match" and a["score_id"] in sid and a["performance_id"] in pid
        )
        counts = collections.Counter(a["label"] for a in alignment)
        if not pairs:
            print(f"  {take}: no matches")
            continue
        lo, hi = pairs[0][0], pairs[-1][0]
        covered = [n for q, n in measures if lo <= q <= hi]
        rate = counts["match"] / max(1, len(pna))
        results[take] = {
            "played_notes": len(pna),
            "matched": counts["match"],
            "match_rate": round(rate, 4),
            "insertions": counts["insertion"],
            "first_measure": covered[0] if covered else None,
            "last_measure": covered[-1] if covered else None,
            "pairs": [[round(b, 4), round(t, 4)] for b, t in pairs],
        }
        flag = "" if rate >= 0.80 else "   LOW"
        print(f"  {take}: {len(pna):>4} notes  matched {rate:6.1%}  "
              f"m.{covered[0] if covered else '?'}-{covered[-1] if covered else '?'}{flag}")
    Path(args.out).write_text(json.dumps({
        "source": "parangonar_automatic_note_matcher",
        "score": Path(args.score).name,
        "part": part.part_name,
        "takes": results,
    }, indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}: {len(results)} take(s) aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
