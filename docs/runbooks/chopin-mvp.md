# Runbook: First Chopin MVP Excerpt

## Objective

Render and then perform one short excerpt of Chopin Piano Concerto No. 1 with
computer accompaniment.

## Inputs Needed

- Excerpt boundaries.
- Authoritative score source selection.
- Clean symbolic solo piano part.
- Accompaniment events from full score, reduction, or MIDI arrangement.
- PDF/source references for human checking.
- Soloist MIDI takes.
- Section map.
- Keyboard input/output verification.

## Available Source Set

Rubato now includes a committed source set at
`assets/scores/chopin_op11_i_allegro_maestoso/source/`.

Known structure:

- MusicXML: 15 parts, 690 measures per part.
- MIDI: type 1, 16 tracks, 480 ticks per beat.
- PDF: 98 pages.
- Important mapping gap: MusicXML has one `Piano solo` part, while MIDI exports
  `Piano solo` as two tracks.

## Procedure

1. Register source files under `data/scores/chopin_op11/<version>/`.
2. Build or hand-edit the canonical score bundle.
3. Split or identify solo and accompaniment events.
4. Create `sections.json` with follow/lead/hold/stop behavior.
5. Create `instrument_map.yaml` for Yamaha GM/XG playback.
6. Record 3-5 solo MIDI takes from the soloist.
7. Run offline alignment against the solo reference.
8. Render offline accompaniment from the aligned solo timing map.
9. Review output on the Yamaha synth.
10. Run simulated online replay.
11. Run live Yamaha input with trace logging.
12. Add human feedback to the run folder or PWA review screen.

## Acceptance Check

- Accompaniment entrances land within a musically tolerable window.
- Tutti/lead sections move forward without solo input.
- The follower logs score beat and confidence throughout the excerpt.
- The scheduler logs score beat, tempo state, section mode, and emitted events.
- The Yamaha output is clear enough to judge timing, even if not orchestral-real.

## Related

- [System Design](../SYSTEM_DESIGN.md)
- [Score Bundle Ingestion](../concepts/score-bundle-ingestion.md)
- [Score Bundle Contract](../concepts/score-bundle-contract.md)
- [Chopin Op. 11 I Source Set](../sources/chopin-op11-i-source-set.md)
- [Realtime Performance Dataflow](../concepts/realtime-performance-dataflow.md)
- [PWA Rehearsal UI](../concepts/pwa-rehearsal-ui.md)
- [Yamaha MIDI Setup](yamaha-midi.md)
