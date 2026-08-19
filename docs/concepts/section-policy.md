# Section Policy

## Summary

Section policy decides whether the accompaniment follows Eric, leads without
solo input, holds, or stops.

## Current Implementation

Use a bundle-authored symbolic section map for the MVP. The split solo reference
is evidence for whether the pianist is active, but it does not own performance
policy at runtime. Movement II revision `movement2-symbolic-path-v4` declares
continuous `FOLLOW`/`LEAD` spans in `derived/sections.json`. Machine drafts that
do not yet have a usable section artifact may infer exact, unrounded solo gaps
offline; that fallback is never relabelled as reviewed musical structure.

Movement II declares its first sounding accompaniment event through the
opening as `LEAD`, ending at the first solo onset (measure 12, beat 4). The same
map owns the measure-22 orchestral interlude, the continuation after the piano
resolves around measure 52, and the later measure-104 interlude. Pressing **Perform live
with orchestra** therefore sounds measure 1 without waiting for a
piano note. At the solo entry the scheduler cancels unsounded future events but
does not panic already-sounding notes, reports `Waiting`, and changes to
`FOLLOW` after the symbolic follower has enough live Yamaha evidence.

The lead clock uses the performance profile's robust base tempo when one is
available (otherwise the movement default), adjusted by the performer's
quarter-note metronome control. `♩ = N` always means canonical score quarters:
the section's canonical duration calibrates its denser expressive
reference-MIDI clock. Changing it while live reanchors the current
reference-MIDI position and replaces future mutable events immediately; it does
not cut a note already sounding. Follower observations are diagnostic-only
while `LEAD` owns the clock; even a confident estimate beyond the boundary
cannot end the passage early. A pickup inside the authored entry window may be
buffered after two locally stable Matchmaker estimates (or ordinary confident
lock) and applied only after the endpoint. Its original observation timestamp
is retained, so an early re-entry is neither lost nor allowed to truncate the
orchestra.
Piano or MIDI-loopback notes cannot modulate an autonomous orchestral passage.
Rehearsal takes do not replace the score follower
or teach pitch identity: the score bundle remains the pitch/location reference;
takes currently contribute the opening tempo prior.

Selecting a printed measure for **Perform live** does not bypass section policy.
The measure resolves to canonical score identity and then once through the
reference warp. After a four-quarter count-off, the section containing that
beat owns startup: `FOLLOW` warm-starts tracker/timing state, `LEAD` owns an
absolute future downbeat immediately, `HOLD` waits for the authored exit, and
`STOP` is rejected before MIDI side effects. FOLLOW's count-off-derived clock is
confidence-zero `CUE_ENTRY`, distinct from follower-dropout `COAST`; the human
count-off cue authorizes the shared downbeat, and the entry expires into HOLD if
piano evidence never arrives. The selected beat and section mode are written to the
runtime trace so a wrong-coordinate start desync cannot hide as a UI choice.

Scheduler lookahead is section-bounded. `FOLLOW` cannot pre-commit events from
the next `LEAD` section using solo timing. At the boundary, ownership changes
first and the new mode creates its own plan. This is essential at measure 22:
the piano cadence reaches the downbeat, then the authored orchestra clock owns
the remaining interlude through the exclusive measure-23 boundary.

During `FOLLOW`, a sub-threshold tracker update is not admitted into the tempo
model. Rubato instead coasts for at most 1.5 seconds from the last confident
symbolic/reference clock, keeping cursor and accompaniment on the same timing
state. If lock does not recover, policy enters `HOLD`, cancels the mutable plan,
silences output once, and reports that it is waiting to relock. Both the coast
and its expiry are explicit trace transitions.

## Modes

- `FOLLOW`: align to solo input and schedule accompaniment from estimated tempo.
- `LEAD`: continue according to score tempo or configured tempo curve.
- `HOLD`: wait for a cue or solo re-entry.
- `STOP`: panic/stop output.

These discrete names currently express structural endpoint policy on one
transport, not two unrelated clocks. `COAST`, lead-entry hold, and dropout hold
are distinct runtime authority states even though the public section vocabulary
remains four modes. A future variance-aware controller may interpolate in mixed
material, but Rubato will not blend uncalibrated follower confidence with a
prior merely because both are numeric. The measure-22 cadence is a specific
asymmetric handoff: the piano owns the arrival phase, but its ritard does not
become the orchestra's outgoing tempo.

## Details

Chopin concerto accompaniment requires this because the soloist is silent during
tutti or orchestral transitions. A pure follower cannot know how to move through
long no-input regions without section knowledge.

## Open Questions

- Which bundle-authored Movement II boundaries need further musical review?
- Should cue notes be added for solo re-entry detection?
- What is the safest panic behavior for the Yamaha/VST output route?

## Related

- [Score Following](score-following.md)
- [Accompaniment Control](accompaniment-control.md)
- [System Design](../SYSTEM_DESIGN.md)
