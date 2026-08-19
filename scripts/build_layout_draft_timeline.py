#!/usr/bin/env python3
"""Build an explicitly machine-draft score timeline from sequential layout boxes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aimusic.accompaniment.bundle_v2 import MeasureSpan, TimelineDocument, TimeSignature


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("display_layout", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--timeline-id", required=True)
    parser.add_argument("--movement-id", required=True)
    parser.add_argument("--ppq", type=int, default=960)
    parser.add_argument("--beats", type=int, default=4)
    parser.add_argument("--beat-type", type=int, default=4)
    args = parser.parse_args(argv)

    layout = json.loads(args.display_layout.read_text(encoding="utf-8"))
    if layout.get("schema_version") != 2:
        raise ValueError("display layout must use schema version 2")
    box_count = int(layout["box_count"])
    if layout.get("review_state") != "machine":
        raise ValueError("this command only converts machine layout evidence")
    duration = args.beats * args.ppq * 4 // args.beat_type
    timeline = TimelineDocument(
        schema_version=2,
        timeline_id=args.timeline_id,
        movement_id=args.movement_id,
        canonical_ppq=args.ppq,
        measures=tuple(
            MeasureSpan(
                measure_index=index,
                measure_label=str(index + 1),
                start_tick=index * duration,
                end_tick=(index + 1) * duration,
                time_signature=TimeSignature(beats=args.beats, beat_type=args.beat_type),
            )
            for index in range(box_count)
        ),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(timeline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {box_count}-measure machine draft to {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
