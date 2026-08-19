#!/usr/bin/env python3
"""Migrate persisted rehearsal takes to canonical performance-profile v2."""

from __future__ import annotations

import argparse

from aimusic.takes.performance_migration import migrate_performance_profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--piece-id", default="chopin_op11")
    parser.add_argument("--movement", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = migrate_performance_profile(
        args.piece_id,
        args.movement,
        apply=args.apply,
    )
    action = "migrated" if args.apply else "would migrate"
    print(
        f"{action} {result.aligned_written} alignment(s); "
        f"{result.aligned_unchanged} already current; "
        f"profile_written={result.profile_written}"
    )
    for backup in result.backups:
        print(f"backup: {backup}")


if __name__ == "__main__":
    main()
