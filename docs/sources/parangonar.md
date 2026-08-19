# Source: Parangonar

## Source

- Repository: https://github.com/sildater/parangonar
- Package: https://pypi.org/project/parangonar/
- Documentation: https://cpjku.github.io/docs/parangonar/
- TheGlueNote paper: https://arxiv.org/abs/2408.04309
- Maintainer affiliation: Institute of Computational Perception, JKU

## Date Read

2026-07-18

## Why It Matters

Parangonar is an open-source Python package for symbolic music alignment. It is
a plausible offline score-bundle tool for matching Audiveris MusicXML notes to
MIDI notes, and it is already installed transitively through Rubato's
`pymatchmaker` dependency. It is not automatically a solution for matching a
piano reduction to a differently voiced orchestral arrangement.

## Key Facts

- Version 3.3.2 is Apache-2.0 and supports Python 3.10 or newer.
- The package uses Partitura for MusicXML/MIDI loading and alignment I/O.
- `DualDTWNoteMatcher` is the documented default for ordinary offline
  score-to-performance note matching.
- `AnchorPointNoteMatcher` accepts externally supplied anchors, which fits
  Rubato's reviewed measure boundaries.
- `TheGlueNoteMatcher` uses a pretrained transformer intended to tolerate
  repeats, skips, insertions, and other large mismatches. The ISMIR 2024 paper
  evaluated solo-piano score/performance pairs, including synthetic mismatch
  segments; it did not validate piano-reduction-to-orchestra alignment.
- `SubPartMatcher` is described as monophonic subpart alignment. It is not a
  general reduction-versus-orchestration matcher.
- The repository is current but small: at the 2026-07-18 inspection it had 61
  stars, three listed contributors dominated by one maintainer, and no visible
  GitHub Actions workflow. Those are maintenance-risk signals, not evidence
  against the published algorithms.
- Release 3.3.2 has an open regression in the optional pretrained matcher:
  `TORCH_AVAILABLE` is referenced but not defined. The open fix has no checks.

## Rubato Smoke Test

Tests used the committed Movement 2 Oguri-derived MIDI artifacts and the
transitively installed Parangonar 3.3.2:

- `DualDTWNoteMatcher` matched 500/500 solo-reference notes when the candidate
  window also contained 207 interleaved orchestra notes. This is encouraging
  for aligning a mostly corresponding OMR solo line against the Oguri solo.
- On the actual Audiveris draft for Joseffy pages 1-13,
  `DualDTWNoteMatcher` matched 1,877 of 2,525 MusicXML Piano I note-array rows
  to the 2,831-note Oguri solo in about five seconds. That is 74.3% score-side
  coverage and 66.3% performance-side coverage; the latter includes Oguri
  material beyond the exported page range. Measures 14-18 had 91-100%
  score-note coverage. In measure 17, matched engraving x-positions and Oguri
  onset times were almost perfectly monotonic (Spearman 0.997), despite
  Audiveris assigning that measure an invalid rhythmic duration.
- `SubPartMatcher` matched only 113/2,831 notes when asked to find the entire
  solo inside the full Oguri file, took about 89 seconds, and labeled 2,718
  score notes as deletions. This path is unsuitable as Rubato's turnkey
  reduction-to-orchestra solution.
- The 500/500 result is not an accuracy benchmark: the derived solo reference
  is literally present in the source MIDI. The Audiveris result is the more
  relevant feasibility signal, but it still lacks manually reviewed note-level
  ground truth.

## Matcher Choice Matters on OMR Input (2026-08-04)

`scripts/align_score_to_performance.py` hardcodes `AutomaticNoteMatcher`. On
clean publisher-typeset MusicXML (MuseScore reduction/orchestra) it's fine --
97.6%/96.1% match, agrees with the production beat map to a median of 0.04s.
Pointed at the noisier Audiveris-derived Joseffy MXL, it drops to 67.6% match
**and drifts** -- 60-85s of accumulated disagreement with the beat map by
m.90, not real error, just a matcher that lost the thread and never resynced.
Switching to `DualDTWNoteMatcher` on the identical Audiveris input dropped
median disagreement to 0.04s (comparable to the clean-score case), despite a
*lower* raw match rate (63.1%) -- global DTW's monotonicity constraint matters
more than match count for OMR-noisy input. `align_score_to_performance.py`
should default to `DualDTWNoteMatcher`, or at minimum expose `--matcher` and
warn when match rate is low with `AutomaticNoteMatcher`, since a silent
drift like this is exactly the "high confidence and wrong" failure mode the
[score-localization skill](../../.agents/skills/score-localization/SKILL.md)
warns about.

Two API gotchas hit while doing this, worth knowing before calling parangonar
matchers directly against an Audiveris export:

- `partitura`'s `Score.note_array()` / `note_array_from_part_list` throws
  `Exception: Note array from parts with multiple divisions is not
  supported` when combining parts, because Audiveris legitimately emits a
  different `<divisions>` value per measure/system (legal MusicXML). Per-part
  `part.note_array()` handles intra-part divisions changes fine; the fix is
  to build each part's array separately and `numpy.concatenate` them, rather
  than asking partitura to reconcile divisions across the whole score.
- `DualDTWNoteMatcher.__call__` requires an `is_grace` field on the note
  array, which the default `note_array()` does not include --
  `note_array(include_grace_notes=True)` on both score and performance sides.
- `parangonar`'s `evaluate` submodule imports `pandas` at package-import
  time, but `pandas` is not declared in the `analysis` extra
  (`pyproject.toml`), so `import parangonar` fails with
  `ModuleNotFoundError: No module named 'pandas'` even though the CLI's own
  docstring shows `uv run python scripts/align_score_to_performance.py` as
  the invocation. Fixed by adding `pandas` to the `analysis` extra.

## Caveats

- Treat Parangonar as a candidate algorithm implementation behind a Rubato
  adapter, not as an unquestioned production dependency.
- Pinning 3.3.1 or carrying a tiny patch would be necessary before evaluating
  the optional pretrained matcher; adding Torch, Symusic, and MidiTok expands
  the dependency surface.
- Package tests use small research fixtures and the repository does not expose
  continuous integration. Rubato needs its own fixed Chopin fixtures,
  alignment metrics, and disagreement reports.
- Cross-arrangement matching should be constrained by reviewed measure anchors
  and corroborated by an independent method. Exact note identity cannot be
  assumed for the Piano II reduction versus orchestra.

## Links

- [Score Bundle Ingestion](../concepts/score-bundle-ingestion.md)
- [Joseffy Two-Piano Reduction](joseffy-reduction.md)
- [Matchmaker](matchmaker.md)
- [Decision 0005](../decisions/0005-score-bundle-v2-canonical-timeline.md)
