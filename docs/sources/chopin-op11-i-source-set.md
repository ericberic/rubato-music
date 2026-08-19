# Source: Chopin Op. 11 I Source Set

## Source

Committed source files live under:

`assets/scores/chopin_op11_i_allegro_maestoso/source/`

Files:

- `score.mxl`
- `score.mid`
- `score.pdf`
- `source_manifest.yaml`

## Date Read

2026-06-14

## Why It Matters

This is Rubato's first concrete Chopin score source set. It gives us a real
MusicXML/MIDI/PDF target for score-bundle ingestion, source validation, MIDI
track analysis, and future Matchmaker/Partitura integration spikes.

## Key Facts

- The MXL is compressed MusicXML 4.0.
- The MXL was exported by MuseScore 4.1.1 on 2023-08-04.
- The PDF is 98 pages and reports MuseScore 4.1.1 as author metadata.
- The MIDI is type 1, 480 ticks per beat, 16 tracks, about 1028 seconds long.
- The MusicXML has 15 score parts and 690 measures per part.
- The MIDI has 16 tracks because `Piano solo` is split across two MIDI tracks.
- The MusicXML has one `Piano solo` part.
- The MusicXML title is `Piano Concerto No. 1 in E Minor` and composer is
  `Frederic Chopin`.
- The first tempo marking is `Allegro maestoso` with sound tempo 126 BPM.
- The source includes full orchestral parts, not just a two-piano reduction.

## MusicXML Parts

- Flute
- 2 Oboes
- 2 Clarinets in G
- 2 Bassoons
- Horns 1 and 2 in G
- Horns 3 and 4 in G
- 2 Trumpets in C
- Trombone
- Timpani
- Piano solo
- Violins 1
- Violins 2
- Violas
- Violoncellos
- Contrabasses

## Design Lessons For Rubato

- This source set is good enough to test ingestion code immediately.
- The PDF should stay a human reference, not a runtime source.
- The MusicXML should be the first authoritative symbolic source for parts,
  measures, notated tempo, and instrumentation.
- The MIDI is useful for playback and timing experiments, but should not be
  assumed to be one-track-per-part because the piano part is split.
- The first canonical bundle converter must explicitly merge or map the two MIDI
  piano tracks back to one `Piano solo` reference part.
- Tests should lock the known source facts so library upgrades or source-file
  replacement do not silently change the import assumptions.

## Current Code Coverage

- `source_analysis.analyze_score_source_dir` reads the committed MXL/MIDI/PDF
  without heavy notation dependencies.
- Tests assert the known source facts: 15 MusicXML parts, 690 measures, 16 MIDI
  tracks, 480 ticks per beat, 98 PDF pages, and the two-track MIDI piano split.
- `ScoreBundle.load` defines the canonical runtime contract separately from this
  raw source set.
- `convert_mxl_excerpt_to_bundle` converts measures 139-141 into a validated
  canonical bundle with 50 solo events and 19 accompaniment events.

## Open Gaps

- We have converted a small MusicXML window, but not the full MusicXML into
  canonical `events.jsonl`.
- We have not yet split solo/reference events from accompaniment events.
- We have not yet created a real section map for the concerto.
- We have not yet validated Matchmaker against this source.
- Source edition/export license provenance should be verified before public
  redistribution beyond the repository, even though Chopin's composition itself
  is public domain.

## Links

- Related concept: [Score Bundle Ingestion](../concepts/score-bundle-ingestion.md)
- Related concept: [MusicXML Excerpt Conversion](../concepts/musicxml-excerpt-conversion.md)
- Related runbook: [First Chopin MVP Excerpt](../runbooks/chopin-mvp.md)
- Related design: [System Design](../SYSTEM_DESIGN.md)
