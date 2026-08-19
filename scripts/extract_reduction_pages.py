#!/usr/bin/env python3
"""Extract a movement page window from a performer-facing reduction PDF."""

from __future__ import annotations

import argparse
from pathlib import Path

from aimusic.accompaniment.reduction_pdf import extract_pdf_page_window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("output_pdf", type=Path)
    parser.add_argument("--first-page", type=int, required=True)
    parser.add_argument("--last-page", type=int, required=True)
    args = parser.parse_args(argv)

    page_count = extract_pdf_page_window(
        args.source_pdf,
        args.output_pdf,
        first_page=args.first_page,
        last_page=args.last_page,
    )
    print(f"Wrote {page_count} pages to {args.output_pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
