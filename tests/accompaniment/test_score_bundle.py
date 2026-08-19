from __future__ import annotations

from pathlib import Path

import pytest

from aimusic.accompaniment import AccompanimentMode, ScoreBundle

FIXTURE = Path("tests/fixtures/score_bundles/tiny_chopin_excerpt")


def test_score_bundle_loads_runtime_contract() -> None:
    bundle = ScoreBundle.load(FIXTURE)

    assert bundle.metadata.piece_id == "tiny_chopin_excerpt"
    assert [part.id for part in bundle.parts] == ["piano_solo", "strings"]
    assert [event.event_id for event in bundle.solo_events] == ["solo_001", "solo_002"]
    assert [event.event_id for event in bundle.accompaniment_events] == [
        "accomp_001",
        "accomp_002",
    ]
    assert bundle.section_map.mode_at(0) == AccompanimentMode.LEAD
    assert bundle.section_map.mode_at(4) == AccompanimentMode.FOLLOW
    assert bundle.instrument_map[0].program == 48


def test_score_bundle_caches_filtered_event_views() -> None:
    bundle = ScoreBundle.load(FIXTURE)

    assert bundle.solo_events is bundle.solo_events
    assert bundle.accompaniment_events is bundle.accompaniment_events


def test_score_bundle_rejects_unknown_event_part(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    for path in FIXTURE.iterdir():
        (bundle_dir / path.name).write_bytes(path.read_bytes())
    (bundle_dir / "events.jsonl").write_text(
        '{"event_id":"bad","measure":1,"beat":0,"part_id":"missing",'
        '"role":"accompaniment","pitch":60,"duration_beats":1}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown part"):
        ScoreBundle.load(bundle_dir)


def test_score_bundle_rejects_unknown_instrument_part(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    for path in FIXTURE.iterdir():
        (bundle_dir / path.name).write_bytes(path.read_bytes())
    (bundle_dir / "instrument_map.yaml").write_text(
        "instruments:\n  - part_id: missing\n    channel: 0\n    program: 48\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Instrument map references unknown parts"):
        ScoreBundle.load(bundle_dir)
