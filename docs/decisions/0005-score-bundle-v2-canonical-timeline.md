# Decision 0005: Score Bundle v2 Uses a Canonical Score Timeline

## Status

Accepted, 2026-07-12.

## Context

Rubato's sources are different editions and coordinate systems. The polished
piano-reduction PDF is the best performer-facing engraving; the Oguri MIDI is a
useful full-orchestra performance; reviewed MusicXML/OMR can describe notation
and layout. Existing code treated scalar MIDI-derived beats, MusicXML layout,
and PDF pages as if they were interchangeable. They are not.

The first runtime event bundle also predates the three workflows: score bundle
sourcing, rehearsal takes, and live performance. It cannot express source
roles, partial correspondences, revisions, or readiness.

## Decision

Adopt Score Bundle v2 as a star model centered on an exact, notation-derived
integer `score_tick` timeline. Preserve measure index/label and beat projection
for human communication. Declare every source and derived artifact role in a
strict versioned manifest. Represent PDF, expressive MIDI, MusicXML, and take
correspondences as first-class, partial, confidence-bearing mappings with
explicit gaps.

Keep the existing event-oriented `ScoreBundle` behind
`LegacyScoreBundleAdapter` until runtime callers migrate. Do not silently infer
v2 identity or source roles from legacy filenames.

## Consequences

- Movement 2 can use the reduction PDF for cockpit communication and the Oguri
  orchestral MIDI for playback without claiming their native coordinates match.
- Alignment and display-map construction become explicit build steps.
- Pickup measures, meter changes, unusual printed labels, edition gaps, and
  revised mappings have stable representations.
- Derived artifacts and takes can pin a `(bundle_id, revision, timeline_id)`.
- A valid but incomplete bundle has workflow-specific readiness instead of one
  ambiguous "valid" state.
- MIDI tick equality and PDF/MusicXML page-count equality are no longer global
  assumptions.

## Rejected Alternatives

- Use Oguri ticks directly as score beats: loses notation identity because its
  expressive performance timing is encoded in tick spacing.
- Make the reduction PDF authoritative: pixels do not provide symbolic meter,
  repeat, part, or scheduler semantics.
- Require one file to supply semantic, display, and playback roles: prevents
  using the best artifact for each job and does not match available sources.
- Rewrite the runtime bundle immediately: needlessly destabilizes working
  follower/scheduler tests; an explicit adapter permits staged migration.

## Revisit Triggers

Revisit canonical PPQ only if imported notation cannot be represented exactly
at 960 PPQ. Revisit mapping segment structure when real Movement 2 alignment or
display geometry reveals a necessary non-numeric coordinate primitive; retain
the same explicit-role and partial-mapping principles.

## Links

- [Score Bundle v2 Contract](../concepts/score-bundle-contract.md)
- [Score Bundle Ingestion](../concepts/score-bundle-ingestion.md)
- [Decision 0006](0006-canonical-beat-evidence-fusion.md)
- [System Design](../SYSTEM_DESIGN.md)
