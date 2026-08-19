# Spotify Pedalboard

## Source

- [Pedalboard API](https://spotify.github.io/pedalboard/reference/pedalboard.html)
- [Pedalboard I/O API](https://spotify.github.io/pedalboard/reference/pedalboard.io.html)
- [Plugin Compatibility](https://spotify.github.io/pedalboard/compatibility.html)

Documentation version observed: 0.9.24.

## Date Read

2026-08-03.

## Why It Matters

Decision 0016 selected Pedalboard as the scriptable host for Rubato's bounded
BBCSO orchestral-audio zone. The live path is implemented; its actual-room
latency, memory, dense-tutti, and long-run behavior remain feasibility-critical.

## Key Facts

- VST3 and Audio Unit instrument plugins can receive a timestamped list of MIDI
  messages and render a fixed-duration audio buffer.
- Repeated plugin processing can preserve state with `reset=False`.
- `AudioStream` supports live device I/O and a running chain of audio-effect
  plugins.
- The official examples document MIDI instrument rendering and live audio
  effects separately.
- Third-party plugins may misbehave or crash the Python interpreter; the
  compatibility guide explicitly warns that not all plugins conform reliably.

## Caveats

- The docs do not demonstrate Rubato's required combination: asynchronously
  arriving MIDI driving a running instrument plugin into a live output stream.
- The intended orchestral plugin's compatibility, preset loading, incremental
  state, callback behavior, resource use, and latency are unmeasured.
- Pedalboard releasing the GIL for documented operations does not by itself
  establish a deadline-safe control-to-audio bridge.
- Process isolation, IPC, and buffer topology must follow a measured spike, not
  package-level assumptions.

## Local BBCSO Spike (2026-08-03)

Tested Pedalboard 0.9.24 with the installed BBC Symphony Orchestra 1.12.14
VST3 on Apple Silicon:

- the VST3 loads as an instrument and exposes 45 ordinary parameters plus raw
  host state;
- an initial fixed-duration m.43--45 string render produced non-silent stereo
  audio at 48 kHz. That spike's substring selector matched both `Violini I`
  and `Violini II`; the corrected exact-name selector reports 9 first-violin
  attacks in the canonical 43..46 window;
- `AudioStream` enumerated named CoreAudio outputs and supports selecting one by
  exact name for blocking playback;
- loading the same plug-in inside the restricted Codex sandbox aborted the
  Python process while JUCE called macOS `RegisterApplication`; normal
  GUI-session execution succeeded;
- the Audio Unit wrapper did not scan successfully through Pedalboard;
- Spitfire `.zpreset` files are not VST3 `.vstpreset` files and
  `load_preset()` rejected them. A patch must be selected once in the plug-in
  editor and persisted through `raw_state` for repeatable headless rendering;
- initialization plus this short offline render took substantially longer than
  real time. This result validates authoring/pre-render use, not a live
  callback host.

## Local BBCSO Manual And Host Audit (2026-08-08)

Read the installed *BBC Symphony Orchestra Discover User Manual* (2023, 23
pages) and checked BBCSO 1.12.14's exposed VST parameters:

- the manual's preset selector is UI-based: browser Load/double-click and
  Next/Previous buttons. It assigns no MIDI program-change numbers;
- the factory presets are installed as Spitfire `.zpreset` assets, but
  Pedalboard's supported `load_preset()` API accepts VST3 `.vstpreset` data;
- the plug-in exposes expression, dynamics, reverb, release, mic, gain,
  pan/tune, and related controls to the host, but no preset/program/instrument
  selector;
- changing BBCSO's global default-preset setting before instantiation produced
  a raw state before the selected samples became safely reloadable. Loading
  that state hung, so this is not an acceptable capture shortcut;
- Discover's manual lists strings, woodwinds, brass, pitched percussion, and
  one untuned-percussion keyboard preset. Untuned percussion maps C2 bass drum,
  D2 tenor drum, E2 snare, F2 suspended cymbal, G2 cymbal roll, A2 anvil, B2 tam
  tam, C3 tambourine, D3 tenor drum (snares off), E3 triangle, F3 castanets, G3
  cowbell, A3 stopped cymbal, B3 piatti, C4 vibra slap, and D4 woodblock;
- default documented controllers are CC1 dynamics, CC7 global gain, CC10 pan,
  CC11 expression, and CC19 reverb.

Therefore reliable headless use still requires a one-time editor-close state
capture per instrument. A prior captured state can be loaded before opening the
editor, reducing adjacent captures to Next then close.

The spike therefore clears the first compatibility rung but strengthens the
case for a disposable worker process. Incremental MIDI, callback jitter,
latency, CPU/RAM, and long-run stability remain open.

## BBCSO Live Startup Measurements (2026-08-09)

Pedalboard's `initialization_timeout` is a grace period for plug-ins that load
resources asynchronously. With BBCSO 1.12.14, this value dominated cold start:

- `initialization_timeout=120`: 121.035 seconds to return one instance;
- `initialization_timeout=1`: 1.955 seconds to return the same instance;
- applying a manually captured violin state: 0.098 seconds;
- first 512-frame, 48 kHz block: 1006.6 ms; and
- subsequent blocks: 0.023 ms median and 0.046 ms p95.

The shortened timeout produced a non-silent violin render, so waiting two
minutes was not required for sample readiness. One silent warm-up block moves
the one-second first-render cost out of performance time. Repeated processing
must retain the instance and pass `reset=False`, matching Pedalboard's streaming
guidance.

BBCSO did not tolerate aggressive host concurrency on this machine: concurrent
threaded construction crashed, concurrent independent construction crashed,
and a third short-timeout instance in one process segfaulted. The successful
test created nine independent hosts sequentially, kept them resident, and
synchronized playback after all were ready. Cold load was 31.4 seconds; the
16.1-second m.43--45 cue then streamed in real time through MacBook Air Speakers
and exited cleanly. This makes persistent per-instrument hosting the current
safe baseline. Long-run stability, aggregate CPU under dense tutti writing, and
measured acoustic latency remain admission gates.

## Links

- [Score-Authored Spatial Mixing](../concepts/psychoacoustic-spatial-mixing.md)
- [Decision 0016](../decisions/0016-pedalboard-decoupled-spatial-synth.md)
- [BBCSO Secondary Audio-Zone Audition](../runbooks/bbcso-audio-zone.md)
