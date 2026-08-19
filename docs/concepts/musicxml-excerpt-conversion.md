# MusicXML Excerpt Conversion

## Summary

Rubato can convert a small MusicXML/MXL measure window into the canonical score
bundle contract used by the simulated-online harness.

The converter is intentionally narrow. It extracts pitched notes, beat positions,
durations, part IDs, roles, and simple instrument mappings. It does not yet
preserve all notation semantics.

## Current Converter

Code:

`src/aimusic/accompaniment/conversion/musicxml.py`

Primary entry point:

```python
convert_mxl_excerpt_to_bundle(
    source_mxl,
    output_dir,
    start_measure=139,
    end_measure=141,
)
```

The converter writes:

```text
metadata.yaml
parts.yaml
events.jsonl
sections.json
instrument_map.yaml
```

Then it loads the result through `ScoreBundle.load`, so conversion output must
pass the same validation as hand-authored bundles.

## First Real Chopin Window

The first tested real-source window is measures 139-141 from the committed
Chopin Op. 11 I MXL source.

Converted facts:

- 7 used parts.
- 50 piano solo events.
- 19 accompaniment events.
- 69 total pitched events.
- 12 beat span.
- Used accompaniment parts: bassoons, first violins, second violins, violas,
  cellos, and basses.

## Capability Test

`tests/accompaniment/test_chopin_musicxml_excerpt.py` verifies:

- The real MXL excerpt converts into a valid `ScoreBundle`.
- Synthetic piano playback is generated from the converted solo events.
- `score_beat` is stripped from performed input before following.
- `ReferencePitchFollower` infers score beats from note pitches over time.
- The scheduler emits accompaniment events from the converted accompaniment
  events.
- Scheduled event times match expected deterministic performance times.

This is a real-source harness test, not a production score-following test. It
still uses `ReferencePitchFollower`, which is monotonic and pitch-only.

## Current Limitations

- No Matchmaker integration yet.
- No polyphonic chord grouping.
- No tie merging.
- No voice/staff preservation in event rows.
- No dynamics/articulation extraction beyond simple default velocities.
- Instrument mapping is generic, not Yamaha-optimized.
- The section map is a single `FOLLOW` section for the converted window.

## Links

- Related source: [Chopin Op. 11 I Source Set](../sources/chopin-op11-i-source-set.md)
- Related concept: [Score Bundle Contract](score-bundle-contract.md)
- Related concept: [Simulated Online Harness](simulated-online-harness.md)
