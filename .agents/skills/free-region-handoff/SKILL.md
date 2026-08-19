# Free regions: getting the orchestra back in after an unfollowable passage

## What this is for

Some passages cannot be score-followed. A cadenza, a fermata, an improvised
lead-in, a heavily ornamented recitative: the performer and the notation share
no reliable note correspondence, so every content-based follower has nothing to
match on. In the Chopin Op. 11 mvt II cadenza the reference recording reaches
only **0.32** pitch-sequence similarity with the printed part.

Worse, the usual repair makes it permanent. Position-gated anchors need the very
position they exist to correct: a frozen estimate is far from every future
anchor, nothing fires, and it stays frozen. Observed live for **16.4 seconds**
while the performer played twenty gestures past it.

This skill is the workflow for turning such a passage into a small, solved
supervised-learning problem, and for producing the runtime artifact the engine
loads. It is piece-agnostic. The Chopin cadenza is the worked example; nothing
here is specific to it.

**Call the passage a *free region*.** The treatment is *handoff detection*.

## The one decision that matters most

**Ask what the orchestra actually needs, and build only that.**

The instinct is to keep tracking position through the region. Resist it. In a
cadenza nothing plays but the soloist, so a beat estimate mid-region has no
consumer. The orchestra needs two instants:

1. Did we **enter** the region?
2. Are we at the **handoff**, so it can come in?

On the worked example this collapsed a 169-class position problem to a binary
one against the same ~600 training events, and took the result from *1 of 8
passes tracked* to *8 of 8 handoffs detected with zero false positives*. The
formulation, not the features or the data, was the difference.

Track position through a free region only if you can name the consumer.

## Workflow

### 1. Declare the region

In `sections.json`, `mode: "FREE"` with a `free_region` block. Boundaries are
authored in **measure and beat**, never ticks: ticks are derived and silently
relocate whenever the beat map is rebuilt. See `score_coordinates.py`.

Split any enclosing `FOLLOW` section so the region is its own entry.

### 2. Record demonstrations

```bash
python scripts/metronome_record.py --bpm 60 --out demos.json
```

Plays a click (Claves every beat, Open Triangle on the downbeat) and records
continuously. The performer repeats the passage 5-10 times with rests between.

**Why a metronome:** it supplies ground truth. Without it you have recordings
but no labels, and cannot measure anything. Accept that it suppresses natural
rubato — you will test rubato synthetically in step 5.

**Two passes is often enough.** On the worked example N=2 scored the same as
N=7. Averaging several passes is still what separates an invariant from an
ornament, which a single pass cannot do: a pitch that looks decisive once may
occur four times.

### 3. Segment and sanity-check

Split on rests (a gap larger than a few beats). Then **look at the pass starts
before trusting them.** On the worked example the starts were:

```
pass 0: beat  7.989  (mod 4 = 3.99)   <- entered on the wrong beat, discarded
pass 1: beat 25.051  (mod 4 = 1.05)
pass 2: beat 48.992  (mod 4 = 0.99)   ... all remaining agree
```

One pass was unusable and the modulus revealed it in one glance.

### 4. Probe identifiability BEFORE building anything

**This step is not optional and it goes first.** Ask whether the information
exists at all, with unlimited compute and full lookahead. If an offline
oracle cannot recover the answer, no online algorithm will.

Use `scripts/probe_region_identifiability.py`. It runs forward-backward over
position x tempo from a uniform prior.

**Then run controls, because the first number is usually partly an artifact.**
On the worked example the raw result was 0.022 beats — and 0.55 of it was grid
geometry, because a 10.4-beat grid and a 10.2-beat passage nearly pin the answer
by themselves:

| control | median error |
|---|---|
| uniform emissions (geometry only) | 0.55 beats |
| shuffled pitches (timing intact) | 0.74-1.84 beats |
| real pitch, grid widened 3x | 0.008-0.052 beats |

A result without controls is not a result. Widen the grid, destroy the
emissions, shuffle the labels — whatever removes the shortcut you suspect.

### 5. Train the detector

```bash
python scripts/train_cadenza_detector.py \
    --demos demos.json --negatives other_playing.mid \
    --region-start-beat 401 --handoff-beat 411 --hand-back-beat 412 \
    --validate --out model.json
```

**Features: per-note decaying pitch-class histograms, six time constants
(0.25-8 beats), each L1-normalised.** Tempo invariance comes from the
normalisation — a ratio within a histogram does not care how fast it was filled.

**Do not segment into chords.** It is tempting, especially if strike sizes look
periodic (on the worked example they run `2222-33` and genuinely demarcate the
beat). But no threshold segments stably: gaps are bimodal (268 below 20 ms, 244
above 80 ms) yet the tails cross under tempo change, and **every** window from
10 to 50 ms was unstable on 7 of 8 passes. Per-note features need no
segmentation and are cheaper.

**Richer features probably will not help.** Register, contour and velocity were
each tried and bought nothing. When accuracy plateaus, suspect your *output
resolution* before your inputs: on the worked example error tracked the
quantisation floor down and only then flattened.

**Choose the shortest useful label horizon.** Longer horizons give more warning
but start firing early under rubato. `last 1.0 beat` was clean across every
tempo condition; 2 and 3 beats were not.

### 6. Validate through the shipped classes

`--validate` runs leave-one-out through the real `CadenzaModel` and
`CadenzaDetector`, with a save/load round trip per fold — not through the probe
script's own arrays.

**This is where the worked example found a bug no probe could have found.** The
veto counted notes arriving after the *fire*, but the detector fires ~0.3 beats
*before* the landing note, so the landing dyad tripped it and **all eight
correct fires retracted themselves.** The probes had measured silence after the
true landing, an instant the runtime never observes.

A probe script and a runtime that agree on paper can still disagree in fact.

### 7. Test tempo robustness synthetically

Warp the held-out pass: 0.7x, 1.4x, a ritardando, sinusoidal push-pull. The
detector must hold.

**Warp only the gaps *between* events.** A chord strike's own spread is physical
and does not scale with tempo; scaling it is unphysical and will silently change
your segmentation.

### 8. Find a retraction signal

Ask what *must* be true just after a correct handoff. Usually the soloist is
scored silent while the orchestra plays. Then notes still arriving are evidence
the entry was early.

On the worked example the separation was total:

```
correct fire   -> 0 notes in the following beat     (8 of 8)
premature fire -> median 6, minimum 3               (554 of 554)
```

A near-free veto is worth more than a cautious detector: it lets the detector
fire eagerly and be pulled back within a beat. **Give the veto a grace period**
so the region's own closing notes do not trip it.

## Traps, each of which cost real time

- **Reporting a number without a control.** Grid geometry alone gave 0.55 beats.
- **Letting a feature leak the answer.** A fresh accumulator makes "how much have
  I accumulated" a near-proxy for position. Re-test with a *warm* accumulator,
  entering the region mid-stream as happens live.
- **Using lookahead by accident.** Forward-backward is offline. An online
  detector must be causal; verify it never reads a future note.
- **Tuning constants past the point of diagnosis.** If a design needs three
  successive fixes and still fails, the formulation is wrong, not the constants.
- **Cross-performer transfer.** A model trained on one performer failed
  completely on another's rendition of the same cadenza, and fired spuriously
  elsewhere. Train on whoever will play.
- **Optimising the fast path.** Inference is ~90 us per note against ~280 ms
  between events. Restructuring the maths to do less arithmetic will not help;
  the cost is interpreter overhead, and the margin is ~1700x regardless.

## What ships

- `model.json` — the trained detector, a few KB.
- A `FREE` section in `sections.json` pointing at it.
- Nothing else. The online path must not import scikit-learn.

## Related

- `.agents/skills/score-localization/SKILL.md` — establishing which measure is
  which, and the provenance rules for derived artifacts.
- `src/aimusic/accompaniment/score_coordinates.py` — the coordinate seam.
- `docs/LOG.md` — the worked example, with the wrong turns left in.
