# Runbook: Yamaha CLP-795GP MIDI Setup

## Goal

Use the soloist's Yamaha CLP-795GP as both the live solo MIDI input and the first MVP
accompaniment synth output.

## Known Device Capabilities

From Yamaha's official CLP-795GP specs:

- MIDI IN/OUT/THRU.
- USB TO HOST.
- 256-note maximum polyphony.
- 53 voices + 14 Drum/SFX kits + 480 XG voices.
- XG/GM compatibility, GS/GM2 for song playback.
- SMF Format 0/1 playback and 16-track song recording.
- AUX OUT for routing piano audio to speakers/interface if needed.

## Recommended Connection

Use USB TO HOST or standard MIDI ports. Do not rely on Bluetooth MIDI for the
first MVP because availability varies by country and latency/debugging are less
predictable.

## Commands

Install live dependencies:

```bash
uv sync --extra dev --extra live
```

List available MIDI ports:

```bash
uv run python -c "import mido; print('inputs:', mido.get_input_names()); print('outputs:', mido.get_output_names())"
```

The cockpit's Sound Check row now reports this distinction itself
(`GET /api/midi/devices` returns `backend_available`): empty dropdowns say
either "MIDI backend not installed" or "No MIDI ports detected" instead of
just showing empty. This terminal command remains useful for debugging
outside the UI, but is no longer the only way to tell the two cases apart
(rubato#98).

Or use Rubato's rehearsal CLI:

```bash
uv run rubato devices
uv run rubato extract-oguri --movement 2
uv run rubato inspect-oguri-cue --movement 2
uv run rubato play-oguri-cue --movement 2 --out Clavinova --volume 0.75
uv run rubato play-oguri --movement 2 --out Clavinova --volume 0.75 --seconds 75
uv run rubato record --in Clavinova --session movement2_take
uv run rubato panic --out Clavinova
```

`extract-oguri` creates the movement-2 `solo_reference.mid` and
`orchestra_accompaniment.mid` derived files. `play-oguri` suppresses the
`PIANO SOLO` track by default, so only the orchestra is sent back to the Yamaha.
`inspect-oguri-cue` is the non-audio diagnostic for the first-entry cue; it
must report a positive `note_on_count`. `play-oguri-cue` sends only that cue to
the Yamaha so cue playback can be tested without recording.

Connect the piano via USB TO HOST directly to the development Mac. USB MIDI is
class-compliant and avoids needing a separate 5-pin MIDI interface.

## System Configuration

1. Connect the USB cable.
2. Turn the piano on.
3. Verify that macOS recognizes the MIDI device in Audio MIDI Setup.
4. Run `python -m aimusic.cli info` to list available MIDI ports.

## Local Control

- If using internal piano speakers for accompaniment only: keep Local Control **ON**
  so the solo piano sound has zero latency.
- If using external VST/DAW for both piano and orchestra: turn Local Control **OFF**
  to avoid doubled notes.

## Cueing & Timing

Use the Take Capture deck for the first human-performance fixtures (see
[Recording Flow Redesign](../design/RECORDING_FLOW_REDESIGN.md) for the full
free-take-vs-cued-take design):

1. Select `Clavinova` as Yamaha input and output.
2. Leave the lead-in length at its default (`8` s) under "Capture options" if
   the first-entry cue is what's wanted.
3. Click `▶ From the top · 8s lead-in`.
4. Listen to the short orchestra cue during the "Lead-in" state, then enter
   on the solo piano part when the deck flips to "Recording".
5. Click `Stop Take (space)`.
6. Find the take in the "Takes for II. Larghetto" list to play it back or
   download its MIDI.

The take's MIDI time zero is the cue start. The default 8-second cue includes
the final orchestral attacks before the first piano entry. Shorter cue windows
can still work because the cue renderer reconstructs notes that are already
sounding at cue start, but they may communicate less tempo context.

The cue player keeps sending note-off/release events for cue notes after the
expected piano-entry anchor. For the first-entry Oguri cue, the expected entry is
8.0 seconds into the take, but the cue's release tail lasts until about 9.304
seconds. This prevents held synth notes while still letting the soloist enter at
the 8.0-second anchor.

The backend writes
`recording_metadata.json` beside `solo.mid` with:

- `cue_start_score_seconds`
- `cue_duration_seconds`
- `cue_release_tail_seconds`
- `expected_solo_entry_score_seconds`
- `expected_solo_entry_recording_seconds`
- `cue_event_count`
- `cue_note_on_count`

This lets early alignment estimate score time as
`cue_start_score_seconds + recorded_event_seconds`, then correct that estimate
against the Oguri `PIANO SOLO` reference.

## Checklist

- CLP-795GP appears as a MIDI input.
- CLP-795GP appears as a MIDI output.
- One-note input can be logged with pitch, velocity, channel, and timestamp.
- One-note output can be heard from the synth.
- Program changes can select at least a basic string voice and woodwind/brass
  voice.
- A panic/all-notes-off command is available before rehearsal.
- Verify whether local control should stay on or be disabled for the chosen
  routing.
- Verify whether external accompaniment can sound while the soloist plays the local
  piano voice.
- For the first movement-2 rehearsal, the soloist can start at a comfortable piano
  entrance. The next follower step should infer the MIDI tick anchor by matching
  the live notes against the Oguri `PIANO SOLO` reference.

## MVP Instrument Strategy

Start with conservative GM/XG mapping:

- channel 1: solo piano input monitoring only, not accompaniment output
- channels 2-5: strings
- channels 6-8: winds/brass as needed
- channel 10: percussion only if needed

For Chopin concerto MVP, the Yamaha synth is expected to be useful for timing
and rehearsal validation, not final orchestral realism.

## Related

- [Yamaha CLP-795GP](../sources/yamaha-clp-795gp.md)
- [System Design](../SYSTEM_DESIGN.md)
- [PWA Rehearsal UI](../concepts/pwa-rehearsal-ui.md)
- [Accompaniment Control](../concepts/accompaniment-control.md)
