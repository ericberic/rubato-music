"""Canonical score-bundle contracts for Rubato accompaniment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from aimusic.accompaniment.section_policy import SectionMap

EventRole = Literal["solo", "accompaniment", "both", "reference"]


@dataclass(frozen=True)
class ScoreBundleMetadata:
    """Human-readable identity and provenance for a score bundle."""

    piece_id: str
    title: str
    composer: str
    version: str
    source: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScoreBundleMetadata":
        return cls(
            piece_id=str(payload["piece_id"]),
            title=str(payload["title"]),
            composer=str(payload["composer"]),
            version=str(payload["version"]),
            source=str(payload["source"]) if payload.get("source") is not None else None,
        )


@dataclass(frozen=True)
class ScorePart:
    """A symbolic score part used by runtime events."""

    id: str
    name: str
    role: EventRole
    abbreviation: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScorePart":
        role = str(payload.get("role", "accompaniment")).lower()
        if role not in {"solo", "accompaniment", "both", "reference"}:
            raise ValueError(f"Unsupported part role: {role}")
        return cls(
            id=str(payload["id"]),
            name=str(payload["name"]),
            role=role,  # type: ignore[arg-type]
            abbreviation=str(payload["abbreviation"])
            if payload.get("abbreviation") is not None
            else None,
        )


@dataclass(frozen=True)
class InstrumentMapEntry:
    """MIDI rendering target for a score part."""

    part_id: str
    channel: int
    program: int
    name: str | None = None
    volume: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstrumentMapEntry":
        channel = int(payload["channel"])
        program = int(payload["program"])
        if not 0 <= channel <= 15:
            raise ValueError(f"MIDI channel must be 0-15 for {payload['part_id']}")
        if not 0 <= program <= 127:
            raise ValueError(f"MIDI program must be 0-127 for {payload['part_id']}")
        volume = payload.get("volume")
        if volume is not None and not 0 <= int(volume) <= 127:
            raise ValueError(f"MIDI volume must be 0-127 for {payload['part_id']}")
        return cls(
            part_id=str(payload["part_id"]),
            channel=channel,
            program=program,
            name=str(payload["name"]) if payload.get("name") is not None else None,
            volume=int(volume) if volume is not None else None,
        )


@dataclass(frozen=True)
class ScoreEvent:
    """Beat-indexed symbolic event consumed by followers and schedulers."""

    event_id: str
    measure: int
    beat: float
    part_id: str
    role: EventRole
    duration_beats: float
    pitch: int | None = None
    velocity: int | None = None
    source_refs: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScoreEvent":
        role = str(payload.get("role", "accompaniment")).lower()
        if role not in {"solo", "accompaniment", "both", "reference"}:
            raise ValueError(f"Unsupported event role: {role}")
        duration_beats = float(payload["duration_beats"])
        if duration_beats <= 0:
            raise ValueError(f"Event {payload['event_id']} must have positive duration")
        beat = float(payload["beat"])
        if beat < 0:
            raise ValueError(f"Event {payload['event_id']} must have non-negative beat")
        return cls(
            event_id=str(payload["event_id"]),
            measure=int(payload["measure"]),
            beat=beat,
            part_id=str(payload["part_id"]),
            role=role,  # type: ignore[arg-type]
            duration_beats=duration_beats,
            pitch=int(payload["pitch"]) if payload.get("pitch") is not None else None,
            velocity=int(payload["velocity"]) if payload.get("velocity") is not None else None,
            source_refs=dict(payload.get("source_refs") or {}),
        )


@dataclass(frozen=True)
class ScoreBundle:
    """Loaded score bundle ready for offline or online accompaniment work."""

    root: Path
    metadata: ScoreBundleMetadata
    parts: tuple[ScorePart, ...]
    events: tuple[ScoreEvent, ...]
    section_map: SectionMap
    instrument_map: tuple[InstrumentMapEntry, ...]

    @classmethod
    def load(cls, root: Path | str) -> "ScoreBundle":
        """Load the v1 runtime event view through its migration adapter."""

        return LegacyScoreBundleAdapter.load(root)

    @property
    def solo_events(self) -> tuple[ScoreEvent, ...]:
        try:
            return self._solo_events  # type: ignore[attr-defined]
        except AttributeError:
            events = tuple(
                event for event in self.events if event.role in {"solo", "both", "reference"}
            )
            object.__setattr__(self, "_solo_events", events)
            return events

    @property
    def accompaniment_events(self) -> tuple[ScoreEvent, ...]:
        try:
            return self._accompaniment_events  # type: ignore[attr-defined]
        except AttributeError:
            events = tuple(
                event for event in self.events if event.role in {"accompaniment", "both"}
            )
            object.__setattr__(self, "_accompaniment_events", events)
            return events

    def validate(self) -> None:
        part_ids = {part.id for part in self.parts}
        instrument_part_ids = {entry.part_id for entry in self.instrument_map}
        event_ids: set[str] = set()
        previous_beat = -1.0

        if not self.events:
            raise ValueError("Score bundle must contain at least one event")
        unknown_instruments = instrument_part_ids - part_ids
        if unknown_instruments:
            raise ValueError(
                f"Instrument map references unknown parts: {sorted(unknown_instruments)}"
            )

        for event in self.events:
            if event.event_id in event_ids:
                raise ValueError(f"Duplicate event id: {event.event_id}")
            event_ids.add(event.event_id)
            if event.part_id not in part_ids:
                raise ValueError(f"Event {event.event_id} references unknown part {event.part_id}")
            if event.beat < previous_beat:
                raise ValueError("Score events must be sorted by non-decreasing beat")
            previous_beat = event.beat

        if not self.solo_events:
            raise ValueError("Score bundle must contain solo/reference events")
        if not self.accompaniment_events:
            raise ValueError("Score bundle must contain accompaniment events")

        missing_instruments = {
            event.part_id for event in self.accompaniment_events
        } - instrument_part_ids
        if missing_instruments:
            raise ValueError(f"Missing instrument mappings for: {sorted(missing_instruments)}")


class LegacyScoreBundleAdapter:
    """Adapter for pre-v2 runtime bundles.

    Bundle v2 models source identity and canonical correspondence.  Until the
    follower/scheduler are migrated, their existing five-file event contract
    remains available here without pretending it is a v2 manifest.
    """

    @staticmethod
    def load(root: Path | str) -> ScoreBundle:
        bundle_root = Path(root)
        metadata = ScoreBundleMetadata.from_dict(_read_yaml(bundle_root / "metadata.yaml"))
        parts = tuple(
            ScorePart.from_dict(item) for item in _read_yaml(bundle_root / "parts.yaml")["parts"]
        )
        events = tuple(
            ScoreEvent.from_dict(item) for item in _read_jsonl(bundle_root / "events.jsonl")
        )
        section_map = SectionMap.from_json_file(bundle_root / "sections.json")
        instrument_map = tuple(
            InstrumentMapEntry.from_dict(item)
            for item in _read_yaml(bundle_root / "instrument_map.yaml")["instruments"]
        )
        bundle = ScoreBundle(
            root=bundle_root,
            metadata=metadata,
            parts=parts,
            events=events,
            section_map=section_map,
            instrument_map=instrument_map,
        )
        bundle.validate()
        return bundle


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected mapping in {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected object on {path}:{line_number}")
        rows.append(payload)
    return rows
