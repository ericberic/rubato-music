"""Declare and clear free regions in a score bundle's ``sections.json``.

A free region is a passage score following cannot track -- a cadenza, fermata or
improvised lead-in. Declaring one is an edit to the section map rather than a
new file, because sections already carry the runtime behaviour for a beat range
and a free passage is a behaviour, not a separate concept. See
``.agents/skills/free-region-handoff/SKILL.md``.

Declaring splits whatever FOLLOW section encloses the span, so the map keeps
tiling the piece without gaps or overlaps. Clearing merges the neighbours back.
Both are done here rather than by hand: the boundaries are easy to get subtly
wrong, and a map that no longer tiles fails far from the edit that caused it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BEATS_PER_BAR = 4.0


@dataclass(frozen=True)
class FreeRegionSpec:
    """A span the performer marked, in the coordinates they think in."""

    from_measure: int
    to_measure: int
    label: str = "Free region"
    detector_model: str | None = None
    performer_silent_after: bool = True

    def __post_init__(self) -> None:
        if self.from_measure < 1:
            raise ValueError("measure numbers are 1-based")
        if self.to_measure < self.from_measure:
            raise ValueError("a region ends no earlier than it starts")

    @property
    def start_beat(self) -> float:
        return (self.from_measure - 1) * BEATS_PER_BAR

    @property
    def end_beat(self) -> float:
        """Exclusive: the hand-back beat, where the next section takes over."""

        return self.to_measure * BEATS_PER_BAR

    @property
    def section_id(self) -> str:
        return f"m{self.from_measure}-free"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    _refresh_manifest_checksum(path)


def _refresh_manifest_checksum(sections_path: Path) -> None:
    """Keep bundle.yaml's sha256 in step with the file we just rewrote.

    The bundle manifest checksums its artifacts, so editing sections.json
    without updating it makes the whole bundle fail to project -- and the error
    surfaces far away, as "sha256 does not match" while loading a timeline,
    long after the edit that caused it.

    The replacement is deliberately targeted at the one sha256 belonging to this
    artifact rather than a YAML round trip, which would reformat a hand-
    maintained file and bury the change in noise.
    """

    manifest = sections_path.parent.parent / "bundle.yaml"
    if not manifest.exists():
        return
    digest = hashlib.sha256(sections_path.read_bytes()).hexdigest()
    relative = f"{sections_path.parent.name}/{sections_path.name}"
    text = manifest.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(path:\s*{re.escape(relative)}\b.*?sha256:\s*)([0-9a-f]{{64}})",
        re.DOTALL,
    )
    updated, count = pattern.subn(rf"\g<1>{digest}", text, count=1)
    if count:
        manifest.write_text(updated, encoding="utf-8")


def declared_regions(path: Path) -> list[dict[str, Any]]:
    return [s for s in _load(path).get("sections", []) if s.get("mode") == "FREE"]


def declare(path: Path, spec: FreeRegionSpec) -> dict[str, Any]:
    """Insert a FREE section, splitting whatever encloses it.

    Only a FOLLOW section may be split. A span that overlaps a LEAD passage is
    rejected rather than silently reshaped: the orchestra already owns the
    timing there, so a free region would be claiming ground it does not hold.
    """

    payload = _load(path)
    sections: list[dict[str, Any]] = list(payload.get("sections", []))
    start, end = spec.start_beat, spec.end_beat

    overlapping = [
        s for s in sections if float(s["start_beat"]) < end and float(s["end_beat"]) > start
    ]
    if not overlapping:
        raise ValueError(f"m.{spec.from_measure}-{spec.to_measure} falls outside the score")
    if any(s["mode"] == "FREE" for s in overlapping):
        raise ValueError("that span already overlaps a free region")
    if any(s["mode"] != "FOLLOW" for s in overlapping):
        modes = sorted({str(s["mode"]) for s in overlapping if s["mode"] != "FOLLOW"})
        raise ValueError(f"a free region may only split FOLLOW, not {', '.join(modes)}")

    free = {
        "id": spec.section_id,
        "start_beat": start,
        "end_beat": end,
        "mode": "FREE",
        "free_region": {
            "label": spec.label,
            # Authored in measure and beat. Ticks are derived and relocate
            # whenever the beat map is rebuilt, so storing them would move the
            # performer's annotation without anyone touching it.
            "from_measure": spec.from_measure,
            "to_measure": spec.to_measure,
            "hand_back_measure": spec.to_measure + 1,
            "hand_back_beat": 1,
            "policy": "handoff_detector",
            "detector_model": spec.detector_model,
            "performer_silent_after": spec.performer_silent_after,
        },
    }

    rebuilt: list[dict[str, Any]] = []
    for section in sections:
        s_start, s_end = float(section["start_beat"]), float(section["end_beat"])
        if s_end <= start or s_start >= end:
            rebuilt.append(section)
            continue
        if s_start < start:
            rebuilt.append({**section, "end_beat": start})
        if s_end > end:
            rebuilt.append({**section, "id": f"{section['id']}-after", "start_beat": end})
    rebuilt.append(free)
    rebuilt.sort(key=lambda s: float(s["start_beat"]))
    payload["sections"] = rebuilt
    _validate_tiling(rebuilt)
    _save(path, payload)
    return free


def clear(path: Path, from_measure: int) -> bool:
    """Remove a free region and close the hole by extending its left neighbour."""

    payload = _load(path)
    sections: list[dict[str, Any]] = list(payload.get("sections", []))
    target = next(
        (
            s
            for s in sections
            if s.get("mode") == "FREE"
            and int(s.get("free_region", {}).get("from_measure", -1)) == from_measure
        ),
        None,
    )
    if target is None:
        return False
    index = sections.index(target)
    if index > 0:
        sections[index - 1] = {**sections[index - 1], "end_beat": target["end_beat"]}
    elif index + 1 < len(sections):
        sections[index + 1] = {**sections[index + 1], "start_beat": target["start_beat"]}
    sections.pop(index)
    sections = _coalesce(sections)
    payload["sections"] = sections
    _validate_tiling(sections)
    _save(path, payload)
    return True


def _coalesce(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge touching sections that behave identically.

    Without this, declaring a region and clearing it again leaves two adjacent
    FOLLOW sections where there was one. Behaviour is unchanged, so nothing
    fails -- but repeat the cycle and the map shreds into fragments that make
    every later diff unreadable. Only plain sections merge; anything carrying
    its own configuration keeps its identity.
    """

    merged: list[dict[str, Any]] = []
    for section in sections:
        previous = merged[-1] if merged else None
        mergeable = (
            previous is not None
            and previous["mode"] == section["mode"]
            and float(previous["end_beat"]) == float(section["start_beat"])
            and not previous.get("free_region")
            and not section.get("free_region")
            and previous.get("tempo_bpm") == section.get("tempo_bpm")
        )
        if mergeable:
            merged[-1] = {**previous, "end_beat": section["end_beat"]}
        else:
            merged.append(section)
    return merged


def _validate_tiling(sections: list[dict[str, Any]]) -> None:
    """A section map that no longer tiles fails far from the edit that broke it."""

    for previous, current in zip(sections, sections[1:]):
        if float(previous["end_beat"]) != float(current["start_beat"]):
            raise ValueError(
                f"sections stopped tiling between {previous['id']} and {current['id']}"
            )
