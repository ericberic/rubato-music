# Yamaha Take Analysis

## Purpose

Human Yamaha takes are the first real fixtures for offline alignment and later
simulated-online following. They should be analyzed as performance data, not as
canonical score truth.

## Movement 2 Take: `movement2_take`

Recorded on 2026-07-06 with the Oguri movement-2 cued-recording workflow.

Local artifacts:

- `data/processed/movement2_take/solo.mid`
- `data/processed/movement2_take/recording_metadata.json`
- `runs/movement2_take/input/solo.mid`

These paths are ignored by git and should be DVC-managed if retained.

## Observations

- Recording length: about 187.11 seconds.
- Captured solo note-ons: 881.
- No solo input notes remained active at the end of the recorded MIDI.
- First played note occurs at about 7.258 seconds into the take.
- Nominal cue metadata expected the first solo entry at 8.0 seconds, so the soloist
  entered about 0.742 seconds before the coarse cue anchor.
- Pitch-sequence alignment maps the take from the first solo entry through about
  Oguri score time 258.2 seconds, which is a natural stopping point in the first
  long solo span.

Approximate pitch-only alignment summary:

- Matched notes: 701.
- Pitch mismatches: 63.
- Extra take notes: 117.
- Missing reference notes: 111.
- Matched reference coverage: first 875 reference note-ons.
- Linear timing fit: `ref_relative_seconds ~= -10.89 + 1.059 * take_seconds`.
- Median absolute timing residual after that crude fit: about 1.31 seconds.
- 90th percentile absolute timing residual: about 2.82 seconds.

The mismatch/extra/missing counts are diagnostic only. They mix true playing
mistakes, chord-order differences, rolled/arpeggiated attack ordering, pedal
artifacts, and failures of the crude pitch-only aligner.

## Cue Release Bug

The first cued-recording implementation cut accompaniment exactly at the solo
entry. In the Oguri source, five orchestra notes are still active at that cut:

- Violini II note 59: releases about 0.014 seconds after entry.
- Contrabassi note 35: releases about 0.083 seconds after entry.
- Viole note 54: releases about 0.131 seconds after entry.
- Corni (E) note 66: releases about 1.073 seconds after entry.
- Corni (E) note 59: releases about 1.304 seconds after entry.

If those release events are omitted, the Yamaha can hold those notes until stop
or panic. The cue renderer should therefore play no new post-entry orchestra
attacks, but must continue long enough to emit release events for any notes it
started or carried into the cue.

## July 19 repeated passage evidence

Three kept passes entered at displayed measure 12. Each contains 158 note-on
events; the two newest have identical pitch multisets and the older differs by
one pitch. The canonical performance-profile v2 migration rebuilt their cells
from raw MIDI and the canonical take warp rather than reusing source-MIDI cell
coordinates. They now share 63 quality-qualified canonical half-quarter cells.
The median take baseline is 62.4 quarter-note BPM, typical normalized-rubato
MAD is 2.8%, and typical velocity MAD is 1. The evidence score is 0.669 versus
a one-pass baseline of 0.282, or 2.37× usable evidence.

The largest normalized timing disagreements now fall at m.14 beats 1 and 2.5,
m.18 beat 1, and m.20 beat 4.5. These are hypotheses for musical or alignment
review, not labels of mistakes. The earlier report of 93 cells and variance
concentrated at m.17 came from the pre-v2 profile, which keyed samples on the
expressive reference-MIDI grid; it must not be compared as if it used the same
coordinate model.

This is the desired statistical role of repeated passes: reveal both the
invariant contour and local freedom that a follower must tolerate. The evidence
score remains a deterministic progress heuristic, not a probability or an LLM
judgment.

The m.23 capture also proved that the former cue path was fixed playback, not
following. Its post-hoc position error grew from about one beat after ten
seconds to 12.7 beats by the end (median absolute error 7.66 beats). The cue
contract now hands off at the solo entry; continued accompaniment belongs to
the live follower.

## Interpretation

Do not treat every alignment mismatch as a mistake to replicate. For the MVP:

- Score follower should follow robustly through wrong, missing, or extra notes.
- Tempo model should learn from matched anchors and ignore low-confidence notes.
- Expressive timing should be derived from stable matched note groups, not every
  raw note event.
- Accompaniment rendering should use a smoothed timing map, not copy local
  mistakes or chord-order artifacts directly.

## Next Implementation Step

Build an offline alignment command for `movement2_take`:

1. Load `solo_reference.mid` and the take.
2. Estimate entry offset from the cue metadata.
3. Run a pitch/onset aligner that supports extra/missing notes and chords.
4. Emit `runs/<run_id>/trace/alignment.jsonl`.
5. Emit `runs/<run_id>/analysis/metrics.json`.
6. Render `orchestra_accompaniment.mid` against the matched timing map.

This comes before live following. The offline render gives a repeatable target
for listening and for deciding which timing deviations are expressive rubato
versus mistakes/noise.

## Related

- [Oguri / Kunst der Fuge MIDI](../sources/oguri-kunstderfuge-midi.md)
- [Experimentation](../ML_EXPERIMENTATION.md)
- [Yamaha MIDI Setup](../runbooks/yamaha-midi.md)
