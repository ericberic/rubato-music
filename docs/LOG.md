# Knowledge Log

## [2026-08-13] LG hardware acceptance | Score-time releases reach BBCSO

After reboot cleared macOS's wedged HDMI/CoreAudio clock, a silent 100-block
PortAudio probe and resident BBCSO preload both reached `LG TV SSCR2`. The first
complete PWA playback exposed one remaining Rubato defect: short score notes had
finite 0.2--2.1 second durations, but produced no ordinary note-offs. The VST
audio timeline trails the control loop while its bounded blocks cross the
process mixer and CoreAudio. `_retime_active_releases` clamped every score-time
release to wall-clock `now`; every control tick therefore moved the deadline
forward again, and the audio renderer could never catch it.

Release retiming now passes the unmodified score-derived deadline to the output
adapter, whose own rendered cursor decides whether it is pending or already
due. Applied VST retimes are explicit `midi_output/release_retime` evidence.
Regression coverage models an audio timeline intentionally behind wall time.

Hardware run `live-codex-noteoff-final` then played the autonomous Movement-II
intro through BBCSO at the 15% PWA fader. It dispatched 17 note-ons and emitted
13 ordinary note-offs before the 12-second stop; the four legitimately active
notes were cleared by the plug-in panic barrier, leaving no notes sounding.
There were zero late MIDI events, zero audio underruns, and all four patch hosts
remained resident with the renderer Ready after the run. Steady-state render
p95 was at most 6.1% of one block, confirming that the original blur was a clock
domain bug rather than CPU or memory exhaustion.

## [2026-08-13] BBCSO lifecycle fix | One HDMI writer and an honest score clock

The blurred ten-second orchestra test was not caused by absent score durations
or a Mac compute/memory limit. BBCSO steady-state p95 render utilization stayed
below 6% for all four collapsed ensembles. Four boundary defects compounded:
continuous release retiming could flood child command queues; the scheduler
clamped score-time releases to a later wall clock; four independent Pedalboard
writers to the same HDMI output stopped draining; and BBCSO's first concurrent
block after resume had variable native wake latency while the score clock had
already started. The latter defects left the renderer behind wall time, so
panic cleared releases that had not reached the plug-ins yet.

The resident renderer now coalesces each event to one in-flight release update,
continuously drains worker responses, and uses one block-indexed zone mixer over
four isolated one-patch hosts. Missing/misaligned blocks fail visibly rather
than playing a partial orchestra. A sacrificial first-block handshake rebases
all workers to one origin before readiness. Stop/panic waits for plug-in-bound
all-notes-off acknowledgements before moving the resident trace lease. A native
ten-second probe dispatched 40 note-ons and 40 note-offs, observed eight panic
controllers, left no notes sounding, and kept every worker at 10.055 seconds.
One intermittent BBCSO `SIGSEGV` recovered through the single bounded cold-host
retry; a hung cold host is now bounded at 30 seconds and receives the same one
retry.

Pedalboard remains the VST host, but live zone output now uses one
sounddevice/PortAudio stream. On the final hardware check, macOS still exposed
`LG TV SSCR2` as the 48 kHz default device but rejected Pedalboard, PortAudio,
and native `afplay` with `'stop'` / `Audio Hardware Not Running`; this is an HDMI
clock/device-state failure, not renderer readiness. The PWA must keep only that
route unavailable until the LG route is reconnected/power-cycled or Core Audio
is restarted, then explicit Retry may preload again.

## [2026-08-13] Live take forensics | BBCSO releases and soundbar gain

Take `live-1786650733984` dispatched 44 BBCSO notes over 16.5 seconds with no
renderer underruns or late events. The score-derived durations were finite and
mostly below two seconds, although two intentionally sustained voices lasted
5.35 and 5.86 seconds. The run started at a 50% orchestra fader, but live VST
telemetry did not yet record the actual plug-in-bound note-off or applied gain,
so the trace could not prove the renderer boundary after the fact.

A silent offline probe of the captured Violins 1 Long state verified that
BBCSO accepts note-off on channels 1–4 and decays to near silence in both one
call and the live renderer's block-by-block `reset=False` mode. The live VST
boundary now traces every note-on, note-off, panic, and applied master gain;
the parent retires released event ownership from that evidence. The soundbar
fader now uses a squared, approximately perceptual gain curve: 50% is 25%
signal amplitude (about -12 dB), rather than the previous 50% amplitude (-6
dB). This makes the middle of the performer control materially safer while
retaining silence at 0 and unity at 100.

## [2026-08-13] BBCSO native crash | Restore per-patch process isolation

Two automatic preload attempts at 12:35 (`renderer-preload-1786649725948463000`
and `renderer-preload-1786649746584607000`) reproduced the same boundary. The
LG HDMI device was present and selected. Each attempt loaded `violins` and
`low_strings`, announced `woodwinds`, then the child exited with `-11` before
opening audio. macOS reports `Python` PIDs 34952 and 34993 as
`EXC_BAD_ACCESS (SIGSEGV)` / possible pointer-authentication failures on BBCSO
`Work Thread 3`; the crashed stacks are inside BBC Symphony Orchestra 1.12.14,
with Pedalboard hosting on the main thread. The FastAPI parent PID 28884 stayed
alive. This is not CoreAudio absence, Python OOM, or a follower/runtime failure.

The live implementation had violated the already-established BBCSO safety
boundary: the known-good full-orchestra audition launches one isolated process
per patch because more than two short-timeout instances in one host are unsafe,
but the PWA live zone placed four instances in one child. The resident live
router now launches each configured binding sequentially in its own process and
CoreAudio stream, then fans score events only to their owning worker. Preload
progress remains one four-ensemble lifecycle. Startup trace rows now retain RSS;
native exits get a parent-authored error row with instrument, last stage, and
exit code. Failed automatic preload is circuit-broken across refreshes/tabs;
only explicit **Retry** sets `force: true` and starts another native host.

The first repaired hardware preload (`renderer-preload-1786650271920668000`)
reached **Ready** on `LG TV SSCR2` in about 17 seconds. All four isolated child
hosts remained alive (one each for `violins`, `low_strings`, `woodwinds`, and
`horns`), and no new macOS Python crash report was produced.

## [2026-08-13] BBCSO preload and missing-HDMI root cause

Forensics on failed run `live-1786635534405` joined the event journal and
runtime trace. All four memory-bounded BBCSO ensembles loaded successfully in
about 11 seconds. The child then failed while opening CoreAudio with
`ValueError('No such device: LG TV SSCR2')`; `uv run rubato audio-devices`
confirmed that the configured HDMI device was absent while Clavinova, MacBook
Air Speakers, Microsoft Teams Audio, and ZoomAudioDevice were visible. The
parent had overwritten that actionable exception with the generic
`VST worker room_center exited during startup` message.

Device availability is now validated before BBCSO instances load. Child
startup reports structured stage, exception type/detail, traceback, and a
durable `audio_worker` startup trace, while the parent retains the precise
failure or native exit code. A PWA-triggered resident renderer preloads at app
mount by default, publishes typed `runtime:renderer_status` progress, and is
leased by Go Live instead of being reconstructed for each performance. Its
trace lives under `runs/renderer-preload-*/trace/renderer.jsonl`. The Sound
drawer owns a compact **Keep BBCSO ready** setting and Retry action. Yamaha MIDI
readiness remains independent: a missing LG HDMI device never makes the Yamaha
orchestra route unavailable.

The performer-UI rule reinforced by this incident is progressive disclosure:
keep the stage surface limited to the next musical action and a compact honest
status; place device choice, volume, and lifecycle preferences together behind
the nearby **Sound** disclosure; expose detailed failure evidence only when it
is actionable.

## [2026-08-12] Rehearsal startup and sound-control recovery

Forensics on failed run `live-1786592666678` found zero solo MIDI events, zero
scheduler dispatches, and a `room_center` worker readiness timeout. The
machine-local four-instance configuration still used Pedalboard's 60-second
initialization grace even though prior measurements established 1 second as
the working BBCSO path; sequential loading therefore exceeded the 120-second
parent deadline. Live-zone defaults and the local configuration now use a
1-second grace plus one silent warm-up block.

The PWA now keeps **Sound** beside **Go live** in a fixed drawer, names the
BBCSO destination **LG soundbar · BBCSO**, exposes the orchestra master level
there, and explains that Yamaha Local Control belongs to the piano. The
unrelated 75-second Oguri diagnostic was removed from the rehearsal surface.
Failed or empty starts no longer become after-performance decisions, and BBCSO
loading/failure status is shown beside the live action. The renderer lifecycle
is now fully observable rather than inferred from a macOS Python dock icon:
the child process reports each ensemble load, loaded/total progress, LG
CoreAudio opening, actual ready, and failure. The compact score-adjacent status
shows **Not loaded -> Loading -> Ready/Failed**, elapsed startup time, and turns
the live action into **Cancel loading** until the already-primed audio stream is
truly ready.

Readiness is route-specific rather than a global orchestra label. Selecting
Yamaha now sends MIDI only, disables the VST mix, and reports **Ready**
immediately; selecting LG sends no doubled Yamaha accompaniment and alone shows
the BBCSO load/open/ready lifecycle. The output selection is durable across
reloads.

## [2026-08-12] Docs reconciliation | Live BBCSO routing and VST-only follow

Reconciled the canonical product, runtime, PWA, spatial-mix, source, and BBCSO
runbook pages with the implemented live path. Live FOLLOW can now fan symbolic
accompaniment to Yamaha plus configured Pedalboard zones, or omit the Yamaha
orchestral MIDI output when the PWA selects **None — orchestra via VST only**.
Yamaha input and Local Control still keep the acoustic solo piano independent.

Documented the machine-local four-instance Movement-II setup: Violins I/II,
low strings, woodwinds, and horns cover the nine sounding Oguri stems through
representative BBCSO states to reduce memory. Kept the acceptance boundary
explicit: zone `ready` validates configuration and file presence, while actual
HDMI latency, first-sound behavior, dense-tutti load, memory, crash recovery,
and rehearsal-length stability remain hardware verdicts.

## 2026-08-08 — BBCSO Oguri full-orchestra audition path

- Confirmed Oguri Movement II has nine sounding orchestral tracks: two violin
  sections, violas, cellos, basses, E horns, flutes, C clarinets, and bassoons.
- Confirmed BBCSO requires one loaded instrument patch per section; Spitfire's
  documented MIDI controls switch techniques within a loaded instrument, not
  the instrument browser itself.
- Read the local 23-page Discover manual and audited the plug-in's exposed VST
  parameters. The manual documents UI preset ordering and the C2-D4 untuned
  percussion map, but neither it nor the host interface supplies an instrument
  program selector. A global-default capture experiment produced a state that
  hung on reload, so reliable setup retains the editor-close handshake.
- Verified an exact first-violin BBCSO render is non-silent and macOS `afplay`
  reaches AirPods, while Pedalboard live `AudioStream` to the same device was
  silent.
- Added the reusable one-time state-capture and multistem audition procedure to
  [the BBCSO runbook](runbooks/bbcso-audio-zone.md).

## [2026-07-30] Yamaha follow-up | late cursor audit and expectation-gated silence

Analyzed the long performance ending near m.100 against three independent
coordinates: live follower trace, offline follower replay, and direct Yamaha
pitch-to-MusicXML alignment. The live endpoint and offline replay agree within
0.02 beat. In the late half the live estimate is slightly behind rather than
ahead, and exact-pitch support for the reported measure beats the same material
shifted by one or two measures in every tested block. That disproves a
one-to-two-measure error in the inferred global beat clock for this take.

The visual seam did contain a real bug. The cursor component claimed that live
position outranked the next rehearsal target, but its reactive expression used
the opposite order. The readout could advance while the PDF stayed on an idle
target page. Live/playback position now wins, PDF raster and SVG overlay switch
atomically, and a reusable Playwright cursor audit asserts measure, page,
system, normalized X, pixel containment, and screenshots at mm.50, 70, 79, 86,
94, and 100.

Real trace review also found false dropout HOLDs during notated sustain/rest
cells. Wall-clock expiry is now gated by score expectation: only an `ACTIVE`
piano cell can turn silence into dropout, while rests/sustains keep the score
and orchestra moving. Entry acquisition likewise keeps the orchestra scheduler
running instead of pausing at the first solo boundary. This restores the
orchestral pickups the soloist waits for around m.12 and m.30 without weakening true
mid-phrase dropout.

The post-performance boundary is now durable rather than process-local. A
scratch marker lives beside `solo.mid`, stale terminal runtime status cannot
re-arm the decision card, Yamaha playback is the primary audition action, and
Mac preview is explicit. Coverage now carries score-derived orchestral demand:
m.16 is green because the orchestra has no notes there, not yellow because no
piano take was collected. Long and short kept performances still combine as
one quality-weighted vote per covered canonical cell.

## [2026-07-30] Yamaha validation | one scratch lifecycle and score-owned sustains

Validated hardware performance `t20260730T153828Z-6a17`: 164.9 seconds and 885
piano onsets aligned unambiguously from m.12 beat 3 through m.52 at 79.3% note
match, producing 299 canonical timing cells. Its trace shows the authored
opening `LEAD -> FOLLOW`, the m.22 `LEAD` interlude and return to `FOLLOW`, and
the m.52 `LEAD` continuation. It is included in the 15-take Interpretation.
Because folding is cell-local, this long pass contributes one observation to
each covered half-quarter cell; it does not outweigh shorter overlapping takes
by virtue of length. Across its m.12–52 span the materialized cells now have
support 4–9.

The test also found two real seams. First, **Record pass** stopped through the
old take endpoint, so it bypassed the post-live scratch decision card and
silently became a kept take. Cued rehearsal, uncued rehearsal, and Perform live
now share `activePerformanceRecordingId -> pendingPerformanceRecordingId`.
Every path records proactively; stop exposes a sticky **Hear recording / Keep
as rehearsal take / Nothing for now** card, and only explicit Keep promotes
the MIDI plus cue/placement metadata into alignment, review, coverage, and the
Interpretation. Browser tests pin this decision before any take lifecycle
event.

Second, Oguri's m.34 orchestral notes overlap the following attacks; the gaps
heard on Yamaha were introduced by Rubato. A note-off had been frozen when its
note-on was delivered while later follower updates could move the next attack,
and one following chord was expired when successive notes of a piano chord
advanced the follower across its onset. Sounding releases now remain
score-owned and retime against the latest reference clock. Ordinary
within-chord crossings dispatch once at the current time; relocks and declared
skips still reject stale catch-up bursts. Deterministic scheduler/output tests
cover both behaviors, and the soloist's real-take expectation replay remains green.

## [2026-07-29] recorded passes now use the causal follower and announce the plan

Completed Steps 4–5 of
[Decision 0015](decisions/0015-follower-always-exists.md). **Record pass** with
**Orchestra cue** enabled no longer runs the fixed, deaf cue recorder. The
`/hardware/record-with-cue/start` compatibility endpoint resolves the selected
canonical measure, obtains the runtime plan, and calls
`LiveRuntimeManager.start_follow()`. Yamaha events now serve both as live
follower corrections and as the captured performance; `sections.json` and the
expectation model govern authority throughout the pass.

The fixed cue clock and take-level authority shape were removed:
`_accompaniment_mode_for_passage`, `handoff_at_entry`,
`LiveControl.start_record_with_oguri_cue`, and
`live_midi.record_midi_with_cue` are gone. New recorded passes start at the
profile's fitted `base_seconds_per_quarter`, not the UI audition tempo. Persisted
cue metadata is only an offline localization hint; v2 artifacts no longer carry
`tempo_scale` or `handoff`.

Before a selected-passage or first-entrance recording is enabled, the UI now
shows the runtime-owned plan: orchestra start/lead span, first FOLLOW
measure/beat, support local to that FOLLOW bar, and learned tempo. The cursor
prefers the live follower position over any open-loop hardware projection.
API, take-pipeline, runtime, Svelte, browser, and generated-contract tests pin
the new boundary; the real-take expectation replay remains the hardware-host
check for m.22 written silence versus a mid-phrase dropout.

Correctness hardening prevents a late plan response from replacing the current
selection, blocks every record entry point until its matching plan resolves,
and pins the explicit-start contract inside a FOLLOW section: the orchestra
cues from the selected measure until the performer joins, then the authored
FOLLOW region owns authority from that same score position. The shared
browser/API runtime fake models whole authored FOLLOW regions rather than only
their start beats.

Capture is now an invariant of the causal engine rather than a request flag.
Both **Perform live** and **Record pass** proactively write the Yamaha events
under the public runtime `run_id`, including partial evidence when the runtime
fails. The single input loop sends note onsets to the follower while preserving
note releases, velocity, pedal, and other channel controls in the scratch MIDI;
there is no second process competing to open the Yamaha port. A live
performance remains ephemeral scratch evidence until the
performer chooses **Keep as rehearsal take**; doing nothing leaves it out of
alignment, review, and the learned profile. This preserves the previous run for
listening and debugging without quietly treating every performance as approved
training data. The recorded-pass start response also returns `LiveControl`'s
authoritative managed-job status instead of constructing a second
timestamp/message beside it.

## [2026-07-29] convergence is the principle; cue starts where the performer points

Recorded as [Decision 0013](decisions/0013-rehearsal-converges-on-performance.md):
rehearsals get closer and closer to the live performance, like real musicians.
Cold start -- an orchestra that leads then drops out -- is the exception, correct
only when there is nothing yet to converge from. Everything in the rehearsal
subsystem now derives from that, and Decisions 0009/0010/0012 point at it.

Three changes landed with it:

- **The lead-in plays the rehearsed shape.** It ran at a flat nominal pulse,
  which is the wrong thing to warm up against when the takes already say how the
  passage goes. It now integrates the bar-smoothed rehearsal prior where support
  exists and falls back to nominal at cold start. (It ran flat because the raw
  reference warp lurches within a bar; the prior is smooth *and* the performer's
  own interpretation, so it was always the better answer.)
- **The cue starts on the selected measure**, not two before it. The performer
  points at a measure and enters whenever ready at any point after; the cue plays
  open-endedly, so warming up longer is just waiting. `CUE_LEAD_MEASURES` is
  gone. The old behaviour existed to land a fixed-length cue and was actively
  confusing -- selecting m.52 started the orchestra at m.50 and silenced it
  exactly where m.52's own interlude began.
- **Every live run is captured under its runtime ID** and can later be promoted
  into a take, the recording half of "a rehearsal is a recorded performance".

Also fixed a regression from the open-ended cue: the score cursor was driven by
the cue's source window, so once that covered the rest of the movement it swept
ahead at the reference's pace while the performer played at their own -- correct
for a few bars, then a measure or more ahead. Bounded to the orchestra-led
portion, which stops it lying without making it right; tracking the performer
during a take needs the follower in the record path, which is the remaining
convergence step (#146).


## [2026-07-29] rehearsal converges on the live performance; cold start is the exception

Correction from the soloist, and it inverts what was built. The unidirectional
"orchestra sets the pulse then drops out" behaviour was only ever the **cold-start
exception**. The rule is:

> Rehearsals get us closer and closer to the live performance, just like real
> musicians.

So a rehearsal pass with prior data should *be* the live performance -- same
follower, same tempo model, same accompaniment -- and simply also be recorded.
Only a passage with no evidence at all needs the degenerate "lead, then get out
of the way" behaviour, because there is nothing yet to converge from.

What had actually been implemented was half of that: the orchestra *plays* from
prior takes (`handoff_at_entry=False`), but nothing *tracks* the performer during
a rehearsal take, because the record path has no follower. Two consequences the soloist
hit directly:

- The score cursor could not follow him, though the system had takes of that
  passage. He reasonably expected it to use what it had learned.
- A regression from the open-ended cue: the cursor was driven by the orchestra
  cue's source window, so once that window covered the rest of the movement it
  swept ahead at the reference's pace while he played at his own -- correct for a
  few bars, then a measure or more ahead. Bounded to the lead-in for now, which
  stops the cursor lying without making it right.

**Direction**: converge the two paths. `start_follow` keeps the performed notes
it already receives, writing them via `_write_captured_performance` under its
public runtime ID. The user later decides whether that scratch evidence becomes
a durable take. That is the recording half of "a rehearsal is a recorded
performance".

Design note: this makes take provenance matter more, not less (#143). A take
recorded while the orchestra followed captures the performer responding to the
model, so it must stay distinguishable from clean solo evidence even though both
are now valid rehearsal artifacts.


## [2026-07-29] an aborted take is kept, not discarded

the soloist recorded a 58-second passage with an orchestra cue-in, then pressed the
prominent red **Silence** control when he finished. The recording was captured
to disk correctly -- 220 notes, chords simultaneous (min IOI 0.00 ms) -- but it
was never registered, never aligned, and never reached the model, with no error
shown. `takes.jsonl` simply had no entry.

Cause: `stop`/`panic` only called `_discard_pending_cue_for_current_job`, which
dropped the pending cue and left the finished MIDI orphaned in the session
directory. Only `POST /takes/{id}/stop` finalized a take, and nothing routed the
abort paths there. The intent was documented in two places ("the stage must never
end a take by any means that bypasses the take store") but nothing enforced it.

This was also a UI trap, not performer error. `Stop Take (space)` lives inside
the Take Capture panel and appears only while recording; `Silence` is large, red,
permanently in the header, and looks like the stop control. Nothing indicated
that one keeps the performance and the other throws it away.

Aborting now **keeps** the performance: `_keep_recording_from_aborted_job` runs
after the job stops and, if a recording with real notes exists and no take was
already registered, finalizes it through the same path as a normal stop (save,
`take:recording_stopped`, alignment). A lead-in cancelled before any note still
just releases the cue, so nothing empty is stored.

Recovery note: the lost take was reinstated manually and aligned **strong**
(82% note match) at m.53 beat 3 -- exactly where the piano enters after the m.52
interlude, which independently confirms the open-ended cue and the entry both
behaved correctly. But needing an agent with a REPL to rescue a performance is
itself the defect; the software owns this now.


## [2026-07-29] transport is an explicit total state machine

Answers the adversarial review's architectural finding. Transport authority was
an *implicit* machine: `process_update` and `tick` each ran their own chain of
``if authority is ...`` branches, and any state a chain forgot simply fell
through. Both real bugs in that review were exactly that -- missing cells,
invisible because no single place described the machine (`FOLLOW` had no tick
branch, so a pianist who stopped playing never triggered the coast contract).

`aimusic.accompaniment.transport` now owns the shape of the machine:

- **Every (authority, event) cell must be declared** -- 8 states x 2 events.
  `validate_total` refuses a partial table and reports *all* missing cells at
  once, so forgetting one fails at construction rather than silently at a
  fermata.
- **`Inert(reason=...)` records why a cell does nothing.** Distinguishing "nothing
  should happen here, because X" from "nobody wrote this branch" is the whole
  point; silent fall-through destroyed exactly that information. Two cells are
  inert today: `HOLD_AWAIT_ENTRY x tick` (nothing may be scheduled until a
  confident position arrives) and `STOP x update` (the authored stop is
  terminal).
- **`describe()`** renders the machine as a table -- the documentation the code
  never had.

`tick` and `process_update` are now thin dispatchers over that table, with the
former branch bodies extracted into named handlers (`_tick_lead`,
`_tick_orchestra_entry`, `_tick_follow`, `_tick_coasting`, `_tick_dispatch_only`;
`_update_during_lead`, `_update_during_orchestra_entry`, `_update_default`).
Behaviour is unchanged: 515 unit tests, 16 browser e2e, and a replay of a real
take all pass, and the replay scorecard shows zero unplanned panics.

Two things this immediately caught, which unit tests had missed:

- `CanonicalFollower.poll_update` called a `_to_canonical` helper that did not
  exist -- the reference-to-canonical conversion was inlined in `observe`. Found
  by the replay harness, not the tests, because the tests use fakes that skip
  `CanonicalFollower`. The conversion is now shared, so a position drained
  without a note cannot skip it.
- The scorecard counted `section_hold` as an unplanned panic. Reaching an
  authored hold silences sustained notes by design, and now legitimately fires at
  the end of a replay via the new FOLLOW silence timeout.

Note the engine file did not shrink much (1599 -> 1675 lines). That is expected
and not the goal: the win is that the machine is now *declared* rather than
emergent. Further extraction of the handlers into per-state objects is possible,
but the bug class the review identified is closed by totality alone.


## [2026-07-29] adversarial review of PR #141: three real findings, three false positives

An external adversarial design review raised 7 findings. Each was verified
against the code rather than accepted or dismissed; 3 were real and are fixed,
3 were incorrect, 1 is minor and deferred.

**Real (fixed):**

- *Positions trapped in IPC during silence.* `ProcessFollower.observe` hands a
  note off without waiting, so that note's answer arrives afterwards -- and
  nothing drained it except the *next* note. The last position before any silence
  (phrase ending, fermata, final note of an entry) was stranded. Added
  `poll_update()` and a drain in `LiveEngine.tick()`, and split the update
  handling out of `process_note` into `process_update` so a late-arriving
  position is applied identically no matter what produced it.
- *`FOLLOW` never coasted on true silence.* `tick()` had no `FOLLOW` branch, so
  the state was left only when a low-confidence note arrived. If the pianist
  simply stopped, `follower_coast_ms` was never enforced. `tick()` now falls into
  `COAST` on elapsed wall-clock time.
- *Scorecard produced both false passes and false failures.* Follower latency was
  gated on the **median**, which hides exactly the HMM's tail-latency failure
  shape (now p95); and `input.min_interval_ms` demanded a sub-5 ms pair even for
  monophonic streams, marking valid runs as failures (now only applies when the
  stream actually contains near-simultaneous notes).

**False positives (verified, no change):**

- *"Shared-memory gauges never write across processes."* The claim was that
  `Value(type, lock=False)` returns a wrapper whose attribute writes go to a
  local `__dict__`. It returns the ctypes Structure itself, allocated in shared
  memory. Verified with a spawned child: the parent reads back the child's
  `iterations`, `events`, and queue high-watermark exactly.
- *"`CanonicalFollower` leaks a canonical beat into Matchmaker's prior."*
  `MatchmakerStreamFollower.reposition_for_entry` uses `reference_beat` whenever
  it is provided, and the engine always provides it (computed through the warp).
  The `score_beat` fallback exists only for legacy identity bundles where the two
  coordinates coincide.
- *"Negative arrival deltas on backward score beats."* `timing_for` returns early
  when `event.beat <= tempo_state.score_beat`, so the negative-interval path is
  unreachable. The cited method (`arrival_time_delta`) does not exist.

**Deferred (minor):** `gc.unfreeze()` promotes the frozen set into gen-2, so
repeated runs in one server process could lengthen later collections. Gen-2 is
still collected, so this is not the claimed monotonic leak; revisit if a long
session shows collection times growing.

**Accepted for the roadmap:** the review's architectural point stands --
`LiveEngine` is a god object with an implicit state machine spread across
`process_note`, `tick`, and helpers, which is exactly how the two real bugs above
hid. An explicit transport state machine is the right next structural move.


## [2026-07-28] rehearsal cue behavior now follows the local prior

Implements the convergence model. `_accompaniment_mode_for_passage` reads the
Interpretation's per-cell `support` at the selected passage and chooses:

- **support 0 (cold start)** -> `led_then_dropped`: the orchestra sets pulse and
  mood, then trails off at the performer's first note, so the take is unbiased
  solo evidence. This is the only case where dropping out is correct.
- **support >= 1 (warmed up)** -> `accompanied`: the orchestra keeps playing its
  best attempt for the whole pass, so the performer rehearses against it and each
  take closes the gap toward the live performance. Both sides adapt.

Missing profile, unparsable profile, or an uncovered passage all degrade to cold
start, so the unbiased-evidence case is the safe default.

Deliberately **not** implemented here: closed-loop *following* during a rehearsal
take (running the live FOLLOW engine while recording). Today the warmed-up
orchestra plays its learned rendering rather than tracking the pianist in real
time. That is a substantially larger change and is tracked as its own issue.

Still outstanding from this design (tracked as issues): tagging each take with
the accompaniment mode it was captured under, so the fit can weight or separate
takes recorded with an accompanying orchestra from clean solo evidence.


## [2026-07-28] rehearsal is mutual convergence, not data harvesting

Refining the previous entry (the soloist): treating rehearsal as purely unidirectional
was too reductive. Once takes exist for a passage, the orchestra should not drop
off -- it should *attempt the live performance* with everything learned so far,
and learn again from the attempt. Each take is a joint practice pass in which the
performer and the follower adapt to each other and converge, rather than a
one-way data harvest.

So the rehearsal cue's behavior should be a **function of local prior strength**,
a signal we already have: the Interpretation's per-cell `support` (and the
coverage surface already showing "0 of 3 observations" vs "strong alignment").

- Thin or no prior at this passage: orchestra sets pulse and mood, then trails
  off at the performer's entry. Clean, unbiased solo evidence. (Implemented.)
- Strong prior: the orchestra follows -- rehearsing the real thing -- and the
  take still feeds learning.

The unidirectional/bidirectional split from the previous entry therefore is not
live-vs-rehearsal; it is **cold-start vs warmed-up**. Live remains its own
contract (never drop the ball); rehearsal spans the range as coverage grows.

**Data-integrity caveat to carry into the design.** When the orchestra follows,
the take records the performer responding to a model that is responding to them.
Learning from it can reinforce the model's own influence rather than the
performer's intent -- training on your own outputs. It is not disqualifying (it
is the condition of real performance), but it means takes stop being one
homogeneous kind of evidence. Therefore: **tag every take with the accompaniment
mode it was captured under** (solo / led-then-dropped / followed) so the fit can
weight or separate them, and so "is this passage learned from the soloist, or from the soloist
plus the model?" stays answerable. Belongs with Decision 0009 (rehearsal as
dataset lifecycle) when implemented.


## [2026-07-28] rehearsal cue-in: the performer ends the lead-in, not a beat

the soloist selected m.52 to record a pass and the orchestra stopped dead there. m.52 is
an *orchestral interlude* -- the piano does not enter until m.53 -- so the cue
fell silent exactly where its own interlude began, and the cursor parking there
read as a hang. Cause: `handoff_at_entry=True` (hardcoded in the route) set
`orchestra_playback_seconds = lead_in_seconds`, ending the orchestra at a
*predicted* entry beat.

The rehearsal cue now plays **open-endedly** from the cue point, and the *first
played note* starts a short trail-off (`handoff_tail_seconds`), after which the
orchestra falls silent while the take continues. A longer interlude is simply a
matter of waiting; the performer decides when to enter.

**Design note (the soloist): live and rehearsal cue-ins are NOT the same mechanism, and
should not be consolidated.** They look alike and were briefly mistaken for
duplication, but the intents differ:

- *Live* is about keeping the show going. It is bidirectional: authority has to
  be explicitly held and explicitly transferred, and the ball must never be
  dropped. Hence acquisition, clamping, coasting, panic guards, trace contracts.
- *Rehearsal* is about getting the performer in the mood -- hearing the orchestra
  and the tempo until ready to jump in and supply data. It is essentially
  unidirectional: the orchestra never follows the pianist, it just sets the pulse
  and then gets out of the way.

So the rehearsal path deliberately stays simple: no follower, no tempo authority,
no handoff contract -- play, detect the first note, trail off. Merging it into the
live `ORCHESTRA_ENTRY` state machine would be premature abstraction over two
requirements that only superficially match.

Open: the trail-off currently ends with an all-notes-off rather than a musical
fade, and leading silence in the take is not yet trimmed at capture time.


## [2026-07-28] the replay panics were a harness bug; runtime now meets contract

Chasing the scheduler panics on replay found the defect in the **harness**, not
the runtime. `notes_from_trace` rebased the note stream to the *first note*,
which deletes the orchestra lead-in: the pianist entered at t=0 at score beat
~152 while the orchestra was still near 140, so the follower could never
localize (raw position oscillated 141-142.7 for seconds, clamped) and the
scheduler thrashed with skip/repeat resets. Replays now originate at
`runtime_start`, preserving the silence the performer actually waited through.

Also corrected the scorecard: stopping a run panics by design (all-notes-off), so
`user_stop`/`output_close` were being counted as failures. The check is now
`output.unplanned_panics`.

With both fixed, replaying the real take scores **8 of 9 checks passing and zero
unplanned panics**: process_note p95 14.1 ms, processing work p95 2.7 ms and
interval max 46 ms, output lateness p95 8.7 ms, follower latency 0.5 ms, struck
chords simultaneous at 0.00 ms. The single remaining miss is
`midi_input.interval_ms_max` 42.7 ms, which is **one window out of 39** (all
others <= 19 ms) -- an isolated OS scheduling hiccup, not systematic.

Method lesson: a self-test harness is itself code under test. Both defects here
were in the measurement path and would have sent us chasing phantom runtime bugs.
Re-entry trigger for the input stall: if a live trace shows repeated
`midi_input` gaps, take arrival timestamps from rtmidi's own callback rather than
the poll loop.


## [2026-07-28] GC pauses were stalling the real-time loops

With the follower isolated and the arrival curve precomputed, the remaining
stalls had a distinctive signature: the input thread missed its cadence by up to
49 ms while doing **0.00 ms of its own work** — it simply was not scheduled. With
no GIL hog left in the conductor process, that points at the garbage collector,
not contention.

`realtime_gc` wraps a live run: collect once, `gc.freeze()` the setup graph
(score bundle, warp tables, prefix curves) into a permanent generation the
collector stops re-walking, and raise thresholds so routine gen-0 churn does not
escalate mid-performance. Restored on exit. Measured A/B on the same replayed
take:

| metric | before | after |
| --- | --- | --- |
| `midi_input` interval max | 49.4 ms | **21.7 ms** |
| `processing` work p95 | 12.4 ms | **2.1 ms** (pass) |
| `processing` interval max | 98.1 ms | **64.4 ms** |
| `process_note` work p95 | 40.1 ms | **25.1 ms** |
| output lateness p95 | 18.8 ms | **5.6 ms** |

Scorecard caveat: for *event-driven* stages (`process_note`, `publish`) a large
inter-iteration interval is just a gap between performed notes, not a stall. Only
the continuously-running loops (`midi_input`, `processing`) get an interval
check; do not read the raw interval columns for event-driven loops as stalls.

Still open: scheduler panics on replay (3), and the last few ms on
`midi_input`/`process_note`. Re-entry: profile the replay path via the harness
rather than tuning blind.


## [2026-07-28] arrival curve as prefix sums; UI publish off the hot path

Fixed the two hot spots the harness localized.

**1. `InterpretationArrivalCurve` — O(grid) per event per note -> O(1).** Each
integration cell contributes `A + B * reference_period`, where `A` (gain x beat
span x fitted period) and `B` (the reference-scaled remainder) depend only on
score geometry and the static fitted profile. The interior cells of any query are
always the same canonical grid/knot cells, so `A` and `B` are prefix-summed once
at construction; a query is then two lookups and a subtraction, plus the two
partial end cells computed live (they carry the live reference beat and the
event's own reference identity). Verified against the original loop on 399
randomized queries: **max difference 6.0e-14 s** — numerically identical, not an
approximation — and **79 ms -> 0.06 ms per scheduler update (~1300x)**.
The prefix tables are built in `__init__` (run setup), not lazily: as a
first-note cost the ~600 ms build showed up as a single huge `process_note`
outlier.

**2. UI publish moved off the conductor loop.** `StatusBroadcaster` holds a
single conflating slot drained by a daemon thread, so the conductor's publish is
one store. Browser updates only need to be *recent*, and a newer status replaces
an unsent one, so a slow or stalled client can never queue work or back-pressure
the runtime. Inline publishes had been spiking 10-34 ms against a ~5 ms tick.

Measured across the scenario suite and a replay of a real take:

| metric | before | after |
| --- | --- | --- |
| `processing` work p95 | 709 ms | **2.2 ms** (synthetic) / 12.4 ms (replay) |
| `process_note` steady state | 65 ms median | **0.6-5 ms** |
| output lateness p95 | 49 ms | **8-19 ms** |
| chord simultaneity | none | **0.00 ms** (chords stay together) |

Harness caveat worth remembering: the synthetic generators emit *rhythmically*
realistic but harmonically meaningless pitches, so Matchmaker thrashes and the
scheduler panics. Use them for cadence/compute/backpressure metrics; use
`--trace <take>` replay for anything follower- or musically-dependent.

Remaining above contract (next): `process_note` p95 40 ms and `processing` 12 ms
on replay, `midi_input.interval_ms_max` ~30-50 ms, and scheduler panics on
replay. Re-entry: profile the replay path the same way before tuning.

## [2026-07-28] hardware-free load harness finds the real hot spots

Built the self-test harness from Decision 0011 so the runtime can be profiled and
regression-tested with no Yamaha attached: `VirtualInputPort` (satisfies mido's
`iter_pending`, releases notes at their real wall-clock times), deterministic
fixture generators (`sparse`/`typical`/`chords`/`storm`/`dropouts`/`ramp`),
replay of a recorded take's input rows, and a scorecard that grades a finished
trace against the Decision 0011 contracts. One command:
`uv run python -m aimusic.realtime.loadtest --scenario storm`.

It immediately reproduced the live pathology with no hardware — `process_note`
141 ms, `midi_input` stalled 94 ms (we measured 80 ms live), processing loop
892 ms — and then found three things guessing had missed:

1. **Isolating the follower in a process is not enough on its own — done naively
   it is *worse*.** `ProcessFollower` with a synchronous `observe()` measured
   224 ms/note: the caller still paid the HMM compute *and* the IPC. The fix is
   the design's stated contract: `observe()` hands the note off and returns the
   freshest position already available (conflated latest-value), so a slow
   follower costs freshness, not the conductor's deadline. Follower latency then
   drops to ~2 ms. `RUBATO_FOLLOWER_PROCESS=0` restores in-process for A/B.
2. **`score_projection()` re-read the canonical timeline and the several-hundred
   anchor beat map from disk on every call**, and the status publish path calls it
   on every publish — ~56 times per performed note, i.e. file I/O + JSON parsing +
   model validation on the real-time loop. Now cached on the inputs' mtimes, so an
   edited beat map or human correction is still picked up. Publish work fell to
   ~1.3 ms median.
3. **`InterpretationArrivalCurve` is the dominant remaining cost.** A/B with
   `RUBATO_FOLLOW_CLOCK=reactive`: `process_note` **265 ms -> 18 ms**, processing
   loop **709 ms -> 1.7 ms**, input stalls 130 ms -> 42 ms. It re-integrates a
   half-beat grid (plus warp knots) for every planned event on every note. Fixing
   it — memoize per (event, tempo-state) or precompute the integral as a cumulative
   curve — is the next task; until then the curve is the reason the loops miss
   their deadlines.

Method note: cProfile on the main thread showed only `time.sleep`, because the
runtime works on the hardware-control worker thread. The `loop_timing` +
scorecard path is what localized these; keep using it rather than a naive
profiler.

## [2026-07-28] serialized-chord input bug and a dedicated MIDI-input thread

Playing the soloist's *recorded input* back for the first time (offline-alignment beat
check) revealed his chords were rolled into ~30 ms arpeggios — notes he struck
together were stored 30-50 ms apart. Root cause in the live FOLLOW loop
(`live_runtime.py`): `now = clock.now()` was read **inside** the per-note loop,
*after* each note's `process_note` + `tick` + two WebSocket publishes. A chord
arrives in one `iter_pending()` drain as simultaneous notes, but each was stamped
only once the previous note finished processing, so they serialized by the
per-note processing time. Every take on record shows the signature: **zero
sub-5 ms intervals, a 12-54 ms floor**. It is structural, not a recent
regression, but the floor grew from ~18 ms to 30-54 ms on the last week's takes
as more per-note work (per-note `tick`, the acquisition, the rehearsal-anchored
model, extra publishes) piled on — which is when it became audible. This false
rhythm fed the follower and is a likely contributor to the jitter/tempo-hunting
chased all week; live, the soloist hears his real piano and never noticed.

Fix, two parts:
1. **Stamp once per poll batch, publish once per batch** — a chord's notes share
   a `perf_time` and the drain stays fast.
2. **Dedicated input-reader thread** (`_read_input_into_queue`) — stamps note
   arrivals into a `deque` at ~0.5 ms independent of the follower/scheduler/
   publish, so processing load can never again delay input timestamps. The
   processing loop only drains what the reader already timestamped; reactive
   output still fires inside `process_note` per note.

Real-time concurrency note (per the soloist): the GIL was **not** the direct cause here
(timestamp-after-processing was), and plain threads are the right tool for this
workload because the GIL is released during MIDI/socket I/O — the input reader,
the Matchmaker follower thread, and the deadline-output thread all run
effectively concurrently. Reach for multiprocessing / free-threaded CPython
(PEP 703) / subinterpreters (PEP 734) / a native (Rust/PyO3) core only if
profiling shows CPU starvation or we add audio/DSP; today the per-note CPU budget
is ~0.08 ms. Re-entry trigger: a trace shows the input reader starved (input
timestamp gaps track processing spikes) despite the dedicated thread.

Deterministic test: a chord drained in one batch shares one timestamp. Validate
live by replaying a fresh take through the offline harness and confirming sub-5 ms
IOIs reappear for struck chords instead of the ~30 ms rolls.

## [2026-07-27] rehearsal-anchored FOLLOW pace | tempo stops hunting on tracker jitter

Third live test (`live-1785205484713`, cue at beat 140) confirmed the earlier
fixes — steady lead-in, clamp at 47.4 BPM (the soloist's real ~50) — but the orchestra
still drifted +-0.5 s and the soloist's marked anchors did nothing. Root-causing both:

- **The tempo hunted 43-95 BPM while the soloist played steadily ~50.** The FOLLOW pace
  was derived from the live follower's position, and Matchmaker's position is a
  jumpy step function (plateau, then leap through a cluster of filigree notes), so
  the runtime turned tracker jitter into tempo fluctuation. A windowed regression
  does not fix it (validated at 1.5-5.0 beat windows: still 24-94 BPM) — the
  signal itself is too jumpy. A rehearsal-curve fit explains the soloist's live timing no
  better than a constant tempo (both ~400 ms residual), i.e. the +-400 ms scatter
  is follower jitter, not real rubato: the soloist *did* play steadily.
- **The anchors never fired.** They armed correctly and the soloist played the trigger
  basses in-window (offline replay fires all four), but the predictive scheduler
  had already committed each anchor's chord ~0.5 s before his bass landed
  (orchestra running ahead), so `dispatch_reactively` declined every time. And
  even firing, anchors only play the chord — they never re-sync ongoing transport.
  (Anchor re-sync deferred to a follow-up; see below.)

Fix (this entry): **the FOLLOW pace is now anchored to the rehearsal profile.**
The profile is the agreed performance plan — for this cue passage it has 3 takes,
<8% dispersion. `RehearsalAnchoredTempoModel` wraps the reactive/LTE model and,
where the takes support a cell, sets the canonical pace to the **bar-smoothed**
rehearsal tempo times **one slowly-adapting scale** (today's overall pace vs
rehearsal), updated only from accepted beat-level candidates so jitter integrates
away. Sub-beat micro-rubato is smoothed out (a live follower cannot resolve it and
playing it as a pulse fluctuates 33-143); the phrase-level arc the takes agree on
is kept. Where the profile has no support the pace degrades to reactive. Missing
data (coast/dropout) holds the plan rather than chasing noise — the graceful
degradation the soloist asked for. Reference and canonical periods are rescaled together
to stay consistent.

Replaying this trace through the new model: output tempo **50.7 BPM, std 4.3
(range 33-56, the 33 being the real ritardando at beat 174)** vs the old 60.6 BPM,
std 12.1, range 44-96. The pace is a stable, musical pulse instead of hunting.

Deferred with explicit re-entry triggers: (1) anchor re-sync + pre-emption fix —
make marked beats hard-correct position/phase on a tight, follower-agreed window
(no big jumps from a lone bass note); re-entry: next trace still shows drift into
an anchored bar. (2) Lead-in on the profile prior instead of steady-nominal, so
the pre-entry orchestra plays the agreed tempo too. (3) The intra-measure
reference-warp noise (prior entry) is now bypassed for pace where the profile is
trusted, but still shapes untrusted passages.

## [2026-07-27] steady lead-in, wider entry window, and the reference-warp finding | Yamaha trace repair

the soloist retested the acquisition clamp (below) and the orchestra was still off. Trace
`live-1785181271384` (same cue at canonical beat 144) showed two compounding
problems and surfaced a third, deeper one.

1. **The clamp window was too short.** The acquisition ran cleanly (5 onsets ->
   handoff) but clamped to **89.6 BPM** while the soloist again played a sustained
   **51 BPM** (global raw-follower regression, 17.6 beats / 19.7 s). Right after
   Matchmaker recenters it does a fast catch-up burst, and its raw canonical beat
   then plateaus for a stretch, so a 0.73 s / 5-onset window read the catch-up
   slope. Both least-squares and Theil-Sen converge on ~52 BPM once **elapsed
   time** reaches ~1.3 s (~10-11 onsets); span-in-beats is an unreliable gate
   because it plateaus. Widened the window to min 6 onsets / 0.5 beats / **1.3 s**
   (config `entry_tempo_*`). After the 89 clamp the reactive EMA took ~10 s to
   crawl down to 50 and then overshot to 39 and oscillated 48-73 -- the clamp was
   the dominant "super off" driver.

2. **The lead-in lurched fast.** the soloist reported the lead-in "went super fast" and
   he had to rush to enter on time. The lead-in drove a constant reference period
   through the reference warp, whose intra-measure slope swings wildly, so its
   canonical tempo lurched 70 -> 113 -> 118 beat-to-beat. Fixed: the pre-entry
   lead-in now runs on the **canonical clock at the performer-set nominal tempo**
   with reference coordinates dropped, so the scheduler spaces lead-in events
   uniformly (the arrival curve already falls back to canonical spacing when a
   state carries no reference period). Matchmaker's prior, which is a reference
   beat, is recentered from the warp separately.

3. **The reference warp is noisy at the intra-measure level (alignment finding).**
   `performance_beat_map.machine.json` has clean, performer-verified downbeats
   (around the cue, measure-to-measure reads 51 -> 48 -> 52 BPM), but the
   intra-measure beat anchors imply a per-beat tempo alternating ~38 <-> 64 BPM.
   Those anchors come from `build_measure_beat_ticks` DP-matching the Audiveris
   reduction against the Oguri solo MIDI, and they have **strong support**
   (17-23 matched notes per bar) -- so this is most likely Oguri's real, if
   exaggerated, rubato rather than random error. The open question is therefore
   whether the runtime should *impose* that intra-measure rubato during FOLLOW,
   not whether the alignment is "broken." Deferred pending the soloist's retest of fixes
   1-2 and his musical review; re-entry trigger: a post-fix trace still shows the
   orchestra lurching within bars while the follower tracks him cleanly.

## [2026-07-26] cue-in tempo/phase acquisition before FOLLOW authority | Yamaha trace repair

the soloist tested the moving cue-in match (below) and the orchestra still did not
synchronize. Trace `live-1785118178612` showed the position handoff now works —
two stable matches near the moving orchestra transferred cleanly, no rewind, no
repeat — but the *timing* handoff was broken. The lead-in follows the
reference-performance warp at a constant reference period, so its instantaneous
canonical tempo had climbed from the performer-set 70 BPM at beat 144 to
~88 BPM by the entry at beat ~152 (the warp compresses ~2.16× there). FOLLOW was
seeded from that 88 while the soloist actually played ~51 BPM (raw-follower regression:
15.36 beats / 17.8 s). Worse, the EMA then *smoothed* the pianist's real
candidates into the 88 seed: the first good candidate of 47.7 BPM was dragged to
72.8 because `0.25·(60/47.7) + 0.75·(60/88)`. Authority transferred at the
second match, where the pianist had advanced 0.016 beats — literally zero tempo
information. The orchestra raced ahead (250–340 ms early clusters around beats
156/161/164) and hunted.

Root cause: the runtime treated *position acquired* as sufficient for full
timing authority. Two matches localize the phrase; they do not establish pace.

`ORCHESTRA_ENTRY` now separates position acquisition from tempo/phase
acquisition. The first confident match still certifies *where* the pianist
entered (unchanged proximity gate). Instead of handing off, the orchestra then
**freezes at the entry beat** and collects piano onsets until they span a
musical interval (defaults: ≥3 onsets, ≥0.75 canonical beats, ≥0.4 s — trace-fit
starting points, not searched). A robust Theil–Sen line is then fit to the
onsets *alone* and FOLLOW is **hard-clamped** onto that pace/phase via
`seed_timing`, discarding the pre-entry orchestra seed rather than smoothing
across the evidence discontinuity. Phase is read from the fit at handoff time
and floored at the frozen beat, so transport steps forward once (orchestra joins
the soloist at the soloist's position and pace) and never backward. New trace
actions: `orchestra_entry_acquiring` and the `orchestra_entry_piano_seed` tempo
decision (the clamp), distinct from the old `orchestra_entry_handoff_seed`.

The clamp adopts whatever the pianist plays, so its correctness does not depend
on the exact ~51 BPM figure; that value is trace-derived, not independently
proven to be the soloist's true tempo. Deferred with explicit re-entry triggers: the
lead-in still warp-accelerates (only what the soloist hears while cueing, no longer a
sync input — revisit if the accelerating pulse makes the entry hard to time);
ongoing FOLLOW still uses the pairwise EMA once clamped (revisit if a new trace
shows post-clamp oscillation now that it starts from truth); coast/relock
backward micro-snaps outside the entry (revisit if a trace shows them retiming
mutable events).

## [2026-07-26] moving cue-in match and continuous FOLLOW handoff | Yamaha trace repair

the soloist's next Yamaha run, `live-1785115909614`, exposed the second half of the
orchestra-led entry contract. The orchestra correctly advanced from canonical
beat 148 to 152.024 while he listened, but Matchmaker's untouched prior remained
at the original cue beat. Two locally stable estimates near that stale prior
transferred authority to FOLLOW, which the scheduler correctly interpreted as
a four-beat repeat. It replayed 13 accompaniment event IDs. The tempo model also
included the 3.45-second listening interval in its first observation and slowed
toward 45 BPM. Output delivery before handoff remained within 20.2 ms, ruling
out MIDI hardware latency as the cause.

`ORCHESTRA_ENTRY` now starts score matching only when the first piano note
arrives. At that seam, the engine carries the orchestra's current canonical and
reference beats explicitly into Matchmaker and recenters the PTHMM prior once.
A confident estimate must also fall in the narrow window around the moving
orchestra position; a stable estimate at the old cue point remains diagnostic
and cannot own transport. A successful match seeds FOLLOW timing from the
orchestra's current monotonic position and pace, so the listening interval is
excluded and the scheduler cannot rewind at the authority transfer. The
runtime trace distinguishes `orchestra_entry_position_mismatch` from
`orchestra_entry_handoff`.

## [2026-07-26] orchestra-led score entry and locator anchors | Yamaha trace repair

the soloist's Yamaha test exposed a contract failure, not a browser freeze. Runtime
trace `live-1785113806755` started m.38 at canonical beat 148 / reference beat
380.847, emitted four count-off clicks and the first orchestral notes, then
entered `count_off_entry_grace_expired` HOLD around canonical beat 150 because
no piano evidence had arrived. the soloist was intentionally listening for the
orchestra's pulse before joining. The generic 1.5-second dropout grace was
therefore enforcing the opposite human contract.

Selected-measure live starts now begin the orchestra immediately without a
metronome under explicit `ORCHESTRA_ENTRY` authority. It advances independently
until confident piano evidence transfers ownership to FOLLOW, while authored
LEAD/HOLD/STOP and Silence remain hard boundaries. This is not a longer grace
timer: no piano input is itself valid during an orchestra-led pickup. The JSONL
`runtime_start` records `start_kind="orchestra_lead_in"` and the existing
canonical/reference seam; the live path emits no `count_off` rows. Re-enter a
bounded handoff only if a trace demonstrates unsafe overrun, and derive that
bound from an authored or symbolic cue endpoint.

The score context menu now puts **Start live with orchestra here** beside the
chosen measure, eliminating the scroll to a remote transport. Reactive anchors
are compact blue `A` locator tabs with 36 px drag/click targets instead of
full-height amber dashed rules and a non-uniformly scaled SVG circle. The old
circle rendered as a giant oval because normalized SVG x/y scaling was
intentionally non-uniform. Blue avoids the semantic collision with amber
passage coverage; drag, click/right-click delete, clear-all, and atomic Undo
remain score-local.

## [2026-07-26] score-local anchor editing | Direct manipulation and atomic undo

Replaced the performer overlay's remote anchor-mode toggle with editing at the
notation itself. Right-clicking an exact onset opens a menu beside the pointer;
amber lines have wide hit targets and drag horizontally; a selected line can be
deleted directly; and an accidental multi-anchor cluster can be cleared from
its measure in one action. Every add, move, delete, and clear presents immediate
local Undo. This makes pointer and scroll distance an explicit usability
constraint: an edit on the score must not require leaving the score viewport.

The backend now owns atomic move, clear-by-measure, and multi-anchor restore
transactions. This avoids implementing correction or undo as client-side
add/delete loops that can expose duplicates, lose a concurrent edit, or stop
halfway. The generated OpenAPI client carries those contracts into Svelte.
Browser regression coverage reproduces the original failure path—accidental
cluster, clear, undo—and the popup is visually checked inside the clipped,
sticky desktop score viewport.

## [2026-07-26] adversarial review: honest entry and evaluation contracts

Triaged both rounds of Antigravity's cross-roadmap review of PRs #125 and
#133-#138. The legitimate measure-entry findings are fixed at their ownership
seams. Count-off MIDI now goes through `MidoAccompanimentOutput` on an explicit
configurable cue channel (Yamaha/GM percussion channel 10 by default), inherits
CC7/master-volume policy, serializes port writes, records actual cue note-on/off
in runtime JSONL, and releases an active click even if trace or timing fails.
It never silently borrows the first orchestral part. A selected FOLLOW entry
seeds the tempo model through an explicit non-observational `TimingSeed` and
uses a distinct confidence-zero `CUE_ENTRY` authority from the shared count-off
downbeat. Only matched piano evidence grants real FOLLOW authority; a missed
entry expires into bounded HOLD. The tempo model's internal seeded anchor is
now an evidence-free timing value rather than a fabricated `FollowerUpdate`.
STOP entry is rejected before MIDI ports or count-off side effects.

The deterministic evaluator's report is now schema 3. It records its
`deterministic_virtual_clock` execution domain, `hardware_delivery_measured =
false`, and follower evidence derived from the follower instance that actually
executed (`synthetic_oracle`, `deterministic_test_follower`, or `unspecified`).
There is deliberately no `recorded_matchmaker_trace` label until a typed loader
can prove that provenance rather than accepting a caller assertion.
This preserves the harness as a precise downstream A/B instrument without
allowing an oracle/downstream result to masquerade as follower or hardware
validation. Decision 0010's anchor-coupling result is correspondingly stated as
synthetic isolation evidence; its Yamaha-trace re-entry trigger remains active.

The coordinate and anchor double-dispatch allegations were not reproduced:
`follower_prior_reference_beat` explicitly requires and returns the mapped
reference beat, failing before runtime side effects when that coordinate is absent;
the Matchmaker constructor and JSONL state now use `reference_beat` in their
names too, removing the ambiguous `initial_score_beat` label that invited the
incorrect diagnosis. Reference coordinate distance is already the exact warp
integral; anchor rolls prefer source-performance coordinates; and failed
cancellation after device delivery aborts the reactive send rather than
duplicating it. Dispersion remains a
repeatability gain (median absolute deviation, not mean error): variable
expressive intent is not a reliable point prediction. Smoothing and stricter
anchor context remain trace-triggered changes, not speculative patches.

## [2026-07-26] section-aware start from any printed measure | Issue #126

Wired the performer score selection into live FOLLOW and deterministic replay.
The server resolves printed measure -> canonical 960-PPQ score beat ->
reference-performance beat once, records both coordinates in `runtime_start`,
and retains the bundle-authored `FOLLOW`/`LEAD`/`HOLD`/`STOP` authority at the
selected beat.

Live entry has a four-quarter routed count-off at the section/config tempo.
The engine seeds its future downbeat during the final quarter so entry events
cross the deadline boundary in time; a pre-entry LEAD tick cannot retime them
backward. FOLLOW seeds Matchmaker's PTHMM distribution at the mapped reference
beat and uses a bounded two-stable-onset warm lock instead of the broad-start
three-onset policy. Replay truncates fully aligned input at the interpolated
entry time and preserves post-entry relative scheduler targets. Runtime JSONL
adds explicit `count_off` and `runtime_start` rows. Browser coverage proves a
clicked score measure is the `start_measure` sent by **Perform live**.

## [2026-07-26] anchor-aware LTE coupling remains YAGNI | Issue #130

Evaluated Decision 0010's re-entry trigger in the deterministic production-path
harness. A matched reference warp owns all future timing while a test curve
injects a 200 ms miss only at an anchored orchestral beat. The active reactive
anchor removes the full miss. The next orchestral beat remains at `0.0 ms` and
has the same delivery time as the no-anchor arm to `1e-12` seconds on the same
input digest.

The runtime trace explains the result: the anchor output lands at the triggering
piano onset, then that same onset immediately becomes the follower/LTE tempo
observation. LTE phase is already refreshed through the ordinary production
path, so a separate anchor callback would count one observation twice. No
anchor-aware clock coupling was added. Re-enter only if a Yamaha trace shows
repeatable immediate post-anchor drift while independently confirming that the
local reference/Interpretation expectation curve matches the passage.

## [2026-07-26] dispersion as live trust gain | Issue #129

Wired the fitted rehearsal Interpretation into LTE FOLLOW through the
curve-aware scheduler seam, without reviving #124's invalid
`reference_beat=None` flat fallback. `InterpretationArrivalCurve` integrates
future canonical half-quarter cells and blends each fitted period against the
reference-warp interval with
`clamp(1 - (MAD / period) / 0.15, 0, 1)`. Profile lookup converts canonical beat
to 960-PPQ tick once. Expected period and MAD are scaled together to the run's
requested base tempo, leaving the dimensionless trust unchanged.

Missing/invalid dispersion, relative MAD >=15%, and cells supported by fewer
than two takes all produce gain zero and exactly preserve reference/reactive
delivery. Supported zero MAD produces gain one. The fitted curve applies only
to FOLLOW; authored LEAD timing is unchanged. It loads automatically for LTE
when `profiles/<piece>/<movement>/profile.json` has usable cells, while absent
profiles degrade to the existing reference curve. Curve revision/input/scale
is recorded in schema-2 closed-loop reports and scheduler JSONL trace rows.

On the fixed deterministic broadening, the trusted fitted arm removes the
synthetic onset bias; high- and missing-dispersion arms match reference
deliveries to `1e-12` seconds on the same input digest. Unit coverage pins the
continuous bounded gain, canonical coordinate, one-take guard, tempo scaling,
and LEAD exclusion.

## [2026-07-26] curve-aware scheduler seam and closed-loop proof | Issue #127

Made future-event timing an explicit injectable `ArrivalTimeCurve`. The
production `ReferenceWarpArrivalCurve` integrates the piecewise
canonical-to-reference warp by taking the current-to-event
reference-performance delta and applying the live reference-period scale once;
`FlatScoreArrivalCurve` preserves the canonical scalar-period path as a named
evaluation baseline. `LiveEngine.from_bundle` and the deterministic closed-loop
harness accept the seam without adding a runtime mode or speculative
Interpretation wiring. Closed-loop report schema 2 records the stable curve ID,
so the intended A/B difference remains explicit beside otherwise matched input
digests and configs.

This corrected the issue's stale diagnosis. Movement II mapped events had
already preserved the reference curve; the behavior was hidden inside
`scheduler._schedule_event`, while only unmapped events used flat canonical
extrapolation. The new deterministic tests show the reference path effectively
exact on a synthetic ritardando while the flat arm exceeds 250 ms maximum onset
error, with the gap increasing under stronger curvature. Both paths agree at
steady tempo. A zero-lead LTE arm remains exact on the matched curve, but the
live lead default is retained because follower/detection phase is orthogonal to
scheduler geometry. Re-entry trigger: reduce it only when matched Yamaha JSONL
traces show equal or better landmark error with a smaller bound.

## [2026-07-26] deterministic closed-loop evaluation | Issue #128

Replaced noisy tempo-model comparisons with a bit-exact production-path
evaluation harness. `evaluate_closed_loop` drives the real
Follower -> LiveEngine/TempoModel -> AccompanimentScheduler ->
DeadlineAccompanimentOutput path on `ManualClock`, advancing deterministically
to recorded note onsets, the live 2 ms control cadence, and immutable MIDI
deadlines without sleeps or worker races. Source/reference follower position
uses the same `CanonicalFollower` seam as live FOLLOW; canonical identity is
converted once.

The versioned `closed-loop-onset-v1` report stores seed, complete config, an
input digest, delivered event times, per-canonical-quarter signed/absolute onset
error, named landmarks, and aggregates. A fixed aligned take is byte-identical
across repeated runs; a committed MIDI fixture also runs twice identically via
an explicit note-index-to-canonical-beat join. The aligned fixture matches its
frozen live-output trace to `1e-12` seconds and gives stable reactive/LTE A/B
timing on the same digest. Output-advance
coverage proves metrics are observed after the deadline seam rather than at a
one-step scheduling proxy. This harness measures only; #127 owns the
curve-aware scheduler, and the Interpretation evaluator remains a curation
compass rather than architecture evidence.

## [2026-07-26] human beat anchors | Reactive firing on annotated beats; LTE default

Added human-curated **beat anchors** ([Decision 0010](decisions/0010-human-beat-anchors.md)).
An echo test had shown the software+MIDI turnaround is ~0.07 ms, so the ~50 ms in
FOLLOW is all prediction, not I/O; and measure-44 analysis showed the LTE residual
(~47 ms) is variance, not a removable constant lag. At structurally important beats
the performer knows the exact note and plays it crisply, so those beats can fire
**reactively** off the matched piano onset instead of being predicted.

New `AnchorFirer` fires an anchored chord from a bass-pitch-matched,
previous-follower-position-gated piano onset. Adversarial review then exposed a
real ownership bug: a chord already transferred to the deadline output could
also fire reactively. The scheduler now atomically replaces the entire
still-pending deadline chord and declines after delivery starts, so the same
note cannot double-play. Trigger selection uses the lowest solo pitch and
canonical distance; the bounded prior-position gate is explicit. Recorded roll
offsets convert once from reference-performance beats using the reference
period; invalid periods collapse rather than smear.

Anchors remain optional: with none, or no qualifying bass/chord, runtime is
exactly LTE. The stronger original claim that no anchor could ever make a run
worse was retracted: an accidental matching bass can still pre-fire, so anchors
belong on crisp, structurally unique onsets and the JSONL MIDI trace remains
ground truth. Anchor persistence now serializes concurrent edits and rejects
nonpositive movements. The score click inverse and cursor share explicit
measure-boundary knots. Persisted at
`profiles/<piece>/<movement>/anchors.json`, separate from the fitted
Interpretation; REST CRUD and performer overlay anchor mode are unchanged.
Flipped `follow_clock` default to `"lte"`. Deferred with an explicit trigger:
anchor-aware prediction *between* anchors only if the deterministic harness
measures post-anchor drift; start-from-any-measure remains a separate issue.

## [2026-07-25] performer UX + calibration | Full-screen score and latency loopback

Added a full-screen performance score view (page + live cursor only, one page
fit to the viewport, auto page-turn) for playing to the score without the
rehearsal chrome. Made the Yamaha output-advance a 10 ms-step control that is
adjustable mid-run through an acknowledged engine-thread mailbox
(`POST /api/runtime/output-advance`), bounded to the dispatch horizon so a
committed event always transfers before its advanced deadline.

Added metronome-loopback latency calibration
(`POST /api/runtime/calibrate/latency`): a short click sequence where the
performer plays one key per click; the median send-to-receive offset suggests
the output-advance. Documented the negative-mean-asynchrony bias, so the result
is a suggestion, not auto-applied.

Reconstructed the ACCompanion lineage while answering "why not just use it":
Rubato deliberately adopted ACCompanion's modular architecture and extended its
unsolved follow/lead "fourth task" (the section-policy/transport machine), but
uses Matchmaker instead of ACCompanion's followers and has not yet ported its
predictive `L`/`LTE` tempo-coupling. That port — a phase-locked FOLLOW clock
seeded by the rehearsal profile — is the identified next experiment for true
simultaneity, kept separate from this change.

## [2026-07-24] live runtime | Multi-horizon transport ownership

Pressure-tested the post-PR-119 runtime against an independent architecture
review and the performer’s producer/consumer model. The cited Jul-23 hardware
trace is valid historical evidence, but it predates the final Jul-24 PR-119
merge and therefore is not proof that the final deadline worker failed.

Adopted the durable invariants instead of the review’s larger speculative
surface. `LiveEngine` now has one six-state authority object; the scheduler
owns generation-stamped mutable plans; an atomic boundary transfers the short
prefix to a deadline-only worker; stale reference-derived deadlines expire
instead of bursting; and panic is a barrier even for a command already popped
by the worker. JSON serialization/filesystem I/O moved to a bounded trace
writer. Handoffs preserve the observed performance phase and use a narrowly
scoped two-estimate entry policy, while global Matchmaker lock remains three
updates.

Documented why Rubato is not adding continuous FOLLOW/LEAD blending, a musical
epoch in the renderer, one unified MIDI queue, or a 10–15 second online matcher
without evidence. Added deterministic coverage for the commit boundary,
generation invalidation, stale dual-coordinate deadlines, early/on-time/late
entries, panic concurrency, asynchronous trace drain, and superseded live
controls. See [Decision 0007](decisions/0007-multi-horizon-runtime-transport.md).

## [2026-07-23] live output | Immutable deadlines and relock suppression

Reconstructed the latest Yamaha trace and separated three latency layers.
Follower processing was normally sub-10 ms and CoreMIDI adapter calls were
normally sub-millisecond, but mode handoffs released stale crossed events as
much as 1.2 seconds late. Near-term orchestral events now leave the mutable
planner 100 ms ahead and enter a dedicated monotonic deadline worker; follower
computation no longer owns their final wait. Events crossed under a different
authority are durably suppressed instead of replayed after relock.

Review hardening also recalibrates mid-section metronome edits over only the
remaining canonical/reference span, anchors releases to actual backend
completion, serializes device writes without holding the volume-state mutex,
and buffers credible pickup evidence until an authored `LEAD` section ends.
Trace analysis reports adapter-call distributions, late-onset thresholds, and
suppressed IDs. The PWA exposes a collapsed 0-100 ms Yamaha output advance for
repeatable setup latency only; zero remains the default and the control is not
used to conceal follower drift or stale transport errors.

## [2026-07-23] live timing | Canonical tempo and section-owned scheduling

Reconstructed live run `live-1784856081981`: accepted lead controls had a
median of 87 BPM, while actual MIDI note-ons joined to canonical score positions
realized about 31 BPM. Performer BPM had been applied directly to a denser
expressive reference coordinate. `LEAD` sections now normalize their wall
duration in canonical score quarters while retaining the reference phrase
shape. Fixed orchestra playback and rehearsal cues now use the same principle:
448 dense score↔MIDI anchors infer a robust nominal reference pulse of about
`50.79 BPM`, replacing the incorrect assumption that the source file's
declared `120 BPM` tick clock was its notated-quarter tempo.

The same trace showed measure-22 `LEAD` events planned from the preceding
`FOLLOW` clock and a confident measure-23 follower update ending the interlude
after 0.64 seconds. Scheduler lookahead is now section-bounded and pianist
observations cannot terminate an authored autonomous passage. Trace analysis
now reports canonical/reference/actual-output lead tempo, with synthetic and
real Movement-II regressions. Scheduler output now follows an ordered event
watermark: an onset crossed between ordinary updates is emitted exactly once,
and forward resynchronization no longer sends a catastrophic all-notes-off.
The architecture docs now treat `FOLLOW`/`LEAD` as authority endpoints over one
prior/live transport and define cadence handoff as pianist-owned phase plus
orchestra-owned outgoing tempo.

## [2026-07-22] live control + observability | Reliable mix and output timing

Root-caused the live balance failure: the PWA volume fader had no live handler,
and the MIDI renderer exposed no runtime mix control. Added an acknowledged
live-volume command and scaled every orchestral channel's authored CC7 at the
output boundary, preserving relative orchestration and note velocity. The
initial mix now travels in the live-run config, changes affect sounding notes,
and the UI distinguishes orchestra mix from the `LEAD`-only metronome value.

The latest hardware trace showed scheduler-lateness outliers but could not
prove the actual CoreMIDI send time, emitted velocity, or release lateness.
Output traces now record adapter call duration, actual onset/release timing,
velocity, CC values, and accepted tempo/mix controls. Note duration is anchored
after measured backend note-on delay. The trace analyzer and deterministic
fake-port/browser regressions expose these signals for the next focused Yamaha
run; automatic solo-conditioned dynamics remains a separate, unevaluated CC11
model rather than being conflated with the manual mix.

## [2026-07-22] live runtime | Review hardening: owned policy, coasting, acknowledged tempo

Validated the six Antigravity findings against PR 118's actual clock model. The
proposed change that would continually warp the reference clock was rejected:
that clock intentionally scales the shared Oguri phrase uniformly, while the
source-to-canonical map carries local rubato. The engine now derives and traces
the local canonical beat period from that map's derivative, so diagnostics no
longer imply the two coordinate rates are identical.

Five real failure modes/design risks were fixed. Movement II revision
`movement2-symbolic-path-v4` now owns continuous LEAD/FOLLOW spans in a hashed
`derived/sections.json`; the machine fallback uses exact solo gaps without
`int`/`round` quantization. Low-confidence FOLLOW updates coast for a bounded
1.5 seconds from the last trusted timing state, keeping cursor and orchestra on
one clock, then explicitly transition to HOLD and trace recovery. Live tempo
edits use a latest-wins acknowledged mailbox instead of an unbounded queue and
the PWA rolls back to its last server-confirmed BPM on failure. The direct
LEAD-to-FOLLOW path now explicitly resumes the scheduler after clearing its old
plan, preserving transition symmetry.

## [2026-07-21] live runtime | Shared-reference scheduling and complete lead gaps

Reconstructed the latest Yamaha run from `runtime.jsonl`. Follower processing
was not CPU-bound (p95 about 6 ms), but a temporary zero-confidence estimate
made the scheduler return before dispatching an already frozen note, producing
a measured 981 ms late onset. A stale post-interlude estimate also forced
`FOLLOW` back inside the m.22 `LEAD` section and generated destructive
skip/repeat panics. Finally, the section map contained only the opening and
m.22 exception, so the orchestra had no autonomous clock after the piano
resolved around m.52.

The runtime now preserves both canonical score beat and the shared Oguri source
beat produced by matching Yamaha input to the split solo reference. Canonical
beat owns measure/beat, cursor, and policy; shared source beat directly owns
split-orchestra onset spacing and note durations. Committed notes dispatch even
during tracker uncertainty, `LEAD` clock progress cannot be mistaken for a
score jump, and stale estimates cannot re-enter `FOLLOW`. Movement II derives
all significant orchestra-led gaps from symbolic solo inactivity. The live
metronome endpoint reanchors active `LEAD` playback when `♩ = N` changes.

Runtime traces now include both coordinates and actual MIDI note-on, note-off,
retrigger, and panic events. Deterministic scheduler/engine/output/projection
tests and a Chromium journey cover the 981 ms regression, source timing and
duration, live tempo changes, stale handoff, m.52 continuation, and browser API
command.

## [2026-07-21] performer UX | Route intent before transport

Replaced the ambiguous generic Orchestra transport plus detached Go Live
control with one top-level intent launcher. The three idle choices are now
**Perform live with orchestra**, **Rehearse a passage**, and **Listen to
orchestra only**; their supporting copy states the starting location, solo
entry or score-selection step, and whether Rubato listens/follows. Live state
now wins over stale hardware status in the stage label, the live action becomes
its own stop action while running, and orchestra-only playback never implies
following. Idle Space no longer guesses which workflow the pianist intended.

Added a Chromium journey that exercises all three routes, verifies the
orchestra-only API contract, and rejects both the generic Orchestra label and
the duplicated Go Live action.

## [2026-07-20] live runtime | Beat evidence, measure-22 lead, reconstructable traces

Root-caused the failed live run from its durable trace. Note-level Matchmaker
positions within one chord were incorrectly treated as tempo observations,
driving the clock from 52 BPM through 683 BPM to 1,981 BPM. The raw follower
also moved backward 12 times, which the cockpit rendered directly. At the end
of the solo phrase the section policy remained `FOLLOW`, so measure 22 had no
autonomous clock; its accompaniment was dispatched in a premature burst by the
tempo spike and was already spent when the interlude should have sounded.

Separated symbolic position evidence from beat-level tempo evidence. Small
backward jitter is clamped for the cursor; tempo now requires a meaningful
score/time baseline, rejects impossible candidates, and smoothly incorporates
accepted beat periods. Movement II explicitly owns measure 22 as `LEAD`, and
the engine can enter later lead sections, advance with no piano input, stop at
their exclusive boundary, and reacquire the soloist afterward. Autonomous
passages ignore follower timing, so the opening cannot be modulated by input.

Reduced the Matchmaker wait budget from 30 ms to 5 ms and interleaved scheduler
ticks after every dense-chord note without sharing scheduler state across
threads. Expanded runtime traces with raw/stabilized location, follower
latency/state, tempo admission decisions, policy transitions, per-note target
and send timing, lateness, and panic reasons; unchanged plan snapshots are no
longer duplicated at control-loop frequency. Added a deterministic trace
analyzer plus regressions for the recorded spike shape, cursor jitter, later
lead handoff, dispatch boundary, and trace reconstruction.

## [2026-07-20] rehearsal/live UX | Replace relative pace with a metronome mark

Removed the performer-facing Orchestra pace percentage. The cockpit now owns
one exact quarter-note metronome value (`♩ = N`) for orchestra-only playback,
rehearsal cue-ins, and the orchestra-led live opening. The live API already
consumed BPM directly; the two fixed-MIDI endpoints now accept `tempo_bpm` and
convert it at the server boundary against the Oguri source's declared
`♩ = 120` clock. Relative `tempo_scale` remains an internal scheduling and
historical-artifact detail rather than a musical control. A new local-storage
key deliberately ignores the obsolete percentage, defaults to the learned
profile suggestion, and preserves an explicitly entered BPM.

Backend and Chromium regressions pin `♩ = 88` through cue, orchestra, and live
requests so the number shown to the pianist is the number used by the runtime.

## [2026-07-19] live runtime | Orchestra-led Movement II opening and causal handoff

Root-caused the frozen **Go live** workflow to the provisional runtime's single
all-`FOLLOW` section: the scheduler could not start until a piano note produced
a follower estimate, although the orchestra owns the Larghetto opening.
Movement II now derives an opening `LEAD` region from the first accompaniment
event through the first solo onset (m.12 beat 4), starts at the first sounding
event at the rehearsal-profile base tempo, advances on one absolute monotonic
clock, waits at the handoff without panicking sustained notes, and changes to
`FOLLOW` after symbolic lock. Fixed the live input worker's relative-vs-absolute
clock-domain mismatch at the same boundary. The real Movement II projection now
also maps both accompaniment events and Matchmaker follower updates from
expressive Oguri ticks to canonical score quarters before tempo/policy; this
prevents a nominal BPM from stretching the m.1-to-m.12 opening according to the
wrong coordinate.

Added `GET /api/runtime/plan` and a score-first cockpit plan that explains m.1,
the m.12 pickup, learned starting BPM, and the **Start orchestra + go live**
action before opening MIDI ports. Deterministic engine/projection/server tests
and a real Chromium journey cover no-input orchestra startup, state transition,
request tempo, and cursor motion.

## [2026-07-15] docs | QUICKSTART.md: retire pending-merge TODO markers

All six PRs from the previous grooming pass (#95, #102–#106) merged to
`main`. Rewrote each `[TODO: rubato#N — pending merge]` marker in
`QUICKSTART.md` to describe current behavior instead of a future one:

- rubato#99: one-time setup is now the single command
  `./scripts/dev-server.sh` (it pulls DVC artifacts and opens the browser
  itself); the explicit `open` command is now framed as the fallback path.
- rubato#98: dropdown-empty guidance now describes the actual in-app
  distinction between "no MIDI backend installed" and "backend fine, no
  ports visible."
- rubato#96/#97: "Go live" is now a normal numbered step describing the
  shipped **Go live** button, state words, and Experimental badge — no
  longer gated behind a PR-merge conditional.
- rubato#100: "Ending a session" leads with the **End session** control in
  the Library drawer, Ctrl-C kept as the alternative.
- rubato#101: the DVC-artifact-missing troubleshooting line now describes
  the Ready-face warning that already surfaces it.

Also deleted the "Appendix: live FOLLOW from the terminal" section — it was
explicitly a temporary developer path pending the Go Live button and said
so ("It will be removed once PR #105 merges").

## [2026-07-14] groom | Docs restructure: fix design/ naming collision and misplacement

Full docs/ audit driven by git history (more recent commit wins on conflicts).
Two structural fixes:

- `docs/design/SYSTEM_DESIGN.md` (added PR #91 `2d76336`, the three-workflow
  star-model diagram wrapper) collided in basename with the canonical
  `docs/SYSTEM_DESIGN.md` (runtime architecture, kept current through PR #92
  `40de300`). Renamed the wrapper to `docs/design/THREE_WORKFLOW_OVERVIEW.md`.
- `docs/REHEARSAL_TAKE_COVERAGE_DESIGN.md` (added PR #64 `f974458`) reads as a
  standalone feature design doc — it explicitly extends
  [Vision and UX Design](VISION_AND_UX_DESIGN.md) and
  [System Design](SYSTEM_DESIGN.md) the same way
  `design/RECORDING_FLOW_REDESIGN.md` and `design/LIFECYCLE_AND_ARTIFACT_V2.md`
  do — so it moved to `docs/design/REHEARSAL_TAKE_COVERAGE_DESIGN.md`.

Also trimmed `ARCHITECTURE_BRIEF.md`'s two mermaid diagrams: they duplicated
`SYSTEM_DESIGN.md`'s diagrams but had gone stale since PR #42 (`19c03fe`)
while `SYSTEM_DESIGN.md` kept gaining nodes (PWA/backend, trace logger) through
PR #92. The brief now points at `SYSTEM_DESIGN.md` for the diagrams and keeps
its unique prose.

Verified and ruled out from a prior partial audit: `pwa-rehearsal-ui.md`'s
"four-screen sketch" is already fully superseded in the current file (as of
PR #92) — no stale section survives. `docs/.obsidian/` is gitignored, not a
committed-docs violation. Deferred-scope terms (style transfer, SOLOIST vs OTHER,
raw audio) all appear only as deliberate non-goal callouts.

Updated all internal links for both moves (`docs/INDEX.md`,
`docs/DOCUMENTATION_MAP.md`, `docs/design/RECORDING_FLOW_REDESIGN.md`,
`docs/sources/joseffy-reduction.md`, this log, and five code-comment path
references under `src/aimusic/takes/` and `tests/`). `make docs-health`
passes with only pre-existing non-blocking warnings.

## [2026-07-13] architecture | Event-driven hardware and rehearsal lifecycle

Replaced `WS /api/events`' 50 ms timeout/get-nowait loop with concurrent waits
for client disconnect and the broadcaster queue. Modeled the shared Yamaha job
slot with explicit `idle`, `running`, `stopping`, `completed`, and `failed`
phases, validated transition edges, idempotent updates, and typed
`hardware:status` events. Added `coverage:materialized` after the durable
coverage job commits successfully.

The cockpit now uses REST only for mount/reconnect snapshots and consumes
hardware, take-alignment, and coverage transitions between snapshots. Removed
the 1 s hardware-status and 1.5 s alignment polling loops. UI clock timers,
socket reconnect backoff, and the real-time MIDI scheduling tick remain because
they advance time-dependent work rather than discover lifecycle state.

## [2026-07-12] docs | Screenshot-driven Movement 2 quickstart

Replaced the pre-cockpit screenshots and stale 284-measure instructions with
three captures of the current unified Movement 2 UI. Rewrote the Quickstart to
cover DVC materialization, server startup, Yamaha selection, orchestra sound
check, score navigation, free and cued takes, capture options, browser preview,
keyboard safety controls, disabled/future controls, and the explicitly
noncanonical developer-only live FOLLOW endpoint. Added a prominent start link
and minimal launch sequence to the README.
The launch verification also exposed two first-run cleanup defects: orphaned
materialization jobs were retried after their take directories disappeared,
and the event WebSocket prevented one-signal server shutdown. Recovery now
cancels orphan jobs and the socket observes disconnect state; the later
2026-07-13 event-driven revision replaced its short timeout loop with
concurrent awaitables.

## [2026-07-12] feature | Durable derived-artifact jobs and migration inventory

Moved profile and coverage rebuilding from synchronous alignment callbacks to
persisted `rebuild_profile` and `rebuild_coverage` jobs. Jobs deduplicate by
pinned input revision, are idempotent after success, record failures and
attempts, and recover queued or stale-running work at server startup. Profile
materialization is idempotent for an unchanged aggregate take revision;
coverage pins and deduplicates against the resulting profile revision.

Extended `rubato migrate-takes-v2` with a read-only compatibility inventory:
v1-only, v2-only, dual-write, fallback-required, and alignment-sidecar counts,
plus an explicit readiness signal for retiring v1 reads. Dry-run mode reports
the untouched pre-migration store; apply mode reports post-migration state.

## [2026-07-12] feature | Lifecycle v2 runtime/store integration

Cut the rehearsal take pipeline over to authoritative `take.v2.json` and
`aligned.v2.json` artifacts while retaining atomic v1 compatibility sidecars.
Take analysis, performer disposition, and profile membership now transition
independently: discard during alignment cannot erase its result, restore is a
real command, and worker crashes are `failed` rather than musical
`unalignable` outcomes. Ambiguous candidates expose stable `candidate_id`
identity with temporary index compatibility.

Alignment and resolve jobs are persisted under each movement's take store with
attempts, failures, pinned input revisions, restart recovery, and idempotent
terminal handling. Profile/coverage materialization moved to write-side state
changes; coverage GET is read-only, and coverage is observation-backed from
`cell_samples` with a compatibility fallback for old alignments. Generated
OpenAPI/TypeScript/Zod contracts now expose lifecycle axes, candidate IDs,
restore, and artifact revisions.

Append-only record of substantial documentation, research, ingest, lint, and
grooming work.

## [2026-07-12] ingest | Movement 2 structural OMR and draft bundle

Ran Audiveris 5.11.0 on the 15-page Joseffy Movement 2 reduction. Full note and
rhythm transcription produced many recognition inconsistencies, so the durable
pipeline now stops at structural `GRID` recognition and clusters staff barlines
without treating OMR notes as score truth. It recovered 126 machine-review
measure boxes. Added DVC-managed OMR, Oguri source/derived MIDI, an explicit
machine-draft Bundle v2 manifest and timeline, and readiness gates requiring a
reviewed semantic score before rehearsal or live performance.

## [2026-07-12] source | Movement 2 Joseffy reduction display artifact

Verified the downloaded 107-page Schirmer/Joseffy two-piano reduction as the
performer-facing score candidate. Movement 2 occupies PDF pages 55-69; extracted
that 15-page window reproducibly with `scripts/extract_reduction_pages.py` and
placed the binary under DVC. Recorded hashes, edition provenance, and the
display-only semantic boundary in
[Joseffy Two-Piano Reduction](sources/joseffy-reduction.md). The reduction PDF
does not define canonical score time: Audiveris/manual layout evidence must map
it to a notation-derived timeline, while the expressive Oguri MIDI needs its
own score-performance alignment.

Use entries like:

```text
## [YYYY-MM-DD] type | Title
```

Types:

- `ingest`: source or research compiled into docs.
- `query`: useful answer or synthesis filed back into docs.
- `lint`: health check without major restructuring.
- `groom`: restructuring, backlinking, deduplication, or index updates.
- `decision`: durable product/architecture decision recorded.

Through 2026-06-16: pivoted from solo-piano style transfer to real-time Chopin
concerto accompaniment ([Decision 0001](decisions/0001-pivot-live-accompanist.md)).
Flattened `docs/wiki/` into `docs/` itself as the knowledge repo and split
primary-source notes into atomic overview/mechanics/evaluation pages for
ACCompanion, Matchmaker, and HeurMiT ([sources](sources/), [concepts](concepts/)).
Chose Matchmaker as the score-following wrapper and the Yamaha CLP-795GP as the
synth target ([Decision 0002](decisions/0002-tracker-and-synth-mvp.md));
researched Magenta RealTime 2 and deferred it to future neural-audio rendering
work ([Decision 0003](decisions/0003-magenta-rt2-renderer-research.md)).
Committed the first Chopin Op. 11 source set under `assets/scores/` with a
code-level [Score Bundle Contract](concepts/score-bundle-contract.md). Built the
initial runtime seams (`ScoreFollower`/`OracleFollower`/`OnlineTempoModel`/
`AccompanimentScheduler`) and a deterministic [Simulated Online Harness](concepts/simulated-online-harness.md),
then validated the pipeline against a real MusicXML excerpt (Op. 11 I mm.
139-141) and the real Matchmaker package running in offline MIDI mode
([Matchmaker Real Follower Tests](concepts/matchmaker-real-follower-tests.md)).

## [2026-07-06] decision | Lightweight docs-as-memory conventions

Compared Rubato's docs against local conventions in `jesse-and-eric` and
`my-life`. Added [Documentation Map](DOCUMENTATION_MAP.md),
[Local Wiki Conventions](sources/local-wiki-conventions.md), and
[Decision 0004](decisions/0004-docs-memory-conventions.md). Rubato adopts
search-before-create, merge-over-create, one-fact-one-place, source
immutability, and path-of-entry linkage while deferring the full Obsidian /
memory-wiki compile stack.

## [2026-07-06] tooling | Docs health and Obsidian review

Added `make docs-health`, `scripts/docs_health.py`, a lightweight repo-local
docs groom skill, and [Obsidian Review](OBSIDIAN.md). The docs remain plain
Markdown plus git, but are now explicitly optimized for local Obsidian review
without relying on wikilinks, Dataview, or vault plugins.

## [2026-07-06] tooling | Shared agent skill discovery

Added `.claude/skills` as a symlink to `.agents/skills` so Claude and
Codex-style agents share the same repo-local docs-groom skill instructions.
Kept the skill text agent-neutral and repo-relative.

## [2026-07-06] source | Oguri movement 2 MIDI

Added the Oguri/Kunst der Fuge Chopin Op. 11 second-movement MIDI as the first
private-use live-following target. the soloist preferred the Oguri playback quality and
asked to start with movement 2 because it is slower and easier to sight-read.
Documented provenance and redistribution caveats in
[Oguri / Kunst der Fuge MIDI](sources/oguri-kunstderfuge-midi.md).

## [2026-07-06] implementation | Yamaha record/play scaffold

Added `rubato` CLI commands for MIDI device listing, Yamaha recording,
Oguri movement-2 orchestra playback, and panic. Added backend rehearsal
endpoints plus a PWA hardware panel for record, playback, stop, panic, and
global volume. The first supported playback source is the Oguri second movement
with the `PIANO SOLO` track suppressed and default orchestra volume 75%.

## [2026-07-06] implementation | Immediate take review

Updated the PWA hardware rehearsal flow so a completed backend Yamaha recording
can be loaded or played immediately in the existing MIDI Player, which already
has play/pause and scrub controls.

## [2026-07-06] implementation | Oguri solo/accompaniment extraction

Added deterministic extraction of the Oguri movement-2 MIDI into
`solo_reference.mid` and `orchestra_accompaniment.mid` under the score bundle's
derived directory. The split preserves the source timing grid and separates the
`PIANO SOLO` track from the non-solo note-bearing orchestra tracks for follower
and rehearsal work.

## [2026-07-06] implementation | Cued Yamaha recording workflow

Added a backend/PWA `record_with_cue` workflow that records Yamaha MIDI while
playing a short Oguri orchestra cue ending at the first movement-2 piano entry.
The default cue is 8.0 seconds so it includes audible orchestral attacks before
the solo entry, and the backend writes cue metadata for later alignment.

## [2026-07-06] fix | Cued recording sustained-note playback

Fixed a silent-cue bug in the first-entry rehearsal workflow. The final
4 seconds before the first solo entry are musically sustained orchestra notes,
but the raw MIDI note-on attacks occur slightly before that window. The cue
renderer now carries active notes into the cue slice and logs cue event counts
so future silent cues are diagnosable.

## [2026-07-06] fix | Cued recording release tail

Analyzed the soloist's first `movement2_take` and found that the cue slice was still
omitting post-entry note-off events for five sustained orchestra notes. Updated
the cue renderer to continue sending release events after the nominal solo entry
without starting new orchestra attacks. Compiled the take analysis in
[Yamaha Take Analysis](concepts/yamaha-take-analysis.md).

## [2026-07-06] implementation | Offline alignment and render scaffold

Added reusable offline alignment/render components plus `rubato align-midi` and
`rubato render-offline`. The scaffold extracts MIDI note events, performs
pitch-sequence local alignment, fits a piecewise-linear timing map, retimes
accompaniment MIDI, and writes run trace/metrics artifacts. Documented the
workflow in
[Offline Alignment And Render](concepts/offline-alignment-render.md).

## [2026-07-06] implementation | PWA-triggered offline accompaniment render

Added the backend/PWA path for rendering accompaniment from a selected rehearsal
session. `POST /api/sessions/{session_id}/render-offline` now aligns
`data/processed/<session_id>/solo.mid` against the Oguri movement-2 solo
reference, writes run trace/metrics/output artifacts, and exposes
`accompaniment.mid` as a session playback variant. Updated
[PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md) and
[Offline Alignment And Render](concepts/offline-alignment-render.md).

## [2026-07-06] implementation | Session MIDI playback through Yamaha backend

Added backend/PWA playback for any available session MIDI variant.
`POST /api/sessions/{session_id}/midi/{variant}/play` resolves the same
registered session variants used by downloads, then sends the MIDI to the
selected backend output through `LiveControl`. This lets a rendered
`accompaniment.mid` be played through the Yamaha without relying on browser Web
MIDI routing.

## [2026-07-06] implementation | Phrase-level offline timing anchors

Added phrase-level timing anchors as a smoothing abstraction above raw matched
note anchors. `phrase_timing_anchors_from_alignment` groups nearby matched notes
and uses median reference/performance times so one locally early or late note
does not dominate the accompaniment timing map. The render path still defaults
to note anchors until real take evaluation validates phrase settings.

## [2026-07-06] implementation | Rehearsal cockpit UI redesign

Redesigned `webapp/src/App.svelte` from a generic MIDI-console layout into a
dark, concert-hall-styled "Perform / Record / Session" cockpit (Fraunces +
Inter typography, brass/ember palette instead of admin-dashboard blue or
AI-purple dark mode). The emergency stop control (renamed from "Panic" to
"Silence" — the MIDI-world term read as alarmist for this UI's calm tone)
lives in a sticky masthead rather than a floating corner button, after an
earlier floating variant was found to overlap the Record control at certain
scroll positions.

## [2026-07-06] implementation | Merge cockpit redesign onto hardware rehearsal controls

Rebased the cockpit redesign onto the Oguri/hardware-rehearsal work (#51),
which landed in parallel and shipped exactly the backend capability the
redesign had assumed did not exist yet. Rewired the Perform and Record decks
to call the real backend (`/api/midi/devices`, `/api/hardware/*`) instead of
the earlier Web MIDI/Tone.js-only approximations: `Play Orchestra` now starts
real Oguri movement-2 playback, `Record Yamaha Take` drives the backend
recorder, and `Silence` calls `/api/hardware/panic` followed by
`/api/hardware/stop` (calling both, since `panic()` reports the job idle
without actually reaping the background thread — calling `stop()` afterward
is a call-site workaround, not a fix to `live_control.py`). Dropped the
client-side Web MIDI device enumeration and browser-captured recording path
entirely, since keeping it alongside the real backend path would have meant
two different, confusingly-named "Record Yamaha Take" controls. The local
MIDI Player transport is now WebAudio-only and relabeled "Local Preview" to
distinguish it from live hardware playback. Updated
[PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md#current-implementation-rehearsal-cockpit-2026-07)
to match.

## [2026-07-07] implementation | Merge cued recording, offline render, and hardware playback into the cockpit

Rebased the cockpit again onto five more parallel PRs (#53-#57) that shipped
cued Yamaha recording (`record-with-cue`), offline accompaniment rendering
(`render-offline`), and session-variant hardware playback
(`/sessions/{id}/midi/{variant}/play`) against the pre-redesign flat UI.
Integrated all three into the cockpit: the Record deck gained a
"Play orchestra lead-in cue before recording entry" toggle (on by default,
matching the existing runbook) with a Cue Seconds field, and the Latest Take
card gained `Render Accompaniment`/`Render & Play`. The Session deck's
Outputs list gained a `Yamaha` button next to `Play` for routing any session
variant to real hardware. Continued using the unified Orchestra Volume fader
(0-100%) for the cue and hardware-playback volume fields instead of
reintroducing a separate `oguriVolume` state.

## [2026-07-07] query | Vision and UX design document

Read the full docs directory, current cockpit implementation, and README from
first principles, then compiled
[Vision and UX Design](VISION_AND_UX_DESIGN.md): what Rubato is, the unified
rehearsal/performance loop, the interpretation-profile learning model
(beat-indexed tempo/dynamic curves with recency-weighted statistics over
aligned takes, grounded in ACCompanion's LTE reference-performance result),
follower/tempo/dynamics/section-policy behavior with a failure-mode table, a
six-stage user workflow, a one-window three-face UI design (Ready / Live /
After) with 2-meter glanceability rules, the canonical aesthetic language
extending the 2026-07 cockpit palette, twelve open questions with
recommendations, and a thirteen-item implementation roadmap. Linked from
[INDEX](INDEX.md) core docs and added ownership to
[Documentation Map](DOCUMENTATION_MAP.md).

## [2026-07-07] groom | Restore PRD and dedupe development docs

Rewrote [PRD](PRD.md) as an actual product requirements doc for the Chopin
live-accompanist MVP, drawing on
[Vision and UX Design](VISION_AND_UX_DESIGN.md) for intent and workflow. The
2026-06-14 pivot commit had overwritten the PRD with a near-copy of
[Index](INDEX.md), leaving only an orphaned "Interaction Surfaces" section
from the old numbered PRD; that four-screen sketch is superseded by the
vision doc's three-face design, so the section was dropped and
[PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md) points at the vision doc as
the target UI. Also removed `PARALLEL_DEVELOPMENT.md`, which had become
byte-identical to [Development](DEVELOPMENT.md); DEVELOPMENT.md now
explicitly owns multi-agent worktree coordination in
[Documentation Map](DOCUMENTATION_MAP.md), and [Index](INDEX.md) was updated
to match.

## [2026-07-07] planning | Vision UX implementation gap plan

Updated `main` to the latest PRD/vision-doc state, created a Codex worktree,
and compared the target in [Vision and UX Design](VISION_AND_UX_DESIGN.md)
against the current Svelte cockpit, FastAPI hardware/session API, offline
alignment/render path, simulated-online primitives, Matchmaker wrapper, tempo
model, scheduler, and section policy. Added
[Vision UX Implementation Plan](concepts/vision-ux-implementation-plan.md) to
record the practical gap assessment and phased migration from today's
Perform/Record/Session cockpit to the Ready/Live/After product loop,
including API, live-status, follower, scheduler, profile, recovery,
measure-map, and reflection work.

## [2026-07-07] groom | Remove agent-team-role scaffolding

Removed the old SWE/Scientist/TPM persona-and-boundary layer that predated
this project's single-agent-per-session workflow: the "Agent Responsibilities"
section and the `[SWE]`/`[SCIENTIST]`/`[TPM]` commit-prefix convention from
[AGENTS.md](../AGENTS.md), the matching commit-format list from
[Development](DEVELOPMENT.md), and the "Scientist/SWE Boundary" section from
[Experimentation](ML_EXPERIMENTATION.md). This was meta-layer coordination
guidance for collaborating agents, not music-system or architecture content,
and the soloist flagged it as an outdated distraction. Worktree coordination itself
(still real, still used) stays documented in
[Documentation Map](DOCUMENTATION_MAP.md) and [Development](DEVELOPMENT.md).

## [2026-07-08] build | Stage cockpit UI redesign

Restructured the Svelte rehearsal cockpit from three parallel Perform/Record/
Session tool decks into a single "stage" surface aligned with the vision doc's
aesthetic law and 2-meter glanceability rules: piece title and color-coded
state word in large Fraunces, ≥64 px Orchestra/Record/Stop transport pills, a
large analog orchestra fader, a quiet sound-check device row, Take and Preview
wing decks, and session plumbing folded into a collapsed Library drawer. Added
the Space=primary-action / Escape=Silence keyboard model. All backend API
calls, the canonical stage/ivory/brass/ember palette, and the masthead Silence
control carried over unchanged; the web e2e test now opens the Library drawer
before driving session controls. Updated
[PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md) to describe the new state.

## [2026-07-09] spike | Localization de-risk (roadmap item 1)

Validated the two-stage alignment approach from
[Rehearsal Take Coverage Design](design/REHEARSAL_TAKE_COVERAGE_DESIGN.md) §2.4
against the real movement-2 solo reference
(`assets/scores/chopin_op11_ii_larghetto/derived/solo_reference.mid`, 2831
notes). No recorded take fixture (`data/processed/movement2_take/`) was
available in this checkout — it is DVC-managed and not pulled — so the spike
uses synthetic partial takes: contiguous slices of the reference at lengths
20–80 notes with 10% of notes dropped, drawn from random positions across the
whole movement. Added
[`tests/accompaniment/test_localization_spike.py`](../tests/accompaniment/test_localization_spike.py).

Result: pitch n-gram seeding (n=4..6, chord-grouped, pitch-sorted within
chord) plus the existing `align_note_events` local aligner localizes
97–100% of ≥20-note synthetic takes uniquely and correctly across ten random
seeds (roadmap done-when: ≥90%), with the fine aligner hitting
`match_rate = 1.0` inside the localized window in the overwhelming majority
of trials. The ambiguity detector correctly flags a genuine 23-note repeated
passage in the piece (canonical positions ~2605 and ~2719, the coda restating
an earlier phrase) and does not flag unrelated unique passages.

One real bug surfaced and fixed during the spike: naively bucketing the
diagonal-vote histogram by a fixed bin width splits a single true location
into two adjacent buckets when drop-induced drift lands near a bin boundary,
which the naive top-2-margin check then misreports as ambiguous — this
dropped the raw accuracy to 61–81% depending on seed. Fixing it required
merging candidate positions within the stage-2 alignment window's slack
(15 notes) before computing the ambiguity margin, since two candidates that
close would be captured by the same fine-alignment window anyway and are not
a meaningful second score location — only merge-surviving candidates like the
real coda repeat (114 positions apart) represent genuine ambiguity.

Conclusion: roadmap item 1 is de-risked. Proceed to item 2 (take store +
capture path) and item 3 (alignment pipeline v1, which should port this
spike's n-gram index and merge-aware ambiguity check from the test file into
`offline_alignment.py` as production code).

## [2026-07-10] fix | Real profile fold, not placeholder data (roadmap item 4)

`aimusic/takes/coverage.py`'s `compute_coverage` wrote `profile.json` with
fake data: a hard-coded `velocity = 64.0` for every cell, `spread = 0.0`
always, and no pedal signal at all -- despite `docs/design/REHEARSAL_TAKE_COVERAGE_DESIGN.md`
§2.3/§2.5 specifying `cell_samples` (real per-cell velocity/timing/pedal
resampled from each take's MIDI) and a quality-weighted recency-median fold.
`aligner.py`'s docstring had explicitly deferred `cell_samples` to "the
profile-fold pipeline (roadmap item 4)," which had never been built.

Added [`aimusic/takes/profile.py`](../src/aimusic/takes/profile.py):
`compute_cell_samples` resamples a take's actual note-on velocities, local
tempo (the take/score timing map's seconds-per-beat slope, not an IOI density
proxy), and CC64 sustain pedal (step-function-sampled, not averaged) onto the
grid, using the take's `timing_map` anchors both directions (take-seconds ->
score-beat for note bucketing, score-beat -> take-seconds for pedal
sampling) via `PiecewiseLinearTimingMap`. `fold_takes` fuses `cell_samples`
across every `ALIGNED` take with a weighted median (`_weighted_median`/
`_weighted_mad`), weight = per-cell quality × 0.5^(recency_rank / 8) --
an eight-take-per-cell half-life, ranked per cell (not globally), matching
§2.5's fusion rules exactly. `align_take` now populates `cell_samples` and
`edge_trim_beats` on `AlignedTake`, so they're persisted in `aligned.json` for
future full rebuilds. `coverage.py`'s existing span+match_rate cell-count
logic for `coverage.json` (measure `state`/`min_n`) was left untouched --
that data was already real, just coarser; only the `profile.json` curve
generation was faked.

One real bug caught by testing before merge: the first cut snapped grid
cells to `score_start_beat + k*0.5` -- relative to each take's own start
beat, not an absolute grid. Two overlapping takes essentially never start on
the same note, so their cells would (almost) never land on the same beat
value and could never be fused. Fixed to `round(beat / grid_beats) *
grid_beats`, an absolute grid from 0, matching the convention `coverage.py`'s
own cell-count logic already used. Regression test:
[`tests/takes/test_profile.py::test_cell_grid_is_absolute_not_relative_to_each_takes_start`](../tests/takes/test_profile.py).

Not done: `rubato profile show/fold/unfold/rebuild` and `rubato coverage`
CLI (roadmap item 4's other done-when clauses), and pedal is captured but
still not consumed anywhere downstream (§2.5: "folded from v1.1", by design).

## [2026-07-10] fix | Real movement-2 beat->measure map, not a magic-number guess

`coverage.py`'s movement-2 (Oguri recording) branch hard-coded "119 measures
of 2 beats each" and a `scale_factor = 1332.0 / (119 * 2)` to rescale grid
lookups -- a guess, not a derivation, and wrong on both counts. Inspecting
`assets/scores/chopin_op11_ii_larghetto/derived/solo_reference.mid` directly:
it carries exactly one `time_signature` meta message, 4/4, and the movement
is 1332 beats long (`end_of_track` at tick 319680 / 240 ticks-per-beat) --
1332 / 4 = 333 measures exactly, not 119 of 2 beats. The "1332.0" constant in
the old code was the right total-beats number; everything built from it
("119 measures", "2 beats each") was not.

Worse, the fallback marked every measure `solo: True` unconditionally. The
piano track has real tacet stretches -- a 36-measure orchestral introduction
before the piano's first note (beat ~144.25) plus four shorter interludes
(measures 60, 79, 131-135, 265-268) and a 2-measure orchestral close -- 49 of
333 measures have no piano note at all. Coverage percentages were being
computed against the wrong denominator (119 fake measures, all counted as
literal "piano part" per the design doc's §2.6 definition) rather than the
true 284 solo measures.

Added `parse_midi_measures()` to `coverage.py`, mirroring the existing
`parse_mxl_measures()` (movement 1's MusicXML equivalent, per [Vision Open
Question 4](VISION_AND_UX_DESIGN.md)): walks the MIDI's own
`time_signature` meta messages to lay out measure boundaries in beats
(handles mid-piece signature changes generally, though the Oguri file has
only one), and flags `solo` from whether the "PIANO SOLO" track has any
note-on inside each measure's span -- real tacet detection, not an assumed
constant. Extracted the measure-rollup logic (grid-cell -> `covered`/
`touched`/`uncovered`/`tutti` state) that movement 1 and movement 2 were
each duplicating almost verbatim into a shared `_rollup_measure()`, since
both now just need `{measure, start_beat, end_beat, solo}` in and don't care
which parser built it.

Tests: `tests/takes/test_coverage.py::test_parse_midi_measures_derives_real_time_signature_and_solo_status`
pins the exact structure (333 measures, 284 solo, first solo measure is 37);
`test_coverage_movement_2_uses_real_measure_map` checks the full
`compute_coverage` path reflects it end to end.

## [2026-07-10] feature | Ambiguous-take resolution endpoint + UI

Ambiguous takes (design doc §2.4: a genuinely repeated passage where the
top-2 localization candidates score within 15% of each other) had no way to
resolve -- `store.AMBIGUOUS` was a terminal state with no exit besides
discard. Implements `POST /api/takes/{id}/resolve` (design doc §3.2/§4.3:
"It was here / It was there") plus a minimal inline card in the take list
(the full PDF-strip candidate highlighting from §3.2 is future work, gated
on the measure-geometry anchor tool, roadmap item 7).

`aligner.py`'s `candidates` only ever carried `{start_beat, score}` -- not
enough to *re-align* at a chosen candidate, only to relabel
`score_start_beat`. Added `start_position` (the reference index) to each
candidate, and extracted `_align_at_position()` (stage 2/3: windowed
alignment, quality gate, cell-sample resampling) out of `align_take()` so a
new `resolve_take()` can re-run the same real alignment at the chosen
candidate's position instead of just relabeling a beat -- `match_rate`,
`timing_map`, and `cell_samples` all get recomputed for the window actually
chosen, not carried over stale from the original (possibly wrong) window.
Resolving never re-flags `ambiguous`: choosing a candidate is the
resolution, so the outcome is `aligned` or `unalignable` per the same
`MATCH_RATE_GATE` every other take is held to. The original candidate list
is preserved in `aligned.json` after resolving, for audit.

Verified end-to-end in a browser against a real seeded ambiguous take (the
repeated-coda fixture from `tests/takes/test_aligner.py`): the take list
shows the `AMBIGUOUS` badge with a "Couldn't place this take — which is
it?" card (`It was here` / `It was there` / `Discard`); clicking a
candidate flips the take to `ALIGNED` and the coverage grid updates to
reflect it, with no console errors.

Tests: `tests/takes/test_aligner.py::test_resolve_take_picks_the_other_candidate`
(and two rejection-path tests) at the aligner layer;
`tests/test_takes_api.py::test_resolve_take_endpoint_picks_the_other_candidate`
(and 404/422 tests) at the route layer.

Ten rounds of Gemini review turned up several real bugs beyond the initial
implementation, each fixed with a regression test confirmed to fail
against the pre-fix code:

- **Discard race**: a background alignment/resolve job unconditionally
  overwrote a take's status on completion (success or exception path),
  which could clobber a `DISCARDED` status the user set while the job was
  still running. Fixed by re-checking the take's current status
  immediately before every status-changing write and skipping if already
  `DISCARDED` -- applied across four call sites (`_perform_resolve`,
  `_run_resolve`'s exception handler, and both the success and
  exception-handler paths of the pre-existing `align_and_store`/`_run`,
  since it's the same bug class in the same file).
- **Synchronous resolve blocking the request thread**: `resolve_take` was
  initially wired directly to the route, meaning the CPU-bound re-align
  work (same cost as the original alignment pass) ran on the request
  thread. Split into `_validate_resolve_candidate` (fast, synchronous --
  so a bad `candidate_index` or non-ambiguous take still gets an immediate
  404/422) and `AlignmentWorker.enqueue_resolve` (backgrounds the actual
  work), matching the existing `enqueue`/`_run` pattern exactly.
- **`piece_id` path traversal**: unlike `take_id` (a URL path segment,
  normalized client-side before a raw `..` ever reaches the server),
  `resolve_take_route`'s `piece_id` arrives as a JSON body field and was
  joined unvalidated into a filesystem path via `paths.take_dir`. A
  `../../etc` payload reached the take store and returned a 404 instead of
  being rejected outright. Fixed by validating against the same
  `_SESSION_ID_PATTERN` already used for `take_id`/`session_id`. The same
  gap on other pre-existing endpoints (`delete_take`, `download_take_midi`,
  etc.) was flagged separately as follow-up work, out of this PR's scope.
- **Malformed `candidates` on disk**: a corrupted or hand-edited
  `aligned.json` with a non-list or partially-malformed `candidates` field
  raised a raw `KeyError`/`TypeError` instead of a clean `ValueError` (422)
  or, worse, crashed the entire take listing on one bad record. Both
  `_validate_resolve_candidate` and `_take_response` now guard against it.
- **Frontend poll-timer bugs**: the auto-poll added to detect `aligning`
  takes reset its timer *before* the awaited refresh calls completed
  (allowing a concurrent re-entry to schedule an overlapping poll), and
  wasn't cleared in `onDestroy` (matching the existing `statusPollTimer`
  cleanup it should have mirrored from the start). Also fixed a toast-copy
  bug where the post-resolve success message always read "could not be
  placed" because it checked for the wrong intermediate status.

Gemini Code Assist stopped responding after round 9 (three `/gemini
review` retriggers over ~30+ minutes with zero reply, versus a consistent
2-3 minute turnaround for every prior round) -- consistent with its
review body's own notice that the consumer version is being sunset, with
review activity ceasing 2026-07-17. Rather than block indefinitely on an
unresponsive external service, PR #79 was merged on the strength of the
nine substantive rounds already completed and addressed, with the full
test suite green and the round-9 fix (piece_id validation, malformed-
candidates guards) verified locally.

## [2026-07-10] feature | Measure-geometry map + Level 1 PDF coverage overlay (roadmap item 7)

Movement 1's score bundle now has `measure_boxes.json`
(`assets/scores/chopin_op11_i_allegro_maestoso/derived/`), built by
`scripts/build_measure_boxes.py` / `aimusic.takes.measure_boxes`. Method,
per design doc §3.3's own priority order: `score.mxl`'s `<print
new-system="yes"/new-page="yes">` elements (identical across all 15 parts of
this full-score export) give exact, free system/page membership -- 139
break markers, 140 systems, 98 pages, cross-checked against
`parse_mxl_measures`'s independent measure count so the two readers must
agree or the build fails loudly. The *vertical* extent of each system is
the doc's sanctioned fallback: an even split of each page's usable band
across its known system count -- correct membership, approximate banding,
never OMR.

Backend: `GET /api/scores/{movement}/pdf` and
`GET /api/scores/{movement}/measure_boxes` (`piece_id` query param, matching
the existing `/api/coverage/{movement}` convention). Added
`paths.score_bundle_dir(piece_id, movement)` and used it to de-duplicate the
hardcoded movement-1 bundle path that used to live only in `coverage.py`.

Frontend: new `webapp/src/PdfCoverageOverlay.svelte` (pdf.js canvas + an
absolutely-positioned SVG overlay, plus a measure-strip that doubles as the
page scrubber per §3.2). Mounted in `App.svelte` alongside (not replacing)
the existing movement-2 coverage grid, since only movement 1 has a PDF
today.

Found and fixed a real pdf.js integration bug while building this: the
component used to both assign `pdfDoc` (which a `$:` block watches to
trigger the first page render) *and* call `renderPage` directly from the
same load path. That races two `page.render()` calls against one canvas 2D
context -- pdf.js does not throw when that happens, it wedges the render
pipeline forever with no error, which froze the entire page (reproduced via
Playwright: a `details.library > summary` click that normally resolves in
under 100ms timed out at 30s, with zero console output). Fixed by keeping
exactly one call site and guarding the reactive statement against re-firing
on unrelated component invalidation (it does re-fire on nearly every parent
update, not only when its tracked values change -- tracking the
last-requested doc/page explicitly makes the render idempotent regardless).
Regression-tested in `tests/test_web_ui_e2e.py::test_score_coverage_overlay_renders_pdf_and_scrubs_pages`,
which asserts on real rendered canvas pixel content (mostly-white paper with
black ink), not just DOM presence.

Tests: `tests/takes/test_measure_boxes.py` (builder, synthetic + real
movement-1 bundle), `tests/test_server_api.py` (new routes),
`tests/test_web_ui_e2e.py` (end-to-end render + page navigation).

## [2026-07-10] feature | `WS /api/events` + capture-cockpit toast/auto-arm (roadmap item 5)

Built the WebSocket plumbing design doc §2.7 specified but nothing had
implemented yet (`grep -rn websocket` across the backend and `webapp/src`
came up empty before this work): an in-process pub/sub
(`aimusic.core.events.EventBroadcaster`, singleton at
`aimusic.core.events.events`) and `WS /api/events`
(`aimusic.server.routes.take_events_ws`). Put in `core` rather than
`server` so `aimusic.takes.aligner` (which needs to publish from the
background alignment thread) doesn't have to depend on the `server`
package -- `server` already depends on `takes`, so the reverse would have
inverted the existing layering.

Threading note that cost real debugging time: the alignment worker
publishes from a `ThreadPoolExecutor` thread, so `EventBroadcaster.publish`
hands events to connected clients' `asyncio.Queue`s via
`loop.call_soon_threadsafe`, using the loop bound at app startup (now a
`lifespan` context manager, replacing the deprecated `@app.on_event`
hook). FastAPI's `TestClient` only runs that startup hook -- and gives
every request/websocket its own throwaway event loop otherwise -- when used
as `with TestClient(app) as client:`; the plain `TestClient(app)` form
(what the existing take-API tests use, and which is fine for tests that
don't touch the broadcaster) silently skips lifespan entirely and any
WebSocket test built on it just hangs forever waiting for a message that
can never arrive. `tests/test_events_ws.py` documents this in the fixture.

Three events, matching the design doc's take.json status lifecycle
(§2.2 `captured -> aligning -> aligned | ambiguous | unalignable`):
`take:recording_started` (on `/api/takes/record` and
`/hardware/record-with-cue/start`, not on the legacy session-only
`/hardware/record/start` -- that path never becomes a take, so labeling it
one would be misleading), `take:recording_stopped` (on
`/api/takes/{id}/stop`, once `take.json` is actually written -- not, as
originally assumed, "on `/hardware/record/stop`", which is the legacy
session path and never touches the take store), and `take:alignment_done`
(from `aligner.align_and_store`/`_perform_resolve`/the `AlignmentWorker`
exception handlers, whichever actually lands the terminal status --
skipped when a take was discarded mid-flight, same discard-race the status
updates themselves already guarded).

Frontend: `webapp/src/takeEvents.ts` is a small reconnect-with-backoff
WebSocket client (1s initial delay, doubling to a 15s cap); `App.svelte`
subscribes on mount and drives a dedicated `captureToast` (separate from
the general single-line `message`/`.toast`) that appears on
`take:recording_stopped` ("placing it in the score...") and resolves in
place on `take:alignment_done` for the same `take_id` ("kept" /
"couldn't place it"), per design doc §3.1. Confirmed by inspection that the
Record button was never actually gated on alignment status (only on
`hardwareStatus.running`, which flips idle as soon as the stop request
returns) -- so "auto-arm" was already true before this work; what was
missing was the toast resolving on its own instead of just timing out.

Tests: `tests/test_events_ws.py` (broadcaster unit tests + `TestClient`
WebSocket route tests), `tests/takes/test_aligner_events.py` (alignment
pipeline emits the right payload at every terminal transition, independent
of real thread timing), `tests/test_capture_flow_e2e.py` (Playwright,
against a real uvicorn server with `live_control`/`alignment_worker` faked
the way `test_takes_api.py` does: real WebSocket wire, real Svelte
reactivity -- toast resolves in place, button re-arms before the fake
alignment worker's artificial delay elapses, and the socket reconnects
after a forced drop).

## [2026-07-11] decision | Recording flow redesign: free takes vs. cued takes

Implemented [Recording Flow Redesign](design/RECORDING_FLOW_REDESIGN.md) in
full (§3.1-3.5; §3.6 "From here" stays a disabled placeholder pending
arbitrary-position orchestra playback). Added `docs/design/` as a new
top-level knowledge category (see [Documentation Map](DOCUMENTATION_MAP.md))
for standalone feature/UI design docs scoped to one surface, as distinct from
[Vision and UX Design](VISION_AND_UX_DESIGN.md)'s whole-product scope.

Root cause the doc diagnosed: the "Play orchestra lead-in cue before
recording" checkbox modeled a *mode* (different endpoint, different device
requirements, different score-position assumption) as a *preference*, with
the wrong default (`true`), and secretly also drove the stage transport's
Record button through a legacy session-slot path
(`ensureSessionSlot` -> `/hardware/record/start` / `/hardware/record-with-cue/start`
-> `/hardware/record/stop`) that never touched the take store at all.

Backend (`src/aimusic/server/routes.py`,
`src/aimusic/server/live_control.py`, `src/aimusic/server/schemas.py`):
`/hardware/record-with-cue/start` now records cue metadata (`kind`,
`target_beat`, `cue_seconds`, `output_name`) server-side keyed by take_id
(survives a page reload mid-take) and `POST /takes/{id}/stop` attaches it to
`take_store.save_take(..., cue=...)` -- `take.json.cue` is no longer always
`null` for a cued take, giving the aligner the localization hint the
coverage design always intended it to have. The cue-start response also
reports `actual_cue_seconds` (the truncated lead-in when the requested
length would start before the movement's first solo entry), computed via
the new shared `compute_cue_start_seconds` helper, so the client's countdown
never lies about how long the lead-in actually is.

Frontend (`webapp/src/App.svelte`): removed `playLeadInCue` and the
checkbox entirely. The Take Capture deck is now `Record Take` (primary,
free take, default) plus `▶ From the top · Ns lead-in` (secondary, cued
take) and a disabled `▶ From here` placeholder (§3.6, v2). An explicit
`captureState` machine (`idle -> lead-in -> recording -> idle`, or
`idle -> recording` directly for a free take) replaces the old
`isHardwareRecording`-only branching, so a cued take's countdown state is
distinct from its recording state even though the recording itself starts
at cue-start server-side. The stage transport lost its `Record`/`End Take`
buttons entirely (design doc §3.5 option 1) -- it's Orchestra/Stop only now,
with Stop disabled while a take is recording so it can never end one
without going through `/takes/{id}/stop`; the space bar's primary action
was rewired the same way (stop the take, or cancel the lead-in, before
falling back to stop-playback/play-orchestra).

Tests: centralized the three near-identical `FakeLiveControl` copies
(`test_hardware_api.py`, `test_takes_api.py`, `test_capture_flow_e2e.py`)
into `tests/fake_hardware.py` so no test depends on real MIDI hardware --
all 166 tests pass with the Yamaha disconnected. Added
`test_cued_take_saves_through_the_same_take_store_pipeline` and
`test_compute_cue_start_seconds_truncates_at_the_movement_start` to
`test_takes_api.py`; updated `test_capture_flow_e2e.py` (checkbox removed,
so free takes no longer need an `.uncheck()` step) and
`test_web_ui_e2e.py` (the PDF overlay's `▶` next-page button needed
`exact=True` once the deck's own `▶`-prefixed buttons existed).

## [2026-07-11] decision | Schema/validation architecture review (root cause of PR #82 guard whack-a-mole)

Architecture review before implementing issues #83/#84. Full findings and
the green-light design in
[Schema & Validation Architecture](design/SCHEMA_VALIDATION_ARCH.md).

Diagnosis: the system has exactly one validated data boundary (FastAPI
request ingress via Pydantic); the other four -- persisted JSON artifacts
read back from disk (`take.json`/`aligned.json`/`profile.json`/
`coverage.json` come back as raw `dict[str, object]`), WS events
(hand-built dict literals, contract only in a hand-written TS union),
frontend response parsing (`any` / `as Type` assertions), and the
hand-written TS mirror types -- pass unvalidated data, so every consumer
grew its own null/NaN/`isinstance` guard. Timestamps typed `str` instead
of `datetime` are the type-level root of the `started_at` saga
specifically. Guards on true external input (user keystrokes,
MusicXML/MIDI files) are correct and stay.

Decision: Pydantic v2 (already a dependency) becomes the single source of
truth for every shape -- artifacts and WS events get models (#85, new
issue, lands first); the OpenAPI spec is exported statically and
`@hey-api/openapi-ts` + its Zod plugin generate the frontend's TS types,
runtime schemas, and typed client from it (#83/#84, rewritten with the
chosen stack and deletion scope); CI regenerates and diffs so drift fails
the build. Fail-loudly at boundaries replaces per-field silent fallbacks.
Rejected: msgspec (second schema system), beartype/typeguard (wrong
boundary), hand-written Zod or JSON Schema (recreates the drift).

## [2026-07-11] decision | Implement #85: Pydantic v2 artifact + WS event schemas

Landed the backend half of [Schema & Validation Architecture](design/SCHEMA_VALIDATION_ARCH.md)
(issue #85). New `src/aimusic/takes/models.py`: frozen Pydantic v2 models
for every persisted artifact -- `TakeRecord` (`take.json`, with an explicit
`TakeCue` sub-model and a `status: Literal[...]`), `AlignedResult`
(`aligned.json`, with `TimingMapPoint`/`LocalizationCandidate`/`CellSample`
sub-models -- `LocalizationCandidate.start_position: int | None = None` is
the one intentional widening, replacing a stringly `"start_position" not
in candidate` version-skew probe), `ProfileDoc` (`profile.json`), and
`CoverageDoc` (`coverage.json`). `store.py`'s read path is now
`Model.model_validate_json(...)` (replacing `Take(**data)  #
type: ignore[arg-type]` and raw-dict `aligned.json` reads); write path is
`model_dump_json()`. `aligner.py`, `profile.py`, and `coverage.py` build
these models directly instead of dict literals; every isinstance/
key-presence probe this created downstream (`aligner.py`
`_validate_resolve_candidate`, `routes.py` `_take_response`,
`coverage.py`'s raw indexing) is deleted -- a malformed on-disk file now
raises `pydantic.ValidationError` loudly instead of being silently
tolerated or defaulted.

WS events: `TakeRecordingStarted`/`TakeRecordingStopped`/`TakeAlignmentDone`
plus a `type`-discriminated `TakeEvent` union added to
`aimusic.server.schemas`; `EventBroadcaster.publish()` now takes a Pydantic
model (`model_dump_json()`), not a hand-built dict, at all four publish
sites (`routes.py` x3, `aligner.py`'s `_publish_alignment_done`). The union
is injected into the OpenAPI document's `components.schemas` in
`aimusic.server.app` (FastAPI only auto-includes schemas reachable from an
HTTP `response_model`, and `/api/events` is a plain `WebSocket` route), so
issues #83/#84's future codegen covers it.

Timestamps (`started_at`, `recorded_at`, `updated`, `computed_at`) are now
timezone-aware `datetime` end-to-end; the three duplicate strftime-based
`_utc_now()` copies (`store.py`, `live_control.py`, `profile.py`'s
`fold_takes`) collapsed into one `aimusic.core.time.utc_now()` helper.
API response models (`TakeResponse`, `TakeCandidateResponse`) reuse the
artifact sub-models instead of parallel re-declarations; `GET
/api/coverage/{movement}` serves `CoverageDoc` directly as its
`response_model` (typed pass-through, no last-moment re-validation).

Added `scripts/export_openapi.py`, dumping `app.openapi()` to a committed
`webapp/openapi.json` (design doc §2.2) -- the static export #83/#84's
frontend codegen pipeline will read from, no running server required.

Testing: `tests/takes/test_models.py` and `tests/test_server_schemas.py`
add round-trip (write -> read -> equal) and malformed-file
(`pydantic.ValidationError`) coverage per model, plus frozen-model
immutability checks. All existing tests updated to the typed
attribute-access API (no data migration -- the models mirror the prior
on-disk shape exactly). Frontend (`.svelte`, `takeEvents.ts`) untouched:
that is issues #83/#84's scope.

## [2026-07-11] feature | Implement #84: Zod runtime validation at the frontend API boundary

Landed the frontend-consumer half of [Schema & Validation
Architecture](design/SCHEMA_VALIDATION_ARCH.md) (issue #84), on
`feat/frontend-zod`. Issue #83 (hey-api codegen from `webapp/openapi.json`)
had not merged yet, so `webapp/src/generated/` is a hand-written stub --
`schemas.ts` (Zod schemas + `z.infer` types mirroring every Pydantic
response/request model in `aimusic.server.schemas` /
`aimusic.takes.models`), `client.ts` (`apiRequest` transport +
`parseJson` validator, replacing the old bare `api()` fetch helper), and
`index.ts`. Each file's header says it is superseded 1:1 when #83 lands;
consumers only ever import from `./generated`, so the swap stays
contained to that directory.

`App.svelte`: deleted the hand-written mirror types (`SessionSummary`,
`FileState`, `PlaybackEntry`, `SessionStatus`, `OfflineRenderResponse`,
`MidiDevicesResponse`, `LiveStatusResponse`) in favor of the generated
ones; `takes: any[]` -> `TakeResponse[]`, `coverageData`/
`movement1CoverageData: any` -> `CoverageDoc | null`; every `take: any`
parameter (`playTake`, `toggleDiscardTake`, `resolveTake`,
`discardAmbiguousTake`) -> `TakeResponse`. All ~20 bare
`await response.json()` / `as MidiDevicesResponse` / `as
OfflineRenderResponse` sites now go through `parseJson(schema, context,
...)`, which throws (logging via `console.error`) on a schema mismatch --
every call site already sits in a try/catch that reports the error via
`setMessage`, so this is the "one error surfaces at the boundary" policy
from design doc §2.2 with no restructuring needed.
`takeEvents.ts`'s `JSON.parse(event.data) as TakeEvent` assertion is now
`TakeEventSchema.parse(...)`.

Deleted the residue design doc §1.3/§3 catalogued: `parseTimeOrNow()` and
both call-site fallbacks (`started_at` is `z.coerce.date()` --
already a real, validated `Date` by the time app code sees it, so a
malformed timestamp now fails loudly at the response boundary instead of
degrading to "pretend it started now"; the two sites that still need a
`Date` where the type is nullable -- cued-recording start and reload
recovery -- now call a `requireStartedAt()` assertion that throws instead
of silently substituting `Date.now()`); `formatRecordedTime`'s NaN
fallback/try-catch (recorded_at is a guaranteed-valid `Date`);
`data.takes ?? []`; `event.duration_seconds ?? 0`; `take.duration_seconds
?? 0` / `take.note_on_count ?? 0` (same class of residue, same fix);
`summary?.percent_covered` chaining collapsed to one outer `coverageData?.`
(a legitimate "not loaded yet" guard) with hard dots into the
now-guaranteed `summary` fields. `PdfCoverageOverlay.svelte`'s
hand-written `CoverageMeasure`/`CoverageData`/`MeasureBoxSystem`/
`MeasureBoxesData` mirror types are gone in favor of the generated
`CoverageDoc`/`MeasureBoxesResponse` types, and its `measure_boxes.json`
fetch goes through `apiRequest`/`parseJson` too.

Added `webapp/src/generated/schemas.test.ts` (new `vitest` dependency,
`npm run test`) asserting the fail-loudly policy directly: a well-formed
response yields real `Date` fields, a structurally invalid response and an
unparseable timestamp both throw `ApiValidationError`, and a WS event
outside the discriminated union throws. `tsconfig.json` `strict` was
already on; no `@ts-ignore`/`@ts-expect-error` added. `npm run check`
(svelte-check) is clean, `npm run build` was re-run and the rebuilt bundle
verified against the full Playwright E2E suite
(`test_web_ui_e2e.py`/`test_capture_flow_e2e.py`) plus a manual browser
pass -- no console errors, coverage/session/capture flows all correct.

**Superseded same day** by #83's `@hey-api/openapi-ts` codegen landing (next
entry): the hand-written `webapp/src/generated/schemas.ts`/`client.ts` stub
above was deleted wholesale and replaced by the generated equivalent, exactly
as its own file headers said would happen. The two Gemini-review fixes from
this entry's follow-up commits (try/catch wrapping the remaining bare
`response.json()`-adjacent call sites; defensive `/api` path-prefix
normalization in the now-deleted `apiRequest` helper) were reconciled forward
during that merge -- the try/catch coverage gap was real and got ported into
the post-codegen `App.svelte`; the path-prefix bug class doesn't exist in the
generated client (each SDK function's URL is baked in from the OpenAPI spec,
not assembled from a caller-supplied path), so no equivalent fix was needed
there.

## [2026-07-11] decision | Implement #83+#84: generated TS types, Zod validation, typed client

Landed the frontend half of [Schema & Validation Architecture](design/SCHEMA_VALIDATION_ARCH.md)
(issues #83 and #84, combined into one PR per the doc's own §4 sequencing
note -- one `@hey-api/openapi-ts` generation pass produces both halves).

Pipeline: `webapp/openapi-ts.config.ts` reads the committed
`webapp/openapi.json` (from #85's `scripts/export_openapi.py`) and
generates `webapp/src/generated/` -- TS types, Zod runtime schemas, and a
typed fetch client -- via three plugins: `@hey-api/typescript`, `zod`, and
`@hey-api/sdk` with `validator: { response: 'zod' }`. The generated client
is configured `throwOnError: true` / `responseStyle: 'data'`
(`@hey-api/client-fetch` plugin option + `@hey-api/sdk` option): every SDK
call resolves to the parsed response body directly and throws uniformly on
a non-2xx response *or* a Zod validation failure -- the design doc's
"fail loudly at the boundary" policy, enforced by the generator's own
`responseValidator: async (data) => await zSchema.parseAsync(data)` inside
each generated function, not by convention. Generation is wired into
`npm run predev`/`prebuild` (chained after re-running `export_openapi.py`),
and `make check-types` (new) regenerates both stages and
`git diff --exit-code`s them -- wired into a new `webapp-checks` CI job
(`.github/workflows/ci.yml`), since CI previously had no Node step at all.

`src/aimusic/server/routes.py`: every route now sets an explicit
`operation_id=` (e.g. `"getCoverage"`, `"startTakeRecord"`) -- FastAPI's
default (`get_coverage_api_coverage__movement__get`, the function name plus
the full path) produces unusable generated client function names.

Deleted per design doc §3: every hand-written API mirror type in
`App.svelte` (`SessionSummary`, `FileState`, `PlaybackEntry`,
`SessionStatus`, `OfflineRenderResponse`, `MidiDevicesResponse`,
`LiveStatusResponse`) and `PdfCoverageOverlay.svelte` (`CoverageMeasure`,
`CoverageData`, `MeasureBoxSystem`, `MeasureBoxPage`, `MeasureBoxesData`),
replaced by the generated equivalents; `takeEvents.ts`'s hand-written
`TakeEvent` union, replaced by the generated type, with the WS parser now
`zTakeEvent.parse(...)` instead of `JSON.parse(...) as TakeEvent`; every
`takes: any[]` / `coverageData: any` / `take: any` boundary; the ~20 bare
`await response.json()` sites and `as MidiDevicesResponse` /
`as OfflineRenderResponse` assertions, routed through the generated client
instead; the bare `api()` fetch helper, retired. Whack-a-mole residue that
became dead once the real types landed: `parseTimeOrNow` (replaced by
`startedAtMsOrNow`, which only resolves the field's legitimate *absent*
case to "now" -- there's no more unparseable-string case to guess around,
since a malformed `datetime` throws before this code runs),
`formatRecordedTime`'s NaN fallback, `data.takes ?? []`,
`event.duration_seconds ?? 0`, `take.duration_seconds ?? 0` /
`take.note_on_count ?? 0`, and the nested `summary?.percent_covered?`
optional chains (now a single top-level null check where `coverageData`
itself is a real "not loaded yet" state, or none at all where a guard
already narrows it). Kept, per design doc §1.4: the `cueSeconds` derivation
from `cueSecondsInput`, the `hardwareRecordSeconds` parse, and all
MusicXML/MIDI parsing guards -- genuine external-input handling, not
symptoms of the schema gap.

**Real bug the generator surfaced, not found by inspection:** the WS event
models' `type: Literal[...] = "..."` default made `type` *optional* in the
emitted JSON Schema (Pydantic: a field with a default isn't `required`), so
hey-api's Zod plugin generated `.optional().default(...)` for the
discriminator instead of a plain literal. Zod v4's `discriminatedUnion`
can't extract a literal value through that wrapper, so every event's `type`
resolved to the same `undefined` discriminator key, and the *first* real
`take:recording_stopped` message sent to a running browser threw
"Duplicate discriminator value" inside `takeEvents.ts`'s `onmessage`
handler -- caught by the existing `test_capture_flow_e2e.py` Playwright
test once the webapp was rebuilt against the new generated client (the
test had been silently exercising a stale pre-#83/#84 bundle checked into
`src/aimusic/server/static/` until then). Fixed by making `type` a required
field with no default on all three event models and passing it explicitly
at every construction site (`routes.py` x3, `aligner.py` x1, plus three
test files); a discriminator must always be present on the wire regardless,
so this is the correct shape, not a workaround. Regression test:
`tests/test_server_schemas.py::test_type_is_required_not_defaulted`.

Testing: `tests/test_frontend_schema_validation_e2e.py` (new) is the design
doc §5 "one test that a schema-invalid response rejects" -- a real
Playwright browser hits a real server whose `/api/midi/devices` response is
intercepted and replaced with a schema-violating body (`inputs` as a string
instead of `string[]`), asserting the generated client throws, the app
reports it via `setMessage`/`describeApiError` instead of adopting the bad
value, and the device pills stay in their "not connected" state. All
existing Python tests pass unchanged; `npm run check` (svelte-check, TS
strict mode) is clean; `make check-types` is a verified no-op against the
committed output.
## [2026-07-12] decision | Score Bundle v2 canonical timeline foundation

Accepted and implemented the initial strict Pydantic Score Bundle v2 sourcing
contract: explicit source/derived roles, immutable bundle/timeline identity,
integer canonical score ticks with measure/meter projection, partial monotonic
source mappings with explicit gaps, workflow readiness, filesystem/hash
validation, and an explicit registry. Added a synthetic fixture covering a
pickup, unusual measure label, meter change, expressive-MIDI mapping, and an
unmapped score span. The existing runtime event bundle remains available only
through `LegacyScoreBundleAdapter` during migration.

Recorded the architecture in
[Decision 0005](decisions/0005-score-bundle-v2-canonical-timeline.md) and
[Score Bundle v2 Contract](concepts/score-bundle-contract.md). Corrected two
unsafe global assumptions: Oguri MIDI ticks are expressive performance
coordinates rather than notated score beats, and PDF/MusicXML page-count
equality applies only to files explicitly paired as the same layout engraving.

## [2026-07-12] implementation | Hardware-independent live runtime v2

Added typed run lifecycle/status/configuration and discriminated runtime trace
contracts, monotonic system/manual clocks, recorded-note replay, JSONL and
memory trace sinks, and a hardware-independent output seam. Reworked the live
scheduler into mutable planning, immutable dispatch, and due-output horizons
with `output_advance_ms`, tempo retiming, exactly-once dispatch, skip/repeat
reset, `HOLD` cancellation, and latched `STOP` panic behavior while retaining
the original beat-window API for existing simulations. Added `LiveEngine` to
wire follower, tempo, section policy, scheduler, output, and traces causally.
Deterministic tests cover retiming/freeze boundaries, early output calibration,
skip/repeat, hold/stop, lifecycle validation, replay, and JSONL traces. The
runtime deliberately retains `score_beat` as a Bundle-v2 integration seam;
stream-backed Matchmaker and Yamaha MIDI rendering remain explicit hardware/
optional-dependency follow-ups.

## [2026-07-12] decision | Lifecycle and artifact model v2 foundation

Separated rehearsal take analysis state, performer disposition, and learned
profile membership into independent, validated axes. Documented and implemented
explicit transition tables for takes, durable background jobs, and rehearsal /
performance runs; performance runs freeze their profile revision on activation.
Added stable alignment candidate IDs and a non-destructive, idempotent v1
migration that writes v2 sidecars while runtime consumers transition. The
lifecycle layer depends only on a narrow bundle/timeline revision reference;
Score Bundle v2 remains the owner of the full manifest contract.

## [2026-07-12] implementation | Stream-backed FOLLOW and Yamaha output edge

Implemented the next causal runtime slice on top of the hardware-independent
engine: a bounded Matchmaker 0.3 `BytesMidiStream` adapter for live `pthmm`, a
fakeable mido accompaniment renderer with instrument program/volume setup,
timed note-offs, retrigger protection, and all-channel panic, plus backend
replay/FOLLOW lifecycle endpoints and typed `runtime:status` WebSocket events.
All runtime jobs claim the existing `LiveControl` hardware slot atomically, so
they cannot overlap Yamaha recording or playback. Corrected the runtime's
default confidence gate from zero to `0.5`; Matchmaker's confidence and lock
remain explicitly labeled local heuristics because the package exposes no
calibrated probability. Added deterministic no-sound replay, fake stream, fake
MIDI output, and hardware-exclusion tests; real Yamaha tests remain opt-in.

## [2026-07-12] implementation | Unified Movement 2 performer score

Replaced the cockpit's visible split—Movement 2 take capture beside a Movement
1 full-score overlay—with one coherent Movement 2 performer context. The
DVC-managed Joseffy two-piano reduction is now the central rehearsal score and
is served through the score PDF API via an explicit source registry. PDF.js
rendering no longer depends on measure geometry: before reviewed Audiveris or
manual anchors exist, all 15 pages remain readable and the cockpit identifies
highlighting as pending. Coverage remains available as a collapsed provisional
measure grid rather than being painted onto unverified locations. Added a
Ready / Live / After situation rail driven by existing capture/hardware state,
updated backend and Playwright coverage, and documented the DVC pull step.

## [2026-07-12] implementation | Bundle-identity live runtime projection

Removed client-supplied bundle and score paths from replay/FOLLOW APIs. Runtime
starts now resolve immutable Bundle v2 `bundle_id`/`revision` through a server
registry that discovers the repo-owned Movement 2 draft. Added a generic,
temporary MIDI projection that validates the declared follower-reference and
accompaniment files, derives legacy events and instrument routing from source
MIDI tracks/channels, and labels every event/status as noncanonical MIDI
performance coordinates and not performance-ready. This enables Movement 2
plumbing tests without repeating the invalid claim that expressive Oguri ticks
are canonical measure/beat time. The reviewed semantic timeline, mappings,
sections, and instrument artifact remain required before performance readiness.

## [2026-07-14] analysis + implementation | QUICKSTART terminal-touchpoint root causes

Redesigned [Quickstart](QUICKSTART.md) around one one-time terminal setup step
and an otherwise all-UI session flow (rubato#95), then classified every
remaining terminal touchpoint as either a **missing feature** (the UI
genuinely doesn't support the action) or **fragile setup** (the capability
exists but isn't surfaced/wrapped), tracing each to its actual cause rather
than treating them as uniform docs gaps:

- **rubato#96/#97 — Go Live has no UI.** `/api/runtime/follow/*` and its
  generated typed client were complete; `App.svelte` had zero references to
  either. While implementing the Go Live panel (PR #105), found `#97`'s
  premise wrong: the `runtime:status` WS event it asked for already existed
  server-side in `live_runtime.py`, throttled via `only_if_changed`, and
  already documented in
  [Realtime Performance Dataflow](concepts/realtime-performance-dataflow.md).
  Closed #97 as already-satisfied; #105 only needed the frontend
  subscription. Verified end-to-end against a real running server via
  `/api/runtime/replay/start` (no hardware/`matchmaker` needed) — watched an
  already-open browser tab transition live through a real WS-pushed run.
- **rubato#98 — swallowed MIDI diagnostic.** `midi_ports.list_midi_ports()`
  caught the `ImportError` from a missing `python-rtmidi`/`live` extra and
  returned the same empty `MidiPorts` as "backend fine, zero ports visible,"
  discarding the distinguishing signal before it reached the API. Added
  `backend_available: bool` threaded through to the Sound Check row (PR
  #102).
- **rubato#99 — unwrapped server startup.** `dev-server.sh` execed uvicorn in
  the foreground with no readiness signal and no `dvc pull`/browser-open
  wrapping. Backgrounded the server, added a `curl`-polled readiness wait
  against the existing `/` redirect route, auto-`open` on macOS, and a
  `--skip-dvc` flag (PR #103). Real terminal `SIGINT` (vs. `SIGTERM`,
  confirmed to propagate correctly) could not be fully verified from this
  sandboxed tool environment — a trivial isolated `trap ... INT` script
  didn't receive synthetic cross-process `kill -INT` either, indicating a
  harness limitation rather than a script defect; flagged for a real-terminal
  smoke test.
- **rubato#100 — no shutdown route.** `app.py` called `uvicorn.run("module:
  app", ...)`, a wrapper that builds and discards its own `Server` instance,
  so no request handler had a handle to trigger a graceful stop. Switched to
  an explicit `Config`/`Server` pair stashed on `app.state.uvicorn_server`,
  added `POST /api/server/shutdown` (503 without that handle), and a
  confirmation-gated **End session** control in the Library drawer rather
  than a stage-level button, given the misclick risk during rehearsal (PR
  #106). Verified against a real server process exiting after a simulated
  browser click, including the Cancel path doing nothing.
- **rubato#101 — no DVC-pull detection.** "PDF or MIDI artifact missing" was
  a troubleshooting-doc line with no in-app check. Added
  `paths.score_bundle_missing_artifacts()`, which walks a bundle directory's
  committed `*.dvc` pointers and reports any whose real file isn't on disk
  yet, exposed as `GET /api/scores/{movement}/health` and a Ready-face
  banner (PR #104). Verified against this repo's own never-pulled Movement 2
  bundle before pulling it for other testing.

Each fix's design decision is recorded in the concept/runbook doc it
touches — see
[PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md#current-implementation-movement-2-performer-cockpit-2026-07),
[Score Bundle v2 Contract](concepts/score-bundle-contract.md#dvc-presence-vs-bundle-readiness),
and [Development](DEVELOPMENT.md) — rather than only in PR descriptions.
At the time of this entry, `docs/QUICKSTART.md` used `[TODO: rubato#N]`
markers while each fix was under review. PRs #102–#106 have since merged; the
2026-07-16 documentation consistency pass removed those transitional markers
and rewrote the quickstart around the resulting single-command startup and
in-app controls. README.md's MIDI-extra note and its "experimental developer
control" line were also corrected alongside the original fixes.

## [2026-07-16] design + groom | Continuous take-to-accompanied-review workflow

Reconciled the Quickstart, product vision, PWA implementation concept, and
offline-render concept after direct pianist feedback that a terminal render
command breaks the rehearsal loop. The audit found a deeper implementation
boundary: take capture/background alignment are take-native, while offline
render was session-native and playback selected either solo or accompaniment,
so the recorded take and retimed orchestra could not be heard together as one
review.

Implemented the resulting contract as Stop → durable automatic alignment →
durable review preparation → one **Hear take + orchestra** action → Record
another take or experimental Go Live. A take-owned, idempotent `render_review`
job consumes `take.mid` plus authoritative `aligned.v2.json`, crops the
orchestra to the aligned score span, and writes normalized `solo.mid`, retimed
`accompaniment.mid`, and combined `ensemble.mid`. `TakeResponse.review`,
take-native prepare/status/MIDI/play routes, and `take:review_status` make the
workflow recoverable across refresh/restart. The persistent **After this
take** card reuses Sound Check output and exposes Yamaha review, browser
preview, solo-only playback, retry, Record another, and Go Live without
session/upload/run-ID/variant vocabulary. Documented honest placement, render,
and device failure states with in-app resolution or retry rather than terminal
escape hatches.

Also removed stale pending-merge guidance for merged PRs #102–#106. The
Quickstart now accurately says `./scripts/dev-server.sh` performs dependency
sync, DVC pull, conditional UI build, readiness wait, server start, and macOS
browser open; Go Live, End session, missing-artifact warnings, and empty-MIDI
diagnostics are described as present on current `main`. The combined
accompanied-review path is now documented as implemented in this branch while
retaining the warning that its expressive MIDI coordinates are provisional and
the Movement 2 bundle is not performance-ready.

Verified the full non-audio path against the existing Yamaha take
`data/processed/take-6ca80e42/solo.mid` in isolated application state. A
31.98-second prefix contained 97 note-ons, aligned at 0.8557 match rate (83
matched, 11 extra, 26 missing), and produced a 31.98-second combined
solo-plus-orchestra review artifact in `ready` state. This smoke test did not
open the Clavinova output or emit MIDI; performer listening remains the next
hardware-gated validation.

## [2026-07-16] design + implementation | Score-first rehearsal workstation

Direct rehearsal testing exposed that the implemented cockpit still treated
the score as a document above a vertical stack of controls. Starting a cued
take required scrolling the PDF out of view, and neither lead-in nor accompanied
review showed the current score location. A newly aligned take also left the
only visible percentage at 0% because that number represented the three-take
maturity target rather than whether any kept take had observed the passage.

Reframed the at-piano UI as a score-first workstation: the actual rendered PDF
page and rehearsal controls share one desktop viewport; sparse score-transport
anchors support a cursor during orchestral cue, accompanied review, and live
following; and each aligned take exposes a durable score span painted as a
separate latest-take overlay. Coverage now distinguishes nonzero **observed**
progress from mature **covered** progress. One kept take is amber/touched with
the default `n_target=3`; it is not falsely green, but it no longer looks like
nothing happened. Machine-draft coordinates remain explicitly non-canonical.
Post-capture copy now says the take is **in the bank** / **kept**, not “safe.”

Added a browser regression journey at a 1440×900 viewport that keeps the
rendered score page and controls simultaneously visible through lead-in,
recording, stop, alignment, and Yamaha review; proves the position cursor
advances during cue and review; requires a distinct latest-take span; requires
nonzero observed progress after alignment; and rejects the stale “safe” copy.

## [2026-07-16] implementation + verification | Real-time score cursor and click-to-rehearse

Follow-up Yamaha/PWA testing found three coupled errors in the score-first
workstation. The browser refreshed its clock every 100 ms but copied measure
identity from one side of a sparse anchor interval and drew at measure center,
so the cursor jumped every several seconds. The latest-take wash ended at the
last matched onset even when the recording continued through a sustain/rest.
Finally, exact PDF boxes were display-only; **From here** remained disabled.

Cursor interpolation now resolves every interpolated display beat against the
126-measure timeline and scans inside the exact Audiveris box, with a 750 ms
page preturn. Review transport collapses chord-order regressions and extends to
the take's real duration. On the real take `t20260717T033751Z-22a6`, independent
MIDI timing and rendered-score inspection agree that matched notes end at
measure 20 while Stop occurs 2.38 seconds later in measure 21; the performer
wash now ends at display beat 81.29 instead of truncating at the last attack.

Movement 2 now projects its declared solo-reference MIDI through the same
explicit machine map to mark 111 piano-active measures. Uncovered PDF boxes are
clickable, the next post-take gap is suggested, and `target_score_beat` drives
an inverted-map orchestra cue saved as `from_position`. Rebuilding the layout
from the pulled Audiveris `.omr` reproduced all 126 boxes across 15 pages with
an empty diff. Unit/API/browser regressions cover continuous x movement,
lookahead, performed-span extension, click selection, and cue payloads.

The final production smoke test also caught test-process leakage: delayed
materialization workers could finish after a per-test environment fixture and
write an empty profile into the real local take bank. A process-wide pytest
data/run root now keeps every asynchronous test task isolated. The real profile
was rebuilt from the aligned take and restored to 9 observed measures (8.11%).

## [2026-07-17] implementation + verification | Durable rehearsal diagnostics

The PR #110 review pass found that the live WebSocket feed was the only shared
timeline across hardware capture, take placement, review rendering, and
coverage. Events published without a connected browser disappeared, while
hardware worker failures retained only a short status message and lost their
traceback after the terminal closed.

Added an ordered local JSON Lines journal at
`data/logs/rehearsal-events.jsonl`. Every typed lifecycle publication is now
journaled before socket delivery with UTC time, event ID, original payload,
and available take/session/run/job correlation. Hardware, alignment,
resolution, review, profile, and coverage worker failures add their full local
traceback to the same timeline. Logging is deliberately best-effort so a disk
or journal failure cannot interrupt MIDI recording, playback, or analysis;
durable take/job/run artifacts remain authoritative.

The same review pass fixed score-beat gaps selecting the previous measure,
anchor interpolation retaining stale measure metadata when geometry is absent,
and secondary capture actions remaining enabled during lead-in/recording.
Regression tests cover all three UI cases plus offline event and exception
journaling.

## [2026-07-17] implementation + verification | Score selection and opening cursor calibration

PWA rehearsal found that a slightly moving click on an amber measure could
select the PDF canvas as a replaced browser element. Chromium's native blue
selection paint covered the rendered score while the SVG rectangles remained
visible, making the PDF appear to disappear. The score canvas is now
non-draggable and the complete canvas/SVG surface disables native selection;
the Playwright regression uses an actual down/move/up gesture before confirming
the rendered page remains present and an ordinary click still selects measure
12.

The same rehearsal showed the review cursor one bar behind immediately after
the first piano entrance. The alignment timing itself was consistent: the
take's first matched onset and review MIDI both begin at 6.729 seconds. The
error was the provisional display projection, which interpolated from the
first solo B directly to measure 21 and treated Joseffy's short measure-12
pickup like a full bar. Checked Oguri/Joseffy anchors now place native ticks
34,619, 35,374, and 39,488 at printed measures 12, 13, and 14. Boundary tests
cover the last instant of measure 12, the measure-13 downbeat and aligned top
note, and the independently corroborated measure-14 system. This initial local
calibration was superseded by the measure-anchored correction below after a
second Yamaha review exposed that 39,488 is measure 15, not measure 14.

## [2026-07-17] design + implementation | Self-explaining rehearsal score

PWA testing showed that the score-first layout still exposed internal state
rather than a readable musical interface: the control card named “Measure 24”
without putting measure numbers on the engraving, amber and gray layers had no
key, and the newest-take layer appeared as unexplained dotted boxes. Automatic
suggestions could also leave the PDF on a different page from the named target.

The score now carries unobtrusive `m.` labels over every detected measure and
plain-language badges for `Next take`, `Newest take`, and `Now`. A compact key
defines amber as in the bank, green as the repeated-take target, gray hatch as
needing a take, the dashed outline as the newest take, and the red line as the
current playback location. Latest-take geometry merges adjacent measures into
one continuous dashed span per system. The selected-passage card explicitly
says to look for `Next take`, uses a single `Rehearse m. N` action, and removes
the duplicate “From here” action. Idle auto-selection now opens the target's
PDF page. Playwright regressions assert the key, on-score measure/target badges,
automatic page correspondence, newest-take badge, and live `Now` marker.

## [2026-07-17] correction + verification | Measure-anchored review timeline

Follow-up Yamaha review proved the sparse opening calibration was still
musically wrong after its first anchor. The persisted alignment matched the
performed pitches and times, but the display projection stretched the source
interval from measure 13 to an incorrectly labeled later anchor. It put the
F-sharp at 13.992 seconds in mid-measure 13 and the final E-G-sharp-B attack at
49.253 seconds in measure 20, although the notation places those attacks on
the downbeats of measures 14 and 22.

The corrected projection now carries explicit downbeat anchors for every
measure from 12 through 23, corroborated against the saved Yamaha take, Oguri
solo pitches, Joseffy barlines, and a separately engraved reduction with
printed bar numbers. Coverage cache identity was bumped so the existing take
reprojects through measure 22. Yamaha and in-browser review now share the same
dense timing transport. Transport anchors journal their source tick, nearby
performed pitches, and whether they represent a matched onset or Stop.
Browser preview fetches that MIDI-derived transport only when the performer
starts a preview; take-bank refreshes remain metadata-only, and pitch
diagnostics use a binary-searched onset index rather than rescanning the take.
Playwright replays the G-sharp, F-sharp, and final chord at their exact take
timestamps, takes browser screenshots, and asserts measures 13, 14, and 22
without opening hardware.

## [2026-07-18] correction + implementation | Selected-passage intent and recoverable placement

A 142-second Yamaha take explicitly cued into measure 23 matched 603 of 731
performed notes (82.5%) through the phrase-ending onset, but appeared as
“couldn't place it.” The primary alignment was valid; a one-percent alternate
n-gram hypothesis had `start_position=-361`, and v2 sidecar validation crashed
while serializing that invalid diagnostic candidate. Localization now rejects
negative score starts, legacy migration defensively drops them, and retryable
software failures are no longer presented as musical placement failures.

Selected passage is now the durable recording intent. The deck says **Record
from m. N** and offers **Cue orchestra, then record** (with an explicit
automatic-recording/entry explanation) or **Record now, no lead-in**. Both
retain the selected location: cued takes use `cue`; uncued takes use the new
`placement_hint`. The aligner treats that location plus the cue-entry time as
an authoritative anchor, so captured orchestral lead-in notes cannot shift
the pianist's entry. Retryable failures expose an in-place retry and durable
events record the placement basis, target tick, match metrics, and failure.

The same take exposed a stale late-movement display anchor and trailing-time
extrapolation. The final matched onset at source beat 516.4 is the notated beat
one of measure 52. The projection now pins that phrase boundary directly, and
the take wash/cursor holds there through trailing sustain or silence rather
than inventing progress into unplayed measures. The retained take was retried
successfully and now projects from measure 23 to measure 52.

## [2026-07-18] correction + UX | Audible score origin and passage review

From-the-top Yamaha playback exposed a distinct opening-origin defect: the
Oguri file's native ticks 0–2016 contain setup and silence, while its first
audible orchestral event is Violin I E4 at tick 2017 (4.202083 seconds). The
Joseffy reduction engraves that E-natural on the downbeat of printed measure 1,
but the display projection previously declared source tick zero to be that
downbeat. The cursor therefore crossed measure 1 before sound began. Movement
metadata now records the first orchestral entry, normal playback trims that
technical preroll, and projection v5 maps tick 2017 to measure 1. A real-source
test verifies E4 is the first scheduled note at relative time zero while the
hardware transport begins at measure 1 beat one. Playback logs retain the
source offset, window, scheduled-event count, and first event time. This fixes
the opening origin; the separately checked local anchors still govern later
measure correspondence.

Take review is now score-positioned instead of being an undifferentiated play
button. Selecting an amber score passage or **Select on score** on a banked
take produces explicit **Take only** and **Take + orchestra** choices for both
Yamaha and browser output. The API selects `solo.mid` or `ensemble.mid`, maps
the selected display beat into take-relative time, and slices the same dense
transport used by the red cursor. The take bank exposes every recording's
measure span and profile membership and explains that repeated passes remain
separate recordings: their timing, dynamic, and pedal observations are fused
by the existing robust quality/recency-weighted median rather than mixed as
audio. Playwright now drives this selected-passage review journey end to end.

## [2026-07-18] correction + implementation | Unified review clock and pickup-aware cues

Yamaha rehearsal showed three symptoms with one timing-model boundary: banked
take buttons unexpectedly used the browser synth, accompanied-review audio was
aligned while its PDF cursor accumulated lag, and review artifacts retained
the several seconds between arming capture and the first played note. Banked
take actions now invoke backend MIDI playback and name Yamaha explicitly.
Browser audio remains available only under controls explicitly labeled as a
browser preview.

The raw take remains immutable. Review render revision
`trim-aligned-preroll-v1` crops only the derived solo at its first aligned
onset, subtracts the identical preroll from orchestra timing anchors and score
transport, and logs raw duration, trim, review duration, and anchor count.
Old review artifacts are invalidated by renderer revision and regenerated.

The accumulating cursor lag was not an audio-alignment error. Oguri encodes an
expressive performance in fixed-tempo ticks, while the PDF is a notated measure
grid; the prior m.23-to-m.52 interpolation therefore treated expressive time
as constant score tempo. Projection v6 pins every downbeat through m.53 using
Oguri onset groups, Joseffy barlines, and Audiveris page/system measure boxes.
The m.52 rolled bass and high attack share one printed downbeat.
Coverage cache identity advances with the projection so persisted overlays are
recomputed instead of retaining v5's sparse mid-movement measure assignment.

Finally, a clicked measure now identifies the passage but an orchestral cue
ends at the first piano onset inside it. For m.53 this is the B-natural pickup
at 270.29375 seconds, not the barline at 260.035 seconds; the preceding eight
seconds now contain the expected orchestral melody. Cue logs record the
selected tick, resolved source time, projected measure/beat, pitch, cue window,
and device routing. Deterministic MIDI tests require fresh orchestra attacks
late in that lead-in, and Playwright requires take-bank play to issue a Yamaha
`/review/play` request.

## [2026-07-18] research + experiment | Full Joseffy OMR and symbolic alignment survey

Ran Audiveris 5.11.0 beyond the structural GRID stage across the 15-page
Movement 2 reduction. The retained `.omr` contains note/chord geometry for all
126 measure stacks; 14 pages reached `PAGE`, while page 14 failed inside
Audiveris `LINKS`. The run exposed pervasive rhythm warnings, including an
invalid 35/16 duration for measure 17, so Audiveris semantics remain machine
evidence rather than canonical score truth.

Researched open-source MusicXML/MIDI alignment options and recorded
[Parangonar](sources/parangonar.md) as the most directly usable current Python
candidate, with Nakamura's Symbolic Music Alignment Tool as an independent
HMM baseline. Repository inspection found meaningful maintenance risk in
Parangonar 3.3.2, including no visible CI and an open pretrained-model
regression. Rubato tests also rejected its whole-file `SubPartMatcher` path.
However, `DualDTWNoteMatcher` matched 1,877 of 2,525 independently recognized
Piano I MusicXML note rows to the Oguri solo; measures 14-18 achieved 91-100%
coverage with highly monotonic note-x/performance-time correspondence. The
recommended bundle build is therefore anchored and corroborated alignment,
not adoption of a single package as authority.

## [2026-07-18] decision + implementation | Canonical beat evidence fusion

Accepted [Decision 0006](decisions/0006-canonical-beat-evidence-fusion.md):
canonical 960-PPQ `score_tick` is the only musical coordinate, while Audiveris
MusicXML/geometry, Oguri MIDI, and Yamaha take MIDI remain evidence sources.
Promoted the pages 1–13 Audiveris MXL to DVC and added a deterministic Movement
II builder. The resulting artifact contains 448 beat anchors through m.112,
PDF x coordinates, evidence/confidence, and explicit gaps for mm.113–126.

This closes the cursor/audio clock split. Take alignment remains MIDI-to-MIDI,
then persists canonical ticks plus diagnostic reference ticks. Review audio,
coverage, cursor transport, and PDF cursor x now consume that shared mapping.
The reported large rubato in m.17 is represented by four nonuniform reference
beat anchors rather than uniform bar interpolation; the B-natural pickups in
m.12 and m.53 are explicit piece annotations rather than generalized pitch
rules.

Added an exceptional in-app correction: freeze the audible source instant,
select the printed measure, and choose beat 1–4. Sparse corrections are
monotonicity-validated, revision the mapping identity, optionally requeue the
affected take, and append `score:alignment_corrected` to the rehearsal journal.
Unit/API/browser coverage includes MusicXML parsing, expressive beat recovery,
correction persistence, journal reconstruction, and the no-terminal fallback.

## [2026-07-19] migration | Removed layout 0/1 compatibility

Migrated Movement II's reproducible OMR layout artifact from schema v1 to v2.
Raw detected rectangles now use zero-based `box_index`; canonical timeline and
display-map records use zero-based `measure_index`; performer-facing numbering
comes only from `measure_label`. The beat-map builder and server now consume
the canonical display map rather than raw OMR layout, eliminating the legacy
normalizer and runtime `+1` geometry join. Bundle revision advanced to
`movement2-beat-fusion-v2`; the canonical display and performance beat maps
remained byte-identical.

## [2026-07-19] decision | Semantic score-anchored rehearsal cues

Reframed a lead-in as two canonical score anchors—cue start and pianist
entry—rather than a duration selected in seconds. A normal score click chooses
the entry; an explicit **Change on score** action disambiguates a second click
that moves the cue start. **Cue & Record** should continue the orchestra
through the entry until Stop while storing only the pianist's MIDI as the
take. At decision time, pre-entry-only playback remained an implementation gap;
the implementation entry below closes it.

Owning design: [Recording Flow Redesign](design/RECORDING_FLOW_REDESIGN.md#37-semantic-lead-in-span).

## [2026-07-19] implementation | Two-measure Cue & Record

Implemented the practical semantic cue rule: resolve the actual piano onset
inside the selected passage, start orchestra playback at the barline two
printed measures earlier (clamped at the movement start), and derive seconds
only for scheduling/countdown. Removed the lead-in duration control and API
input. The take now retains both cue-start and entry score anchors, while the
orchestra continues through the entry until Stop instead of dropping out. The
hardware transport now includes the fused beat anchors across that full window
so extending accompaniment does not degrade the score cursor to whole-span
linear interpolation.

## [2026-07-19] architecture correction | Symbolic identity precedes timing

Take 2 exposed a local but architectural failure around mm.45–51. The dense
historical table labeled native Oguri tick 107,926 as the m.46 downbeat even
though it is a mid-phrase onset; the Audiveris/Oguri pitch sequence places the
notated m.46 opening chord `[47, 63, 71]` at ticks 109,900–109,915. The cursor
therefore crossed m.46 about 4.4 performed seconds early while take/orchestra
audio remained aligned, then recovered at the separate verified m.52 anchor.

Amended [Decision 0006](decisions/0006-canonical-beat-evidence-fusion.md) so
ordered symbolic correspondence owns measure/beat inference and time is fitted
only afterward. Replaced the dense anchor override with sparse
performer-verified constraints, constraint-bounded sequence alignment, and
monotonic gap interpolation. The rebuilt mapping retains expressive m.17 and
the m.53 pickup while assigning mm.45–51 from pitch/chord evidence. Added
regressions that allow arbitrary rubato duration ratios and pin the m.46 chord
independently of elapsed time. Bundle revision
`movement2-symbolic-path-v3` and the declared derived-artifact hash identify
the new mapping so stale canonical take/coverage artifacts cannot masquerade as
current evidence.

## [2026-07-19] architecture + docs | Explicit symbolic inference lineage

The score-alignment architecture is now documented as a directed inference
chain: canonical notation establishes musical identity; constraint-bounded
symbolic matching joins score states to reference MIDI and each Yamaha take;
performed timestamps fit the rubato warp only after those identities are
known; Audiveris geometry paints the resulting canonical position on the PDF.
System Design now records what each source knows and cannot know, separates the
one-time bundle build from per-take inference, and uses the corrected m.46 chord
correspondence as a worked example. The Architecture Brief and Vision use the
same “identity first, timing second, pixels last” mental model.

Git provenance traced the superseded `MOVEMENT_2_ANCHORS` table to PR #109 on
2026-07-16. It began as five explicitly provisional, noncanonical display
calibration points, expanded during cursor debugging, and was inadvertently
promoted to `REVIEWED_DOWNBEAT` truth by the first fusion compiler. Decision
0006 now records that history and the obsolete assumptions so the project does
not recreate a dense timestamp lookup under a new name.

## [2026-07-19] architecture clarification | Three workflows, one coordinate

Corrected the architecture overview after the inference rewrite had collapsed
rehearsal and live performance into one runtime bucket. Rubato has three
workflows: offline score bundling creates piece knowledge; rehearsal aligns
completed takes and learns the interpretation profile; live performance uses
the prepared bundle and frozen profile in the causal accompaniment loop.
Rehearsal and live intentionally reuse runtime components but have different
lifecycle authority and latency constraints. Updated System Design, the
Architecture Brief, Vision, and the existing Three-Workflow Overview to show
both the cross-workflow dependency chain and the inference owned by each phase.

## [2026-07-19] rehearsal UX | Recordings filed by score location

Replaced the flat, globally numbered take bank with a score-first rehearsal
library. Recordings are grouped by the performer-selected entry measure, with
the aligned start as the fallback for legacy and free captures. Repeated
recordings become local passes within that passage; failed placement attempts
remain visible but do not inflate aligned-pass counts. The PDF now displays
the number of kept aligned passes crossing each measure, and both the library
and the contextual review navigate through that canonical score location.
This makes the visible information architecture match the performer's mental
model: piece → movement → place in the music → recordings.

## [2026-07-19] rehearsal UX | Compact passage retakes and visible cue intent

The selected score passage now owns one **Record pass** action with an adjacent
**Orchestra cue** switch, on by default. The control states the musical
contract in place: the orchestra begins two measures earlier, recording starts
automatically, and the pianist enters at the selected measure. This removes the
ambiguous pair of record-like buttons without hiding the uncued option.

Repeated recordings at one entry use a compact recording picker; full passage
rows, browser diagnostics, download, exclude, and dismiss controls are
progressively disclosed. This adapts the closed take-folder/take-lane patterns
documented by Logic Pro and Ableton while keeping Rubato's score passage—not a
DAW region—as the primary object. An interrupted zero-note attempt now stays
filed at its intended passage and offers the same cue-enabled retry instead of
advancing the user elsewhere or presenting an unexplained alignment failure.

## [2026-07-19] rehearsal evidence + cue control | Repeatability, pace, handoff

Analyzed the three real measure-12 passes as overlapping symbolic evidence.
They share 93 quality-qualified timing cells, yield a 65.9 BPM median pace and
2.7% typical tempo MAD, and provide 2.39× the usable evidence of one pass. The
variance report correctly isolates the expressive m.17–18 region instead of
flattening it. Added the same robust support/quality/consistency summary to the
passage UI.

Root-caused the distracting measure-23 recording: cue playback was a fixed
transport, not a score follower, and drifted to a median 7.66-beat position
error. Cue & Record now scales from the global Orchestra pace, hands off at the
selected solo entry, and allows only outstanding note releases afterward.
Continued accompaniment remains the live follower's responsibility. Passage
recordings now use one compact picker and accessible icon toolbar; raw MIDI is
saved automatically instead of occupying the performance UI as a download.

## [2026-07-19] architecture + migration | Canonical performance-profile v2

Replaced the ambiguous float-beat rehearsal profile with a strict canonical
schema: 960-PPQ integer `score_tick`, 480-tick cells, explicit
`seconds_per_quarter`, and a dimensionless per-take `rubato_ratio`. The fold now
reads canonical `aligned.v2.json` only; it no longer reads legacy
`aligned.json`. Absolute pace and local phrase dilation are therefore separate
signals, and all fused cells retain robust median/MAD, support, and quality.

Root-caused a real source-of-truth violation: the prior profile could be keyed
on the expressive reference MIDI while cursor/review code used canonical score
time. Applied the one-time migration to all seven persisted Movement II
alignments and rebuilt `profile.json` from raw take MIDI plus canonical timing
maps. Recovery-only `*.pre-performance-v2.json` backups were written; runtime
code has no old-profile fallback. A second dry run found zero remaining work.

Reanalysis of the three measure-12 passes now finds 63 shared canonical cells,
a 62.4 BPM median baseline, 2.8% typical normalized-rubato MAD, and 2.37× the
one-pass evidence. Updated System Design, Vision, the Architecture Brief, ML
Strategy, and source notes to distinguish the deterministic rehearsal model
from live HMM/OLTW score following and to compare it with ACCompanion, nASAP,
Matchmaker 2025, and HeurMiT.

## [2026-07-19] rehearsal UX | Explain alignment evidence and score actions

Analyzed the third real measure-23 pass (`t20260720T023701Z-3ccb`): it aligned
unambiguously across roughly mm. 23–51 with 584 matched notes and an 80.7%
score-note match. The three entry-m.23 passes share 226 canonical half-beat
observations, yield a 60.9 BPM learned pace, 2.4% typical tempo MAD, 66.4%
learning confidence, and 2.45× the usable evidence of one pass.

Made that evidence legible in the rehearsal surface. Passage-local recording
options now use 12-hour local timestamps, pass numbers, qualitative alignment
fit, and note-match percentages; the selected pass explains matched, extra,
and missing notes and whether placement was ambiguous. Amber now explicitly
names whether the weakest half-beat misses the three-observation target, the
50% average alignment-quality floor, or both; green means every half-beat meets
both targets, and another pass is optional. A right-click score
menu offers record, latest-take playback, accompanied playback, and passage
details at the clicked measure. API and Playwright coverage own these claims.

## 2026-07-25 — Predictive FOLLOW clock and full-screen cursor

Diagnosed the FOLLOW orchestra-simultaneity lag from the timing path rather than
a captured trace (none on disk yet). The current FOLLOW clock is already
ACCompanion `L`-equivalent (full phase re-anchor + EMA period), and the
scheduler already times orchestra events along the reference rubato when a
`reference_beat` is present. The gap: the live Matchmaker follower emits
`reference_beat=None`, so live FOLLOW falls back to flat extrapolation and the
whole reference shape goes dark — orchestra rushes ritardandos, drags
accelerandos.

Added the predictive FOLLOW clock (`ReferenceAnticipatingTempoModel`) as the
LTE reduction: back-fill the missing `reference_beat` by projecting `score_beat`
through the bundle's score→reference map, then let `OnlineTempoModel`'s existing
reference-period EMA and the scheduler reproduce the reference's local shape at
the live pace. Selected by `RuntimeConfig.follow_clock`; defaults to
`"reactive"` so the shipped clock is unchanged until validated on hardware. A
run opts in with `"follow_clock": "predictive"`. Deterministic ritardando test
shows the predictive clock lands an inter-onset orchestra note on the pianist
(0.75 s) while reactive fires early (0.625 s). See
[Decision 0008](decisions/0008-predictive-follow-clock.md).

Also fixed the full-screen score cursor: the SVG overlay sized to the scroll
viewport (`height:100%` of a `max-height`/`overflow-y` wrap) instead of the
canvas, so in performance mode the cursor compressed onto the top (piano)
staves. Gave the canvas + overlay their own canvas-sized wrapper and left the
scroll on the outer element, so the cursor spans the full system (all parts) in
both views. Clarified in the response that the ~0 ms calibration is a legitimate
result (device round-trip ≈ human negative-mean-asynchrony), not the source of
the FOLLOW lag.

## 2026-07-26 — Rehearsal-as-dataset lifecycle; fit-quality evaluation refactor

Consulted a first-principles design pass (music + ML best practices) on the
rehearsal→train→perform loop. Adopted the frame in
[Decision 0009](decisions/0009-rehearsal-as-dataset-lifecycle.md): rehearsal is
dataset curation, the fitted per-cell model is the **Interpretation**, training
is a background materialization (not a phase), and per-passage **Readiness**
metrics are the curation compass ("ready" = held-out error near the consistency
floor = more takes won't help). Working reference:
[Rehearsal Model Workflow](concepts/rehearsal-model-workflow.md).

Refactor to match, correcting a real flaw: the offline evaluator had
re-implemented a *different* fold (unweighted) than the runtime's weighted,
recency-aware `fold_takes`, so it scored a phantom model. Extracted one fitting
implementation `profile.fold_cells`; `fold_takes` -> `fit_interpretation_for`
(thin store wrapper); rewrote `takes/tempo_expectation.py` as `TempoPrior`
(runtime read-view) + `evaluate_interpretation` -> `Readiness`, fitting through
`fold_cells` so metrics grade the real model. Honest re-run of the measure-44
passage: onset ~38 ms vs reactive ~76 ms (skill +0.50), verdict "learning".
Renames: `ProfileDoc` -> `Interpretation` (alias kept), `_included_takes` ->
`selects`; removed unused `ProfileMembership.QUARANTINED`. Full suite green
(457 passed; two pre-existing DVC-gated Playwright e2e failures untouched).

## 2026-07-26 — Adversarial review fixes (PR #124)

Acted on an adversarial architecture review. Deleted the experimental
Interpretation tempo prior from `LteTempoModel`: the review showed its offline
"it's worse" result was invalid, caused by the wiring setting `reference_beat=None`
and thereby stripping the scheduler's reference warp -- an unfair test, and
committed speculative generality. Retracted that conclusion in Decision 0009.
Made the "one fold" claim true: `analyze_passage` now folds through the single
`fold_cells` implementation. Completed the rename (no aliases): `ProfileDoc` ->
`Interpretation`, `fold_takes` -> `fit_interpretation_for`, `_included_takes` ->
`selects`. Documented the evaluator honestly as a 1-step proxy (not closed-loop),
bounded by take count and alignment quality, and recorded the two accepted next
architectural steps -- a curve-aware scheduler and a deterministic closed-loop
harness -- as deferred work rather than band-aids. Also fixed a CI blocker: a
stray `npm install` had desynced `webapp/package-lock.json`; restored the
in-sync lock and regenerated the OpenAPI/SDK contract.

## 2026-07-29 — Cockpit simplification, visible beat clock, and two cue-in root causes

Root-caused two live failures from a single take, then rebuilt the surface
around them (Decision 0014).

**The orchestra went silent at the m.22 interlude.** `_accompaniment_mode_for_passage`
judged the whole take by the support at the *cue start tick*. the soloist started the
cue at m.8 — an orchestral lead-in where the piano is tacet, so solo support
there is 0 and always will be. Every warm-up-then-enter take was therefore
condemned to cold start: the orchestra trailed off at the first note and stayed
silent, including passages with plenty of evidence (m.17 support 7, m.22 support
6). This is the same category error Decision 0012 names for coverage — demanding
evidence where none can exist. Now the mode is chosen from the best support over
a 16-measure window ahead, so m.8 resolves to `accompanied`.

**The cursor drifted ahead.** The score cursor preferred an open-loop projection
(wall clock × fixed `tempo_scale`) over the follower's estimate, so it ran as a
metronome regardless of the performer's rubato and the gap accumulated. Authority
is now chosen by what is actually sounding; the readout labels dead reckoning as
`projected`.

**"Beat 5" was real.** `(beat_in_measure + 1).toFixed(1)` rendered 3.999 as
"5.0" for one frame before the barline. Floored now.

Surface: score full-width and first, everything else behind a collapsed
inspection panel; masthead carries state + measure + beat + elapsed; the active
system scrolls into view in every mode (not only the performance view); anything
that sounds pulls the score back on screen. Retired the "score and controls share
the viewport" test contract and the prose above the engraving.

Found and fixed a self-inflicted bug along the way: the scroll effect read and
wrote `scoreWasSounding` inside one reactive block, making the statement depend
on its own assignment — a reactive loop that froze the cursor mid-playback. Edge
detection now lives in a plain function outside the reactive graph.

542 backend tests and 23 frontend tests pass.

## 2026-07-29 — Follower redesign: expectation before evidence (Decision 0015)

the soloist, after we had spent several sessions asking "is there a follower here?":

> "A follower should always be able to be in existence. Its functionality just
> needs to be able to handle this state and distinguish between insufficient data
> compared to expected amount of data, vs intentional silence as written by the
> music itself."

Redesigned the follower around that. The follower is a predict–correct estimator
that always has a position; performer input is a correction signal, not a
prerequisite. Expectation comes from the score (`solo_reference.mid` for per-cell
onset counts, `sections.json` for authority regions); evidence is judged
*relative* to expectation, so zero-expected/zero-observed is a complete match
rather than a gap. TACET, UNREHEARSED and DISAGREEING become three distinct
states instead of one `support == 0`.

Verified ground truth along the way: the piano's first note of Movement II is at
canonical beat 47.0 — the soloist's B-natural pickup — and `sections.json`
`opening-orchestra-lead` ends at exactly beat 47.0. The section map was already
right; nothing consumes it except the live engine (`coverage.py`, `live_midi.py`
and `routes.py` reference it zero times).

Two corrections to my own earlier work recorded in the decision: the
16-measure-lookahead in `_accompaniment_mode_for_passage` produced the right
answer for m.9 by borrowing confidence from downstream bars — the same
one-global-mode-per-take error in the opposite direction; and I reported that the
piano plays at m.3-12 after bucketing PPQ-240 reference ticks against PPQ-960
canonical bounds. Mixed units, plausible-looking nonsense. Step 1 of the
implementation is the expectation model *with unit-pinning tests* for that reason.

## 2026-07-29 — Decision 0015 implemented through step 3

`expectation.py` derives per-cell expected onsets, role and authority from the
bundle's canonical solo events, so nothing crosses the PPQ-240 reference /
PPQ-960 canonical seam. Unit-pinning tests assert the known bars because that
seam fails silently rather than loudly.

Found a bug in the decision doc while implementing it: the recorded formula
`1 - observed / max(expected, 1)` returns 1.0 for nothing-expected-nothing-observed
-- a total miss for a passage played exactly as written. Corrected in code and doc.

Two design corrections during implementation:

- The handback must test the **predicted** beat, not the last corrected one. In
  FOLLOW the position only advances when notes arrive, and the performer's last
  note lands *before* a tacet passage -- that is what makes it tacet -- so a check
  against the corrected position could never fire.
- It must test region **density**, not literal silence. m.22 holds two solo notes,
  so a TACET requirement would not have fired there either. Density separates the
  cases with an eight-fold margin (interludes <= 0.5 onsets/beat, solo passages
  > 4.2).

Also found that FOLLOW never re-checked the section map at all, so an authored
LEAD interlude was reached only after a coast timeout -- opening with "Tracker
uncertain" for a passage the performer had played exactly as written.

Readiness: solo-led now means a solo line. m.12, 22, 52, 53, 104 reclassify as
orchestra-led, matching `sections.json` from an independent derivation.

Validated against real recorded takes: four takes ending at the m.22 interlude
reach LEAD with no dropout; a take stopped mid-phrase at m.17 does register one.
Same silence, opposite handling, decided by the score.

Steps 4 (retire the deaf cue path) and 5 (state the plan in the UI) are specified
in issue #154. 558 backend + 23 frontend tests pass.


## [2026-08-01] Spatial Zone Architecture | Lock-Free Shared Memory, GCC-PHAT & Sequence Flushing

Superseded as current guidance by the 2026-08-03 correction below; retained as
the chronological record of the earlier proposal.

Incorporated adversarial architecture review feedback into [Decision 0016](decisions/0016-pedalboard-decoupled-spatial-synth.md) and [Psychoacoustic Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md):
- Specified lock-free shared memory ring buffers (`multiprocessing.shared_memory`) for `MultiprocessSynthZone` to eliminate Python GIL and IPC queue serialization overhead.
- Added sequence invalidation (`FlushSequence(seq_id)`) and DSP gain wipe on `panic` to resolve `cancel_pending` race conditions during score follower re-anchoring.
- Adopted GCC-PHAT (Generalized Cross-Correlation with Phase Transform) for `rubato calibrate-zones` to withstand living room reverberation and multipath reflections.
- Added dynamic feature-based ornament classification (Inter-Onset Interval $\text{IOI} < 150\text{ ms}$) to route fast trills and runs to the anchor zone (0ms) rather than high-$\tau$ remote audio zones.
- Defined explicit mid-performance degradation fallback to collapse routing back to anchor synthesis if Bluetooth transport jitter spikes.

## [2026-08-03] Spatial design correction | Score-authored commitment windows

Reworked [Decision 0016](decisions/0016-pedalboard-decoupled-spatial-synth.md),
[Score-Authored Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md), and
the owning system/control docs around the soloist's intended workflow: he designates
exact musical regions where a swell or spatial layer may be committed 250 ms or
more ahead.

Separated raw zone latency, compensated residual timing error, musical residual
tolerance, and runtime prediction horizon. Early commitment is now
action-specific and policy-gated rather than a global scheduler advance.
Routing resolves before commitment; swells use phrase/stem or zone-bus gain
envelopes instead of per-note CC11 fades; existing `authority_generation`
replaces the proposed duplicate `seq_id`; downstream FIFO tails remain explicit
and unrecallable.

Removed unvalidated fixed timing/IOI thresholds, exact-cancellation claims, and
the preselected lock-free shared-memory implementation. Added primary-source
notes for compound-sound timing and Pedalboard, and gated live orchestral hosting
on calibration, virtual/MIDI routing, listening, and plugin feasibility tests.

## [2026-08-03] Mix UX boundary | Separate authoring modality

the soloist clarified that current PWA score annotations are data-correction controls
for tracking and playback, while spatial swells are a different authoring task.
Added [Mix Authoring Mode](design/MIX_AUTHORING_MODE.md) and reconciled the
vision, PWA, system, and Decision 0016 contracts.

Mix authoring explicitly replaces the rehearsal score interaction layer: it
hides amber/green coverage, latest-take spans, reactive anchors, alignment
corrections, and their pointer targets. It reuses PDF geometry, local context
menus, drag affordances, iconography, and durable Undo with a distinct
click/shift-click region grammar, zone/stem lanes, envelope handles, commitment
presets, and deterministic audition.

The durable `MixProgram` is independent from the immutable bundle,
`anchors.json`, human alignment corrections, takes, coverage, and the learned
Interpretation. A selected program compiles with revisioned zone/calibration
configuration into an immutable per-run `MixPolicy`; live performance never
consumes partially edited browser state.

## [2026-08-03] BBCSO audio-zone spike | Score-addressed pre-render succeeds

Implemented the first bounded Pedalboard/BBCSO path for Joseffy m.43--45. The
CLI resolves printed measures through the canonical Oguri projection, isolates
one patch-compatible MIDI track (`Violini I` by default), renders it through
BBCSO VST3, writes a WAV, enumerates named CoreAudio outputs, and can play the
WAV to one explicit device. An optional audition envelope leaves the m.44
remote layer dark for 250 ms after each local attack and swells after the m.45
downbeat.

The real local spike loaded BBCSO 1.12.14 under Pedalboard 0.9.24 and rendered
19 attacks/101 events to 17.127 seconds of non-silent stereo 48 kHz audio (peak
0.174). It also found the architectural edges: BBCSO aborts a restricted host
during JUCE/macOS application registration but succeeds in a normal GUI
session; its Audio Unit wrapper did not scan; Spitfire `.zpreset` files are not
accepted by the VST3 preset API; and the short offline render was slower than
real time. Added one-time editor/raw-state capture and documented the workflow
in [BBCSO Secondary Audio-Zone Audition](runbooks/bbcso-audio-zone.md). This
validates isolated authoring/pre-render use, not live incremental hosting.

## [2026-08-03] Spatial design simplification | Direct-HDMI low-latency baseline

the soloist selected the LG flagship soundbar's direct-HDMI Game path as the room
output and accepted 59 ms as the MVP planning assumption. Reframed
[Decision 0016](decisions/0016-pedalboard-decoupled-spatial-synth.md) and
[Calibrated Low-Latency Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md)
around zones that fit the ordinary 100 ms dispatch boundary. The 59 ms value
remains a planning assumption until the complete BBCSO/CoreAudio/HDMI/acoustic
path is measured; evidence and model-name caveats live in
[LG S95-Series HDMI Latency](sources/lg-s95-series-hdmi-latency.md).

Removed commitment permission, per-region residual budgets, and confidence
basis from the primary `MixProgram`. The new contract stores a piece-wide
Yamaha/soundbar base blend plus sparse score-aligned route envelopes. Timing
and calibration belong to `ZoneConfig`; a zone that exceeds the normal horizon
is not live-eligible rather than widening the score contract. Rare m.44
Yamaha-note-triggered attacks remain separate reactive scheduler cues.

The spatial objective is unchanged and clearer: Yamaha anchors the piano-side
image, the soundbar supplies a broad center, and future named zones or proven
HDMI channel maps may place a solo oboe across the room. A quiet Yamaha
orchestral floor provides continuity and graceful fallback while spatial
regions shape attention, depth, and antiphonal effects.

## [2026-08-03] Mix control semantics | Conventional 0--100 volume

the soloist clarified that the PWA should use ordinary `0..100` volume sliders rather
than expose dB. Updated the `MixProgram` contract so route defaults and envelope
points persist that same user-facing scale, with `0` off and `100` the route's
calibrated full level. A pinned mapping revision translates the authored value
into internal audio gain or Yamaha CC7 without making renderer details part of
the authoring task.

macOS remains at full/unity. Physical Yamaha and soundbar dials establish the
room-reference loudness during setup and remain outside the mix plan.

## [2026-08-03] Mix authoring MVP | Durable spatial program and runtime policy

Implemented the separate score-centered Mix workspace, validated `MixProgram`
artifact, atomic revision-checked API, durable Undo, named zone readiness, and
immutable runtime compilation described by the prior design pass. The PWA now
hides rehearsal overlays and pointer targets, exposes 0--100 Yamaha/soundbar
base controls, authors score ranges with drag or Shift-extension, and creates,
edits, bypasses, and deletes explicit route envelopes. New Movement-II programs
seed the agreed m.44 room-tail and m.45 swell example.

The live Yamaha route evaluates the compiled score policy into CC7 without
flattening MIDI velocity; the generic BBCSO render applies the same policy at a
200 Hz control rate for local WAV/HDMI audition. The soundbar stays
`needs_calibration` and falls back to Yamaha for live policy until the physical
bench test supplies a calibration revision. Added model/store/API, MIDI/audio,
generated-client, Svelte type, and Chromium persistence/modality tests.

## 2026-08-03 — Audiveris page-14 crash root-caused; GUI-free .omr repair proven

Movement 2's Audiveris transcription stopped exporting at m.113, leaving
m.114-126 with no recognized notes. The working theory (untagged 16th-note
triplets breaking the rhythm engine) was **wrong**: the triplets only produce
`INFO` time-inconsistencies that earlier sheets export straight through.

Root cause: on sheet 14 (m.118, staff 9) Audiveris double-recognized one
notehead (heads `#3708`/`#6784`, identical bounds) and built a phantom chord
`#6783` over a zero-length stem `#6428` at `(0,0)`. The `(0,0)` chord belongs to
no measure, so `Measure.getClefBefore(null)` throws and aborts the sheet at
`PAGE`; sheet 15's "check time signatures" errors were entirely a cascade
(`Time value not yet available in sheet#14`). One spurious glyph, whole tail
lost — an Audiveris bug, not a bad engraving.

Proven repair, fully GUI-free: transcribe with `-save -swap` (so the `.omr`
retains per-sheet SIG), delete the phantom chord + zero-length stem + their five
relations from `sheet#14.xml`, then re-run `-step PAGE -export` on the edited
`.omr` **without** `-force` (resumes at `LINKS`, keeps the edit; `-force`
re-detects the duplicate). Yields a clean 126-measure MusicXML (parts P1-P4).

Recorded the general defect + procedure in the Score Localization skill and the
piece-specific instance in `docs/sources/joseffy-reduction.md`. Not yet
integrated into the bundle: rebuilding the beat map from the 126-measure export
to give m.114-126 real note-anchored geometry is the follow-on.

## [2026-08-04] PR #158 review hardening | Continuous automation and recoverable history

Validated Antigravity's architectural review against the implemented runtime.
Moved Yamaha mix evaluation off note-on polling and onto the forward transport
clock, with quantized CC7 suppression and exact-part-to-orchestra fallback.
Added explicit artistic Undo parents, recoverable stale-score status plus
review/rebind, inter-process program locks, half-open region bounds, and
order-independent fallback aggregation with same-zone/stem overlap rejection.

The PWA now uses engraving beat knots without depending on rehearsal coverage,
offers exact stem/part authoring, and resolves bed/feature/custom/swell/fade
presets to distinct explicit envelopes. Kept `volume-linear-v1` unchanged:
assuming a universal square Yamaha CC7 acoustic law would bypass the project's
measurement gate, so any perceptual remap remains a new calibration-backed,
pinned revision rather than an unverified silent behavior change.

## 2026-08-04 — Audiveris tutti repair actually landed; beat map rebuilt; matcher-choice bug found

Follow-on to the Aug 3 entry above. That repair was proven but not promoted —
its own commit message said so explicitly — and the regenerated MusicXML from
the proof run went to a scratch path that was never kept. Verified this by
searching `git log --all` for the `.dvc` pointer across every branch: it had
been touched exactly once, before the repair existed. Genuinely nowhere, not
just hard to find.

Re-ran the full pipeline end to end and this time committed the result:
`run_audiveris.py --transcribe` (fails at `PAGE` on sheet 14 exactly as
documented, but persists the `.omr` via `-save -swap`), then
`repair_audiveris_omr.py` — same phantom chord (`#6783`/stem `#6428`) as
before. Promoted the resulting 126-measure MusicXML into
`source/joseffy_reduction_movement2.audiveris.mxl` (DVC), updated
`bundle.yaml` (`page_scope: [1, 15]`, new sha256; also fixed
`display_map_machine`'s sha256, which was already stale before this and
unrelated to today's work), then rebuilt `performance_beat_map.machine.json`
via `build_movement2_beat_map.py`: 448 anchors (mm.1-112) -> 500 (mm.1-125,
only m.126 unmapped).

**Verification, not just regeneration.** Two independent checks before
trusting any of it:
- The irregular-measures set for mm.1-113 is byte-identical between the old
  and new export (same 63 measures — a much larger set than an earlier
  reading of `docs/sources/joseffy-reduction.md` implied, since that doc only
  quoted the tail of a truncated check). Confirms the fresh transcription
  reproduced the existing recognition rather than introducing a regression;
  the only new irregular measures are in the newly-recovered mm.114-124.
- The new mm.113-125 anchors were cross-validated against
  `orchestra_oguri_alignment.machine.json` (a MuseScore orchestral score
  aligned to Oguri with Parangonar — independent of the Audiveris pipeline
  entirely) to a median of well under 0.2s, as tight as the pre-existing
  mm.1-112 agreement. That's real confirmation per the score-localization
  skill's cardinal rule, not self-consistency.
- Corrected a standing misconception along the way: mm.113-126 was described
  as a piano-tacet "closing tutti" in prior docs. It isn't — the recovered
  data shows the piano solo playing 14-52 notes/measure through m.125. The
  "no notes" description was the OMR crash's absence of recognition, not the
  music. Fixed in `docs/concepts/score-coordinate-systems.md` and
  `docs/sources/joseffy-reduction.md`.
- 176 relevant tests pass (score fusion/health/anchor-chain/cadenza,
  server API, capture flow, live runtime, take lifecycle).

**Separate bug found while verifying with a third, independent alignment
path.** Pointed Parangonar directly at the Audiveris MXL (not the usual
MuseScore ground-truth) using `scripts/align_score_to_performance.py`, which
hardcodes `AutomaticNoteMatcher`. On the OMR-noisy input it drifted 60-85s by
m.90 — not a real defect, the matcher just lost the thread with no global
constraint to catch it. `DualDTWNoteMatcher` on the identical input held
0.04s median disagreement with the beat map, matching the clean-MuseScore
case, despite a *lower* raw match rate (63% vs 68%) — match rate alone is not
a reliable proxy for alignment quality here. Also hit two API gotchas getting
this far: `partitura.Score.note_array()` rejects parts with differing
`<divisions>` (legal MusicXML that Audiveris emits per-measure; fixed by
concatenating per-part arrays instead), and `DualDTWNoteMatcher` needs
`include_grace_notes=True` note arrays (an `is_grace` field). Fixed the CLI:
now defaults to `DualDTWNoteMatcher`, exposes `--matcher`, and handles both
gotchas — it can now be pointed at an Audiveris export directly instead of
needing a bespoke one-off script. Also added the transitively-required
`pandas` to the `analysis` extra; the documented CLI invocation crashed with
`ModuleNotFoundError` without it.

Full writeup: `docs/sources/parangonar.md` ("Matcher Choice Matters on OMR
Input"), `docs/sources/joseffy-reduction.md` ("August 3-4"),
`docs/concepts/score-coordinate-systems.md` (tacet correction).

**Method lesson**, restated because it cost real time twice in one session:
a "proven" repair with an unpromoted artifact is not a landed fix — check
`git log --all -- <path>.dvc` before trusting that a documented repair is
actually in effect, not just the prose describing it.
## [2026-08-04] Score workspace redesign | Perform, Data, Mixing

Replaced the separate rehearsal/integrity navigation, **Author mix** button,
and oversized intent-card launcher with one persistent, mutually exclusive
**Perform · Data · Mixing** masthead switch. The engraved score and inferred
MusicXML measure labels now persist in every workspace. Data alone owns
amber/green coverage, pass counts, reactive anchors, and correction actions;
Mixing alone owns editable mix automation; Perform shows only a minimalist,
read-only preview of the prepared mix plan.

Replaced translucent mix-region bands with thin notation-like automation
brackets, gesture glyphs, and distinct hollow/filled endpoints. Selection is a
brass dashed outline rather than an unexplained purple fill. The piece-wide
Yamaha and room levels collapsed into one summary with a small stacked popover.
A draft or right-clicked automation opens a centered compact context editor
with stacked start/end `0..100` sliders and explicit interpolation copy. Visual
browser QA caught and fixed both 0--100/0--1 envelope normalization and
duplicate zero-width barline marks; Chromium tests now cover mode boundaries,
persistent measure labels, durable region editing, and the read-only Perform
overlay.

## [2026-08-04] Mixing UX simplification | Layers and shared beat markers

Reordered the persistent workspace switch to **Data · Mix · Perform** and
removed the separate Base mix surface. The existing global Orchestra volume is
now the sole performer-facing master for the spatial program; calibrated route
defaults remain internal configuration rather than duplicate score chrome.

Added per-workspace Data and Mix visibility toggles. Perform begins with a clean
score and both overlays off, but either may be enabled read-only without changing
the active interaction grammar. Mix automation is now a prominent horizontal
bracket in a dedicated lane above the notation, with hollow/filled downward
triangles at its endpoints. Data anchors use the same simplified downward beat
marker. Right-clicking an empty beat in Mix can attach a start point or complete
an ending point before opening the compact envelope editor.

## [2026-08-04] Mix-aware causal load harness and applied-gain observer

Extended the existing real-time load harness rather than creating a parallel
mix-only runner. Recorded Yamaha input traces can now run through live FOLLOW
with a pinned mix revision, an in-process software MIDI sink, and a deterministic
unit-signal room renderer. The scorecard checks that mix samples exist, the
observer remains off-path, the fake signal equals the effective gain, and an
authored automation actually changes a route.

Added optional `off` / `counters` / `trace` telemetry. In trace mode the live
VST render process publishes score ticks, route/master/effective gains, and
output RMS/peak through lock-free numeric gauges; a parent observer serializes
`mix_state` rows. Fake-VST block tests now prove that a 50% compiled route turns
a unit signal into exact 0.5 samples. The integration test also exposed and
fixed initial global orchestra volume not being propagated to room VST zones.

After rebasing onto the promoted m.113–125 score repair, the full suite exposed
two stale frozen expectations on main. Updated the inferred reference-quarter
tempo to the rebuilt correspondence map and coverage from 96 to 109 solo-led
measures; only m.126 remains unknown.

## [2026-08-09] BBCSO full-orchestra live audition | Cold-start timeout isolated

Captured and mapped the nine sounding Oguri Movement-II sections to individual
BBCSO Discover Long states, with exact MIDI track matching so `Violini I` no
longer also selects `Violini II`. Added offline full-orchestra rendering and a
live CoreAudio audition command. The first offline implementation reloaded all
nine instances and took over ten minutes for a 16-second cue.

Timing isolated the cause: Pedalboard's 120-second initialization grace period
was consumed almost exactly (121.035 seconds), while a one-second grace returned
the same working BBCSO instance in 1.955 seconds and applied raw state in 0.098
seconds. The first 512-frame render cost 1006.6 ms, but warmed steady-state
blocks measured 0.023 ms median and 0.046 ms p95. BBCSO initialization proved
unsafe under thread/process concurrency and unsafe beyond two short-timeout
instances in one host, so the working spike creates one isolated process per
instrument sequentially, warms each, and holds all at a shared playback barrier.

The known-good run loaded nine patches in 31.4 seconds, streamed Oguri m.43--45
for 16.1 seconds through MacBook Air Speakers, dispatched 52 note-ons, and
exited cleanly. the soloist confirmed hearing a convincing full orchestra with multiple
instrument families. This proves correct mapping and deadline-safe warmed
processing; integration as a resident rehearsal service plus dense-tutti,
long-run, and acoustic-latency qualification remain follow-ons.

The connected Yamaha subsequently enumerated as `Clavinova` for CoreMIDI input,
CoreMIDI output, and CoreAudio output, while MacBook Air Speakers remained the
default audio route. The development launcher now retains the `audio` extra in
addition to `dev` and `live`, so restarting the UX no longer uninstalls
Pedalboard. The PWA rehearsal flow sees and selects the Clavinova MIDI ports;
the isolated full-BBCSO audition remains a CLI/hardware-spike path rather than
a PWA action.

## [2026-08-09] Perform score selection | Native-blue canvas guard

Fixed a real-Chrome failure where clicking a printed measure could leave the
entire PDF canvas covered by the browser's native blue selection highlight,
making the engraving appear blank while Rubato's measure labels remained. The
existing `user-select: none` CSS was not sufficient: Perform mode's measure
`pointerdown` returned before preventing Chrome's default selection behavior.

Left-button measure and strip pointer-downs now suppress the native default and
clear any stale browser range before Rubato handles selection; Mix-mode pointer
capture and ordinary measure clicks remain intact. The Chromium regression now
constructs a stale range, clicks the printed measure, verifies the selection is
collapsed, and confirms the PDF canvas stays rendered. Svelte checks, 38 web
unit tests, the production build, and the focused end-to-end test passed; a
post-build visual check selected m.18 with the engraving still visible.

## [2026-08-09] Data timing validation | Clock-locked orchestral audition

Restored the intended performer validation loop for the Oguri-to-canonical
beat map. Data → Timing now plays four count-in clicks followed by one exact
Oguri orchestral measure with clicks at the hypothesized beats through the
selected MIDI output. The orchestra and metronome are scheduled together by
the backend rather than combining hardware MIDI with browser oscillator audio;
the soloist confirmed the first real Clavinova audition sounded correct.

The global Controls surface now groups Orchestra and Metronome as the two
listening levels, is available in Data as a compact panel, and retains the full
rehearsal controls in Perform. Metronome defaults to 30%, persists locally, and
also controls the latency-calibration click. Focused MIDI/API tests cover the
shared clock, click scaling/mute, and managed hardware boundary.

## [2026-08-09] Score localization | Repair orchestra-led mm.103–105 boundary

the soloist heard the purported m.104 downbeat on the orchestral G-sharp printed as
m.103 beat 4. Note-level inspection confirmed Violins I/II G-sharp at native
tick `252985` / `527.052s`, followed by the full m.104 downbeat chord at
`529.454s`; two independent symbolic alignments also placed m.104 near
`529.45–529.67s`, while the production map's `526.910s` anchor had zero matched
notes and interpolation-only evidence.

Fixed the compiler's evidence routing: Piano I → solo stays authoritative where
it produces a downbeat, while Piano II → orchestra now fills only absent later
downbeats and supplies interior beat timing in those orchestra-led measures.
The rebuilt map places m.104 at `529.510s` and m.105 at `536.275s`, clearing
both from the independent-disagreement worklist. Stored the soloist's m.103 beat-4
identity as a reviewed machine-local correction at exact tick `252985`.

Data → Timing now permits opening any measure directly and offers a collapsed
boundary correction. It groups rolled orchestral note-ons into attacks and asks
“Which sound begins printed m.N?”; selecting one persists the group's exact
native tick. This keeps the human task at musical identity rather than requiring
an awkward early/late offset description.

## [2026-08-09] Data timing validation | Score-led selection and m.109 pulse

the soloist found that m.109's measure boundaries were right while the metronome fired
three compressed clicks and left a long tail. The old interior anchors came
from Piano I's rapid run (`555.879`, `556.985`, `557.896`, `558.688s`), not the
orchestral quarter-note pulse. Piano II and grouped Oguri orchestra attacks put
the four printed beats near `555.879`, `557.392`, `558.488`, and `559.579s`.
The beat-map compiler now promotes reviewed m.109 to reduction → orchestra
interior timing, guarded by independent downbeat agreement within half a
quarter note. It does not silently generalize that human verdict to other bars;
the displaced m.54 reduction candidate remains excluded.

The Data timing workflow now follows one navigation grammar. Clicking a measure
in either the global strip or the printed PDF immediately loads that measure's
four-count audition. The duplicate number field, Open action, refresh icon, and
side-panel measure list were removed. Timing review temporarily owns the score
overlay: amber means **needs listening** because independent alignments disagree,
an outline means **spot check**, green means heard and confirmed this session,
and neutral means unflagged but still auditionable. Rehearsal coverage, newest
take decoration, reactive anchors, and their vocabulary stay out of this mode.

Beat correction no longer exposes “wrong note?”, floating-point timestamps, or
candidate deltas. A proportional ruler shows four numbered metronome markers
and lettered grouped orchestral attacks in heard order. The performer selects a
printed beat and then its orchestral attack; Rubato stores the attack's exact
native MIDI tick, leaving rhythmic subdivisions visible but unassigned.

## [2026-08-09] Score localization | Apply repeated-theme pulse fix to m.107

the soloist confirmed that mm.105 and 109 sounded right, then heard m.107 reproduce
the same three-fast-clicks/long-tail failure previously diagnosed at m.109.
The root cause was the same evidence-routing error: three exact Piano I matches
inside a dense solo run compressed the supposed orchestral beats into the first
half of a steady four-beat bar.

Promoted performer-reviewed m.107 to the existing guarded Piano II rhythm path.
The compiler rejected the reduction's collapsed symbolic warp and placed the
four clicks evenly between independently agreed barlines at MIDI ticks
`262133`, `262692`, `263252`, and `263811`. The generated artifact diff changes
only m.107; all other measures, including the already-correct mm.105 and 109,
remain byte-identical.

## [2026-08-09] Data timing UX | Visible targets and race-free local Play

the soloist confirmed m.107's corrected pulse, then exposed three workflow failures:
the Timing panel described review counts without visibly updating the global
strip, a completed audible pass could show a raw managed-job 409 when its loop
restarted during MIDI teardown, and right-clicking a printed measure could
reproduce Chromium's full-canvas blue selection.

The missing strip marks were a real reactive dependency bug: worklist state
arrived asynchronously, but the score helper closed over it so Svelte did not
know to repaint. Review state is now explicit input to strip and score geometry,
with a sticky high-contrast movement strip and a compact row of exact measure
targets. The Timing panel is reduced to Alignment, two short facet labels,
measure/tempo, **Play**, opt-in loop, correction affordances, and **Sounds
right**. Right-click opens a one-action Play menu and shares the native-selection
guard. Loop playback polls real hardware-idle state before retrying, absorbing
the harmless same-audition teardown race instead of surfacing HTTP internals.

The focused real-Chromium test now asserts all of these human interactions,
including a simulated 409 followed by successful retry. This fills the exact
coverage gap: prior tests checked left-click selection and text presence, but
did not test right-button pointerdown, asynchronous review-mark repainting, or
managed-job teardown timing.

## [2026-08-09] Data timing UX | Omit orchestra-tacet listening targets

the soloist identified that m.17's accompaniment is entirely rests, so its independent
alignment disagreement could not be judged through the orchestra-only timing
audition. The listening worklist now retains that disagreement in machine
diagnostics while filtering every orchestra-tacet measure from both the
needs-listening set and the sparse spot-check spine. This also removes silent
m.16 from the review row.

Follower continuity remains independent of accompaniment note demand: the live
follower projects the solo-reference position through the canonical beat map.
The existing Movement 2 regression covers all four observed beats of m.17 and
the transition to the verified m.18 downbeat, so the tacet bar is traversed
rather than skipped.

## [2026-08-09] Full live-run review | reactive ownership, pulse repair, and final release

Analyzed the complete scratch run `live-1786330629261` from its runtime JSONL
and automatically captured solo MIDI. The run produced 2,394 follower updates
and 1,230 orchestral note-ons; device onset lateness was 3.09 ms median, 24.15
ms p95, and 55.28 ms max, with no 100 ms misses. The capture is intentionally
ephemeral until the soloist chooses to keep it as a rehearsal take.

The trace separated four causes that sounded superficially alike:

- m.32 had no follower jump or premature device release. Most adjacent string
  attacks overlapped; the coarse articulation came from the PWA's Yamaha GM/XG
  fallback. The nine-patch BBCSO renderer remains a separate warmed audition
  service, not the normal PWA performance renderer.
- m.35 and m.87 used solo-derived interior beat evidence that compressed or
  delayed the orchestral quarter pulse. Their reviewed Piano-II/orchestra pulse
  now owns the four interior anchors. m.93 received the same repair, with the
  reviewed m.94 resolution downbeat at native Oguri tick 223,861. m.38's actual
  attacks were within roughly -121..+77 ms of the accepted live beat and its
  score map was left unchanged.
- In m.44, reactive beats 1 and 4 fired, but ordinary prediction had already
  begun beats 2 and 3 just before the bass attacks. Marked chords now have
  exclusive reactive ownership, and checked-in m.44/m.45 plus m.93/m.94 cues
  survive an empty local anchor store. Regression tests cover the exact
  predictive-versus-reactive race.
- The final low E strings released about 2.6 seconds before the pianist. Their
  Movement-II projection now sustains to canonical beat 504 (end of m.126) and
  follows active-release retiming.

The run crossed slightly past beat 504, causing mix automation and then failure
status projection to throw on tick 483,840; this explained the stale ACTIVE UI
after the performance. Both paths now clamp to the final valid tick. The focused
suite covers corrected pulse landmarks, durable reactive cues, exclusive chord
ownership, final sustain, and terminal-state clamping.

## [2026-08-10] Live-run forensics | One command joins the latest take

The full-run investigation exposed an avoidable discovery cost: a live take's
runtime journal, cursor journal, automatically captured Yamaha MIDI, and
ephemeral marker lived in two state-root subtrees and had to be correlated by
hand. `scripts/analyze_live_trace.py` now defaults to the latest actual
`live-*` run, accepts `--run-id`, and reports that artifact join alongside its
existing diagnostics. Auditions and replays are excluded from latest-take
selection.

Added a concise operational runbook and repo-local `live-run-forensics` skill
so future agents follow the same evidence order, distinguish scheduler intent
from adapter delivery, and preserve the rule that scratch captures are not
rehearsal data until the performer explicitly keeps them.

## [2026-08-10] Full live-run review | source-clock opening and exact m.44 cues

Analyzed scratch run `live-1786391346736` by joining its runtime/cursor traces
and automatically captured solo MIDI. The output adapter was healthy: note-on
lateness was 2.83 ms median, 22.42 ms p95, and 56.09 ms max, with no 100 ms
misses. The capture remains ephemeral unless the soloist explicitly promotes it.

The mm.3 and 8 lurch was upstream of MIDI output and did not involve the
follower: no piano notes or authority transitions occurred there. The
`ORCHESTRA_ENTRY` clock discarded Oguri's source coordinate and scheduled notes
at their projected canonical positions, allowing mapping irregularities to
reshape an otherwise autonomous orchestra passage. Orchestra-led playback now
keeps a uniform source-performance clock, calibrated once across the opening
section to the global quarter-note tempo. It preserves Oguri's relative timing
and ignores the rehearsal pace profile until the pianist takes FOLLOW authority.

The m.44 backend was not wholly silent, but its authored reactive contract was
wrong: source-derived ticks armed treble pitch 71 for the first chord and
provided no usable fifth cue at the m.45 downbeat. Replacing them with the exact
canonical beats 172–176 arms bass pitches 40, 39, 32, 31, and 30, matching the
already-successful m.93→94 pattern. A runtime diagnostic surface and regression
now assert all ten durable cues. The same run also ended at score beat
`505.000125...`; the engine now treats final-barline follower/coast overshoot as
normal completion rather than asking the section map for an out-of-range beat.

## [2026-08-11] Live rehearsal review | dropout panic, ritardando, and the mix footgun

Analyzed three live takes into m.44-45 (`live-1786415621122`, cue-in
`performance-20260811T033756Z-0f59`, live `live-1786419930240`) plus the m.53-63
and m.105 regions. Three distinct, independently-confirmed defects, all now
fixed with deterministic regressions.

**1. Follower dropout panicked the orchestra.** Every reported symptom — the
m.45 downbeat cutting out, the m.53-63 gaps/non-legato, the m.105 chromatic
desync — was one mechanism: a follower coast-expiry flipped the section to HOLD,
and entering HOLD fired an all-notes-off (`section_hold`) that silenced the
orchestra until relock. Two dropout signatures fed it. **Type A (input
starvation):** the coast window was a fixed 1500 ms, but into the m.45 climax the
pianist decelerated to ~27 BPM where one beat lasts >2 s, so a single beat of
notated ritardando tripped a false dropout on the very downbeat it was expanding
toward. **Type B (confidence loss while playing):** in dense/chromatic writing
the matcher sat at 0.35-0.49 (just under its 0.5 lock) while notes kept arriving;
the timer keyed on the last *confident* position, so it expired even though the
pianist never stopped, then could not relock for seconds. Fix: the coast budget
is now tempo-relative (`follower_coast_beats` at the current beat period, clamped
by `follower_coast_ms`/`follower_coast_max_ms`); a dropout additionally requires
genuine input silence (a fresh onset means the pianist is playing, never a
dropout); a dropout hold is *gentle* (`hold_panic=False`) so ringing chords
release on their own note-offs; and during genuine silence where sound is due the
coast decelerates by `follower_coast_slowdown` (~20%) so the orchestra eases into
the wait instead of marching ahead. Aligns the code with Decision 0015's
"unsure != stopped" contract.

**2. The reactive triggers fired — the mix muted them (live mode only).** The
first take's "I heard nothing in m.44" was not the engine: all 27 anchor-owned
chord notes fired reactively on the bass octaves, identical MIDI/velocity/latency
to the audible m.93 take. The difference was CC7. The auto-seeded starter region
`m44_room_tails` authored level-0 dips on every trigger beat for the `room_center`
zone; that zone is uncalibrated, so it falls back to `yamaha_anchor` and the duck
rode onto the Clavinova, driving orchestra channel volume to 0 exactly on beats
172-175. It applied only in live runs because only that path loaded the mix
program. Fix: deleted the region from `main`, stopped `_starter_regions` seeding
demo ducks, and added a mix floor — `audible_gain_for_part_at(..., floor=...)`
plus `min_audible_orchestra_gain` — so routing gaps or fallbacks can never fully
silence the audible orchestra. Intentional silence remains the separate global
orchestra mute.

**3. Rehearse and live were one runtime with a latent split.** They differed
only in whether the mix was applied (the source of #2's mode-dependence).
Unified behind a single `mix_enabled` switch and one `_resolve_mix_policy`
helper used by every entry point; a stale client revision pin is now logged
rather than aborting the run. Rehearse-from-a-bar and perform-from-the-top now
differ only by start measure.

## [2026-08-11] Performance must never block | advisory readiness + shared DVC cache

Live performance is the floor capability and nothing may gate it on data quality
or mix state. Two blocks were removed. **Frontend:** the score-artifact readiness
signal (`missingScoreArtifacts`) hard-disabled Go Live and showed a blocking
banner; it is now advisory only, and the real gates are just the physical MIDI
input/output. **Backend:** `LiveRuntimeManager._resolve_mix_policy` now degrades
to no mix on *any* failure (missing program, stale score identity, malformed
region) instead of aborting the run — a spatial refinement can never abort a
performance.

Root cause of the "N score artifacts haven't finished setting up" alarm: git
worktrees each have a separate, near-empty `.dvc/cache` and `git worktree add`
does not `dvc checkout`, so DVC-tracked score *sources* were unmaterialized in
the worktree (the derived artifacts the runtime performs from were present, which
is why rehearsals worked). Fixed with a single user-global shared DVC cache
(`dvc config --global cache.dir`) inherited by every worktree; `dvc checkout`
then materializes from it instantly with no re-download. Version control is
unaffected — identity is the md5 in the git-tracked `.dvc` file; the cache is a
content-addressed pool. See [the runbook](runbooks/dvc-worktrees-shared-cache.md).

## [2026-08-14] Live renderer migration | REAPER owns audio

Replaced the preferred live Pedalboard/BBCSO host with an experimental REAPER
boundary after the final hardware trace localized roughly 470--500 ms of delay
to Rubato's four-process block queues, mixer, prefill, and blocking audio writer;
BBCSO's measured plug-in work itself remained below 1 ms per block. GarageBand
over the same LG HDMI route was interactively responsive, further isolating the
custom host rather than the soundbar.

Rubato now exposes one `Rubato Orchestra` CoreMIDI virtual source, retains the
four collapsed ensemble mappings on channels 1--4, and reuses the proven MIDI
deadline/note-off/CC7/panic path. REAPER owns BBCSO, mixing, CoreAudio, and the
LG output. A deferred Lua bridge creates/repairs the four tracks and publishes
a fresh readiness heartbeat with exact project, MIDI input, FX, audio-engine,
device, buffer, sample-rate, and latency facts. The PWA labels and readiness
messages now describe REAPER rather than invisible Python workers. Pedalboard
is retained for offline rendering, diagnostics, and rollback.

Crash-report follow-up separated two unrelated popups. The 2026-08-13 Python
segfault was BBCSO executing inside `pedalboard_native`; the REAPER boundary
removes that plug-in and its audio work threads from Python entirely. The
2026-08-14 REAPER abort was a second GUI process failing in macOS
AppKit/HIServices registration before any project or VST frame; the original
REAPER process stayed alive. The runbook now forbids command-line bridge
reinjection while REAPER is open and uses the Actions window for bridge reruns.

The first successful automated bridge launch exposed a semantic readiness gap:
all four BBCSO FX were enabled and online, but the RPP serialized each instance
with BBCSO's `<empty/>` payload. The bridge now treats that marker as
`setup_required`, opens each unconfigured FX in sequence, and advances only
after the plug-in writes non-empty patch state. “Ready” can no longer mean four
silent, patchless plug-ins.

REAPER bridge startup is now automatic. The machine setup installs the bridge
under REAPER's resource `Scripts/Rubato` directory and a guarded
`Scripts/__startup.lua`, using REAPER's native startup-script convention. It
refuses to overwrite a pre-existing non-Rubato startup script. The manual
Actions registration remains a recovery path, not the normal rehearsal path.

The REAPER resident route and new PWA sessions now default to 20% rather than
75%. The LG HDMI output cannot be attenuated through macOS, so a high implicit
default was unsafe; an existing browser-saved level remains explicit operator
intent.

Low-latency configuration is now enforced rather than advisory. REAPER had
48 kHz/512 written in preferences but both CoreAudio request flags were off,
while the project itself pinned 44.1 kHz; the live engine therefore reported
44.1 kHz/512 and 662 output-latency samples (~15.0 ms). Machine-local settings
and the generated project now request 48 kHz/128, which REAPER accepted at 278
samples (~5.8 ms). Renderer readiness fails closed if the live rate differs
from 48 kHz or the block exceeds 128 samples.

The first room-only ten-second test exposed a separate false-ready state:
Rubato dispatched and released every event, but the soloist heard nothing because
REAPER listed `Rubato Orchestra` with Input disabled. Readiness now sends a
non-sounding channel-16 CC119 probe and requires the bridge to observe it via
`MIDI_GetRecentInputEvent`. The heartbeat also publishes MIDI receipt and
track/master peaks. Mix audition now honors the PWA Sound level, accepts live
fader changes, leases the resident renderer, and supports REAPER-only output
without an orchestral MIDI copy to Yamaha. the soloist enabled both cached same-name
MIDI rows; the remaining handoff task is one fresh audible verification.

## [2026-08-14] Ingress-probe false negative | Duplicate same-name MIDI rows

The forced retry still failed the ingress probe *after* both `Rubato Orchestra`
Input rows were enabled (`reaper.ini` `midiins=3`/`midiins_all=3`), so a disabled
preference bit no longer explained it. Root cause found in
`reaper-midihw.ini`'s `[mididevcache]`: REAPER holds **two** input rows both
named `Rubato Orchestra` (indices 0 and 1) plus Clavinova at index 2. Because
Rubato's virtual source is process-owned, every backend restart registers a
*new* CoreMIDI endpoint while the previous same-name row lingers. The Lua
bridge's `midi_input_index()` returned the first present name match, which can be
a stale ghost row; it then (a) filtered `MIDI_GetRecentInputEvent` on that dead
device index so the live probe never matched — a false negative, REAPER *was*
consuming the source — and (b) armed the four tracks to that dead index, so even
a "ready" state would have produced no audio. The failed preload confirmed this:
it reached bridge-level `ready` and emitted the four init CC7 volumes, then
failed only `_verify_midi_ingress`.

Fix: the bridge now enumerates *all* same-name candidate rows, latches the live
device index from the first event actually received on any candidate (rather than
guessing by name order), binds the four tracks and the probe filter to that
latched index, and logs the candidate set, the recent-input device-index census,
and which index received input. This self-heals across endpoint churn and does
not weaken readiness — it strengthens track binding to the proven-live endpoint.
The Python probe now re-sends the channel-16 CC119 across its timeout window so a
single shot cannot race REAPER's endpoint binding, and its failure message points
at the device-cache duplicate and `Reset all MIDI devices`. Unit tests cover the
resend and the strict never-observed failure. The bridge reload and the audible
10-second verification remain pending because REAPER-GUI automation was declined;
the reload is the only manual step left before the API-driven audition can run.

## [2026-08-14] AUDIBLE — CoreMIDI unique-ID churn was the real blocker

With the fixed bridge reloaded, live diagnostics exposed a second, deeper cause
under the duplicate-row one. The bridge logged `candidates: 0/true,1/false`
(index 0 present) yet REAPER's global input history stayed empty and REAPER
raised **"Error opening devices: Rubato Orchestra"** — REAPER enumerated the
source but never opened it for input, so no probe and no notes were consumed.

Two layered faults:

1. **Duplicate enabled rows.** REAPER's `reaper-midihw.ini` held two same-name
   `Rubato Orchestra` input rows, both Input-enabled. REAPER tried to open both
   against one endpoint and failed. Fixed by disabling Input on the stale
   `<not found>` ghost row (ID 1) in Preferences → Audio → MIDI Inputs; the
   fixed bridge additionally tolerates duplicates by latching the live index
   from received events.
2. **CoreMIDI unique-ID churn.** `mido`/`python-rtmidi` create the virtual
   source with a *fresh* CoreMIDI `kMIDIPropertyUniqueID` every open (confirmed:
   1815082742 → 2114376863). REAPER keys enabled input rows by unique ID, so the
   enabled row was always bound to a stale ID and reported `<not found>` /
   "could not be opened" for the current source — MIDI was never consumed.
   Fixed by pinning a constant unique ID (`0x52424F31`, "RBO1") on the source
   immediately after creation via a best-effort CoreMIDI `ctypes` call in
   `live_reaper._pin_coremidi_unique_id`. One `Reset all MIDI devices` then bound
   the enabled REAPER row to that stable ID; every later launch matches it.

After both fixes the resident preload reached `ready` and a fresh forced preload
reached `ready` again with **no manual REAPER interaction**, confirming
durability. The room-only 10-second audition
(`mix-audition-1786772630406031000`, `output_name=""`, ticks 0–11520, 76 BPM,
0.35) was **audible — the soloist confirmed hearing it**. Trace: 27 note-ons, 25
note-offs, 2 terminal panics, `notes_left_sounding: []` (the two uneven notes
released by the terminal all-notes-off), note-on lateness median 3.25 ms / p95
5.0 ms, note-off median 3.9 ms / p95 5.1 ms. REAPER heartbeat saw the MIDI and
`peak_since_start` rose above zero. Device: 48 kHz, 128-sample block (2.67 ms);
REAPER-reported output latency read 278→534 samples (~5.8–11 ms) across resets.
The end-to-end MIDI→audible acoustic latency (BBCSO + soundbar HDMI tail) is not
yet measured; the loopback `POST /api/runtime/calibrate/latency` remains the way
to get it. Probe timeout default raised 1.5 s → 6 s to absorb REAPER's
source-open latency; unit tests cover the unique-ID helper and constant.

## [2026-08-14] Oguri->BBCSO range remap prepared (9 sections)

The 4-track BBCSO collapse routed double basses onto the Cellos patch (notes
below C2 dropped) and flutes/bassoons onto Clarinets (wrong timbre), so the lower
staff was thin/absent. Checked every Oguri part's full-movement pitch range
against the BBCSO Discover manual: with one dedicated patch per section (Violins
I/II, Violas, Cellos, Basses, Horns, Flutes, Clarinets, Bassoons) *every* note
fits its patch — zero out-of-range notes. All nine patch states were already
captured under `plugin-states/`. Software prepared on `claude/oguri-bbcso-range-
remap`: the bridge now builds nine channel-filtered tracks, and
`scripts/apply_reaper_9section_config.py` rewrites the room_center zone to nine
sections (validated; router maps each part to its own channel). The live cutover
(apply config, reinstall bridge, load nine patches in REAPER, verify memory/
audibility) is staged and does not touch the `Live-v1.0` snapshot until run.
