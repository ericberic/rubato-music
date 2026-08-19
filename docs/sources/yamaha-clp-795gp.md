# Source: Yamaha CLP-795GP

## Source

- Official specs: https://uk.yamaha.com/en/musical-instruments/pianos/products/clavinova/clp-795gp/specs.html
- Owner manual PDF: https://data.yamaha.com/files/download/other_assets/5/1342335/clp785_en_om_d0.pdf

## Date Read

2026-06-14

## Why It Matters

Eric's MVP hardware is a Yamaha CLP-795GP. Rubato should initially route live
solo MIDI from this piano and send accompaniment MIDI back to its internal synth
before adding a DAW/VST dependency.

## Key Facts

- The CLP-795GP has 88 keys, GrandTouch keyboard, and three pedals.
- It supports 256-note maximum polyphony.
- It includes 53 preset voices, 14 Drum/SFX kits, and 480 XG voices.
- Compatibility includes XG/GM, GS for song playback, and GM2 for song playback.
- It supports 16-track song recording and SMF Format 0/1 playback.
- Connectivity includes MIDI IN/OUT/THRU and USB TO HOST.
- It has AUX OUT and a substantial built-in speaker/amplifier system.
- Bluetooth audio/MIDI availability varies by country; USB/MIDI ports are the
  reliable MVP route.

## Design Lessons For Rubato

- The CLP-795GP is viable for the MVP because it can act as MIDI input and a
  multitimbral-ish XG/GM playback target.
- The orchestral sound will be serviceable for proof-of-concept, not production
  quality. Expect strings/woodwinds/brass to be generic XG voices.
- The first renderer should emit conservative GM/XG program changes, channels,
  notes, velocities, and sustain/pedal only when needed.
- A later VST/DAW path should remain in the architecture for better orchestral
  realism, articulations, balance, and reverb.
- Hardware setup should prefer USB TO HOST or standard MIDI ports over Bluetooth
  for lower ambiguity and easier debugging.

## Open Questions

- Which exact CLP-795GP MIDI port names appear on Eric's Mac?
- Does the piano accept live external MIDI on multiple channels while Eric plays
  the local piano voice?
- Which built-in XG orchestral voices sound acceptable for Chopin rehearsal?
- Does local control need to be disabled or split to prevent doubled piano sound?

## Related

- [Yamaha MIDI Setup](../runbooks/yamaha-midi.md)
- [PWA Rehearsal UI](../concepts/pwa-rehearsal-ui.md)
