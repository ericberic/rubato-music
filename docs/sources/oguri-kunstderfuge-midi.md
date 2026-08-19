# Source: Oguri / Kunst der Fuge Chopin Op. 11 MIDI

## Source

- Source page: [Kunst der Fuge Chopin MIDI](https://www.kunstderfuge.com/chopin.htm)
- Movement 2 direct file:
  `https://kunstderfuge.com/-/mid.files/chopin/concerto_11_2_(c)oguri.mid`
- Local file:
  `assets/scores/chopin_op11_ii_larghetto/source/oguri_concerto_11_2.mid`

## Date Read

2026-07-06

## Why It Matters

Eric preferred the Oguri/Kunst der Fuge MIDI playback quality over the committed
MuseScore-exported full-score MIDI. Movement 2 is slower and easier to sight
read, so it is now the first live-following rehearsal target.

## Key Facts

- The file is a Standard MIDI File type 1 with 18 tracks and 240 ticks per beat.
- Runtime length is about 666 seconds.
- Track 1 is named `PIANO SOLO` and is the reference part Eric will play.
- The remaining note-bearing tracks are orchestral accompaniment: strings,
  horns, flute, clarinet, bassoon, and related setup/empty tracks.
- The solo and orchestra tracks share one expressive MIDI performance timeline,
  so they remain synchronized with each other and are useful for playback and
  follower experiments.
- Manual Yamaha listening found the Oguri first movement beautiful at 50% and
  the preferred default for this source should be 75% global orchestra volume.

## Derived Files

Run:

```bash
uv run rubato extract-oguri --movement 2
```

This writes:

- `assets/scores/chopin_op11_ii_larghetto/derived/solo_reference.mid`
- `assets/scores/chopin_op11_ii_larghetto/derived/orchestra_accompaniment.mid`

The solo reference preserves source timing/setup metadata plus the `PIANO SOLO`
track. The orchestra accompaniment preserves source timing/setup metadata plus
all non-solo note-bearing tracks.

Current extraction facts:

- Solo reference: 2,831 note-on events.
- Orchestra accompaniment: 1,285 note-on events.
- Both files preserve the 240 ticks-per-beat timing grid.

Important coordinate caveat: the file has a single fixed 120 BPM tempo event,
while musical expression appears in event tick spacing. Its 240 PPQ is therefore
the resolution of an expressive performance coordinate, not proof that
`tick / 240` equals a notated Chopin quarter-beat. Movement 2 measure identities
must come from the canonical semantic timeline, with Oguri related through a
performance mapping. Do not use fixed groups of four Oguri MIDI beats as score
measures.

## First Solo Cue

The MIDI contains one tempo event at tick 0: 120 BPM. The first solo note in
the `PIANO SOLO` track starts at about 72.123 seconds. The default PWA cued
recording mode plays an 8.0-second orchestra cue ending at that first solo
entry.

For that workflow:

- Cue start score time: about 64.123 seconds.
- Expected solo entry in the recording: 8.0 seconds after recording start.
- The first recorded take does not need to start at full movement time zero.
- A 4.0-second cue is musically still during the orchestra sustain, but it has
  no new orchestra note-on attacks in the raw MIDI. The cue renderer therefore
  must carry active notes into the sliced cue window or the Yamaha will only
  receive note-off messages and no audible sound.
- The cue renderer must also send release events after the solo-entry cut for
  notes that were active during the cue. For the default 8.0-second first-entry
  cue, the last natural release occurs about 1.304 seconds after entry, so the
  actual cue playback lasts about 9.304 seconds while still using 8.0 seconds as
  the expected solo-entry anchor.

## Caveats

- Chopin's composition is public domain, but the MIDI sequence has its own
  rights/provenance.
- Kunst der Fuge permits private non-commercial use and limits redistribution.
- Treat this as a private local rehearsal/development source. Do not use it as a
  public/commercial redistributed asset without a separate license review.
- A machine-reviewed Oguri-to-canonical beat map is available for rehearsal
  targeting. Oguri ticks remain expressive source evidence, never score time;
  each runtime position is projected to canonical `score_tick` through the
  offline fusion artifact.
- Cursor calibration now uses beat-level Oguri/Joseffy evidence through
  recognized m.112. Native ticks 34,619 and 35,374 identify the m.12 beat-four
  pickup and m.13 downbeat. The rolled A/F-sharp downbeat at 37,485--37,493
  begins m.14; 55,772 begins m.22 and 58,155 begins m.23. The four m.17 beat
  anchors explicitly preserve its expressive rubato, so interpolation cannot
  stretch an entire notated measure uniformly
  across several engraved bars. The same three-signal check now covers every
  downbeat from measure 23 through measure 53. In particular, native tick
  124,817 is the m.53 barline, the solo B-natural at 270.29375 seconds is the
  late pickup inside m.53, and native tick 130,285 begins m.54.
- This correction was corroborated three ways: pitches/onsets in the persisted
  Yamaha take and Oguri solo reference, barlines in the Joseffy scan, and the
  printed bar numbers in this [digital piano reduction](https://static1.squarespace.com/static/57815e37893fc04f816e8b90/t/64957effbd709b3efe8f58aa/1687518975392/Chopin_concerto_e_minor.pdf)
  (read 2026-07-17). These remain checked display anchors, not a claim that the
  whole performance map has received musical review.

## Links

- Related design: [System Design](../SYSTEM_DESIGN.md)
- Related concept: [Realtime Performance Dataflow](../concepts/realtime-performance-dataflow.md)
- Related contract: [Score Bundle v2](../concepts/score-bundle-contract.md)
- Related runbook: [First Chopin MVP Excerpt](../runbooks/chopin-mvp.md)
- Related issue: [Global Orchestra Volume](https://github.com/ericberic/Rubato/issues/50)
