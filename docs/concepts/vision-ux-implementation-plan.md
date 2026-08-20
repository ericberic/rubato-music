# Vision UX Implementation Plan

## Purpose

This page maps [Vision and UX Design](../VISION_AND_UX_DESIGN.md) to the
repository state after the 2026-07 cockpit and PRD updates. The vision doc owns
product intent and the canonical roadmap; this page records the practical gap
assessment and sequencing.

## Current Baseline

Already present:

- Backend MIDI device discovery, hardware recording, cued recording, Oguri
  movement-2 playback, stop, Silence, offline render, and session-variant
  Yamaha playback in `src/aimusic/server/routes.py` and
  `src/aimusic/server/live_control.py`.
- A Svelte cockpit in `webapp/src/App.svelte` with the canonical palette,
  sticky masthead, Silence, device pickers, local preview, Latest Take actions,
  and session output management.
- Offline alignment/render scaffolding, phrase-anchor helpers, run artifacts,
  and deterministic tests.
- Simulated-online primitives: `ScoreFollower`, `FollowerUpdate`,
  `OnlineTempoModel`, `SectionMap`, `AccompanimentScheduler`,
  `OracleFollower`, `ReferencePitchFollower`, and tests around their contracts.
- A Matchmaker wrapper for offline MIDI-file follower runs behind the optional
  `live` extra.
- A unified Movement 2 cockpit context whose central score is the Joseffy
  reduction, with a Ready / Live / After situation rail derived from existing
  runtime state. The PDF has 126 machine-review measure rectangles and remains
  readable independently of them; the previous visible Movement 1 score /
  Movement 2 take split is removed.
- Bundle v2 schemas, exact score ticks, explicit PDF/performance mappings and
  readiness gates; lifecycle-v2 takes/jobs/runs; durable alignment and derived
  rebuild jobs; live causal scheduler/engine; stream-backed Matchmaker; Yamaha
  MIDI renderer; replay/FOLLOW APIs and typed runtime status events.

Still missing relative to the target:

- Fully distinct Ready / Live / After faces; the current situation rail and
  staged shell are the migration seam, while the underlying cards are still a
  tool cockpit.
- Product language of pieces and takes; visible UI still exposes sessions,
  slots, files, and MIDI variants.
- A reviewed Movement 2 semantic score/timeline and a reviewed alignment from
  Oguri performance ticks to canonical score ticks.
- Musical section/cue policy and calibrated follower confidence/latency from
  real Yamaha rehearsals.
- Interpretation profile storage, fold/unfold, profile-aware tempo/dynamics,
  keep-default/discard-veto, and profile-aware Ready/After copy.
- Profile-aware tempo/dynamics in the causal engine and a fully distinct After
  reflection face.
- Tested recovery behaviors: Waiting, taper-and-hold, skip/repeat recovery,
  disconnect safety, and play-anywhere re-acquisition.

## Phased Plan

### Phase 0 - Product-facing API shape

Keep backend sessions as storage concepts, but introduce a rehearsal context
API that speaks in piece, movement, excerpt, take, selected devices, latest
take, and profile strength. Preserve existing session endpoints and tests.

Done when the frontend can render Ready without deriving product copy from
session ids and file names.

### Phase 1 - Three-face shell using existing plumbing (in progress)

Split `webapp/src/App.svelte` into Ready, Live, After, Masthead/Silence,
Library drawer, and shared controls. Reuse current endpoints:

- Ready: last piece, Sound Check summary, Rehearse primary, Perform secondary.
- Live v0: state word from hardware job status, placeholder measure/letter,
  Stop, journey placeholder, Silence, Space = primary, Escape = Silence.
- After v0: current Latest Take render/playback flow, with keep/discard as a
  UI affordance until profiles exist.
- Library/Reflection: hide upload/session/variant controls away from the
  at-piano loop.

Done when cued-record -> render -> hardware-playback works through Ready /
Live / After without touching session-management controls.

Current landing: one Movement 2 performer context, situation rail, central
Joseffy score, and hidden legacy Library plumbing. Remaining: make the three
situations materially distinct and carry the latest take into a durable After
face rather than a short completion state.

### Phase 2 - Runtime status stream (implemented)

Define and expose a WebSocket event contract: run/take id, phase, state word,
score beat, optional measure, section id/mode, tempo, confidence, device health,
and latest observation. Bridge existing hardware status first; later the real
live loop becomes the producer. Keep `/api/hardware/status` as the initial and
reconnect snapshot; typed transition events own changes between snapshots.

Done when Live face state is stream-driven and testable without hardware.

### Phase 3 - Live follower, no sound (implemented)

Add a stream-backed Matchmaker adapter or equivalent wrapper that consumes
Yamaha input and emits `FollowerUpdate` rows. Log
`runs/<run_id>/trace/score_position.jsonl`; add a replay path for recorded
takes through the same follower seam. Keep output muted.

Done when a real Yamaha take locks within the target 2-4 salient onsets on the
first solo span and can be replayed deterministically.

### Phase 4 - Live loop v0 and scheduler refactor (software implemented)

Measure Yamaha latency and add `output_advance_ms`. Refactor the scheduler from
single-shot beat lookahead to a wall-clock lookahead queue that can retime
undispatched events and only finalizes emitted events. Wire follower -> tempo ->
section policy -> scheduler -> renderer -> Yamaha on a short movement-2
`FOLLOW` excerpt, then add movement-2 sections, `LEAD`, `HOLD`, and cue-window
re-entry.

Done when the soloist can play a one-minute movement-2 excerpt with live
accompaniment, and replay produces comparable traces.

The causal queue, retiming, Matchmaker stream bridge, Yamaha renderer, replay,
FOLLOW API, safety exclusion, and traces are implemented. Real-hardware
latency/confidence calibration and a reviewed semantic bundle still gate this
phase's musical acceptance criterion.

### Phase 5 - Interpretation profile write path (storage/lifecycle implemented)

Define `data/profiles/<piece_id>/<movement>/profile.json`. Resample aligned
takes onto a beat grid; store recency-weighted median/spread tempo and dynamic
curves; add gross-outlier quarantine; add profile fold/unfold CLI/API;
auto-fold after a take; make discard unwind the fold.

Done when three real kept takes produce a profile curve and discard cleanly
removes a take's contribution.

### Phase 6 - Profile-aware musical behavior

Blend live tempo with profile prior using an explicit confidence/variance
function. Use profile or notated tempo in `LEAD`, add live intensity tracking
and bounded velocity modulation, and represent marked breaths/broadenings.
Compare prior-blended vs reactive timing in simulated-online replay before live
tuning.

Done when replay shows timing improvement from the profile and the soloist hears
dynamic response on a piano/forte contrast.

### Phase 7 - Recovery and performance mode

Implement the vision failure table: taper-and-hold, Waiting, play-anywhere
re-acquisition, skip/repeat handling without catch-up playback, and disconnect
safety. Add Performance mode: top-of-movement start, frozen profile, minimal
Live face, saved take for later Reflection.

Done when each failure row has deterministic tests and the state word matches
the engine behavior.

### Phase 8 - Measure map, journey bar, reflection (machine draft)

The PDF side now has 126 explicit machine-review boxes joined to draft score
ticks. Derive or review the Oguri performance-to-score mapping before showing
live measure numbers. Use measure and section maps for the journey bar. Generate
Reflection v1 per run: takes, rendered MIDI, traces, tempo-vs-profile curves,
alignment metrics, and feedback notes.

Done when Live `m. N` spot-checks against the score PDF and the soloist can review a
week of takes away from the piano.

## Suggested Work Items

1. Review/correct Movement 2 semantic notation and approve the 126-measure timeline.
2. Align Oguri performance ticks to that reviewed timeline.
3. Author Movement 2 section policy and cue windows.
4. Run Yamaha latency and follower-confidence acceptance rehearsals.
5. Feed the learned profile into causal tempo/dynamics behavior.
6. Finish distinct After/Reflection and frozen-profile Performance faces.
7. Add journey bar and reviewed live measure projection.

## Documentation Follow-ups

- Update [PWA Rehearsal UI](pwa-rehearsal-ui.md) after the three-face shell
  lands.
- Update [System Design](../SYSTEM_DESIGN.md) when the profile, WebSocket, or
  scheduler queue contracts are finalized.
- Update [Testing](../TESTING.md) when live-status and failure-mode tests land.
- Add hardware findings to [Yamaha MIDI Setup](../runbooks/yamaha-midi.md) and
  [Knowledge Log](../LOG.md) after latency and local-control experiments.
