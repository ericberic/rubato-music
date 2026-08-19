# Simulated Online Harness

## Summary

The simulated-online harness replays deterministic piano observations through the
same runtime seams used by live performance:

```text
PerformedNote -> ScoreFollower -> FollowerUpdate -> TempoModel -> Scheduler
```

When a mix program is selected, the same harness continues through both output
branches:

```text
Scheduler -> software MIDI sink
          -> MixPolicy -> unit-signal room renderer -> mix_state trace
```

The software MIDI sink is deterministic and sufficient for CI. A named CoreMIDI
destination such as an IAC bus may replace it for an optional software-synth
loopback, but listening is not the assertion boundary. The unit-signal renderer
has a stronger machine-checkable contract: its RMS and peak equal the effective
route gain.

It exists so Rubato can test scheduling, tempo response, section policy, and
trace behavior before live Yamaha input or probabilistic score following are
stable.

This does not need wall-clock sleeps for CI. The preferred test shape is to feed
observations in timestamp order using their `perf_time` values, as if they had
arrived live. That makes tests deterministic while preserving the causal data
flow. Actual real-time sleeps belong in later latency/hardware tests.

`aimusic.realtime.loadtest` is the single wall-clock load entry point for both
plain accompaniment and mix-enabled replay. It accepts `--mix-program`, an
optional pinned `--mix-revision`, and `off|counters|trace` telemetry rather than
maintaining a second mix-only harness. A mix scorecard can require observed
gain changes, catching a replay that never reached the authored passage.

## Follower Abstraction

`ScoreFollower` is a protocol with one causal method:

```python
observe(note: PerformedNote) -> FollowerUpdate | None
```

Implementations can include:

- `OracleFollower`: test-only follower that reads known `score_beat` from
  synthetic observations.
- `ReferencePitchFollower`: test-only follower that infers score beat by matching
  incoming pitches against the known solo reference.
- Matchmaker wrapper: production candidate that estimates `score_beat` from live
  MIDI against the solo reference.
- ACCompanion-style follower: future reference/integration path.

The rest of the runtime should consume only `FollowerUpdate`, not follower-specific
state.

`RecordedNoteReplay` and `LiveEngine` are the next-level causal harness. Unlike
the original `run_simulated_online` compatibility helper, they use a monotonic
clock, keep a mutable wall-clock scheduler plan, tick dispatch independently of
new piano onsets, write the production trace shapes, and send through the same
`AccompanimentOutput` protocol intended for Yamaha. `ManualClock` and
`CapturingOutput` make this path deterministic in CI.

## Closed-Loop Evaluation Harness

`closed_loop_evaluation.evaluate_closed_loop` is the measurement-grade path for
tempo-model and scheduler A/B decisions. It drives the production composition:

```text
recorded/aligned PerformedNote
  -> ScoreFollower
  -> LiveEngine / TempoModel
  -> AccompanimentScheduler
  -> DeadlineAccompanimentOutput
  -> deterministic virtual MIDI adapter
```

The event loop uses `ManualClock`; it advances to input onsets, the live
2-millisecond control cadence, and immutable MIDI deadlines without sleeping.
There are no wall-clock reads or background deadline threads. A nonzero Yamaha
output advance is therefore visible in the delivered time rather than being
lost in a scheduler target-time proxy.

The output is a `ClosedLoopEvaluationReport` with schema version 3 and metric
version `closed-loop-onset-v1`. It records:

- the deterministic seed, runtime/evaluation configuration, and a SHA-256 input
  digest;
- `execution_domain = deterministic_virtual_clock`,
  `hardware_delivery_measured = false`, and follower evidence derived from the
  follower instance that executed: synthetic oracle, deterministic test
  follower, or unspecified;
- the stable arrival-curve identifier so curve-aware and flat A/B artifacts
  cannot be mistaken for the same runtime geometry;
- every orchestral event after the deadline-output seam;
- signed and absolute onset error for each canonical quarter beat, measured
  against the piecewise-linear aligned solo timing map;
- named landmark metrics and small aggregates (count, mean signed, mean
  absolute, maximum absolute error).

`load_aligned_midi_take` reads performed timestamps, pitches, and velocities
from a recorded Standard MIDI File and joins only the supplied
`note_index -> canonical score beat` alignment evidence. Unmatched note-ons
remain in the follower input with no oracle position; the loader never infers a
coordinate by tick equality.

The per-beat grouping floors canonical accompaniment positions to the owning
quarter beat. Thus the Movement-II measure-44 roll at canonical
`176.048–176.145` is compared with the solo timing at canonical beat `176`.
Source-performance position is not silently treated as canonical:
`evaluate_projection_closed_loop` wraps the raw follower through the same
`runtime_projection.runtime_follower` / `CanonicalFollower` seam as live
FOLLOW, where source/reference position becomes canonical identity exactly
once.

The fixed aligned-take regression runs twice for byte-identical reports, matches
a frozen same-take live output trace within `1e-12` seconds, and produces stable
reactive-vs-LTE A/B results on one input digest. This harness measures behavior;
it does not select the winning model.

The dispersion-trust regression adds three curve arms on the same digest:
supported zero-dispersion Interpretation, high-dispersion Interpretation, and
missing-dispersion Interpretation. The last two must reproduce
`reference-warp-v1` delivered output within `1e-12` seconds; the trusted arm
must improve the declared synthetic broadening landmark. The trace assertion
also pins `SchedulerTrace.arrival_curve_id`, so a report cannot claim fitted
behavior while the runtime silently used another curve.

The anchor-phase-need regression synthetically isolates Issue #130 without conflating phase
with expectation shape. A test curve adds 200 ms only to the anchored
orchestral beat and leaves the following reference warp exact. On the same input
digest, the reactive anchor removes the injected miss; the following orchestral
beat remains at `0.0 ms` and matches the unanchored arm to `1e-12` seconds.
Runtime trace rows also show the anchor output and the triggering follower/LTE
observation sharing the same performed time. This is evidence against adding a
second anchor-to-LTE phase input until a real Yamaha trace meets Decision 0010's
narrowed re-entry trigger.

This harness measures algorithmic scheduling targets under its derived
follower evidence. It does not measure Matchmaker when the report says
`synthetic_oracle`, and it never measures OS thread preemption, CoreMIDI/device
buffering, or hardware delivery. A scheduler/tempo A/B may use oracle evidence
to isolate downstream behavior; a score-follower claim requires recorded
Matchmaker evidence, and a hardware-latency claim requires the live JSONL trace
or latency-calibration path. The evaluator does not currently offer a
`recorded_matchmaker_trace` label: that label must wait for a typed trace loader
that proves the executed provenance instead of trusting caller metadata. The
report fields make the supported domains visible in the artifact rather than
relying on prose around it.

## OracleFollower Role

`OracleFollower` does not solve score following. It is an isolation tool.

Use it to answer:

- Does the synthetic piano performance encode the intended rubato and dynamics?
- Does the tempo model update from incoming score-position observations?
- Does the scheduler place future accompaniment events at the expected wall time?
- Are section modes respected independently of tracker uncertainty?

Once these pass, replace `OracleFollower` with Matchmaker and measure tracking
error separately.

Optional Matchmaker tests now run the real package in offline MIDI simulation
mode. See [Matchmaker Real Follower Tests](matchmaker-real-follower-tests.md).

## ReferencePitchFollower Role

`ReferencePitchFollower` is a deliberately small score follower used to ensure
tests do not always give away the answer. It consumes performed notes that may
omit `score_beat`, searches forward through the solo reference for matching
pitches, and emits inferred `FollowerUpdate` objects.

It is useful for testing:

- hidden ground truth: the input note has no `score_beat`;
- mixed supervision: one note supplies an anchor beat, later notes omit it;
- downstream behavior when follower confidence is less than 1.0.

It is not a replacement for Matchmaker. It is pitch-only, monotonic, and does not
handle repeated ambiguous passages, wrong notes, polyphonic matching, skips,
flourishes, or long rests.

## Tracker Warm-Up

An oracle has no warm-up requirement because the test provides the score beat.

A real symbolic tracker should support two startup modes:

- Explicit start beat selected by UI: tracker can emit useful estimates after the
  first distinctive onset or chord. The implemented live seam seeds
  Matchmaker's PTHMM distribution at the selected reference-performance beat
  and admits lock after two locally stable estimates; canonical score identity
  remains separate.
- Free listening from a broad region: tracker may need several observations to
  disambiguate repeated patterns.

The deterministic replay seam accepts the same printed measure only when every
observation is canonically aligned. It interpolates the take time at the entry,
truncates earlier notes, and shifts the remainder while preserving relative
timing. Integration coverage compares post-entry scheduler JSONL targets
against the corresponding suffix of a full replay.

For MVP UX, design around roughly 1-2 seconds or 2-4 salient onsets before the
system is fully trusted, while still logging confidence from the first event.
Repeated accompaniment patterns, tremolos, scales, or long rests can require a
longer window. The scheduler should gate risky output on confidence and section
policy rather than assuming a fixed number of seconds is always enough.

## Current Test

`tests/accompaniment/test_simulated_online.py` verifies the earlier lightweight
component harness:

- Synthetic solo notes can carry known score beats.
- Dynamics can be modulated in the generated performed notes.
- `OracleFollower` emits `FollowerUpdate` objects through the same interface as a
  real tracker.
- `ReferencePitchFollower` can infer score beats when synthetic notes do not
  include `score_beat`.
- The harness covers both fully hidden ground truth and mixed anchor/hidden
  observations over time.
- `OnlineTempoModel` updates beat period from observed rubato timing.
- `AccompanimentScheduler` schedules accompaniment events once they enter the
  lookahead window.

`tests/accompaniment/test_synthetic_accompaniment_capability.py` adds a fuller
synthetic capability case:

- score beats are hidden from the performed input stream;
- the follower receives notes one at a time in timestamp order;
- the soloist plays steady time, then broadens from 0.5 to 0.75 seconds/beat;
- the tempo model follows that change;
- accompaniment events are scheduled against expected wall-clock times;
- `evaluate_scheduled_events` reports missing, unexpected, and mistimed events.

This is still not a Matchmaker capability test. It proves the deterministic
accompaniment runtime and a toy pitch follower can work together under a
timestamped playback simulation.

`tests/accompaniment/test_closed_loop_evaluation.py` owns the stronger
measurement contract described above. Use it, not `run_simulated_online` or the
one-step Interpretation proxy, for scheduler/model ship-or-kill decisions.

`tests/accompaniment/test_matchmaker_real_follower.py` adds optional real-package
coverage when the `live` extra is installed:

- Matchmaker tracks a tiny deterministic MIDI score/performance pair.
- Matchmaker tracks a solo-only MIDI representation generated from the real
  Chopin MusicXML excerpt.
- The returned alignment path is converted into Rubato `FollowerUpdate` rows.

## Links

- Related design: [System Design](../SYSTEM_DESIGN.md)
- Related concept: [Realtime Performance Dataflow](realtime-performance-dataflow.md)
- Related concept: [Score Bundle Contract](score-bundle-contract.md)
- Related concept: [Matchmaker Real Follower Tests](matchmaker-real-follower-tests.md)
