#!/usr/bin/env python3
"""Hardware-free, fresh-process cold vs prepared follower startup benchmark.

This does not clear OS filesystem caches. No MIDI device or audio renderer is
opened. The warmed budget is opt-in, so base CI never asserts machine speed.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default="chopin_op11_movement_2")
    parser.add_argument("--score-file", type=Path)
    parser.add_argument("--tempo", type=float, default=68)
    parser.add_argument("--max-ready-ms", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    from aimusic.realtime.follower_preparation import FollowerPreparation
    from aimusic.realtime.follower_process import FollowerSpec

    score = args.score_file
    if score is None:
        from aimusic.accompaniment.runtime_projection import (
            default_bundle_registry,
            project_bundle_v2_to_provisional_runtime,
        )

        score = project_bundle_v2_to_provisional_runtime(
            bundle_id=args.bundle_id,
            revision=None,
            registry=default_bundle_registry(),
        ).follower_reference_path
    ready = threading.Event()
    pool = FollowerPreparation(observer=lambda s: ready.set() if s.state == "ready" else None)
    spec = FollowerSpec(score_file=str(score), tempo_bpm=args.tempo)
    child = None
    try:
        started = time.monotonic()
        child = pool.claim(spec, str(score), threading.Event())
        cold_ms = (time.monotonic() - started) * 1000
        cold_stages = child.startup_timings_ms.copy()
        child.close()
        child = None
        ready.clear()
        pool.release()
        if not ready.wait(65):
            raise RuntimeError(pool.status().message)
        preparation = pool.status().model_dump()
        started = time.monotonic()
        child = pool.claim(spec, str(score), threading.Event())
        ready_ms = (time.monotonic() - started) * 1000
        report = {
            "score_file": str(score),
            "cold_claim_ms": cold_ms,
            "cold_stages_ms": cold_stages,
            "prepared_claim_ms": ready_ms,
            "next_take_preparation": preparation,
            "scope": "Fresh spawned Python processes; OS caches retained; no MIDI/audio ports",
        }
        result = json.dumps(report, indent=2)
        print(result)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result + "\n")
        if args.max_ready_ms is not None and ready_ms > args.max_ready_ms:
            raise SystemExit(f"Prepared claim exceeded {args.max_ready_ms} ms")
    finally:
        if child is not None:
            child.close()
        pool.close()


if __name__ == "__main__":
    main()
