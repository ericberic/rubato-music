#!/usr/bin/env python3
"""Export the FastAPI app's OpenAPI document to `webapp/openapi.json`.

Design doc docs/design/SCHEMA_VALIDATION_ARCH.md §2.2: a static, committed
export -- not a live server -- so the frontend contract-generation pipeline
(issues #83/#84, `@hey-api/openapi-ts`) never needs a running server to
generate types/Zod schemas/client from, and a schema-changing PR shows up as
a reviewable diff in `webapp/openapi.json`.

This is a rerunnable, static build tool, not runtime code -- run it again
whenever a request/response/artifact Pydantic model changes.

Usage:
    uv run --extra dev python scripts/export_openapi.py
    uv run --extra dev python scripts/export_openapi.py --out webapp/openapi.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aimusic.server.app import app

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_PATH = REPO_ROOT / "webapp" / "openapi.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help=f"Output path for the OpenAPI document (default: {DEFAULT_OUT_PATH})",
    )
    args = parser.parse_args(argv)

    schema = app.openapi()
    out_path: Path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
