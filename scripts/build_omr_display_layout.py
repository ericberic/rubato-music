#!/usr/bin/env python3
"""Build zero-based PDF layout evidence from an Audiveris .omr project."""

from __future__ import annotations

import argparse
from pathlib import Path

from aimusic.accompaniment.omr_layout import (
    extract_omr_display_layout,
    write_omr_display_layout,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("omr_project", type=Path)
    parser.add_argument("output_json", type=Path)
    args = parser.parse_args(argv)

    display_layout = extract_omr_display_layout(args.omr_project)
    output = write_omr_display_layout(display_layout, args.output_json)
    print(
        f"Wrote {display_layout.box_count} layout boxes across "
        f"{display_layout.page_count} pages to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
