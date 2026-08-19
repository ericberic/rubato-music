#!/usr/bin/env python3
"""Emit a `measure_boxes` JSON straight from the beat-geometry algorithm.

The `/measure_boxes` API composes the same result from a *performance beat map*,
which couples beat geometry to the timing/alignment pipeline (and falls back to
even spacing for any measure the timing pipeline skipped -- e.g. the closing
tutti). This tool bypasses timing entirely: it runs
`beat_geometry.infer_measure_beats` over the Audiveris MusicXML for every display
box and prints the exact schema `scripts/render_measure_geometry_pdf.py` reads.

Use it to *review the geometry algorithm itself* on a given MusicXML -- most
usefully after repairing the transcription so the previously-missing measures
(Joseffy m.114-126) carry real note-anchored beats instead of even margins.

    uv run python scripts/build_beat_geometry_boxes.py \
        --mxl <audiveris.mxl> \
        --display-map data/scores/chopin_op11_movement_2/derived/display_map.machine.json \
        --pdf data/scores/chopin_op11_movement_2/source/joseffy_reduction_movement2.pdf \
        --out /tmp/measure_boxes.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aimusic.accompaniment.beat_geometry import (  # noqa: E402
    infer_measure_beats,
    read_beat_glyphs,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mxl", required=True, help="Audiveris MusicXML (.mxl)")
    ap.add_argument("--display-map", required=True, help="display_map.machine.json")
    ap.add_argument("--pdf", required=True, help="score PDF (recorded in the output, not read)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    display = json.loads(Path(args.display_map).read_text(encoding="utf-8"))
    glyphs = read_beat_glyphs(Path(args.mxl))

    grouped: dict[tuple[int, int], list[dict]] = {}
    for box in sorted(display["boxes"], key=lambda b: b["measure_index"]):
        label = int(box["measure_label"])
        measure = glyphs.get(label)
        inf = infer_measure_beats(
            list(measure.glyphs) if measure else [],
            box_x0=box["x0"],
            box_x1=box["x1"],
            new_system=measure.new_system if measure else False,
        )
        beats = [
            {
                "beat_in_measure": float(beat),
                "x": inf.beat_x[beat],
                "confidence": inf.confidence,
                # A real note sat on this beat vs. interpolated/even-spaced.
                "anchored": inf.anchored[beat],
                "staff": inf.staff,
            }
            for beat in range(len(inf.beat_x))
        ]
        grouped.setdefault((box["page"], box["system"]), []).append(
            {
                "measure": label,
                "x0": box["x0"],
                "x1": box["x1"],
                "y0": box["y0"],
                "y1": box["y1"],
                "beats": beats,
                "beat_staff": inf.staff,
            }
        )

    pages: dict[int, list[dict]] = {}
    for (page, _system), measures in sorted(grouped.items()):
        numbers = [m["measure"] for m in measures]
        pages.setdefault(page, []).append(
            {
                "y0": min(m["y0"] for m in measures),
                "y1": max(m["y1"] for m in measures),
                "first_measure": min(numbers),
                "last_measure": max(numbers),
                "measures": [
                    {"measure": m["measure"], "x0": m["x0"], "x1": m["x1"],
                     "beats": m["beats"], "beat_staff": m["beat_staff"]}
                    for m in measures
                ],
            }
        )

    payload = {
        "pdf": Path(args.pdf).name,
        "page_count": max(pages),
        "level": 2,
        "method": "beat-geometry-direct",
        "review_state": "machine",
        "pages": [{"page": page, "systems": systems} for page, systems in sorted(pages.items())],
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    total = sum(len(s["measures"]) for ss in pages.values() for s in ss)
    print(f"wrote {args.out}: {len(pages)} pages, {total} measures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
