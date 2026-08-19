---
name: live-run-forensics
description: Diagnose Rubato live performances by joining the runtime trace, cursor trace, automatically captured solo MIDI, and scratch-take metadata. Use when a user asks what happened in the latest take, reports timing, articulation, follower, reactive-trigger, sustain, output-latency, or terminal-state problems during a live performance, or asks whether the follower and orchestra behaved correctly.
---

# Live Run Forensics

## Read First

- [Live Run Forensics runbook](../../../docs/runbooks/live-run-forensics.md)
- [Realtime Performance Dataflow](../../../docs/concepts/realtime-performance-dataflow.md)
- [Accompaniment Control](../../../docs/concepts/accompaniment-control.md)
- [Testing](../../../docs/TESTING.md)

## Workflow

1. Preserve the run ID before starting another live performance. Do not assume
   the browser's last visible state is the durable run state.
2. Run `uv run python scripts/analyze_live_trace.py` with no arguments to find
   the latest `live-*` run. Use `--run-id <id>` when the user names a take.
3. Treat the returned `run` block as the artifact join. The runtime trace is
   authoritative for follower, policy, scheduler, and output behavior; the
   captured `solo.mid` is authoritative for what the performer sent; the
   cursor trace explains what the UI displayed.
4. Start with aggregate evidence, then inspect event-level rows around every
   reported measure. Do not infer a musical cause from one metric alone.
5. Separate these layers explicitly:
   - canonical score and alignment;
   - follower observation and stabilization;
   - section policy and reactive-anchor ownership;
   - scheduler target/commit/expiry;
   - adapter note-on/note-off delivery;
   - renderer articulation and patch behavior;
   - cursor/status projection.
6. Compare scheduler lateness with `midi_output` lateness. A clean scheduler
   cannot prove the device emitted on time. For a clipped sustain, pair each
   note-on with its release and check authority changes, retriggers, panic, and
   end-of-score projection. For a missed reactive chord, prove whether the cue
   armed, whether prediction reserved the event, and whether a trigger fired.
7. Reproduce the smallest failing musical region with deterministic tests.
   Keep hardware assertions opt-in and preserve the raw run ID in the report.
8. Record durable musical or architectural findings in the owning doc and
   append substantial run analyses to `docs/LOG.md`.

## Guardrails

- Never treat a browser screenshot as proof of backend timing.
- Never diagnose score alignment by comparing two artifacts derived from the
  same alignment source; use the score-localization skill for that work.
- Never promote an automatically captured scratch take into rehearsal data
  without the performer's explicit choice.
- Never erase or rewrite the original trace while investigating it.
