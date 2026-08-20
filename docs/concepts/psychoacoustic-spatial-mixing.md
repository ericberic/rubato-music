# Calibrated Low-Latency Spatial Mixing

## Summary

Rubato treats physical speaker placement as an orchestration dimension. The
Yamaha supplies a stable source at the piano, a wired HDMI soundbar supplies a
wide room-filling center, and future named zones may place a solo instrument
elsewhere in the room. Score-authored gain envelopes move attention and depth
between those stable sources without turning ordinary mixing into a special
score-following mode.

For the first room system, use 59 ms as the approved planning assumption for
direct HDMI Game-mode output. That value is comfortably inside Rubato's normal
100 ms dispatch horizon, so the primary design no longer requires 250--500 ms
score-specific commitment permission. It remains an assumption until the full
BBCSO/CoreAudio/HDMI/acoustic path is measured at the piano bench. See
[LG S95-Series HDMI Latency](../sources/lg-s95-series-hdmi-latency.md).

## Primary Design

Use one ordinary predictive score clock and one ordinary commit boundary for
all enabled low-latency zones:

```text
score event + arrival clock + MixPolicy
  -> zone actions with one intended acoustic time
  -> ordinary 100 ms commit boundary
  -> per-zone output advance
  -> Yamaha MIDI / BBCSO audio / future renderer
```

Each renderer may have a different calibrated output advance, but all actions
remain inside the existing scheduling contract. A zone whose required advance
does not fit that contract is unavailable for live use; it does not cause the
score annotation or global scheduler horizon to grow.

The first implementation should exercise:

1. `yamaha_anchor`: the piano-side Yamaha synth and speaker;
2. `room_center`: BBCSO through the direct-HDMI soundbar; and
3. a future `opposite_solo` zone for an oboe or another spatially distinct
   solo layer after its transport and channel mapping are proven.

## Timing Contract

Zone timing remains calibrated rather than assumed at runtime.

```text
residual_error(zone) = measured_acoustic_latency(zone)
                     - configured_output_advance(zone)
```

A zone is live-eligible when:

```text
configured_output_advance_ms <= dispatch_horizon_ms
configured_output_advance_ms <= planning_horizon_ms
residual_error_p95_ms <= zone_timing_tolerance_ms
```

The planning value of 59 ms is enough to simplify the architecture, but it is
not copied into mix annotations. `ZoneConfig` owns the measured value,
calibration revision, and readiness state. The mix program merely names the
zone and says how loudly it should sound.

For ordinary predicted score events, the deadline worker sends each action at
`intended_acoustic_time - configured_output_advance`. A Yamaha action may be
sent close to the acoustic target while the matching soundbar action is sent
roughly 59 ms earlier. Both were frozen by the same normal commit boundary.

## Spatial Mix Program

The durable `MixProgram` contains a piece-wide base blend and sparse
score-region overrides. It is independent from the immutable score bundle,
take-derived Interpretation, coverage, reactive cues, and alignment
corrections.

```yaml
default_routes:
  - zone_id: yamaha_anchor
    stem_ids: [orchestra]
    level: 25
  - zone_id: room_center
    stem_ids: [orchestra]
    level: 100

regions:
  - region_id: m45_room_swell
    start_tick: <m45-start>
    end_tick: <m46-start>
    gesture: swell
    routes:
      - zone_id: room_center
        stem_ids: [orchestra]
        envelope:
          - {score_tick: <m45-start>, level: 15, curve: equal_power}
          - {score_tick: <m45-peak>, level: 100, curve: equal_power}
      - zone_id: yamaha_anchor
        stem_ids: [orchestra]
        envelope:
          - {score_tick: <m45-start>, level: 50, curve: equal_power}
          - {score_tick: <m45-peak>, level: 25, curve: equal_power}
    enabled: true
```

Conceptually:

```text
MixProgram
  program_id
  piece_id / movement
  score_bundle_id / score_bundle_revision / timeline_digest
  level_mapping_revision
  revision / updated_at
  default_routes[]
  regions[]

MixRegion
  region_id
  start_tick / end_tick
  gesture
  routes[]
  enabled

MixRoute
  zone_id
  stem_ids[]
  level (0..100)
  envelope[]
  fallback_zone_id
```

Level is authored, displayed, and persisted as the conventional `0..100`
volume scale: `0` is off and `100` is that route's calibrated full level. It is
not a claim about sound-pressure level or a fixed percentage of amplifier
power. A pinned `level_mapping_revision` lets the runtime translate the same
authored value deterministically into an internal audio gain or Yamaha CC7
value. The PWA never requires the soloist to reason in dB.

That mapping revision is a control law, not a claim that two unmeasured devices
produce identical acoustic SPL at the same number. MIDI CC7 standardizes a
controller value, not the Yamaha's room-level transfer curve. Do not assume a
square/log hardware response from a generic synth heuristic; measure the actual
Yamaha and BBCSO/HDMI paths, then introduce a new pinned mapping revision if the
listening calibration supports it.

The macOS output stays at full/unity. The physical Yamaha and soundbar volume
dials establish the room's reference loudness during setup and are normally
left fixed while Rubato performs. Mix-plan levels operate inside those
references.

At run start, the selected `MixProgram`, `ZoneConfig` revisions, and active
calibrations compile into an immutable `MixPolicy`. Performance never reads a
partially edited browser document.

The Yamaha control stream samples the transport score clock independently of
note attacks and suppresses duplicate quantized CC7 values. This makes swells
continuous across sustained notes and rests. A route naming an exact score part
takes precedence for that part; otherwise it inherits the `orchestra` stem.
Enabled regions use half-open score bounds. Multiple active routes that converge
on one fallback zone combine by maximum level, while authored overlaps for the
same requested zone/stem are rejected instead of depending on list order.

## Named Zones And Spatial Identity

A zone is a stable acoustic identity, not merely a device name:

```text
ZoneConfig
  zone_id
  renderer_id / device_id
  channel_map
  acoustic_position
  configured_output_advance_ms
  residual_error_distribution
  calibration_revision
  health
  fallback_zone_id
```

Examples:

- `yamaha_anchor`: dry, immediate, physically at the pianist;
- `room_center`: broad orchestral image through the soundbar;
- `opposite_solo`: a future oboe source across the room;
- later front/rear HDMI channel groups, only if macOS and the soundbar expose
  deterministic multichannel routing.

Keep a gesture in one spatial identity for long enough that the room can
perceive it as an intentional source. Avoid note-by-note source hopping. Move
or rebalance sources at phrase boundaries with smooth gain envelopes.

Spatial depth comes from more than left/right placement:

- dry local sound reads as near;
- a broader, more reverberant soundbar image reads as room-filling;
- lower direct level plus more room return reads as farther away;
- a stable opposite-side solo source creates antiphonal contrast;
- retaining a quiet Yamaha orchestral floor prevents the center image from
  disappearing when another zone fades.

Equal-power crossfades are a useful starting point, not a perceptual guarantee.
Different samples and acoustic paths can still double attacks or tear timbrally,
so the actual room remains the approval environment.

## Rare Reactive Cues Are Separate

The m.44 low-latency cue is not the normal mixing model. It is a rare scheduler
policy: a specific Yamaha note fires an accompaniment attack immediately when
the ordinary follower cannot decide quickly enough.

Reactive cue data and mix data remain separate:

```text
ReactiveCue
  says when an accompaniment event fires from observed Yamaha input

MixRegion
  says how the already-scheduled stems are distributed across zones
```

At m.44 the Yamaha may own the immediate attack while `room_center` stays low
on the transient and contributes body afterward. Elsewhere the predictive
clock schedules both Yamaha and soundbar normally. Mix mode may explain a
referenced reactive cue read-only, but it must not edit tracking or firing
authority.

## Calibration And Readiness

The direct-HDMI path should be tested with 48 kHz PCM before Atmos, eARC,
WOWCAST, or Bluetooth variants. Compare Standard and Game modes over repeated
clicks and record:

- median acoustic onset latency at the piano bench;
- p95 residual after configured advance;
- first-sound versus continuously active latency;
- reconnect variation and rehearsal-length drift;
- BBCSO/CoreAudio buffer size, callback jitter, and underruns; and
- whether wireless rear synchronization changes the result.

If the full path does not remain inside the 100 ms boundary with adequate
residual margin, the soundbar remains available for deterministic audition but
not ordinary live attacks. Rubato should not restore score-by-score commitment
budgets merely to rescue an unstable route; that is a future slow-zone
extension, not the MVP.

## Renderer Boundary

Pedalboard/BBCSO is now wired into live FOLLOW as the incremental
`room_center` renderer. A zone worker runs in a child process, retains its
instrument instances, receives scheduled symbolic events and transport/mix
updates, and streams blocks to one exact CoreAudio device. The same scheduler
output can fan out to Yamaha plus VST, or run VST-only when the performer
chooses no orchestral MIDI output.

Implementation is not performance admission. The current four-ensemble local
binding reduces BBCSO memory by letting one representative patch cover several
Oguri stems. It still needs actual-room acoustic calibration, first-sound and
dense-tutti checks, callback/underrun observation, crash recovery, and a
rehearsal-length stability verdict. See the [BBCSO audio-zone
runbook](../runbooks/bbcso-audio-zone.md) and [Pedalboard source
notes](../sources/pedalboard.md).

## Degradation And Safety

Each optional route declares a fallback, normally `yamaha_anchor`.

- Missing or stale-calibration zones fail closed before a run.
- A lost soundbar route crossfades toward the Yamaha instead of disappearing.
- `authority_generation` invalidates mutable work after a score-clock reset.
- Panic stops new output immediately inside Rubato, while already accepted
  device-buffer audio remains physically unrecallable.
- The Yamaha orchestral floor makes a fallback change spatial size and timbre,
  not musical continuity.

Trace rows record acoustic target, zone, configured advance, calibration
revision, commit time, adapter time, fallback, and renderer health. Ordinary
mix envelopes do not need an authored confidence source or commitment budget.

## First Listening Example

For m.44--45:

- keep the global Yamaha orchestral floor active;
- let the existing rare m.44 reactive cues emphasize Yamaha attacks;
- use the soundbar as a quiet room bed through m.44;
- swell `room_center` after the m.45 downbeat while Yamaha preserves local
  definition; and
- taper the soundbar smoothly rather than solving the exit with a special
  250 ms cutoff rule.

This example validates spatial blend and gesture authoring. It is no longer a
prototype for a piece-wide slow-output transport.

## Related

- [Decision 0016](../decisions/0016-pedalboard-decoupled-spatial-synth.md)
- [Decision 0007](../decisions/0007-multi-horizon-runtime-transport.md)
- [Accompaniment Control](accompaniment-control.md)
- [Realtime Performance Dataflow](realtime-performance-dataflow.md)
- [System Design](../SYSTEM_DESIGN.md)
- [Mix Authoring Mode](../design/MIX_AUTHORING_MODE.md)
