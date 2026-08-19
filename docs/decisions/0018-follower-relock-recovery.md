# Decision 0018 — The follower re-localizes when it stalls

Status: **Accepted** (2026-08-12)

## The problem, measured

Replaying real rehearsal takes through the live follower stack (seeded at the
cue-in, mapped to canonical beats) exposed a take that the tracker never
recovered from. Take `t20260718T174455Z-3da4` mis-locks in its opening
(m13–24), falls steadily behind, and stays **~70 canonical beats** behind for
the entire piece — the orchestra would be in the wrong place the whole take.
Yet the *same take is perfectly trackable*: seeded past the opening (start m25 or
m40) it tracks at ~4 beats, exactly like a clean take
(`t20260720T023701Z-3ccb`, ~4 beats end to end). The failure is not the
material and not the tempo (error was invariant to the tempo hint) — it is that
**a tracker that falls behind early has no way to re-acquire.** PTHMM's prior is
seeded once (at construction, or at a known cue-in beat via
`reposition_for_entry`), always as a *narrow* distribution; nothing ever widens
it again.

This surfaced only because the offline follower harness was fixed first: it had
been cold-starting the matcher at beat 0 while takes begin mid-piece, so *every*
take looked ~65 beats lost and no comparison was trustworthy. Seeding the
harness like the live worker (`follower_take_eval`) collapses a clean take from
64 → 4 beats and made the real, take-specific failure visible.

## Decision

Add a **global re-search** to the tracker and a **watchdog** that triggers it.

### 1. `MatchmakerStreamFollower.relocalize()`

A counterpart to `reposition_for_entry`: instead of centering a narrow prior on
a known beat, `_broaden_pthmm_prior` resets the PTHMM initial distribution to a
broad prior and clears the forward variable, so the next observations
re-localize the tracker from the pitch stream. The re-search is **forward-biased
from the tracker's current estimate** (uniform over `[current - small slack,
end]`), not globally uniform: a stalled tracker is always stalled *behind* the
performer, and the Romance restates its opening near the end, so a uniform
re-seed jumps *backward* to the wrong restatement -- an early attempt at a global
re-seed sent two end-of-piece takes ~400 beats backward. Forward-bias lets the
tracker leap ahead to catch up while forbidding the backward mis-lock. Same
mid-stream seam `reposition_for_entry` uses; transition and observation models
untouched. `ProcessFollower` proxies it to the subprocess worker by the same
fire-and-forget command path as reposition, so it works in the default
out-of-process configuration.

### 2. `RelockingFollower` watchdog

Wraps the canonical follower (so it watches the score beat the orchestra
actually acts on) and calls `relocalize` when the reported position advances
less than `min_advance_beats` (4) over `window_seconds` (8) **despite** at least
`min_updates` (25) note updates in that span, with a `cooldown_seconds` (6)
guard. The update-count gate is the safety: rests, holds, and ritardandos emit
too few updates to trip it, so only sustained playing that makes no score
progress does. It re-searches the raw tracker underneath the canonical mapping,
so it is coordinate-agnostic.

Enabled by default (`RuntimeConfig.follower_relock_enabled`). Being stuck tens
of beats behind for a whole performance is catastrophic; the downside of a rare
re-search is bounded because a broad re-seed re-localizes to the *correct*
position from the pitches (in the eval, extra re-seeds on the clean take still
landed at ~4 beats).

## Evaluation

Through the shipped stack on **all 17 prior recordings that have reviewed ground
truth**, seeded like live, relock off vs on (gross median canonical-beat error):

- **2 recovered, 0 regressed.** The stuck opening take goes **70.0 -> 4.0**; the
  two end-of-piece takes a *uniform* re-seed had broken (6.9 -> 418, 14.8 -> 241)
  instead **improve** under the forward-biased re-seed (6.9 -> 3.5, 14.8 -> 7.7).
- Every one of the 15 already-tracking takes is **unchanged** (delta <= 0.1
  beats): the watchdog does not fire on a follower that is keeping up.

The full-set run is how the backward-jump regression was caught -- two takes had
hidden it -- and the forward-bias is what turned those two regressions into
recoveries.

## Limitations

- Validated offline across the take library, not yet live. A pathological long
  trill/tremolo (many notes, little score progress) is the most plausible false
  trigger; its cost is now doubly bounded -- a *forward* re-search from the
  current position barely moves a follower that is already keeping up.
- Forward-biased re-seed assumes the stall is always a *lag*. A tracker stuck
  *ahead* (not observed) would not be helped; the small backward slack forgives
  only a slight overshoot.
- The offline harness (`follower_take_eval`) is untimed; it cannot reproduce
  real-time GIL/scheduling pressure. It measures tracking accuracy, not the felt
  live result.

## Consequences

- New `follower_take_eval` module: the *correct*, seeded offline follower harness.
  Cold-starting is now a deliberate `seed=False` A/B, not the default.
- `relocalize()` on `MatchmakerStreamFollower` and `ProcessFollower`;
  `RelockingFollower` wrapper; five `follower_relock_*` config fields.

## Links

- [Decision 0015](0015-follower-always-exists.md) (the follower always exists)
- [Decision 0011](0011-realtime-multiprocess-architecture.md) (out-of-process follower)
- [Score Following](../concepts/score-following.md)
