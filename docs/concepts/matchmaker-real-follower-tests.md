# Matchmaker Real Follower Tests

## Summary

Rubato now has optional tests that run the real `matchmaker` package in offline
MIDI simulation mode. These tests are separate from the toy `ReferencePitchFollower`.

They verify that Matchmaker can emit causal score-beat updates from MIDI score
and MIDI performance files, including a generated solo-only MIDI representation
of the real Chopin Op. 11 I measures 139-141 excerpt.

## Adapter

Code:

`src/aimusic/accompaniment/matchmaker_follower.py`

Entry point:

```python
run_matchmaker_midi_file(score_file, performance_file, method="arzt")
```

The adapter uses `matchmaker.Matchmaker` with:

- `input_type="midi"`
- an offline `performance_file`
- `wait=False`
- `unfold_score=False` by default

It returns Rubato `FollowerUpdate` rows so later tests can feed the tempo model
and scheduler using the same shape as other followers. File-mode rows use the
same explicitly uncalibrated `0.5` policy value; Matchmaker's path does not
provide posterior confidence just because the complete file is available.

The same module now includes `MatchmakerStreamFollower`, a bounded live adapter
for Matchmaker 0.3's `BytesMidiStream`. It accepts one `PerformedNote` at a time,
forwards MIDI bytes to Matchmaker's live generator on a background thread, and
returns within a configured timeout even if no estimate is ready.

## Tests

Code:

`tests/accompaniment/test_matchmaker_real_follower.py`

Coverage:

- A tiny deterministic MIDI score/performance pair.
- The real Chopin MusicXML excerpt converted to a bundle, then rendered as
  solo-only score MIDI plus rubato synthetic performance MIDI.
- Matchmaker `arzt` MIDI following returns the expected score-beat onset path.
- The real Matchmaker `pthmm` follower consumes six notes incrementally through
  `BytesMidiStream`, returns the expected beat path, and stays at zero
  confidence until Rubato's three-update warm-up completes.
- Fake-stream tests verify the live adapter's MIDI-byte bridge, bounded timeout,
  and explicit uncalibrated confidence metadata without importing or opening a
  MIDI device.

## Running

Default CI does not install the `live` extra, so these tests skip when
`matchmaker` is unavailable.

Run locally with:

```bash
uv sync --extra dev --extra live
uv run pytest -m matchmaker
```

Run the full suite with Matchmaker installed:

```bash
uv run pytest
```

## Current Limitations

- The real-package capability tests cover both offline files and incremental
  bytes, but the new stream adapter still has no Yamaha acceptance result.
- Live streaming is limited to `pthmm` until each method's processor/stream
  pairing is validated.
- It tracks a solo-only MIDI score generated from the Chopin bundle, not the full
  orchestral MusicXML score.
- Matchmaker exposes score positions but no calibrated confidence/lock value.
  Rubato's three-update lock and `0.5` confidence are explicitly heuristics.
- Movement 2 semantic score and projected runtime event bundle are still needed
  before this can be the real cockpit start path.

## Links

- Related source: [Matchmaker](../sources/matchmaker.md)
- Related concept: [Score Following](score-following.md)
- Related concept: [Simulated Online Harness](simulated-online-harness.md)
- Related concept: [MusicXML Excerpt Conversion](musicxml-excerpt-conversion.md)
