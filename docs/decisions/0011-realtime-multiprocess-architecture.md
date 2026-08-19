# Decision 0011 — Real-time multiprocess architecture and self-serve load testing

Status: **Proposed** (2026-07-28) — awaiting review before implementation.

## Context

Per-loop instrumentation (Decision 0008 follow-up; `loop_timing` trace rows) of
the live FOLLOW path under a real performance revealed the true bottleneck: the
Matchmaker follower runs the `hiddenmarkov` HMM (numpy/C) in a thread and holds
the GIL for tens of ms **uninterrupted and growing** (7 ms → 180 ms per note over
30 s). This starves the other real-time loops — the input reader stalled up to
80 ms, the processing loop spiked to 1.8 s — even though their own work is
sub-millisecond. Chord serialization (fixed separately) and much of the "lag /
out of sync" Eric hears under load trace back to this GIL starvation.

The system was built as one thread doing everything, with the follower and
deadline-output on side threads. That is the wrong shape for a hard real-time
app: a CPU-heavy stage can silently steal the deadline of every other stage.

## Decision

Rebuild the live runtime as a set of **workers with explicit real-time
contracts**, isolate the one GIL-hog (the follower) in its own **process**, and
make the whole thing **self-testable under load without hardware**.

### Worker decomposition and real-time contracts

Only the follower is a CPU-bound GIL-hog, so only it becomes a separate process;
the rest are I/O-bound or light CPU and stay threads in the main "conductor"
process (a thread's GIL is released during MIDI/socket I/O, and putting every
note through inter-process IPC would add latency on the hot path). More stages
can be processified later if profiling shows another GIL-hog.

| Worker | Process/Thread | Responsibility | Real-time contract (targets, to be tuned from data) |
|---|---|---|---|
| **input-capture** | thread (main) | Stamp MIDI note arrivals, fan out to follower + conductor | stamp < 1 ms after arrival; poll cadence p95 < 2 ms |
| **follower** | **process** | Matchmaker HMM: MIDI bytes → score position | per-note p95 < 20 ms; never exceed the incoming IOI on average; hard cap → coast |
| **conductor** | thread (main) | positions → tempo/section policy → plan orchestra events | tick work p95 < 5 ms |
| **output-deadline** | thread (main) | send committed MIDI at target times | send jitter p95 < 5 ms (exists) |
| **telemetry/publish** | thread (main) | throttled UI status + perf logging | best-effort, strictly off the hot path |

### IPC — stdlib `multiprocessing`, deliberately simple

**Rate calibration first.** Strict lock-free/wait-free discipline comes from the
48 kHz audio-callback regime (64-sample buffers, ~1.3 ms deadlines, millions of
samples/sec) where an allocation genuinely blows a deadline. **Rubato is three
orders of magnitude away from that**: ~10 notes/sec (~50 msgs/sec through a
chord) against ~5–20 ms deadlines. `mp.Queue` moves 10k+ msgs/sec at ~50 µs per
round trip — roughly **0.25% of a 20 ms budget**, at well under 1% of its
capacity. The measured pathology was **80 ms of GIL starvation from the follower
HMM**, three orders of magnitude larger than any queue overhead.

So the design isolates the GIL hog and otherwise stays boring:

- **`multiprocessing.Process` + `mp.Queue`** for capture → follower
  (`(arrival_time, midi_bytes)`) and follower → conductor
  (`(observed_at, position, confidence)`), plus lifecycle/config/shutdown.
- **Gauges**: plain `mp.Value` / `shared_memory` scalars. No seqlock — at a
  50–100 ms sampling cadence a torn read is harmless and self-corrects next tick.
- **No third-party ring-buffer dependency.** Available packages
  (`shm-ring-buffer`, `skabbass1/ring-buffer`, `bslatkin/ringbuffer`,
  `InterProcessPyObjects`) are small single-author projects; taking a
  correctness-critical dependency on one is worse than stdlib at our rates.
- The follower process is spawned per run and torn down on stop; the supervisor
  restarts it if it dies (the conductor coasts meanwhile).

**Re-entry trigger for the complex version**: if the harness shows IPC latency
material against the budget (p99 > 1 ms) or queue saturation, revisit an SPSC
ring buffer over `shared_memory` — with measurements justifying it. Likewise
`prometheus_client` multiprocess mode is a real option for gauges if we later
want Grafana dashboards; not needed to start.

### Lifecycle: follower crash / hang recovery

Split detection from recovery; the watchdog is neither inline nor its own process.

- **Detection = in the Conductor event loop** (cheap, non-blocking). Each tick
  checks a liveness flag / stale-position timestamp and, if the follower is dead
  or stale, trips the circuit-breaker → coast immediately. No deadline risk.
- **Recovery = a supervisor thread in the Conductor process** (never inline).
  Spawning the follower process is tens–hundreds of ms (spawn + numpy/matchmaker
  warm-up + prior seeding); doing it in the hot loop would blow the tick deadline.
  The supervisor waits on the process off the hot path and respawns there.
- **Not its own process** — a process watching a process needs its own watcher
  (recursive-watchdog problem). The Conductor process owns the run lifecycle; if
  *it* dies the run is over anyway, handled a layer up by hardware-control.
- **Re-seed on respawn**: warm-start the new follower at the Conductor's current
  coasted position (reuse the PTHMM-prior seeding), never re-acquire from zero.
- **Two timescales**: a short staleness threshold → *coast* (follower briefly
  behind); a long heartbeat timeout → supervisor *kills + respawns* an
  alive-but-hung follower.

### Observability: three mechanisms, three jobs

Continuous/gradual degradation needs a different tool than catastrophic detection.

1. **Self-instrumentation** (`loop_timing`, exists): each worker measures its own
   work/cadence and emits aggregates. Accurate for per-stage compute — but goes
   silent exactly when a worker stalls (no emit if the loop is not turning).
2. **Monitor / vital-signs sampler** (new): a dedicated sampler thread, ~50–100 ms
   cadence, reads O(1) **shared gauges** the workers update — queue depth +
   **high-watermark**, per-worker **heartbeat**, IPC enqueue→dequeue lag (cross-
   process gauges in `mp.Value`/`shared_memory`; in-process queues read directly).

   **Consolidated via a metrics registry, not scattered logging.** Every worker
   registers its gauges once at startup; the monitor walks the registry and emits
   **one snapshot per tick under a single timestamp**. This is the sanctioned RT
   pattern (hot path does only atomic stores; a non-RT reader does the I/O), and
   it buys three things scattered logs cannot: a **global end-to-end view** in one
   record, **trend/derivative analysis** across all structures on a common clock,
   and a single audited list of what is instrumented — no guessing whether a probe
   sits in the interaction path. Caveat: across a process boundary this is a
   *near*-coherent cut, not an atomic one; use it for trends, never assume two
   gauges are transactionally consistent.
   Samples from *outside* on its own clock, so it keeps reporting **when a worker
   is stuck** (a stalled follower's heartbeat stops *and* its input waterline
   climbs — the monitor sees both) and surfaces **rising waterlines / backpressure
   before overflow**. Optionally a **governor**: on a rising follower-input
   waterline it signals *shed load* (conflate harder) early, rather than waiting
   for the queue to blow. Emits a steady `WaterlineSample` time series.
3. **Supervisor** (above): catastrophic recovery.

**Monitor is a separate process; supervisor is a thread.** These are not folded
together, and the monitor is deliberately *not* a thread: the measured 80 ms
`midi_input` stall proves an in-process thread can be starved by its own
process's GIL/GC — a monitor thread would go blind exactly when its data matters
most (a canary in the same mine). As its own process it is immune to conductor
stalls and can additionally detect a **fully wedged conductor** (heartbeats stop
advancing), which no in-process observer could report. The supervisor stays a
thread because its job is the opposite: it must *own* the follower's process
handle to `join()`/respawn, and lifecycle ownership belongs with the parent.

Consequence: **all gauges live in one shared-memory block**, published by both
conductor threads and the follower process — uniform, and what makes
single-timestamp sampling possible.

**Sync structure — publisher/observer over shared memory, no locks, no
handshake** (workers never know the monitor exists):
- *Scalar gauges* (counters, high-watermarks, heartbeats): plain stores into
  `mp.Value`/`shared_memory`; monotonic, so the monitor differences successive
  samples for rates and the worker never resets anything.
- *No seqlock.* At a 50–100 ms sampling cadence a torn multi-field read is
  harmless and self-corrects on the next tick; the complexity is not earned.
- Each snapshot carries **one sampler timestamp**, giving a *near*-coherent cut:
  right for trends, not for transactional claims.

The monitor can therefore be stopped, restarted, or re-rated with zero effect on
the audio path; the RT path pays one atomic store per gauge.

**Telemetry levels** (gprof-style, config/env flag): `off` / `counters`
(always-on, cheap — the vital signs and `loop_timing`) / `trace` (per-event,
heavy — on demand for a profiling/load session). Production runs `counters`;
flip to `trace` for a bench load test. Also add **end-to-end latency** (note
arrival → orchestra reaction). All emitted as trace rows and summarized by the
scorecard.

### Self-serve simulation & load-test harness (the enabler)

So the redesign can be developed and regression-tested **without the Clavinova**:

- **Input sources**: (a) a pre-recorded take (a trace's `input` rows or a
  recorded solo MIDI), (b) a **fixture generator** producing note streams with
  controllable density, chord size/rate, tempo curve, dropouts, and worst-case
  bursts (seeded, deterministic).
- **Drive modes**:
  - **Deterministic** (`ManualClock`, follower in-process): step the pipeline
    note-by-note for fast, reproducible correctness tests in CI.
  - **Real-time** (wall clock, follower process live): a **virtual input port**
    injects notes at their real times into the actual pipeline, to measure true
    latency/cadence under load. This is the load test.
- **Scorecard**: per-stage cadence/work, IPC queue depth/latency, end-to-end
  latency, chord simultaneity (sub-5 ms), and accompaniment phase vs the offline
  ground truth — checked against the contracts above, pass/fail.
- **Scenario suite**: committed fixtures ("dense chords @120", "fast filigree",
  "tempo ramp", "burst→silence") run the scorecard; deterministic mode gates CI,
  real-time mode is an opt-in local/bench target.

## Build order (incremental, each validated by the harness)

1. **Harness + virtual input port + generators + scorecard** — self-testing first
   so every later step is measurable without hardware.
2. **Follower → subprocess** behind the unchanged `observe()` contract; confirm
   with the harness that `process_note` / `input-capture` cadence stay flat under
   load and chords stay simultaneous.
3. **Formalize worker contracts + IPC metrics/queue-depth logging**; wire the
   scorecard's pass/fail to the contracts.
4. **Iterate** on whatever the harness still flags (e.g., move `publish` fully
   off the hot path; consider processifying output for GC-jitter isolation).

## Deliberate non-decisions / DEFER

- **Not** moving input/conductor/output to processes now — they are not GIL-hogs;
  IPC on the note path would add latency. Re-entry trigger: the scorecard shows a
  non-follower thread starved or a GC pause blowing an output deadline.
- **Not** Cython/Numba/Rust on the HMM — third-party code we do not own; the
  process split gets the isolation without a rewrite. Reserve native `nogil`
  kernels for hot code we own.
- **Not** free-threaded CPython (PEP 703) yet — revisit when numpy/rtmidi/
  matchmaker support it, at which point threads regain true parallelism.

## Research: how established real-time systems do this

Validated against safety-critical RTOS standards and pro-audio practice
(2026-07-28).

- **Health monitoring is a first-class service, not an add-on.** ARINC 653
  (avionics RTOS) lists the **Health Monitor** alongside partition, process, time,
  memory and IPC services: an OS function that monitors hardware/application/OS
  errors and reports them. AUTOSAR's Watchdog Manager is the ISO 26262 analogue.
  These standards also distinguish a **watchdog timer** (notifies only the setter)
  from a **watchdog monitor** (notifies all registered components) — the same
  supervisor-vs-monitor split adopted above.
- **Never allocate, lock, block, syscall, or log on the real-time path.** Waiting
  on a mutex additionally risks priority inversion. Share simple numeric state via
  lock-free atomics; pre-allocate everything else. This is why telemetry must be
  atomic stores read by a non-RT sampler, and why `mp.Queue` is control-plane only.
- **Lock-free SPSC ring buffers** are the standard RT inter-thread/process
  transport; wait-free operations give a fixed step count per op.
- **Triple-thread architecture** (RT / message-UI / worker) is the JUCE-standard
  shape, matching Capture+Output / Telemetry-UI / Follower here.
- **Elevated scheduling priority** for RT threads is expected practice.

Sources: [ARINC 653](https://en.wikipedia.org/wiki/ARINC_653) ·
[Wind River on ARINC 653 health monitoring](https://www.windriver.com/solutions/learning/arinc-653-compliant-safety-critical-applications) ·
[watchdog monitor vs timer (USPTO 9563494)](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/9563494) ·
[Using locks in real-time audio safely (timur.audio)](https://timur.audio/using-locks-in-real-time-audio-processing-safely) ·
[Real-time audio programming 101 (Bencina)](http://www.rossbencina.com/code/real-time-audio-programming-101-time-waits-for-nothing) ·
[RTMLib lock-free/wait-free ring buffer](https://anmaped.github.io/rtmlib/doc/md_doc_lock_free.html) ·
[wait-free SPSC ring buffer](https://blog.paul.cx/post/a-wait-free-spsc-ringbuffer-for-the-web/) ·
[multi-threaded audio processing architecture](https://acestudio.ai/blog/multi-threaded-audio-processing/)

## Links

- [Decision 0008](0008-predictive-follow-clock.md) (FOLLOW clock + instrumentation)
- [Realtime performance dataflow](../concepts/realtime-performance-dataflow.md)
