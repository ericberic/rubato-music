"""Centralized helpers for data/run directory management."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from aimusic.core.constants import PROJECT_ROOT


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_root() -> Path:
    return PROJECT_ROOT


def data_root() -> Path:
    return _ensure(_resolve_data_root())


def runs_root() -> Path:
    return _ensure(_resolve_runs_root())


def state_root() -> Path:
    return _ensure(_resolve_state_root())


def live_audio_config_path() -> Path:
    """Machine-local VST/CoreAudio bindings for named performance zones."""

    return _ensure(state_root() / "config") / "live-audio-zones.json"


def _resolve_data_root() -> Path:
    override = os.environ.get("AIMUSIC_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return _resolve_state_root() / "data"


def _resolve_runs_root() -> Path:
    override = os.environ.get("AIMUSIC_RUNS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return _resolve_state_root() / "runs"


def _resolve_state_root() -> Path:
    override = os.environ.get("AIMUSIC_STATE_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Rubato"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if not base:
            raise RuntimeError(
                "Could not determine AppData directory on Windows: "
                "LOCALAPPDATA and APPDATA are not set."
            )
        return Path(base).expanduser().resolve() / "Rubato"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser().resolve() / "rubato"
    return Path.home() / ".local" / "state" / "rubato"


def recordings_root() -> Path:
    return _ensure(data_root() / "recordings")


def processed_root() -> Path:
    return _ensure(data_root() / "processed")


def score_bundles_root() -> Path:
    return _ensure(data_root() / "scores")


# Piece/movement -> on-disk score bundle directory. Score bundles are
# version-controlled assets (assets/scores/<bundle>/), not per-user data, so
# this is deliberately separate from `score_bundles_root()` above. Only
# registered combinations are known; this mirrors the explicit per-movement
# dispatch `aligner.resolve_reference_midi_path` already uses for movement 2
# rather than inventing a generic piece_id-to-folder-name mapping prematurely.
_SCORE_BUNDLE_DIRS: dict[tuple[str, int], Path] = {
    ("chopin_op11", 1): Path("assets/scores/chopin_op11_i_allegro_maestoso"),
    ("chopin_op11", 2): Path("data/scores/chopin_op11_movement_2"),
}

# Performer-facing score PDFs are independent source artifacts in Bundle v2.
# Movement 1 still uses the legacy co-located engraving, while the Movement 2
# cockpit uses the DVC-managed Joseffy reduction source.  Keep this registry
# explicit: source filenames and movement numbers must never be guessed.
_SCORE_DISPLAY_PDFS: dict[tuple[str, int], Path] = {
    ("chopin_op11", 1): Path(
        "assets/scores/chopin_op11_i_allegro_maestoso/source/score.pdf"
    ),
    ("chopin_op11", 2): Path(
        "data/scores/chopin_op11_movement_2/source/"
        "joseffy_reduction_movement2.pdf"
    ),
}

_SCORE_DISPLAY_GEOMETRY: dict[tuple[str, int], Path] = {
    ("chopin_op11", 1): Path(
        "assets/scores/chopin_op11_i_allegro_maestoso/derived/measure_boxes.json"
    ),
    ("chopin_op11", 2): Path(
        "data/scores/chopin_op11_movement_2/derived/display_map.machine.json"
    ),
}

_SCORE_PERFORMANCE_BEAT_MAPS: dict[tuple[str, int], Path] = {
    ("chopin_op11", 2): Path(
        "data/scores/chopin_op11_movement_2/derived/performance_beat_map.machine.json"
    ),
}


def score_bundle_dir(piece_id: str, movement: int) -> Path:
    relative_path = _SCORE_BUNDLE_DIRS.get((piece_id, movement))
    if relative_path is None:
        raise ValueError(f"No score bundle registered for {piece_id} movement {movement}")
    return project_root() / relative_path


def score_bundle_missing_artifacts(piece_id: str, movement: int) -> list[str]:
    """Return DVC-tracked paths under the score bundle whose real file is absent.

    Every ``<name>.dvc`` pointer file under the bundle directory names a
    DVC-managed artifact at the same path with ``.dvc`` stripped (DVC
    convention). A `dvc pull` that hasn't run yet, or a partial one, leaves
    the pointer committed to git but the real file missing -- this is the
    one clean signal that distinguishes "never pulled" from "not a tracked
    artifact at all". Paths are returned relative to the bundle directory,
    for display (rubato#101).
    """

    bundle_dir = score_bundle_dir(piece_id, movement)
    missing: list[str] = []
    for dvc_pointer in sorted(bundle_dir.rglob("*.dvc")):
        artifact_path = dvc_pointer.with_suffix("")
        if not artifact_path.exists():
            missing.append(artifact_path.relative_to(bundle_dir).as_posix())
    return missing


def score_sections_path(piece_id: str, movement: int) -> Path:
    """Where the runtime behaviour for each beat range lives, free regions included."""

    return score_bundle_dir(piece_id, movement) / "derived" / "sections.json"


def score_display_pdf_path(piece_id: str, movement: int) -> Path:
    """Return the registered performer-facing engraving for a movement.

    The returned file may be absent when a DVC source has not been pulled;
    callers should report that separately from an unknown piece/movement.
    """

    relative_path = _SCORE_DISPLAY_PDFS.get((piece_id, movement))
    if relative_path is None:
        raise ValueError(f"No display score registered for {piece_id} movement {movement}")
    return project_root() / relative_path


def score_display_geometry_path(piece_id: str, movement: int) -> Path:
    """Return registered performer geometry (legacy bands or canonical map)."""

    relative_path = _SCORE_DISPLAY_GEOMETRY.get((piece_id, movement))
    if relative_path is None:
        raise ValueError(f"No display geometry registered for {piece_id} movement {movement}")
    return project_root() / relative_path


def score_performance_beat_map_path(piece_id: str, movement: int) -> Path:
    """Return the offline-built reference-MIDI to canonical-beat mapping."""

    relative_path = _SCORE_PERFORMANCE_BEAT_MAPS.get((piece_id, movement))
    if relative_path is None:
        raise ValueError(f"No performance beat map registered for {piece_id} movement {movement}")
    return project_root() / relative_path


def score_alignment_corrections_path(piece_id: str, movement: int) -> Path:
    """Performer-authored sparse anchors, stored outside immutable bundle sources."""

    return data_root() / "alignment-corrections" / piece_id / f"movement-{movement}.json"


def score_solo_reference_path(piece_id: str, movement: int) -> Path:
    """The reference-performance solo MIDI whose onsets the beat map maps to.

    May be absent when the bundle's DVC artifacts have not been pulled; callers
    should report that separately from an unknown piece/movement.
    """

    return score_bundle_dir(piece_id, movement) / "derived" / "solo_reference.mid"


def score_cross_check_alignment_paths(piece_id: str, movement: int) -> tuple[Path, Path]:
    """The two independently-derived alignments used to cross-check the beat map.

    (reduction-vs-Oguri, orchestra-vs-Oguri). Disjoint provenance from the
    Audiveris-built beat map, which is what makes their agreement meaningful
    (see the score-localization skill's Cardinal Rule). Either may be absent in
    a fresh checkout; the validation worklist degrades to unavailable then.
    """

    derived = score_bundle_dir(piece_id, movement) / "derived"
    return (
        derived / "musescore_oguri_alignment.machine.json",
        derived / "orchestra_oguri_alignment.machine.json",
    )


def recording_manifest_path() -> Path:
    return recordings_root() / "manifest.json"


def recording_file_path(recording_id: str) -> Path:
    return recordings_root() / f"{recording_id}.mid"


def processed_recording_dir(recording_id: str) -> Path:
    return processed_root() / recording_id


def session_dir(session_id: str) -> Path:
    return _ensure(processed_recording_dir(session_id))


def takes_root() -> Path:
    return _ensure(data_root() / "takes")


def take_movement_dir(piece_id: str, movement: int, *, create: bool = True) -> Path:
    path = takes_root() / piece_id / str(movement)
    return _ensure(path) if create else path


def take_manifest_path(piece_id: str, movement: int, *, create: bool = True) -> Path:
    return take_movement_dir(piece_id, movement, create=create) / "takes.jsonl"


def take_dir(piece_id: str, movement: int, take_id: str, *, create: bool = True) -> Path:
    path = take_movement_dir(piece_id, movement, create=create) / take_id
    return _ensure(path) if create else path


def mix_programs_dir(piece_id: str, movement: int, *, create: bool = True) -> Path:
    """Performer-owned mix programs, separate from score and rehearsal data."""

    path = data_root() / "profiles" / piece_id / str(movement) / "mix-programs"
    return _ensure(path) if create else path


def mix_program_dir(
    piece_id: str, movement: int, program_id: str, *, create: bool = True
) -> Path:
    path = mix_programs_dir(piece_id, movement, create=create) / program_id
    return _ensure(path) if create else path


def mix_program_path(
    piece_id: str, movement: int, program_id: str, *, create: bool = True
) -> Path:
    return mix_program_dir(piece_id, movement, program_id, create=create) / "mix-program.json"


def mix_program_revision_path(
    piece_id: str,
    movement: int,
    program_id: str,
    revision: int,
    *,
    create: bool = True,
) -> Path:
    directory = mix_program_dir(piece_id, movement, program_id, create=create) / "revisions"
    if create:
        directory = _ensure(directory)
    return directory / f"{revision:06d}.json"


def run_dir(run_id: str, create: bool = True) -> Path:
    path = runs_root() / run_id
    return _ensure(path) if create else path


def run_input_dir(run_id: str) -> Path:
    return _ensure(run_dir(run_id) / "input")


def run_output_dir(run_id: str) -> Path:
    return _ensure(run_dir(run_id) / "output")


def run_trace_dir(run_id: str) -> Path:
    return _ensure(run_dir(run_id) / "trace")


def run_analysis_dir(run_id: str) -> Path:
    return _ensure(run_dir(run_id) / "analysis")


def run_summary_path(run_id: str) -> Path:
    return run_dir(run_id) / "summary.txt"


def run_config_snapshot_path(run_id: str) -> Path:
    return run_dir(run_id) / "config_snapshot.yaml"


__all__ = [
    "project_root",
    "data_root",
    "runs_root",
    "state_root",
    "live_audio_config_path",
    "recordings_root",
    "processed_root",
    "score_bundles_root",
    "score_bundle_dir",
    "score_display_pdf_path",
    "score_display_geometry_path",
    "score_performance_beat_map_path",
    "score_alignment_corrections_path",
    "recording_manifest_path",
    "recording_file_path",
    "processed_recording_dir",
    "session_dir",
    "takes_root",
    "take_movement_dir",
    "take_manifest_path",
    "take_dir",
    "mix_programs_dir",
    "mix_program_dir",
    "mix_program_path",
    "mix_program_revision_path",
    "run_dir",
    "run_input_dir",
    "run_output_dir",
    "run_trace_dir",
    "run_analysis_dir",
    "run_summary_path",
    "run_config_snapshot_path",
]
