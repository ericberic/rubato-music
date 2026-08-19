from __future__ import annotations

import os
from multiprocessing import get_context
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimusic.mixing import store
from aimusic.mixing.models import MixProgramCreate, MixRoute
from aimusic.server.app import create_app


@pytest.fixture()
def isolated_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(root))
    return root


def test_store_revision_conflict_and_durable_undo(isolated_data: Path) -> None:
    created = store.create_program(
        MixProgramCreate(piece_id="chopin_op11", movement=2)
    )
    updated = store.update_default_routes(
        "chopin_op11",
        2,
        "main",
        expected_revision=created.revision,
        routes=(MixRoute(zone_id="yamaha_anchor", stem_ids=("orchestra",), level=30),),
    )
    assert updated.revision == 2
    assert updated.default_routes[0].level == 30

    with pytest.raises(store.MixRevisionConflictError):
        store.update_default_routes(
            "chopin_op11",
            2,
            "main",
            expected_revision=1,
            routes=updated.default_routes,
        )

    second_update = store.update_default_routes(
        "chopin_op11",
        2,
        "main",
        expected_revision=updated.revision,
        routes=(MixRoute(zone_id="yamaha_anchor", stem_ids=("orchestra",), level=40),),
    )
    first_undo = store.undo(
        "chopin_op11", 2, "main", expected_revision=second_update.revision
    )
    assert first_undo.revision == 4
    assert [route.level for route in first_undo.default_routes] == [30]
    second_undo = store.undo(
        "chopin_op11", 2, "main", expected_revision=first_undo.revision
    )
    assert second_undo.revision == 5
    assert [route.level for route in second_undo.default_routes] == [25, 100]
    exhausted = store.undo(
        "chopin_op11", 2, "main", expected_revision=second_undo.revision
    )
    assert exhausted.revision == second_undo.revision
    revision_path = (
        isolated_data
        / "profiles/chopin_op11/2/mix-programs/main/revisions/000005.json"
    )
    assert revision_path.exists()


def test_store_lists_stale_program_and_supports_explicit_rebind(
    isolated_data: Path,
) -> None:
    created = store.create_program(
        MixProgramCreate(piece_id="chopin_op11", movement=2)
    )
    document = isolated_data / "profiles/chopin_op11/2/mix-programs/main/mix-program.json"
    document.write_text(
        created.model_copy(update={"timeline_digest": "stale"}).model_dump_json(indent=2),
        encoding="utf-8",
    )

    listed = store.list_programs("chopin_op11", 2)
    assert listed[0].score_identity_status == "stale"
    stale = store.load_program("chopin_op11", 2, "main")
    assert stale.score_identity_status == "stale"
    with pytest.raises(store.MixScoreIdentityError, match="score identity is stale"):
        store.update_default_routes(
            "chopin_op11",
            2,
            "main",
            expected_revision=stale.revision,
            routes=stale.default_routes,
        )

    rebound = store.rebind_score_identity(
        "chopin_op11", 2, "main", expected_revision=stale.revision
    )
    assert rebound.revision == stale.revision + 1
    assert rebound.score_identity_status == "current"
    assert store.load_program(
        "chopin_op11", 2, "main", require_current_score=True
    ) == rebound


def _concurrent_mix_write(data_root: str, start, results, level: int) -> None:
    os.environ["AIMUSIC_DATA_ROOT"] = data_root
    start.wait()
    try:
        updated = store.update_default_routes(
            "chopin_op11",
            2,
            "main",
            expected_revision=1,
            routes=(
                MixRoute(
                    zone_id="yamaha_anchor",
                    stem_ids=("orchestra",),
                    level=level,
                ),
            ),
        )
        results.put(("updated", updated.revision))
    except store.MixRevisionConflictError:
        results.put(("conflict", None))


def test_cross_process_writes_preserve_one_revision_winner(isolated_data: Path) -> None:
    store.create_program(MixProgramCreate(piece_id="chopin_op11", movement=2))
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_mix_write,
            args=(str(isolated_data), start, results, level),
        )
        for level in (31, 32, 33, 34)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    assert [outcome[0] for outcome in outcomes].count("updated") == 1
    assert [outcome[0] for outcome in outcomes].count("conflict") == 3
    assert store.load_program("chopin_op11", 2, "main").revision == 2


def test_mix_api_region_lifecycle_and_conflict(isolated_data: Path) -> None:
    client = TestClient(create_app())
    create = client.post(
        "/api/mix/programs",
        json={"piece_id": "chopin_op11", "movement": 2, "program_id": "main"},
    )
    assert create.status_code == 200
    program = create.json()
    region = {
        "region_id": "m45_swell",
        "start_tick": 1_000,
        "end_tick": 2_000,
        "gesture": "swell",
        "routes": [
            {
                "zone_id": "room_center",
                "stem_ids": ["orchestra"],
                "level": 100,
                "envelope": [
                    {"score_tick": 1_000, "level": 15, "curve": "equal_power"},
                    {"score_tick": 2_000, "level": 100, "curve": "equal_power"},
                ],
            }
        ],
    }
    added = client.post(
        "/api/mix/programs/main/regions?piece_id=chopin_op11&movement=2",
        json={"expected_revision": program["revision"], "region": region},
    )
    assert added.status_code == 200, added.text
    assert "m45_swell" in {
        item["region_id"] for item in added.json()["regions"]
    }

    stale = client.delete(
        "/api/mix/programs/main/regions/m45_swell"
        "?piece_id=chopin_op11&movement=2&expected_revision=1"
    )
    assert stale.status_code == 409

    current_revision = added.json()["revision"]
    deleted = client.delete(
        "/api/mix/programs/main/regions/m45_swell"
        f"?piece_id=chopin_op11&movement=2&expected_revision={current_revision}"
    )
    assert deleted.status_code == 200
    assert "m45_swell" not in {
        item["region_id"] for item in deleted.json()["regions"]
    }


def test_mix_zone_contract_is_explicit_about_uncalibrated_soundbar(
    isolated_data: Path,
) -> None:
    response = TestClient(create_app()).get("/api/mix/zones")
    assert response.status_code == 200
    zones = {zone["zone_id"]: zone for zone in response.json()["zones"]}
    assert zones["yamaha_anchor"]["health"] == "ready"
    assert zones["room_center"]["configured_output_advance_ms"] == 59
    assert zones["room_center"]["health"] == "needs_calibration"
