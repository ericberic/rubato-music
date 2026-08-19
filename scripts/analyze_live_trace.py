#!/usr/bin/env python3
"""Print a compact JSON diagnostic summary for a Rubato live run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aimusic.accompaniment.trace_analysis import (
    analyze_runtime_trace,
    describe_live_run_artifacts,
    resolve_runtime_trace,
)
from aimusic.core import paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trace",
        type=Path,
        nargs="?",
        help="Path to trace/runtime.jsonl (defaults to the latest live run)",
    )
    parser.add_argument("--run-id", help="Analyze runs/<run-id>/trace/runtime.jsonl")
    args = parser.parse_args()
    if args.trace is not None and args.run_id is not None:
        parser.error("pass either trace or --run-id, not both")
    try:
        trace = resolve_runtime_trace(
            args.trace,
            run_id=args.run_id,
            runs_dir=paths.runs_root(),
        )
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    summary = analyze_runtime_trace(trace)
    summary["run"] = describe_live_run_artifacts(
        trace,
        processed_dir=paths.processed_root(),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
