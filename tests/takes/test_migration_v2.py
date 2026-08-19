from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

from aimusic.takes.lifecycle import AnalysisState, ProfileMembership, UserDisposition
from aimusic.takes.migration import _atomic_write, inventory_tree, migrate_take_directory
from aimusic.takes.models import AlignedResult, LocalizationCandidate, TakeRecord

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _write_v1(directory: Path, *, status: str, aligned: bool = False) -> None:
    directory.mkdir(parents=True)
    take = TakeRecord(
        take_id=directory.name,
        piece_id="chopin_op11",
        movement=2,
        recorded_at=NOW,
        duration_seconds=5.0,
        note_on_count=8,
        status=status,
        midi_path="take.mid",
    )
    (directory / "take.json").write_text(take.model_dump_json(indent=2), encoding="utf-8")
    if aligned:
        result = AlignedResult(
            take_id=directory.name,
            aligner="legacy-v1",
            score_start_beat=2.0,
            score_end_beat=6.0,
            match_rate=0.9,
            ambiguous=False,
            matched_notes=8,
            extra_notes=0,
            missing_notes=0,
            timing_map=(),
            candidates=(LocalizationCandidate(start_beat=2.0, score=0.9, start_position=4),),
            cell_samples=(),
            edge_trim_beats=(0.0, 0.0),
        )
        (directory / "aligned.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")


def test_migration_is_dry_run_then_idempotent_apply(tmp_path: Path) -> None:
    take_dir = tmp_path / "takes" / "chopin_op11" / "2" / "take-1"
    _write_v1(take_dir, status="aligned", aligned=True)

    dry_run = migrate_take_directory(take_dir)
    first_apply = migrate_take_directory(take_dir, apply=True)
    second_apply = migrate_take_directory(take_dir, apply=True)

    assert dry_run.action == "would_write"
    assert (take_dir / "take.v2.json").exists()
    assert first_apply.action == "written"
    assert second_apply.action == "unchanged"
    assert first_apply.take is not None
    assert first_apply.take.analysis_state == AnalysisState.ALIGNED
    assert first_apply.take.profile_membership == ProfileMembership.INCLUDED
    assert first_apply.aligned is not None
    assert first_apply.aligned.start_score_tick == 1920
    assert first_apply.aligned.candidates[0].candidate_id.startswith("cand_")


def test_alignment_migration_drops_invalid_negative_legacy_candidate(tmp_path: Path) -> None:
    """A weak negative alternate must not erase a successful primary result."""

    take_dir = tmp_path / "take-negative-alternate"
    _write_v1(take_dir, status="aligned", aligned=True)
    path = take_dir / "aligned.json"
    aligned = AlignedResult.model_validate_json(path.read_text(encoding="utf-8"))
    with_negative = aligned.model_copy(
        update={
            "candidates": aligned.candidates
            + (LocalizationCandidate(start_beat=0.0, score=0.01, start_position=-361),)
        }
    )
    path.write_text(with_negative.model_dump_json(indent=2), encoding="utf-8")

    result = migrate_take_directory(take_dir, apply=True)

    assert result.aligned is not None
    assert [candidate.start_position for candidate in result.aligned.candidates] == [4]
    assert (take_dir / "aligned.v2.json").exists()


def test_discarded_migration_recovers_alignment_without_losing_disposition(
    tmp_path: Path,
) -> None:
    take_dir = tmp_path / "take-2"
    _write_v1(take_dir, status="discarded", aligned=True)

    result = migrate_take_directory(take_dir)

    assert result.take is not None
    assert result.take.analysis_state == AnalysisState.ALIGNED
    assert result.take.disposition == UserDisposition.DISCARDED
    assert result.take.profile_membership == ProfileMembership.EXCLUDED


def test_discarded_without_alignment_uses_captured_as_honest_fallback(tmp_path: Path) -> None:
    take_dir = tmp_path / "take-3"
    _write_v1(take_dir, status="discarded")

    result = migrate_take_directory(take_dir)

    assert result.take is not None
    assert result.take.analysis_state == AnalysisState.CAPTURED
    assert result.take.alignment_artifact is None


def test_inventory_reports_v1_fallback_and_dual_usage(tmp_path: Path) -> None:
    root = tmp_path / "takes"
    v1_only = root / "chopin_op11" / "2" / "take-1"
    dual = root / "chopin_op11" / "2" / "take-2"
    _write_v1(v1_only, status="captured")
    _write_v1(dual, status="aligned", aligned=True)
    migrate_take_directory(dual, apply=True)

    report = inventory_tree(root)
    assert report.take_directories == 2
    assert report.v1_only == 1
    assert report.dual == 1
    assert report.aligned_dual == 1
    assert report.v1_fallback_required == 1
    assert report.ready_to_retire_v1_reads is False


def test_inventory_is_dry_run_and_reports_retirement_readiness(tmp_path: Path) -> None:
    root = tmp_path / "takes"
    directory = root / "chopin_op11" / "2" / "take-1"
    _write_v1(directory, status="captured")
    migrate_take_directory(directory, apply=True)
    (directory / "take.json").unlink()

    before = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    report = inventory_tree(root)
    after = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    assert before == after
    assert report.v2_only == 1
    assert report.v1_fallback_required == 0
    assert report.ready_to_retire_v1_reads is True


def test_atomic_write_uses_distinct_temporary_files_for_concurrent_writers(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "take.v2.json"
    barrier = Barrier(2)
    original_write_text = Path.write_text

    def synchronized_write(path: Path, content: str, **kwargs: object) -> int:
        written = original_write_text(path, content, **kwargs)
        barrier.wait(timeout=2)
        return written

    monkeypatch.setattr(Path, "write_text", synchronized_write)
    contents = ('{"writer": 1}\n', '{"writer": 2}\n')

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_atomic_write, destination, content) for content in contents]
        for future in futures:
            future.result()

    assert destination.read_text(encoding="utf-8") in contents
    assert list(tmp_path.glob("*.tmp")) == []
