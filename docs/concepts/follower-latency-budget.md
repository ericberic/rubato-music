# Follower Latency: Why Our PTHMM Costs ~25 ms/note, Not 3.6 ms

## Summary

Matchmaker's published HMM alignment latency (3.59 ms) and Rubato's measured
per-note follower cost (~25 ms median, ~45 ms p95, with 180 ms outliers under
load) are **both correct**. They differ because per-note HMM cost in
`matchmaker` 0.3 scales with the **length of the score**, and our
full-movement Chopin reference is roughly an order of magnitude longer than the
pieces in the ISMIR 2025 benchmark.

Smith–Waterman is **not** involved. It appears only in the offline take-alignment
path (`offline_alignment.align_note_events`, used by `score_fusion`,
`offline_render`, `rehearsal_position`, `closed_loop_evaluation`). The live
follower never touches it.

## Measured (Apple M1, 17 GB, `matchmaker` 0.3.0, numpy 1.26.4)

Real production reference `data/scores/chopin_op11_movement_2/derived/solo_reference.mid`
(2831 notes, 2779 unique onsets), method `pthmm` → `matchmaker.prob.hmm.PitchHMM`
with `has_insertions=True`, so `n_states = 2 × onsets − 1 = 5557`.

Per-note `hmm.step()` cost against prefixes of that same score:

| onsets | states | transition matrix | median | p95 |
|---|---|---|---|---|
| 100 | 199 | 0.3 MB | 0.26 ms | 3.18 ms |
| 250 | 499 | 2.0 MB | 0.80 ms | 4.82 ms |
| 500 | 999 | 8.0 MB | 2.91 ms | 7.11 ms |
| 1000 | 1999 | 32 MB | 5.92 ms | 12.89 ms |
| 2000 | 3999 | 128 MB | 15.06 ms | 24.59 ms |
| **2779 (full)** | **5557** | **247 MB** | **24.54 ms** | **44.85 ms** |

The paper's benchmark corpora (`(n)ASAP`, `Batik`, `Vienna4x22`) average roughly
760 beats per piece. At that scale this table gives ~0.8–3 ms — consistent with
the reported 3.59 ms. **The paper number is not wrong and we are not doing
anything anomalous; we are simply running a score ~5× longer than the benchmark,
on a cost curve that is quadratic.**

## Where the 25 ms goes

Decomposed at the full 5557 states:

| component | cost |
|---|---|
| `BernoulliPitchObservationModel.__call__` — `(P**o) * ((1-P)**(1-o))` over a 5557×88 profile matrix, then `prod` | 8.7 ms |
| `np.dot(T.T, forward_variable)` — dense 5557×5557 float64 matvec | 9.3 ms |
| elementwise combine, normalize, `argmax` over 5557, call overhead | ~6 ms |

Both dominant terms are **memory-bandwidth bound, not arithmetic bound**. But
they are *not* equally wasteful, and conflating them is a mistake — see
"Tradeoffs" below.

### Units: what one "state" is

State offsets below are **state indices**, not notes and not beats. With
`has_insertions=True` the state space alternates real score onsets with
synthetic in-between states, so `n_states = 2 x onsets - 1` and the even indices
are the real onsets. The odd inserted states absorb notes that match no score
onset (wrong notes, ornaments, grace notes).

For the movement-2 reference (2831 notes, 2779 onsets, 5557 states):

| states | score onsets | beats p5 / median / p95 | max |
|---|---|---|---|
| 2 | 1 | 0.01 / 0.33 / 1.17 | 24.2 |
| 8 | 4 | 0.59 / **1.36** / 3.54 | 27.3 |
| 16 | 8 | 1.41 / **2.91** / 6.44 | 30.0 |
| 744 | 372 | ~122 | - |

**Do not read the median as a constant.** Because the ceiling counts *onsets*,
its musical reach swings ~6x with local note density: +8 states is worth about
half a beat in dense filigree and ~3.5 beats in sparse writing, and a single
onset step spans 24 beats across this movement's long rest. The beats column is
the solid one; the reference MIDI carries a single nominal 120 bpm `set_tempo`
(almost certainly a file default), so derived seconds are indicative only.

The ceiling is also a **rate, not a one-shot jump**: it applies per note played,
and since the performer advances too, net closure is (k-1) onsets per note.
Roughly +8 closes ~1 beat per note played and +16 closes ~2.3 beats per note --
so a 32-beat gap needs ~32 notes at +8, ~14 at +16.

So `+8` means the belief may advance at most **4 score onsets**, and `-744`
means it may jump back ~372 onsets. Critically this budget is **per observation
(per note played), not per unit time**. Ordinary playing advances 1 onset
(2 states) per note; the +8 ceiling is headroom for skipped or unmatched notes.
This reference is ~monophonic (1.02 notes/onset) so onset ~= note here; that
stops holding on a chordal bundle.

### Actual structure of the transition matrix

Not a simple band. Each row has ~752 nonzeros out of 5557, spanning offsets
**-744 .. +8**: the model can advance at most 4 onsets (8 states) per step, but
can jump back up to ~372 onsets in one step. Mass is distributed
**92.4% in the forward offsets [0, +8]**, 6.5% in offsets [-2, -1], and only
**1.06% in the entire long backward tail** (741 entries averaging 1.4e-5 each).

Truncating to a band therefore drops ~1.96% of row mass on average (5.5% worst
row), *all of it from the backward tail* — the drop is identical whether the
forward band is 4 or 128, because the forward direction is already exact at +8.

A banded step alone lands the transition term under 0.1 ms. The observation
term is the one that cannot simply be windowed (below).

## Tradeoffs: the two optimizations are NOT equivalent

**These were measured, not assumed.** Harness: seed the prior the way
`_seed_pthmm_prior` does, play 150 in-order notes (all variants lock 100%), then
jump the observation stream and compare beat paths.

### 1. Banded sparse transition — a pure data-structure change, near-lossless

This does **not** narrow the hypothesis range. The state space stays global,
every state stays reachable, and the posterior is still defined over the whole
piece. The only thing capped is how far probability can move **in one step** —
and the original matrix already caps forward motion at +8. Banding only discards
the 1.06% backward tail.

Measured effect on the beat path:

| scenario | full dense | banded -8 |
|---|---|---|
| in-order, 400 onsets | — | **bit-identical** |
| control (no jump) | 100% | 100% |
| backward jump -300 onsets | 0% | 0% |
| forward jump +300 onsets | 0% | 0% |
| jump +110 onsets | 86.7% | 84.7% |

The long backward tail buys **~2 percentage points in one marginal case and
nothing anywhere else**. Notably the full dense matrix *also* fails every real
jump — the tail is not what recovers position. So the ~300x transition speedup
is available at essentially no modelling cost.

### 2. Windowed observation model — this one DOES narrow the hypothesis range, and it breaks re-localization

Scoring only states within +/-W of the current estimate is a genuine pruning of
the hypothesis space, and it is **not free**. It collides directly with
[Decision 0018](../decisions/0018-follower-relock-recovery.md): `relocalize()`
works by spreading the prior across the whole score so the pitch stream can
re-acquire absolute position. A window centred on the (wrong) current estimate
cannot see the correct distant state, so the mass never moves there.

Measured, reproducing the Decision 0018 scenario (tracker stalled 700 onsets
behind, then `relocalize()`):

| observation model | re-acquires | accuracy |
|---|---|---|
| full (all 5557 states) | **after 2 notes** | 98.3% |
| window +/-1024 | never | 0% |
| window +/-512 | never | 0% |
| window +/-128 | never | 0% |

In steady-state following the window costs nothing (all windows scored 100% on
the control and matched full observation on every jump case), because the banded
transition already prevents mass from being far away. The window is harmful
**only in exactly the situation it would be needed**.

So a windowed observation model is viable only if it is **state-dependent**:
narrow while locked, and widened to the full score whenever the prior has been
broadened — i.e. the window must be a function of posterior entropy or an
explicit "re-localizing" mode, not a constant. That is a real design commitment,
not a drop-in optimization.

### Why raising the ceiling (+8 -> +16) would not help

The +8/+16 numbers above are **ceilings, not behavior**, and the ceiling is not
currently the binding constraint -- so widening it buys nothing measurable.

The +300-onset forward jump above was not blocked by the per-step ceiling: over
150 played notes, +8/note allows up to 600 onsets of advance. It failed because
the ceiling is a hard limit while actual motion is driven by where the
observation model puts likelihood, and nothing in the model proposes a large
skip. That is precisely the gap `relocalize()` fills, by resetting the prior
rather than travelling there through transitions.

Before ever changing the ceiling, look for the follower pinned at maximum
advance rate for sustained stretches in a real trace. Absent that evidence, jump
recovery is `relocalize()`'s job, not the transition ceiling's.

### Evidence from 117 real run traces

`~/Library/Application Support/Rubato/runs/*/trace/runtime.jsonl` holds 44,463
consecutive `raw_score_beat` deltas across 117 takes. Converting every backward
step to a state offset (the unit the band is specified in):

| | backward motion |
|---|---|
| p50 | 1.0 states |
| p90 | 4.0 states |
| p99 | 8.3 states (~2.5 beats) |
| p99.9 | 29 states |
| **max ever** | **35 states (~16.9 beats)** |
| matrix capacity | **744 states (~122 beats)** |

**The long backward tail has never been used beyond ~5% of its range**, across
every take on record. Only 2 steps in 44,463 exceeded 10 beats; none exceeded 30.
This matches the musical intuition that a rehearsed performance needs backward
slack for rubato and fermatas -- a few beats -- not two minutes.

Caveats: these are *raw tracker* deltas, so some backward motion is tracker
jitter rather than the performer (the engine already logs
`clamp_backward_jitter` 3,864 times downstream). And this is evidence that the
capacity was never *used* while it was available, on this repertoire.

| band | covers observed steps | clipped | matvec |
|---|---|---|---|
| -8 | 98.97% | 11 | 0.04 ms |
| -16 | 99.44% | 6 | 0.06 ms |
| -32 | 99.91% | 1 | 0.12 ms |
| **-64** | **100%** | **0** | **0.28 ms** |
| -744 | 100% | 0 | 4.58 ms |
| full dense | 100% | 0 | 5.86 ms |

### Known limits of the band evidence

The 117 traces behind `-64` are **linear run-throughs** of a partial score. They
contain no rehearsal looping -- a pianist stumbling and restarting two bars
earlier -- and the takes cover well under half the movement, so the middle
section, filigree and coda are unrepresented. A two-bar backward jump in
sixteenth notes is ~64 states, right at the band edge, and the dense matrix
could serve it in one step where the band cannot. The mitigation is
`relocalize()`, not the transition tail -- dense was measured to fail large
jumps too -- but the gap is real and `transition_backward_states` stays tunable
because of it.

### Status: both rewrites are canonical

Neither is behind a flag. Both are exact -- verified bit-identical against stock
Matchmaker over 5673 notes -- so there is no slower path worth keeping, and a
toggle would only add a way to accidentally ship the 50x-slower behaviour.
`transition_backward_states` stays tunable as a *value* (default 64) because the
right backward reach may prove score-dependent; it can no longer be disabled.

Equivalence is pinned in tests against a stock `PitchHMM` built directly, not
against a disabled code path.

### Historical: the recommended split, before the factorization

- Take the banded transition unconditionally, at **backward band -64**
  (forward stays +8, which is the original matrix's own exact reach). -64
  covers **100% of every backward step in 117 real traces with 2x headroom over
  the observed maximum**, costs 0.28 ms against 5.86 ms dense, and drops the
  transition model from 247 MB to ~4.8 MB. Do not tune to -8/-16 on synthetic
  evidence alone; both clip real observed steps, and the cost difference is
  negligible.
- Treat the observation term separately. Options, in increasing risk: score in
  float32 rather than float64; precompute `log` profiles and sum instead of
  `prod` of two `pow`s; only then consider an entropy-gated window.
- Even banded-transition-only takes the step from ~24.5 ms to ~15 ms, which
  brings the full movement back inside Decision 0011's 20 ms p95 contract.

## The "7 ms → 180 ms growth" in Decision 0011

[Decision 0011](../decisions/0011-realtime-multiprocess-architecture.md) records
cost growing from 7 ms to 180 ms per note over 30 s. A clean single-process
replay does **not** reproduce monotonic growth: over 600 consecutive steps the
per-step cost stays flat, and denormal/subnormal arithmetic in the forward
variable was ruled out (flushing subnormals to zero changes the matvec by 0.03 ms).

Two mechanisms remain the plausible explanation, in order of confidence:

1. **Unbounded request backlog.** `ProcessFollower.observe` unconditionally
   `put`s every note on the request queue and returns immediately; the child
   drains FIFO at ~25 ms/note. Above ~40 notes/s the backlog grows without
   bound, so each note's answer is measured further and further after its
   arrival. `GaugeBlock` has a `dropped` field for shedding, but **nothing sheds
   on the follower input side**. This also matches the symptom behind
   [Decision 0018](../decisions/0018-follower-relock-recovery.md) — a tracker
   found ~70 beats behind for a whole take.
2. **Memory pressure.** ~494 MB of matrices streamed per note in a spawned
   child, alongside REAPER/BBCSO sample memory, degrades badly once the working
   set stops fitting.

The original figure was in-thread wall time, so it also included time the
follower thread spent descheduled — which is what motivated the process split in
the first place.

## Chord serialization

`MatchmakerStreamFollower` constructs `BytesMidiStream(..., polling_period=None)`,
so each `note_on` is its own HMM step and chords are **not** grouped, even
though `PitchProcessor` is built to accept a whole chord as one observation.
For the current movement-2 reference this costs almost nothing (1.02 notes per
onset — the solo reference is effectively monophonic), but it multiplies
per-onset cost directly by chord size on any denser bundle, and it feeds
one-hot single pitches against chord pitch profiles.

## Implications

- Per-note follower cost is a function of **score length**, so it is a budget
  that must be re-measured per bundle, not a constant.
- The `follower` contract in Decision 0011 (per-note p95 < 20 ms) is **already
  violated at rest** on the full movement (44.85 ms p95), before any load.
- The banded transition is the safe, high-leverage fix; the windowed
  observation is **not** a drop-in and must not be adopted without an
  entropy-gated widening path, or it silently disables Decision 0018 recovery.
  Both live in `matchmaker` / `hiddenmarkov` rather than in Rubato. Decision
  0011 deliberately declined to rewrite the HMM; that non-decision is worth
  revisiting now that the cost is attributed.
- Independently, the follower input side needs **load shedding or chord
  conflation** so backlog cannot grow without bound.

## Measuring follower latency

### What the pre-existing fields can and cannot tell you

`processing_latency_ms` is **not** HMM cost: it is `now - input_at` around
`ProcessFollower.observe`, which is deliberately non-blocking, so its p50 is
0.00 ms. `loop_timing` has no `follower` loop either. What *is* derivable from
the archive is end-to-end lag: on `follower` rows, `monotonic_time - perf_time`
is note-to-position latency (queue + IPC + HMM + return), because the two share
a clock (verified on `input` rows: median offset 0.124 ms).

Across 117 traces, filtered to `follower_state.follower == "matchmaker"`
(44,525 rows; 154 further rows are a stub follower at ~0.01 ms and must be
excluded):

| p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|
| 9.39 ms | 31.25 ms | 49.36 ms | **176.82 ms** | **2153 ms** |

The p99 and the maximum independently reproduce Decision 0011's "180 ms per
note" and "1.8 s processing spike" from the trace archive.

### Direct measurement (implemented)

The `follower` gauge now carries per-note HMM compute. Two mistakes are worth
recording, because both produce plausible-looking numbers:

1. **`mp.Queue.qsize()` raises `NotImplementedError` on macOS**, so the old
   `set_queue_depth(_qsize(...))` pinned the backlog gauge to 0 on the only
   machine that runs performances. Depth is now derived from two monotonic
   counters (parent counts puts, child counts gets).
2. **Timing `observe()` measures dispatch, not the HMM.** The follower has three
   layers -- parent enqueues, child process dispatches, and an inner thread
   inside the adapter drives Matchmaker's generator. Only the innermost runs the
   HMM. Timing the child's `observe()` read **0.10 ms p50** while the HMM was
   really costing ~22 ms. The gauge now wraps the follower's `step`, which is
   exactly one forward recursion.

With the timing on `step`, a replayed take measures **p50 22.16 ms, mean
25.37 ms** for dense -- confirming the 24.5 ms benchmark in the real
multiprocess path.

## The observation model: an exact factorization

The band leaves the observation model as ~97% of the remaining cost. It does not
need a window. It needs to stop recomputing what never changes.

Matchmaker evaluates, per state, per note:

```
L(s) = prod_j  P[s,j]**o_j * (1 - P[s,j])**(1 - o_j)
```

`o` is a **binary** piano-roll, so each term collapses to `(1-P)` when the pitch
was not played and `P` when it was. Multiplying and dividing by `(1-P)` on the
played dimensions gives an exact identity:

```
L(s) = prod_j (1 - P[s,j])  *  prod_{j played} P[s,j] / (1 - P[s,j])
       \___ independent of o ___/   \___ ~1 term ___/
```

The left factor is a constant per state -- precompute once. The right touches
only the sounding pitches (1.02 per onset on this reference).

| | per note |
|---|---|
| before | 5557 x 88 x 2 = ~978,000 `pow` calls |
| after | 5557 x (1 + chord size) = ~11,000 multiplies, zero `pow` |

Roughly 977,000 of those `pow` calls were computing `x**0` and `x**1`.

This is the standard Bernoulli naive-Bayes factorization, kept deliberately in
**linear** space rather than the conventional log-odds sum: log space is more
stable but would not be bit-identical to Matchmaker's linear recursion, and
`base` lands in [0.298, 0.366] so stability is not at risk anyway.

Exact under two conditions, both guarded in
`_FactorizedPitchObservationModel`: `o` must be binary (non-binary input falls
back to the direct expression) and `P` strictly in (0,1) (rejected at
construction; measured range here is [0.0024, 0.5153]).

### Validated on the full corpus

16 distinct takes, **5673 notes**, every note, banded transition held constant so
the observation is the only variable:

| | p50 | p95 | p99 | max |
|---|---|---|---|---|
| banded -64 + direct observation | 8.001 | 11.994 | 18.434 | 71.77 ms |
| **banded -64 + factorized** | **0.341** | **0.630** | **1.131** | **36.05 ms** |

**Every beat path identical on all 16 takes, max difference exactly 0.0.** Plus
240 synthetic 2-8 note chords (the corpus is near-monophonic): max relative
error 2.2e-15.

**Precisely what "identical" covers.** The claim is over the *emitted beat
path* -- `state_space[argmax(forward_variable)]` -- under deterministic,
synchronous driving. It is **not** a claim that the posteriors are identical:
banding renormalizes each row after dropping ~2e-17 of tail mass, so the
probability density differs in the last bits even though the argmax does not.
Nor does it describe the asynchronous production path: `ProcessFollower.observe`
is non-blocking and returns whichever position happens to be ready, so two runs
of the *same* configuration sample different positions. Run-to-run sampling
differences there are a property of the non-blocking contract, not of these
rewrites.

23.5x at p50, 19x at p95. Against Decision 0011's `p95 < 20 ms` contract that is
3% of budget, and the p99 falls from 18.4 ms -- effectively touching the limit
-- to 1.1 ms. Against the paper's 3.59 ms it is 10.5x under, on a score ~5x
longer than their corpora.

With the observation nearly free, the band becomes the cost driver again:
`-64` measures 0.321 ms against `-16`'s 0.079 ms. `-64` is kept for margin;
0.24 ms is a cheap price for 4x the backward reach.

### Why this beat the windowed alternative

A windowed observation model was explored first and measured 1.38 ms p50 at
w=512 -- 4x slower than factorizing, with a tunable parameter, a corpus
calibration, and one fatal property: **a window is position-dependent, so it
destroys the posterior mass outside itself.** Measured, full observation
re-acquires a stalled tracker in 2 notes and a +/-512 window never does.

Factorizing is position-independent. The tables derive from static pitch
profiles, so `relocalize()` survives untouched -- verified: factorized and
direct both re-acquire in 2 notes. It also needs no parameter, no per-song
tuning and no equivalence testing, because it is exact.

A `relocalize()` itself costs **0.031 ms** and rebuilds nothing. Unlike OLTW,
which accumulates a `global_cost_matrix`, an HMM forward pass is Markovian: the
entire sufficient statistic is one `forward_variable` vector, so resetting the
prior is two writes.

## Replay result: banded vs dense on a real take

Take `t20260719T210112Z-5a92.mid`, 400 notes injected at their true wall-clock
intervals through `ProcessFollower` + `VitalsMonitor`:

| | dense | banded -64 |
|---|---|---|
| HMM work/note p50 | 22.16 ms | **12.48 ms** |
| HMM work/note p95 | 38.27 ms | **19.51 ms** |
| HMM work/note max | 130.43 ms | **20.56 ms** |
| mean work/note | 25.37 ms | **12.58 ms** |
| queue depth (max / high-watermark) | 2 / 1 | 1 / 0 |

Roughly **2x on HMM compute**, and the remaining ~12.5 ms is the observation
model plus normalize/argmax, exactly as the decomposition predicted. The
transition term went from ~9.3 ms to ~0.03 ms.

Two things this replay does **not** show:

- **No backlog.** At a realistic ~5 notes/s the queue high-watermark was 0-1.
  The unbounded-backlog hypothesis for the 177 ms p99 is **not supported at this
  note rate**; it remains open only for denser passages.
- **It cannot validate equivalence.** Beat paths matched on only 29/322 samples,
  but that is a timing artifact: `observe()` is non-blocking and returns
  whichever position happens to be ready, so the *sampled sequence* differs run
  to run regardless of banding. Equivalence is established deterministically
  instead: waiting for each update, banded and dense agree **400/400 with
  max |delta| = 0.000000 beats**, and the mean row mass dropped at band 64 is
  2.0e-17 -- zero within double precision.

## Reproducing

The corpus sweep and the factorization validation both run hardware-free
against the takes already on the machine
(`~/Library/Application Support/Rubato/data/recordings` plus `runs/*/input`,
de-duplicated by pitch sequence -- 16 distinct takes, 5673 notes):

```bash
uv run --extra live python scripts/experiment_coarse_fine_follower.py --sweep
```

That script is the throwaway window experiment and its docstring carries the
band/window sweep tables. The factorization itself is covered by
`tests/accompaniment/test_factorized_observation.py` (pure numpy, no `live`
extra, <0.3 s) and the integration tests in
`tests/accompaniment/test_matchmaker_banded_transition.py`.

### Original single-take reproduction

The measurements above come from direct `PitchHMM.step()` timing against the
production reference; no hardware and no run trace is needed. Requires
`uv sync --extra live`.

## Links

- [Score Following](score-following.md)
- [Matchmaker Real Follower Tests](matchmaker-real-follower-tests.md)
- [Matchmaker Evaluation](../sources/matchmaker-evaluation.md)
- [Decision 0011](../decisions/0011-realtime-multiprocess-architecture.md)
- [Decision 0018](../decisions/0018-follower-relock-recovery.md)
- [Realtime Performance Dataflow](realtime-performance-dataflow.md)
