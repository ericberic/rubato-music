# Decision 0016: Calibrated low-latency spatial output zones

- Status: proposed
- Date: 2026-08-01
- Amended: 2026-08-03
- Superseded for live hosting by: [Decision 0019](0019-reaper-external-orchestra-host.md)

## Context

Rubato originally centered spatial audio on a Bluetooth-like zone requiring
250 ms or more of output advance. That led to score-authored commitment
windows, action-specific early-transfer rules, and musical-tolerance fields in
every mix region.

The intended room output is now a direct-HDMI LG flagship soundbar in Game
mode. The soloist accepts 59 ms as the MVP planning assumption, based on published
direct-HDMI S95-series evidence. Rubato's normal runtime already plans 500 ms
ahead and commits its deadline prefix 100 ms ahead. A stable 59 ms route fits
inside that existing boundary and does not justify making early commitment the
organizing principle of mix authoring.

The artistic objective remains: use the Yamaha at the piano, the soundbar as a
wide room-filling center, and future named outputs such as an opposite-side
oboe to create deliberate spatial orchestration.

## Decision

### 1. Make calibrated low-latency HDMI the normal spatial route

The MVP uses direct HDMI, initially 48 kHz PCM, rather than Bluetooth, WOWCAST,
or a TV/eARC round trip. The 59 ms value is a design assumption, not a runtime
constant. Bench calibration replaces it with a revisioned measured advance
before performance readiness.

A zone is live-eligible only if its configured output advance fits the normal
100 ms dispatch horizon and its residual spread meets the zone's readiness
budget. A zone that does not fit is unavailable for normal live playback; it
does not enlarge the scheduler horizon through score annotations.

### 2. Reuse one ordinary commitment model

All ordinary Yamaha and soundbar actions use Decision 0007's existing planning
and ownership-transfer contract. The zone planner assigns one intended
acoustic time, then per-zone deadline outputs apply their calibrated advances.

The primary `MixProgram` no longer contains `max_commit_ahead_ms`,
`residual_tolerance_ms`, or a prediction-confidence basis. Those fields solved
the superseded slow-zone design. Calibration and runtime configuration own
timing; mix authoring owns sound.

An action-specific long-horizon extension remains possible later, but it is
not part of the MVP or its persisted mix schema.

### 3. Store a base blend plus sparse score overrides

`MixProgram` contains:

- piece/timeline identity and revision;
- a pinned `0..100` level-mapping revision;
- piece-wide default routes and gains;
- sparse score-aligned `MixRegion` overrides;
- per-route stem selection, destination, gain envelope, and fallback; and
- enabled/bypassed state.

The initial blend keeps a Yamaha orchestral floor around `20..30` while BBCSO
through the soundbar supplies the main room image around `100`. These are
ordinary PWA volume values, not dB, SPL, or amplifier-power measurements. The
versioned renderer mapping translates them to audio gain or MIDI CC7.

At run start the selected program, zone revisions, and calibrations compile
into an immutable `MixPolicy`. Live performance never consumes browser draft
state.

### 4. Treat named zones as stable artistic identities

`ZoneConfig` owns renderer/device identity, optional channel map, acoustic
position, configured advance, residual distribution, calibration revision,
health, and fallback.

The first zones are `yamaha_anchor` and `room_center`. A future
`opposite_solo` zone may place an oboe across the room after its physical route
or HDMI channel map is proven. Routing changes occur at reviewed phrase
boundaries with smooth gain envelopes; Rubato does not bounce individual notes
between unrelated timbres or locations.

### 5. Keep reactive attack cues separate from mixing

The rare m.44 Yamaha-note-triggered attacks are scheduler/section-policy data,
not mix annotations. They answer *when does this event fire?* A `MixRegion`
answers *where and how loudly does it sound?*

At a reactive cue, Yamaha may own the immediate attack and the soundbar may
contribute the body. Most of the piece continues through the ordinary
predictive follower and low-latency multi-zone scheduler.

### 6. Automate route gain, not note expression

Swells and fades are sparse score-aligned stem/zone-bus gain envelopes. They do
not claim CC11, replace articulation, or require a fresh fade on every note.
Equal-power crossfades are a starting curve subject to room audition.

### 7. Keep calibration and renderer acceptance explicit

Calibration measures the complete BBCSO/CoreAudio/HDMI/acoustic path at the
piano bench, including central latency, p95 residual, reconnect behavior,
first-sound behavior, and drift. Standard and Game modes are compared rather
than inferred from their labels.

Pedalboard ran as the crash-isolated incremental live-MIDI renderer, but
BBCSO/HDMI remains hardware-gated until it passes callback, buffer, CPU/RAM,
preload, state, latency, and rehearsal-length stability tests. The deterministic
suite replaces the plugin with a unit-signal instrument and verifies the exact
post-plugin gain applied to captured blocks.

Decision 0019 replaces this live host after hardware traces found roughly
470--500 ms of delay in the isolated-worker/queue/mixer topology while BBCSO's
actual block processing remained below 1 ms. The zone, mix, and calibration
contract in this decision remains; REAPER now owns plug-in audio and CoreAudio.

Detailed mix observation is optional. In `trace` mode the audio process writes
only numeric shared-memory gauges (score ticks, route/master/effective gain, RMS,
and peak); an off-path sampler emits `mix_state` rows. `counters` retains bounded
worker health windows, and `off` removes the mix sampler for a time-critical
performance.

### 8. Preserve Yamaha fallback

Every optional zone falls back to `yamaha_anchor`. Missing zones fail closed
before a run. Mid-run loss crossfades toward Yamaha when possible. Panic
prevents new output inside Rubato but cannot recall samples already accepted by
an OS or device buffer.

## Consequences

- Mix authoring becomes primarily spatial and musical instead of a latency
  permission editor.
- One normal scheduler contract covers Yamaha and the direct-HDMI soundbar.
- The Yamaha underlay improves continuity when the room route moves or fails.
- Sparse regions describe artistic exceptions; the whole score does not need
  annotation.
- Precise reactive attacks remain rare, explicit scheduler exceptions.
- Multichannel or opposite-side placement remains available through named
  zones without precommitting to a particular HDMI channel topology.
- Hardware calibration is still required because plugin buffers, soundbar DSP,
  wireless satellites, and acoustic distance can consume the nominal margin.

## Alternatives Considered

- **Keep per-region 250--500 ms commitment budgets:** rejected for the MVP
  because the accepted direct-HDMI assumption fits the ordinary boundary and
  those controls distract from the musical task.
- **Treat 59 ms as a hard-coded device fact:** rejected; it is a planning
  assumption until measured on the actual complete path.
- **Route everything only through the soundbar:** rejected because a quiet
  Yamaha floor improves source continuity and safe degradation.
- **Use reactive firing everywhere:** rejected; it is needed only at a few
  attacks where observed Yamaha input must bypass normal follower latency.
- **Move individual notes between locations automatically:** rejected because
  it breaks phrase identity and can expose timbre or timing discontinuities.
- **Design a general slow-zone transport now:** deferred until an actual desired
  zone exceeds the normal horizon and listening tests justify the complexity.

## Reconciled Decisions

- [Decision 0002](0002-tracker-and-synth-mvp.md): Yamaha remains the reliable
  anchor and fallback while BBCSO is the quality/spatial renderer.
- [Decision 0007](0007-multi-horizon-runtime-transport.md): its ordinary locked
  prefix is sufficient for calibrated low-latency zones; the invariant is
  unchanged.
- [Decision 0010](0010-human-beat-anchors.md): rare reactive cues remain a
  separate timing-authority mechanism.
- [Decision 0011](0011-realtime-multiprocess-architecture.md): live plugin
  process/IPC details remain gated by renderer measurements.
- [Decision 0015](0015-follower-always-exists.md): the follower remains the
  normal score clock across the piece.

## Revisit Trigger

Revisit if the measured direct-HDMI path exceeds the 100 ms horizon, p95
residual makes attacks unacceptable, the soundbar cannot expose a stable PCM
route, a desired future zone is genuinely slow, or room listening rejects the
Yamaha/soundbar blend.

## Links

- [Calibrated Low-Latency Spatial Mixing](../concepts/psychoacoustic-spatial-mixing.md)
- [LG S95-Series HDMI Latency](../sources/lg-s95-series-hdmi-latency.md)
- [Accompaniment Control](../concepts/accompaniment-control.md)
- [System Design](../SYSTEM_DESIGN.md)
- [Mix Authoring Mode](../design/MIX_AUTHORING_MODE.md)
- [BBCSO Secondary Audio-Zone Audition](../runbooks/bbcso-audio-zone.md)
