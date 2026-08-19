"""Section policy for follow/lead behavior in live accompaniment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class AccompanimentMode(StrEnum):
    """Runtime behavior for a score region."""

    FOLLOW = "FOLLOW"
    LEAD = "LEAD"
    HOLD = "HOLD"
    STOP = "STOP"
    #: A passage score following cannot track -- a cadenza, fermata or
    #: improvised lead-in. The orchestra needs only two instants from it, entry
    #: and handoff, supplied by a trained detector rather than by the follower.
    #: Transport behaves as FOLLOW until the detector fires, then snaps to the
    #: region's hand-back beat. See .agents/skills/free-region-handoff/SKILL.md.
    FREE = "FREE"


@dataclass(frozen=True)
class Section:
    """A score beat range with an accompaniment behavior."""

    id: str
    start_beat: float
    end_beat: float
    mode: AccompanimentMode
    tempo_bpm: float | None = None
    #: Present only on FREE sections. Carries the detector model name and the
    #: hand-back point for a passage score following cannot track. Authored in
    #: measure and beat; see .agents/skills/free-region-handoff/SKILL.md.
    free_region: dict[str, Any] | None = None

    def contains(self, beat: float) -> bool:
        return self.start_beat <= beat < self.end_beat


@dataclass(frozen=True)
class SectionMap:
    """Ordered section map for a piece or excerpt."""

    piece_id: str
    sections: tuple[Section, ...]

    @classmethod
    def from_json_file(cls, path: Path | str) -> "SectionMap":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SectionMap":
        piece_id = str(payload["piece_id"])
        sections = tuple(
            sorted(
                (_section_from_dict(item) for item in payload.get("sections", [])),
                key=lambda section: section.start_beat,
            )
        )
        _validate_sections(sections)
        return cls(piece_id=piece_id, sections=sections)

    def section_at(self, beat: float) -> Section:
        for section in self.sections:
            if section.contains(beat):
                return section
        raise ValueError(f"No section covers beat {beat}")

    def mode_at(self, beat: float) -> AccompanimentMode:
        return self.section_at(beat).mode


def _section_from_dict(payload: dict[str, Any]) -> Section:
    return Section(
        id=str(payload["id"]),
        start_beat=float(payload["start_beat"]),
        end_beat=float(payload["end_beat"]),
        mode=AccompanimentMode(str(payload["mode"]).upper()),
        tempo_bpm=float(payload["tempo_bpm"]) if payload.get("tempo_bpm") is not None else None,
        free_region=payload.get("free_region"),
    )


def _validate_sections(sections: tuple[Section, ...]) -> None:
    if not sections:
        raise ValueError("Section map must contain at least one section")

    previous_end: float | None = None
    seen_ids: set[str] = set()
    for section in sections:
        if section.id in seen_ids:
            raise ValueError(f"Duplicate section id: {section.id}")
        seen_ids.add(section.id)

        if section.end_beat <= section.start_beat:
            raise ValueError(f"Section '{section.id}' must have end_beat > start_beat")

        if previous_end is not None and section.start_beat < previous_end:
            raise ValueError(f"Section '{section.id}' overlaps the previous section")
        previous_end = section.end_beat
