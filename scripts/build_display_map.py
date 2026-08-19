#!/usr/bin/env python3
"""Join sequential PDF layout boxes explicitly to a Bundle v2 timeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aimusic.accompaniment.bundle_v2 import (
    DisplayMappingDocument,
    DisplayMeasureBox,
    MappingKind,
    TimelineDocument,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("display_layout", type=Path)
    parser.add_argument("timeline", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--mapping-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--confidence", type=float, default=0.8)
    args = parser.parse_args(argv)

    layout = json.loads(args.display_layout.read_text(encoding="utf-8"))
    if layout.get("schema_version") != 2:
        raise ValueError("display layout must use schema version 2")
    timeline = TimelineDocument.model_validate_json(args.timeline.read_text(encoding="utf-8"))
    raw_boxes = layout["boxes"]
    if len(raw_boxes) != len(timeline.measures):
        raise ValueError("layout box count must equal timeline measure count")

    boxes = []
    for raw, measure in zip(raw_boxes, timeline.measures):
        if int(raw["box_index"]) != measure.measure_index:
            raise ValueError("layout box and canonical measure indexes must match")
        boxes.append(
            DisplayMeasureBox(
                page=raw["page"],
                system=raw["system"],
                x0=raw["x0"],
                x1=raw["x1"],
                y0=raw["y0"],
                y1=raw["y1"],
                measure_index=measure.measure_index,
                measure_label=measure.measure_label,
                score_start_tick=measure.start_tick,
                score_end_tick=measure.end_tick,
                confidence=args.confidence,
            )
        )
    document = DisplayMappingDocument(
        schema_version=2,
        mapping_id=args.mapping_id,
        source_id=args.source_id,
        timeline_id=timeline.timeline_id,
        kind=MappingKind.DISPLAY,
        page_count=layout["page_count"],
        boxes=tuple(boxes),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(document.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(boxes)} explicit PDF-to-score boxes to {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
