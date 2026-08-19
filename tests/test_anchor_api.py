"""The anchors API adds, lists, and deletes structural beat anchors."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimusic.server.app import create_app

_PIECE = {"piece_id": "chopin_op11"}


def test_anchor_api_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    client = TestClient(create_app())

    assert client.get("/api/scores/2/anchors", params=_PIECE).json()["anchors"] == []

    created = client.post(
        "/api/scores/2/anchors",
        params=_PIECE,
        json={"score_tick": 169920, "measure": 44, "label": "climax bass"},
    )
    assert created.status_code == 200
    assert [a["score_tick"] for a in created.json()["anchors"]] == [169920]

    # Persisted across a fresh list.
    listed = client.get("/api/scores/2/anchors", params=_PIECE)
    assert [a["measure"] for a in listed.json()["anchors"]] == [44]

    moved = client.patch(
        "/api/scores/2/anchors/169920",
        params=_PIECE,
        json={"new_score_tick": 170400, "measure": 44, "label": "climax bass"},
    )
    assert moved.status_code == 200
    assert [a["score_tick"] for a in moved.json()["anchors"]] == [170400]

    client.post(
        "/api/scores/2/anchors",
        params=_PIECE,
        json={"score_tick": 170880, "measure": 44},
    )
    collision = client.patch(
        "/api/scores/2/anchors/170400",
        params=_PIECE,
        json={"new_score_tick": 170880, "measure": 44},
    )
    assert collision.status_code == 409
    assert [
        anchor["score_tick"]
        for anchor in client.get("/api/scores/2/anchors", params=_PIECE).json()["anchors"]
    ] == [170400, 170880]

    deleted = client.delete("/api/scores/2/anchors/170400", params=_PIECE)
    assert deleted.status_code == 200
    assert [anchor["score_tick"] for anchor in deleted.json()["anchors"]] == [170880]


def test_anchor_api_clears_only_one_measure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    client = TestClient(create_app())
    for score_tick, measure in ((165120, 44), (166080, 44), (169920, 45)):
        response = client.post(
            "/api/scores/2/anchors",
            params=_PIECE,
            json={"score_tick": score_tick, "measure": measure},
        )
        assert response.status_code == 200

    cleared = client.delete(
        "/api/scores/2/anchors",
        params={**_PIECE, "measure": 44},
    )
    assert cleared.status_code == 200
    assert [(anchor["score_tick"], anchor["measure"]) for anchor in cleared.json()["anchors"]] == [
        (169920, 45)
    ]

    restored = client.post(
        "/api/scores/2/anchors/restore",
        params=_PIECE,
        json={
            "anchors": [
                {"score_tick": 165120, "measure": 44},
                {"score_tick": 166080, "measure": 44},
            ]
        },
    )
    assert restored.status_code == 200
    assert [(anchor["score_tick"], anchor["measure"]) for anchor in restored.json()["anchors"]] == [
        (165120, 44),
        (166080, 44),
        (169920, 45),
    ]


def test_anchor_api_rejects_bad_piece_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    client = TestClient(create_app())
    assert client.get("/api/scores/2/anchors", params={"piece_id": "bad id!"}).status_code == 422


def test_anchor_api_rejects_nonpositive_movement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    client = TestClient(create_app())
    assert client.get("/api/scores/0/anchors", params=_PIECE).status_code == 422
