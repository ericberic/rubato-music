"""Atomic, revision-checked persistence for mix programs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from aimusic.core import paths
from aimusic.mixing.models import (
    MixProgram,
    MixProgramCreate,
    MixRegion,
    MixRoute,
    ScoreIdentityStatus,
)


class MixProgramNotFoundError(LookupError):
    pass


class MixRevisionConflictError(RuntimeError):
    pass


class MixRegionNotFoundError(LookupError):
    pass


class MixScoreIdentityError(RuntimeError):
    pass


def _program_lock(piece_id: str, movement: int, program_id: str) -> FileLock:
    """Serialize revisions across threads and server worker processes."""

    directory = paths.mix_program_dir(piece_id, movement, program_id, create=True)
    return FileLock(directory / ".write.lock", timeout=10)


def list_programs(piece_id: str, movement: int) -> tuple[MixProgram, ...]:
    root = paths.mix_programs_dir(piece_id, movement, create=False)
    if not root.exists():
        return ()
    programs = []
    for document in sorted(root.glob("*/mix-program.json")):
        program = MixProgram.model_validate_json(document.read_text(encoding="utf-8"))
        programs.append(_with_score_identity_status(program))
    return tuple(programs)


def load_program(
    piece_id: str,
    movement: int,
    program_id: str,
    *,
    require_current_score: bool = False,
) -> MixProgram:
    document = paths.mix_program_path(piece_id, movement, program_id, create=False)
    if not document.exists():
        raise MixProgramNotFoundError(f"Mix program not found: {program_id}")
    program = MixProgram.model_validate_json(document.read_text(encoding="utf-8"))
    program = _with_score_identity_status(program)
    if require_current_score:
        _require_current_score(program)
    return program


def create_program(request: MixProgramCreate) -> MixProgram:
    with _program_lock(request.piece_id, request.movement, request.program_id):
        document = paths.mix_program_path(
            request.piece_id, request.movement, request.program_id, create=False
        )
        if document.exists():
            return load_program(request.piece_id, request.movement, request.program_id)
        bundle_id, revision, digest = _score_identity(request.piece_id, request.movement)
        program = MixProgram(
            schema_version=2,
            program_id=request.program_id,
            name=request.name,
            piece_id=request.piece_id,
            movement=request.movement,
            score_bundle_id=bundle_id,
            score_bundle_revision=revision,
            timeline_digest=digest,
            updated_at=datetime.now(timezone.utc),
            default_routes=(
                MixRoute(zone_id="yamaha_anchor", stem_ids=("orchestra",), level=25),
                MixRoute(zone_id="room_center", stem_ids=("orchestra",), level=100),
            ),
            regions=_starter_regions(request.piece_id, request.movement),
        )
        _write_snapshot(program)
        return program


def update_default_routes(
    piece_id: str,
    movement: int,
    program_id: str,
    *,
    expected_revision: int,
    routes: tuple[MixRoute, ...],
) -> MixProgram:
    return _mutate(
        piece_id,
        movement,
        program_id,
        expected_revision,
        lambda current: current.model_copy(update={"default_routes": routes}),
    )


def add_region(
    piece_id: str,
    movement: int,
    program_id: str,
    *,
    expected_revision: int,
    region: MixRegion,
) -> MixProgram:
    def apply(current: MixProgram) -> MixProgram:
        if any(item.region_id == region.region_id for item in current.regions):
            raise MixRevisionConflictError(f"Mix region already exists: {region.region_id}")
        return current.model_copy(update={"regions": (*current.regions, region)})

    return _mutate(piece_id, movement, program_id, expected_revision, apply)


def update_region(
    piece_id: str,
    movement: int,
    program_id: str,
    region_id: str,
    *,
    expected_revision: int,
    region: MixRegion,
) -> MixProgram:
    if region.region_id != region_id:
        raise ValueError("region id in the document must match the URL")

    def apply(current: MixProgram) -> MixProgram:
        if not any(item.region_id == region_id for item in current.regions):
            raise MixRegionNotFoundError(f"Mix region not found: {region_id}")
        return current.model_copy(
            update={
                "regions": tuple(
                    region if item.region_id == region_id else item
                    for item in current.regions
                )
            }
        )

    return _mutate(piece_id, movement, program_id, expected_revision, apply)


def delete_region(
    piece_id: str,
    movement: int,
    program_id: str,
    region_id: str,
    *,
    expected_revision: int,
) -> MixProgram:
    def apply(current: MixProgram) -> MixProgram:
        regions = tuple(item for item in current.regions if item.region_id != region_id)
        if len(regions) == len(current.regions):
            raise MixRegionNotFoundError(f"Mix region not found: {region_id}")
        return current.model_copy(update={"regions": regions})

    return _mutate(piece_id, movement, program_id, expected_revision, apply)


def undo(
    piece_id: str,
    movement: int,
    program_id: str,
    *,
    expected_revision: int,
) -> MixProgram:
    with _program_lock(piece_id, movement, program_id):
        current = load_program(
            piece_id, movement, program_id, require_current_score=True
        )
        _require_revision(current, expected_revision)
        target_revision = current.undo_parent_revision
        # Programs written by the initial MVP did not persist an explicit
        # history parent. Preserve one-step compatibility for those documents.
        if (
            target_revision is None
            and current.schema_version < 2
            and current.revision > 1
        ):
            target_revision = current.revision - 1
        if target_revision is None:
            return current
        previous_path = paths.mix_program_revision_path(
            piece_id, movement, program_id, target_revision, create=False
        )
        if not previous_path.exists():
            raise MixProgramNotFoundError("Previous mix revision is unavailable")
        previous = MixProgram.model_validate_json(previous_path.read_text(encoding="utf-8"))
        restored = previous.model_copy(
            update={
                "revision": current.revision + 1,
                "schema_version": 2,
                "undo_parent_revision": previous.undo_parent_revision,
                # Rebinding is metadata acknowledgement, not an artistic edit.
                # Keep the current score pin while restoring earlier mix content.
                "score_bundle_id": current.score_bundle_id,
                "score_bundle_revision": current.score_bundle_revision,
                "timeline_digest": current.timeline_digest,
                "score_identity_status": ScoreIdentityStatus.CURRENT,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        _write_snapshot(restored)
        return restored


def rebind_score_identity(
    piece_id: str,
    movement: int,
    program_id: str,
    *,
    expected_revision: int,
) -> MixProgram:
    """Acknowledge human review and pin a program to the current score files."""

    with _program_lock(piece_id, movement, program_id):
        current = load_program(piece_id, movement, program_id)
        _require_revision(current, expected_revision)
        bundle_id, revision, timeline_digest = _score_identity(piece_id, movement)
        rebound = current.model_copy(
            update={
                "score_bundle_id": bundle_id,
                "score_bundle_revision": revision,
                "timeline_digest": timeline_digest,
                "score_identity_status": ScoreIdentityStatus.CURRENT,
                "revision": current.revision + 1,
                "schema_version": 2,
                # Do not insert metadata-only rebinding into artistic Undo.
                "updated_at": datetime.now(timezone.utc),
            }
        )
        _write_snapshot(rebound)
        return rebound


def _mutate(
    piece_id: str,
    movement: int,
    program_id: str,
    expected_revision: int,
    operation: Callable[[MixProgram], MixProgram],
) -> MixProgram:
    with _program_lock(piece_id, movement, program_id):
        current = load_program(
            piece_id, movement, program_id, require_current_score=True
        )
        _require_revision(current, expected_revision)
        candidate = operation(current)
        updated = MixProgram.model_validate(
            candidate.model_dump()
            | {
                "revision": current.revision + 1,
                "schema_version": 2,
                "undo_parent_revision": current.revision,
                "score_identity_status": ScoreIdentityStatus.CURRENT,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        _write_snapshot(updated)
        return updated


def _require_revision(program: MixProgram, expected: int) -> None:
    if program.revision != expected:
        raise MixRevisionConflictError(
            "Mix program changed: "
            f"expected revision {expected}, current revision {program.revision}"
        )


def _write_snapshot(program: MixProgram) -> None:
    current = paths.mix_program_path(
        program.piece_id, program.movement, program.program_id, create=True
    )
    revision = paths.mix_program_revision_path(
        program.piece_id,
        program.movement,
        program.program_id,
        program.revision,
        create=True,
    )
    content = program.model_dump_json(indent=2) + "\n"
    _atomic_write(revision, content)
    _atomic_write(current, content)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _score_identity(piece_id: str, movement: int) -> tuple[str, str, str]:
    root = paths.score_bundle_dir(piece_id, movement)
    bundle_manifest = next(
        (
            candidate
            for candidate in (root / "bundle.yaml", root / "bundle.json")
            if candidate.exists()
        ),
        None,
    )
    timeline = next(
        (
            candidate
            for candidate in (
                root / "timeline.machine.json",
                root / "derived" / "timeline.machine.json",
                root / "derived" / "timeline.json",
            )
            if candidate.exists()
        ),
        None,
    )
    fallback = json.dumps({"piece_id": piece_id, "movement": movement}).encode()
    bundle_digest = hashlib.sha256(
        bundle_manifest.read_bytes() if bundle_manifest is not None else fallback
    ).hexdigest()
    timeline_digest = hashlib.sha256(
        timeline.read_bytes() if timeline is not None else fallback
    ).hexdigest()
    return root.name, bundle_digest[:12], timeline_digest


def _with_score_identity_status(program: MixProgram) -> MixProgram:
    bundle_id, revision, timeline_digest = _score_identity(
        program.piece_id, program.movement
    )
    status = (
        ScoreIdentityStatus.STALE
        if (
            program.score_bundle_id != bundle_id
            or program.score_bundle_revision != revision
            or program.timeline_digest != timeline_digest
        )
        else ScoreIdentityStatus.CURRENT
    )
    return program.model_copy(update={"score_identity_status": status})


def _require_current_score(program: MixProgram) -> None:
    if program.score_identity_status is ScoreIdentityStatus.STALE:
        raise MixScoreIdentityError(
            "Mix program score identity is stale; review it against the current timeline"
        )


def _starter_regions(piece_id: str, movement: int) -> tuple[MixRegion, ...]:
    """Seed no regions. A new mix program starts empty.

    A program previously auto-seeded an m.44 "room tails" listening example
    whose envelope dipped the level to 0 on every beat and fell back to
    ``yamaha_anchor``. Because ``room_center`` is uncalibrated it falls back to
    the audible Yamaha, so that demo silenced the Clavinova orchestra exactly on
    the m.44 reactive-trigger beats (traced in the 2026-08-11 live runs). Mix
    regions are an intentional, author-driven overlay, never a default; the
    program now starts with only its full-level default routes so the orchestra
    is always audible until the performer authors a region on purpose.
    """

    del piece_id, movement
    return ()


__all__ = [
    "MixProgramNotFoundError",
    "MixRegionNotFoundError",
    "MixRevisionConflictError",
    "MixScoreIdentityError",
    "add_region",
    "create_program",
    "delete_region",
    "list_programs",
    "load_program",
    "rebind_score_identity",
    "undo",
    "update_default_routes",
    "update_region",
]
