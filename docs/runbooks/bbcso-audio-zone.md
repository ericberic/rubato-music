# BBCSO Secondary Audio-Zone Audition

> Legacy live host: [Decision 0019](../decisions/0019-reaper-external-orchestra-host.md)
> moves the live rehearsal path to REAPER. Keep this runbook for offline proofs,
> isolated Pedalboard diagnostics, and rollback. Use the
> [REAPER host runbook](reaper-orchestra-host.md) for current rehearsal setup.

Use this opt-in workflow to capture BBC Symphony Orchestra states, render an
offline proof, or stream Oguri's sounding sections live through separate,
persistent BBCSO hosts. BBCSO is also wired into the normal PWA Go Live path as
a machine-local, hardware-gated audio zone. The CLI auditions remain useful for
isolating plug-in and CoreAudio behavior from the follower.

## Scope And Safety

- Yamaha MIDI input remains the timing anchor. With Yamaha Local Control on,
  Eric's solo continues through the piano's natural speakers even when Rubato
  sends no orchestral MIDI back to it.
- One BBCSO instance owns one selected instrument patch. The full-fidelity CLI
  audition uses one patch per sounding Oguri section. The current live PWA
  setup deliberately groups several `stem_ids` under representative patches
  to reduce instance count and memory; this trades section fidelity and pitch
  coverage for rehearsal viability.
- CLI auditions and the PWA live path host each BBCSO patch in its own child
  process and open those hosts sequentially. The PWA sums their indexed audio
  blocks into one zone-owned sounddevice/PortAudio stream; four concurrent
  Pedalboard writers to one HDMI device can stall. A plug-in failure therefore
  does not share the FastAPI process, and no host crosses BBCSO 1.12's observed
  two-instance safety ceiling.
- Run plug-in commands from a normal macOS Terminal session. Restricted or
  headless sandboxes can make BBCSO/JUCE abort during application registration.
- Keep captured plug-in state and rendered WAV files out of git. They may
  contain machine-local paths and can become large.
- `--end-measure` is an exclusive boundary. The default `43..46` renders
  printed measures 43, 44, and 45.

## 1. Install The Live And Audio Extras

```bash
uv sync --extra dev --extra live --extra audio
```

`scripts/dev-server.sh` performs this sync automatically. Base tests and
Yamaha-only use do not require Pedalboard; a configured VST zone does.

## Current Four-Ensemble PWA Setup

The machine-local document
`~/Library/Application Support/Rubato/config/live-audio-zones.json` binds the
stable `room_center` mix zone to an exact CoreAudio device. It is intentionally
not committed because device and plug-in paths are local to this Mac.

The current memory-bounded Movement-II setup uses four instances for all nine
sounding Oguri accompaniment stems:

| Live instance | Oguri sections | Representative BBCSO state |
| --- | --- | --- |
| `violins` | Violins I and II | Violins 1 Long |
| `low_strings` | Violas, cellos, and basses | Cellos Long |
| `woodwinds` | Flutes, clarinets, and bassoons | Clarinets Long |
| `horns` | E horns | Horns Long |

One binding may name multiple exact score `stem_ids`; this is what reduces the
host count. It does not merge or rewrite the Oguri MIDI source. Representative
patches can have imperfect range or timbre for the grouped sections.

To rehearse through the soundbar without doubling the orchestra on the Yamaha:

1. Start Rubato with `./scripts/dev-server.sh`.
2. Turn on/select the LG HDMI output, run `uv run rubato audio-devices`, and
   confirm the exact configured name `LG TV SSCR2` appears under `outputs`.
   `GET /api/mix/zones` should also report `room_center` with plug-in/state
   `health: ready`, but that file check does not prove the HDMI device exists.
3. Open **Sound** beside **Go live**, then choose **Piano input → Clavinova**
   and **Orchestra output → LG soundbar · BBCSO**.
4. Leave **Keep BBCSO ready** enabled. The PWA starts the resident loader when
   it opens, before Go Live. The status beside Go Live moves through **Not
   loaded**, **Loading** (with ensemble/count), and **Ready** only after the LG
   CoreAudio stream is open. A failure stays visible with its exact worker
   error and **Retry** action. Go Live then leases the ready renderer rather
   than loading four isolated hosts on the performance path.
5. Keep Yamaha Local Control on at the piano, then start Go Live. Rubato does
   not expose or change that piano hardware setting.

The orchestra-output picker selects exactly one renderer. **Yamaha speakers**
sends accompaniment MIDI to the piano synth, disables the live spatial/VST mix,
and is immediately **Ready**—it must never wait for or launch BBCSO. **LG
soundbar · BBCSO** omits the Yamaha accompaniment MIDI copy and is the only
choice that enters the BBCSO loading lifecycle. The choice persists across a
PWA reload.

The live zone must use the measured BBCSO startup settings:
`plugin_initialization_timeout_seconds: 1.0` and `warmup_blocks: 1`. BBCSO
treats Pedalboard's initialization timeout as a grace period. A stale value of
60 seconds costs about 60 seconds for each sequential instance and can exceed
the parent worker's readiness deadline; it is not a safer loading window.

Each binding must remain in its own process. BBCSO 1.12.14 reproducibly
segfaulted on its third instance when the four PWA bindings shared one child;
both crashes occurred on the plug-in's `Work Thread 3` before `woodwinds`
finished loading. Do not give those isolated hosts separate writers to the same
HDMI device: the PWA renderer uses block-indexed queues and exactly one
zone-owned output stream. Do not consolidate the bindings into one host as a
memory optimization.

The zone API's `ready` state checks that the configured plug-in and state files
exist. It is not evidence that BBCSO is resident or its audio stream is open,
nor that the 59 ms planning value, residual distribution, memory headroom, or
rehearsal-length stability has been measured successfully. The PWA renderer
badge is the authority for the current run.

If the badge says the CoreAudio output is unavailable, reconnect or select the
LG HDMI destination in macOS, confirm it appears in `rubato audio-devices`, then
press **Retry**. Do not substitute the Clavinova unless you intentionally want
BBCSO audio through the piano. The failed preloader's full lifecycle remains in
`runs/renderer-preload-*/trace/renderer.jsonl`; the ordered rehearsal journal
also carries each `runtime:renderer_status` transition.

If a native worker crashes, automatic preload stays failed across hard refresh
and other open tabs. Inspect the named `renderer-preload-*` trace and the newest
macOS `~/Library/Logs/DiagnosticReports/Python-*.ips`; press **Retry** only after
the cause or configuration has changed. The Retry action is the sole forced
restart path, preventing a browser refresh from becoming a plug-in crash loop.

## 2. Confirm The Default Output Route

Select the intended speakers or headphones in macOS Control Center. Then run:

```bash
uv run rubato audio-devices
```

`default_output` should name the intended device. The offline audition uses
macOS `afplay`, which follows this system selection. The PWA
sounddevice/PortAudio stream is a separate path and must not be treated as proof
that file playback will work (or vice versa). If both report macOS `'stop'` /
`Audio Hardware Not Running`, the HDMI clock itself is stopped: power-cycle or
reconnect the LG route (or restart Core Audio), then press **Retry**.

## 3. Capture The BBCSO Instrument Patches Once

Pedalboard can reload captured VST state but does not expose BBCSO's proprietary
instrument browser as a programmable preset list. Open BBCSO through Rubato,
choose **Strings → Violins 1**, leave **Long** selected, then close the plug-in
editor:

```bash
mkdir -p ~/Library/Application\ Support/Rubato/plugin-states
uv run rubato capture-bbcso-state \
  --out ~/Library/Application\ Support/Rubato/plugin-states/bbcso-violins1-long.state
```

Closing the editor writes BBCSO's raw host state. Repeat for the nine sounding
sections in Oguri Movement II, using these exact filenames:

| Oguri MIDI track | BBCSO instrument | State filename |
| --- | --- | --- |
| `Violini I` | Strings: Violins 1 | `bbcso-violins1-long.state` |
| `Violini II` | Strings: Violins 2 | `bbcso-violins2-long.state` |
| `Viole` | Strings: Violas | `bbcso-violas-long.state` |
| `Violoncelli` | Strings: Cellos/Celli | `bbcso-cellos-long.state` |
| `Contrabassi` | Strings: Basses | `bbcso-basses-long.state` |
| `Corni (E)` | Brass: Horns | `bbcso-horns-long.state` |
| `Flauti` | Woodwinds: Flutes | `bbcso-flutes-long.state` |
| `Clarinetti (C)` | Woodwinds: Clarinets | `bbcso-clarinets-long.state` |
| `Fagotti` | Woodwinds: Bassoons | `bbcso-bassoons-long.state` |

For adjacent presets, reopen from the state just captured so the next selection
is one Next-arrow click rather than another browser search:

```bash
uv run rubato capture-bbcso-state \
  --from-state ~/Library/Application\ Support/Rubato/plugin-states/bbcso-violas-long.state \
  --out ~/Library/Application\ Support/Rubato/plugin-states/bbcso-cellos-long.state
```

Oguri's timpani, trumpet, C-horn, trombone, and oboe tracks contain no note
events in this movement, so they do not need instances. The piano-solo track is
deliberately excluded from the accompaniment mix.

## 4. Render The Full-Orchestra M.43--45 Proof

Render without playing first:

```bash
uv run rubato audition-bbcso-orchestra \
  --start-measure 43 \
  --end-measure 46 \
  --wav-out /tmp/rubato-bbcso-orchestra-m43-45.wav
```

The command should report:

- source window near `212.856..228.983s`;
- one stem line for each of the nine sections;
- positive note-on counts for the sections active in the selected window;
- `peak` greater than zero; and
- the rendered WAV path.

Track selection is exact: `Violini I` never also selects `Violini II`. If a stem
reports `peak=0`, reopen that state capture and confirm the patch, sample-library
health, Long technique, and playable pitch range.

## 5. Play Through AirPods Or MacBook Speakers

After checking the macOS default-output selection and volume, render and play:

```bash
uv run rubato audition-bbcso-orchestra --play-default
```

The command renders before playback, so initial sample loading may take several
minutes. Once the WAV exists, replaying it directly with `afplay` is immediate.

## 6. Stream The Cue Live

Select the intended macOS output first and confirm its current exact name with
`audio-devices`. Bluetooth devices disappear from the list when disconnected;
do not reuse a stale AirPods name.

The connected Clavinova can expose two independent roles at once: `Clavinova`
appears as both the Yamaha MIDI input/output and a named CoreAudio speaker
output. Pass `--device "Clavinova"` to use its speakers for a BBCSO audition,
or `--device "MacBook Air Speakers"` for the Mac. One audition command targets
one CoreAudio device; simultaneous spatial use requires separately calibrated
zones or a macOS Aggregate/Multi-Output Device.

```bash
uv run rubato play-bbcso-live \
  --start-measure 43 \
  --end-measure 46 \
  --device "MacBook Air Speakers"
```

BBCSO does not safely initialize multiple short-timeout instances concurrently,
or more than two such instances in one process. The audition therefore starts
one ordinary, isolated host per patch in sequence. Each host uses a one-second
Pedalboard initialization grace period, applies its captured state, warms one
silent block, opens the selected CoreAudio device, and waits at a shared start
barrier. Keep these hosts resident for rehearsal; relaunching them for each cue
would reintroduce cold-start delay.

## 7. Measure Before Live Admission

The current command proves named-device playback but does not measure acoustic
onset. Before accepting the HDMI zone for live use, measure the complete
BBCSO/CoreAudio/HDMI path at the piano bench in Standard and Game modes. Record
median, p95 residual after compensation, first-sound behavior, and a
rehearsal-length stability run. The working planning assumption is 59 ms; the
measured hardware profile is authoritative.

## Known-Good Local Spike

On 2026-08-08, BBCSO 1.12.14 under Pedalboard rendered the exact `Violini I`
m.43--45 stem to a non-silent stereo 48 kHz WAV (peak 0.265, RMS 0.035). macOS
`afplay` reached AirPods successfully; Pedalboard's live `AudioStream` path to
the same AirPods produced silence and remains a separate live-I/O issue.

On 2026-08-09, the isolated live audition loaded the nine required patches in
31.4 seconds and streamed Oguri m.43--45 for 16.1 seconds through MacBook Air
Speakers. It dispatched 52 note-ons, exited cleanly, and Eric confirmed hearing
the full orchestra with multiple correct instrument families. A single BBCSO
instance measured 121.0 seconds with a 120-second Pedalboard initialization
grace period, but only 1.96 seconds with a one-second grace period; applying the
captured state took 0.10 seconds. Its first 512-frame block took 1.01 seconds,
while warmed blocks measured 0.023 ms median and 0.046 ms p95 against a 10.7 ms
48 kHz deadline. Cold start is therefore a residency problem, not a steady-state
rendering limitation.

On 2026-08-13, the four-group PWA renderer passed a ten-second native BBCSO
lifecycle probe behind one real-time-paced discard stream: 40 note-ons, 40
note-offs, eight panic controllers, and zero notes left sounding. Every patch
reached 10.055 seconds of renderer time with p95 utilization below 6%. The
loader recovered one intermittent `SIGSEGV` with its single bounded retry.
Readiness now includes a sacrificial indexed block from every resumed host,
then rebases all four to one score/audio origin before opening the live lease.

After reboot restored the LG HDMI clock, PWA run `live-codex-noteoff-final`
completed the real-output acceptance at a 15% orchestra fader: 17 note-ons, 13
ordinary score-timed note-offs, four still-active notes cleared at Stop, zero
notes left sounding, zero late events, and zero audio underruns. BBCSO remained
resident and Ready on `LG TV SSCR2`. If a future trace has finite scheduler
durations but no ordinary note-offs, inspect `midi_output/release_retime`: a
deadline must stay in the renderer's score/audio clock and must never be
repeatedly clamped to the control loop's later wall time.

## Related

- [Calibrated Low-Latency Spatial Mixing](../concepts/psychoacoustic-spatial-mixing.md)
- [Decision 0016](../decisions/0016-pedalboard-decoupled-spatial-synth.md)
- [Mix Authoring Mode](../design/MIX_AUTHORING_MODE.md)
- [Pedalboard source notes](../sources/pedalboard.md)
- [LG S95-Series HDMI Latency](../sources/lg-s95-series-hdmi-latency.md)
