# Decision 0006: Canonical beat coordinates with evidence fusion

- Status: accepted for Movement II; symbolic-precedence amendment 2026-07-19
- Date: 2026-07-18

## Context

Movement II exposed a category error. Oguri MIDI ticks encode the expressive
timing of one performance; Audiveris rhythm is imperfect optical recognition;
and PDF x/y coordinates describe engraving. Treating any of those as score
time made the audio align while the cursor drifted, especially through the
large rubato in measure 17.

The performer needs stable musical locations—measure and beat—not a choice
between source clocks. The build also has unusually strong evidence: a
two-piano reduction PDF, full Audiveris MusicXML/geometry, an orchestra-plus-
piano MIDI, a separately extracted solo MIDI, recorded Yamaha MIDI takes, and
occasional high-signal piece knowledge such as the B-natural pickup in m. 53.

## Decision

Canonical integer `score_tick` at 960 PPQ is Rubato's sole musical coordinate.
Measure and beat are projections of that coordinate. Every other value is
evidence joined through an explicit mapping:

```text
PDF pixels ← Audiveris geometry ← MusicXML note/beat
                                  ↕ offline symbolic fusion
reference MIDI tick/seconds ───→ canonical score_tick ← performance MIDI
```

The offline bundle build emits `performance_beat_map.machine.json`, with one
anchor per recognized beat. Each anchor retains source MIDI tick/seconds,
canonical score tick, measure/beat, PDF x geometry, evidence kinds, confidence,
and explicit unmapped measures. Runtime code consumes this artifact; it never
runs Audiveris or whole-score alignment during rehearsal.

Evidence is fused in an explicit inference order:

1. sparse performer corrections and genuinely performer-verified landmarks
   constrain the possible monotonic path;
2. ordered Joseffy Piano I ↔ Oguri solo note/chord sequence alignment owns
   measure and beat identity inside those constraint windows;
3. Piano II reduction ↔ orchestra pitch-class/harmonic sequence alignment
   supplies evidence where the solo is absent;
4. structural interpolation fills only explicit symbolic gaps and is labeled
   with lower confidence;
5. elapsed time is derived from the resulting correspondence and is used for
   playback warping, interpolation inside one evidenced beat, and diagnostic
   plausibility—not to infer how many measures elapsed.

A dense table of historical MIDI ticks is not reviewed evidence merely because
it was once inspected. Such a table may be retained as a diagnostic comparison,
but it cannot override a contradictory symbolic path. This distinction matters
in rubato: timing communicates the notes expressively; it does not identify the
notes or barlines.

### Provenance of the superseded anchor table

The table was not inherited from a score publisher or produced by Audiveris.
It was created inside Rubato on 2026-07-16 in PR #109 as
`MOVEMENT_2_ANCHORS`. At that time the take aligner stored Oguri performance
ticks while the UI needed Joseffy measure labels. The first version contained
only five manually checked calibration points and described itself in code as
“provisional,” “small,” and “noncanonical.” Its legitimate purpose was a
temporary piecewise-linear display projection before the canonical beat-map
compiler existed.

As Yamaha tests exposed cursor errors, later fixes added explicit points for
m.12–23 and then for nearly every downbeat from m.24–53. Those points were
chosen by inspecting Oguri onset groups near expected barlines, engraving
barlines, and observed playback. In the first beat-fusion compiler on
2026-07-18, every table entry was imported as `REVIEWED_DOWNBEAT`. Because
reviewed values had precedence, the new automatic symbolic matcher could not
correct an incorrectly chosen onset. A temporary calibration mechanism had
quietly become the ground truth it was originally documented not to be.

The table embedded four assumptions that do not hold:

1. a plausible onset near an expected boundary is necessarily that downbeat;
2. dense manual onset selection is equivalent to ordered score/MIDI alignment;
3. expressive MIDI tick distance is reliable evidence of measure progression;
4. assigning the same confidence to each inspected point makes it reviewed
   musical evidence.

The m.46 failure made the distinction observable. The table called solitary
pitch 54 at native tick `107926` the m.46 downbeat and called the chord at
`109900` m.47. Audiveris identifies m.46's opening chord as `[47, 63, 71]`,
which matches the Oguri onset group at `109900..109915`. The old table was one
measure early because it named candidate timestamps without first proving
their symbolic identity.

`MOVEMENT_2_ANCHORS` is therefore removed. Its replacement is not another
dense table: a small set of performer-substantiated landmarks remains as hard
constraints, the ordered symbolic path fills the interior, and interpolation
is used only for explicit gaps with lower confidence. Piece-specific landmarks
such as the m.53 B-natural pickup remain labeled annotations rather than global
heuristics.

The build therefore follows this dependency order:

```text
canonical notation + OMR uncertainty
  → ordered symbolic score states (measure/beat, pitches, chord/onset groups)
  → constraint-bounded symbolic alignment against reference MIDI
  → confidence-bearing reference-MIDI ↔ canonical-beat anchors
  → per-take MIDI ↔ reference-MIDI alignment
  → performed-time warp and accompaniment render
  → cursor interpolation within one evidenced beat
```

Pitch/chord identity, bass and soprano contour, intervals, harmonic
progression, and event order are valid correspondence signals. Wall-clock or
source-tick distance may regularize an otherwise ambiguous choice but must not
create or relabel a measure boundary.

Interior symbolic warps are accepted only when all four beat durations remain
plausible relative to their enclosing symbolically identified downbeats.
Collapsed matches
(several score beats paired to one ornament or rolled chord) are retained only
as diagnostics and replaced by explicitly low-confidence structural beats.

The m. 53 B-natural is a Movement II annotation, not a global “B-natural means
pickup” heuristic. The m. 17 beat anchors preserve the reference performance's
large rubato; the cursor interpolates only within a single evidenced beat.

Take alignment remains MIDI-to-MIDI because it has the strongest pitch and
relative-order signal; performed timing is fitted only after the symbolic
matches are known. Its persisted v2 result converts matched reference ticks to
canonical ticks once and retains the reference ticks as diagnostics.
Coverage, review audio, cursor transport, and the PDF overlay then read the
same canonical timing map instead of independently projecting it.

### Runtime timing amendment (2026-07-21)

Canonical beat remains the sole musical identity, but audible scheduling may
retain a source-performance coordinate. Movement II's solo reference and
orchestra accompaniment are deterministic splits of the same Oguri MIDI, so
`source_performance_beat` is an exact join between them. The runtime follower
therefore emits both canonical beat and the matched shared reference beat.
Policy and the cursor consume canonical beat; accompaniment onset spacing and
note duration consume the shared reference coordinate, scaled uniformly by the
performer's metronome setting or tracked reference-beat period. This is a dual
coordinate representation, not dual ground truth: reference beat answers
"when in this source performance?", while canonical beat answers "which
musical measure and beat?"

Automatic mapping is the normal workflow. Human correction is an exceptional
in-app operation: freeze the currently audible source moment, select a printed
measure, and choose beat 1–4. That single sparse anchor is validated for
monotonicity, revisioned into the mapping identity, journaled, and optionally
requeues the affected take. No terminal or exhaustive measure labeling is
required.

## Consequences

- Audio and cursor share one timing map; source MIDI and PDF are no longer
  competing clocks.
- Audiveris is a reproducible offline build dependency and evidence source,
  not runtime truth.
- Machine confidence and mapping review state remain visible even though the
  coordinate itself is canonical.
- Measures 113–126 remain explicitly unmapped until page recognition is
  repaired; the system must not invent anchors there.
- Generalization to other movements means supplying evidence adapters and
  annotations, not changing the canonical coordinate model.

## Links

- [Score Bundle Contract](../concepts/score-bundle-contract.md)
- [Score Bundle Ingestion](../concepts/score-bundle-ingestion.md)
- [Decision 0005](0005-score-bundle-v2-canonical-timeline.md)
- [Joseffy source notes](../sources/joseffy-reduction.md)
