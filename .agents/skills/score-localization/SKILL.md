# Score Localization Skill

Use this skill when establishing or repairing the **canonical measure/beat
coordinate system** for a score bundle: deciding what bar 1 beat 1 means, which
notated bar every performance event belongs to, and where each bar is drawn on
a page.

Everything downstream — the follower, coverage, anchors, the cursor, rehearsal
takes, the live scheduler — is expressed in canonical ticks. If localization is
wrong, every one of those is wrong in a way that still looks self-consistent.
**Localization is a precursor to all rehearsal and performance workflows, not a
step inside them.**

## Read First

- [Score Bundle Contract](../../../docs/concepts/score-bundle-contract.md)
- [Score Bundle Ingestion](../../../docs/concepts/score-bundle-ingestion.md)
- [Decision 0006](../../../docs/decisions/0006-canonical-beat-evidence-fusion.md)
- [Parangonar](../../../docs/sources/parangonar.md)

## The Cardinal Rule

**Never validate a derived artifact against another artifact derived from the
same source.** Rubato shipped a beat map that was a full measure wrong while
reporting `confidence 0.92` with 18 "exact symbolic matches", and every check
passed, because:

- solo events and orchestra events both flow through the same warp, so they
  agree with each other no matter how wrong the warp is;
- searching for "canonical bar N's pitches" inside the recording that bar N was
  derived from will always succeed;
- per-measure matchers that receive a tick window and refine *within* it cannot
  detect that the window is the wrong bar.

A check only counts if its two sides have **independent provenance**. See
"Independent Evidence Ladder" below.

## Ground Truth, Ranked

Prefer the highest available rung. Record which rung each fact came from.

1. **Publisher-typeset symbolic data** (MuseScore/Finale MusicXML with matching
   MIDI + PDF exported from one source). No OMR, and the MIDI/score
   correspondence is exact by construction — use it to validate a matcher
   before pointing that matcher at a real performance.
2. **Printed bar numbers and rehearsal marks** on a scan. Read, not inferred.
   Full scores usually number every system; solo/reduction editions often
   number nothing.
3. **Human confirmation** — the performer listens for one second and names the
   bar. Outranks every inference. Store it (see "Human Annotations").
4. **OMR layout pass** (Audiveris `.grid.omr`): staves, systems, barlines with
   pixel coordinates. In practice reliable.
5. **OMR note pass** (Audiveris `.mxl`): pitches per measure. Merges bars in
   dense/unbarred passages — its *measure numbering* is not trustworthy even
   when its pitches are.
6. **Symbolic-to-performance alignment** (Parangonar). Good, but it is
   inference; validate it against rungs 1–3.

## Independent Evidence Ladder

To assert "notated bar N is at performance time T", combine at least two of:

- pitch-content fingerprint of bar N from a **non-OMR** score vs the recording;
- printed bar number on a scan whose geometry is joined to the same bar;
- the performer confirming by ear;
- an alignment produced by a matcher that was **first validated** on a
  score/MIDI pair with known-exact correspondence.

Distinctive content pins bars cheaply. A pitch class that occurs in only one bar
of a neighbourhood (a chromatic outlier, an extreme register) discriminates
better than a whole-bar match of ordinary material, because sequential passages
match their neighbours well. This is how a human does it by ear, and it is the
method to automate.

## Tools Available (all already installed)

| Package | Version | Use |
|---|---|---|
| `parangonar` | 3.3.2 | Offline symbolic alignment. `DualDTWNoteMatcher` = global score↔performance note matching; a whole-bar offset cannot survive a global DTW. `AnchorPointNoteMatcher` accepts externally supplied anchors — this is how human annotations enter the alignment. |
| `partitura` | 1.9.0 | MusicXML/MIDI loading, score objects, alignment I/O. Parangonar's substrate. |
| `matchmaker` | 0.3.0 | **Online** following only. Not an alignment tool. |
| `pymupdf` | — | PDF rasterization and geometry. Not a dependency of the server; keep it to offline scripts. |
| `mido` | — | Raw MIDI track/tempo handling. |

Audiveris is invoked out-of-process (`scripts/run_audiveris.py`). Its `.omr`
output is a zip of `sheet#N/sheet#N.xml`; barlines are `inters` referenced by
`<staff><barlines>id id id</barlines>`, with geometry in `median/p1,p2`. A
`.grid.omr` is the **layout step only** — it has no note heads. A full
transcription run is slow (~1 hour) but yields layout and notes in one
self-consistent artifact; prefer it when numbering matters.

**Do not hand-roll pixel barline detection.** It was tried and produced 238
"barlines" against a true 126 by picking up note stems. Audiveris's own grid
pass already gives correct barlines; use it.

**Matcher choice matters on OMR-noisy input.**
`scripts/align_score_to_performance.py` defaults to `DualDTWNoteMatcher` for
this reason: on a clean publisher-typeset score, `AutomaticNoteMatcher` and
`DualDTWNoteMatcher` perform similarly, but pointed at an Audiveris export,
`AutomaticNoteMatcher` was measured to drift 60-85s of accumulated
disagreement with a known-good beat map by silently losing the thread with no
global constraint to catch it — while `DualDTWNoteMatcher` on the identical
input held 0.04s median disagreement, despite a *lower* raw match rate. A
higher match percentage is not evidence of a better alignment; a drifting
matcher can still report a plausible-looking match rate while every measure
past the drift point is wrong. See docs/sources/parangonar.md, "Matcher
Choice Matters on OMR Input," for the full numbers.

**Two API gotchas when calling parangonar matchers directly** (both already
handled inside `align_score_to_performance.py`, but relevant if you're
scripting against parangonar yourself): `partitura.Score.note_array()`
rejects parts with differing `<divisions>` values, which Audiveris emits
legitimately per measure/system — build each part's `note_array()`
separately and concatenate instead of asking partitura to combine them.
`DualDTWNoteMatcher` additionally requires `include_grace_notes=True` on both
note arrays (it needs an `is_grace` field the default omits).

**The MuseScore ground-truth files are local-only, not in git or DVC.**
`source/musescore_reduction_movement2.mxl` and
`source/musescore_orchestra_movement2.mxl` (used to produce
`musescore_oguri_alignment.machine.json` / `orchestra_oguri_alignment.machine.json`,
the independent cross-check for the Audiveris-derived beat map) are
gitignored and live only on Eric's machine — publisher-licensed content isn't
committed. A fresh worktree or clone will not have them. If you need to
re-derive or extend the cross-check, ask Eric for copies rather than
searching git/DVC history for them.

## Workflow

1. **Inventory the sources.** Put every artifact for a movement in the bundle's
   `source/`. Multiple PDFs, MIDIs and MusicXMLs of the same movement are an
   asset, not clutter — they are the independent provenance the ladder needs.
2. **Pick the canonical score** by the ranking above. Prefer publisher symbolic
   data. Record the choice and why.
3. **Fix the measure grid** from the canonical score. Reconcile bar *counts*
   across sources before anything else — and note that matching totals prove
   nothing, since compensating errors cancel. Walk printed bar numbers at every
   system start and find where the delta between sources steps.
4. **Validate the matcher on a known-exact pair** (publisher MusicXML vs its own
   exported MIDI) before trusting it on a real performance.
5. **Align the performance** to the canonical score with Parangonar, seeding
   human anchors where they exist.
6. **Cross-check with an independent path** — e.g. align the orchestral tracks
   to a full score as well as the solo line to a reduction. Agreement is
   evidence; disagreement is a question for the performer, not a tiebreak to
   decide silently.
7. **Join page geometry last.** Display boxes carry both an x-range and a tick
   range; those must point at the same bar. This join is a bar-count problem,
   not a music-alignment problem.
8. **Run the health checks** (`aimusic.accompaniment.score_health`) and record
   remaining findings rather than suppressing them.

## Known Source Defect: Split Bars Across System Breaks

A bar that wraps across a system or page break is engraved as two partial bars.
Notation software should emit that as **one** measure (or mark the continuation
`implicit="yes"`, as for a pickup). Hand-entered scores frequently do not, and
export it as **two numbered measures**, each shorter than the time signature.

Rubato's MuseScore source has exactly this: `m.106` and `m.107` are 2 beats each
where every other bar is 4. Its 127 measures are really 126, matching the PWM
full score's printed last bar and the Joseffy engraving.

**Parangonar does not correct this and cannot.** It matches note sequences and
returns `(score_beat, seconds)` pairs; it has no notion of a measure. The
alignment is unaffected -- the damage appears only when `score_beat` is
converted to a measure number, where every bar after the split is labelled one
too high. That makes it invisible to alignment-quality metrics: a 97.6% match
against a real performance sat on top of a numbering that was wrong from m.107
onward, and comparing a *correct* beat map against it manufactured a convincing
"one bar late" error across mm.107-112 that did not exist.

**Detect** with `score_localization.py irregular-measures <score>`: any measure
whose summed duration differs from its time signature, excluding those marked
implicit. Two adjacent half-bars are the signature.

**Correct** by preference:

1. Fix the source: merge the pair into one measure. Cleanest, and everything
   downstream inherits it.
2. Failing that, carry a renumbering map (real bar N -> source measure N for
   N <= split, N+1 after) and apply it wherever source measure numbers are read.

Always reconcile the corrected count against a source that *prints* bar numbers
before trusting it. Totals alone prove nothing -- compensating errors cancel.

## Known OMR Defect: Duplicate-Head Phantom Chord Crashes a Page

A full Audiveris transcription can abort **export for a whole sheet and every
sheet after it** because of one spurious glyph, producing a clean cliff in the
MusicXML rather than one or two bad bars. The Joseffy Movement 2 run stopped at
m.113; measures 114-126 were missing entirely. It looked like a hard passage
(untagged 16th-note triplets) confusing the rhythm engine. **It was not.** The
triplets only produce `INFO` "time inconsistency" notes that Audiveris exports
straight through (earlier sheets with the same triplets exported fine).

**Root cause.** In a dense passage Audiveris can recognize one notehead twice
(two `<head>` inters at *identical* bounds) and build a **phantom chord** over
the pair with a **zero-length stem** -- median `p1 == p2`, `bounds` at `(0,0)`.
Because the chord's location is `(0,0)` it belongs to no measure column, and
`Measure.getClefBefore` dereferences that null measure and throws. The
exception aborts the sheet at the `PAGE` step, so nothing exports. Later sheets
then fail with `Time value not yet available in sheet#N` and a cascade of
`MeasureFixer ... please check time signatures` -- **all downstream of the one
crash**, not independent problems. Fix the crash and they resolve.

This is an Audiveris bug (a duplicate glyph, plus an un-guarded null
dereference), not a bad engraving. The printed bar is correct.

**Detect.** After a `-transcribe` run, grep the Audiveris log:

    grep -E "No containing measure|Error processing stub|reaching step PAGE" run.log

`No containing measure for HeadChordInter#<id>` on the first failing sheet names
the phantom chord. Only that first sheet throws `Error processing stub` / an
NPE; every later sheet shows only `WARN`-level cascade. In the sheet's
`sheet#N/sheet#N.xml` (inside the `.omr` zip), the offending chord and its stem
carry `<bounds x="0" y="0" .../>` and the stem's `<median>` is a point.

**Correct (proven, GUI-free, scripted).** Because only the recognized
*geometry* is wrong -- the real heads survive on a healthy twin chord -- you do
not need to fix the rhythm or re-recognize anything. The whole repair is baked
into a CLI so none of the flags below have to be rediscovered:

    # 1. Transcribe with -save -swap (audiveris_command already does), which
    #    persists per-sheet SIG into the .omr. The run still fails at PAGE.
    uv run python scripts/run_audiveris.py <pdf> <dir> --transcribe
    # 2+3. Detect & delete the phantom chord + zero-size stem + their relations,
    #      then resume the edited .omr through PAGE and re-export.
    uv run python scripts/repair_audiveris_omr.py <dir>/<book>.omr --output <out>

`scripts/repair_audiveris_omr.py` (backed by
`aimusic.accompaniment.audiveris.repair_phantom_chord_omr` /
`repair_and_reexport_omr`) removes every degenerate `head-chord` (bounds at the
page origin), every zero-size `stem`, and all `<relation>` edges referencing
them, then re-exports. The pitfalls it encodes, in case you ever do it by hand:

- `-swap` is essential at transcribe time, or the `.omr` holds only `book.xml`
  with no sheet SIG to edit.
- Remove the chord **and** its orphaned zero-size stem; deleting only the chord
  moves the NPE from `getClefBefore` to `getMeasure() because "chord" is null`.
- Re-export resumes from the `.omr` with **no `-force`** (it re-runs at `LINKS`
  and keeps the edit); `-force` re-runs `HEADS` and re-detects the duplicate.

A clean run exports every measure (Joseffy: 1-126, parts P1-P4). The edited
`.omr`/MusicXML is a **DERIVED** artifact: the repair is deterministic and
idempotent, so re-running the script reproduces it rather than silently dropping
it. The healthy alternative is still the Audiveris GUI (delete the duplicate
head, re-export); the script is the automatable equivalent.

Piece-specific ids and the exact blocks removed for Joseffy Movement 2 are in
[Joseffy reduction source notes](../../../docs/sources/joseffy-reduction.md).

## Provenance Graph

Classify every artifact before reasoning about it. The class determines whether
it can be trusted, whether it can be re-derived, and what it invalidates.

**SOURCE** -- acquired externally, never edited in place. If one is wrong, the
answer is to acquire a better one, not to patch it.

    Joseffy PDF                 the engraving the performer reads
    Oguri MIDI                  a human performance; carries timing, NOT measures
    MuseScore MusicXML          symbolic: pitches, measures, beats (orchestra + reduction)
    MuseScore MIDI              rendered from its own MusicXML -> an exact pair
    PWM full score PDF          printed bar numbers and rehearsal marks

**HUMAN** -- the performer's observations and annotations. Outranks every
inference, permanently. Store what they said (measure + beat), never the
resolved canonical tick, so re-deriving the map cannot silently relocate the
annotation placed to fix that very map.

**DERIVED** -- recomputable from sources, and therefore disposable. Never hand-
edit a derived artifact without recording why; the next rebuild discards it.

    Joseffy PDF --Audiveris--> grid.omr --> display_map (bar -> pixels)
                                        \-> timeline    (measure grid)

    MusicXML + Oguri --Parangonar--> alignment (score_beat <-> seconds)
                                            \-> beat map (measure -> seconds)

    display_map (X) timeline  on MEASURE INDEX   <-- the fragile join

## Invalidation

When evidence questions a node, everything downstream of it is suspect and must
be re-derived. Work down the column:

| questioned | re-derive | re-check |
|---|---|---|
| Joseffy PDF or Audiveris output | display_map, timeline | `health`, `annotate` + performer walk-through |
| MusicXML measure grid | alignment, beat map | `irregular-measures`, then `cross-validate` |
| Oguri MIDI | alignment, beat map | `--validate` first, to separate a bad recording from a bad matcher |
| the measure-index join | display_map labels only | `annotate` and have the performer count |
| a human annotation | nothing -- it is a root | ask again; do not out-argue it |

**Agreement between derived nodes is evidence only when their failure modes
are independent, not merely their inputs.** Two alignments built from different
scores against the same recording agreed with each other to 0.25s and were both
2.2s wrong at one bar, because the difficulty lived in the performance -- a
sparse chromatic passage where both interpolated across the same gap. The
performer's fingerprint settled it against both.

**Two derived nodes that share a parent cannot validate each other.** The
display map and the timeline both come from the Audiveris grid, so they agreed
perfectly while both were missing a bar. Cross-checks are only meaningful
between nodes whose paths to the sources are disjoint -- which is why a
non-OMR symbolic source earns its place, and why two independent alignments
find each other's false positives.

**A self-consistent aggregate is not evidence.** The box count reconciled at
126 across three artifacts while containing two errors -- a dropped bar and a
spurious fragment -- that cancelled. Totals, counts and medians can all be
right for the wrong reason. Prefer a check that localizes (which bar?) over one
that aggregates (how many?).

## Why One Skill

Ingestion, fusion, audit and correction are one skill, not four, because they
interleave rather than sequence: an audit finding drives a correction, which
drives re-fusion, which changes what the audit says. Split into separate
skills, an agent entering at "audit" would not read the cardinal rule -- which
is the single thing most likely to make it waste a day.

What *is* split out is every deterministic operation. Almost every error in the
investigation that produced this skill came from doing analysis ad-hoc in prose;
everything scripted was reliable. **If an operation has a definite answer, it
belongs in `scripts/score_localization.py`, not in an agent's reasoning.**

Enter at the phase you need:

| phase | do this |
|---|---|
| ingest | `inventory`; record provenance and rights in the source manifest |
| fuse | `align_score_to_performance.py <score> [performance] --validate`, then without it |
| audit | `health`, `irregular-measures`, `cross-validate` |
| debug | `measure-content`, `fingerprint`, `find-bar` |
| ask the human | `annotate` -- never hand-roll the artifact |
| validate & correct | `worklist`, then audition and `snap`/`apply-anchors` -- see "Validate & Correct an Existing Beat Map" below |

## Involving the Performer

The performer resolves in seconds what inference gets wrong for hours. Their
direct observations were right every time; aggregate reasoning that contradicted
them was wrong every time. **When a human observation and a derived count
disagree, the count is wrong.** A count can be self-consistent because two
errors cancel -- that is exactly how a dropped bar and a spurious box hid each
other at a total of 126.

Ask well:

- Use `annotate`. Every marker referenced must be drawn on the page being sent.
- Write to a new filename each time; a browser tab will serve a cached old one.
- Render and *look at* the artifact before sending it. Labels have shipped
  invisible more than once.
- Ask for a *judgement*, not a measurement: "which lettered system contains
  this bar", not "which measure number is this".
- Match instrument to question. Timing questions need listening, not looking --
  a chromatic passage looks identical a half-bar either side, so no visual
  comparison can settle where a bar sits in a recording.

## Every Source Is Noisy

No artifact in a bundle is clean. Over one investigation: the OMR dropped a bar
and invented another; the publisher-typeset MusicXML split one bar in two and
put orchestral notes in a measure the engraving leaves as rests; the reference
recording enters an orchestral cue a beat later than notated and realises a
cadenza with only 0.32 pitch-sequence similarity to the score. None of these
were detectable from the artifact alone.

So human disambiguation is not a failure of the tooling, it is a permanent part
of the system. The goal is not to eliminate it but to **shrink its surface area
and its friction**.

**Shrink the surface area** -- reduce how many decisions need a human:

- Cross-check independent sources so a disagreement is *localized* to a few
  bars instead of casting doubt on everything. Two alignments cut a nine-bar
  defect list to three; a third source would have cut it further.
- Compute everything computable first (`describe`). Most questions worth asking
  a performer are already answered in the score.
- Fail loudly and specifically. A check that names one bar costs one question;
  a check that says "something is wrong" costs a day.

**Shrink the friction** -- make each remaining decision cheap:

- Show the annotated artifact; never ask about music the performer cannot see.
- Ask for a judgement, not a measurement: "which system contains this bar",
  not "what measure number is this".
- Prefer multiple choice to free description, and batch questions rather than
  round-tripping.

**Build detectors that tolerate noise.** Exact predicates break on a single bad
note, and every source has bad notes:

    brittle                          robust
    orchestra has 0 notes        ->  orchestra has < ~10% of a neighbour's
    pitch-class sets are equal   ->  best match beats runner-up by a margin
    this exact pitch triggers    ->  a simultaneity, or any of several cues
    counts agree                 ->  counts agree AND geometry is covered

A detector that a one-note transcription error can flip is not a detector. The
tacet test above failed exactly that way: a stray note in a measure the
engraving leaves empty flipped it from "tacet" to "sounding".

## Measure Fingerprinting: Compute Before You Ask

**Run `describe` over the region before asking the performer anything.** Every
fingerprint that resolved bars in the investigation behind this skill was
computable from the MusicXML and was not computed: "no piano in this measure",
"the bass rises G# A# B# C# D# into the next bar", "this whole bar is an E
dominant seventh". A performer was asked to supply what a query could answer.

Two failures to avoid, both observed:

- **Using the score only to verify, never to interrogate.** Asking "does bar N
  match X?" can only confirm a hypothesis you already hold. Ask "what *is* bar
  N?" and "which bars near here are unusual?" first.
- **Escalating to a human instead of changing instrument.** Pitch-class-set
  overlap saturates in chromatic music -- one bar here contains all twelve
  classes, so a half-bar shift scores identically. That is a signal to change
  representation, not to ask someone.

### Feature bank

Precompute per measure, then reason over the table. Absence is the strongest
and cheapest feature: a tacet part localizes a bar in a recording far more
reliably than pitch content.

    tacet parts              which instruments are silent (strongest signal)
    entries and exits        a part that starts or stops here
    harmony                  chord/roman-numeral label for the bar
    melodic contour          top-voice pitch sequence
    bass contour             lowest-voice sequence; scalar rises are distinctive
    unique pitch classes     present here, absent from both neighbours
    register extremes        highest/lowest note; widest simultaneity
    onset density            filigree vs sustained; gestures per beat
    self-similarity          which nearby bars this resembles (aliasing risk)

Rank bars by how *unlike* their neighbours they are, and use the discriminating
feature rather than whole-bar content. That is what a musician does by ear.

`music21` supplies the theory primitives and is declared in the `analysis`
extra. It is offline tooling: the live server must never import it, so an
unmaintained analysis library cannot affect a performance.

Known limitation: naming the pitch-class set of a whole bar gives set-theory
labels ("forte class 7-29A") for chromatic writing, because passing tones are
included. A bar a performer would call "E dominant 7" does not name itself that
way from its raw PC set. Real harmonic labelling needs music21 roman-numeral
analysis over a reduction, or per-beat chord extraction -- not yet built. Until
then, treat tacet parts, contour and unique-pitch-classes as the reliable
features and harmony labels as a hint.

### What still needs the performer

Compute everything above first, then ask only for what genuinely requires eyes
or ears on the physical artifact:

| needs the performer | do not ask -- compute it |
|---|---|
| does this box sit on that printed bar | which parts are tacet |
| does this sound right / in time | what harmony a bar spells |
| is this mark a barline or a stem | melodic and bass contour |
| which printed system is this | what is unique vs neighbours |

### Anchors: One Annotation, Derived Actions

An anchor is a symbolic human annotation meaning exactly one thing:

> **"Here is where I am."**

That is the only thing a performer reliably knows, and the only thing they
should have to express. Everything else is derived from the score.

    ANCHOR (marked by the human)
        a score position (measure + beat)  +  what they play there

    INTERPRETATION (derived, never annotated)
        always:  correct the follower's position to this beat
        if the score has orchestra notes at or near this beat:
                 also fire them reactively, so they land with the performer

There is no anchor "type" for the human to choose. A cadenza anchor and a
downbeat-sync anchor are the same annotation; they differ only because the
score happens to have orchestra notes at one and not the other.

**Two inversions this corrects**, both present in `anchors.py` as written:

- *Dropping an anchor that has no orchestra chord near it* is backwards. Such
  an anchor still corrects position; it simply makes no sound. This rule alone
  silently discards every anchor in a tacet passage -- which is exactly where
  a lost follower most needs them.
- *Gating an anchor on agreement with the current follower position* is
  self-defeating. The anchor exists to **assert** position; validating it
  against the estimate it overrides inverts the authority. It also deadlocks: a
  frozen position is far from every future anchor, so nothing can fire, so the
  position stays frozen. Observed live -- a follower sat at one beat for 16
  seconds through an entire cadenza.

  Human annotation outranks inference. That principle applies here too.

**Position arms the chain; sequence runs it.** Anchors form *chains* with a
defined extent -- a run of consecutive anchors that begins, advances, and ends.
Position is used once, coarsely, to arm the chain, and never again:

    ARM     the main follower comes within ~2 beats of the chain's first anchor
    RUN     state: waiting for anchor k
              trigger for k        -> fire, set position to k's beat, wait for k+1
              trigger for k+1/k+2  -> the performer skipped; fire and resync forward
              trigger for k-1      -> already passed; ignore
    END     past the last anchor, hand a corrected position back to the follower

Arming on proximity uses position while it is still trustworthy -- before the
passage that breaks the follower. Once armed the chain is self-sustaining, so a
follower that freezes mid-chain cannot stall it. That is the deadlock's cure:
the gate happens once, early, when it can still succeed.

**A running chain is a small independent follower.** While it owns the passage
the HMM's position is not consulted, which frees it to use signals of its own:

    pitch          the trigger note or simultaneity
    order          only anchors k..k+2 are live
    elapsed time   anchor k fired 1.2 s ago and the beat period is ~1.2 s,
                   so k+1 is due now -- a prior that separates a repeated
                   bass pitch from the same pitch three beats later
    region         armed only inside the marked passage

None of these need a position estimate, and together they are far more robust
in a free passage than note-sequence matching, which is what the main follower
is doing when it fails. The chain is not a patch on the follower; it is a
different, simpler estimator for the passages the follower cannot serve.

**Between anchors, ignore the notes.** In an anchored passage the performer
marks one anchor per beat, and there is usually a note that lands *on* the
beat; everything between two beats is free. That has two consequences:

- **The trigger is the on-beat note, never an interpolating one.** Ornaments
  and passing figures between beats must not arm anything. The score says which
  note carries the beat; isolated rehearsal takes confirm which one the
  performer actually lands on.
- **Notes between anchors are not position evidence.** Position advances by
  interpolation from anchor k to anchor k+1, and incoming notes in between are
  observed but not used to correct it. Treat them as evidence and rubato reads
  as drift; ignore them and rubato is free at no cost.

The live beat period comes from measured anchor-to-anchor intervals, not from
individual note timings -- which is also the interval the elapsed-time prior
should be built on.

This is why an anchored passage is *more* robust than a followed one, not less.
The follower is trying to match every note in a passage where note order and
ornamentation vary between performances; the chain only has to recognise one
note per beat and may ignore the rest.

**Deriving triggers from demonstration.** Do not ask the performer to name
pitches. Have them play the anchored passage in isolation, no accompaniment,
two or three times, as they would actually play it. Then:

    invariant across takes  -> a reliable trigger
    present in some takes   -> an ornament; never a trigger
    spread across takes     -> the timing tolerance, measured rather than guessed

One caveat: a free passage rehearsed against a click may be played more strictly
than it will be performed. Pitch invariants survive that, timing tolerances do
not -- take those from a full performance take instead.

**Scale the design for sparse anchors.** Dense per-beat anchoring is the
exception: across a whole movement expect only two or three measures to need
every beat marked, in genuinely ambiguous passages. The common case is a
handful of isolated anchors. Do not build machinery that assumes a dense chain,
and do not make the sparse case pay for the dense one.

Where dense anchoring *is* needed, propose the anchors rather than asking for
twenty clicks: the score supplies the beat grid and the lowest note of each
beat's chord, so the performer confirms a generated set instead of authoring it.

### The Interaction Loop

Ground truth about the engraving comes from the performer reading it. They can
only describe what they can point at, so **never ask a question about the music
without first showing the annotated music.** The loop is:

1. **Annotate** the engraving with symbols -- measure boxes and numbers, or
   letters where numbering is what is in doubt (`annotate`).
2. **Show** it: write to a new filename, open it in their browser, and render
   the artifact yourself to confirm the markers are visible before sending.
3. **They describe** what they see using those symbols: "the box labelled m.96
   has no A#", "m.100 is the first bar of system G", "this box is a sliver with
   no notes in it".
4. **You search the data** for what they described, and report where it occurs.

The symbols are a shared vocabulary that has to exist before the conversation
can happen. Referring to a marker that is not drawn on the page they are
holding -- or to one from a previous version of the file -- costs a whole round
trip. So does asking for a measure number when the engraving prints none.

Ask for the *distinctive* thing, not a full description: a pitch that occurs in
only one bar nearby, an extreme register, a unique rhythmic figure. That is how
a musician discriminates, and it is far more reliable than comparing whole-bar
content, which in chromatic music matches its neighbours almost as well.

## Human Annotations

Store what the performer said, not what it was resolved to.

- Coordinate is **measure + beat**, 1-based, as printed. No score anywhere
  prints a global beat offset from bar 1; do not ask a human to think in one.
- Canonical ticks are a **derived** field, recomputed whenever the mapping
  changes. Storing ticks as primary means re-deriving the map silently
  relocates the very anchors placed to fix it.
- Qualify by edition only if bar numbering actually differs between sources.
  Verify before adding the field; two editions of one work usually agree.
- A human annotation outranks inference permanently. Mark it `reviewed` and
  never let a re-run overwrite it.

## Failure Signatures

- *High confidence and wrong by a whole bar* — a windowed matcher. Look for
  code that filters performance notes to an assumed tick range before matching.
- *Displayed measure numbers look right, audio is a bar off* — geometry and
  tick assignment disagree; both artifacts validate individually.
- *Totals agree but labels drift mid-piece* — compensating errors, one bar
  merged and another split. Aggregate counts cannot detect this.
- *A region positioned by `structural_interpolation`* — the matcher gave up
  there; position is inherited from neighbours, not measured.
- *Cadenzas and unbarred passages* — the solo staff may carry no barlines while
  the accompaniment does. OMR passes disagree here first.
- *A derived artifact is missing evidence a fix commit says it should have* —
  check whether the fix actually got promoted, not just proven. A commit that
  demonstrates a repair works (produces a corrected file to an ad-hoc
  `--output` path, or similar) is not the same as one that `dvc add`s /
  commits the artifact that downstream code actually reads. Run
  `git log --all -- <path>.dvc` (or `git log --all -- <path>` for a
  git-tracked derived JSON) before trusting that a documented fix is live;
  a LOG entry or commit message describing a fix is not proof it landed.

## Validate & Correct an Existing Beat Map

Enter here when the map already exists and the question is "is it right, and fix
where it isn't" -- the repeated data-annotation loop, not from-scratch
localization. This is a phase of *this* skill: the Cardinal Rule, the Ground
Truth ranking, and the Independent Evidence Ladder above all still apply. Do not
factor it into a separate skill; an agent that corrects a beat without the
Cardinal Rule is the failure the "Why One Skill" section exists to prevent.

### Two objects, never confused

You are validating one of two different things at a time. Say which; they do not
mix (see [score coordinate systems](../../../docs/concepts/score-coordinate-systems.md)):

- **MIDI timing** -- the beat map's `source_midi_tick` / `source_seconds`: where
  in the Oguri performance each canonical beat sounds. Wrong timing = the
  orchestra enters at the wrong moment / the cursor is early or late in time.
  This is what `worklist`, `snap`, and `apply-anchors` operate on.
- **PDF geometry** -- the same anchors' `pdf_x`: where a beat is *drawn* on the
  Joseffy page. Wrong geometry = the cursor is painted over the wrong spot on a
  correctly-timed beat. That is a display-map problem (`omr_layout` / display
  map corrections), not a timing one. Correcting timing never moves a box.

Deciding which you're looking at is the first move: "the orchestra is late" is
timing; "the red line sits over the wrong note but sounds right" is geometry.

### Identity in, precision out

A human is excellent at *identity* ("that sound is m.53"; "the note on beat 3 is
that low B") and hopeless at *measurement* ("that onset is 180 ms late"). A
150-250 ms reaction jitter swamps exactly the errors worth catching, so **never
ask a person for a time or a magnitude.** The exact onset already exists in the
reference MIDI at tick resolution; a correction *selects* it. The performer
supplies which measure, or which note carries a beat; `beat_anchor.snap_to_pitch`
supplies the time.

### The loop

1. **Narrow** with `worklist <align_a> <align_b> <beat_map>`. It intersects two
   independently-derived alignments (Cardinal Rule: disjoint provenance) and
   returns the bars *both* reject -- the real defects -- plus a sparse **spine**
   (every ~15th bar) to catch a systematic offset the two alignments *share*,
   which their intersection is blind to. This turns 126 bars into a handful.
2. **Audition** each listed bar. The at-piano surface (see below) plays a
   4-beat metronome count-in at the bar's local tempo, then the measure with a
   click on each hypothesised beat. The performer reads the engraving and judges:
   right measure? does each beat's note land on its click? `propose-beat-anchors`
   supplies, per beat, the map's current time and (given a score) the bass note
   that carries the beat -- the pitch to confirm.
3. **Record a verdict**, always as identity:
   - **Correct** -> a reviewed anchor; nothing to change.
   - **Beat on the wrong note** (Mode B) -> the performer picks the note that
     carries the beat; `snap` resolves it to that note's exact onset. Precise,
     with no human timing. This is the workhorse correction.
   - A whole-measure *relabel* ("this bar is really m.18") is **not** a one-beat
     correction: moving one downbeat to a neighbour's time makes the source map
     non-monotonic and is rejected. A mislabelled region is a re-alignment job
     (anchor-seeded `AnchorPointNoteMatcher`), not a single anchor.
4. **Snap** (Mode B): `snap <beat_map> <performance.mid> --measure N --beat B
   --pitch P` returns the reference onset carrying pitch P nearest the beat's
   hypothesis, and flags ambiguity (a second onset nearly as close) instead of
   guessing. Pass the onset's exact tick when persisting -- never re-interpolate
   a picked onset from seconds.
5. **Preview before committing.** `preview <beat_map> --measure N --beat B
   --source-midi-tick T [--corrections file]` is a dry run: it shows which
   downstream beats a hypothetical anchor would move, and writes nothing. Use it
   to see the cascade before you trust it.
6. **Persist** with `apply-anchors <beat_map> <selections.json>` -- a list of
   `{measure_label, beat_in_measure, source_midi_tick|source_seconds, note?}`.
   It writes `HumanBeatCorrection`s to the registered corrections file, the same
   artifact the in-app workflow writes, and refuses a set that makes the source
   map non-monotonic. `undo <corrections>` removes a correction (most recent by
   default) if you mis-clicked.
7. **Re-derive (explicitly) and re-check.** A correction pins one beat; run
   `rederive <beat_map> <corrections> --out <new_map>` to propagate it -- it
   re-spaces the *interpolated* beats in the spans adjacent to each correction,
   so one anchor can repair a stretch the matcher only interpolated, while
   untouched regions stay byte-for-byte the same. This is deterministic
   re-spacing, not re-alignment: it cannot fix a beat pinned to the wrong note.
   It is never automatic -- nothing regenerates until you ask. Then re-run
   `worklist`/`cross-validate` to confirm the flagged bars cleared and no good
   bar regressed. A correction is a `reviewed` root (Ground Truth rung 3): store
   what the performer said (measure + beat), never let a re-run overwrite it.

### Interpolated beats have nothing to audition

`propose-beat-anchors` marks beats the map *interpolated* (no matched reference
note under them). There is nothing audible to judge on a beat with no note and
nothing to snap to, so the audition skips them -- do not manufacture a
correction there. A whole measure of interpolated beats (as m.104-105 are today)
is the signature of a matcher that gave up in that region; the fix is a real
anchor at the nearest sounding note, not a per-beat nudge.

## CLI

`scripts/score_localization.py` wraps the checks used above:

```
uv run python scripts/score_localization.py inventory <bundle_root>
uv run python scripts/score_localization.py measure-content <score> --measures 95-98
uv run python scripts/score_localization.py fingerprint <score> --measures 90-110
uv run python scripts/score_localization.py find-bar <score> <performance.mid> --measure 96
uv run python scripts/score_localization.py health <bundle_root>
uv run python scripts/score_localization.py irregular-measures <score>
uv run python scripts/score_localization.py cross-validate <align_a> <align_b> <beat_map>
uv run python scripts/score_localization.py annotate <bundle_root> --out NEW-NAME.pdf
uv run python scripts/align_score_to_performance.py <score> [performance] --validate

# Validate & correct an existing Oguri->beat map (MIDI timing, not PDF geometry):
uv run python scripts/score_localization.py worklist <align_a> <align_b> <beat_map> [--json]
uv run python scripts/score_localization.py propose-beat-anchors <beat_map> --measure N [--score <mxl>]
uv run python scripts/score_localization.py snap <beat_map> <performance.mid> --measure N --beat B --pitch P
uv run python scripts/score_localization.py preview <beat_map> --measure N --beat B --source-midi-tick T
uv run python scripts/score_localization.py apply-anchors <beat_map> <selections.json>
uv run python scripts/score_localization.py rederive <beat_map> <corrections.json> --out <new_map>
uv run python scripts/score_localization.py undo <corrections.json> [--correction-id ID | --all]
```

Run `--help` on any subcommand. Use `fingerprint` to find the discriminating
pitch in a neighbourhood before trying to localize a bar.

## Agent Discovery

Canonical path is `.agents/skills/score-localization/SKILL.md`. `.claude/skills`
symlinks to `.agents/skills`, so Claude and Codex-style agents find the same
file. Keep instructions agent-neutral and paths repo-relative.
