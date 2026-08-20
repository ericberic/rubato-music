# Mixing Mode

- Status: implemented (MVP)
- Date: 2026-08-03

## Goal

Provide a score-centered workspace where the soloist authors spatial placement,
piece-wide blends, swells, fades, and stem emphasis without seeing or editing
rehearsal coverage, tracking corrections, or reactive beat anchors.

Mixing reuses the PWA's proven score geometry, score-local menus,
dragging, and immediate Undo. It is a separate modality because its question is
different:

- **Data mode:** What tracking/rehearsal evidence do we have, and where does it
  need correction?
- **Mixing mode:** Where in the room should each layer sound, and how
  should that balance change through the score?
- **Perform mode:** Play, rehearse, and listen using the prepared tracking data
  and mix plan without editing either one.

The same click must never ambiguously mean both.

## Non-Goals

- Do not edit follower alignment, coverage, section authority, or take data.
- Do not expose a general-purpose DAW timeline.
- Do not require text entry for normal region creation.
- Do not infer artistic authority from rehearsal quality colors.
- Do not run live FOLLOW or record a take while the authoring workspace is
  active.

## Modality Boundary

The masthead owns one persistent, mutually exclusive segmented switch in task
order: **Data · Mix · Perform**. The engraved score and its MusicXML-derived
measure numbers remain visible in every mode; only the interaction grammar and
task-specific overlays change. Mixing is idle-only and cannot be entered while
a performance, recording, or audition owns hardware.

The PWA owns three independent score workspaces:

```text
Perform workspace
  live/rehearsal transport + cursor + read-only minimal mix cues

Data workspace
  coverage + latest take + tracking anchors + exceptional corrections

Mixing workspace
  mix-region selection + zone/stem automation + live audition
```

They share PDF rendering and canonical score geometry only.

## Visible Layers

| Score layer | Data | Mix | Perform |
| --- | --- | --- | --- |
| Engraved PDF and measure labels | visible | visible | visible |
| Playback cursor | visible | visible during audition | visible |
| Amber/green coverage | visible by default | hidden by default | hidden by default |
| Latest-take span and pass counts | visible | hidden | hidden |
| Reactive beat anchors | visible/editable | hidden | hidden |
| Alignment-correction controls | available exceptionally | absent | absent |
| Passage actions | absent | absent | visible |
| Mix automation | hidden by default | visible/editable | hidden by default, optional read-only cues |
| Gain-envelope controls | absent | endpoint editor | absent |
| Zone availability warning | absent | visible | only when blocking |

The compact **Layers** menu stores independent Data and Mix visibility choices
for each workspace. Visibility never changes interaction ownership: showing
tracking color in Mix does not make its corrections editable, and showing mix
cues in Perform never arms an endpoint. Hidden interactive marks must not catch
pointer events underneath the active modality.

## Entry And Exit

- Enter through **Mixing** in the persistent mode switch while idle.
- If a live, recording, calibration, or review job owns hardware, entry is
  blocked with a plain explanation.
- Entry loads the last selected mix program and validates its score-timeline
  identity and zone references.
- **Perform** returns to the live/rehearsal score; **Data** returns to tracking
  labels and corrections. Accepted mixing writes remain durable.
- Unsaved writes are not held only in browser memory. Each accepted edit is a
  durable revision; Undo creates a new revision that restores the prior value.

## Global Level And Route Defaults

The existing global **Orchestra** volume is the only piece-wide performer
control. It scales the whole spatial program immediately, like the adjacent
tempo control, so Mixing does not add a duplicate Base mix panel. Internal
default routes still define the calibrated Yamaha/room spatial image where no
region overrides it; they are configuration, not day-to-day score authoring.

Regions remain sparse artistic deviations: a swell, featured stem,
antiphonal entrance, or temporary rebalance. Endpoint controls use the
conventional `0..100` scale relative to that configured route. macOS remains at
full/unity, while Yamaha and soundbar hardware dials establish fixed
room-reference levels outside the program.

## Region Selection Grammar

The interaction follows familiar score-editing conventions while remaining
distinct from reactive-anchor editing.

### Pointer

- **Click** selects a canonical score position or an existing mix region.
- **Shift-click** extends from the current selection anchor to the clicked
  canonical position, creating a draft region.
- **Click-drag across score geometry** creates the same draft region when the
  gesture remains within mapped notation.
- **Drag a boundary handle** refining an existing region is a post-MVP direct-
  manipulation enhancement; the MVP edits canonical bounds through selection.
- **Drag an envelope handle** is likewise post-MVP. The MVP resolves musical
  presets to explicit points and edits their levels in the region popover.
- **Right-click** on an empty beat opens a compact attachment menu: set the
  effect start here, or—after a start exists—set the end here and open the
  endpoint editor. Right-clicking an existing endpoint edits its region.
- Clicking unoccupied score space clears the transient selection after any
  pending edit is confirmed or cancelled.

Selections snap to the canonical beat grid by default. A modifier may opt into
finer supported score ticks when the engraving/map can identify them honestly.
The popover always states the resolved measure/beat range.

### Keyboard

- `Shift` extends a selection.
- Arrow keys move the active boundary by the current snap unit;
  `Shift+Arrow` extends it.
- `Command/Ctrl+Z` performs durable Undo.
- `Space` auditions or stops the selected region when focus is not in a form
  control.
- `Escape` always invokes Silence first, preserving the cockpit's safety muscle
  memory. A visible **Cancel selection** action handles draft cancellation.

## Mix Context Menu

Selecting a draft range offers **Create mix region**. Right-clicking an existing
automation bracket or either endpoint opens its editor. The centered compact
context window stacks the start and end sliders, states that the values are
interpolated continuously, and keeps presets before advanced fields:

- orchestral stem/layer;
- output zone;
- gesture: `swell`, `fade`, `bed`, `feature`, or `custom`;
- level/intensity and envelope preset;
- fallback behavior;
- enabled/bypassed.

Preset labels are musical authoring shortcuts. The persisted region resolves
them to explicit score control points and `0..100` levels for deterministic
runtime validation. The program pins the renderer level-mapping revision;
device latency is never copied into the region.

## Visual Language

Mix automation uses a horizontal bracket in a dedicated typographic lane above
the system, borrowing the grammar of octave spans rather than painting a graph
through the notes. A hollow downward triangle marks its start and a filled
triangle its end. This is the same beat-marker primitive used by reactive Data
anchors: the shape means “behavior is attached to this beat,” while the active
workspace determines which behavior and menu it owns. Levels stay in the
endpoint editor rather than distorting the bracket into a diagonal contour.
Selection is a brass dashed range outline, not a second semantic fill. In
Perform the plan is off by default; when enabled through Layers it is read-only
and lower-contrast. In Mixing, the selected region shows:

- start/end handles;
- a gain-envelope line with draggable control points;
- destination icons and current `0..100` values;
- a fallback icon;
- validation state in words.

Coverage amber/green is forbidden in this mode. Mix validation uses neutral
language such as **Ready**, **Zone unavailable**, **Latency exceeds live
budget**, or **Needs calibration**, paired with icons rather than rehearsal
colors.

Overlapping regions occupy separate lanes rather than painting over one
another. A part/zone filter may reduce clutter, but filtering never changes the
program.

## Audition

Audition is deterministic score playback, not live tracking. It uses a selected
reference/review clock, begins with enough preroll to hear the entrance, and
loops the selected region only when requested.

The score cursor remains visible during audition. Coverage, take spans, and
tracking corrections remain hidden because they are irrelevant to the mix
decision. The popover may provide bypass and A/B controls, but trace/latency
telemetry stays in a collapsed diagnostic disclosure.

If a physical zone is unavailable, the PWA may preview the envelope through a
local substitute only when the UI labels that substitution clearly. It must not
present such a preview as spatial approval.

## Persistence Boundary

The authoring document is a `MixProgram`, stored independently from both the
immutable score bundle and tracking/rehearsal annotations:

```text
profiles/<piece>/<movement>/mix-programs/<program-id>/mix-program.json
```

A conceptual contract contains:

```text
MixProgram
  program_id
  piece_id / movement
  score_bundle_id / score_bundle_revision / timeline_digest
  level_mapping_revision / revision / updated_at
  default_routes[]
  regions[]

MixRegion
  region_id / start_tick / end_tick / gesture
  routes[]
  enabled

MixRoute
  zone_id / stem_ids[] / level (0..100)
  envelope[] / fallback_zone_id
```

This is not `anchors.json`, a `HumanCorrectionDocument`, coverage, a take, or an
Interpretation-profile parameter. Rebuilding rehearsal data cannot alter it.

At runtime, a selected `MixProgram` is validated with `ZoneConfig` and the
active calibration revision to produce an immutable `MixPolicy` snapshot for
that run. The runtime never reads partially edited browser state.

## API And Concurrency

The eventual API needs list/get/create/update/delete region operations plus
revision-checked Undo. Writes carry the last observed program revision; a stale
browser receives a conflict and reloads instead of overwriting another edit.

Region creation, move/resize, envelope edits, delete, and multi-region restore
are atomic server operations. The browser must not emulate a move with a
racing delete/create pair. This follows the existing reactive-anchor precedent
without sharing its storage or endpoints.

Writes are serialized by a per-program inter-process file lock, then checked
against the caller's observed revision. Each artistic revision records its
explicit Undo parent; an Undo revision points to the preceding artistic parent
rather than assuming the numerically previous file is always the next Undo
target.

Score identity mismatch is recoverable and non-destructive. List/get return the
program with `score_identity_status: stale`, while editing and live compilation
remain blocked. After reviewing region placement against the changed score, the
performer can explicitly rebind the unchanged program to the current identity
as a new revision.

## Validation

Before accepting a region revision:

- bounds are ordered, mapped, and inside the referenced timeline;
- envelope points are ordered and lie inside the region;
- stem and zone references resolve;
- levels are finite and inside `0..100`;
- fallback is valid and cannot recurse;
- overlap rules for the same stem/zone are explicit;
- an unavailable or stale-calibration zone is reported without corrupting the
  authored program.

Timing readiness belongs to the zone, not the musical region. A valid region
can still fall back when its zone is unavailable, uncalibrated, or requires
more advance than the normal live scheduler permits.

## Accessibility And Safety

- Mode identity uses text and iconography, never color alone.
- Every region and handle is keyboard reachable and has a measure/beat label.
- Touch/pointer targets match the existing wide anchor-handle precedent.
- Delete is explicit and immediately undoable.
- Escape always silences audition output.
- Entering Perform requires leaving Mixing; no background mix edit
  remains armed.

## Implementation Order

1. Add the explicit mode shell and layer-visibility contract with no editing.
2. Add canonical click/shift-click/drag range selection and client-only draft
   visualization.
3. Add `MixProgram` schemas, storage, revisioned APIs, and Undo.
4. Add region lanes, context menus, direct envelope manipulation, and tests.
5. Add deterministic local audition and bypass A/B.
6. Add zone/calibration validation and compile the selected program into the
   runtime `MixPolicy` snapshot.
7. Enable direct-HDMI room audition after the renderer/calibration gates in
   Decision 0016 pass; add further physical zones only after their routes are
   proven.

## Implemented MVP

The PWA now has a persistent **Data · Mix · Perform** workspace switch.
MusicXML-derived measure labels remain on the score in every workspace. Data
owns amber/green coverage, pass counts, reactive anchors, and correction
actions; Mix owns automation interaction. A per-workspace Layers menu can
reveal either read-only overlay without changing edit semantics. Perform owns
the live and rehearsal controls and starts with both optional overlays hidden.
The former full-width intent launcher is gone in favor of a compact action rail.

Mixing accepts click/drag ranges and Shift-extension. It also supports a sparse
point workflow: right-click a beat to set the start, then right-click an ending
beat to open the editor. Existing automation is a horizontal notation-like
bracket with hollow/filled downward triangles; right-clicking its line or
control points opens the mixing-only editor. There is no Base mix surface: the
existing global Orchestra volume scales the full spatial program. A draft
region opens a centered compact editor with stacked start/end sliders and
explicit continuous-interpolation copy.

The backend persists immutable, validated program revisions under
`profiles/<piece>/<movement>/mix-programs/<program-id>/`, checks every write's
observed revision, and implements atomic base-route, region create/update/delete,
and Undo operations. New Movement-II programs contain the agreed m.44 room-tail
and m.45 room-swell listening examples in addition to the 25% Yamaha / 100%
room-center base hypothesis.

Undo walks an explicit artistic-history parent, so repeated Undo reaches older
states instead of alternating between the last two revision files. Stale score
pins remain readable in list/get, are blocked from performance/edit writes, and
have an explicit review-and-rebind action. File-backed locking covers multiple
server processes as well as threads.

At live startup the selected document and current zone readiness compile once
into an immutable `MixPolicy`. The Yamaha renderer evaluates the transport score
position continuously, including between attacks and through held notes, and
emits CC7 only when its quantized value changes. Exact score-part routes take
precedence over the whole-`orchestra` stem. Note velocity and the independent
performer master-volume control remain intact. The BBCSO VST worker evaluates
the same policy at a 200 Hz control rate and renders MIDI to the room zone in
real time; no offline WAV is part of the performance path. The HDMI zone
remains `needs_calibration`, so live policy falls back safely to Yamaha until
the physical path passes the runbook gate; an explicitly labeled live audition
may preview that route before live admission.

## Test Contract

- Switching modes removes both visible out-of-modality layers and their pointer
  targets while preserving measure labels.
- Click and shift-click resolve identical canonical bounds under PDF zoom and
  page changes.
- Dragging either boundary cannot invert or escape the region.
- Context menus contain only actions for the active modality.
- Undo restores one complete durable revision.
- Repeated Undo walks backward through artistic history without toggling.
- Simultaneous worker-process writes produce one winner and revision conflicts
  for stale writers.
- Held-note CC7 automation advances without another MIDI note-on.
- Stale score identities remain listable and require explicit rebind before edit
  or performance.
- Reload reconstructs identical region geometry and envelope values.
- Audition never starts a follower or recording job.
- Perform cannot start while a Mixing audition remains active.

## Related

- [Calibrated Low-Latency Spatial Mixing](../concepts/psychoacoustic-spatial-mixing.md)
- [PWA Rehearsal UI](../concepts/pwa-rehearsal-ui.md)
- [Vision and UX Design](../VISION_AND_UX_DESIGN.md)
- [Decision 0016](../decisions/0016-pedalboard-decoupled-spatial-synth.md)
- [System Design](../SYSTEM_DESIGN.md)
