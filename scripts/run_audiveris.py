#!/usr/bin/env python3
"""Transcribe a scanned score PDF with Audiveris as a build-time step."""

from __future__ import annotations

import argparse
from pathlib import Path

from aimusic.accompaniment.audiveris import extract_score_layout, transcribe_score_pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="also recognize notes/rhythms and export MusicXML (requires musical review)",
    )
    args = parser.parse_args(argv)

    operation = transcribe_score_pdf if args.transcribe else extract_score_layout
    result = operation(
        args.input_pdf,
        args.output_dir,
        executable=args.executable,
    )
    print("Audiveris artifacts:")
    for artifact in result.artifacts:
        print(f"  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
