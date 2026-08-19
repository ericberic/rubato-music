"""Structural beat anchors persist independently, keyed by score tick."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from aimusic.takes import store


def _ticks(piece: str, movement: int) -> list[int]:
    return [anchor.score_tick for anchor in store.load_anchor_set(piece, movement).anchors]


def test_anchor_add_load_remove_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))

    assert _ticks("chopin_op11", 2) == []

    store.add_anchor("chopin_op11", 2, score_tick=169920, measure=44, label="climax bass")
    store.add_anchor("chopin_op11", 2, score_tick=163200, measure=43)
    # Sorted by tick, and persisted across a fresh load.
    assert _ticks("chopin_op11", 2) == [163200, 169920]

    # Adding the same tick again is idempotent (replaces, does not duplicate).
    store.add_anchor("chopin_op11", 2, score_tick=169920, measure=44)
    assert _ticks("chopin_op11", 2) == [163200, 169920]

    store.remove_anchor("chopin_op11", 2, score_tick=169920)
    assert _ticks("chopin_op11", 2) == [163200]

    # A different movement has its own set.
    assert _ticks("chopin_op11", 1) == []


def test_anchor_move_and_measure_clear_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    store.add_anchor("chopin_op11", 2, score_tick=165120, measure=44)
    store.add_anchor("chopin_op11", 2, score_tick=166080, measure=44)
    store.add_anchor("chopin_op11", 2, score_tick=169920, measure=45)

    moved = store.move_anchor(
        "chopin_op11",
        2,
        score_tick=165120,
        new_score_tick=165600,
        measure=44,
    )
    assert [anchor.score_tick for anchor in moved.anchors] == [165600, 166080, 169920]

    with pytest.raises(store.AnchorConflictError, match="already exists"):
        store.move_anchor(
            "chopin_op11",
            2,
            score_tick=165600,
            new_score_tick=166080,
            measure=44,
        )
    assert _ticks("chopin_op11", 2) == [165600, 166080, 169920]

    cleared = store.remove_anchors_in_measure("chopin_op11", 2, measure=44)
    assert [(anchor.score_tick, anchor.measure) for anchor in cleared.anchors] == [(169920, 45)]
    restored = store.restore_anchors(
        "chopin_op11",
        2,
        anchors=tuple(anchor for anchor in moved.anchors if anchor.measure == 44),
    )
    assert [anchor.score_tick for anchor in restored.anchors] == [
        165600,
        166080,
        169920,
    ]

    with pytest.raises(ValueError, match="does not exist"):
        store.move_anchor(
            "chopin_op11",
            2,
            score_tick=165120,
            new_score_tick=165600,
            measure=44,
        )


def test_concurrent_anchor_edits_serialize_the_read_modify_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    original_load = store.load_anchor_set
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    start = threading.Barrier(3)

    def observed_load(piece_id: str, movement: int):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        try:
            return original_load(piece_id, movement)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(store, "load_anchor_set", observed_load)

    def add(score_tick: int) -> None:
        start.wait()
        store.add_anchor("chopin_op11", 2, score_tick=score_tick, measure=44)

    threads = [
        threading.Thread(target=add, args=(169920,)),
        threading.Thread(target=add, args=(170880,)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert max_active == 1
    assert _ticks("chopin_op11", 2) == [169920, 170880]
