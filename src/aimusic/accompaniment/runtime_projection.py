"""Bundle-v2 MIDI projection for the legacy live engine.

This adapter is intentionally narrow and temporary.  It lets the Movement 2
machine draft exercise follower/scheduler wiring while retaining explicit
coordinate ownership. Movement II events use its fused machine
performance-to-canonical map; unsupported bundles remain in their declared
source-MIDI coordinate and are never relabelled canonical ticks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import mido

from aimusic.accompaniment.bundle_v2 import (
    BundleLoaderV2,
    BundleRegistry,
    DerivedArtifact,
    DerivedArtifactRole,
    LoadedBundleV2,
)
from aimusic.accompaniment.following import (
    EntryPositionableScoreFollower,
    FollowerUpdate,
    PerformedNote,
    ScoreFollower,
)
from aimusic.accompaniment.rehearsal_position import ScoreProjection, score_projection
from aimusic.accompaniment.score_bundle import (
    InstrumentMapEntry,
    ScoreBundle,
    ScoreBundleMetadata,
    ScoreEvent,
    ScorePart,
)
from aimusic.accompaniment.section_policy import AccompanimentMode, Section, SectionMap
from aimusic.core.constants import PROJECT_ROOT

MOVEMENT_2_BUNDLE_ID = "chopin_op11_movement_2"
# Machine-draft fallback only. Production Movement II policy is bundle-owned.
MOVEMENT_2_MINIMUM_LEAD_GAP_BEATS = 3.0


@dataclass(frozen=True)
class ProvisionalRuntimeProjection:
    """Legacy runtime view plus an explicit statement of its limitations."""

    source: LoadedBundleV2
    bundle: ScoreBundle
    follower_reference_path: Path
    coordinate_system: str = "midi_performance_provisional"
    canonical: bool = False
    performance_ready: bool = False

    @property
    def orchestra_start_beat(self) -> float:
        """First sounding accompaniment event in the source-MIDI coordinate."""

        return min(event.beat for event in self.bundle.accompaniment_events)

    @property
    def first_solo_entry_beat(self) -> float:
        """First sounding solo event used as the opening LEAD/FOLLOW handoff."""

        return min(event.beat for event in self.bundle.solo_events)


class CanonicalFollower:
    """Map a source-performance follower position to canonical score beat.

    This is the single coordinate seam shared by live FOLLOW and deterministic
    closed-loop evaluation. The delegate reports its source/reference beat;
    the projection supplies canonical identity exactly once.
    """

    def __init__(self, delegate: ScoreFollower, projection: ScoreProjection) -> None:
        self._delegate = delegate
        self._projection = projection

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        update = self._delegate.observe(note)
        return None if update is None else self._to_canonical(update)

    def _to_canonical(self, update: FollowerUpdate) -> FollowerUpdate:
        """Convert one delegate position from reference-performance to canonical.

        Shared by ``observe`` and ``poll_update`` so a position drained without a
        note cannot skip the coordinate conversion.
        """

        source_tick = round(update.score_beat * 960)
        position = self._projection.position_at_source_tick(source_tick)
        return FollowerUpdate(
            perf_time=update.perf_time,
            score_beat=position.score_beat,
            confidence=min(update.confidence, position.confidence),
            raw_state={
                **update.raw_state,
                "source_performance_beat": update.score_beat,
                "canonical_score_tick": position.score_tick,
                "mapping_confidence": position.confidence,
            },
            reference_beat=update.score_beat,
        )

    def poll_update(self) -> FollowerUpdate | None:
        """Drain a delegate position that arrived without a new note."""

        poll = getattr(self._delegate, "poll_update", None)
        if poll is None:
            return None
        update = poll()
        return None if update is None else self._to_canonical(update)

    def reposition_for_entry(
        self,
        *,
        score_beat: float,
        reference_beat: float | None,
    ) -> None:
        """Keep the canonical/reference conversion explicit at cue-in."""

        if isinstance(self._delegate, EntryPositionableScoreFollower):
            self._delegate.reposition_for_entry(
                score_beat=score_beat,
                reference_beat=reference_beat,
            )


def runtime_follower(
    follower: ScoreFollower,
    projection: ProvisionalRuntimeProjection,
) -> ScoreFollower:
    """Apply the live source→canonical mapping when the bundle owns one."""

    if projection.coordinate_system != "canonical_score":
        return follower
    manifest = projection.source.manifest
    return CanonicalFollower(
        follower,
        score_projection(manifest.work.work_id, int(manifest.work.movement_id)),
    )


def default_bundle_registry(project_root: Path | None = None) -> BundleRegistry:
    """Register known repo-owned bundles that have a manifest on disk."""

    registry = BundleRegistry()
    root = (project_root or PROJECT_ROOT) / "data" / "scores" / MOVEMENT_2_BUNDLE_ID
    if (root / "bundle.yaml").is_file():
        registry.register(root)
    return registry


def project_bundle_v2_to_provisional_runtime(
    *,
    bundle_id: str,
    revision: str | None,
    registry: BundleRegistry,
    explicit_root: Path | None = None,
) -> ProvisionalRuntimeProjection:
    """Build a noncanonical runtime event view from two declared MIDI artifacts.

    ``explicit_root`` is an internal test seam.  Public callers resolve only by
    immutable bundle identity through ``BundleRegistry``.
    """

    loaded = (
        BundleLoaderV2.load(explicit_root)
        if explicit_root is not None
        else registry.load(bundle_id, revision)
    )
    if loaded.manifest.bundle_id != bundle_id:
        raise ValueError(
            f"resolved bundle id {loaded.manifest.bundle_id!r} does not match {bundle_id!r}"
        )
    if revision is not None and loaded.manifest.revision != revision:
        raise ValueError(
            f"resolved revision {loaded.manifest.revision!r} does not match {revision!r}"
        )

    follower_artifact = _one_artifact(loaded, DerivedArtifactRole.FOLLOWER_REFERENCE)
    accompaniment_artifact = _one_artifact(loaded, DerivedArtifactRole.ACCOMPANIMENT)
    follower_path = _required_valid_midi(loaded, follower_artifact)
    accompaniment_path = _required_valid_midi(loaded, accompaniment_artifact)

    solo_parts, solo_events, _ = _midi_events(follower_path, role="solo")
    accompaniment_parts, accompaniment_events, instruments = _midi_events(
        accompaniment_path, role="accompaniment"
    )
    coordinate_system = "midi_performance_provisional"
    canonical = False
    runtime_source = (
        "Bundle v2 MIDI-only draft projection; score_beat is source MIDI tick/PPQ, "
        "not the canonical timeline"
    )
    if (loaded.manifest.work.work_id, loaded.manifest.work.movement_id) == (
        "chopin_op11",
        "2",
    ):
        display_projection = score_projection("chopin_op11", 2)
        solo_events = tuple(_canonical_event(event, display_projection) for event in solo_events)
        accompaniment_events = tuple(
            _canonical_event(event, display_projection) for event in accompaniment_events
        )
        accompaniment_events = _movement_2_terminal_sustain(
            accompaniment_events,
            display_projection,
        )
        coordinate_system = "canonical_score"
        canonical = True
        runtime_source = (
            "Bundle v2 machine projection; event identity is canonical score beat, "
            "with expressive source MIDI ticks retained in source_refs"
        )
    events = tuple(
        sorted(
            (*solo_events, *accompaniment_events),
            key=lambda event: (event.beat, event.event_id),
        )
    )
    end_beat = max(event.beat + event.duration_beats for event in events)
    first_solo_beat = min(event.beat for event in solo_events)
    section_end = max(end_beat + 1.0, 1.0)
    is_movement_2 = (loaded.manifest.work.work_id, loaded.manifest.work.movement_id) == (
        "chopin_op11",
        "2",
    )
    declared_sections = _declared_sections(loaded)
    if declared_sections:
        sections = declared_sections
    elif is_movement_2 and first_solo_beat > 0:
        sections = _movement_2_sections(solo_events, section_end)
    elif first_solo_beat > 0:
        sections = (
            Section(
                id="opening-orchestra-lead",
                start_beat=0.0,
                end_beat=first_solo_beat,
                mode=AccompanimentMode.LEAD,
            ),
            Section(
                id="solo-follow",
                start_beat=first_solo_beat,
                end_beat=section_end,
                mode=AccompanimentMode.FOLLOW,
            ),
        )
    else:
        sections = (
            Section(
                id="follow-from-opening",
                start_beat=0.0,
                end_beat=section_end,
                mode=AccompanimentMode.FOLLOW,
            ),
        )

    runtime_bundle = ScoreBundle(
        root=loaded.root,
        metadata=ScoreBundleMetadata(
            piece_id=loaded.manifest.bundle_id,
            title=loaded.manifest.work.title,
            composer=loaded.manifest.work.composer,
            version=f"{loaded.manifest.revision}-midi-performance-provisional",
            source=runtime_source,
        ),
        parts=tuple((*solo_parts, *accompaniment_parts)),
        events=events,
        section_map=SectionMap(
            piece_id=loaded.manifest.bundle_id,
            sections=sections,
        ),
        instrument_map=instruments,
    )
    runtime_bundle.validate()
    return ProvisionalRuntimeProjection(
        source=loaded,
        bundle=runtime_bundle,
        follower_reference_path=follower_path,
        coordinate_system=coordinate_system,
        canonical=canonical,
    )


def _movement_2_sections(
    solo_events: tuple[ScoreEvent, ...], section_end: float
) -> tuple[Section, ...]:
    """Fallback: derive orchestra-led passages from exact solo inactivity.

    Production bundles should declare a ``sections`` artifact. This exact-gap
    inference exists for machine drafts only; it deliberately does not round
    sounding durations into apparently authoritative measure boundaries.
    """

    ordered = sorted(solo_events, key=lambda event: (event.beat, event.event_id))
    if not ordered:
        return (
            Section(
                id="orchestra-lead-entire-movement",
                start_beat=0.0,
                end_beat=section_end,
                mode=AccompanimentMode.LEAD,
            ),
        )
    lead_ranges: list[tuple[float, float]] = [(0.0, ordered[0].beat)]
    sounding_until = ordered[0].beat + ordered[0].duration_beats
    for event in ordered[1:]:
        gap_start = sounding_until
        gap_end = event.beat
        if gap_end - gap_start >= MOVEMENT_2_MINIMUM_LEAD_GAP_BEATS:
            lead_ranges.append((gap_start, gap_end))
        sounding_until = max(sounding_until, event.beat + event.duration_beats)

    sections: list[Section] = []
    cursor = 0.0
    for index, (start, end) in enumerate(lead_ranges):
        start = max(start, cursor)
        if start > cursor:
            sections.append(
                Section(
                    id=f"solo-follow-{len(sections) + 1}",
                    start_beat=cursor,
                    end_beat=start,
                    mode=AccompanimentMode.FOLLOW,
                )
            )
        if end > start:
            section_id = (
                "opening-orchestra-lead" if index == 0 else f"machine-orchestra-gap-{index}"
            )
            sections.append(
                Section(
                    id=section_id,
                    start_beat=start,
                    end_beat=end,
                    mode=AccompanimentMode.LEAD,
                )
            )
            cursor = end
    if cursor < section_end:
        sections.append(
            Section(
                id=f"solo-follow-{len(sections) + 1}",
                start_beat=cursor,
                end_beat=section_end,
                mode=AccompanimentMode.FOLLOW,
            )
        )
    return tuple(sections)


def _declared_sections(bundle: LoadedBundleV2) -> tuple[Section, ...]:
    """Load the bundle-owned section policy when one has been authored."""

    matches = [
        artifact
        for artifact in bundle.manifest.derived
        if artifact.role is DerivedArtifactRole.SECTIONS
    ]
    if not matches:
        return ()
    if len(matches) != 1:
        raise ValueError(f"bundle must declare at most one sections artifact; found {len(matches)}")
    artifact = matches[0]
    artifact_errors = [
        issue
        for issue in bundle.readiness.issues
        if issue.severity == "error" and issue.artifact_id == artifact.artifact_id
    ]
    if artifact_errors:
        raise ValueError("; ".join(issue.message for issue in artifact_errors))
    section_path = bundle.root / artifact.path
    payload = json.loads(section_path.read_text(encoding="utf-8"))
    if not payload.get("sections"):
        return ()
    section_map = SectionMap.from_dict(payload)
    if section_map.piece_id != bundle.manifest.bundle_id:
        raise ValueError(
            f"sections artifact piece_id {section_map.piece_id!r} does not match "
            f"bundle id {bundle.manifest.bundle_id!r}"
        )
    return section_map.sections


def _canonical_event(event: ScoreEvent, projection: ScoreProjection) -> ScoreEvent:
    """Project one source-MIDI event onto the canonical score coordinate."""

    source_tick = round(event.beat * 960)
    source_end_tick = round((event.beat + event.duration_beats) * 960)
    start = projection.position_at_source_tick(source_tick)
    end = projection.position_at_source_tick(source_end_tick)
    source_refs = dict(event.source_refs)
    source_refs.update(
        {
            "runtime_coordinate_system": "canonical_score",
            "source_performance_beat": event.beat,
            "source_performance_duration_beats": event.duration_beats,
        }
    )
    return ScoreEvent(
        event_id=event.event_id,
        measure=start.measure_index + 1,
        beat=start.score_beat,
        part_id=event.part_id,
        role=event.role,
        duration_beats=max(end.score_beat - start.score_beat, 1 / 960),
        pitch=event.pitch,
        velocity=event.velocity,
        source_refs=source_refs,
    )


def _movement_2_terminal_sustain(
    events: tuple[ScoreEvent, ...],
    projection: ScoreProjection,
) -> tuple[ScoreEvent, ...]:
    """Honor the engraved final bass sustain through the end of m.126.

    The Oguri MIDI releases the two final low E notes during m.126, about 2.5
    seconds before the pianist's ending. The score and the soloist's rehearsal verdict
    make the intended ownership unambiguous: those terminal strings hold until
    the movement ends. Remove the source-duration override for this final chord
    so active-release retiming follows the live canonical clock to that point.
    """

    if not events:
        return events
    end_beat = projection.timeline.end_tick / projection.timeline.document.canonical_ppq
    final_onset = max(event.beat for event in events)
    terminal_ids = {
        event.event_id for event in events if final_onset - event.beat <= 0.25
    }
    result: list[ScoreEvent] = []
    for event in events:
        if event.event_id not in terminal_ids:
            result.append(event)
            continue
        source_refs = dict(event.source_refs)
        source_refs.pop("source_performance_duration_beats", None)
        source_refs["terminal_sustain_to_score_end"] = True
        result.append(
            replace(
                event,
                duration_beats=max(event.duration_beats, end_beat - event.beat),
                source_refs=source_refs,
            )
        )
    return tuple(result)


def _one_artifact(bundle: LoadedBundleV2, role: DerivedArtifactRole) -> DerivedArtifact:
    matches = [artifact for artifact in bundle.manifest.derived if artifact.role is role]
    if len(matches) != 1:
        raise ValueError(
            f"bundle must declare exactly one {role.value} artifact; found {len(matches)}"
        )
    return matches[0]


def _required_valid_midi(bundle: LoadedBundleV2, artifact: DerivedArtifact) -> Path:
    path = bundle.root / artifact.path
    artifact_errors = [
        issue
        for issue in bundle.readiness.issues
        if issue.severity == "error" and issue.artifact_id == artifact.artifact_id
    ]
    if artifact_errors:
        raise ValueError("; ".join(issue.message for issue in artifact_errors))
    if path.suffix.lower() not in {".mid", ".midi"}:
        raise ValueError(f"{artifact.artifact_id} must be a MIDI file: {artifact.path}")
    try:
        mido.MidiFile(path, clip=False)
    except (EOFError, OSError, ValueError) as exc:
        raise ValueError(f"invalid MIDI artifact {artifact.path}: {exc}") from exc
    return path


def _midi_events(
    path: Path,
    *,
    role: str,
) -> tuple[tuple[ScorePart, ...], tuple[ScoreEvent, ...], tuple[InstrumentMapEntry, ...]]:
    midi = mido.MidiFile(path, clip=False)
    parts: list[ScorePart] = []
    events: list[ScoreEvent] = []
    instruments: list[InstrumentMapEntry] = []
    for track_index, track in enumerate(midi.tracks):
        absolute_tick = 0
        active: dict[tuple[int, int], list[tuple[int, int, str]]] = {}
        programs: dict[int, int] = {}
        volumes: dict[int, int] = {}
        track_name = next(
            (message.name for message in track if message.type == "track_name"),
            f"Track {track_index + 1}",
        )
        used_channels: set[int] = set()
        for message in track:
            absolute_tick += message.time
            if message.type == "program_change":
                programs.setdefault(message.channel, message.program)
            elif message.type == "control_change" and message.control == 7:
                volumes.setdefault(message.channel, message.value)
            elif message.type == "note_on" and message.velocity > 0:
                part_id = f"{role}_t{track_index}_c{message.channel}"
                used_channels.add(message.channel)
                active.setdefault((message.channel, message.note), []).append(
                    (absolute_tick, message.velocity, part_id)
                )
            elif message.type in {"note_off", "note_on"}:
                stack = active.get((message.channel, message.note))
                if not stack:
                    continue
                start_tick, velocity, part_id = stack.pop(0)
                duration_ticks = max(absolute_tick - start_tick, 1)
                events.append(
                    _score_event(
                        role=role,
                        event_index=len(events),
                        start_tick=start_tick,
                        duration_ticks=duration_ticks,
                        ppq=midi.ticks_per_beat,
                        pitch=message.note,
                        velocity=velocity,
                        part_id=part_id,
                        track_index=track_index,
                    )
                )
        for (channel, pitch), stack in active.items():
            for start_tick, velocity, part_id in stack:
                events.append(
                    _score_event(
                        role=role,
                        event_index=len(events),
                        start_tick=start_tick,
                        duration_ticks=max(absolute_tick - start_tick, 1),
                        ppq=midi.ticks_per_beat,
                        pitch=pitch,
                        velocity=velocity,
                        part_id=part_id,
                        track_index=track_index,
                    )
                )
        for channel in sorted(used_channels):
            part_id = f"{role}_t{track_index}_c{channel}"
            parts.append(
                ScorePart(
                    id=part_id,
                    name=f"{track_name} ch {channel + 1}",
                    role=role,  # type: ignore[arg-type]
                )
            )
            if role == "accompaniment":
                instruments.append(
                    InstrumentMapEntry(
                        part_id=part_id,
                        channel=channel,
                        program=programs.get(channel, 0),
                        volume=volumes.get(channel),
                        name=f"{track_name} (provisional)",
                    )
                )
    if not events:
        raise ValueError(f"MIDI artifact contains no note events: {path}")
    return tuple(parts), tuple(events), tuple(instruments)


def _score_event(
    *,
    role: str,
    event_index: int,
    start_tick: int,
    duration_ticks: int,
    ppq: int,
    pitch: int,
    velocity: int,
    part_id: str,
    track_index: int,
) -> ScoreEvent:
    beat = start_tick / ppq
    return ScoreEvent(
        event_id=f"{role}_{event_index:07d}",
        measure=int(beat // 4) + 1,
        beat=beat,
        part_id=part_id,
        role=role,  # type: ignore[arg-type]
        duration_beats=duration_ticks / ppq,
        pitch=pitch,
        velocity=velocity,
        source_refs={
            "coordinate_system": "midi_performance_provisional",
            "canonical": False,
            "source_tick": start_tick,
            "source_ppq": ppq,
            "track_index": track_index,
        },
    )


__all__ = [
    "MOVEMENT_2_BUNDLE_ID",
    "ProvisionalRuntimeProjection",
    "default_bundle_registry",
    "project_bundle_v2_to_provisional_runtime",
]
