# REAPER external orchestra host

## Sources

- [REAPER ReaScript documentation](https://www.reaper.fm/sdk/reascript/reascript.php)
- [REAPER 7.78 generated API](https://www.reaper.fm/sdk/reascript/reascripthelp.html)
- [REAPER C/C++ extension mini-SDK](https://github.com/justinfrankel/reaper-sdk)
- [Real-time symphony orchestra example](https://veliugurguney.com/blog/post/playing_symphony_orchestra_real_time)

Read 2026-08-14 against REAPER 7.78.

## Relevant facts

- ReaScript can call most REAPER API functions available to compiled
  extensions. Embedded Lua 5.4 requires no separate runtime.
- Deferred scripts can remain active and react to project state.
- The initial `reaper.defer(callback)` must be scheduled by the Lua script's
  top-level chunk. On the local 7.78 build, entering a helper synchronously and
  deferring from inside it allowed the chunk to exit and immediately invoked
  `atexit`. The bridge mirrors REAPER's documented top-level defer example.
- The API can enumerate MIDI inputs, set a track's MIDI device/channel input,
  arm and monitor tracks, add a named VST instrument, inspect FX enabled/offline
  state, check whether audio is running, identify the current audio output, and
  report block size, sample rate, and input/output latency samples.
- `I_RECINPUT` encodes MIDI input as `4096 + device_index * 32 + channel`, where
  channel 0 means all and 1--16 select one channel.
- `MIDI_GetRecentInputEvent(0)` latches global recent-input history and exposes
  event bytes, sequence, device index, and sample-relative age. The bridge uses
  it to verify Rubato's silent ingress probe and report whether REAPER consumed
  MIDI, independently of Python's send trace.
- REAPER's C/C++ SDK is for compiled extensions loaded in-process. It is a much
  larger crash and maintenance surface than needed for the initial bridge.
- The linked orchestral example uses Python `rtmidi` only to send messages into
  virtual MIDI ports. REAPER hosts the BBCSO VST instances and owns audio. It
  does not demonstrate Python audio rendering.

## Local installation

- `/Applications/REAPER.app` is REAPER 7.78 on Apple Silicon.
- REAPER's VST cache contains `BBC Symphony Orchestra (Spitfire Audio)` as a
  VST3 instrument.
- The initial REAPER engine ignored its written 48 kHz/512-sample requests
  because both request flags were disabled, and the project pinned 44.1 kHz.
  Enabling the request flags and pinning the generated project to 48 kHz/128
  samples reduced reported output latency from 662 samples (~15.0 ms) to 278
  samples (~5.8 ms). The menu bar independently showed `48kHz · 128spls ·
  ~5.7ms` output.
- A second direct executable launch at 2026-08-14 06:50 aborted in macOS
  AppKit/HIServices registration before loading the Rubato project or BBCSO.
  The original REAPER process (launched 06:46) remained healthy. Bridge reruns
  therefore use REAPER's Actions window rather than command-line reinjection
  into an already-running GUI instance.

## Links

- [Decision 0019](../decisions/0019-reaper-external-orchestra-host.md)
- [REAPER orchestra runbook](../runbooks/reaper-orchestra-host.md)
