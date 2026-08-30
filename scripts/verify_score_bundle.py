#!/usr/bin/env python
"""Verify a score bundle against the sha256 hashes recorded in its bundle.yaml.

Each ``bundle.yaml`` records a ``sha256`` for every source and derived artifact.
This checks the bytes on disk against those recorded hashes, so a drifted
source-of-truth (a beat map rebuilt without updating its recorded hash, a
corrupted artifact, a bad merge) is caught with one command instead of forensics.

Usage:
    uv run python scripts/verify_score_bundle.py                # all bundles under data/scores
    uv run python scripts/verify_score_bundle.py <bundle-dir>   # one bundle

Exit code is 0 when every materialized artifact matches its recorded hash, and
non-zero when any artifact mismatches. Artifacts that are DVC pointers not yet
materialized in this worktree are reported as SKIPPED, not failures (run
``make dvc-sync`` to materialize them, then re-check).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "data" / "scores"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(bundle_dir: Path) -> tuple[int, int, int]:
    """Return (ok, mismatched, skipped) counts for one bundle."""

    manifest = yaml.safe_load((bundle_dir / "bundle.yaml").read_text())
    print(f"\n{bundle_dir.name}  (revision: {manifest.get('revision', '?')})")
    ok = mismatched = skipped = 0
    entries: list[tuple[str, str, str]] = []
    for group in ("sources", "derived"):
        for entry in manifest.get(group, []):
            rel = entry.get("path") or entry.get("artifact")
            recorded = entry.get("sha256")
            if not rel or not recorded:
                continue
            entries.append((group, rel, recorded))
    width = max((len(rel) for _, rel, _ in entries), default=0)
    for group, rel, recorded in entries:
        path = bundle_dir / rel
        if not path.exists():
            # A DVC pointer that hasn't been materialized in this worktree.
            print(f"  [{group:7}] {rel:<{width}}  SKIPPED (not materialized)")
            skipped += 1
            continue
        actual = _sha256(path)
        if actual == recorded:
            print(f"  [{group:7}] {rel:<{width}}  OK")
            ok += 1
        else:
            print(f"  [{group:7}] {rel:<{width}}  *** MISMATCH ***")
            print(f"              recorded {recorded}")
            print(f"              on disk  {actual}")
            mismatched += 1
    return ok, mismatched, skipped


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        bundle_dirs = [Path(argv[1]).resolve()]
    else:
        bundle_dirs = sorted(
            p.parent for p in DEFAULT_ROOT.glob("*/bundle.yaml")
        )
    if not bundle_dirs:
        print("no bundles found", file=sys.stderr)
        return 2

    total_ok = total_bad = total_skip = 0
    for bundle_dir in bundle_dirs:
        ok, bad, skip = verify_bundle(bundle_dir)
        total_ok += ok
        total_bad += bad
        total_skip += skip

    print(
        f"\nRESULT: {total_ok} OK, {total_bad} MISMATCH, {total_skip} skipped"
        + ("  -- SOT INTACT" if total_bad == 0 else "  -- SOT DRIFT DETECTED")
    )
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
