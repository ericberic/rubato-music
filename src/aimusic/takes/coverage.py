"""Coverage computation and profile fusion (roadmap item 4/5).

Computes `profile.json` and `coverage.json` from aligned takes, rolling up
per-cell beat grid observations to measures for the Svelte UI.
"""

from __future__ import annotations

import logging
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import TypedDict

import mido

from aimusic.accompaniment.bundle_v2 import BundleLoaderV2, BundleRegistry
from aimusic.accompaniment.expectation import LEAD_MAX_SOLO_ONSETS_PER_BEAT
from aimusic.accompaniment.rehearsal_position import ScoreProjection, score_projection
from aimusic.accompaniment.runtime_projection import (
    project_bundle_v2_to_provisional_runtime,
)
from aimusic.accompaniment.score_fusion import load_performance_beat_map
from aimusic.core import paths
from aimusic.takes import profile, store
from aimusic.takes.models import CoverageDoc, CoverageMeasure, CoverageSummary


class CellStats(TypedDict):
    n: int
    qualities: list[float]


class MeasureSpan(TypedDict):
    """A measure's beat span and solo status, before coverage rollup.

    Shared internal shape produced by semantic notation or a Bundle v2
    timeline. The legacy `parse_midi_measures` utility remains only for
    source inspection; expressive MIDI is not a canonical measure map. Readers produce
    correctly-typed `int`/`float`/`bool` values, so `_rollup_measure` no
    longer needs to re-coerce them (design doc §3: that re-coercion was a
    guard against a boundary that was never actually untyped).
    """

    measure: int
    start_beat: float
    end_beat: float
    solo: bool | None


COVERAGE_ALGORITHM_REVISION = "coverage-v10-observed-population-fix"
QUALITY_TARGET = 0.5


def parse_bundle_timeline_measures(
    bundle_root: Path,
    *,
    projection: ScoreProjection | None = None,
) -> list[MeasureSpan]:
    """Project exact Bundle v2 ticks to coverage beats without inventing MIDI bars.

    A draft bundle without reviewed semantic part activity marks every measure
    neutral/tutti. That is intentionally less colorful than claiming false
    solo coverage from expressive performance ticks.
    """

    bundle = BundleLoaderV2.load(bundle_root)
    ppq = bundle.timeline.document.canonical_ppq
    measures: list[MeasureSpan] = [
        {
            "measure": measure.measure_index + 1,
            "start_beat": measure.start_tick / ppq,
            "end_beat": measure.end_tick / ppq,
            # The machine-draft Movement 2 timeline has measure geometry but
            # no reviewed semantic part activity.  ``None`` preserves that
            # uncertainty; it is not the same statement as orchestral tutti.
            "solo": None,
        }
        for measure in bundle.timeline.document.measures
    ]
    if projection is None:
        return measures

    # Movement 2 has exact PDF measure boxes but no reviewed MusicXML part
    # activity.  Projecting the declared solo-reference MIDI through the same
    # explicit Oguri->display map used by the cursor gives a second, symbolic
    # evidence path for which printed measures contain a piano entrance.  It
    # remains labelled machine-reviewed on CoverageDoc; unlike `unknown`, it
    # can honestly drive uncovered rehearsal targets. Unmapped beat-map spans
    # stay unknown and are never promoted by endpoint extrapolation.
    solo_path = bundle_root / "derived" / "solo_reference.mid"
    if not solo_path.exists() and bundle_root.name == "chopin_op11_movement_2":
        # The legacy extracted source is a small committed compatibility
        # fixture used by base/CI tests; production `dev-server.sh` pulls the
        # bundle-owned DVC artifact above. Both are byte-equivalent Oguri solo
        # references, never an alternate score coordinate.
        solo_path = (
            paths.project_root()
            / "assets/scores/chopin_op11_ii_larghetto/derived/solo_reference.mid"
        )
    if not solo_path.exists():
        return measures
    mapped_measures: set[int] | None = None
    try:
        beat_map_path = paths.score_performance_beat_map_path("chopin_op11", 2)
        if beat_map_path.exists():
            beat_map = load_performance_beat_map(beat_map_path)
            mapped_measures = {anchor.measure_index + 1 for anchor in beat_map.anchors}
    except ValueError:
        pass
    midi = mido.MidiFile(solo_path, clip=True)
    onsets_by_measure: Counter[int] = Counter()
    scale = bundle.timeline.document.canonical_ppq / midi.ticks_per_beat
    for track in midi.tracks:
        elapsed_ticks = 0
        for message in track:
            elapsed_ticks += message.time
            if message.type == "note_on" and message.velocity > 0:
                source_tick = round(elapsed_ticks * scale)
                onsets_by_measure[
                    projection.position_at_source_tick(source_tick).measure_index + 1
                ] += 1

    def _is_solo_led(measure: MeasureSpan) -> bool:
        """Solo-led means a solo *line* here, not merely a solo note.

        This used to be ``measure in active_measures`` -- presence. A single note
        made the whole bar demand full coverage, so an orchestral interlude such
        as m.22 (two solo notes in four beats) could never leave "uncertain" no
        matter how well rehearsed the passage was, and asked the performer to
        rehearse what is essentially silence (Decisions 0012 and 0015).

        Density separates the two cleanly: solo passages in this movement run
        above four onsets per beat, interludes at or below one half.
        """

        beats = measure["end_beat"] - measure["start_beat"]
        if beats <= 0:
            return False
        density = onsets_by_measure[measure["measure"]] / beats
        return density > LEAD_MAX_SOLO_ONSETS_PER_BEAT

    return [
        {
            **measure,
            "solo": (
                None
                if mapped_measures is not None and measure["measure"] not in mapped_measures
                else _is_solo_led(measure)
            ),
        }
        for measure in measures
    ]


def parse_mxl_measures(mxl_path: Path) -> list[MeasureSpan]:
    """Parse a zipped MusicXML file to extract measure beat boundaries and solo markers.

    Finds the Piano solo track, tracks divisions, and time signature changes,
    mapping each measure to its start_beat, end_beat, and solo status.
    """
    if not mxl_path.exists():
        raise FileNotFoundError(f"MusicXML score file not found: {mxl_path}")

    with zipfile.ZipFile(mxl_path) as z:
        main_xml_path = None
        if "META-INF/container.xml" in z.namelist():
            container_xml = z.read("META-INF/container.xml")
            root = ET.fromstring(container_xml)
            rootfile = root.find(".//rootfile")
            if rootfile is not None and "full-path" in rootfile.attrib:
                main_xml_path = rootfile.attrib["full-path"]

        if main_xml_path is None:
            xml_names = [
                n
                for n in z.namelist()
                if (n.endswith(".xml") or n.endswith(".musicxml")) and not n.startswith("META-INF/")
            ]
            if not xml_names:
                raise ValueError("No MusicXML (.xml or .musicxml) file found in the zip archive.")
            main_xml_path = xml_names[0]

        xml_data = z.read(main_xml_path)
        root = ET.fromstring(xml_data)

    # 1. Find piano solo part ID
    piano_part_id = None
    for part_info in root.findall(".//part-list/score-part"):
        name = part_info.find("part-name")
        if name is not None and name.text and "piano" in name.text.lower():
            piano_part_id = part_info.attrib.get("id")
            break

    if piano_part_id is None:
        raise ValueError("Could not find a piano part in the MusicXML file.")

    # 2. Build measure list from the first part to get time signatures and beats
    first_part = root.find(".//part")
    if first_part is None:
        raise ValueError("No part found in score.mxl")

    measures_data = []
    current_beat = 0.0
    current_beats = 3
    current_beat_type = 4

    for measure in first_part.findall("measure"):
        measure_num = int(measure.attrib.get("number", len(measures_data) + 1))

        # Check for time signature change
        time_elem = measure.find(".//attributes/time")
        if time_elem is not None:
            beats_text = time_elem.findtext("beats")
            beat_type_text = time_elem.findtext("beat-type")
            if beats_text and beat_type_text:
                current_beats = int(beats_text)
                current_beat_type = int(beat_type_text)

        duration_beats = current_beats * 4.0 / current_beat_type
        start_beat = current_beat
        end_beat = current_beat + duration_beats
        current_beat = end_beat

        measures_data.append(
            {"measure": measure_num, "start_beat": start_beat, "end_beat": end_beat, "solo": False}
        )

    # 3. Check which measures are solo in the piano part
    piano_part = root.find(f".//part[@id='{piano_part_id}']")
    if piano_part is not None:
        for idx, measure in enumerate(piano_part.findall("measure")):
            if idx >= len(measures_data):
                break
            # Check if there's any note that is NOT a rest
            has_note = False
            for note in measure.findall("note"):
                if note.find("rest") is None and note.find("pitch") is not None:
                    has_note = True
                    break
            if has_note:
                measures_data[idx]["solo"] = True

    return measures_data


MIN_BEATS_PER_MEASURE = 1e-3  # far smaller than any real subdivision (a 1024th note)


def _beats_per_measure(numerator: int, denominator: int) -> float:
    """Beats-per-measure from a time_signature's numerator/denominator.

    Guards against a malformed numerator/denominator that would make
    beats_per_measure zero, or -- from a merely huge denominator, not just a
    non-positive one -- vanishingly small: either would never meaningfully
    advance `current_beat` in the measure-walk loop below, hanging it while
    unboundedly growing `measures_data` (a memory-exhaustion DoS, not just a
    spin). Falls back to 4/4.
    """

    if numerator > 0 and denominator > 0:
        value = numerator * 4.0 / denominator
        if value >= MIN_BEATS_PER_MEASURE:
            return value
    return 4.0


def parse_midi_measures(
    midi_path: Path, *, track_name_contains: tuple[str, ...] = ("PIANO SOLO",)
) -> list[MeasureSpan]:
    """Derive measure beat boundaries and solo markers from a MIDI file's own
    `time_signature` meta messages and note events.

    This is a source-diagnostic projection, not Bundle v2 score truth. A MIDI
    file's native beat grid can be grouped for inspection, but expressive
    Oguri ticks must never supply performer-facing measure identities.
    Walks every `time_signature` change (there is exactly one for the Oguri
    file, 4/4, but this handles mid-piece changes generally) to lay out
    measure boundaries in beats, then flags a measure `solo` if the named
    track (e.g. "PIANO SOLO") has any note-on inside it -- the piano is
    tacet for real stretches (an orchestral intro and two interludes in the
    Oguri recording), so this is not "always True".
    """

    midi = mido.MidiFile(midi_path, clip=True)
    if midi.ticks_per_beat <= 0:
        # SMPTE-timecode-divided files can carry a negative division field,
        # and a corrupt file could carry zero; every elapsed_ticks /
        # ticks_per_beat below assumes a positive quarter-note tick rate.
        raise ValueError(
            f"Unsupported or invalid ticks_per_beat ({midi.ticks_per_beat}) in "
            f"{midi_path}; only standard musical MIDI files are supported."
        )
    markers = tuple(marker.upper() for marker in track_name_contains)

    signature_changes: list[tuple[float, float]] = []  # (start_beat, beats_per_measure)
    end_beat = 0.0
    solo_note_beats: list[float] = []
    any_track_matched = not markers

    for track in midi.tracks:
        elapsed_ticks = 0
        track_notes: list[float] = []
        track_name_matched = not markers
        for message in track:
            elapsed_ticks += message.time
            if message.type == "time_signature":
                beat = elapsed_ticks / midi.ticks_per_beat
                beats_per_measure = _beats_per_measure(message.numerator, message.denominator)
                signature_changes.append((beat, beats_per_measure))
            elif message.type == "track_name" and markers:
                if any(marker in message.name.upper() for marker in markers):
                    track_name_matched = True
                    any_track_matched = True
            elif message.type == "note_on" and message.velocity > 0:
                track_notes.append(elapsed_ticks / midi.ticks_per_beat)

        end_beat = max(end_beat, elapsed_ticks / midi.ticks_per_beat)
        if track_name_matched:
            solo_note_beats.extend(track_notes)

    # A marker that matches no track would otherwise silently produce every
    # measure as solo=False (all tutti) -- a confusing "everything is grayed
    # out" UI with no error. Fail loudly instead.
    if markers and not any_track_matched:
        raise ValueError(f"No track name matched any of {track_name_contains} in {midi_path}")

    signature_changes.sort(key=lambda item: item[0])
    if not signature_changes or signature_changes[0][0] > 1e-9:
        # MIDI spec: absence of a time_signature meta message -- including
        # the region before the first explicit one, if there is one -- means
        # 4/4.
        signature_changes.insert(0, (0.0, 4.0))
    solo_note_beats.sort()

    measures_data: list[MeasureSpan] = []
    current_beat = 0.0
    sig_index = 0
    beats_per_measure = signature_changes[0][1]
    note_index = 0
    note_count = len(solo_note_beats)
    measure_num = 1
    while current_beat < end_beat - 1e-9:
        while (
            sig_index + 1 < len(signature_changes)
            and signature_changes[sig_index + 1][0] <= current_beat + 1e-9
        ):
            sig_index += 1
            beats_per_measure = signature_changes[sig_index][1]

        start_beat = current_beat
        measure_end_beat = current_beat + beats_per_measure

        has_note = False
        while note_index < note_count and solo_note_beats[note_index] < measure_end_beat:
            has_note = True
            note_index += 1

        measures_data.append(
            {
                "measure": measure_num,
                "start_beat": start_beat,
                "end_beat": measure_end_beat,
                "solo": has_note,
            }
        )
        current_beat = measure_end_beat
        measure_num += 1

    return measures_data


def _rollup_measure(
    m_data: MeasureSpan,
    cell_map: dict[float, CellStats],
    n_target: int,
    *,
    accompaniment_required: bool | None = None,
) -> CoverageMeasure:
    """Roll up one measure's 0.5-beat grid cells into a coverage state.

    Shared between the MusicXML (movement 1) and MIDI-derived (movement 2)
    measure maps -- both produce the same `MeasureSpan` shape, so the rollup
    itself doesn't care which one built it. `m_data`'s fields are already
    correctly typed by its producer (design doc §3), so no re-coercion here.
    """

    m_num = m_data["measure"]
    m_start = m_data["start_beat"]
    m_end = m_data["end_beat"]
    scope = "unknown" if m_data["solo"] is None else "solo" if m_data["solo"] else "tutti"

    # Walk every 0.5-beat grid cell overlapping [m_start, m_end): m_start/
    # m_end come from a measure map (MusicXML beat/beat-type math, or a MIDI
    # time signature) that isn't guaranteed to land exactly on cell_map's 0.5
    # grid. Start at the *floor* of m_start (not nearest-round): rounding
    # both ends to nearest can put them on the same grid point for a short
    # measure (or one that doesn't cross a 0.25/0.75 boundary), producing an
    # empty cell_beats even though the measure genuinely overlaps a cell.
    cell_beats = []
    curr = int(m_start * 2) / 2
    while curr < m_end - 1e-9:
        cell_beats.append(curr)
        curr += 0.5

    cell_stats = [cell_map.get(cb, {"n": 0, "qualities": []}) for cb in cell_beats]
    observed = any(stats["n"] >= 1 for stats in cell_stats)

    if scope == "tutti":
        return CoverageMeasure(
            measure=m_num,
            start_beat=m_start,
            end_beat=m_end,
            solo=False,
            state="tutti",
            scope=scope,
            observed=observed,
            accompaniment_required=accompaniment_required,
        )

    min_n = min(s["n"] for s in cell_stats) if cell_stats else 0

    cell_avg_qualities: list[float] = []
    flat_qualities: list[float] = []
    for s in cell_stats:
        cell_avg_qualities.append(
            sum(s["qualities"]) / len(s["qualities"]) if s["qualities"] else 0.0
        )
        flat_qualities.extend(s["qualities"])
    min_quality = min(cell_avg_qualities) if cell_avg_qualities else 0.0
    mean_quality = sum(flat_qualities) / len(flat_qualities) if flat_qualities else 0.0

    # bool(cell_stats): Python's all([]) is vacuously True, which would mark
    # a measure with no cells at all (shouldn't happen now that cell_beats
    # starts at the floor, but this is the correctness invariant, not an
    # artifact of that fix) as "covered" with zero real observations.
    all_covered = bool(cell_stats)
    for s, cell_avg_quality in zip(cell_stats, cell_avg_qualities, strict=True):
        if s["n"] < n_target or cell_avg_quality < QUALITY_TARGET:
            all_covered = False
            break

    all_touched = (
        observed
        if scope == "unknown"
        else bool(cell_stats) and all(s["n"] >= 1 for s in cell_stats)
    )

    if all_covered:
        state = "covered"
    elif all_touched:
        state = "touched"
    elif scope == "unknown":
        # Unreviewed scope stays visually neutral until an alignment actually
        # lands here.  Once observed it becomes amber/green without claiming
        # that the whole machine-draft measure is a canonical piano measure.
        state = "tutti"
    else:
        state = "uncovered"

    return CoverageMeasure(
        measure=m_num,
        start_beat=m_start,
        end_beat=m_end,
        solo=scope == "solo",
        min_n=min_n,
        min_quality=round(min_quality, 4),
        mean_quality=round(mean_quality, 4),
        state=state,
        scope=scope,
        observed=observed,
        accompaniment_required=accompaniment_required,
    )


def accompaniment_demand_by_measure(
    bundle_root: Path,
    measures: list[MeasureSpan],
) -> dict[int, bool]:
    """Return score-time orchestral activity, not rehearsal-data coverage.

    This is also the authoritative filter for listening workflows: a timing
    diagnostic can remain useful to the machine while still being impossible
    for a performer to judge from an orchestra-only audition when the printed
    accompaniment is tacet.
    """

    loaded = BundleLoaderV2.load(bundle_root)
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=loaded.manifest.bundle_id,
        revision=loaded.manifest.revision,
        registry=BundleRegistry(),
        explicit_root=bundle_root,
    )
    events = projection.bundle.accompaniment_events
    return {
        measure["measure"]: any(
            event.beat < measure["end_beat"] - 1e-9
            and event.beat + max(event.duration_beats, 1e-9)
            > measure["start_beat"] + 1e-9
            for event in events
        )
        for measure in measures
    }


def materialize_profile(piece_id: str, movement: int) -> profile.Interpretation:
    """Fold and persist a profile, idempotently for one input revision."""

    candidate = profile.fit_interpretation_for(piece_id, movement)
    profile_dir = paths.data_root() / "profiles" / piece_id / str(movement)
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / "profile.json"
    if profile_path.exists():
        existing = profile.Interpretation.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )
        if existing.input_revision == candidate.input_revision:
            return existing
        candidate = candidate.model_copy(update={"revision": existing.revision + 1})
    else:
        candidate = candidate.model_copy(update={"revision": 1})
    _atomic_artifact(profile_path, candidate.model_dump_json(indent=2) + "\n")
    return candidate


def materialize_coverage(
    piece_id: str,
    movement: int,
    *,
    n_target: int = 3,
) -> CoverageDoc:
    """Persist coverage for the cached profile, idempotently by revision."""

    profile_path = paths.data_root() / "profiles" / piece_id / str(movement) / "profile.json"
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile has not been materialized: {piece_id}/{movement}")
    profile_doc = profile.Interpretation.model_validate_json(
        profile_path.read_text(encoding="utf-8")
    )
    coverage_path = profile_path.with_name("coverage.json")
    existing: CoverageDoc | None = None
    if coverage_path.exists():
        try:
            existing = CoverageDoc.model_validate_json(coverage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "Replacing invalid cached coverage %s: %s", coverage_path, exc
            )
        if existing is not None:
            if (
                existing.profile_revision == profile_doc.revision
                and existing.n_target == n_target
                and existing.algorithm_revision == COVERAGE_ALGORITHM_REVISION
            ):
                return existing

    aligned_takes = profile_doc.included_take_ids

    # Coverage grid: n/quality per cell from every aligned take's span --
    # this is real (span + match_rate come straight off the alignment), just
    # coarser than the profile fold below, which needs actual note/pedal data.
    cell_map: dict[float, CellStats] = {}

    projection = score_projection(piece_id, movement) if movement == 2 else None
    for take_id in aligned_takes:
        if projection is None:
            result = store.get_aligned_result(piece_id, movement, take_id)
            if result is None:
                continue
            take_cells: dict[float, list[float]] = {}
            for sample in result.cell_samples:
                take_cells.setdefault(sample.beat, []).append(sample.quality)
            if not take_cells:
                current = round(result.score_start_beat * 2) / 2
                end = round(result.score_end_beat * 2) / 2
                while current < end:
                    take_cells[current] = [result.match_rate]
                    current += 0.5
            for current, qualities in take_cells.items():
                cell_data = cell_map.setdefault(current, {"n": 0, "qualities": []})
                cell_data["n"] += 1
                cell_data["qualities"].append(sum(qualities) / len(qualities))
            continue

        # The legacy branch above always continues, so every v2 source-tick
        # projection below has the Movement II score projection it requires.
        # Keep the invariant explicit for future refactors and static review.
        assert projection is not None
        result_v2 = store.get_aligned_result_v2(piece_id, movement, take_id)
        if result_v2 is None:
            continue
        if result_v2.coordinate_system != "canonical_score":
            raise ValueError(
                f"take {take_id} requires the one-time canonical performance migration"
            )

        # One take contributes at most one vote per projected cell.  Several
        # source-performance samples can collapse into the same display cell;
        # counting each would incorrectly turn a single take green (n>=3).
        take_cells: dict[float, list[float]] = {}
        for sample in result_v2.cell_samples:
            score_beat = sample.score_tick / store.CANONICAL_PPQ
            current = round(score_beat * 2) / 2
            take_cells.setdefault(current, []).append(sample.quality)

        if not take_cells:
            # Compatibility for pre-cell-sample alignments.  The broad span is
            # still a real observed region, but only a coarse vote.
            start_beat = result_v2.start_score_tick / store.CANONICAL_PPQ
            end_beat = result_v2.end_score_tick / store.CANONICAL_PPQ
            current = round(start_beat * 2) / 2
            projected_end = round(end_beat * 2) / 2
            while current < projected_end:
                take_cells[current] = [result_v2.match_rate]
                current += 0.5

        # Cell samples are deliberately edge-trimmed for stable profile
        # statistics. Coverage has different semantics: the first and last
        # matched note onsets are real played locations and must remain visible
        # on the score (especially a phrase-ending downbeat). Retain those two
        # coarse endpoint votes without changing the learned timing profile.
        for score_beat in (
            result_v2.start_score_tick / store.CANONICAL_PPQ,
            result_v2.end_score_tick / store.CANONICAL_PPQ,
        ):
            current = round(score_beat * 2) / 2
            take_cells.setdefault(current, []).append(result_v2.match_rate)

        for current, qualities in take_cells.items():
            cell_data = cell_map.setdefault(current, {"n": 0, "qualities": []})
            cell_data["n"] += 1
            cell_data["qualities"].append(sum(qualities) / len(qualities))

    # Generate coverage.json
    measures: list[CoverageMeasure] = []

    if movement == 1:
        mxl_path = paths.score_bundle_dir(piece_id, movement) / "source" / "score.mxl"
        try:
            mxl_measures = parse_mxl_measures(mxl_path)
            measures = [_rollup_measure(m, cell_map, n_target) for m in mxl_measures]
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to parse MusicXML measures from %s", mxl_path
            )
            measures = []
    else:
        # Movement 2 uses Bundle v2 score time. Oguri ticks are an expressive
        # performance coordinate and are never promoted to printed measures.
        try:
            bundle_measures = parse_bundle_timeline_measures(
                paths.score_bundle_dir(piece_id, movement),
                projection=projection,
            )
            accompaniment_demand = accompaniment_demand_by_measure(
                paths.score_bundle_dir(piece_id, movement),
                bundle_measures,
            )
            measures = [
                _rollup_measure(
                    m,
                    cell_map,
                    n_target,
                    accompaniment_required=accompaniment_demand[m["measure"]],
                )
                for m in bundle_measures
            ]
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to read bundle timeline for piece %s movement %d", piece_id, movement
            )
            measures = []

    solo_measures = [m for m in measures if m.solo]
    n_solo = len(solo_measures)
    covered = sum(1 for m in solo_measures if m.state == "covered")
    touched = sum(1 for m in solo_measures if m.state == "touched")
    uncovered = sum(1 for m in solo_measures if m.state == "uncovered")

    percent_covered = (covered / n_solo * 100.0) if n_solo > 0 else 0.0
    # Numerator and denominator MUST come from the same population. They did
    # not: observed counted every measure with scope != "tutti" (a superset)
    # while observable counted only solo measures, so the ratio could exceed
    # 100% -- the PWA showed 114.6% rehearsed. Clamping would have hidden the
    # mismatch rather than fixed it; drawing both from one list makes <=100%
    # true by construction.
    observable = (
        solo_measures
        if projection is None or projection.mapping_review_state.value == "reviewed"
        else [measure for measure in measures if measure.scope == "solo"]
    )
    observable_measures = len(observable)
    observed_measures = sum(1 for measure in observable if measure.observed)
    percent_observed = (
        observed_measures / observable_measures * 100.0 if observable_measures > 0 else 0.0
    )

    coverage = CoverageDoc(
        piece_id=piece_id,
        movement=movement,
        computed_at=profile_doc.updated,
        n_target=n_target,
        quality_target=QUALITY_TARGET,
        measures=tuple(measures),
        summary=CoverageSummary(
            solo_measures=n_solo,
            covered=covered,
            touched=touched,
            uncovered=uncovered,
            percent_covered=round(percent_covered, 2),
            observable_measures=observable_measures,
            observed_measures=observed_measures,
            percent_observed=round(percent_observed, 2),
        ),
        revision=(existing.revision + 1) if existing is not None else 1,
        profile_revision=profile_doc.revision,
        mapping_review_state=(
            projection.mapping_review_state.value if projection is not None else "reviewed"
        ),
        canonical_positions=projection.canonical_position if projection is not None else True,
        algorithm_revision=COVERAGE_ALGORITHM_REVISION,
    )
    _atomic_artifact(coverage_path, coverage.model_dump_json(indent=2) + "\n")
    return coverage


def compute_coverage(piece_id: str, movement: int, n_target: int = 3) -> CoverageDoc:
    """Compatibility command that materializes profile then coverage."""

    materialize_profile(piece_id, movement)
    return materialize_coverage(piece_id, movement, n_target=n_target)


def _atomic_artifact(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tf:
        tf.write(content)
    Path(tf.name).replace(path)


def get_cached_coverage(piece_id: str, movement: int) -> CoverageDoc:
    """Read the last write-side materialization without mutating state."""

    path = paths.data_root() / "profiles" / piece_id / str(movement) / "coverage.json"
    if not path.exists():
        raise FileNotFoundError(f"Coverage has not been materialized: {piece_id}/{movement}")
    return CoverageDoc.model_validate_json(path.read_text(encoding="utf-8"))


def empty_coverage(piece_id: str, movement: int, n_target: int = 3) -> CoverageDoc:
    """Build an unmaterialized read view from score structure only."""

    bundle_root: Path | None = None
    if movement == 1:
        source = paths.score_bundle_dir(piece_id, movement) / "source" / "score.mxl"
        spans = parse_mxl_measures(source)
    else:
        projection = score_projection(piece_id, movement)
        bundle_root = paths.score_bundle_dir(piece_id, movement)
        spans = parse_bundle_timeline_measures(
            bundle_root,
            projection=projection,
        )
    accompaniment_demand = (
        accompaniment_demand_by_measure(bundle_root, spans)
        if bundle_root is not None
        else {}
    )
    measures = tuple(
        _rollup_measure(
            span,
            {},
            n_target,
            accompaniment_required=accompaniment_demand.get(span["measure"]),
        )
        for span in spans
    )
    solo = tuple(measure for measure in measures if measure.solo)
    projection = score_projection(piece_id, movement) if movement == 2 else None
    observable = (
        len(solo)
        if projection is None or projection.mapping_review_state.value == "reviewed"
        else sum(1 for measure in measures if measure.scope == "solo")
    )
    return CoverageDoc(
        piece_id=piece_id,
        movement=movement,
        computed_at=profile.utc_now(),
        n_target=n_target,
        quality_target=QUALITY_TARGET,
        measures=measures,
        summary=CoverageSummary(
            solo_measures=len(solo),
            covered=0,
            touched=0,
            uncovered=len(solo),
            percent_covered=0.0,
            observable_measures=observable,
            observed_measures=0,
            percent_observed=0.0,
        ),
        mapping_review_state=(
            projection.mapping_review_state.value if projection is not None else "reviewed"
        ),
        canonical_positions=projection.canonical_position if projection is not None else True,
        algorithm_revision=COVERAGE_ALGORITHM_REVISION,
    )
