# Source: Joseffy Two-Piano Reduction of Chopin Op. 11

## Source

- Local source: `~/Downloads/Chopin - Piano Concerto No 1 (reduction).pdf`
- IMSLP listing: https://imslp.org/wiki/Piano_Concerto_No.1,_Op.11_(Chopin,_Frederic)
- IMSLP file number: 112477
- Arranger: Rafael Joseffy
- Publisher: G. Schirmer, 1918, plate 25650

## Date Read

2026-07-12

## Why It Matters

This is Rubato's performer-facing display score for the Movement 2 MVP. The
two-piano layout keeps Chopin's solo part and a readable orchestral reduction
on the same page, making it much more useful at the piano than the 15-stave
full score.

The PDF is a UX and layout source, not the canonical musical timeline. Its
page/system/measure locations must be mapped to notation-derived score time;
they must not be inferred from the expressive Oguri MIDI tick grid.

## Key Facts

- The complete reduction is a 107-page, image-only PDF.
- Each page contains one approximately 600-dpi monochrome scan image.
- Movement 2 begins on PDF page 55 (printed page 53).
- Movement 3 begins on PDF page 70 (printed page 68).
- The Movement 2 display window is therefore PDF pages 55-69 inclusive.
- `scripts/extract_reduction_pages.py` produces the 15-page Movement 2
  artifact without adding musical semantics.
- The extracted PDF is DVC-managed at
  `data/scores/chopin_op11_movement_2/source/`.

## OMR Position

Audiveris is the first build-time OMR candidate because the source is a clean
printed scan and Audiveris can retain an editable `.omr` project and export
MusicXML. Recognition remains evidence that must be reviewed. The canonical
timeline is created only after structural validation of measures, meter, and
the Piano I/Piano II part assignment.

The July 12 pilot established a narrower, safer pipeline: run Audiveris only
through `GRID`, then reconstruct measure boxes from the barlines detected on
each staff. It found 126 sequential measure boxes across all 15 pages. The
geometry is useful immediately, but remains `review_state: machine`; the
generated 126-measure, 4/4 timeline is explicitly a machine draft and does not
make the bundle sourcing-, rehearsal-, or performance-ready.

Visual PDF QA sampled pages 1, 8, and 15 after overlaying the normalized boxes.
The rectangles follow the engraved barlines and system bounds, and the final
page's printed labels confirm measures 120-126. This validates the rendering
pipeline, not yet every musical correspondence.

Run the reproducible adapter with an installed official Audiveris application:

```bash
uv run python scripts/run_audiveris.py \
  data/scores/chopin_op11_movement_2/source/joseffy_reduction_movement2.pdf \
  data/scores/chopin_op11_movement_2/omr
```

Set `AUDIVERIS_BIN` or pass `--executable` when the launcher is not installed
in a standard location. The adapter defaults to fast structural recognition
and saves the `.omr` project; it is never imported by the live runtime. Pass
`--transcribe` only when a MusicXML draft is actually needed. The pilot's full
transcription reported many note/rhythm inconsistencies, so process success is
not musical validation.

Convert the structural project into inspectable JSON with:

```bash
uv run python scripts/build_omr_display_layout.py \
  data/scores/chopin_op11_movement_2/source/joseffy_reduction_movement2.grid.omr \
  data/scores/chopin_op11_movement_2/derived/display_layout.machine.json
```

Preserve scan-space layout evidence separately from exported MusicXML: the
MusicXML interchange format can lose the exact page geometry needed for the
cockpit overlay.

### July 18 Full-Recognition Experiment

Audiveris 5.11.0 was resumed from the committed GRID project and run through
`PAGE` for the complete 15-page Movement 2 PDF. This was materially more useful
than the earlier pilot, but it is not a clean semantic transcription:

- The `.omr` contains 3,957 detected noteheads, 3,186 head chords, and all 126
  previously identified measure stacks with scan-space bounds.
- Pages 1-13 and 15 reached `PAGE`. Page 14 failed in Audiveris's `LINKS` step
  with an internal null pointer after a chord was placed outside any containing
  measure; the project retained that page through `SYMBOLS`.
- The run logged 52 time inconsistencies, 16 chords without a time offset,
  seven measure stacks without a correct rhythm, and a missing inherited time
  target in every system. English text OCR was unavailable locally, although
  that is secondary to note alignment.
- Audiveris exported pages 1-13 as a 113-measure MusicXML draft with 2,525
  Piano I note-array rows and 947 Piano II rows. Exporting valid page groups
  separately is required because the failed page splits the book and otherwise
  the final group overwrites the first export filename.
- Measure 17 demonstrates why rhythm is not authoritative: Audiveris assigned
  it a duration of 35/16 and reported incompatible cross-staff offsets. Its
  notehead/chord x-positions remain useful. **This is not an isolated case**:
  `score_localization.py irregular-measures` on this export flags 63 of the
  113 exported measures (P1) as having a duration inconsistent with 4/4,
  spread from m.8 onward, not just in the cadenza-adjacent mm.93-113 window an
  earlier reading of this doc implied. Treat Audiveris's own duration/rhythm
  as unreliable everywhere in this export, not just locally -- pitch, order,
  and x-position evidence is what alignment should lean on.
- Offline note alignment is nevertheless promising. Parangonar's
  `DualDTWNoteMatcher` matched 74.3% of the exported Piano I notes to the Oguri
  solo globally. Measures 14-18 matched at 91-100% score-note coverage, and
  measure 17's matched x-position/onset sequence had Spearman correlation
  0.997. The geometry plus pitch/order evidence is therefore likely sufficient
  for dense visual cursor anchors, provided canonical rhythm comes from a
  reviewed timeline rather than raw Audiveris durations.

The pages 1–13 `.mxl` is now DVC-tracked as
`source/joseffy_reduction_movement2.audiveris.mxl` in bundle revision
`movement2-symbolic-path-v4`. Its SHA-256 and machine provenance are declared in
`bundle.yaml`. `scripts/build_movement2_beat_map.py` deterministically combines
this semantic/geometry evidence with Oguri MIDI through sparse
constraint-bounded symbolic alignment. (Historical: this originally produced
448 anchors for mm.1-112, with mm.113-126 an explicit gap rather than a
fabricated continuation. See "August 3-4" below -- the gap is now filled
through m.125.)

The schema-v2 `display_layout.machine.json` stores raw OMR rectangles as
zero-based `box_index`; it does not call unjoined geometry a score measure. The
separate `display_map.machine.json` joins those boxes to canonical zero-based
`measure_index` plus the printed one-based `measure_label`. Beat fusion and the
runtime consume only that canonical display map.

The resulting m.17 anchors preserve the expressive source timings at native
ticks 42,703, 44,653, 45,109, and 45,550 before m.18 at 46,242. This is the
important acceptance result: Audiveris does not supply authoritative rhythm,
but its pitch/order/x evidence is sufficient to find score beats inside the
reference performance when constrained by known measure windows.

### August 3 — Page-14 Crash Root-Caused and Repaired

The page-14 failure that capped the export at m.113 was traced to a single
spurious glyph, not the triplets and not the rhythm engine. On **sheet 14,
measure 118, staff 9** (the dense 16th-triplet run staff, bottom system),
Audiveris recognized one notehead twice — heads `#3708` and `#6784`, byte-
identical at `bounds=(533,2302,28,23)` — and built a **phantom chord `#6783`**
over the pair joined by a **zero-length stem `#6428`** (`bounds=(0,0,0,0)`,
`<median>` a point at `(534,2321)`). The `(0,0)` location put the chord in no
measure, and `Measure.getClefBefore(null)` threw, aborting sheet 14 at `PAGE`.
Sheet 15's `check time signatures` errors were pure cascade (`Time value not
yet available in sheet#14`). The real heads were already owned by a **healthy
twin chord `#6785`** (valid stem `#6407`), so no note is lost by discarding the
phantom.

**Proven repair — scripted** (general procedure and rationale in the
[Score Localization skill](../../.agents/skills/score-localization/SKILL.md),
"Known OMR Defect: Duplicate-Head Phantom Chord"):

```bash
uv run python scripts/repair_audiveris_omr.py <transcribe-dir>/book.omr --output <out>
```

The tool auto-detects and removes `head-chord #6783`, `stem #6428`, and the five
`<relation>` blocks referencing either (`6783→6428` chord-stem, `6783→3708`
containment, `6784→6783` no-exclusion, `3708→6428` head-stem, `6784→6428`
no-exclusion) — leaving both duplicate heads bound to the healthy twin `#6785` —
then resumes the edited `.omr` through `PAGE` (no `-force`) and re-exports.
Requires the transcription to have used `-save -swap` (which
`scripts/run_audiveris.py --transcribe` does).

Result: a clean **126-measure** MusicXML (parts P1–P4, measures 1–126) with
real durations and pitches for mm.114-126. This makes m.114–126 beat
geometry recoverable from recognized note x-positions instead of even-margin
synthesis. Rhythm still is not treated as authoritative — the canonical
timeline governs — but the geometry gap is closed.

**Correction: mm.113-126 is not a piano-tacet passage.** Earlier notes here
and in [Score Coordinate Systems](../concepts/score-coordinate-systems.md)
called this a "closing tutti" on the assumption that the missing note-pass
measures meant the piano was silent. That was backwards -- it was the *OMR
crash* that produced no notes, not the music. The repaired export shows the
piano solo (P1) actively playing 14-52 notes per measure through m.125; only
m.126 itself is sparse (2 notes, likely a final sustained chord). Check actual
note counts (`measure_contents` in `scripts/score_localization.py`) before
inferring a passage is tacet from a gap in derived data.

### August 3-4 — Repair Actually Landed; Beat Map Rebuilt and Cross-Validated

The August 3 repair above was proven but not promoted: its commit message
says explicitly it "does not yet swap the DVC-tracked source MXL," and the
126-measure MusicXML it produced went to an ad-hoc `--output` scratch path
that was never kept. The committed
`source/joseffy_reduction_movement2.audiveris.mxl` silently stayed the old
113-measure, July 18 export for another day of work until this was noticed
and fixed on 2026-08-04.

Re-ran the full pipeline end to end (`run_audiveris.py --transcribe` then
`repair_audiveris_omr.py`) and this time **committed the result**: new
`joseffy_reduction_movement2.audiveris.mxl` (DVC, sha256 `9783fc37...`),
`bundle.yaml` updated (`page_scope: [1, 15]`), and
`performance_beat_map.machine.json` rebuilt via
`build_movement2_beat_map.py` -- 448 anchors (mm.1-112) -> 500 anchors
(mm.1-125; only m.126 stays unmapped).

Verified before promoting, so a future agent doesn't have to redo this
check: the irregular-measures set for mm.1-113 is byte-identical between the
old and new export (same 63 measures, same beats-per-measure values) -- this
was a faithful re-run of the same recognition, not a fresh regression. The
only new irregular measures are in mm.114-124 itself. And of the beat map's
original 448 anchors, only 3 moved at all after the rebuild (each <0.4s) --
the previously-covered span is unchanged.

**Independent verification of the new mm.113-125 anchors**: cross-validated
against `orchestra_oguri_alignment.machine.json` (a MuseScore orchestral
score aligned to Oguri with Parangonar -- independent of the Audiveris
pipeline entirely) to a median of well under 0.2s, as tight as the
pre-existing mm.1-112 agreement. This is real confirmation, not just
internal self-consistency, because the two sides share no common ancestor
per the [Score Localization skill](../../.agents/skills/score-localization/SKILL.md)'s
cardinal rule.

### August 9 — Orchestra-Led Boundary at mm.103–105

Eric's Data-timing audition identified a concrete one-beat boundary defect:
the click labelled m.104 beat 1 landed on the first orchestral G-sharp, which
the Joseffy Piano II staff assigns to m.103 beat 4. MIDI inspection confirmed
the identity independently: Violins I/II enter on G-sharp at native tick
`252985` (`527.052s`); the full G-sharp/D-sharp/F-sharp chord begins at
`529.454s`, matching m.104 beat 1. Subsequent orchestral attacks at `531.158s`,
`532.577s`, and `534.154s` match beats 2–4, and `536.275s` begins m.105.

The production map had put m.104 at `526.910s` with zero matched notes and
`structural_interpolation` evidence. Both local MuseScore-derived independent
alignments put its downbeat around `529.45–529.67s`. Root cause: after m.12 the
compiler used Piano I → solo exclusively, even in orchestra-led bars where the
solo line could not identify a downbeat. `build_movement2_beat_map.py` now uses
Piano II → orchestra pitch-class matches only to fill measures absent from the
stronger solo path. The rebuilt map places m.104 at `529.510s` and m.105 at
`536.275s`; both clear the cross-validation worklist. Eric's m.103 beat-4
identity is also stored as a machine-local reviewed human correction at exact
native tick `252985`.

### August 9 — Interior Beat Rhythm at mm.107 and 109

Eric later heard the same three-fast-clicks/long-tail failure at m.107 while
confirming that mm.105 and 109 sounded right. The old m.107 interior anchors
were ticks `262133`, `262502`, `262870`, and `263239`: just three exact Piano I
matches had pulled the first three intervals onto a rapid solo run. The guarded
Piano II route rejects that collapsed symbolic warp and evenly interpolates the
printed orchestral pulse between the independently agreed barlines. The rebuilt
ticks are `262133`, `262692`, `263252`, and `263811` (approximately `546.110`,
`547.275`, `548.442`, and `549.606s`). This changes only m.107.

Eric confirmed that m.109's barlines were correct but the audition click track
compressed its first three clicks and left a long empty tail. Inspection showed
that the map had taken all four interior beats from the rapid Piano I solo run:
ticks `266822`, `267353`, `267790`, `268170`. Piano II and the Oguri orchestra
instead agree on the slower orchestral pulse. The rebuilt anchors are ticks
`266822`, `267548`, `268074`, `268598` (approximately `555.879`, `557.392`,
`558.488`, `559.579s`). Nearby grouped orchestra attacks independently support
those locations at ticks `266802`, `267488`, `268087`, and `268593`; the attack
at `267897` is the notated subdivision inside beat 2, not another quarter-note
beat.

The compiler now marks reviewed mm.107 and 109 as reduction-owned for interior
beat rhythm, guarded by an independent check that the reduction and resolved
downbeats agree within half a source quarter note. This is deliberately not
auto-promoted across every coincident reduction match: those bars have not had
the same listening verdict, and m.54 demonstrates that displaced reduction
candidates exist. This separates two questions that can have different best
evidence in the same bar: which sound starts the measure, and which orchestral
attacks carry its printed beats.

### August 9 — Full-Run Pulse Review at mm.35, 87, and 93–94

The complete live run exposed three more bars with the same evidence-selection
failure. In m.35, solo evidence collapsed the final two click intervals, while
Piano II and the orchestra place beats 1–4 at native ticks `84874`, `85399`,
`85931`, and `86357`. In m.87 the old interior ticks `206784`, `207147`, and
`207855` made the quarter-note orchestra sound roughly an eighth late; the
reviewed pulse is `206422`, `207066`, `207589`, and `208305`. M.93 now uses
`220944`, `221551`, `222086`, and `222884`, resolving to the performer-verified
m.94 downbeat at `223861` rather than the former solo-derived `224128`.

These measures join mm.107 and 109 in the guarded reduction-owned rhythm set.
M.38 was deliberately not changed: its live trace placed actual orchestral
attacks roughly `-121..+77 ms` around the accepted piano beat, and its reduction
downbeat candidate disagreed with the resolved boundary by more than the
half-quarter admission guard.

## Caveats

- This edition may differ from the Oguri MIDI in measure numbering, cuts,
  ornament realization, or reduction details.
- OMR note accuracy is not assumed, especially in dense solo passagework.
- Large PDFs and `.omr` projects belong in DVC, not git.
- The timeline and beat map remain machine-reviewed. Full semantic readiness
  still requires rhythm repair; page-14/15 recognition is now recovered via
  the August 3 `.omr` repair and landed in the committed source/beat map on
  August 4 (see above).
- Beat-level x geometry is available for recognized measures; confidence and
  evidence type remain part of every anchor.
- **A "proven repair" is not a landed one.** The August 3 entry proved the
  fix worked but left the actual regenerated artifact in a scratch path,
  never `dvc add`ed or committed -- the bundle silently kept serving the old
  data for a full day of subsequent work. When a repair script produces a
  new artifact meant to replace a tracked one, promote and commit it in the
  same change, or the LOG/doc entry explaining the fix is not evidence that
  it's actually in effect. Check `git log --all -- <path>.dvc` before
  trusting that a documented fix is live.

## Related

- [Score Bundle Ingestion](../concepts/score-bundle-ingestion.md)
- [Rehearsal Take Coverage Design](../design/REHEARSAL_TAKE_COVERAGE_DESIGN.md)
- [Oguri / Kunst der Fuge MIDI](oguri-kunstderfuge-midi.md)
- [Parangonar](parangonar.md)
- [Vision and UX Design](../VISION_AND_UX_DESIGN.md)
