"""Utility helpers for managing recorded MIDI files and manifest metadata."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict

from aimusic.core import paths


@dataclass
class RecordingEntry:
    recording_id: str
    original_name: str
    stored_filename: str
    created_at: str


def _load_manifest() -> Dict[str, RecordingEntry]:
    manifest_path = paths.recording_manifest_path()
    if not manifest_path.exists():
        return {}
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries: Dict[str, RecordingEntry] = {}
    for key, payload in data.items():
        entries[key] = RecordingEntry(**payload)
    return entries


def _save_manifest(entries: Dict[str, RecordingEntry]) -> None:
    manifest_path = paths.recording_manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {key: asdict(value) for key, value in entries.items()}
    manifest_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")


def register_recording(recording_id: str, original_name: str) -> RecordingEntry:
    entries = _load_manifest()
    created = datetime.now(tz=timezone.utc).isoformat()
    entry = RecordingEntry(
        recording_id=recording_id,
        original_name=original_name,
        stored_filename=f"{recording_id}.mid",
        created_at=created,
    )
    entries[recording_id] = entry
    _save_manifest(entries)
    return entry


def get_recording(recording_id: str) -> RecordingEntry | None:
    entries = _load_manifest()
    return entries.get(recording_id)


def list_recordings() -> list[RecordingEntry]:
    entries = _load_manifest()
    return sorted(entries.values(), key=lambda item: item.created_at, reverse=True)


__all__ = [
    "RecordingEntry",
    "register_recording",
    "get_recording",
    "list_recordings",
]
