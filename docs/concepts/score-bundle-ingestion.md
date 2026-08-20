# Score Bundle Ingestion

## Summary

Rubato should not use PDF pixels or raw MIDI ticks as the primary runtime
representation. It should ingest source materials into a canonical score bundle
with event tables, part maps, section maps, and source references.

PDFs, MusicXML, MIDI files, piano reductions, and recordings can all contribute,
but the runtime should operate on symbolic beat-indexed events.

## Source Materials

Useful Chopin concerto sources may include:

- Full score PDF.
- Solo piano score PDF.
- Two-piano or piano reduction PDF.
- MusicXML exports from notation/OCR tools.
- MIDI files for solo piano, reduction, or orchestral arrangement.
- Commercial/public recordings for reference listening.
- the soloist's MIDI rehearsal takes.

Raw audio is deferred for runtime, but it remains useful as listening/reference
material.

Rubato's first committed source set is documented in
[Chopin Op. 11 I Source Set](../sources/chopin-op11-i-source-set.md).

## Canonical Bundle Shape

Bundle v2 separates immutable inputs from rebuildable outputs:

```text
bundle.yaml
source/
  semantic_score.musicxml
  display_score.pdf
  playback_performance.mid
derived/
  timeline.json
  follower_reference.mid
  accompaniment.mid
  display_map.json
  performance_map.json
  sections.json
  instrument_map.yaml
```

The canonical timeline is exact integer `score_tick` plus measure index, printed
label, offset, and meter. Events and all external mappings refer to those ticks.
See [Score Bundle v2 Contract](score-bundle-contract.md) for schemas and
readiness rules.

Each eventual runtime score event should be expressible as:

```text
event_id
measure
beat
part_id
voice/staff
pitch or rest
duration_beats
notated_dynamic
articulation
score_tick/duration_ticks
source_refs
```

External source coordinates are not copied into `score_tick`. First-class
mapping artifacts relate PDF page geometry, MusicXML identities, expressive
MIDI ticks/seconds, and take timing to score time, preserving traceability
without forcing runtime code to reason over pixels or source-specific timing.

## Piano Reduction Role

A piano reduction is useful for early mapping and MVP rendering because it shows
many orchestra notes on a piano-readable timeline. It is not equivalent to full
orchestration. It often loses instrument assignment, articulation, register
intent, balance, and orchestral texture.

MVP path:

1. Use reviewed semantic notation to build the canonical timeline.
2. Map the reduction engraving to that timeline for performer communication.
3. Align expressive MIDI to the timeline rather than equating MIDI ticks with
   notated beats.
4. Derive the solo reference and accompaniment in canonical score time.
5. Map accompaniment events to GM/XG channels for Yamaha playback.
6. Replace with better orchestral parts/VST mappings later.

## Offline Correspondence Strategy

No reviewed package currently makes a piano-reduction-to-orchestral-MIDI
correspondence trustworthy in one call. Use a constrained, corroborated build
stage instead:

1. Run full Audiveris recognition and retain both `.omr` scan geometry and a
   MusicXML draft.
2. Convert the recognized notation into ordered canonical measure/beat states
   carrying pitch sets, chord/onset groups, bass/soprano contour, intervals,
   and OMR uncertainty.
3. Use only sparse performer-verified musical landmarks as hard constraints.
   PDF boxes provide geometry, not musical-time boundaries.
4. Align the Joseffy Piano I sequence to the Oguri solo track first. This is
   the strongest shared symbolic signal and owns note/beat identity wherever
   the solo is active. Fit expressive source time only after matching.
5. Align Piano II reduction material to orchestral events locally inside the
   resulting symbolic windows, using pitch-class/onset/chord and harmonic
   progression rather than requiring identical orchestration.
6. Interpolate only explicit gaps, compare at least two methods, and emit
   confidence plus rejected alternatives. A
   Parangonar matcher can be one candidate, but Rubato owns validation and the
   final canonical mapping.

Movement II now implements the smallest sufficient version of this plan. Sparse
verified landmarks divide the piece into monotonic windows; constraint-bounded
pitch-sequence alignment infers downbeats, and local symbolic matching recovers
interior beats. The build emits four anchors per recognized 4/4 measure and
interpolates only inside one evidenced beat at runtime. A historical dense MIDI
tick table no longer overrides contradictory symbolic evidence. The isolated
B-natural at m. 53 beat four remains a piece annotation, not a general pitch
heuristic. Measures 113–126 were explicit gaps until the Aug 3-4 phantom-chord
repair (see [Joseffy source notes](../sources/joseffy-reduction.md)); mm.113–125
now carry note-anchored evidence, cross-validated against an independent
MuseScore-orchestra/Parangonar alignment to well under 0.2s. Only m.126 stays
unmapped -- the two independent alignments don't agree with each other there
either, so no source is trustworthy enough to anchor it.

The solo line remains the primary correspondence signal after m.12, but it may
not define an orchestral barline while the pianist is silent. The compiler now
uses Piano II → Oguri orchestra pitch-class alignment as a fallback only for a
downbeat the Piano I → Oguri solo path could not identify; it never competes
with or displaces an available solo match. This corrected the orchestra-led
mm.104–105 gap, where solo-only interpolation had placed m.104 beat 1 on the
G-sharp that the engraving assigns to m.103 beat 4.

The one-time schema-v1 layout migration is complete. Raw OMR geometry now uses
schema-v2 zero-based `box_index`; the display-map build joins it to canonical
zero-based `measure_index` and the printed `measure_label`. Fusion and runtime
consume the display map, so no production path carries 0/1 compatibility
logic.

The Nakamura Symbolic Music Alignment Tool is a useful independent HMM-based
baseline for ordinary MusicXML/MIDI alignment. The published bootleg-score
method is another independent idea for projecting MIDI and scanned piano music
into a common notehead-image representation, but its public implementation is
an older research notebook rather than a maintained library.

## Open Gaps

- How should the machine MusicXML rhythm be structurally repaired before it can
  promote the Movement 2 timeline from machine review?
- How much manual correction is needed for repeats, cues, and solo/tutti splits?
- How much of the reduction display map can be recovered from OMR versus manual
  system/barline anchors?
- Which Movement 2 spans differ between the Oguri performance and reviewed
  semantic edition?
- What automatic acceptance thresholds should promote dense note/x anchors?
  (Page 14's crash is now repaired reproducibly — a duplicate-head phantom
  chord, see the [Score Localization skill](../../.agents/skills/score-localization/SKILL.md)
  and [Joseffy source notes](../sources/joseffy-reduction.md).)

## Related

- [Realtime Performance Dataflow](realtime-performance-dataflow.md)
- [Score Bundle Contract](score-bundle-contract.md)
- [Chopin Op. 11 I Source Set](../sources/chopin-op11-i-source-set.md)
- [Parangonar](../sources/parangonar.md)
- [First Chopin MVP Excerpt](../runbooks/chopin-mvp.md)
- [System Design](../SYSTEM_DESIGN.md)
