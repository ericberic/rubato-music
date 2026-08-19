# Recording Flow Redesign: Free Takes vs. Cued Takes

> A lead-in cue is a property of *starting from a known place in the score*,
> not a property of *recording*. The current UI models it as the latter, which
> is why it is incoherent.

Status: **implemented and converged on the causal follower** (§3.1–3.7,
including arbitrary-position **From here**, a selected-passage cue switch,
preflight authority/tempo plan, and compact passage-local recording review).
Decision 0015 retired the intermediate fixed cue transport; this document keeps
the diagnosis that motivated the UI, while the current behavior below is
authoritative.
Owner surface: the Take Capture deck in `webapp/src/App.svelte`, plus the
stage transport and (later) the Coverage face.
Extends: [Rehearsal Take Coverage Design](REHEARSAL_TAKE_COVERAGE_DESIGN.md)
§3.1–3.2; [Vision and UX Design](../VISION_AND_UX_DESIGN.md) §6.

---

## 1. Diagnosis: what is incoherent and why

The Take Capture deck presents one checkbox:

> ☑ Play orchestra lead-in cue before recording

(`App.svelte:1239–1247`, state `playLeadInCue` at `App.svelte:57`, default
**true**.) Five distinct problems hide behind it:

### 1.1 A mode is dressed as a preference

The checkbox reads like a DAW-style count-in toggle — an ornament on the same
action. It actually selects between two different recording *modes* with
different endpoints, different device requirements, different failure modes,
and different validity conditions:

| | Cued (checkbox on) | Free (checkbox off) |
|---|---|---|
| Endpoint | `POST /hardware/record-with-cue/start` | `POST /takes/record` |
| Requires | MIDI **input and output** | MIDI input only |
| Score position | Implicitly *the top* (first solo entry) | Anywhere; localized post-hoc |
| User protocol | Wait ~8 s for orchestra, enter on the theme | Play immediately |
| Meaningful for | One passage in the whole movement | Every passage |

A checkbox is the wrong control for a decision this consequential. The user
cannot see any of these differences; they discover them by hitting the "No
backend MIDI output selected for orchestra cue" error, or by getting eight
seconds of the movement's opening before a take of measure 60.

### 1.2 The cue's positional constraint is invisible

The cue start is computed as
`max(0, first_solo_entry_seconds − cue_seconds)`
(`src/aimusic/server/live_control.py:110–117`,
`first_solo_entry_seconds` fixed per movement in
`src/aimusic/accompaniment/oguri.py`). Nothing in the UI says this. The label
says "before recording," which implies "before *whatever* I'm about to
record." For any mid-piece take — the entire point of the take-coverage
system — the cue is musically unrelated audio that delays the take, burns the
"< 3 s between takes" budget (coverage design §3.1), and teaches the user a
false model ("the orchestra knows where I am"). The take still aligns
correctly post-hoc, so the system silently *works* while the UX *lies*, which
is the worst variant of the bug: nothing forces a correction.

### 1.3 The default is backwards

`playLeadInCue = true` makes the special case (from-the-top, cued) the
default and the primary workflow (free partial takes) the opt-out. The
coverage design's Position 4 is explicit: capture must be "one button, no
session naming, **no cue configuration required**." The default contradicts
the product's own thesis.

### 1.4 One flag secretly drives two different record buttons

The same `playLeadInCue` also changes the **stage** transport button
(`App.svelte:1171`, label flips to "Record · cued") and routes
`startHardwareRecording` (`App.svelte:558–623`) through the **legacy
session-slot path** (`ensureSessionSlot` → `/hardware/record/start` →
`/hardware/record/stop`). Recordings made from the stage button never enter
the take store, never get aligned, and never count toward coverage. So the
cockpit currently has two Record buttons whose *persistence semantics*
differ, both modulated by one checkbox whose *label* describes neither. A
user cannot predict whether pressing Record will produce a take.

### 1.5 The one legitimate capture-time distinction is dropped

The coverage design (§2.2) gives the cue exactly one architectural role: the
take's `cue` field is "a strong localization hint." But
`stop_take_record` (`src/aimusic/server/routes.py:475`) calls
`take_store.save_take(...)` without any cue metadata — `take.json.cue` is
always `null`, even for cued takes. The flows are distinguished everywhere
the user can see and nowhere the aligner could benefit. That is precisely
inverted.

**Root cause, in one sentence:** the UI hardcodes "cue from the top" and
presents it as a general recording option, when in the product's own
conceptual model a cue is a *parameter of a score position* ("give me context
before beat X") and the top is just one value of X.

---

## 2. Design principle

Make the user's *intent* the mode, and name each mode by what the pianist is
doing, not by what the machine will do:

1. **Just play.** (free take) — "I'm going to play something; figure out
   where it was." Available when no score passage is selected.
2. **From the top.** (cued take, v1) — "Start me at the beginning; give me
   the orchestra lead-in so I enter in tempo." An explicitly named,
   secondary action.
3. **From here.** (cued take, v2 — the generalization) — "Start me at
   measure N with a lead-in." Launched by tapping the score/coverage view,
   never by configuring the capture deck. This is the coverage design's §3.2
   "see gap → record gap" interaction; *From the top* is just *From here*
   with N = 1.

Consequences:

- The cue is not a global preference. Once a passage is selected, its compact
  workspace exposes an **Orchestra cue** switch immediately beside the one
  **Record pass** action. It defaults on for each newly selected passage and
  explains the musical result inline: where the orchestra starts, which bars
  it leads, where it follows, and the fitted tempo. Turning it off changes the
  same action to immediate recording at that measure. This local switch is
  safe because its scope and consequence are both visible at the decision
  point.
- Both modes produce **the same artifact**: a take in the take store, aligned
  by the same pipeline. A cued take differs only in carrying a localization
  hint. One data model, two entry points.
- The legacy stage-transport recording path is retired from the UI (see §5).

When a score passage is selected, the passage—not the transport mechanism—is
the primary intent. The deck presents one **Record pass** action under
**Record from m. N**, with the adjacent cue switch selecting cued or immediate
capture. Both retain m. N as an authoritative alignment hint. The uncued path
persists `placement_hint`; the cued path persists `cue`. A global Record button
and a competing From-the-top button are hidden while this selected-passage
decision is active.

Repeated recordings are also passage-local. The active workspace shows one
compact **Recording** picker rather than vertically expanding every pass.
Aligned passes, incomplete attempts, and their times/spans are selectable in
that control; Yamaha playback remains primary, while browser previews,
download, and recoverable exclude/dismiss actions live under **Recording
options**. The full rehearsal library keeps passage groups collapsed by
default and reveals their recording rows only on demand.

---

## 3. UI specification

### 3.1 Take Capture deck — idle state

```text
┌─ Take Capture ────────────────────────────────────────────┐
│                                                            │
│              [  ●  Record Take  ]          ← primary, ember│
│                                                            │
│         ▶ Cue first entrance · 2 measures  ← secondary,    │
│                                              ghost/brass   │
│                                                            │
│  ▸ Capture options                         ← disclosure,   │
│      Auto-stop after  [ — ] s                by default    │
│                                                            │
│  take 37 today · movement 56% covered                      │
└────────────────────────────────────────────────────────────┘
```

**Controls:**

- **Record Take** (primary button, ember outline, ≥ 64 px). Starts a free
  take immediately via `POST /takes/record`. Disabled only when no MIDI
  input is selected. Space bar is equivalent (existing keyboard law:
  space = primary action). When auto-arm ships (coverage design §3.1 /
  roadmap item 6), this button becomes the *explicit* start and the first
  note-on becomes the implicit one; the button stays for silence-first
  passages.
- **Record first piano entrance with orchestra** (secondary button, ghost
  style). The runtime plan immediately above it states the orchestra start,
  first FOLLOW location, support, and learned tempo before the action becomes
  available. One press starts the causal follower with capture through the
  cued endpoint. Disabled (with reason shown on hover/tap) when no MIDI output
  is selected or while the plan is resolving.
  No keyboard shortcut — it is the rare case, and mild friction is correct.
- **Capture options** (collapsed `<details>` disclosure):
  - *Auto-stop after* — the existing `hardwareRecordSeconds` field, demoted
    here. It is a testing convenience, not a performance control; it does
    not belong at the top level of a deck whose design target is "no
    configuration."

**Removed outright:**

- The "Play orchestra lead-in cue before recording" checkbox and the
  `playLeadInCue` state.
- The always-visible "Cue seconds" field.
- The conditional "Record · cued" label logic on any button.

### 3.2 Flow — free take ("Just play")

1. Press **Record Take** (or space; or, post-auto-arm, just play).
2. Deck enters recording state: elapsed counter dominant, **Stop Take**
   replaces both buttons, ember pulse. A small mode chip reads `free`.
3. Press **Stop Take** (or space). Existing toast lifecycle takes over:
   `Take 38 · 0:47 · placing it in the score…` →
   `Take 38 · mm. 24–41 · kept ✓`.

No step asks where the take is; that is the pipeline's job.

### 3.3 Flow — cued take ("From the top")

1. Press **Cue first piano entrance · starts 2 measures earlier**.
2. Deck enters a distinct **lead-in state** — visually *playback*, not
   recording: brass accents, state word `Lead-in`, and a countdown that
   answers the only question the pianist has:
   `Orchestra playing · your entry in 5 s` (the seconds are derived from the
   two score anchors and current source projection; they are not configured).
   A single **Cancel** button (esc = Silence also works, as always).
3. At the entry point the deck flips to the same recording state as §3.2,
   with mode chip `from the top`. Recording actually starts at cue start
   (the MIDI capture window includes the lead-in; alignment trims), but the
   *displayed* state change happens at the musical entry — the UI narrates
   the musician's timeline, not the process's.
4. Stop as in §3.2. Toast reads `Take 39 · from the top · mm. 1–17 · kept ✓`.

### 3.4 Recording state (shared)

```text
│  ● Recording · from the top                 ← mode chip     │
│                                                            │
│                     ● 1:23                                 │
│                                                            │
│              [  ⏹  Stop Take (space)  ]                    │
```

Identical for both modes except the chip. There is exactly one way to stop.

### 3.5 Stage transport

The stage's Record button (`App.svelte:1149–1172`) currently duplicates
capture with legacy session-slot semantics (§1.4). Two acceptable
resolutions, in preference order:

1. **Remove it.** The stage keeps Orchestra / Stop; recording lives in the
   Take Capture deck only. One action per surface (Vision §6 law), and the
   stage's job is playback and state, not capture.
2. If a stage-level Record must remain during transition: rewire it to be a
   *proxy for the deck's Record Take* — same endpoint, same take store, no
   cue variant, label plain `Record`. It must never create session slots.

Either way, `startHardwareRecording` / `startHardwareCueRecording` /
`ensureSessionSlot`-for-recording are retired from the UI path.

### 3.6 Coverage face — "From here" (v2)

Per coverage design §3.2, tapping an uncovered region is *the* coverage
interaction. Spec for the recording half:

1. Tap/click an uncovered span (strip cell or PDF region), e.g. mm. 58–66.
2. The selected-passage workspace states the runtime plan before enabling its
   action:

   ```text
   Record from m. 58
   Orchestra leads m. 58–60.
   Follows you from m. 61, beat 1 · 3 prior takes · ♩ = 64 learned.
   Orchestra cue [ On ]                     [ Record pass ]
   ```

   The exact measures come from the bundle's section policy, not a UI rule or
   fixed lookahead.
3. **Record pass** with the cue on starts the causal follower and records its
   Yamaha input. Turning the switch off uses the ordinary solo-recording path,
   so the score selection is never a trap.

Implemented backend contract: `POST /api/hardware/record-with-cue/start`
accepts optional `target_score_beat`. The route resolves the selected printed
measure, asks `LiveRuntimeManager.performance_plan(start_measure=...)` for the
authority/tempo plan, then calls `start_follow()`. Its runtime `run_id` is the
recording ID. Omitting the
target preserves first-entrance capture. The saved cue is `from_position`;
both paths stop through `/takes/{id}/stop` and enter the same
alignment/review pipeline.

### 3.7 Semantic runtime plan

Seconds are an implementation clock, not the pianist's cue vocabulary. A cued
take therefore exposes score positions:

1. **Orchestra start** — the selected printed measure.
2. **First FOLLOW position** — the exact measure/beat where the score gives
   timing authority to the piano.
3. **Subsequent LEAD/FOLLOW transitions** — derived continuously from
   `sections.json` and expectation roles.

The plan also exposes the fitted base tempo and support local to the first
FOLLOW bar. Support is provenance for “learned”; it does not decide authority.
The UI never derives these facts from a density threshold or from its global
tempo slider.

**A recorded accompanied pass is the live performance path, recorded.** The
follower predicts during orchestra-led material, corrects from Yamaha input in
solo material, and treats written silence differently from a mid-phrase
dropout. Only Yamaha input is stored as the take; scheduled orchestra output
remains a separate timeline. The cue metadata retained on the take is an
offline localization hint, not a fixed transport or handoff instruction.

---

## 4. Data and API implications

No new endpoints for v1; the change is who calls what, plus metadata.

1. **`take.json.cue` gets written, finally.** Cued takes carry:

   ```json
   "cue": {
     "kind": "from_top",            // or "from_position"
     "target_beat": 47.0,            // score beat of the actual piano entry
     "cue_start_beat": 44.0,         // selected runtime start (m. 12)
     "cue_seconds": 2.8,             // learned-tempo estimate to the pickup
     "output_name": "CLP-795GP USB"
   }
   ```

   The cue endpoint records this server-side keyed by take ID, and
   `stop_take_record` attaches it when finalizing the take. The server-side
   ownership survives a page reload mid-take.

2. **The aligner consumes the hint.** When `cue.target_beat` is present,
   localization (coverage design §2.4 Stage 1) restricts or strongly weights
   candidates near the hinted beat; if the take later lands `ambiguous`, a
   candidate consistent with the hint wins automatically. This is the entire
   architectural payoff of keeping a cued flow at all.

3. **Cued takes must stop via `/takes/{id}/stop`.** Already true in the
   deck's `handleRecordButtonClick`; becomes true everywhere once §3.5
   removes the legacy stop path from the UI.

4. **v2 addition (implemented):** the cue-record endpoint accepts
   `target_score_beat` for arbitrary-position orchestra playback + cued
   record.

---

## 5. Edge cases and sequencing constraints

- **Plan ownership.** The selected score measure and first FOLLOW position are
  canonical score identity. Derived source seconds are localization metadata,
  never runtime authority.
- **Orchestra tacet at the selected start.** Silence is not repaired with a
  lookback heuristic. The follower still predicts, expectation marks the
  silence as written or anomalous, and the preflight plan states the section
  authority honestly.
- **Mid-piece start tempo.** The runtime starts from the profile's fitted
  `base_seconds_per_quarter`, falling back to the movement default when no
  fitted profile exists. Position and authority do not depend on rehearsal
  support, so “From here” is never gated by a density threshold.
- **Hint vs. reality disagreement.** If a cued take's post-hoc alignment
  lands far from `cue.target_beat` (the pianist noodled somewhere else),
  trust the alignment, keep the take, and surface it normally; the hint is
  a prior, never a constraint. Do not warn — the take is fine.
- **Device asymmetry.** Free takes need input only; cued takes need input +
  output. With the checkbox gone this stops being a surprise error and
  becomes a disabled secondary button with a stated reason (§3.1).
- **Auto-arm interaction (roadmap item 6).** When first-note-on auto-start
  ships, it applies to free takes only. During a lead-in, note-ons before
  the entry point must *not* spawn a separate free take; the cued state owns
  the input. This is the one real sequencing hazard between the two modes —
  make the capture state machine explicit: `idle → (free-armed | lead-in) →
  recording → idle`, and only `idle` may auto-arm.
- **Migration result.** §3.1–3.6 now share one explicit capture state machine;
  free, from-top, and from-position takes differ only in their cue hint.

---

## 6. What this deliberately does not change

- The alignment pipeline, take store, toast lifecycle, and WS events are
  untouched; this is a re-presentation of existing capability plus one
  metadata field.
- The Silence law, space-bar law, and palette are inherited unchanged.
- Session slots remain as Library/legacy plumbing per the coverage design's
  migration plan; this doc only removes them from the *recording* path.

## Related

- [Rehearsal Take Coverage Design](REHEARSAL_TAKE_COVERAGE_DESIGN.md) — capture cockpit (§3.1), coverage interactions (§3.2), cue-as-hint (§2.2)
- [Vision and UX Design](../VISION_AND_UX_DESIGN.md) — surface laws, keyboard model
- `webapp/src/App.svelte` — current implementation being critiqued
- `src/aimusic/server/live_control.py`, `src/aimusic/server/routes.py` — cue and take endpoints referenced above
