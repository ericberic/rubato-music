# Accompaniment Control

## Summary

Accompaniment control is the layer that turns score-following output into a
musical orchestral response. It decides when Rubato follows the soloist, when it leads,
how it smooths tempo, and how it maps solo dynamics/articulation into
accompaniment MIDI.

## Current Recommendation

Implement accompaniment control as explicit modules:

- Score follower: estimates score beat.
- Tempo model: smooths and predicts beat timing.
- Section policy: chooses `FOLLOW`, `LEAD`, `HOLD`, or `STOP`.
- Expressive renderer: maps score notes to MIDI onset, duration, and velocity.
- Trace logger: records decisions for rehearsal debugging.

Do not let the score follower directly drive accompaniment timing without a
tempo/control layer.

## Why This Matters

ACCompanion's musician feedback shows a central failure mode: a system that only
reacts can become hard to trust. If it overreacts to a pause, wrong cue, or
ambiguous start, both human and machine can slow down or drift together.

For Chopin concerto accompaniment, Rubato needs to know when the orchestra is
supporting the soloist and when it must carry the music through tutti or
interlude material without live solo input.

## Control Modes

- `FOLLOW`: accompaniment timing is derived from score follower plus tempo
  model.
- `LEAD`: accompaniment proceeds from score tempo, configured tempo curve, or
  learned/reference prior.
- `HOLD`: accompaniment waits for a cue or safe re-entry condition. A follower
  dropout enters this *gently* — it stops scheduling but does not fire an
  all-notes-off, so sustaining orchestra notes ring to their own release. Only
  `STOP` panics. The dropout itself is judged on a tempo-relative silence budget
  and requires genuine input silence, so notated rubato and low-confidence dense
  passages (where onsets keep arriving) never trip it. See
  [Decision 0015](../decisions/0015-follower-always-exists.md).
- `STOP`: output panic/stop state.

The spatial mix decides balance and routing; it never decides whether the
audible orchestra exists. A per-part gain floor keeps the Yamaha/anchor output
present even when a zone is uncalibrated and falls back, or a region is authored
to zero. Intentional silence is the global orchestra mute, applied after the mix.
One `mix_enabled` switch governs the mix identically for rehearsal and live runs,
which differ only by start measure.

These names are policy endpoints, not a claim that ensemble timing is always
binary. The musical target has one continuous transport with two evidence
sources:

- a score-indexed rehearsal/reference prior with beat-period and phase
  variance;
- live pianist tempo/phase evidence with follower confidence.

The current MVP deliberately uses discrete authority plus bounded coast because
Matchmaker confidence and rehearsal variance are not yet calibrated as
comparable weights. Piano-led material uses live evidence; orchestra-only
interludes use the authored prior. A cadence
handoff uses the pianist's arrival downbeat as a phase anchor, then returns
tempo toward the orchestra's learned/reference curve instead of extrapolating
the outgoing ritard. Bounded phase correction should bring both sides together
over subsequent beats without jumps. Continuous authority blending remains a
future experiment: it must first define measurable confidence, influence, and
failure bounds rather than silently mixing two clocks.

## Tempo Model Lessons

Raw inter-onset interval tempo is too jagged for musical accompaniment. The
tempo model should:

- Smooth score-follower output.
- Predict the next accompaniment onset.
- Resist one-note timing anomalies.
- Allow configured leading behavior.
- Use rehearsal/reference performances when available.

ACCompanion's LTE model is especially relevant: it uses reference performances
as tempo expectations, with fallback to a simpler linear model. For Rubato, this
suggests a practical workflow where the soloist records several takes of the target
excerpt before live accompaniment is attempted.

Repeated takes expose information that one take cannot: the median local
period is the stable interpretation prior, while per-cell MAD distinguishes
consistent style from an isolated delay, rolled chord, or mistake. The
rehearsal UI summarizes support, alignment quality, consistency, and
high-variance measure/beat cells. These statistics prepare the FOLLOW prior
used by every recorded accompanied pass. They never decide whether the
follower exists or assign one mode to a whole take.

## Recorded rehearsal path

`Record pass` with **Orchestra cue** enabled is the causal accompaniment
runtime with capture turned on. The selected score measure initializes the
runtime, `sections.json` determines `LEAD`/`FOLLOW` authority as the score
advances, and Yamaha input both corrects the follower and becomes the take.
The orchestra does not stop at a predicted entry and there is no fixed
take-level handoff.

Before recording, the UI asks the runtime for the exact plan and states which
bars the orchestra leads, the first score position where it follows, local
rehearsal support, and the learned opening tempo. The opening pace comes from
the performance profile's fitted `base_seconds_per_quarter`; the audition/live
tempo control is not a second clock for recorded passes. A cue stored on the
take is only a localization hint for offline alignment.

## Expressive Rendering Lessons

Start with deterministic rendering:

- Preserve accompaniment score pitches.
- Advance one ordered symbolic transport from the tempo model and emit every
  crossed onset exactly once.
- Preserve notated duration unless a section policy says otherwise.
- Map dynamics from score defaults plus simple solo velocity trends.

Human-reviewed terminal notation may override an early reference-performance
release. Movement II's final two low E string notes are explicitly sustained
from m.125 through the end of m.126; their note-offs remain mutable on the live
score clock so the soloist and the orchestra can release together.

Later, add:

- Soloist-conditioned accompaniment velocity.
- Articulation scaling.
- Phrase-level dynamic curves.
- Reference-performance tempo priors.

## Tempo, Mix, And Expression Are Different Controls

Rubato keeps three musical quantities separate:

- **Lead tempo (`♩ = N`)** is expressed in canonical, printed score quarters.
  For fixed orchestra playback, Rubato first infers the
  reference's nominal notated-quarter pulse from the dense score↔MIDI map; the
  MIDI header's tick-to-seconds tempo is not assumed to be the score tempo.
  For each `LEAD` section, Rubato makes the complete canonical span last
  `score_quarters * 60 / N` seconds, then derives the internal reference-clock
  period from that duration. This preserves the reference performance's local
  phrase shape without confusing its denser coordinate with notated quarters.
  A recorded accompanied pass instead starts from the performance profile's
  fitted base period, so the displayed rehearsal plan and runtime share one
  learned clock.
  During `FOLLOW`, live symbolic timing from the pianist remains authoritative;
  changing the lead tempo stores the pace for the next orchestra-led passage
  rather than fighting the pianist.
- **Orchestra mix (0-100)** is an immediate global balance control. At the MIDI
  boundary it scales each channel's authored CC7 volume, so orchestral parts
  retain their relative balance and per-note velocity. It affects notes that
  are already sounding as well as future attacks. The BBCSO/soundbar renderer
  maps the fader through a squared gain curve, so its midpoint is about -12 dB
  rather than a deceptively loud -6 dB linear-amplitude midpoint.
- **Expression** is phrase-level dynamics. Live input velocity is now traced,
  but Rubato does not yet infer an automatic accompaniment-expression curve
  from it. That later model should use a robust beat/phrase envelope and CC11,
  remain bounded by the explicit mix control, and be evaluated against repeat
  takes before it is enabled in performance.

This separation makes the manual balance reliable today without claiming that
raw key velocity is already a calibrated acoustic-loudness model.

## Score-Authored Spatial Mix Automation

Spatial swells are a fourth, separate layer. the soloist marks exact score regions
where a named stem or room zone may fade, swell, enter, or move in emphasis.
The program also owns a piece-wide base blend so those regions remain sparse.
These envelopes control stem/zone gain; they do not take ownership of phrase
expression or CC11. Device advance and readiness remain `ZoneConfig` facts,
not musical annotation fields.

See [Calibrated Low-Latency Spatial Mixing](psychoacoustic-spatial-mixing.md)
for the zone, calibration, spatial-identity, and fallback contracts, and
[Mix Authoring Mode](../design/MIX_AUTHORING_MODE.md) for the separate PWA
workspace that creates those regions.

## Rehearsal Trace Requirements

Each run should log:

- Incoming MIDI events.
- Estimated score beat.
- Section mode.
- Tempo estimate.
- Next scheduled accompaniment event.
- Panic/hold/lead transitions.
- Human feedback.
- Every accepted tempo/mix control value and the section mode in which it was
  applied.
- The MIDI adapter's emitted velocity/CC value, target and actual note-on time,
  scheduled and actual note-off time, lateness, and backend call duration.
- Canonical `LEAD` tempo both as predicted by the section clock and as realized
  from actual MIDI note-on timestamps joined back to symbolic score beats.

Without this trace, musical failures will be hard to reproduce.

## Related

- [Score Following](score-following.md)
- [Section Policy](section-policy.md)
- [Calibrated Low-Latency Spatial Mixing](psychoacoustic-spatial-mixing.md)
- [ACCompanion](../sources/accompanion.md)
