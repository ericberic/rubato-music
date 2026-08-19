# PWA Rehearsal UI

## Summary

Rubato needs a local web UI/PWA because Eric will be at the piano and needs a
low-friction way to start, stop, recover, choose devices, set volume, and capture
feedback without editing code.

The UI should sit on top of a local FastAPI backend. Browser Web MIDI can help
for device access, but the backend owns score bundles, run artifacts, traces,
and long-running accompaniment sessions.

## User Workflows

### Rehearsal Mode

Eric should be able to:

- Select score bundle and excerpt.
- Select Yamaha MIDI input and choose Yamaha orchestral MIDI, VST-only live
  audio, or both through the pinned mix policy.
- Set accompaniment volume and mute/solo groups.
- Click `Start Listening`.
- See current measure/beat, confidence, tempo, and section mode.
- Click `Stop`, `Panic`, `Restart`, or `Jump to measure`.
- Add quick feedback tags: late, early, too loud, too soft, missed entrance.

The at-piano workflow is continuous. A captured take is not finished merely
because its MIDI reached disk:

```text
Record Take
  -> Stop
  -> hear / keep / nothing
  -> explicit keep
  -> place the take in the score
  -> prepare solo + retimed orchestra review
  -> hear them together
  -> record again or Go Live
```

Alignment and review preparation are system work, not buttons Eric must know
to press. Ambiguity is the one legitimate interruption: Rubato asks which
candidate location is correct, then resumes preparation from that choice.
There is no session creation, upload, CLI render, run-ID entry, or manual file
handoff in this loop.

Current scaffold:

- Backend MIDI device listing: `GET /api/midi/devices`.
- Backend hardware record: `POST /api/hardware/record/start` and
  `POST /api/hardware/record/stop`.
- Oguri movement-2 orchestra playback: `POST /api/hardware/play-oguri`.
- Named live-audio-zone discovery: `GET /api/mix/zones`; Go Live can omit
  `output_name` when the compiled mix has an active configured VST zone.
- Stop/panic controls: `POST /api/hardware/stop` and
  `POST /api/hardware/panic`.
- Offline accompaniment render from a selected solo take:
  `POST /api/sessions/{session_id}/render-offline`.
- Backend Yamaha playback for any available session MIDI variant:
  `POST /api/sessions/{session_id}/midi/{variant}/play`.
- Default Oguri orchestra volume is 75%.
- Backend recordings can be loaded directly into the existing MIDI player for
  immediate play/pause/scrub review.
- The generated accompaniment appears as the `accompaniment` playback variant
  and can be loaded, scrubbed, downloaded, played from the browser MIDI player,
  or sent to the selected Yamaha backend output.

Those session APIs remain useful developer/Reflection plumbing, but post-take
review no longer depends on them. The take API owns capture, alignment, review
preparation, the combined review artifact, and Yamaha playback. It does not
reuse the old coincidence that hardware capture also left a session-shaped
copy under `data/processed/<take_id>`.

### Take-native accompanied review

The first-class review path implements the following contract:

- The selected `take_id`, its immutable raw solo MIDI, its resolved alignment,
  and the bundle/timeline revisions are explicit inputs. Review must not depend
  on constructing a session slot with the same string ID.
- The UI automatically queues a durable, idempotent `render_review` job for
  the latest aligned take. A candidate resolution returns the take to the same
  path. Unalignable and failed takes do not silently render.
- The worker consumes `take.mid` and the authoritative `aligned.v2.json`
  timing map, crops the orchestra source to the take's aligned score span, and
  writes normalized `solo.mid`, retimed `accompaniment.mid`, and combined
  `ensemble.mid` under the take's run output directory. “Play accompaniment”
  alone is not an accompanied review.
- Job state is restart-safe and idempotent. `TakeResponse.review` and
  `GET /api/takes/{take_id}/review` provide recovery snapshots;
  `take:review_status` advances the mounted UI after committed transitions.
  `POST /api/takes/{take_id}/review` deduplicates by its take/alignment input
  revision, and queued/running work is recovered at server startup.
- Playback claims the existing process-wide Yamaha hardware slot, uses the
  output selected during Sound Check, and remains interruptible by
  **Silence**. Both review endpoints accept an explicit `solo` or `ensemble`
  variant. `POST /api/takes/{take_id}/review/play` can also accept a displayed
  `start_score_beat`; the backend interpolates it through the take's dense
  transport and starts both MIDI and cursor at the same take-relative time.
  `GET /api/takes/{take_id}/review/midi` supplies the chosen artifact for
  browser preview or download.
- The focused **After this take** card narrates honest indeterminate
  states—`Placing it in the score…`, `Starting orchestral review…`,
  `Preparing orchestra playback…`—then
  presents one hero action, **Hear take + orchestra**. Secondary actions are
  **Preview here** and **Solo only**; the next-loop actions are **Record
  another** and **Go live · Experimental**.
- Failure is local and recoverable: placement ambiguity offers candidate
  choices in the take list; render failure offers **Retry orchestral review**;
  an unavailable output directs Eric back to Sound Check selection/refresh.
  The raw take and successful alignment are retained.

The existing synchronous session render endpoint remains available for
developer/Reflection file workflows, but session/slot vocabulary and
individual MIDI variants do not belong in the rehearsal face. See
[Offline Alignment And Render](offline-alignment-render.md) for the current
renderer boundary.

### Source Prep Mode

Eric or an agent should be able to:

- Register source files for a score bundle.
- Mark authoritative solo/accompaniment files.
- Review part/instrument mappings.
- Edit section boundaries and modes.
- Attach notes to ambiguous measures.

This can start as JSON/file editing plus a thin UI; it does not need a full score
editor for the MVP.

### Review Mode

After a run, the UI should show:

- Solo MIDI recording.
- Accompaniment MIDI output.
- Score-position trace.
- Tempo trace.
- Section-mode trace.
- Feedback notes.
- Download/export buttons.

## Backend Responsibilities

FastAPI backend should provide:

- Score bundle registry.
- Session/run creation.
- MIDI device enumeration or bridge to browser Web MIDI.
- Start/stop/panic endpoints.
- WebSocket stream for live status.
- Trace writing under `runs/<run_id>/`.
- File upload/download for MIDI and score sources.
- Server lifecycle: start via `dev-server.sh` (dvc pull, build, readiness
  wait, auto-open); stop via Ctrl-C in that terminal or a confirmation-gated
  in-UI control (`POST /api/server/shutdown`).

## Target UI

The target interaction design is owned by
[Vision and UX Design](../VISION_AND_UX_DESIGN.md): one window with three
faces (Ready / Live / After) plus a Library drawer, with device setup
automated as a Sound Check rather than a screen. An earlier four-screen
sketch (Setup / Perform / Review / Sources) is superseded by that document.

### Interaction principle: progressive disclosure

The score and the next musical action own the primary surface. Controls that
change one concern belong in one nearby disclosure, not in a second distant
panel: **Sound** groups orchestra destination, volume, input, and renderer
lifecycle preferences beside Go Live. Its closed label carries the value most
likely needed at a glance. Readiness stays visible outside the drawer because
it gates action; device inventories and lifecycle preferences do not. Failure
copy names the recoverable cause and offers one local action. This compact
surface + truthful status + contextual disclosure pattern is the default for
new performer-facing controls throughout the PWA.

## Current Implementation: Movement 2 Performer Cockpit (2026-07)

The Svelte app (`webapp/src/App.svelte`) implements one coherent Movement 2
performer context. A Ready / Live / After rail is driven by existing capture,
hardware, preview, and completion state; the content remains an explicit
staged shell rather than three fully separate views. This is the actual state
after the 2026-07 bundle-v2 cockpit integration; treat
[Vision and UX Design](../VISION_AND_UX_DESIGN.md) as the target and this
section as what exists today.

- **The intent launcher and stage**: a compact full-width launcher sits above
  the persistent score and capture controls, followed by the piece stage with
  its Fraunces title and color-coded state word with a breathing pulse dot
  (Ready/Leading/Following/Playing/Recording/Stopping,
  ink-dim/brass/ember), followed by three mutually distinct musical intents:
  **Perform live with orchestra**, **Rehearse a passage**, and **Listen to
  orchestra only**. The first names the orchestra-led opening and solo entry;
  the second moves focus to the selectable score and take controls; the third
  explicitly says that Rubato will neither listen nor follow. There is no
  context-free `Orchestra` play button and no second, detached Go Live button.
  When idle, Space does not guess an intent; while anything is active it is a
  stop shortcut. Recording lives in the Take Capture deck only (see
  [Recording Flow Redesign](../design/RECORDING_FLOW_REDESIGN.md) §3.5).
  The compact **Sound** disclosure beside Go Live owns the Orchestra volume
  fader (0–100, default 75), backend Yamaha input, exclusive orchestra output
  (Yamaha MIDI or LG/REAPER/BBCSO), REAPER readiness preference, and device refresh.
  Empty in/out dropdowns carry a reason rather than
  silence: `MidiDevicesResponse.backend_available` distinguishes "no
  `python-rtmidi`/`live` extra installed" (`mido.get_input_names()` raises
  `ImportError`, caught in `midi_ports.list_midi_ports`) from "backend fine,
  zero ports currently visible" (USB unplugged, port held elsewhere) — both
  used to collapse to the same empty `inputs`/`outputs` lists, which is why
  telling them apart previously required a terminal `mido` one-liner
  (rubato#98).
- **Perform live**: the primary intent is wired to the causal live
  runtime (`GET /api/runtime/plan`, `POST /api/runtime/follow/start`,
  `GET /api/runtime/status`, `GET /api/runtime/renderer/status`,
  `POST /api/runtime/renderer/preload`,
  `POST /api/runtime/stop`) — reuses the same in/out selection as
  Orchestra/Record rather than a separate device picker, so there is nothing
  to re-enter. `bundle_id`/`revision` are fixed to the one registered
  Movement 2 bundle (`revision: null` resolves it; only one revision is
  registered). Before any port is opened, the plan card names the musical
  contract: **Orchestra-led opening · starts at m. 1**, **enter at m. 12,
  beat 4**, and the starting BPM/profile evidence. The adjacent global tempo
  control is a conventional, editable quarter-note metronome mark (`♩ = N`),
  never a reference-file percentage. The same exact BPM feeds orchestra-only
  playback, rehearsal cue-ins, and this live opening; when untouched it starts
  from the profile's learned suggestion. The action is correspondingly
  explicit: **Start live performance**. The server starts the first sounding
  orchestra event immediately, advances the score cursor without piano input,
  waits at the solo handoff, then follows the Yamaha after symbolic lock. A
  state word (`Not live` when idle, otherwise
  `RuntimeStatus.state_word`: `Listening`/`Following`/`Leading`/`Waiting`/
  `Silent`) sits beside a persistent "Experimental" badge — the runtime
  reports `canonical_position: false`/`performance_ready: false` and this is
  never hidden. **Silence** additionally calls `POST /api/runtime/stop` when
  a run is active, alongside its existing hardware panic/stop calls. State
  updates arrive over the existing `WS /api/events` connection as
  `runtime:status` (`LiveRuntimeStatusEvent`) — this event and its
  publish-on-transition/throttle behavior already existed server-side in
  `live_runtime.py` before this control was added; only the frontend
  subscription is new (rubato#96, rubato#97; see [Realtime Performance
  Dataflow](realtime-performance-dataflow.md) for the event's backend
  lifecycle). Every performance is recorded proactively under its visible run
  ID. After stopping, the UI offers Yamaha playback, explicit Mac preview,
  **Keep for piano + orchestra review**, or **Nothing for now**. The recording remains ephemeral scratch
  evidence; only the explicit keep action promotes it into alignment, review,
  and the learned rehearsal profile. Clicking a printed score measure
  also changes this action to **Count off + start m. N**. The card names the
  one-bar count-off and the selected measure's `FOLLOW`/`LEAD` authority; the
  request carries `start_measure`, never a UI-derived source-performance beat.
  The automatically suggested next rehearsal target does not opt into a live
  mid-piece start; only a deliberate score/passage selection does.
  This remains a guarded developer path in spirit — the
  Experimental badge says so — later Movement II section policy and
  concert-readiness validation remain incomplete even though the opening is no
  longer curl-only or piano-start-only.
  A collapsed **Advanced Yamaha timing** control stores a 0-100 ms output
  advance for the next live run. Its copy explicitly limits the control to
  compensating a measured, repeatable output-path delay; ordinary use stays at
  zero, and score-following errors are diagnosed from the trace instead of
  being hidden with this setting.
  The Sound drawer chooses exactly one orchestral renderer. Yamaha sends MIDI
  to the piano and is ready independently. LG/REAPER/BBCSO sends no doubled
  Yamaha accompaniment; **Keep REAPER ready** opens Rubato's virtual MIDI source
  when the PWA mounts. A compact score-adjacent badge consumes
  `runtime:renderer_status` and exposes the REAPER bridge's project, track/FX,
  MIDI, audio-engine, and exact-device readiness, exact failure, and Retry
  without moving the main action away from the score.
- **The rehearsal score (persistent workstation)**: the DVC-managed, 15-page
  Joseffy two-piano reduction for Movement 2, served by
  `GET /api/scores/2/pdf?piece_id=chopin_op11`, remains visible beside the
  rehearsal controls at desktop widths. Capture and post-take review no longer
  require scrolling the score out of view. The score is the primary performer /
  system communication surface, not a report above a stack of controls.
  PDF rendering is independent of geometry, and the current machine-review
  Audiveris artifact supplies 126 exact measure rectangles. The cockpit
  displays them while honestly identifying review as pending. The legacy
  Movement 1 full-score overlay is no longer exposed beside Movement 2 data.
- **Score position and take span**: a position cursor is painted over the score
  whenever Rubato has a score-time transport: orchestral cue, accompanied
  review, or live follower state. Hardware status carries sparse score
  transport anchors; the browser interpolates the score beat from `started_at`
  every 100 ms, resolves the active measure from the canonical display
  timeline, and scans within the true OMR measure box instead of snapping to a
  measure center. Live/playback position owns page selection ahead of the idle
  next-rehearsal target. A page raster and its SVG geometry switch together;
  the UI never draws a new page's cursor over the previous canvas. Browser
  regression tests expose measure/page/system/X attributes, assert that the red
  line lies inside its named measure, and save screenshots at late-movement
  landmarks through m.100. A 750 ms lookahead preturns the PDF page. The latest take's
  performer span ends at its final matched musical onset; trailing sustain or
  armed silence is not evidence of a later score location. It is a separate
  labeled overlay and stays distinct from
  accumulated coverage so “this is the take I just played” never masquerades
  as “this passage has enough takes.” The map uses canonical measure/beat
  coordinates while its evidence remains labeled machine-reviewed. The
  build-time `performance_beat_map` has one reference-MIDI anchor per
  recognized beat, including expressive beat knots through the large m.17
  rubato and piece-specific B-natural pickups in m.12 and m.53. The PDF cursor
  interpolates only inside one beat using Audiveris note-x geometry. Yamaha playback and
  browser preview consume the same dense transport instead of letting browser
  preview stretch a take linearly across only its start/end span. Browser
  preview loads that MIDI-derived detail on demand from
  `GET /api/takes/{take_id}/score-transport`; the ordinary take-bank list stays
  metadata-only and does not parse every recorded MIDI file on each refresh. The score
  canvas and SVG hit surface disable native text/replaced-element selection so
  a slightly moving click cannot cover the PDF with Chromium's blue selection
  paint. Every visible measure box now carries an `m.` label. The selected
  passage is marked `Next take`, the current cursor is marked `Now`, and the
  newest recording is one continuous, labeled dashed span per system. A nearby key
  names every visual layer, and an automatic next-passage suggestion opens the
  containing PDF page so the control card and engraving always correspond.
  Rehearsal-passage primary play actions always use the selected Yamaha backend and
  say so in their labels; only controls explicitly labeled **In this browser**
  invoke the browser synthesizer / computer audio output.
- **Orchestra origin**: Oguri movement 2 contains 4.202 seconds of setup and
  silence before its first audible orchestral event, Violin I E4 at native
  tick 2017. The Joseffy reduction engraves that E-natural on beat one of
  printed measure 1. Ordinary from-the-top orchestra playback therefore trims
  this technical preroll and defines tick 2017—not MIDI tick zero—as the
  displayed measure-1 downbeat. Playback diagnostics log the source offset,
  scheduled event count, and first relative event time. This fixes the opening
  silence/cursor disagreement; later local measure anchors remain independently
  responsible for the checked mid-piece projection.
- **Coverage detail**: the score hero reports the percentage of solo measures
  *observed in at least one kept take*. Mature coverage remains the stricter
  `n_target` quantity: with the default target of three takes, a first take
  paints its measures amber/touched, not green/covered. The 126 rows come from
  the Bundle v2 draft timeline, not fixed groups of Oguri MIDI beats;
  `mapping_review_state: machine`, anchor confidence, and explicit unmapped
  measures keep that limitation visible while still showing honest rehearsal
  progress. Readiness is not identical to rehearsal coverage:
  `accompaniment_required` is derived from canonical orchestral events. A bar
  with no orchestral notes (m.16) is green/ready even with no piano take,
  because Rubato has nothing there to miss; orchestra-led bars are ready for
  the same score-owned reason. The coordinate itself remains canonical.
- **Choose the next take on the score**: the declared Oguri solo-reference
  track is projected through the same machine score map to mark piano-active
  measures. Uncovered measures are selectable directly on the PDF and strip;
  the first uncovered measure after the latest take is suggested. **From
  here** sends its display beat to the cue API, which inverts the score map and
  schedules an arbitrary-position orchestra lead-in. The resulting
  `from_position` cue remains only a localization hint and the take follows the
  ordinary durable alignment/review pipeline. A measure click identifies the
  passage, while the cue resolves to the first piano onset *inside* that
  measure. This distinction matters at m.53: the orchestra plays most of the
  bar before the solo B-natural pickup. The cue begins at the m.51 barline and
  the entry remains that m.53 pickup rather than the m.53 barline. The deck
  describes this as “before your first piano note,” and cue diagnostics retain
  selected measure, resolved source time, printed beat, pitch, event counts,
  and MIDI devices.
- **Edit reactive anchors where the music is**: there is no global anchor mode
  to find and then carry back to the working measure. Right-clicking the exact
  bass onset opens the anchor actions beside that notation. Amber anchor lines
  have wide pointer targets and drag horizontally; selecting a line exposes
  direct delete, and a measure with multiple accidental marks exposes one
  clear-all action. Add, move, delete, and clear retain immediate local Undo.
  Move, clear, and multi-anchor restore cross the REST boundary as atomic
  operations, so the browser never approximates one edit with a racing sequence
  of add/delete requests.
- **Exceptional cursor correction**: ordinary rehearsal is fully automatic.
  While a score transport is sounding, a collapsed **Cursor not on the sound
  you heard?** control can freeze that exact source moment. The performer then
  selects its printed measure and one of beats 1–4. This sparse statement is
  validated, persisted outside the immutable bundle, revisioned into the map
  identity, journaled, and can requeue the affected take. It is a fallback for
  major evidence failure, not a measure-labeling workflow or terminal task.
- **Data timing audition**: Data → Timing narrows the Oguri-to-canonical map to
  bars that **need listening** because two independent alignments reject the
  current timing, plus sparse movement-wide **spot checks** that can catch a
  drift shared by those alignments. These are review priorities, never claims
  that ground truth is already known. Orchestra-tacet bars stay in the machine
  diagnostics but are omitted from this orchestra-only listening queue: silence
  cannot validate accompaniment timing, and the solo-driven canonical follower
  traverses those bars independently. The global measure strip and printed
  score are the one navigator: clicking either immediately selects and loads
  that measure's audition. There is no duplicate measure-number form, refresh
  button, or explanatory side-panel worklist. A compact row names the exact
  review measures, while the sticky global strip makes the same priorities
  unmistakable during score scrolling (bright amber needs listening, strong
  ivory outline spot check, green heard this session, neutral unflagged).
  Right-clicking a printed measure opens one local action, **Play**, and uses
  the same native-selection guard as left-click so Chromium cannot paint the
  score canvas blue. Timing mode temporarily owns this overlay vocabulary
  instead of showing rehearsal-coverage colors.
  Playback sends four metronome clicks and
  then exactly one Oguri orchestral measure, with a click at every mapped beat,
  through the selected MIDI output. Orchestra and clicks share one backend
  clock; browser audio is not used for the verdict, because two clocks could
  manufacture the offset being tested. Looping is opt-in and waits for the
  backend hardware job to become idle before scheduling the next pass; brief
  CoreMIDI/VST teardown is not exposed as a raw HTTP 409. The global Controls
  panel exposes a
  saved Orchestra level and a separate, quieter Metronome level in both Data
  and Perform; the metronome level also applies to latency calibration. Any
  measure can be opened directly even after it clears the suspect list. When a
  boundary sounds shifted, the correction surface groups rolled/simultaneous
  orchestral note-ons into musician-readable attacks and asks **Which sound
  begins printed m.N?** Everything before the selected attack belongs to the
  preceding measure. The human supplies that identity; the selected group's
  earliest native MIDI tick supplies precision. This avoids asking the
  performer to translate what they heard into “the map is early/late by one
  beat,” which is both awkward and error-prone.
  When the boundary is right but the pulse is wrong, **Clicks don't line up
  with the orchestra?** opens a proportional one-measure ruler. Four numbered
  markers show the current metronome clicks and lettered markers show grouped
  orchestral attacks in heard order. The performer chooses a printed beat
  (1–4), then the attack belonging on it; the backend persists that attack's
  exact native MIDI tick. Internal subdivisions remain visible but unassigned,
  so a dotted-eighth/sixteenth figure is not mistaken for two quarter beats.
  No floating-point times, “wrong note?” vocabulary, tapping, or hand-measured
  offsets enter the interaction.
- **Mixing is a separate score modality**: mix-program authoring does not add
  more marks to this rehearsal surface. Entering **Mix authoring** hides
  amber/green coverage, latest-take spans, reactive anchors, and alignment
  correction actions, then installs a different click/shift-click region
  grammar and mix-only context menus. Both workspaces reuse the PDF renderer,
  canonical geometry, local popovers, drag handles, and durable Undo; they do
  not share visible layers, persistence, or edit commands. See
  [Mix Authoring Mode](../design/MIX_AUTHORING_MODE.md).
- **Keyboard model** (per the vision doc): Space triggers the situation's
  primary action — stop the active take if recording, cancel the lead-in if
  cueing, stop playback if playing, hear the latest ready accompanied review,
  record another take, or start the orchestra as the available context
  permits — and Escape triggers Silence. Space is ignored while a form
  control has focus.
- **Rehearsal controls** (redesigned per
  [Recording Flow Redesign](../design/RECORDING_FLOW_REDESIGN.md)): a selected
  score passage becomes a single **Record from m. N** intent and one **Record
  pass** action. An adjacent, passage-scoped **Orchestra cue** switch defaults
  on. Before recording becomes available, a plan card states where the
  orchestra starts, which bars it leads, the first measure/beat where it
  follows, local prior-take support, and the learned quarter-note tempo.
  Turning the switch off makes the same action record immediately and labels
  the pass as solo recording. Both paths preserve the selected
  location for later alignment; the latter sends
  `target_score_beat` to `POST /api/hardware/record/start` and retains a
  `placement_hint` with the scratch performance. With no selection, the free
  and first-entrance cue actions
  remain available. The cue switch is not a global preference and there is no
  duration choice. A
  collapsed "Capture options" disclosure holds only the auto-stop-after field.
  A cued pass starts `LiveRuntimeManager.start_follow` with a performance ID. The
  follower, tempo model, section policy, scheduler, selected outputs, cursor,
  and recording therefore share the causal performance path. Cued recording
  still requires a real MIDI output; the explicit **None — orchestra via VST
  only** choice is currently scoped to Go Live. The fitted
  `base_seconds_per_quarter` supplies the start tempo; the request has no UI
  tempo or orchestra-only field. `take.json.cue` carries only the localization
  hint (`kind`, entry and cue-start score anchors, derived duration, and
  `output_name`)—no fixed clock or take-level handoff. `Stop Take` ends the
  hardware/runtime job and exposes one sticky decision card regardless of
  whether this was a cued rehearsal, uncued rehearsal, or live performance.
  **Keep as rehearsal take** calls `POST /api/performances/{recording_id}/keep`;
  only then do both paths converge on the take-store pipeline. The plain "Latest
  take" card described in earlier drafts is superseded by the focused **After
  this take** card plus the full takes list (play / discard / download per
  take, and candidate choices for `ambiguous` takes).
- **After this take**: while capture is idle, the latest take gets a persistent
  post-take card. Alignment completion causes the client to enqueue its
  take-native review when no review snapshot exists. The card follows
  `TakeResponse.review` and `take:review_status` through queued/running/ready/
  failed states, offers retry without losing the take or alignment, and makes
  **Hear take + orchestra** the hero action once `ensemble.mid` is ready.
  **Preview here**, **Solo only**, **Record another**, and **Go live ·
  Experimental** keep diagnostic and next-loop choices adjacent without a
  Library detour. The card says the ordinary persisted take is **in the
  bank** or **kept**; it does not imply danger by calling it “safe.” Space
  plays the newest unreviewed ready ensemble before it starts another take.
- **Scratch performance decision**: every pianist performance is recorded
  proactively, but capture and dataset curation are separate decisions. The
  decision card is sticky below the masthead so it remains visible from the
  score position where the performer stopped. **Hear piano on Yamaha** is the
  at-piano default, while **Preview on Mac** remains an explicitly labeled
  diagnostic. **Keep for piano + orchestra review** promotes cue/placement metadata
  and MIDI atomically, and **Nothing for now** leaves the recording out of
  alignment, review, coverage, and the Interpretation. A long kept performance
  is not split into artificial takes: canonical half-quarter cells receive one
  quality-weighted vote from each take that covers them, so long and short
  overlapping passes combine locally without length-based domination.
  Scratch identity is persisted beside `solo.mid`, not only in process memory.
  A restart or stale terminal runtime status therefore cannot reinterpret an
  already-kept scratch run as a second hidden durable take.
- **Selected-passage review and rehearsal passages**: selecting an amber
  passage or choosing **Show on score** from a recording opens one review card
  at that score location. It names the measure, the number of aligned passes
  crossing it, and the selected recording's covered range. A compact
  **Recording** picker switches among aligned passes and incomplete attempts
  at that entry. Each option uses a passage-local pass number, an explicit
  12-hour local timestamp, and the observable score-note match (for example,
  **Pass 3 · Strong alignment (81% score match) · 7:39 PM**), rather than an
  unexplained 24-hour number or a generic “aligned pass.” The selected pass
  exposes matched/extra/missing note counts and says whether its placement was
  ambiguous; note match is an alignment-fit statistic, not a calibrated
  probability. Library groups use the same one-line picker and a compact
  icon toolbar. Every icon has a familiar visible symbol plus an accessible
  name and tooltip. This follows the DAW pattern of keeping alternate takes
  hidden until the performer chooses to inspect them, while preserving the
  score location as the primary object. The card then
  separates destination (**On Yamaha** / **In this browser**) from content
  (**Take only** / **Take + orchestra**). Playback starts at the selected
  measure when it lies inside the take and otherwise at the take's first
  aligned point; the red cursor uses the same sliced transport. The library is
  filed by score entry rather than a global `Take N` counter: **From measure
  12**, **From measure 53**, and so on. Repeated recordings at one entry are
  local passes inside that passage; failed placement attempts remain visible
  but do not inflate the aligned-pass count. A zero-note attempt is described
  as an incomplete recording rather than an alignment error, stays filed at
  its selected entry, and offers the same cue-enabled retry instead of
  advancing the next-passage suggestion. Exclude, restore, and browser
  diagnostic actions are secondary **Recording options**; raw MIDI is stored
  automatically and is not presented as a routine at-piano download task.
  Measure labels on the PDF expose
  how many kept aligned passes cross each measure. Each recording retains its
  time, span, alignment state, and whether the rehearsal profile is learning
  from it. Repeated passes are not mixed or averaged as audio: each kept
  aligned pass contributes timing,
  dynamics, and pedal cell observations, which the profile fuses with a robust
  quality/recency-weighted median and spread. More passes increase support and
  reduce the influence of a single mistake. The passage card reports an
  evidence score (`support × mean alignment quality × consistency`), shared
  timing-cell count, learned quarter-note pace, typical MAD, and the few
  measure/beat cells with materially high variance. One pass establishes a
  curve; three passes can estimate spread and reject an outlier.
  Amber and green are instructions, not verdicts: amber means aligned evidence
  exists but at least one half-beat has fewer than the target three observations
  or less than 50% average alignment quality; green means every half-beat has
  reached both targets. The selected-measure card states the failing threshold and
  whether another pass is recommended. Right-clicking a recognized measure
  opens the same passage-local record, latest-take playback, accompanied
  playback, and detail actions without making the performer hunt through the
  right rail.
- **Orchestra controls**: volume and pace are separate global rehearsal
  controls. Pace ranges from 50–200%, persists locally in the browser, and is
  applied to whole-orchestra playback and future cue preparations. It never
  time-stretches an already aligned take-plus-orchestra review.
- **Event-driven state**: the cockpit takes one REST snapshot at mount and on
  every WebSocket reconnect, then follows typed lifecycle events. Hardware
  controls consume `hardware:status`; take placement consumes
  `take:alignment_done`; resident BBCSO readiness consumes
  `runtime:renderer_status`; the score refreshes on `coverage:materialized`, after
  the derived artifact is actually committed. The former 1 s hardware-status
  loop and 1.5 s alignment loop are removed. Lead-in, elapsed-time, and local
  preview timers remain because they render clocks rather than discover
  backend state. Every publication is also written to the ordered local
  `data/logs/rehearsal-events.jsonl` diagnostic journal before best-effort
  socket delivery, so a disconnected browser does not erase the rehearsal
  timeline. Background worker failures add tracebacks and take/session/job
  correlation to that same journal; persisted take and job artifacts remain
  the authoritative recovery state. Alignment-completion events additionally
  retain the placement basis, selected target tick, aligner, match rate, and
  matched/extra/missing counts. Retryable software failures remain distinct
  from musically unalignable takes and expose **Retry score placement**.
- **Preview deck**: the local WebAudio transport (play/pause, scrub, loop)
  for auditioning a loaded take, combined accompanied review, or file in the
  browser — not the live hardware output — plus `Open MIDI File…`.
- **Library drawer**: a collapsed `<details>` at the foot of the page holding
  session plumbing away from the at-piano loop — session create/select, solo
  upload, `Use Loaded File as Solo`, and the outputs/playback list from
  `SessionStatusResponse` (`solo.mid` / `accompaniment.mid` readiness, `Play`
  for local preview, `Yamaha` for real hardware playback via
  `POST /api/sessions/{id}/midi/{variant}/play`, `Download`). The web e2e
  test opens this drawer before driving session controls. It also holds
  **End session**, a confirmation-gated (`window.confirm`) button calling
  `POST /api/server/shutdown` — deliberately not a stage-level control given
  the misclick risk during rehearsal (rubato#100). The route 503s unless the
  process was started via `aimusic.server.app.main()`, which builds an
  explicit `uvicorn.Config`/`Server` pair and stashes the server on
  `app.state.uvicorn_server` instead of the `uvicorn.run("module:app", ...)`
  convenience wrapper — the latter throws the handle away, leaving no way
  for a request handler to trigger `should_exit`. The shutdown itself is
  deferred ~0.3s past the response so it can flush before the process exits.
  Ctrl-C in the setup terminal remains a fully valid, untouched fallback.
  Library session creation, upload, render, and per-variant playback are
  developer/Reflection plumbing; they are not prerequisites for reviewing a
  take in the target at-piano loop.
- **Silence**: an all-notes-off/all-sound-off emergency control (labeled
  "Silence" rather than the MIDI-world "Panic," which read as alarmist for
  this UI's calm tone) that lives in a sticky top masthead rather than a
  floating corner button — an earlier floating variant overlapped the Record
  control at some scroll positions, a real misclick risk during rehearsal.
  It calls `POST /api/hardware/panic` followed by `POST /api/hardware/stop`.
  The hardware lifecycle publishes `stopping` and terminal
  `completed`/`failed` phases, so the client does not poll to discover when
  the shared Yamaha slot is safe again.
- **Score bundle health banner**: a one-line warning under the masthead,
  shown only when `GET /api/scores/2/health?piece_id=chopin_op11` reports
  non-empty `missing_artifacts` (see [Score Bundle v2
  Contract](score-bundle-contract.md#dvc-presence-vs-bundle-readiness)) —
  "N score artifacts haven't been pulled yet — run `dvc pull` in the
  terminal." Read-only detection; there is deliberately no in-UI retry that
  shells out to `dvc pull` (rubato#101).

There is no client-side Web MIDI device enumeration or browser-captured
recording path anymore — the backend hardware endpoints are the single
source of truth for both input and output, matching the "Backend Yamaha
input/output selector" requirement directly instead of exposing two
differently-scoped device pickers.

The score source is intentionally registered independently from the legacy
event bundle. Normal startup pulls it along with the other DVC artifacts:

```bash
./scripts/dev-server.sh
```

## Related

- [Realtime Performance Dataflow](realtime-performance-dataflow.md)
- [Mix Authoring Mode](../design/MIX_AUTHORING_MODE.md)
- [Yamaha CLP-795GP](../sources/yamaha-clp-795gp.md)
- [System Design](../SYSTEM_DESIGN.md)
