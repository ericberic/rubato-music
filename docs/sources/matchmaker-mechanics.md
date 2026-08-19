# Source Detail: Matchmaker Mechanics

## Source

- Parent source note: [Matchmaker](matchmaker.md)
- Repo: https://github.com/pymatchmaker/matchmaker

## Summary

Matchmaker is an integration and benchmarking framework for online alignment. It
is a practical first package to wrap in Rubato.

## Runtime Pipeline

- Input source: file, live device, or byte stream.
- Stream: chunks audio or MIDI and sends observations.
- Processor: converts input into features.
- OnlineAlignment: consumes features and yields current score beat.

The output position is in score beats, following Partitura's `onset_beat`
representation. That maps directly onto Rubato's section map and scheduler.

## Audio Processors

Matchmaker can use chroma, mel, CQT, MFCC, log-spectral energy, CQT spectral
flux, or raw-spectrum processors for audio input.

## MIDI Processors

For MIDI input, Matchmaker can use:

- `pitch_chord`: pitch features grouped per chord onset.
- `pitch`: pitch features per note.
- `pianoroll`: piano-roll features.
- `pitchclass`: pitch-class features.

## Runtime Modes

Simulation mode avoids hardware/threading variability and is the first target
for Rubato experiments.

Live mode supports connected audio or MIDI devices when optional device
dependencies are installed.

The README also documents `BytesMidiStream`, which accepts raw MIDI bytes from a
queue. This is directly relevant to the existing Web MIDI console: browser MIDI
events can be forwarded as binary WebSocket frames without JSON conversion.

## Related

- [Matchmaker](matchmaker.md)
- [Score Following](../concepts/score-following.md)
- [Yamaha MIDI Setup](../runbooks/yamaha-midi.md)
