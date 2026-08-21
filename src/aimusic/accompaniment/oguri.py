"""Oguri/Kunst der Fuge MIDI source metadata for private rehearsal use."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aimusic.core import paths

DEFAULT_ORCHESTRA_VOLUME = 0.75
PIANO_TRACK_MARKER = "PIANO SOLO"


@dataclass(frozen=True)
class OguriMovement:
    movement: int
    title: str
    source_url: str
    local_path: Path
    derived_dir: Path
    expected_ticks_per_beat: int
    expected_track_count: int
    first_solo_entry_seconds: float
    default_cue_seconds: float
    first_orchestra_entry_seconds: float = 0.0

    @property
    def solo_reference_path(self) -> Path:
        return self.derived_dir / "solo_reference.mid"

    @property
    def orchestra_accompaniment_path(self) -> Path:
        return self.derived_dir / "orchestra_accompaniment.mid"

def oguri_movement_2() -> OguriMovement:
    """Return the local Oguri second-movement source descriptor."""

    return OguriMovement(
        movement=2,
        title="Chopin Piano Concerto No. 1, Op. 11: II. Romanza: Larghetto",
        source_url="https://kunstderfuge.com/-/mid.files/chopin/concerto_11_2_(c)oguri.mid",
        local_path=(
            paths.project_root()
            / "data"
            / "scores"
            / "chopin_op11_movement_2"
            / "source"
            / "oguri_concerto_11_2.mid"
        ),
        derived_dir=(
            paths.project_root() / "data" / "scores" / "chopin_op11_movement_2" / "derived"
        ),
        expected_ticks_per_beat=240,
        expected_track_count=18,
        # First sounding orchestral note: Violin I E4, native tick 2017.
        # The preceding 4.202 s are technical MIDI preroll, not an engraved
        # measure; performer playback starts here so score m.1 and sound share
        # the same downbeat.
        first_orchestra_entry_seconds=2017 / 480,
        first_solo_entry_seconds=72.123,
        default_cue_seconds=8.0,
    )


def oguri_movement(movement: int) -> OguriMovement:
    """Resolve a supported Oguri movement descriptor."""

    if movement == 2:
        return oguri_movement_2()
    raise ValueError(f"Unsupported Oguri movement: {movement}")
