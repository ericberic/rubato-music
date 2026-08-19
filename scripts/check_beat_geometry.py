#!/usr/bin/env python3
"""Re-run beat inference over every measure and report regressions.

The beat->pixel algorithm is under active development, so "measure 90 looked
good last time" must be re-checked every iteration, not locked in. This recomputes
the beat positions from the score with the CURRENT algorithm and reports:

  - invariants that must always hold (every beat inside its box, monotonic);
  - the geometry-confidence distribution and which measures fell back to even
    spacing;
  - if a baseline beat map is given, which measures MOVED and by how much, so a
    change meant to help one measure cannot silently shift a good one.

    python scripts/check_beat_geometry.py \\
        --mxl <audiveris.mxl> --display-map <display_map.json> \\
        [--baseline <performance_beat_map.json>] [--focus 80-100]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aimusic.accompaniment.beat_geometry import infer_measure_beats, read_beat_glyphs  # noqa: E402


def _parse_focus(spec: str | None) -> range | None:
    if not spec:
        return None
    lo, _, hi = spec.partition("-")
    return range(int(lo), int(hi or lo) + 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mxl", required=True)
    ap.add_argument("--display-map", required=True)
    ap.add_argument("--baseline", help="a performance_beat_map.json to diff against")
    ap.add_argument("--focus", help="measure range to list in full, e.g. 80-100")
    ap.add_argument("--move-threshold", type=float, default=0.01,
                    help="page-fraction move that counts as a change")
    args = ap.parse_args()

    display = json.loads(Path(args.display_map).read_text(encoding="utf-8"))
    boxes = {int(b["measure_label"]): b for b in display["boxes"]}
    glyphs = read_beat_glyphs(Path(args.mxl))

    baseline: dict[int, list[float]] = {}
    if args.baseline:
        payload = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        for anchor in payload.get("anchors", []):
            if anchor.get("pdf_x") is not None:
                baseline.setdefault(int(anchor["measure_label"]), []).append(anchor["pdf_x"])

    focus = _parse_focus(args.focus)
    out_of_box: list[int] = []
    non_monotonic: list[int] = []
    fallback: list[int] = []
    confidences: list[float] = []
    moved: list[tuple[int, float]] = []

    for label in sorted(boxes):
        box = boxes[label]
        measure = glyphs.get(label)
        inf = infer_measure_beats(
            list(measure.glyphs) if measure else [],
            box_x0=box["x0"],
            box_x1=box["x1"],
            new_system=measure.new_system if measure else False,
        )
        xs = inf.beat_x
        confidences.append(inf.confidence)
        if inf.method == "even_margins":
            fallback.append(label)
        if any(x < box["x0"] - 1e-6 or x > box["x1"] + 1e-6 for x in xs):
            out_of_box.append(label)
        if list(xs) != sorted(xs):
            non_monotonic.append(label)
        if label in baseline and len(baseline[label]) == len(xs):
            delta = max(abs(a - b) for a, b in zip(xs, baseline[label]))
            if delta > args.move_threshold:
                moved.append((label, delta))
        if focus and label in focus:
            flag = "" if inf.method == "staff" else "  <-- even-spacing fallback"
            print(f"  m{label:>3}  staff={str(inf.staff):8}  conf={inf.confidence:.2f}  "
                  f"x={[round(x, 3) for x in xs]}{flag}")

    print(f"\n{len(boxes)} measures")
    print(f"  out of box (must be 0):     {len(out_of_box)} {out_of_box or ''}")
    print(f"  non-monotonic (must be 0):  {len(non_monotonic)} {non_monotonic or ''}")
    print(f"  even-spacing fallback:      {len(fallback)} {fallback}")
    print(f"  confidence: median {statistics.median(confidences):.2f}  "
          f"min {min(confidences):.2f}  below 0.5: "
          f"{sum(1 for c in confidences if c < 0.5)}")
    if args.baseline:
        moved.sort(key=lambda m: -m[1])
        print(f"\n  moved vs baseline (> {args.move_threshold}): {len(moved)}")
        for label, delta in moved[:20]:
            print(f"    m{label:>3}  by {delta:.3f} page-fraction")
    return 1 if out_of_box or non_monotonic else 0


if __name__ == "__main__":
    raise SystemExit(main())
