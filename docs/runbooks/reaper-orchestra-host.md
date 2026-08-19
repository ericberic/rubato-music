# REAPER orchestra host

> ⚠️ **ALPHA — the living-room / dual-output mix does not work yet.**
>
> The REAPER/BBCSO living-room path (surround room feed, dual Yamaha+room output,
> per-section balance, latency compensation) is **experimental and not
> performance-ready**. Known blockers as of this writing:
> - **Uncompensated output latency ~120–150 ms.** REAPER itself is fast (~8.5 ms;
>   BBCSO PDC 0 ms), but the TV → eARC → soundbar (surround DSP) chain adds the
>   rest, and `output_advance` is not yet calibrated to cancel it — so the
>   orchestra plays audibly late through the room.
> - **Latency calibration unfinished.** The acoustic mic measurement is
>   unreliable on legato material; the "flam against the Clavinova anchor" and
>   per-zone advance (room vs Yamaha) approach is designed but not dialed in.
> - **Dual-output balance (Yamaha + room) is still being tuned.**
>
> **The supported live-performance path is the orchestra through the Clavinova's
> own synth** — a single MIDI output, near-zero latency. That path is unaffected
> by this alpha work: the room/REAPER layer is opt-in (it only runs when the
> REAPER renderer is enabled). Keep it off for real playing until this notice is
> lifted.

This is the current experimental path for BBCSO live accompaniment. REAPER
renders audio; Rubato sends only scheduled MIDI and observes readiness.

## One-time machine setup

From the active Rubato worktree:

```bash
uv run rubato configure-live-reaper-zone
```

This preserves the existing four-group score routing, creates the machine-local
project at `~/Library/Application Support/Rubato/reaper/Rubato Orchestra.RPP`,
and changes `room_center` from `pedalboard` to `reaper`. It deliberately resets
output advance to zero until the new path is measured.

Open the generated project in REAPER. In **Preferences → Audio → Device**:

- choose CoreAudio;
- choose the connected LG soundbar output;
- use 48 kHz;
- start with a 128-sample request block size.

In **Preferences → Audio → MIDI Inputs**, enable **Input** (not Control) for
exactly **one** `Rubato Orchestra` row and **disable** any duplicate same-name
`<not found>` row. Two enabled same-name rows make REAPER fail to open the input
entirely ("Error opening devices"), so no MIDI is consumed. Rubato pins a stable
CoreMIDI unique ID (`0x52424F31`) on its virtual source so a single enabled row
stays valid across backend restarts; without that pin macOS assigns a new unique
ID each launch and REAPER's remembered row goes `<not found>`. After the source
is first present, run **Reset all MIDI devices** once so REAPER binds the enabled
row to the pinned ID. The dots under Input/All are the durable setting;
`<not found>` only means the backend is not currently holding the source open.

Start Rubato or open its PWA with **LG soundbar · REAPER/BBCSO** and **Keep
REAPER ready** enabled. That creates the virtual CoreMIDI source `Rubato
Orchestra`.

The setup command installs the bridge into REAPER's standard Scripts folder.
It also installs a guarded `Scripts/__startup.lua`, so later REAPER launches
start the bridge automatically. The installer refuses to replace an unrelated
existing startup script.
In REAPER press `?` to open **Actions**, choose **New action... → Load
ReaScript...**, and load:

```text
~/Library/Application Support/REAPER/Scripts/Rubato/rubato_reaper_bridge.lua
```

Run it. The bridge creates and monitors four channel-filtered tracks, adds one
BBCSO VST3 instrument to each, detects BBCSO's unconfigured `<empty/>` state,
and opens each unconfigured plug-in in sequence. Choose these patches and save
the REAPER project:

1. `Rubato | Violins` → Violins 1 Long
2. `Rubato | Low strings` → Cellos Long
3. `Rubato | Woodwinds` → Clarinets Long
4. `Rubato | Horns` → Horns Long

The old Pedalboard raw-state files cannot be inserted directly into a REAPER
project, so this patch selection is intentionally manual once. Subsequent
launches load REAPER's saved project state.

### Nine-section mapping (range-correct)

The original four patches collapse the orchestra so lower-staff instruments play
the wrong patch: **double basses land on the Cellos patch** (their lowest notes
fall below the cello's C2 and vanish) and **flutes/bassoons land on Clarinets**.
BBCSO Discover ships a dedicated patch for every section this reduction needs,
and all nine states are already captured under `plugin-states/`. Every part's
full-movement pitch range fits its natural patch (verified against the Discover
manual), so nothing falls out of range.

Cutover (does not endanger the `Live-v1.0` tag — revert by re-running the old
setup):

1. `uv run python scripts/apply_reaper_9section_config.py --apply` (rewrites the
   `room_center` zone to nine sections).
2. Reinstall the bridge (`uv run rubato configure-live-reaper-zone`, or copy
   `reaper/rubato_reaper_bridge.lua` to the Scripts folder) — it now creates nine
   channel-filtered tracks.
3. Re-run the bridge in REAPER and load one Discover patch per prompted track:

   | Track | Discover patch | Oguri part |
   | --- | --- | --- |
   | Rubato \| Violins I | 1st Violins Long | Violini I |
   | Rubato \| Violins II | 2nd Violins Long | Violini II |
   | Rubato \| Violas | Violas Long | Viole |
   | Rubato \| Cellos | Cellos Long | Violoncelli |
   | Rubato \| Basses | Basses Long | Contrabassi |
   | Rubato \| Horns | Horns Long | Corni |
   | Rubato \| Flutes | Flutes Long | Flauti |
   | Rubato \| Clarinets | Clarinets Long | Clarinetti |
   | Rubato \| Bassoons | Bassoons Long | Fagotti |

4. Verify: nine tracks Ready, no CoreAudio underruns/memory pressure with nine
   Discover instances, and the lower staff (basses, bassoons) is audible.

On macOS, do not invoke the `REAPER` executable a second time with the Lua file
as a command-line argument while REAPER is already open. In the first local
setup that second GUI process aborted during AppKit application registration;
the already-running REAPER process, project, bridge, and plug-ins were
unaffected. Load and rerun the bridge through REAPER's **Actions** window.

## Ready means ready

The PWA becomes **Orchestra Ready** only when all of these are true:

- the ReaScript heartbeat is fresh;
- REAPER has the exact Rubato project open;
- `Rubato Orchestra` is present and assigned to channels 1--4;
- all four tracks are armed and input-monitored;
- each track has an enabled, online BBCSO instrument;
- each BBCSO instance has non-empty patch state;
- REAPER's audio engine is running; and
- REAPER reports the configured LG audio output;
- the engine is actually running at 48 kHz; and
- the active CoreAudio block is no larger than 128 samples; and
- REAPER observes Rubato's silent channel-16 CC119 MIDI-ingress probe.

The last check prevents a false Ready state when a device name is enumerated
but its REAPER Input permission is disabled. The heartbeat also reports the
latest Rubato MIDI event and track/master peaks, separating MIDI dispatch,
REAPER receipt, BBCSO output, and master output.

The heartbeat lives at
`~/Library/Application Support/Rubato/reaper/renderer-status.json` and includes
the audio device, sample rate, block size, output-latency samples, and track
counts. A stale heartbeat fails closed.

## First sound experiment

New sessions and the resident REAPER route start at 20% because HDMI output
volume is not adjustable from macOS on this setup. A previously saved browser
level remains explicit user intent. Mix audition receives that same PWA Sound
percentage and accepts fader changes while it runs. Then:

1. Move the volume fader and confirm already-sounding BBCSO notes change level;
   Rubato sends CC7 on all four channels.
2. Audition ten seconds of the orchestral introduction.
3. Confirm releases remain distinct and no note cloud accumulates.
4. Play Yamaha MIDI live and compare the follower cursor with the audible
   orchestra attack.
5. Record the run ID and use the ordinary live-run trace for scheduler/MIDI
   dispatch timing. REAPER's heartbeat supplies the host/device facts.

If the response is still late, compare 64 and 128 samples. Do not compensate
with output advance until the stable central latency is measured.

## Self-healing (automatic)

Two recoveries that used to be manual now run on their own:

- **MIDI ingress auto-reset (bridge).** When Rubato's source is present and
  REAPER's audio is running but no Rubato event is ever consumed for the current
  endpoint (a stale same-name row after a backend restart, or a first launch
  before the input opens), the bridge fires **Reset all MIDI devices** itself,
  once per source appearance and globally rate-limited (grace 3 s, ≥5 s apart,
  ≤4 attempts before it pauses and logs). Detection keys off the source
  presence-edge and a per-appearance MIDI-sequence baseline, so REAPER's
  pre-restart input history can't mask a new unbound endpoint, and an idle-but-
  healthy source is never reset. The grace is shorter than the Python probe
  window (8 s) so the rebind lands while CC119 is still being resent. The reset
  action is resolved by *name*
  via `kbd_getTextFromCmd` (a scan of the native id range), never a hardcoded
  number, so it can never fire a wrong action and survives REAPER version
  changes. On this REAPER 7.78 the id is 41175. `bridge.log` records
  `MIDI ingress auto-reset armed` at startup and each `Auto-resetting MIDI
  devices` it performs. If the action can't be verified, auto-reset stays
  disabled and the manual step below remains.
- **Renderer status auto-retry (backend).** A `failed`/`unavailable` renderer no
  longer stays stale until a manual forced Retry. When the REAPER bridge
  heartbeat is fresh and `ready` again, the PWA's own status poll kicks off one
  forced preload (rate-limited to every 20 s) so the badge clears on its own
  after the host recovers (LG reconnected, MIDI re-bound). The retry's startup
  still performs the authoritative readiness check.

## Recovery

- **Waiting for virtual MIDI:** leave the PWA preload enabled; it owns the
  virtual source. Restart the bridge after the port appears if necessary.
- **Probe not observed / "Error opening devices: Rubato Orchestra":** two causes,
  both now fixed but worth recognizing. (1) More than one same-name `Rubato
  Orchestra` **Input-enabled** row in **Preferences → Audio → MIDI Inputs**:
  disable the duplicate `<not found>` ghost so REAPER opens the single live input
  (two enabled rows conflict and REAPER opens neither). (2) REAPER's enabled row
  shows `<not found>` even though the source is live: the CoreMIDI unique ID
  drifted. Rubato pins `0x52424F31`; if a row is still bound to an old ID, run
  **Reset all MIDI devices** once with the source present to rebind. `bridge.log`
  records `Rubato MIDI input candidates`, the `Recent MIDI input device indices`
  census, and `Rubato active input latched to device index N`, which distinguish
  a false negative (REAPER consumed the probe on another index) from REAPER truly
  not opening the source.
- **After changing the bridge script:** REAPER keeps running the previously
  loaded copy. Re-run `rubato_reaper_bridge.lua` from the Actions window
  (terminate the old instance when the ReaScript task-control dialog appears) so
  the new code takes effect; editing the installed file alone does not reload it.
- **Wrong output:** select the LG device in REAPER, not only in macOS System
  Settings.
- **Bridge stale:** run `rubato_reaper_bridge.lua` again in the Rubato project.
- **No sound with Ready status:** inspect the four BBCSO patches, track meters,
  and master meter, then lower/raise the PWA fader to force a visible CC7 test.
- **Stuck notes:** use the PWA **Silence** control. Rubato sends CC123 and CC120
  on every MIDI channel directly to REAPER.

## Rollback

The previous Pedalboard implementation and
[BBCSO audio-zone runbook](bbcso-audio-zone.md) remain available during the
experiment. Do not run both hosts for the same room zone.

## Agent handoff snapshot (2026-08-14)

- Branch/worktree: `claude/mix-vst-routing` at
  `/Users/ehuang/.codex/worktrees/df76/Rubato`.
- Start: `./scripts/dev-server.sh --skip-build --skip-dvc`, then use
  `http://localhost:8000/app/`. REAPER auto-loads the installed bridge through
  `~/Library/Application Support/REAPER/Scripts/__startup.lua`.
- Expected host: LG/CoreAudio Default, 48 kHz, 128 samples; last accepted output
  latency was 278 samples (~5.8 ms).
- Logs: `~/Library/Application Support/Rubato/reaper/bridge.log` and
  `renderer-status.json`; run traces are under
  `~/Library/Application Support/Rubato/runs/<run-id>/trace/`.
- The silent run `mix-audition-1786765497842542000` proved 27 note-ons, 25
  scheduled note-offs, two terminal panics, and no notes left sounding, but was
  **not audible** because REAPER Input was disabled. Do not call it an audio
  success.
- Eric enabled both cached Rubato MIDI rows immediately before this handoff;
  `reaper.ini` confirms `midiins=3` and `midiins_all=3`. A forced retry
  (`renderer-preload-1786766323250252000`) still failed the silent ingress
  probe, so this is no longer explained by the preference bit alone.
- **RESOLVED (2026-08-14): audible.** Two layered faults blocked ingress:
  (1) duplicate enabled same-name `Rubato Orchestra` input rows made REAPER fail
  to open the input, and (2) the virtual source's CoreMIDI unique ID churned on
  every backend restart, so REAPER's enabled row (keyed by unique ID) never
  matched the live source. Fixes: disable the duplicate ghost row in
  Preferences → MIDI Inputs; pin a stable unique ID (`0x52424F31`) on the source
  in `live_reaper._pin_coremidi_unique_id`; the fixed bridge also latches the
  live index from received events. After a one-time `Reset all MIDI devices`, the
  resident preload reaches `ready` and stays durable across forced preloads with
  no manual step. The room-only 10-second audition
  (`mix-audition-1786772630406031000`) was audible (Eric confirmed): 27 note-ons,
  25 note-offs, 2 terminal panics, none left sounding; note-on lateness median
  3.25 ms / p95 5.0 ms.
- Analyze a known run with
  `uv run python scripts/analyze_live_trace.py --run-id <run-id>`.
