"""The alignment-validation API: worklist + per-measure audition.

Runs against the committed Chopin Movement 2 bundle (beat map + the two
cross-check alignments are git-tracked), so these exercise the real defect
region rather than a synthetic fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aimusic.server.app import create_app

_PIECE = {"piece_id": "chopin_op11"}


from tests.oguri_guard import requires_oguri_derived

@requires_oguri_derived
def test_worklist_only_assigns_audible_orchestra_measures() -> None:
    client = TestClient(create_app())
    body = client.get("/api/scores/2/alignment-worklist", params=_PIECE).json()
    assert body["available"] is True
    by_label = {m["measure_label"]: m for m in body["measures"]}
    # The orchestral fallback now identifies mm.104-105 instead of interpolating
    # through the solo's rest. m.17's large-rubato cross-check disagreement is
    # retained in the machine artifacts, but mm.16-17 are orchestra tacet and
    # cannot be judged through this orchestra-only listening workflow.
    assert "16" not in by_label
    assert "17" not in by_label
    assert "104" not in by_label
    assert "105" not in by_label
    assert all(item["reason"] == "spine" for item in body["measures"])
    # A sparse spine remains across measures with audible accompaniment.
    assert any(m["reason"] == "spine" for m in body["measures"])


def test_worklist_validates_piece_and_movement() -> None:
    client = TestClient(create_app())
    bad_piece = client.get("/api/scores/2/alignment-worklist", params={"piece_id": "bad id!"})
    assert bad_piece.status_code == 422
    assert client.get("/api/scores/0/alignment-worklist", params=_PIECE).status_code == 422


@requires_oguri_derived
def test_beat_audition_returns_beats_candidates_and_a_count_in_tempo() -> None:
    client = TestClient(create_app())
    body = client.get("/api/scores/2/beat-audition/13", params=_PIECE).json()
    assert body["measure_label"] == "13"
    assert body["beat_period_seconds"] > 0.0
    assert body["measure_end_seconds"] > body["measure_start_seconds"]
    beats = body["beats"]
    assert [b["beat_in_measure"] for b in beats] == [0.0, 1.0, 2.0, 3.0]
    # The reference solo is committed, so a real bar offers onsets to pick among.
    assert any(b["candidates"] for b in beats)
    downbeat = beats[0]
    assert downbeat["source_seconds"] > 0.0
    # Candidates carry their own exact time; nothing is estimated by hand.
    for candidate in downbeat["candidates"]:
        assert abs(candidate["delta_seconds"]) <= 1.5


def test_beat_audition_unknown_measure_is_404() -> None:
    client = TestClient(create_app())
    assert client.get("/api/scores/2/beat-audition/9999", params=_PIECE).status_code == 404


@requires_oguri_derived
def test_measure_104_exposes_musical_orchestra_boundary_choices() -> None:
    body = TestClient(create_app()).get("/api/scores/2/beat-audition/104", params=_PIECE).json()

    groups = body["orchestra_groups"]
    previous_g_sharp = next(
        group
        for group in groups
        if group["source_seconds"] < body["measure_start_seconds"] - 2 and 68 in group["pitches"]
    )
    downbeat_chord = min(groups, key=lambda group: abs(group["delta_seconds"]))
    assert previous_g_sharp["pitches"] == [56, 68]
    assert downbeat_chord["source_seconds"] == pytest.approx(529.4541666667)
    assert {56, 63, 66, 68}.issubset(downbeat_chord["pitches"])
    assert "Corni (E)" in downbeat_chord["instruments"]


@requires_oguri_derived
def test_measure_109_uses_orchestral_reduction_for_the_four_printed_beats() -> None:
    """The solo run must not compress three clicks into the measure's first half."""

    body = TestClient(create_app()).get("/api/scores/2/beat-audition/109", params=_PIECE).json()
    beats = body["beats"]
    assert [beat["source_midi_tick"] for beat in beats] == [266822, 267548, 268074, 268598]
    assert beats[-1]["source_seconds"] - beats[0]["source_seconds"] > 3.5
    # The rapid attack between beats 2 and 3 is a subdivision, not a click.
    group_ticks = [group["source_midi_tick"] for group in body["orchestra_groups"]]
    for expected in (266802, 267488, 268087, 268593):
        assert expected in group_ticks


def test_measure_107_rejects_the_collapsed_solo_run_as_its_printed_pulse() -> None:
    """A few solo-run matches must not bunch three orchestral clicks together."""

    body = TestClient(create_app()).get("/api/scores/2/beat-audition/107", params=_PIECE).json()
    beats = body["beats"]
    assert [beat["source_midi_tick"] for beat in beats] == [262133, 262692, 263252, 263811]
    intervals = [
        beats[index + 1]["source_seconds"] - beats[index]["source_seconds"]
        for index in range(3)
    ]
    assert max(intervals) - min(intervals) < 0.01
    assert beats[-1]["source_seconds"] - beats[0]["source_seconds"] > 3.4


def test_beat_audition_play_starts_one_managed_hardware_job(monkeypatch) -> None:
    from aimusic.server import routes
    from tests.fake_hardware import FakeLiveControl

    fake = FakeLiveControl()
    monkeypatch.setattr(routes, "live_control", fake)
    response = TestClient(create_app()).post(
        "/api/scores/2/beat-audition/13/play",
        params=_PIECE,
        json={"output_name": "Clavinova", "volume": 0.6},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "alignment_audition"
    assert fake.managed_args is not None
    assert fake.managed_args["kind"] == "alignment_audition"
    assert "measure 13" in str(fake.managed_args["message"])


@requires_oguri_derived
def test_mode_b_correction_persists_the_exact_selected_tick(tmp_path, monkeypatch) -> None:
    # A picked candidate must be stored at its own MIDI tick, not re-interpolated.
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    client = TestClient(create_app())
    audition = client.get("/api/scores/2/beat-audition/13", params=_PIECE).json()
    beat = next(b for b in audition["beats"] if b["candidates"])
    candidate = beat["candidates"][0]
    resp = client.post(
        "/api/scores/2/alignment-corrections",
        params=_PIECE,
        json={
            "source_seconds": candidate["source_seconds"],
            "source_midi_tick": candidate["source_midi_tick"],
            "measure": int(audition["measure_label"]),
            "beat_in_measure": beat["beat_in_measure"],
            "note": "exact-tick test",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["source_midi_tick"] == candidate["source_midi_tick"]
    revised = client.get("/api/scores/2/beat-audition/13", params=_PIECE).json()
    revised_beat = next(
        item for item in revised["beats"] if item["beat_in_measure"] == beat["beat_in_measure"]
    )
    assert revised_beat["source_midi_tick"] == candidate["source_midi_tick"]


@requires_oguri_derived
def test_final_measure_audition_bounds_do_not_truncate() -> None:
    # The last measure has no next downbeat; its window must still extend past
    # the last beat rather than collapsing to it.
    client = TestClient(create_app())
    worklist = client.get("/api/scores/2/alignment-worklist", params=_PIECE).json()
    last_label = max((m["measure_label"] for m in worklist["measures"]), key=int)
    body = client.get(f"/api/scores/2/beat-audition/{last_label}", params=_PIECE).json()
    last_beat = body["beats"][-1]["source_seconds"]
    assert body["measure_end_seconds"] > last_beat
    assert body["beat_period_seconds"] > 0.0
