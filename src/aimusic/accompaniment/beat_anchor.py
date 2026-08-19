"""Validate and correct the Oguri->measure/beat map, without measuring time by hand.

This is the deterministic core behind the "Validate & correct" phase of the
score-localization skill. It exists because a human is excellent at *judgement*
("that note is beat 3", "that click is a bar early") and terrible at
*measurement* ("that onset is 180 ms late"). So nothing here ever asks a person
for a duration:

- The human supplies **identity** -- which measure a sound is, or which note
  carries a beat -- by ear against the score.
- The machine supplies **precision** -- the exact onset is already sitting in
  the reference MIDI at tick resolution; a correction *selects* it, never
  estimates it.

Two objects are being validated and they are NOT the same thing (see
docs/concepts/score-coordinate-systems.md):

- **MIDI timing** -- the beat map's ``source_midi_tick`` / ``source_seconds``:
  where in the Oguri performance each canonical beat sounds. That is what this
  module operates on.
- **PDF geometry** -- ``pdf_x`` on the same anchors: where a beat is *drawn* on
  the Joseffy page. This module never touches geometry; correcting timing does
  not move a box and vice versa.

The runtime never runs any of this while the performer plays; corrections are
persisted as :class:`HumanBeatCorrection` and applied at projection time.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import mido

from aimusic.accompaniment.score_fusion import (
    EvidenceKind,
    HumanBeatCorrection,
    HumanCorrectionDocument,
    PerformanceBeatAnchor,
    PerformanceBeatMapDocument,
)

DEFAULT_SOLO_TRACK = "PIANO SOLO"
# Half a beat at the movement's slow Larghetto pace comfortably brackets the
# note that carries a beat while excluding the neighbouring beats' notes.
DEFAULT_WINDOW_SECONDS = 1.5
# Two candidate onsets closer than this are indistinguishable to the ear as
# "the beat", so a pitch match that only wins by less is called ambiguous
# rather than silently chosen.
DEFAULT_AMBIGUITY_SECONDS = 0.12
# A beat every ~this many bars is auditioned even when the cross-check does not
# flag it, to catch a systematic offset both alignments share (the cross-check
# is blind to an error common to both). See the skill's Cardinal Rule.
DEFAULT_SPINE_STEP = 15


# --------------------------------------------------------------- reference MIDI


@dataclass(frozen=True)
class OnsetEvent:
    """One note-on in the reference performance, in its own two coordinates."""

    native_tick: int
    seconds: float
    pitch: int
    track_name: str = ""


@dataclass(frozen=True)
class OnsetGroup:
    """A rolled or simultaneous orchestral attack presented as one sound."""

    native_tick: int
    seconds: float
    pitches: tuple[int, ...]
    track_names: tuple[str, ...]


def reference_onsets(
    midi_path: Path | str, track_filter: str | None = DEFAULT_SOLO_TRACK
) -> tuple[OnsetEvent, ...]:
    """Note-ons from the reference MIDI, carrying both native tick and seconds.

    ``native_tick`` is the coordinate the beat map's ``source_midi_tick`` lives
    in, so a snapped correction can be persisted at exact tick precision.
    ``seconds`` is the tempo-aware wall time used to place a correction near a
    beat's current hypothesis. Both come straight from the file; neither is
    estimated by a human.
    """

    midi = mido.MidiFile(str(midi_path))
    ppq = midi.ticks_per_beat
    tempos: list[tuple[int, int]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                tempos.append((tick, message.tempo))
    tempos.sort()

    def seconds(target: int) -> float:
        total = 0.0
        last = 0
        current = 500000
        for tick, tempo in tempos:
            if tick >= target:
                break
            total += mido.tick2second(tick - last, ppq, current)
            last, current = tick, tempo
        return total + mido.tick2second(target - last, ppq, current)

    out: list[OnsetEvent] = []
    for track in midi.tracks:
        name = (track.name or "").strip()
        if track_filter and track_filter.lower() not in name.lower():
            continue
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                out.append(
                    OnsetEvent(
                        native_tick=tick,
                        seconds=seconds(tick),
                        pitch=message.note,
                        track_name=name,
                    )
                )
    out.sort(key=lambda event: (event.seconds, event.native_tick, event.pitch))
    return tuple(out)


def group_onsets(
    onsets: tuple[OnsetEvent, ...], merge_window_seconds: float = 0.35
) -> tuple[OnsetGroup, ...]:
    """Collapse one orchestral attack into a musician-readable chord candidate.

    Oguri's ensemble attacks are naturally spread by a few dozen milliseconds;
    exposing every instrument as a separate correction choice would ask the
    performer to reason about MIDI implementation details. The earliest event
    retains exact tick identity while the full pitch/instrument set describes
    the sound they heard.
    """

    if merge_window_seconds <= 0:
        raise ValueError("merge_window_seconds must be positive")
    if not onsets:
        return ()
    groups: list[list[OnsetEvent]] = []
    for onset in sorted(onsets, key=lambda event: (event.seconds, event.native_tick, event.pitch)):
        if not groups or onset.seconds - groups[-1][0].seconds > merge_window_seconds:
            groups.append([onset])
        else:
            groups[-1].append(onset)
    return tuple(
        OnsetGroup(
            native_tick=group[0].native_tick,
            seconds=group[0].seconds,
            pitches=tuple(sorted({event.pitch for event in group})),
            track_names=tuple(sorted({event.track_name for event in group if event.track_name})),
        )
        for group in groups
    )


# --------------------------------------------------------------- beat hypothesis


@dataclass(frozen=True)
class BeatHypothesis:
    """What the current map believes about one beat -- the thing under test."""

    measure_label: str
    beat_in_measure: float
    score_tick: int
    source_midi_tick: int
    source_seconds: float
    confidence: float
    # True when a real matched reference note underlies this beat; False when it
    # was interpolated. An interpolated beat has nothing to snap to and nothing
    # audible to judge, so the audition should not ask about it.
    anchored: bool


# Evidence kinds that stand on a real, human-verified position even with no
# symbolic-matcher note count: a reviewed downbeat or a human correction is an
# anchor, not an interpolation, so the audition must not hide it.
_ANCHORED_KINDS = frozenset({EvidenceKind.REVIEWED_DOWNBEAT, EvidenceKind.HUMAN_CORRECTION})


def _anchor_is_anchored(evidence: tuple) -> bool:
    return any(
        getattr(item, "matched_notes", 0) > 0 or getattr(item, "kind", None) in _ANCHORED_KINDS
        for item in evidence
    )


def beat_hypotheses(
    beat_map: PerformanceBeatMapDocument, measure_label: str
) -> tuple[BeatHypothesis, ...]:
    """Every beat the map places in one measure, in beat order."""

    hypotheses = [
        BeatHypothesis(
            measure_label=anchor.measure_label,
            beat_in_measure=anchor.beat_in_measure,
            score_tick=anchor.score_tick,
            source_midi_tick=anchor.source_midi_tick,
            source_seconds=anchor.source_seconds,
            confidence=anchor.confidence,
            anchored=_anchor_is_anchored(anchor.evidence),
        )
        for anchor in beat_map.anchors
        if anchor.measure_label == measure_label
    ]
    hypotheses.sort(key=lambda h: h.beat_in_measure)
    return tuple(hypotheses)


def beat_hypothesis(
    beat_map: PerformanceBeatMapDocument, measure_label: str, beat_in_measure: float
) -> BeatHypothesis | None:
    for hypothesis in beat_hypotheses(beat_map, measure_label):
        if abs(hypothesis.beat_in_measure - beat_in_measure) < 1e-6:
            return hypothesis
    return None


# ------------------------------------------------------------------ snap to note


@dataclass(frozen=True)
class SnapCandidate:
    native_tick: int
    seconds: float
    pitch: int
    delta_seconds: float  # candidate seconds minus the beat's current hypothesis


@dataclass(frozen=True)
class SnapResult:
    """The onset a correction should select, with its runner-up for honesty."""

    chosen: SnapCandidate | None
    runner_up: SnapCandidate | None
    ambiguous: bool
    reason: str


def candidate_onsets(
    onsets: tuple[OnsetEvent, ...],
    near_seconds: float,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> tuple[SnapCandidate, ...]:
    """Reference onsets within a window of a beat's hypothesis, nearest first.

    These are the real notes the human chooses among by ear. Picking one is an
    identity judgement; its exact time comes free from the MIDI.
    """

    candidates = [
        SnapCandidate(
            native_tick=onset.native_tick,
            seconds=onset.seconds,
            pitch=onset.pitch,
            delta_seconds=onset.seconds - near_seconds,
        )
        for onset in onsets
        if abs(onset.seconds - near_seconds) <= window_seconds
    ]
    candidates.sort(key=lambda candidate: abs(candidate.delta_seconds))
    return tuple(candidates)


def snap_to_pitch(
    candidates: tuple[SnapCandidate, ...],
    target_pitch: int,
    ambiguity_seconds: float = DEFAULT_AMBIGUITY_SECONDS,
) -> SnapResult:
    """Pick the candidate onset carrying ``target_pitch`` nearest the hypothesis.

    Matches exact MIDI pitch first, then pitch class (tolerating octave and OMR
    noise). The nearest match wins; a second equally-good match within
    ``ambiguity_seconds`` makes the result ``ambiguous`` -- a signal to ask the
    performer which onset, not to guess. ``target_pitch`` is the identity a human
    supplies (the note that carries the beat); the returned time is the
    machine's.
    """

    if not candidates:
        return SnapResult(
            chosen=None, runner_up=None, ambiguous=False, reason="no candidate onsets in window"
        )

    exact = tuple(c for c in candidates if c.pitch == target_pitch)
    pool = exact or tuple(c for c in candidates if c.pitch % 12 == target_pitch % 12)
    match_kind = "exact pitch" if exact else "pitch class"
    if not pool:
        return SnapResult(
            chosen=None,
            runner_up=None,
            ambiguous=False,
            reason=f"no onset matching pitch {target_pitch} in window",
        )

    ranked = sorted(pool, key=lambda candidate: abs(candidate.delta_seconds))
    chosen = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    ambiguous = runner_up is not None and (
        abs(runner_up.delta_seconds) - abs(chosen.delta_seconds) < ambiguity_seconds
    )
    reason = f"{match_kind} match {chosen.delta_seconds:+.3f}s from hypothesis"
    if ambiguous:
        reason += " (AMBIGUOUS: a second onset is nearly as close -- confirm by ear)"
    return SnapResult(chosen=chosen, runner_up=runner_up, ambiguous=ambiguous, reason=reason)


# ------------------------------------------------------------- suspect worklist


@dataclass(frozen=True)
class SuspectMeasure:
    """A measure the audition should visit, and why."""

    measure_label: str
    reason: str  # "both_disagree" or "spine"
    hypothesis_downbeat_seconds: float | None
    disagreement_seconds: float | None


def _downbeat_seconds(beat_map: PerformanceBeatMapDocument) -> dict[str, float]:
    return {
        anchor.measure_label: anchor.source_seconds
        for anchor in beat_map.anchors
        if abs(anchor.beat_in_measure) < 1e-6
    }


def confirmed_defects(
    downbeats_a: dict[str, float],
    downbeats_b: dict[str, float],
    map_downbeats: dict[str, float],
    tolerance: float = 2.0,
) -> tuple[SuspectMeasure, ...]:
    """Measures where BOTH independent alignments disagree with the beat map.

    A bar flagged by only one alignment is that alignment's own error; a bar
    both reject is a real map defect. This is the intersection the skill's
    Cardinal Rule requires -- two sides with independent provenance.
    """

    defects: list[SuspectMeasure] = []
    for label, mapped in sorted(map_downbeats.items(), key=lambda kv: _label_key(kv[0])):
        if label not in downbeats_a or label not in downbeats_b:
            continue
        da = mapped - downbeats_a[label]
        db = mapped - downbeats_b[label]
        if abs(da) > tolerance and abs(db) > tolerance:
            defects.append(
                SuspectMeasure(
                    measure_label=label,
                    reason="both_disagree",
                    hypothesis_downbeat_seconds=mapped,
                    disagreement_seconds=max(abs(da), abs(db)),
                )
            )
    return tuple(defects)


def _label_key(label: str) -> tuple[int, str]:
    try:
        return (int(label), "")
    except ValueError:
        return (10**9, label)


def suspect_measures(
    downbeats_a: dict[str, float],
    downbeats_b: dict[str, float],
    map_downbeats: dict[str, float],
    tolerance: float = 2.0,
    spine_step: int = DEFAULT_SPINE_STEP,
) -> tuple[SuspectMeasure, ...]:
    """The audition worklist: confirmed defects plus a sparse systematic spine.

    Defects come from the cross-check. The spine is a bar every ``spine_step``
    measures that the cross-check did NOT flag, included because a whole-piece
    offset shared by both alignments is invisible to their intersection; a few
    spread checks catch it. Spine bars carry no disagreement figure.
    """

    defects = confirmed_defects(downbeats_a, downbeats_b, map_downbeats, tolerance)
    flagged = {d.measure_label for d in defects}
    spine: list[SuspectMeasure] = []
    if spine_step > 0:
        numeric = sorted(
            (label for label in map_downbeats if label.isdigit()), key=lambda s: int(s)
        )
        for label in numeric[::spine_step]:
            if label not in flagged:
                spine.append(
                    SuspectMeasure(
                        measure_label=label,
                        reason="spine",
                        hypothesis_downbeat_seconds=map_downbeats.get(label),
                        disagreement_seconds=None,
                    )
                )
    return tuple(sorted(defects + tuple(spine), key=lambda s: _label_key(s.measure_label)))


# -------------------------------------------------------------- persist selection


def correction_from_selection(
    beat_map: PerformanceBeatMapDocument,
    measure_label: str,
    beat_in_measure: float,
    source_midi_tick: int,
    created_at: datetime,
    note: str | None = None,
) -> HumanBeatCorrection:
    """Turn a selected onset into a correction, taking ``score_tick`` from the map.

    The canonical ``score_tick`` for (measure, beat) is read off the existing
    anchor rather than recomputed, so a Mode B correction cannot drift from the
    timeline the rest of the map already uses.
    """

    hypothesis = beat_hypothesis(beat_map, measure_label, beat_in_measure)
    if hypothesis is None:
        raise ValueError(f"no beat {beat_in_measure} in measure {measure_label!r} to correct")
    return HumanBeatCorrection(
        correction_id=str(uuid4()),
        source_midi_tick=source_midi_tick,
        score_tick=hypothesis.score_tick,
        measure_label=measure_label,
        beat_in_measure=beat_in_measure,
        created_at=created_at,
        note=note,
    )


def merge_corrections(
    existing: tuple[HumanBeatCorrection, ...],
    new: tuple[HumanBeatCorrection, ...],
) -> tuple[HumanBeatCorrection, ...]:
    """Later corrections at the same beat replace earlier ones (keyed by score_tick)."""

    by_score_tick = {item.score_tick: item for item in existing}
    for item in new:
        by_score_tick[item.score_tick] = item
    return tuple(by_score_tick[key] for key in sorted(by_score_tick))


# ------------------------------------------------------ re-derive downstream beats
#
# A human correction pins one beat. "Re-derivation" then propagates that pin to
# the *interpolated* beats around it: they are re-spaced between the nearest
# trusted knots (anchored/reviewed/corrected beats), so one anchor can repair a
# whole stretch the matcher only interpolated (e.g. an all-interpolated region).
# It is deterministic and does NOT re-run alignment, so it cannot fix a beat
# pinned to the wrong *note* -- only re-spacing, never re-identification. It is
# explicit by design: nothing here runs unless a caller asks, and `diff_beat_maps`
# lets a hypothetical be previewed before it is committed.


@dataclass(frozen=True)
class BeatChange:
    """One beat whose source time would move under a re-derivation."""

    measure_label: str
    beat_in_measure: float
    old_source_seconds: float
    new_source_seconds: float
    delta_seconds: float


def _interpolate(x: float, xs: list[int], ys: list[int]) -> float:
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    for left, right in zip(range(len(xs) - 1), range(1, len(xs))):
        if xs[left] <= x <= xs[right]:
            span = xs[right] - xs[left]
            ratio = 0.0 if span == 0 else (x - xs[left]) / span
            return ys[left] + ratio * (ys[right] - ys[left])
    return float(ys[-1])


def rederive_beat_map(
    beat_map: PerformanceBeatMapDocument,
    corrections: HumanCorrectionDocument | None,
) -> PerformanceBeatMapDocument:
    """Re-space interpolated beats between trusted knots, honouring human anchors.

    Knots = beats that carry real evidence (``_anchor_is_anchored``) or a human
    correction; the correction's exact ``source_midi_tick`` wins. Every other
    (interpolated) beat's ``source_midi_tick`` is re-interpolated across
    ``score_tick`` between its surrounding knots. ``source_seconds`` is recovered
    from the original tick→seconds relationship (exact for a constant-tempo
    reference). Human corrections are roots: this never moves them.
    """

    corrected = {}
    if corrections and corrections.timeline_id == beat_map.timeline_id:
        corrected = {c.score_tick: c.source_midi_tick for c in corrections.corrections}

    anchors = sorted(beat_map.anchors, key=lambda a: a.score_tick)
    # Tick→seconds reference from the original anchors (the tempo evidence).
    sec_ticks = sorted({a.source_midi_tick for a in anchors})
    sec_secs_map = {a.source_midi_tick: a.source_seconds for a in anchors}
    sec_secs = [sec_secs_map[t] for t in sec_ticks]

    def seconds_of(tick: int) -> float:
        if tick in sec_secs_map:
            return sec_secs_map[tick]
        return _interpolate(float(tick), sec_ticks, [round(s * 1000) for s in sec_secs]) / 1000.0

    # Knots in score order, each flagged as human-corrected or not. Only spans
    # bounded by a corrected knot are recomputed; everywhere else keeps its
    # existing source tick, so a correction never silently moves a distant beat.
    knot_ticks: list[int] = []
    knot_sources: list[int] = []
    knot_corrected: list[bool] = []
    for anchor in anchors:
        if anchor.score_tick in corrected:
            knot_ticks.append(anchor.score_tick)
            knot_sources.append(corrected[anchor.score_tick])
            knot_corrected.append(True)
        elif _anchor_is_anchored(anchor.evidence):
            knot_ticks.append(anchor.score_tick)
            knot_sources.append(anchor.source_midi_tick)
            knot_corrected.append(False)

    rebuilt: list[PerformanceBeatAnchor] = []
    previous_source = -1
    for anchor in anchors:
        if anchor.score_tick in corrected:
            source = corrected[anchor.score_tick]
        elif _anchor_is_anchored(anchor.evidence):
            source = anchor.source_midi_tick
        else:
            index = bisect.bisect_left(knot_ticks, anchor.score_tick)
            in_range = 0 < index < len(knot_ticks)
            touches_correction = in_range and (knot_corrected[index - 1] or knot_corrected[index])
            if touches_correction:
                source = round(_interpolate(float(anchor.score_tick), knot_ticks, knot_sources))
            else:
                source = anchor.source_midi_tick  # untouched region: leave as-is
        source = max(source, previous_source + 1)  # keep strictly increasing
        previous_source = source
        rebuilt.append(
            anchor.model_copy(
                update={"source_midi_tick": source, "source_seconds": seconds_of(source)}
            )
        )

    mapping_id = beat_map.mapping_id
    if not mapping_id.endswith("+rederived"):
        mapping_id = f"{mapping_id}+rederived"
    return beat_map.model_copy(update={"mapping_id": mapping_id, "anchors": tuple(rebuilt)})


def diff_beat_maps(
    old: PerformanceBeatMapDocument,
    new: PerformanceBeatMapDocument,
    min_delta_seconds: float = 1e-4,
) -> tuple[BeatChange, ...]:
    """Beats whose source time differs between two maps -- the preview of a change."""

    old_by_tick = {a.score_tick: a for a in old.anchors}
    changes: list[BeatChange] = []
    for anchor in new.anchors:
        previous = old_by_tick.get(anchor.score_tick)
        if previous is None:
            continue
        delta = anchor.source_seconds - previous.source_seconds
        if abs(delta) > min_delta_seconds:
            changes.append(
                BeatChange(
                    measure_label=anchor.measure_label,
                    beat_in_measure=anchor.beat_in_measure,
                    old_source_seconds=previous.source_seconds,
                    new_source_seconds=anchor.source_seconds,
                    delta_seconds=delta,
                )
            )
    return tuple(changes)
