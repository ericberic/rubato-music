# Offline Alignment And Render

## Purpose

Offline alignment is the deterministic bridge between a recorded Yamaha solo
take and an accompaniment render. It lets us debug score-following assumptions
before live scheduling.

## Components

- `extract_note_events`: reads MIDI note-on events with absolute seconds.
- `align_note_events`: aligns performed note pitches to reference note pitches.
- `timing_map_from_alignment`: builds a piecewise-linear map from reference time
  to performed time using pitch-matched anchors.
- `phrase_timing_anchors_from_alignment`: groups nearby matched notes into
  phrase-level timing anchors using median reference/performance times.
- `retime_midi`: renders accompaniment MIDI through that timing map.
- `write_alignment_artifacts`: writes trace and metrics under a run directory.

These are intentionally separated so later Matchmaker/ACCompanion followers can
replace only the alignment/follower component while the timing map and renderer
remain reusable.

## CLI

Align only:

```bash
uv run rubato align-midi \
  --reference data/scores/chopin_op11_movement_2/derived/solo_reference.mid \
  --reference-track-marker "PIANO SOLO" \
  --performance data/processed/<session_id>/solo.mid \
  --run-id <run_id>
```

Align and render accompaniment:

```bash
uv run rubato render-offline \
  --reference data/scores/chopin_op11_movement_2/derived/solo_reference.mid \
  --reference-track-marker "PIANO SOLO" \
  --performance data/processed/<session_id>/solo.mid \
  --accompaniment data/scores/chopin_op11_movement_2/derived/orchestra_accompaniment.mid \
  --run-id <run_id>
```

Outputs:

- `runs/<run_id>/trace/alignment.jsonl`
- `runs/<run_id>/analysis/metrics.json`
- `runs/<run_id>/output/accompaniment.mid`

## PWA / Backend

The local FastAPI server currently exposes the same workflow for rehearsal
sessions:

```bash
POST /api/sessions/{session_id}/render-offline
```

For the current MVP this uses the checked-in Oguri movement-2 solo reference and
orchestra accompaniment by default. Input is
`data/processed/<session_id>/solo.mid`; output is
`runs/<session_id>/output/accompaniment.mid`.

The PWA calls this endpoint from session plumbing in the Library, then exposes
the generated `accompaniment` MIDI through the existing browser player,
scrubber, download link, and backend Yamaha hardware playback.

This is developer/Reflection plumbing, not the at-piano product contract. Take
capture and accompanied review are now owned end to end by `/api/takes`; they
do not pass a take ID through this session route or depend on the legacy
session-shaped capture copy under `data/processed/`.

## Take-native accompanied review

For an aligned take, the review worker:

1. Reads the take's immutable `take.mid` and authoritative
   `aligned.v2.json` timing map.
2. Converts the aligned score span to source-reference seconds and creates a
   piecewise-linear performed-time map from the persisted anchors. It does not
   run a second independent pitch alignment.
3. Finds the first aligned musical onset and trims the armed/hand-positioning
   silence from the *derived review only*. It shifts the solo, orchestra timing
   map, and score transport by the same amount; immutable `take.mid` keeps the
   complete raw evidence. The worker writes `solo.mid`, crops and retimes the
   orchestra to the aligned span as `accompaniment.mid`, then combines both on
   one 120 BPM transport timeline as `ensemble.mid`.
4. Persists an idempotent `render_review` job keyed by a hash of the take and
   alignment inputs. Queued work resumes at server startup; running work
   interrupted by restart is requeued with an explicit failure transition.
5. Publishes `take:review_status`; `TakeResponse.review` and
   `GET /api/takes/{take_id}/review` provide recovery snapshots.

`POST /api/takes/{take_id}/review` prepares or retries the artifact.
`GET /api/takes/{take_id}/review/midi?variant=solo|ensemble` returns the chosen
review for browser preview/download. `POST /api/takes/{take_id}/review/play`
sends the chosen variant to the selected backend Yamaha output through the
shared hardware slot; an optional displayed `start_score_beat` is interpolated
through the persisted dense score transport so the MIDI window and cursor begin
at the same aligned take-relative instant. The UI automatically requests
preparation for the latest aligned take and narrates queued/running/failed/ready
state on the **After this take** card.

The source of truth is deliberately layered. Canonical notation owns musical
identity. The offline symbolic-path artifact maps ordered Oguri reference notes
to those canonical measure/beat states. `aligned.v2.json` maps the take's MIDI
notes to the Oguri reference notes, and only then fits performed wall time. The
rendered orchestra follows that same fitted map. The red cursor composes those
correspondences and never derives musical identity from time or PDF pixels;
PDF/OMR geometry only determines where an already-known printed position is
painted. Sparse verified landmarks constrain repeated or ambiguous symbolic
windows without replacing automatic alignment.

The CLI and session endpoint remain valuable for experiments and file-level
inspection, but the normal UI does not ask Eric to run them. See
[PWA Rehearsal UI](pwa-rehearsal-ui.md#take-native-accompanied-review)
and [Vision and UX Design](../VISION_AND_UX_DESIGN.md#64-the-after-face).

## Current Limitations

- Alignment is pitch-sequence local alignment, not yet a production score
  follower.
- It treats chord ordering and rolled attacks naively.
- Phrase anchors now provide a first smoothing abstraction, but offline render
  still defaults to note anchors until enough real take data validates the
  phrase settings.
- The renderer retimes MIDI messages in seconds and writes a fixed-tempo output
  MIDI. That is sufficient for playback validation, but future renderers should
  preserve richer tempo/source metadata when useful.
- The review faithfully uses the current machine alignment and expressive
  Oguri MIDI projection. Those coordinates are still provisional:
  `canonical_position: false` and `performance_ready: false` remain true until
  the Movement 2 semantic timeline, sections, and instrument policy are
  musically reviewed.

## Next Step

The next ML/research slice should turn phrase anchors into confidence-bearing
render policy:

- classify extra/missing/mismatched notes as likely mistake, ornament, or
  alignment uncertainty;
- smooth the timing map so expressive rubato is preserved without copying local
  note mistakes into the accompaniment.
- compare note-anchor and phrase-anchor renders against Eric's listening
  feedback on the same recorded take.

## Related

- [Yamaha Take Analysis](yamaha-take-analysis.md)
- [Experimentation](../ML_EXPERIMENTATION.md)
- [System Design](../SYSTEM_DESIGN.md)
