# Live Run Forensics

Use this runbook to reconstruct what happened during a Rubato live performance.
It is an operational companion to
[Realtime Performance Dataflow](../concepts/realtime-performance-dataflow.md);
that concept page owns the runtime architecture and trace semantics.

## Quick Start

Analyze the latest actual live performance (auditions and replays are excluded):

```bash
uv run python scripts/analyze_live_trace.py
```

Analyze a known run:

```bash
uv run python scripts/analyze_live_trace.py --run-id live-1786330629261
```

An explicit `runtime.jsonl` path remains supported for archived or copied
artifacts. The JSON output begins with aggregate diagnostics and includes a
`run` block that joins the files belonging to the take.

## Artifact Join

One live performance is intentionally represented by several artifacts:

- `runs/<run_id>/trace/runtime.jsonl`: follower, tempo, policy, scheduler,
  adapter output, renderer, and lifecycle evidence.
- `runs/<run_id>/trace/cursor.jsonl`: score position shown by the performance
  cursor.
- `data/processed/<run_id>/solo.mid`: the automatically captured Yamaha input.
- `data/processed/<run_id>/.rubato-scratch-performance.json`: marks the capture
  as ephemeral until the performer explicitly keeps it.

The helper resolves these under the configured Rubato state/data/run roots, so
do not hard-code macOS `Application Support` paths in diagnostic scripts.

## Reading a Complaint

Start with the reported measure and symptom, then move through the pipeline:

1. Confirm canonical score ticks and alignment for the measure.
2. Compare the captured solo MIDI with raw and stabilized follower positions.
3. Inspect policy transitions, reactive-anchor arming, and authority changes.
4. Compare scheduled target/commit timing with adapter `midi_output` timing.
5. Pair every relevant note-on with its note-off, retrigger, panic, or terminal
   release.
6. Compare cursor rows separately when the UI looked wrong but audio was right.

Aggregate percentiles locate broad problems; they do not explain a single
musical event. Preserve the run ID and cite event-level rows when concluding
that alignment, following, scheduling, output, rendering, or UI projection was
responsible.

## Regression Boundary

Turn each confirmed cause into the smallest deterministic regression: a score
landmark for alignment, a follower/control-clock sequence for scheduling, or an
event ownership/release test for reactive and legato behavior. Hardware latency
checks remain opt-in. Record durable musical findings in the relevant concept
or source page and substantial analyses in [the knowledge log](../LOG.md).
