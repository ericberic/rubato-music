# Score Bundle v2 Contract

## Summary

Score Bundle v2 is the versioned, inspectable boundary between source
preparation and Rubato's rehearsal/live workflows. Independent engravings,
semantic scores, expressive MIDI performances, and layout evidence map around
one exact canonical score timeline. A source's filename or native timing never
defines that timeline implicitly.

The implemented schemas live in
`src/aimusic/accompaniment/bundle_v2.py`. Current follower/scheduler code still
loads the older event bundle through `LegacyScoreBundleAdapter`; that adapter is
a migration seam, not a second v2 representation.

## Directory Shape

```text
bundle.yaml
source/
  semantic_score.musicxml
  display_score.pdf
  playback_performance.mid
  recognition.omr                 # optional layout evidence
derived/
  timeline.json
  follower_reference.mid
  accompaniment.mid
  display_map.json
  display_layout.machine.json      # optional pre-mapping OMR geometry
  performance_map.json
  performance_beat_map.machine.json
  sections.json
  instrument_map.yaml
  ngram_index.json                # optional
```

Large and rights-restricted sources remain under DVC/local storage. The
manifest records relative paths, roles, provenance, and optional SHA-256
digests; it does not authorize redistribution.

## Manifest Identity and Roles

`bundle.yaml` has `schema_version: 2`, immutable `(bundle_id, revision)`, work
and movement identity, a canonical timeline reference, declared sources,
derived artifacts, and mapping references.

Source roles are explicit:

- `semantic_score`: measure structure, meter, notation, and parts.
- `display_score`: the engraving shown to the performer.
- `playback_performance`: expressive MIDI or another timed rendition.
- `layout_evidence`: OMR/project geometry used to derive a display map.
- `annotations`: reviewed musical or structural annotations.

Derived roles include `follower_reference`, `accompaniment`, `display_map`,
`display_layout`, `performance_map`, `performance_beat_map`, `sections`,
`instrument_map`, and `ngram_index`. Derived artifacts name their inputs and
remain rebuildable.
`display_layout` is deliberately distinct from `display_map`: layout is
machine geometry in one PDF, while a display map is reviewed correspondence to
canonical score time. Layout schema v2 calls its raw sequential identity
`box_index` and starts at zero. Only the joined display map may use canonical
`measure_index`; performer surfaces show `measure_label` instead.

## Canonical Timeline

The timeline uses integer `score_tick` at a declared `canonical_ppq` (960 for
the initial design). It contains continuous, half-open measure spans with:

```text
measure_index       monotonic zero-based identity
measure_label       printed label such as "1", "pickup", or "24a"
start_tick/end_tick exact canonical score interval
time_signature      beats and beat type
implicit            pickup/implicit-measure marker
```

`ScorePosition` carries both machine and performer coordinates:

```text
movement_id, score_tick,
measure_index, measure_label, offset_ticks, beat_in_measure
```

Integer tick is the persisted identity. `(measure_label, beat_in_measure)` is a
projection for communication; labels can repeat or be non-numeric. `ScoreSpan`
is a non-empty half-open interval in one movement.

## Source Mappings and Gaps

A source mapping is a piecewise monotonic correspondence from unit-labelled
source coordinates (`midi_tick`, seconds, MusicXML divisions, or page
coordinates) to canonical score ticks. It records confidence and explicit
unmapped score spans. Editions can contain cuts, insertions, cues, or recognition
errors, so mappings are allowed to be partial; they are never assumed bijective.

In particular, the Oguri file's 240 PPQ describes its own MIDI performance
coordinate. The single fixed tempo event means expressive timing is encoded in
tick spacing. Therefore `oguri_tick / 240` is not a notated Chopin quarter-beat
and cannot define measure numbers. The reviewed semantic score defines score
time; `performance_map.json` relates Oguri coordinates to it.

Movement II's `performance_beat_map.machine.json` is the implemented dense
form of that relationship. Each anchor carries canonical tick, measure/beat,
native Oguri tick/seconds, confidence/evidence, and optional PDF page/system/x.
Unrecognized measures are listed explicitly. The mapping can be machine
reviewed while its target coordinate is still canonical; review state describes
the evidence, not a second coordinate system.

The mapping is compiled in dependency order. Ordered symbolic note/chord
correspondence assigns each reference event its canonical musical identity;
only then is the event's expressive MIDI tick/second attached and used to fit a
warp. Sparse human landmarks constrain ambiguous sequence windows. A duration,
tempo estimate, PDF x coordinate, or historical dense tick table cannot create
or relabel a measure boundary. See [Decision 0006](../decisions/0006-canonical-beat-evidence-fusion.md).

## Readiness

Loading validates strict Pydantic shapes, timeline identity/PPQ/movement,
monotonic mappings, mapping bounds, declared file existence, and optional file
hashes. Readiness is reported separately for progressive workflows:

- Sourcing: semantic score and valid canonical timeline.
- Rehearsal: sourcing plus display score/map and follower reference.
- Performance: rehearsal plus accompaniment, sections, and instrument map.

A structurally valid but incomplete bundle can be inspected and improved.
Runtime entry points can request performance readiness and fail before touching
hardware.

The temporary Movement 2 live-development path is explicitly different:
`runtime_projection.py` can project declared follower/accompaniment MIDI into a
legacy event view in MIDI performance coordinates. This view is always marked
noncanonical and not performance-ready; it exists to exercise causal plumbing,
not to satisfy or bypass the readiness contract. Public APIs resolve it by
`bundle_id` and `revision` through the server registry, never by a caller-owned
filesystem path.

Timeline references also carry a review state. A machine-draft timeline can be
loaded for source-bundling and cockpit development, but cannot satisfy sourcing
readiness until it is checked against semantic notation.

### DVC Presence vs. Bundle Readiness

The readiness levels above assume the bundle's declared files actually exist
on disk in the current checkout. A checkout that hasn't run `dvc pull` yet
has neither -- it has committed `<name>.dvc` pointer files with no local
readiness to report on. `paths.score_bundle_missing_artifacts(piece_id,
movement)` answers a narrower, purely operational question upstream of any of
this: walk every `*.dvc` pointer under the bundle directory (DVC's own
convention: `<name>.dvc` tracks `<name>`) and report which ones have no
corresponding real file yet. It is a read-only file-existence check with no
subprocess execution, exposed via `GET /api/scores/{movement}/health` and
surfaced as a Ready-face banner (rubato#101) so a missing `dvc pull` reads as
"N artifacts not pulled yet" instead of a PDF silently failing to load. It
says nothing about Pydantic validity, timeline identity, or any of the
readiness levels above -- a fully pulled bundle can still fail sourcing
readiness, and a bundle failing this check has not yet reached the point
where sourcing readiness is even checkable.

## Display-Layout Invariant

PDF page count need not equal the page count implied by an arbitrary MusicXML.
Page/system equality is required only when that exact MusicXML is declared as
layout evidence for that exact PDF. The existing `build_measure_boxes` helper
uses MusicXML print-layout breaks, so its local 98-page equality check is valid
for the paired Movement 1 engraving but cannot be generalized to the 107-page
reduction. A matching reduction MusicXML/OMR display map or reviewed manual
anchors can map the reduction independently.

## Links

- [Score Bundle Ingestion](score-bundle-ingestion.md)
- [System Design](../SYSTEM_DESIGN.md)
- [Decision 0005](../decisions/0005-score-bundle-v2-canonical-timeline.md)
- [Decision 0006](../decisions/0006-canonical-beat-evidence-fusion.md)
- [Oguri source notes](../sources/oguri-kunstderfuge-midi.md)
