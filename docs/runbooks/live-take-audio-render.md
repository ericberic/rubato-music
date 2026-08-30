# Live take: where the data lives, and rendering it to audio

Read this before concluding a take "lost" note-offs/pedal, or before hand-rolling
a MIDI→audio render.

## What every live run captures, and where

A live run persists to **two different places** — do not confuse them:

| What | Where | Contents |
| --- | --- | --- |
| **Run trace** | `<runs_root>/<run-id>/trace/runtime.jsonl` | Diagnostics. `input` rows are the soloist's **note-ons only** (the follower only needs onsets). `midi_output` rows are the orchestra Rubato generated, and **do** carry `pitch` + `note_off`. |
| **Solo capture** | `<session_dir>/solo.mid` — `paths.session_dir(run_id)`, typically `<data_root>/processed/<run-id>/solo.mid` | The **complete** solo performance: note-ons, **note-offs**, **sustain pedal (CC64)**, soft pedal (CC67), aftertouch, hi-res velocity. Written by `_write_captured_performance` in `src/aimusic/server/live_runtime.py`. |

### The trap that caused the investigation

The `input` rows in the trace are **note-ons only, by design** — the follower
consumes onsets. **This is not the capture.** The full solo, with releases and
pedal, is in `solo.mid`. `_read_input_into_queue` appends every
`_PERFORMANCE_MIDI_TYPES` message (note_off, control_change/pedal, pitchwheel,
aftertouch…) to the capture sink; only the *trace* is filtered. **Never conclude
a take lost note-offs or pedal from the trace — check `solo.mid` first.**

Quick check for any run:

```python
import mido
from collections import Counter
from aimusic.core import paths

run_id = "live-..."
m = mido.MidiFile(paths.session_dir(run_id) / "solo.mid")
Counter(msg.type for msg in mido.merge_tracks(m.tracks))
# expect note_on, note_off, and control_change (with control 64 = sustain pedal)
```

The orchestra is not written to its own file; reconstruct it from the trace
`midi_output` rows (`pitch`, `velocity`, `channel`, `target_perf_time`).

## Rendering a take to WAV through the instrument

Rubato is symbolic — no audio is saved by default. To hear/keep a take as audio,
replay its MIDI through the instrument/synth (which renders the sound, applying the
real pedal to the solo) and record the instrument's **audio input**.

One command:

```bash
uv run python scripts/render_take_audio.py --run-id live-<id>   # full take
uv run python scripts/render_take_audio.py --seconds 45         # a proof segment
```

It aligns `solo.mid` (with pedal) and the trace-reconstructed orchestra on the
shared perf clock, replays both to the instrument with the run's channel volumes,
records the audio input, and writes `<run>/<run-id>.wav`. A full take renders in
real time.

### Reconstruction gotchas (guarded by tests)

Guarded by `tests/scripts/test_render_take_audio.py`:

- **`retrigger_note_off` is a release.** When a held pitch is re-struck, the
  scheduler releases the old note with a `retrigger_note_off`, not a plain
  `note_off`. The trace balances as `note_on == note_off + retrigger_note_off`.
  Reconstructing only `note_on`/`note_off` drops those releases and leaves notes
  **droning for the entire render** — the runtime is not at fault. The renderer
  also closes any still-open note at the end and prints a WARNING, so a stuck
  note can never slip through silently.
- **Anchor the solo on its first note-on, not solo.mid's `t=0`.** solo.mid's
  `t=0` is the first *captured* event — usually a pedal press ~1 s before the
  first note — so anchoring on it shifts the whole solo late and desyncs it from
  the orchestra. Match solo.mid's note-ons 1:1 to the trace's exact note-on
  timestamps and use the median offset (it absorbs solo.mid's ~50 ms
  tick-quantization jitter).
- **`retrigger_note_off` rows carry no `target_perf_time`** — fall back to
  `requested_send_time` / `monotonic_time` for their timestamp.

## Preferred for NEW takes: capture audio live

Replaying is only needed for takes already played. For future takes, record the
instrument's audio input **live during the performance** — the instrument already
mixes solo (local) + orchestra (from Rubato) with real pedal, so a live audio
recorder yields a WAV with zero reconstruction.
