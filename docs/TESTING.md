# Testing Strategy

## Test Priorities

The live accompanist depends on deterministic behavior before real-time polish.

Critical tests:

- Score section lookup is deterministic.
- Section policy switches modes correctly.
- Offline MIDI parsing preserves note timing.
- Accompaniment scheduling does not emit events out of order.
- Simulated online replay produces stable traces.
- Closed-loop evaluation produces byte-identical delivered-output timing and
  metrics for the same aligned take, seed, and runtime config.
- Server/API endpoints continue to work for recording and playback.

## Test Categories

- Unit: pure score maps, section policies, tempo helpers.
- Integration: MIDI file in, trace and accompaniment MIDI out.
- Hardware smoke: Yamaha input and output route visible.
- Human smoke: Eric plays an excerpt and rates accompaniment behavior.

Spatial-mix tests are split by boundary:

- unit tests validate persisted-artifact round trips, revision conflicts,
  repeated durable Undo, cross-process write exclusion, stale-score discovery
  and rebind, mix-region bounds/overlap, envelope interpolation, part-aware route
  eligibility, piece-wide defaults, per-zone deadlines, and fallback without
  audio;
- a Chromium browser test switches between rehearsal and mix authoring, then requires both
  the irrelevant pixels and their pointer targets to disappear, authors a
  range and feature preset for an exact stem, changes the base Yamaha level,
  exits, reloads, and proves the same program is reconstructed;
- deterministic replay injects per-zone latency/jitter and proves that live
  zones fit the ordinary commit boundary while their compensated acoustic
  targets remain aligned;
- causal mix load tests replay synthetic notes or a recorded runtime trace
  through the live FOLLOW path with the selected durable `MixProgram`. An
  in-process MIDI sink replaces Yamaha, and a unit-signal room renderer replaces
  Pedalboard/CoreAudio. `mix_state` rows then prove score tick, route gain,
  global master gain, and observed signal level on one monotonic timeline;
- live VST block tests use a fake instrument that emits samples of exactly
  `1.0`; the captured block and shared mix gauge must therefore equal the
  compiled gain. This verifies the actual post-plugin gain multiplication rather
  than only verifying that an automation command was requested;
- synthetic-signal tests validate the calibration estimator against known
  delays and adverse signal-to-noise conditions;
- opt-in hardware tests measure the actual bench arrival distribution and
  downstream tail;
- opt-in renderer tests own plugin compatibility, incremental state, callback
  jitter, underruns, resource use, and crash behavior;
- human listening tests own spatial placement, base-blend, residual timing, and
  timbre-continuity acceptance.

No unit or CI test should require Pedalboard, an orchestral plugin, a microphone,
or an audio device. See
[Calibrated Low-Latency Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md).

## Running Tests

```bash
uv run pytest
```

Hardware tests should be opt-in and never required in CI.

```bash
uv run pytest -m hardware
```

Matchmaker package tests are also optional:

```bash
uv sync --extra dev --extra live
uv run pytest -m matchmaker
```

The Pedalboard/BBCSO renderer is implemented in live FOLLOW but remains an
opt-in hardware path. Deterministic tests cover score-window resolution,
single-track MIDI isolation, channel normalization, VST-only output fanout,
compiled-policy gain-envelope interpolation, and Yamaha CC7 mapping without
loading a plug-in. MIDI adapter tests also advance the policy through a held
note with no second note-on and require a new CC7 value. Follow the
hardware/renderer procedure separately:

```bash
uv sync --extra dev --extra audio
uv run rubato audio-devices
uv run rubato audition-bbcso-cue --wav-out /tmp/rubato-bbcso-cue.wav
uv run rubato audition-bbcso-cue --program-id main --play-default
```

Run BBCSO commands from a normal macOS GUI session and see
[BBCSO Secondary Audio-Zone Audition](runbooks/bbcso-audio-zone.md). The known
plug-in abort under a restricted host is part of the isolation contract, not a
CI test.

The base suite uses fake MIDI input/output ports for channel/program setup,
note lifetimes, panic, runtime replay, FOLLOW wiring, and the one-hardware-job
invariant. Tests marked `hardware` are the only ones allowed to open a real
Yamaha port.

Replay a recorded live trace through the causal runtime and its mix program:

```bash
uv run python -m aimusic.realtime.loadtest \
  --trace /path/to/runtime.jsonl \
  --mix-program main \
  --mix-revision 3 \
  --telemetry trace
```

The default output is a capturing software MIDI port and the room renderer is a
deterministic unit-signal probe, so this command needs neither Yamaha nor an
audio device. Pass `--midi-output "IAC Driver Bus 1"` only for an optional local
listening/loopback run. `--telemetry off` disables detailed mix observations;
`counters` retains bounded worker timing summaries; `trace` adds sampled
`mix_state` rows. The real VST worker publishes only plain numeric shared-memory
stores from its render loop, while an off-path observer serializes samples.
`scripts/analyze_live_trace.py` groups those observations by zone/instrument and
reports the pinned program revision, score-tick span, gain range/change,
RMS/peak, and observer delay alongside follower and scheduler diagnostics.

`tests/conftest.py` installs a process-wide temporary data/run root before test
modules load. Per-test roots may narrow it further, but delayed timers and
materialization workers can never fall back to the performer's real
Application Support take bank after a fixture tears down.

Score-first rehearsal regressions have three independent checks:

- TypeScript unit tests interpolate score beats through sparse transport
  anchors, resolve measure boundaries from coverage timeline spans, verify
  within-measure cursor progress, and exercise the 750 ms page lookahead.
- Browser tests at 1440x900 require the score and controls to remain visible,
  assert that both score beat and cursor x advance during cue/review, click an
  engraved PDF measure, verify the two selected-passage recording starts are
  unambiguous, and verify `target_score_beat` reaches both cued and uncued
  capture paths. A take-bank journey selects an aligned take on the score,
  verifies its visible measure span and learning state, then asserts that
  **Take only** and **Take + orchestra** preserve the selected displayed beat
  across Yamaha and browser review requests. A
  Chromium landmark journey also screenshots the persisted take's G-sharp,
  F-sharp, and final chord at 10.014s, 13.992s, and 49.253s and asserts the
  visible `Now` marker is in measures 13, 14, and 22 without opening MIDI
  hardware. Another browser journey freezes an active score transport and
  submits a one-beat in-app correction, proving the fallback never requires a
  terminal.
- Backend tests round-trip the piecewise Oguri/display projection, pin the
  first audible orchestra E4 to relative time zero and printed measure 1 beat
  one (after trimming the source's 4.202-second technical preroll), pin the
  performed landmarks to measures 13/14/22 and the long-take phrase ending to
  beat one of measure 52, hold the visible span at the final matched onset,
  require all four expressive beat anchors in m.17 (including its large
  rubato), reject collapsed symbolic beat warps without rejecting that rubato,
  permit arbitrary duration ratios between symbolically identified measures,
  and require the m.46 opening chord `[47, 63, 71]` to place its downbeat near
  native Oguri tick 109,912 while the former time-led tick 107,926 remains
  inside m.45,
  pin the m.53 B-natural to beat four, validate correction
  monotonicity/revision journaling,
  reject negative localization candidates during both alignment and legacy
  migration, and verify arbitrary-position cue and uncued placement metadata.
  For a pulled Movement 2 OMR artifact, regenerate geometry and
  require byte-equivalent JSON:

  ```bash
  uv run python scripts/build_omr_display_layout.py \
    data/scores/chopin_op11_movement_2/source/joseffy_reduction_movement2.grid.omr \
    /tmp/rubato-display-layout.json
  diff -u data/scores/chopin_op11_movement_2/derived/display_layout.machine.json \
    /tmp/rubato-display-layout.json
  ```

  The semantic fusion artifact is likewise byte reproducible:

  ```bash
  uv run python scripts/build_movement2_beat_map.py \
    data/scores/chopin_op11_movement_2/source/joseffy_reduction_movement2.audiveris.mxl \
    data/scores/chopin_op11_movement_2/source/oguri_concerto_11_2.mid \
    data/scores/chopin_op11_movement_2/derived/timeline.machine.json \
    data/scores/chopin_op11_movement_2/derived/display_map.machine.json \
    /tmp/rubato-performance-beat-map.json
  diff -u data/scores/chopin_op11_movement_2/derived/performance_beat_map.machine.json \
    /tmp/rubato-performance-beat-map.json
  ```

Live-start regressions are likewise split across layers:

- projection tests require an accompaniment-first MIDI bundle to become
  `LEAD` before its first solo onset and `FOLLOW` afterward;
- coordinate-boundary tests require expressive follower beats to pass through
  the same fused map as accompaniment events before either reaches tempo or
  scheduling;
- deterministic engine tests require the first orchestra event to dispatch
  with zero piano observations, advance on the monotonic clock, enter
  `Waiting` at the handoff without panic, and resume only after follower lock;
- a later-interlude regression requires symbolic arrival at measure 22 to enter
  `LEAD`, dispatch the interlude with no further piano notes, stop planning at
  the measure-23 boundary, and wait for re-entry;
- the same boundary regression requires `FOLLOW` lookahead not to pre-commit
  measure-22 notes, requires piano estimates beyond measure 23 not to terminate
  `LEAD`, and verifies that the real Movement-II bundle contains its fourth-beat
  accompaniment events;
- canonical-tempo regressions use a deliberately denser reference coordinate:
  a four-quarter section at `♩ = 90` must take 2.667 seconds while retaining its
  internal expressive proportions;
- ordered-transport regressions set lookahead to zero, cross an accompaniment
  onset between two updates, and require that event to emit exactly once;
- relock regressions cross an onset while the transport is held and require it
  not to emit late after `HOLD -> FOLLOW`; a separate dual-coordinate case
  places an event canonically ahead but gives it a reference-derived deadline
  in the past and requires an explicit expiration rather than a burst;
- authority-generation regressions require a re-anchor to cancel the mutable
  suffix, increment the generation, and stamp every replacement plan;
- handoff regressions exercise piano entry 300 ms early, on time, and 300 ms
  late; two stable estimates inside the authored entry window must relock while
  retaining the observation's original monotonic timestamp;
- a forward score resynchronization clears stale mutable plans without an
  all-notes-off panic; backward repeats still release and rearm;
- tempo regressions feed chord-note estimates milliseconds apart and require
  them to remain position evidence without producing impossible BPM; backward
  sub-measure jitter must not move the accepted cursor backward;
- the mocked-port server test requires audible MIDI output before any input and
  pins live note timestamps to the scheduler's absolute clock domain;
- the Chromium journey reads the m.1/m.12 plan, enters an exact `♩ = 88`,
  sets a 42% orchestra mix and 12 ms Yamaha output advance, persists the setup
  setting across reload, verifies all values in the live request, changes
  tempo and volume while the run is active, verifies the acknowledged API
  commands, restores the last confirmed value after a simulated failure, and observes a
  measure-advancing score cursor while the runtime remains `Leading`;
- fake-port renderer tests assert that the global mix scales every authored
  channel CC7 without flattening note velocity, that a live change reaches the
  deadline queue without blocking the control thread, that a backend-delayed
  note-on still receives its full duration, and that note-on/backend and
  note-off/deadline timestamps are traceable without MIDI hardware, and a
  command popped concurrently with panic cannot emit after the panic barrier;
- asynchronous trace tests require serialization to occur only on the trace
  worker and clean close to persist every accepted row without overflow;
- projection and hardware API tests infer the reference performance's nominal
  notated-quarter tempo from the dense score↔MIDI correspondence (about
  `50.79 BPM` for the current Movement-II artifact), reject the misleading
  declared `120 BPM` MIDI tick clock as musical tempo, and verify that
  performer-facing `♩ = 88` becomes `88 / inferred_reference_bpm`.

## Determinism Rules

- No sleeps in unit tests.
- Use recorded MIDI fixtures for simulated online tests.
- Keep hardware latency out of deterministic assertions.
- Store seeds and runtime config in run artifacts.
- Tempo-model and scheduler comparisons must use
  `closed_loop_evaluation.evaluate_closed_loop`, which advances a virtual clock
  across production control ticks and MIDI deadlines. A one-step math proxy or
  `run_simulated_online` component trace is not ship/kill evidence.
- Compare A/B arms by `input_digest`; unequal digests mean the variants did not
  receive identical aligned solo timing.

## Manual Rehearsal Checklist

For each live rehearsal:

- Confirm MIDI input device.
- Confirm MIDI output device.
- Run a one-note input/output test.
- Record the session.
- Save run traces.
- Add human feedback immediately after playing.
- Summarize the latest live take with
  `uv run python scripts/analyze_live_trace.py` (or name it with `--run-id`) and
  inspect follower backtracks/latency, tempo decisions, section transitions,
  dispatch lateness, and panic reasons.
- Compare `autonomous_lead_reference_bpm`,
  `autonomous_lead_canonical_bpm`, and
  `realized_lead_canonical_bpm_from_midi_output`. The first is an internal
  clock rate and may differ; the latter two must agree with the selected score
  metronome marking within retained expressive variation.
- For an onset/release complaint, inspect both scheduler lateness and
  `midi_output.note_on_lateness_ms` / `note_off_lateness_ms`. Compare input and
  orchestra velocity distributions and confirm a `control` row plus per-channel
  `channel_volume` rows for each fader move. If scheduler timing is clean but
  `adapter_call_duration_ms` or adapter lateness spikes, repeat a short passage with
  the same output port and preserve that run ID for CoreMIDI/device diagnosis.
  `commit_lead_time_ms` shows how long the deadline worker owned each attack
  before its target; `scheduler.authority_changes` reconstructs why mutable
  plans were invalidated.
  The analyzer also reports 50/100/250 ms late-onset counts and scheduler
  expiration IDs and authority generations. Only after stale/scheduler
  outliers are absent should a
  repeatable median residual be compensated with **Advanced Yamaha timing**.
- When investigating a failure, preserve
  `data/logs/rehearsal-events.jsonl` alongside the relevant take/job/run
  artifacts. Filter by `take_id`, `session_id`, `run_id`, or `job_id` to
  reconstruct the lifecycle in file order; exception records include the
  background-worker traceback.

Deterministic tests verify that events are journaled even with no WebSocket
loop, correlation fields and UTC timestamps are present, exception tracebacks
are retained, and a journal write failure cannot escape a worker error path.
