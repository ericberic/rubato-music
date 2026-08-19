"""Contract, conversion, readiness, and migration tests for Score Bundle v2."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aimusic.accompaniment.bundle_v2 import (
    BundleLoaderV2,
    BundleManifest,
    BundleRegistry,
    SourceMappingDocument,
)
from aimusic.accompaniment.score_bundle import LegacyScoreBundleAdapter, ScoreBundle

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_v2")
LEGACY_FIXTURE = Path("tests/fixtures/score_bundles/tiny_chopin_excerpt")


def test_loads_complete_v2_bundle_and_reports_performance_ready() -> None:
    bundle = BundleLoaderV2.load(FIXTURE, require_ready=True)

    assert bundle.manifest.ref.model_dump() == {
        "bundle_id": "synthetic_movement_2",
        "revision": "fixture-v1",
        "timeline_id": "notation-v1",
    }
    assert bundle.readiness.sourcing
    assert bundle.readiness.rehearsal
    assert bundle.readiness.performance
    assert bundle.readiness.issues == ()


def test_canonical_timeline_handles_pickup_meter_change_and_unusual_label() -> None:
    timeline = BundleLoaderV2.load(FIXTURE).timeline

    pickup = timeline.position_at(480)
    assert pickup.measure_index == 0
    assert pickup.measure_label == "pickup"
    assert pickup.beat_in_measure == 0.5

    changed_meter = timeline.position_at(4800)
    assert changed_meter.measure_index == 2
    assert changed_meter.measure_label == "2a"
    assert changed_meter.offset_ticks == 960
    assert changed_meter.beat_in_measure == 1.0
    assert timeline.tick_at(2, 960) == 4800


@pytest.mark.parametrize("score_tick", [-1, 7680])
def test_canonical_timeline_rejects_positions_outside_half_open_range(score_tick: int) -> None:
    with pytest.raises(ValueError, match="score_tick must be"):
        BundleLoaderV2.load(FIXTURE).timeline.position_at(score_tick)


def test_performance_mapping_preserves_explicit_score_gap() -> None:
    mapping = next(
        mapping
        for mapping in BundleLoaderV2.load(FIXTURE).mappings
        if mapping.mapping_id == "performance_to_notation"
    )

    assert mapping.source_unit == "midi_tick"
    assert mapping.segments[0].source_end == 510
    assert mapping.segments[0].score_end_tick == 3840
    assert mapping.unmapped_score_spans[0].start_tick == 3840
    assert mapping.unmapped_score_spans[0].end_tick == 4800


def test_mapping_rejects_reversed_or_overlapping_correspondence() -> None:
    payload = json.loads((FIXTURE / "derived/performance_map.json").read_text())
    payload["segments"][1]["score_start_tick"] = 3000

    with pytest.raises(ValidationError, match="must not overlap or reverse"):
        SourceMappingDocument.model_validate(payload)


def test_manifest_rejects_parent_path() -> None:
    payload = BundleManifest.model_validate(
        yaml.safe_load((FIXTURE / "bundle.yaml").read_text())
    ).model_dump(mode="json")
    payload["sources"][0]["path"] = "../outside.musicxml"

    with pytest.raises(ValidationError) as exc_info:
        BundleManifest.model_validate(payload)

    messages = str(exc_info.value)
    assert "bundle-relative paths" in messages


def test_manifest_rejects_unknown_mapping_source() -> None:
    payload = yaml.safe_load((FIXTURE / "bundle.yaml").read_text())
    payload["mappings"][0]["source_id"] = "missing"

    with pytest.raises(ValidationError, match="references unknown source missing"):
        BundleManifest.model_validate(payload)


def test_manifest_rejects_unknown_source_role() -> None:
    payload = yaml.safe_load((FIXTURE / "bundle.yaml").read_text())
    payload["sources"][0]["role"] = "magic_score"

    with pytest.raises(ValidationError, match="semantic_score"):
        BundleManifest.model_validate(payload)


def test_readiness_rejects_declared_hash_drift(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(FIXTURE, root)
    manifest_path = root / "bundle.yaml"
    payload = yaml.safe_load(manifest_path.read_text())
    payload["sources"][0]["sha256"] = "0" * 64
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False))

    bundle = BundleLoaderV2.load(root)

    assert bundle.readiness.performance is False
    assert any(issue.code == "hash_mismatch" for issue in bundle.readiness.issues)


def test_readiness_reports_missing_file_without_losing_valid_manifest(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(FIXTURE, root)
    (root / "derived/accompaniment.txt").unlink()

    bundle = BundleLoaderV2.load(root)

    assert bundle.readiness.sourcing is False
    assert bundle.readiness.rehearsal is False
    assert bundle.readiness.performance is False
    assert any(issue.code == "missing_artifact" for issue in bundle.readiness.issues)
    with pytest.raises(ValueError, match="not performance-ready"):
        BundleLoaderV2.load(root, require_ready=True)


def test_machine_draft_timeline_is_not_sourcing_ready(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(FIXTURE, root)
    manifest_path = root / "bundle.yaml"
    payload = yaml.safe_load(manifest_path.read_text())
    payload["timeline"]["review_state"] = "machine"
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False))

    bundle = BundleLoaderV2.load(root)

    assert not bundle.readiness.sourcing
    assert any(issue.code == "timeline_not_reviewed" for issue in bundle.readiness.issues)


def test_loader_rejects_manifest_timeline_identity_drift(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(FIXTURE, root)
    timeline_path = root / "derived/timeline.json"
    payload = json.loads(timeline_path.read_text())
    payload["timeline_id"] = "different"
    timeline_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="timeline_id differ"):
        BundleLoaderV2.load(root)


def test_registry_requires_revision_when_bundle_id_is_ambiguous(tmp_path: Path) -> None:
    second = tmp_path / "second"
    shutil.copytree(FIXTURE, second)
    manifest_path = second / "bundle.yaml"
    manifest_path.write_text(manifest_path.read_text().replace("fixture-v1", "fixture-v2"))

    registry = BundleRegistry()
    registry.register(FIXTURE)
    registry.register(second)

    with pytest.raises(ValueError, match="revision required"):
        registry.resolve("synthetic_movement_2")
    assert registry.load("synthetic_movement_2", "fixture-v2").manifest.revision == "fixture-v2"


def test_legacy_runtime_bundle_remains_available_through_adapter() -> None:
    direct = LegacyScoreBundleAdapter.load(LEGACY_FIXTURE)
    delegated = ScoreBundle.load(LEGACY_FIXTURE)

    assert direct.metadata == delegated.metadata
    assert [event.event_id for event in delegated.solo_events] == ["solo_001", "solo_002"]
