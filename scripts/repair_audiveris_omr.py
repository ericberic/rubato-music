#!/usr/bin/env python3
"""Repair a transcription `.omr` that Audiveris crashed on, then re-export.

Audiveris can abort MusicXML export for an entire sheet -- and every sheet after
it -- because of one spurious glyph: in a dense passage it recognizes a notehead
twice and builds a *phantom chord* over the pair with a zero-length stem at
`(0,0)`. That chord belongs to no measure, so `Measure.getClefBefore(null)`
throws at the PAGE step and nothing exports. The printed engraving is fine; it is
an Audiveris bug (a duplicate glyph plus an un-guarded null dereference).

This tool removes the junk structurally (degenerate chords, zero-size stems, and
every relation edge referencing them -- the real heads survive on their healthy
twin chord) and re-exports. It is the automatable equivalent of deleting the
duplicate head in the Audiveris GUI.

Requirements and gotchas (learned the hard way, so nobody re-derives them):

  * The input `.omr` MUST have been transcribed with `-save -swap`, or it holds
    only `book.xml` and no per-sheet SIG to edit. `audiveris_command` already
    passes both.
  * Re-export resumes from the `.omr` (not the PDF) with **no** `-force`, so the
    hand-edit survives; `-force` re-runs HEADS and re-detects the duplicate.

Both are baked into `aimusic.accompaniment.audiveris`; this script is the CLI.

    uv run python scripts/repair_audiveris_omr.py <in.omr> --output <dir>
    uv run python scripts/repair_audiveris_omr.py <in.omr> --output <dir> --patch-only

Full background: docs/sources/joseffy-reduction.md and the score-localization
skill ("Known OMR Defect: Duplicate-Head Phantom Chord").
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aimusic.accompaniment.audiveris import (
    OmrRepairReport,
    repair_and_reexport_omr,
    repair_phantom_chord_omr,
)


def _print_report(report: OmrRepairReport) -> None:
    if not report.touched_sheets:
        print("No phantom chords or zero-size stems found -- .omr already clean.")
        return
    for sheet in report.touched_sheets:
        chords = report.removed_chords.get(sheet, ())
        stems = report.removed_stems.get(sheet, ())
        print(f"  sheet {sheet}: removed chords {list(chords)} stems {list(stems)}")
    print(
        f"Removed {report.total_removed_inters} inter(s) and "
        f"{report.removed_relations} relation(s) across "
        f"{len(report.touched_sheets)} sheet(s)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("omr", type=Path,
                        help="the transcription .omr (saved with -save -swap)")
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for the repaired book + MusicXML")
    parser.add_argument("--executable", type=Path,
                        help="Audiveris CLI launcher (else AUDIVERIS_BIN / default install)")
    parser.add_argument("--patch-only", action="store_true",
                        help="write the repaired .omr but do not invoke Audiveris to re-export")
    args = parser.parse_args(argv)

    if args.patch_only:
        args.output.mkdir(parents=True, exist_ok=True)
        patched = args.output / "book.omr"
        report = repair_phantom_chord_omr(args.omr, patched)
        _print_report(report)
        print(f"Wrote patched .omr: {patched}")
        return 0

    report, run = repair_and_reexport_omr(args.omr, args.output, executable=args.executable)
    _print_report(report)
    print("Re-export command:")
    print("  " + " ".join(run.command))
    mxl = [a for a in run.artifacts if a.suffix.lower() == ".mxl"]
    print(f"MusicXML: {mxl[0] if mxl else '(none exported -- check the Audiveris log)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
