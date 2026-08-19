# ML and MIR Strategy

## Current Strategy

Rubato's near-term intelligence is mostly MIR and control, not generative ML.

The system must solve:

- Real-time MIDI score following.
- Offline score-performance alignment for evaluation.
- Tempo and onset prediction.
- Section-aware accompaniment scheduling.
- Expressive response to soloist timing and dynamics.

The first reliable version should prefer proven probabilistic and dynamic
programming methods over neural score following.

## Why Not Start With Style Transfer

Style transfer needs aligned score/performance data, stable symbolic ingestion,
metrics, and feedback loops. The live accompanist MVP needs the same foundation
but produces a more concrete musical outcome sooner.

The old style-transfer idea is deferred, not discarded.

## Recommended Algorithm Stack

### Score Following

Start with a Matchmaker wrapper, not a home-grown tracker. Matchmaker gives us a
clean Python API for simulation, live MIDI input, MusicXML/MIDI score files, and
score-position output in beats.

Use ACCompanion as the reference architecture for the full accompanist behavior:
live MIDI input/output, score following, tempo modeling, and expressive
accompaniment adaptation. Its demos and paper show the behavior we want, but its
repo is older/heavier than Matchmaker for first integration.

Do not start with HeurMiT. It is a valuable neural research direction, but the
paper explicitly reports that its limitations prevent practical real-world score
following.

Implementation order:

- Matchmaker simulation mode against recorded Eric MIDI.
- Matchmaker live MIDI mode or `BytesMidiStream` from the PWA/backend.
- Compare `pthmm`, `hmm`, `arzt`, and `dixon` on the Chopin excerpt.
- Add ACCompanion-inspired tempo and expressive rendering layers above the
  follower output.

### Offline Symbolic Correspondence

Score-bundle construction and take review must infer musical identity before
timing. Use constrained dynamic programming over ordered note/chord evidence:
exact pitches and chord sets first, with bass/soprano contour, intervals,
pitch-class harmony, and event density as tolerant supporting features. Sparse
verified landmarks bound repeated or ambiguous passages. The result is a
reference-MIDI ↔ canonical-beat path with confidence and explicit gaps.

Only after that path exists should Rubato fit expressive source time or a
take-specific warp. Tempo distance is a weak regularizer and diagnostic, never
the authority for measure advancement. This same separation applies online:
the follower estimates score state from MIDI symbols, while the tempo model
predicts when the next already-identified state will occur.

### Tempo Modeling

Implement a local tempo layer above score position estimates:

- Smooth erratic instantaneous IOI estimates.
- Predict the next accompaniment onset.
- Handle ritardando, accelerando, fermatas, and cadential broadening.
- Switch behavior by section: follow, lead, hold, stop.

### Expressive Rendering

Start simple:

- Keep orchestral pitches and notated durations from the score.
- Time-stretch accompaniment to the current tempo model.
- Scale orchestral velocity using local solo intensity and section defaults.

Then iterate:

- Phrase-level dynamic curves.
- Anticipatory entrances.
- Rehearsal-specific tempo priors.
- Eric-specific preferences.

## Data Plan

### Required Project Data

- Clean MusicXML or MIDI score for Chopin Piano Concerto No. 1.
- Solo piano part isolated from the score.
- Orchestral accompaniment parts isolated from the score.
- Section map with follow/lead/hold behavior.
- Eric rehearsal MIDI takes.
- Optional reference recordings or MIDI performances for tempo priors.

### First Excerpt Selection

Choose a short excerpt that includes:

- Solo piano material with rubato.
- An orchestral response or accompaniment layer.
- At least one solo silence or tutti-style transition if possible.

Avoid starting with the entire concerto. Full-concerto support is a product
milestone after excerpt-level reliability.

## Evaluation

Automatic metrics:

- Alignment error in milliseconds and beats.
- Missed or duplicated score positions.
- Accompaniment onset error.
- Recovery time after pause or wrong note.
- MIDI output jitter.

Human metrics:

- Stayed with me.
- Entered correctly.
- Felt too rigid.
- Felt too reactive.
- Led correctly in tutti.
- Playable enough to rehearse.

## Research Position

The current practical stack is Matchmaker + ACCompanion-inspired control. Neural
followers such as HeurMiT remain research references until they demonstrate
robustness to tempo-scale mismatch, repeated patterns, skips, and long-range
structure in live accompaniment.

Magenta RealTime 2 is a meaningful SOTA advance for local neural audio
generation, not for score following. It can become a later renderer or
generative texture layer, but the MVP should not depend on it for exact Chopin
accompaniment.

### Where Rubato's performance model fits

The rehearsal profile and live score follower are complementary, not competing
models:

| Problem | Research analogue | Rubato now |
| --- | --- | --- |
| Offline note identity | nASAP/Parangonar hierarchical or anchor-constrained symbolic alignment | Seeded MIDI-to-MIDI alignment projected once onto canonical score ticks |
| Expressive description | ACCompanion and nASAP beat period, velocity, articulation, microtiming/chord spread | Explicit seconds-per-quarter, normalized rubato ratio, velocity, and pedal at 480-tick cells |
| Repeated-performance prior | ACCompanion/Arzt-Widmer multiple reference performances | Robust median/MAD profile plus retained individual aligned takes |
| Live position | HMM/OLTW score following; Matchmaker benchmark implementations | Follower seam, Matchmaker `pthmm` integration baseline, canonical output target |
| Live prediction/control | ACCompanion tempo expectation and synchronization models | Online tempo model must blend live evidence with the frozen rehearsal profile |

The parameterization is deliberately conservative and research-aligned. The
[ACCompanion paper](https://www.ijcai.org/proceedings/2023/0641.pdf) encodes
beat period, MIDI velocity, microtiming, and log articulation ratio. The
[nASAP alignment study](https://transactions.ismir.net/articles/10.5334/tismir.149)
uses aligned local tempo, articulation, and chord spread for expressive
analysis. Rubato implements the first timing/dynamic foundations and pedal;
microtiming, articulation, and chord spread remain explicit next parameters,
not facts inferred by an LLM.

Rubato's robust median/MAD fusion is simpler than a learned hierarchical
expressive-performance model. That is intentional: we have three passage
repetitions from one performer, not a population-scale training corpus. It is
deterministic, inspectable, and resistant to one mistake. It does not yet learn
phrase-level latent factors or generate a complete interpretation from score
features alone.

For live tracking, the strongest relevant result remains multiple-reference
OLTW: ACCompanion aligns the live performance to previously recorded,
score-aligned performances and ensembles their positions. The 2025
[Matchmaker study](https://www.jku.at/fileadmin/gruppen/173/Research/Rach_3_Project/ParkEtAl-ISMIR-2025.pdf)
found its Arzt OLTW variant strongest in a large **audio** benchmark and
provides the reproducible evaluation framework, but that result does not by
itself validate Rubato's live Yamaha MIDI path. HeurMiT remains a useful neural
research comparison, but its own 2025 paper reports that current limitations
prevent practical real-world use.

See [Decision 0002](decisions/0002-tracker-and-synth-mvp.md) and
[Decision 0003](decisions/0003-magenta-rt2-renderer-research.md).
