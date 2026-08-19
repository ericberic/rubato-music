# Decision 0007: Multi-horizon runtime transport and authority generations

- Status: accepted
- Date: 2026-07-24

## Context

Live testing exposed two different kinds of work sharing one performance path:

1. symbolic inference may revise score position, tempo, and future orchestral
   timing as new piano notes arrive;
2. committed MIDI must reach the Yamaha at a precise monotonic deadline even
   if inference, tracing, or a device callback is temporarily slow.

The earlier scheduler described a mutable horizon, a “frozen” horizon, and a
deadline queue, but the ownership boundary was ambiguous. Its stale-event
workaround also marked a canonical beat interval as suppressed after an
authority change. That could not detect a future-looking canonical event whose
shared-reference calculation nevertheless produced a past wall-clock
deadline.

An independent runtime review proposed a larger state record, continuous
FOLLOW/LEAD blending, generations throughout the MIDI stack, and one unified
MIDI command queue. Those ideas were pressure-tested against the current
failure evidence. The useful invariant is simpler: inference may revise only
events it still owns, and the deadline worker must never infer music.

## Decision

Rubato uses two operational horizons separated by an atomic ownership transfer:

```text
symbolic control worker
  -> mutable rolling score/reference plan
  -> commit boundary
  -> locked deadline prefix
  -> deadline worker
  -> MIDI renderer / Yamaha
```

The symbolic control worker runs follower, tempo, section policy, and
scheduling. Its lookahead is bounded in both wall time and symbolic beats. At
the beginning of each control tick, plans already inside the commit horizon
transfer under the prior accepted estimate. The new estimate may retime only
the remaining suffix. A newly derived event inside the horizon may transfer in
the same tick.

The deadline worker owns the locked prefix and performs only monotonic waiting,
serialized delivery, and output advance. A local panic barrier prevents a
command popped concurrently with panic from emitting afterward. This barrier
is a queue-concurrency token, not musical authority.

The scheduler owns a musical `authority_generation`. Pause, resume, mode
transition, tempo re-anchor, skip, repeat, and stop advance it and cancel the
mutable plan. Every planned event is stamped with the current generation. A
generation mismatch cannot commit. Re-derived deadlines more than 50 ms in the
past expire with an explicit trace record instead of sounding late.

`LiveEngine` represents transport authority as one six-state object:

- `FOLLOW`
- `LEAD`
- `COAST`
- `HOLD_AWAIT_ENTRY`
- `HOLD_DROPOUT`
- `STOP`

That object owns the current position, extrapolation/lead anchor, active lead
section, expected entry, and buffered entry candidate. It replaces interacting
lead/await/dropout booleans.

Trace production is removed from deadline-sensitive locks. Producers enqueue
validated models into a bounded queue; a dedicated writer serializes and
appends JSONL. Clean shutdown drains accepted rows and treats writer failure or
overflow as an error. Scheduler traces include generation, authority reason,
expired IDs, target time, and commit time; MIDI traces separately record the
adapter return time.

An authored entry window may seed `FOLLOW` after two locally stable Matchmaker
estimates even though global lock normally requires three. A candidate observed
during `LEAD` retains its original performance timestamp but cannot truncate
that lead. This is a narrow handoff policy, not a claim that Matchmaker
confidence is calibrated.

### Low-latency spatial-zone extension (2026-08-03)

Decision 0016 preserves the ownership-transfer invariant and admits calibrated
spatial zones only when their required advance fits this decision's ordinary
100 ms boundary. Routing still resolves before commitment because one event
may yield Yamaha and soundbar actions with different devices and output
deadlines. The actions share the same frozen-prefix boundary; their deadline
workers apply different calibrated advances.

Slow-zone action-specific boundaries are deferred. After ordinary transfer,
the same rule still applies: inference cannot revise the action. An authority
generation may flush work still inside Rubato, but cannot recall samples
already accepted by a downstream device buffer.

## Deliberate Non-decisions

- No continuous FOLLOW/LEAD weight is introduced until confidence and
  influence are measurable on real traces. Section authority remains
  discrete, with bounded coast and explicit handoffs.
- No 9-field handoff object is added; the six-state transport object carries
  only state that changes engine behavior.
- Musical generations do not control note releases or renderer internals.
- Note-ons, note-offs, CC, and panic are not merged into one queue in this
  change. The deadline worker owns committed attacks/controls; the renderer
  still owns release lifetime and same-pitch retrigger correctness.
- No 10–15 second online whole-score matcher is added. Bundle preparation owns
  PDF/MusicXML/reference alignment, and Matchmaker owns causal following. A
  future slow recovery observer may suggest an anchor but may not own output.

## Consequences

- A slow follower update or trace write cannot delay the deadline worker.
- A stale reference-derived event cannot burst out after relock merely because
  its canonical beat lies ahead.
- Panic is a real emission barrier even when a due command was already popped.
- Superseded tempo/volume requests are distinguishable from applied requests.
- The design is soft real time: macOS, Python, CoreMIDI, and Yamaha latency
  remain measurable external factors. Hardware traces are still required for
  final latency acceptance.
- Future planner sophistication can change the mutable suffix without changing
  the committed-prefix contract.
- Calibrated low-latency spatial actions reuse the ordinary boundary without
  adding score-authored transport authority.

## Links

- [Realtime Performance Dataflow](../concepts/realtime-performance-dataflow.md)
- [System Design](../SYSTEM_DESIGN.md)
- [Decision 0006](0006-canonical-beat-evidence-fusion.md)
- [Decision 0016](0016-pedalboard-decoupled-spatial-synth.md)
- [Testing](../TESTING.md)
