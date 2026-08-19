# Experimentation Guide

## Experiment Unit

An experiment is a rehearsal or simulation run for a specific score excerpt.

Each run should declare:

- Score source and version.
- Excerpt boundaries.
- Solo input source: live Yamaha MIDI or recorded MIDI.
- Score follower method.
- Tempo model.
- Section map.
- Output route (Yamaha MIDI, configured live-audio zone, both, or rendered
  file).

## Run Artifacts

Store run outputs under `runs/<run_id>/`:

- `input/solo.mid`
- `output/accompaniment.mid`
- `trace/score_position.jsonl`
- `trace/section_mode.jsonl`
- `analysis/metrics.json`
- `summary.md`
- `config_snapshot.yaml`
- `human_feedback.md`

Large files remain DVC-managed.

## MVP Experiment Ladder

1. Offline score ingestion: load score and split solo/accompaniment.
2. Offline alignment: align recorded solo MIDI to the score.
3. Offline accompaniment render: render orchestra using recorded solo timing.
4. Simulated online following: stream recorded MIDI through the real-time API.
5. Live MIDI following: use Yamaha input and log score-position trace.
6. Live accompaniment output: send symbolic accompaniment to the Yamaha synth,
   a configured BBCSO/CoreAudio zone, or both.
7. Rehearsal loop: tune section map and tempo model from feedback.

Use `OracleFollower` before Matchmaker in the simulated-online step. The oracle
test proves tempo/scheduler behavior against a known score-beat answer; the
Matchmaker test then measures tracker error separately.

The current offline tooling is:

```bash
uv run rubato align-midi --reference <solo_reference.mid> --performance <solo_take.mid> --run-id <run_id>
uv run rubato render-offline --reference <solo_reference.mid> --performance <solo_take.mid> --accompaniment <orchestra.mid> --run-id <run_id>
```

See [Offline Alignment And Render](concepts/offline-alignment-render.md).

## Metrics

Use deterministic metrics wherever possible:

- Median alignment error in milliseconds.
- Median alignment error in beats.
- Number of note anchors and phrase anchors.
- Note-anchor versus phrase-anchor timing residuals.
- Alignment rate within 50, 100, 500, 1000, and 2000 ms.
- Accompaniment event scheduling latency.
- Number of mode transitions.
- Number of panic/stop events.

Tempo-model and scheduler A/B work uses the production-path closed-loop report
(`closed-loop-onset-v1`), not the Interpretation evaluator's one-step proxy:

- aligned solo timing is the ground truth;
- delivered orchestra timing is observed after the virtual deadline output;
- per-canonical-beat signed/absolute error and named-landmark aggregates are
  versioned;
- seed, runtime config, evaluation config, and input digest must match across
  arms.

For live sessions, hardware latency should be reported separately from algorithmic
latency when measurable.

## Spatial-Mix Gates

Spatial output work follows evidence gates rather than one end-to-end build:

1. **Policy gate:** review the piece-wide Yamaha/soundbar base blend and one or
   two annotated swell regions, including routes, `0..100` envelopes, and
   fallbacks.
2. **Calibration gate:** measure each zone at the piano bench over repeated
   trials, reconnects, and a rehearsal-length drift run. Preserve raw captures
   plus estimator version.
3. **Causal gate:** replay with virtual/MIDI zones and injected latency/jitter.
   Verify that active zones fit the ordinary dispatch horizon and that their
   compensated outputs share one intended acoustic time.
4. **Listening gate:** audition base-blend, placement, residual-offset, and
   timbre/routing A/B variants in the actual room.
5. **Renderer gate:** measure the proposed live instrument host independently
   before joining it to the performance runtime.

The spatial scorecard reports zone latency/advance/residual quantiles, ordinary
commit margin, fallback count, stale-generation work discarded before output,
known downstream tail, callback underruns, and the reviewed human verdict. See
[Calibrated Low-Latency Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md).

## Related

- [Simulated Online Harness](concepts/simulated-online-harness.md)
- [Offline Alignment And Render](concepts/offline-alignment-render.md)
- [Calibrated Low-Latency Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md)
