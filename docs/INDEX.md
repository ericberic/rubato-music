# Rubato Docs Index

Rubato is now scoped around the Chopin live-accompanist MVP.

## Agent Context Protocol

Every agent should read this file into context before substantial work. Do not
preload the whole docs directory by default.

Use this file as the routing table:

1. Read the MVP and deferred scope below.
2. Open only the additional docs needed for the task.
3. Check the linked docs pages before re-researching.
4. Add or update docs pages when new durable knowledge is learned.
5. If the docs become stale or contradictory, update this index or ask for a
   grooming pass.

The whole `docs/` directory is the Karpathy-style LLM wiki/knowledge repo. See
[Knowledge Guide](KNOWLEDGE.md) for the read/write/groom workflow and
[Karpathy's LLM wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
for the underlying pattern.

## Core Docs

- [PRD](PRD.md): product goal, success criteria, non-goals.
- [Quickstart](QUICKSTART.md): current Yamaha take-recording and coverage workflow.
- [Vision and UX Design](VISION_AND_UX_DESIGN.md): product vision, core loop,
  learning model, workflow, UI faces, aesthetic language, roadmap.
- [System Design](SYSTEM_DESIGN.md): architecture and data contracts.
- [Three-Workflow Data Model](design/THREE_WORKFLOW_OVERVIEW.md): compact score
  bundle, rehearsal, and live-performance star-model diagrams.
- [Rehearsal Take Coverage Design](design/REHEARSAL_TAKE_COVERAGE_DESIGN.md): take
  capture, offline alignment/fusion, score coverage overlay, live prior.
- [Recording Flow Redesign](design/RECORDING_FLOW_REDESIGN.md): free-take vs.
  cued-take capture UI/API redesign (see [design/](design/) for the surface
  this and future capture-flow design docs live under).
- [Mixing Mode](design/MIX_AUTHORING_MODE.md): the Data/Mix/Perform workspace
  boundary, optional score layers, shared beat-marker grammar, notation-like
  spatial envelopes, real-time audition, and durable Undo.
- [Schema & Validation Architecture](design/SCHEMA_VALIDATION_ARCH.md):
  parse-don't-validate boundary audit and the Pydantic-everywhere plan for
  persisted artifacts, WS events, and the frontend contract pipeline
  (root cause of the PR #82 guard whack-a-mole; issues #85/#83/#84).
- [Lifecycle and Artifact Model v2](design/LIFECYCLE_AND_ARTIFACT_V2.md):
  independent take analysis/disposition/profile state, durable job and run
  transitions, stable alignment candidate IDs, and v1 migration policy.
- [Architecture Brief](ARCHITECTURE_BRIEF.md): five-minute intent and system overview.
- [ML and MIR Strategy](ML_STRATEGY.md): score following and tempo strategy.
- [Experimentation](ML_EXPERIMENTATION.md): rehearsal and simulation workflow.
- [Testing](TESTING.md): deterministic and hardware testing.
- [Development](DEVELOPMENT.md): setup, branches, worktree coordination, DVC, CI.
- [Open-Source Readiness](OPEN_SOURCE_READINESS.md): secret-scan results, safe
  git-history publish procedure, asset licensing, and the pre-publish checklist.
- [Quickstart](QUICKSTART.md): current Yamaha rehearsal workflow and UI walkthrough.
- [Documentation Map](DOCUMENTATION_MAP.md): doc ownership and update protocol.
- [Knowledge Guide](KNOWLEDGE.md): compounding project knowledge.
- [Knowledge Log](LOG.md): chronological ingest, query, lint, and grooming log.
- [Obsidian Review](OBSIDIAN.md): local Obsidian review conventions.

## Knowledge Routing

- Score following, Matchmaker, ACCompanion, HeurMiT:
  [Score Following](concepts/score-following.md),
  [Matchmaker Real Follower Tests](concepts/matchmaker-real-follower-tests.md)
- Tempo modeling, expressive rendering, follow/lead behavior:
  [Accompaniment Control](concepts/accompaniment-control.md),
  [Decision 0008](decisions/0008-predictive-follow-clock.md) (predictive FOLLOW clock),
  [Decision 0010](decisions/0010-human-beat-anchors.md) (human beat anchors, reactive firing),
  [Decision 0018](decisions/0018-follower-relock-recovery.md) (follower re-localizes
  when it stalls; the seeded offline follower harness)
- Real-time worker/process model, health monitoring, and the hardware-free load
  harness: [Decision 0011](decisions/0011-realtime-multiprocess-architecture.md)
- What "ready to perform" means and why it is not a take count:
  [Decision 0012](decisions/0012-performance-readiness.md)
- **Why rehearsal exists and what it converges on** — the mental model the
  rehearsal subsystem is organized around:
  [Decision 0013](decisions/0013-rehearsal-converges-on-performance.md)
- **Why the follower always exists, and how it tells intentional silence apart
  from missing evidence** — the expectation model and per-cell authority:
  [Decision 0015](decisions/0015-follower-always-exists.md)
- **What the performer sees while playing** — the score as the whole surface,
  the always-visible beat clock, and which component owns the cursor:
  [Decision 0014](decisions/0014-cockpit-is-the-score.md)
- Rehearsal as dataset curation, fitting the Interpretation, and the fit-quality
  compass: [Rehearsal Model Workflow](concepts/rehearsal-model-workflow.md),
  [Decision 0009](decisions/0009-rehearsal-as-dataset-lifecycle.md)
- Neural audio generation and future rendering research:
  [Magenta RealTime 2](sources/magenta-realtime-2.md),
  [Decision 0003](decisions/0003-magenta-rt2-renderer-research.md)
- Realtime dataflow and scheduler inputs:
  [Realtime Performance Dataflow](concepts/realtime-performance-dataflow.md),
  [Simulated Online Harness](concepts/simulated-online-harness.md),
  [Decision 0007](decisions/0007-multi-horizon-runtime-transport.md)
- Offline alignment and deterministic accompaniment rendering:
  [Offline Alignment And Render](concepts/offline-alignment-render.md)
- Coordinate frames (timing beats/ticks and page geometry) and how beat pixel
  positions are inferred: [Score Coordinate Systems](concepts/score-coordinate-systems.md)
- Score source ingestion and canonical bundle shape:
  [Score Bundle Ingestion](concepts/score-bundle-ingestion.md),
  [Score Bundle Contract](concepts/score-bundle-contract.md),
  [Decision 0005](decisions/0005-score-bundle-v2-canonical-timeline.md),
  [Decision 0006](decisions/0006-canonical-beat-evidence-fusion.md),
  [MusicXML Excerpt Conversion](concepts/musicxml-excerpt-conversion.md)
- Local rehearsal PWA/backend workflow:
  [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md),
  [Vision UX Implementation Plan](concepts/vision-ux-implementation-plan.md)
- Yamaha human-take analysis and first real fixture:
  [Yamaha Take Analysis](concepts/yamaha-take-analysis.md)
- Solo/tutti behavior and follow/lead/hold/stop:
  [Section Policy](concepts/section-policy.md)
- Product pivot rationale:
  [Decision 0001](decisions/0001-pivot-live-accompanist.md)
- Tracker/synth starting point:
  [Decision 0002](decisions/0002-tracker-and-synth-mvp.md)
- Calibrated low-latency HDMI zones, piece-wide spatial blend, sparse
  score-authored envelopes, and gated headless-renderer research:
  [Psychoacoustic Spatial Mixing](concepts/psychoacoustic-spatial-mixing.md),
  [Decision 0016](decisions/0016-pedalboard-decoupled-spatial-synth.md)
- External REAPER/BBCSO live host, CoreMIDI boundary, and observable readiness:
  [REAPER Host](sources/reaper.md),
  [Decision 0019](decisions/0019-reaper-external-orchestra-host.md)
- Source notes:
  [ACCompanion](sources/accompanion.md),
  [Chopin Op. 11 I Source Set](sources/chopin-op11-i-source-set.md),
  [Matchmaker](sources/matchmaker.md),
  [Parangonar](sources/parangonar.md),
  [HeurMiT](sources/heurmit.md),
  [Magenta RealTime 2](sources/magenta-realtime-2.md),
  [Compound-Sound Timing](sources/danielsen-compound-sound-timing.md),
  [LG S95-Series HDMI Latency](sources/lg-s95-series-hdmi-latency.md),
  [Pedalboard](sources/pedalboard.md),
  [Yamaha CLP-795GP](sources/yamaha-clp-795gp.md),
  [Take-management UX Patterns](sources/take-management-ux-patterns.md),
  [Oguri / Kunst der Fuge MIDI](sources/oguri-kunstderfuge-midi.md),
  [Joseffy Two-Piano Reduction](sources/joseffy-reduction.md),
  [LLM Wiki Pattern](sources/llm-wiki.md)
- Source details:
  [ACCompanion Mechanics](sources/accompanion-mechanics.md),
  [ACCompanion Evaluation](sources/accompanion-evaluation.md),
  [Matchmaker Mechanics](sources/matchmaker-mechanics.md),
  [Matchmaker Evaluation](sources/matchmaker-evaluation.md),
  [HeurMiT Mechanics](sources/heurmit-mechanics.md),
  [HeurMiT Evaluation](sources/heurmit-evaluation.md)
- Knowledge operations:
  [Documentation Map](DOCUMENTATION_MAP.md),
  [Knowledge Guide](KNOWLEDGE.md),
  [Knowledge Log](LOG.md),
  [Obsidian Review](OBSIDIAN.md),
  [Decision 0004](decisions/0004-docs-memory-conventions.md)
- Local wiki convention sources:
  [Local Wiki Conventions](sources/local-wiki-conventions.md),
  [LLM Wiki Pattern](sources/llm-wiki.md)

## Runbooks

- [First Chopin MVP Excerpt](runbooks/chopin-mvp.md)
- [Yamaha MIDI Setup](runbooks/yamaha-midi.md)
- [BBCSO Secondary Audio-Zone Audition](runbooks/bbcso-audio-zone.md)
- [REAPER Orchestra Host](runbooks/reaper-orchestra-host.md)
- [Live Run Forensics](runbooks/live-run-forensics.md)
- [Known-Good Releases](runbooks/known-good-releases.md)
- [Live Take Audio Render](runbooks/live-take-audio-render.md)
- [DVC + git worktrees: shared cache](runbooks/dvc-worktrees-shared-cache.md)

## Current MVP

> Real-time accompaniment for Chopin Piano Concerto No. 1 in E minor while the soloist
> plays the solo piano part on a MIDI-capable piano.

## Deferred

- Soloist-style piano generation.
- SOLOIST vs OTHER A/B output.
- Raw audio pipelines.
- Cloud training/inference.
