#!/usr/bin/env python3
"""Build `measure_boxes.json` for a score bundle (design doc §3.3, roadmap item 7).

Level 1 only: per-page system y0/y1 bands plus first/last measure number per
system, derived from MusicXML `<print>` system/page-break markers with an
even vertical split within each page. See
`aimusic.takes.measure_boxes` for the method and its rationale.

This is a rerunnable, per-bundle build tool, not runtime code -- run it again
whenever a bundle's score.mxl/score.pdf changes.

Usage:
    uv run --extra dev python scripts/build_measure_boxes.py
    uv run --extra dev python scripts/build_measure_boxes.py --piece-id chopin_op11 --movement 1
"""

from __future__ import annotations

import argparse

from aimusic.takes.measure_boxes import write_measure_boxes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--piece-id", default="chopin_op11", help="Piece id (default: chopin_op11)")
    parser.add_argument("--movement", type=int, default=1, help="Movement number (default: 1)")
    args = parser.parse_args(argv)

    out_path = write_measure_boxes(args.piece_id, args.movement)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
