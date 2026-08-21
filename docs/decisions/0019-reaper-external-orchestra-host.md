# Decision 0019: REAPER owns live orchestral audio

- Status: accepted for experiment
- Date: 2026-08-14

## Context

The Pedalboard/BBCSO proof established that BBCSO can render incrementally, but
the safe host topology became four isolated Python processes, four block
queues, a parent mixer, prefill, and one blocking PortAudio/CoreAudio writer.
In the final hardware trace, follower and scheduler timing were healthy while
command-to-render latency accumulated around 470--500 ms. BBCSO's actual
`process()` work remained below 1 ms per 10.7 ms block, with low CPU and no
underruns. The problem was the custom host and buffering topology, not sample
synthesis throughput.

The soloist then observed both GarageBand live MIDI synthesis and system sounds over
the same LG HDMI output at roughly acceptable interactive latency. This rules
out an unavoidable 500--700 ms soundbar delay and makes the host boundary the
next variable to replace.

## Decision

REAPER owns BBCSO instances, audio mixing, the CoreAudio callback, buffer
selection, and the LG output. Rubato continues to own score following, tempo,
section policy, scheduling, note lifetimes, panic, mix policy, and the PWA.

Rubato publishes one CoreMIDI virtual source named `Rubato Orchestra`. The four
existing memory-bounded ensemble bindings become MIDI channels 1--4:

| Channel | REAPER track | Score stems | BBCSO patch |
| --- | --- | --- | --- |
| 1 | Violins | Violins I/II | Violins 1 Long |
| 2 | Low strings | Violas/cellos/basses | Cellos Long |
| 3 | Woodwinds | Flutes/clarinets/bassoons | Clarinets Long |
| 4 | Horns | E horns | Horns Long |

Rubato sends note-on/off, panic, and CC7 directly through CoreMIDI at its
existing immutable deadlines. No audio buffer crosses Python or IPC. A small
deferred Lua ReaScript validates the exact project, virtual MIDI input, four
armed/monitored tracks, BBCSO FX health, audio-engine state, output device,
sample rate, block size, and device-reported output latency. It writes a
short-lived heartbeat; the PWA reports Ready only while that heartbeat is fresh,
the configured LG output matches, and a silent channel-16 probe proves REAPER
actually accepts MIDI from the current CoreMIDI endpoint. Device enumeration
alone is insufficient because REAPER can list a disabled input.

The C/C++ extension SDK is deferred. REAPER documents ReaScript as exposing
most of the same API, and embedded Lua is sufficient for project setup and
readiness. A compiled in-process extension becomes justified only if an
experiment proves that the file heartbeat or virtual-MIDI boundary cannot meet
startup, control, or observability requirements.

Pedalboard remains available as a rollback and offline-rendering tool during
the experiment. It is no longer the preferred live-audio host.

## Acceptance experiment

- 128-sample initial CoreAudio buffer at 48 kHz.
- Four collapsed BBCSO tracks, not the original full orchestral template.
- PWA master-volume changes are immediately audible through CC7.
- A ten-second intro has correct note releases and no cumulative blur.
- MIDI dispatch-to-audible response is comfortably below 100 ms.
- Ten cold launches and a 30-minute soak do not crash or lose the MIDI route.
- Memory pressure remains safe on the rehearsal Mac.

No nonzero output advance is assumed until the complete REAPER/BBCSO/HDMI path
is measured. The prior 59 ms value was a planning assumption for a different
host path.

## Consequences

- The real-time Python audio workers and four Dock Python processes disappear
  when the REAPER configuration is selected.
- BBCSO patch selection remains a one-time GUI operation because the plug-in
  exposes no stable program/preset selector to the host.
- REAPER must be open with the Rubato project and bridge running before the PWA
  can truthfully report the orchestra as Ready.
- The Yamaha local piano sound remains independent: Yamaha MIDI is input to
  Rubato; orchestra MIDI goes only to REAPER; REAPER audio goes only to LG.

## Links

- [REAPER source note](../sources/reaper.md)
- [REAPER orchestra runbook](../runbooks/reaper-orchestra-host.md)
- [Decision 0016](0016-pedalboard-decoupled-spatial-synth.md)
- [Realtime performance dataflow](../concepts/realtime-performance-dataflow.md)
