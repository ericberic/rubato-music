"""API contract tests for the take-store endpoints (roadmap item 2/3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick
from pydantic import ValidationError

import aimusic.server.routes as routes
from aimusic.core import paths
from aimusic.server.app import create_app
from aimusic.takes import aligner as take_aligner
from aimusic.takes import store as take_store
from aimusic.takes.lifecycle import AnalysisState, LifecycleFailure
from aimusic.takes.models import AlignedResult, TimingMapPoint
from tests.fake_hardware import FakeLiveControl, FakeLiveRuntime
from tests.oguri_guard import requires_oguri_derived

REPO_ROOT = Path(__file__).resolve().parent.parent
SOLO_REFERENCE_PATH = (
    REPO_ROOT / "assets/scores/chopin_op11_ii_larghetto/derived/solo_reference.mid"
)


class FakeAlignmentWorker:
    """Records `enqueue` calls without running them (matching the real
    worker's async contract, per `test_stop_take_record_saves_take_and_
    enqueues_alignment`'s "the fake worker doesn't flip it to aligning").

    `enqueue_resolve` runs synchronously instead of backgrounding, since
    resolve tests need to assert on the final resolved state without
    threading/polling -- the real production worker (tested directly at the
    aligner layer) is what actually backgrounds the work.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, int, str]] = []
        self.resolved: list[tuple[str, int, str, int]] = []

    def enqueue(self, piece_id: str, movement: int, take_id: str) -> None:
        self.enqueued.append((piece_id, movement, take_id))

    def enqueue_resolve(
        self, piece_id: str, movement: int, take_id: str, candidate_index: int
    ) -> take_store.Take:
        self.resolved.append((piece_id, movement, take_id, candidate_index))
        return take_aligner.resolve_take(piece_id, movement, take_id, candidate_index)


@pytest.fixture()
def fake_live(monkeypatch: pytest.MonkeyPatch) -> FakeLiveControl:
    fake = FakeLiveControl()
    monkeypatch.setattr(routes, "live_control", fake)
    return fake


@pytest.fixture()
def fake_runtime(
    monkeypatch: pytest.MonkeyPatch, fake_live: FakeLiveControl
) -> FakeLiveRuntime:
    fake = FakeLiveRuntime(fake_live)
    monkeypatch.setattr(routes, "live_runtime", fake)
    return fake


@pytest.fixture()
def fake_alignment_worker(monkeypatch: pytest.MonkeyPatch) -> FakeAlignmentWorker:
    fake = FakeAlignmentWorker()
    monkeypatch.setattr(routes, "alignment_worker", fake)
    return fake


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_live: FakeLiveControl,
    fake_runtime: FakeLiveRuntime,
    fake_alignment_worker: FakeAlignmentWorker,
) -> TestClient:
    _ = fake_runtime
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    return TestClient(create_app())


def test_start_take_record_generates_id_and_starts_hardware_recording(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["take_id"].startswith("t")
    assert body["status"]["running"] is True
    assert fake_live.record_args["input_name"] == "CLP-795GP USB"
    assert fake_live.record_args["session_id"] == body["take_id"]


def test_passage_analysis_ignores_unknown_take_ids(client: TestClient) -> None:
    response = client.post(
        "/api/takes/passage-analysis",
        json={"piece_id": "chopin_op11", "movement": 2, "take_ids": ["missing-take"]},
    )

    assert response.status_code == 200
    assert response.json()["take_count"] == 0


def test_stop_take_record_saves_take_and_enqueues_alignment(
    client: TestClient, fake_live: FakeLiveControl, fake_alignment_worker: FakeAlignmentWorker
) -> None:
    start_response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start_response.json()["take_id"]

    stop_response = client.post(f"/api/takes/{take_id}/stop")

    assert stop_response.status_code == 200
    body = stop_response.json()
    assert body["take_id"] == take_id
    assert body["piece_id"] == "chopin_op11"
    assert body["movement"] == 2
    assert body["note_on_count"] == 2
    assert body["status"] == "captured"  # the fake worker doesn't flip it to "aligning"
    assert body["midi_url"] == f"/api/takes/{take_id}/midi?piece_id=chopin_op11&movement=2"
    assert fake_alignment_worker.enqueued == [("chopin_op11", 2, take_id)]
    assert body["cue"] is None  # a free take carries no localization hint


def test_selected_passage_without_lead_in_is_preserved_as_placement_hint(
    client: TestClient, fake_alignment_worker: FakeAlignmentWorker
) -> None:
    start = client.post(
        "/api/takes/record",
        json={
            "input_name": "CLP-795GP USB",
            "piece_id": "chopin_op11",
            "movement": 2,
            "target_score_beat": 88.0,
        },
    )
    take_id = start.json()["take_id"]

    stopped = client.post(f"/api/takes/{take_id}/stop")

    assert stopped.status_code == 200
    assert stopped.json()["cue"] is None
    assert stopped.json()["placement_hint"] == {
        "target_beat": 88.0,
        "source": "selected_passage",
    }
    assert fake_alignment_worker.enqueued == [("chopin_op11", 2, take_id)]


def test_display_span_holds_at_final_matched_onset_instead_of_inventing_measures(
    client: TestClient,
) -> None:
    start = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")
    take_store.write_aligned_result(
        "chopin_op11",
        2,
        take_id,
        AlignedResult(
            take_id=take_id,
            aligner="phrase-boundary-regression",
            score_start_beat=242.3125,
            score_end_beat=516.4,
            match_rate=0.9,
            ambiguous=False,
            matched_notes=2,
            extra_notes=0,
            missing_notes=0,
            timing_map=(
                TimingMapPoint(score_beat=242.3125, take_seconds=8.0),
                TimingMapPoint(score_beat=516.4, take_seconds=138.15),
            ),
            candidates=(),
            cell_samples=(),
            edge_trim_beats=(0.0, 0.0),
        ),
    )
    take_store.update_take_status("chopin_op11", 2, take_id, take_store.ALIGNED)

    response = client.get("/api/takes?piece_id=chopin_op11&movement=2")
    response_take = response.json()["takes"][0]
    span = response_take["score_span"]

    assert span["start"]["measure_label"] == "23"
    assert span["end"]["measure_label"] == "52"
    assert span["end"]["beat_in_measure"] < 0.15
    assert response_take["alignment"] == {
        "rating": "strong",
        "note_match_rate": 0.9,
        "matched_notes": 2,
        "extra_notes": 0,
        "missing_notes": 0,
        "ambiguous": False,
    }


def test_retry_alignment_requeues_retryable_software_failure(
    client: TestClient, fake_alignment_worker: FakeAlignmentWorker
) -> None:
    start = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")
    take_store.transition_take_analysis("chopin_op11", 2, take_id, AnalysisState.QUEUED)
    take_store.transition_take_analysis("chopin_op11", 2, take_id, AnalysisState.RUNNING)
    take_store.transition_take_analysis(
        "chopin_op11",
        2,
        take_id,
        AnalysisState.FAILED,
        failure=LifecycleFailure(
            code="alignment_crash", message="invalid alternate candidate", retryable=True
        ),
    )
    fake_alignment_worker.enqueued.clear()

    response = client.post(f"/api/takes/{take_id}/alignment/retry?piece_id=chopin_op11&movement=2")

    assert response.status_code == 202
    assert fake_alignment_worker.enqueued == [("chopin_op11", 2, take_id)]
    assert response.json()["failure"]["code"] == "alignment_crash"


def test_retry_alignment_requires_take_namespace(client: TestClient) -> None:
    response = client.post("/api/takes/t20260710T150808Z-9e29/alignment/retry")

    assert response.status_code == 422
    missing_fields = {error["loc"][-1] for error in response.json()["detail"]}
    assert missing_fields == {"piece_id", "movement"}


def test_cued_take_saves_through_the_same_take_store_pipeline(
    client: TestClient, fake_live: FakeLiveControl, fake_alignment_worker: FakeAlignmentWorker
) -> None:
    """Design doc §1/§4: the cued path (`/hardware/record-with-cue/start`)
    must produce a take in the take store, same as the free path, and the
    take must carry the cue as a localization hint -- not silently drop it
    (the exact bug the redesign fixes).
    """

    take_id = "t20260710T150808Z-9e29"
    start_response = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "CLP-795GP USB",
            "output_name": "Fake Synth",
            "session_id": take_id,
        },
    )
    assert start_response.status_code == 200
    start_body = start_response.json()
    assert start_body["kind"] == "live_follow"
    # The orchestra starts on the selected measure; the performer enters
    # whenever ready after it.
    assert start_body["cue_start_measure"] == 12
    assert start_body["entry_measure"] == 12
    assert start_body["cue_start_score_beat"] == 44.0
    assert start_body["entry_score_beat"] == 47.0

    stop_response = client.post(f"/api/takes/{take_id}/stop")

    assert stop_response.status_code == 200
    body = stop_response.json()
    assert body["take_id"] == take_id
    assert body["cue"] == {
        "kind": "from_top",
        "target_beat": 47.0,
        "cue_start_beat": 44.0,
        "cue_seconds": pytest.approx(start_body["actual_cue_seconds"]),
        "output_name": "Fake Synth",
    }
    assert fake_alignment_worker.enqueued == [("chopin_op11", 2, take_id)]


def test_stop_without_a_finished_recording_returns_404(client: TestClient) -> None:
    response = client.post("/api/takes/never-started/stop")
    assert response.status_code == 404


def test_list_takes_returns_recorded_takes(client: TestClient, fake_live: FakeLiveControl) -> None:
    start_response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start_response.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")

    response = client.get("/api/takes?piece_id=chopin_op11&movement=2")

    assert response.status_code == 200
    takes = response.json()["takes"]
    assert [take["take_id"] for take in takes] == [take_id]


def test_list_takes_empty_for_unstarted_movement(client: TestClient) -> None:
    response = client.get("/api/takes?piece_id=chopin_op11&movement=3")
    assert response.status_code == 200
    assert response.json() == {"takes": []}


def test_delete_take_marks_discarded_without_removing_it(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    start_response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start_response.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")

    delete_response = client.delete(f"/api/takes/{take_id}?piece_id=chopin_op11&movement=2")

    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "discarded"

    listed = client.get("/api/takes?piece_id=chopin_op11&movement=2").json()["takes"]
    assert len(listed) == 1
    assert listed[0]["status"] == "discarded"


def test_delete_unknown_take_returns_404(client: TestClient) -> None:
    response = client.delete("/api/takes/does-not-exist?piece_id=chopin_op11&movement=2")
    assert response.status_code == 404


def test_download_take_midi_endpoint(client: TestClient, fake_live: FakeLiveControl) -> None:
    start_response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start_response.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")

    response = client.get(f"/api/takes/{take_id}/midi?piece_id=chopin_op11&movement=2")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/midi"
    assert len(response.content) > 0


def _write_repeated_coda_take_midi(path: Path) -> None:
    """The genuinely-repeated coda passage (design doc §2.4's ambiguity
    example, also used in tests/takes/test_aligner.py) -- lands `ambiguous`
    with exactly two candidates.
    """

    reference = take_aligner.load_canonical_note_sequence(
        SOLO_REFERENCE_PATH, track_name_contains=("PIANO SOLO",)
    )
    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])

    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    tempo = bpm2tempo(120)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    start_seconds = notes[0].time_seconds
    last_time = 0.0
    for note in notes:
        elapsed = note.time_seconds - start_seconds
        delta = max(0.0, elapsed - last_time)
        delta_ticks = int(round(second2tick(delta, 480, tempo)))
        track.append(Message("note_on", note=note.pitch, velocity=64, time=delta_ticks))
        track.append(Message("note_off", note=note.pitch, velocity=0, time=60))
        last_time = elapsed
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


@requires_oguri_derived
def test_resolve_take_endpoint_picks_the_other_candidate(
    client: TestClient, tmp_path: Path
) -> None:
    take_midi_path = tmp_path / "ambiguous.mid"
    _write_repeated_coda_take_midi(take_midi_path)

    take = take_store.save_take("chopin_op11", 2, take_midi_path)
    first_pass = take_aligner.align_and_store("chopin_op11", 2, take.take_id)
    assert first_pass.status == take_store.AMBIGUOUS

    listed = client.get("/api/takes?piece_id=chopin_op11&movement=2").json()["takes"]
    body = next(t for t in listed if t["take_id"] == take.take_id)
    assert body["status"] == "ambiguous"
    assert len(body["candidates"]) == 2

    response = client.post(
        f"/api/takes/{take.take_id}/resolve",
        json={"piece_id": "chopin_op11", "movement": 2, "candidate_index": 1},
    )

    assert response.status_code == 200
    resolved = response.json()
    assert resolved["status"] in {"aligned", "unalignable"}
    # candidates are only surfaced for status="ambiguous" (the resolve
    # card's only consumer) -- exposing them for every other status would
    # be an unconditional aligned.json read per take when listing.
    assert resolved["candidates"] is None


@requires_oguri_derived
def test_list_takes_raises_on_malformed_candidates_on_disk(
    client: TestClient, tmp_path: Path
) -> None:
    """A hand-edited or corrupted aligned.json with candidate entries missing
    'start_beat'/'score', or not shaped like a list at all, is a violation of
    our own `AlignedResult` schema (design doc §2.1/§3) -- listing takes must
    fail loudly (`pydantic.ValidationError` propagating from the read
    boundary), not silently filter the bad entries out. This replaces the
    old isinstance/key-presence tolerance in `_take_response`, which is
    exactly the whack-a-mole bandaid pattern issue #85 removes.
    """

    take_midi_path = tmp_path / "ambiguous.mid"
    _write_repeated_coda_take_midi(take_midi_path)

    take = take_store.save_take("chopin_op11", 2, take_midi_path)
    aligned = take_aligner.align_and_store("chopin_op11", 2, take.take_id)
    assert aligned.status == take_store.AMBIGUOUS

    aligned_path = paths.take_dir("chopin_op11", 2, take.take_id, create=False) / "aligned.json"
    raw = json.loads(aligned_path.read_text(encoding="utf-8"))
    raw["candidates"] = [raw["candidates"][0], {"missing": "fields"}, "not even a dict"]
    aligned_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError):
        client.get("/api/takes?piece_id=chopin_op11&movement=2")


def test_resolve_unknown_take_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/takes/does-not-exist/resolve",
        json={"piece_id": "chopin_op11", "movement": 2, "candidate_index": 0},
    )
    assert response.status_code == 404


def test_ensure_valid_take_id_rejects_traversal_and_special_characters() -> None:
    """take_id is joined directly into a filesystem path (paths.take_dir).
    A literal '../' in the URL path gets collapsed by client-side URL
    normalization before it ever reaches routing (verified: a request to
    '/api/takes/../../etc/passwd/resolve' resolves client-side to
    '/etc/passwd/resolve', a 404 from routing, never reaching this
    function) -- so this validates the function directly rather than via
    an HTTP round-trip that can't actually exercise it with a raw '..'.
    """

    with pytest.raises(HTTPException) as exc_info:
        routes._ensure_valid_take_id("..")
    assert exc_info.value.status_code == 422

    with pytest.raises(HTTPException):
        routes._ensure_valid_take_id("foo/../../etc/passwd")

    # A normal generated take_id must pass through unchanged.
    assert routes._ensure_valid_take_id("t20260710T150808Z-9e29") == "t20260710T150808Z-9e29"


def test_resolve_rejects_take_id_with_disallowed_characters(client: TestClient) -> None:
    """Unlike a literal '..' segment (collapsed before reaching the server,
    see above), a take_id containing e.g. a dot or space is a perfectly
    routable URL segment that reaches the handler -- and must still be
    rejected by _ensure_valid_take_id before touching the filesystem.
    """

    response = client.post(
        "/api/takes/not.a.valid.take_id/resolve",
        json={"piece_id": "chopin_op11", "movement": 2, "candidate_index": 0},
    )
    assert response.status_code == 422


def test_resolve_rejects_piece_id_path_traversal(client: TestClient) -> None:
    """Unlike take_id (a URL path segment, normalized client-side before a
    raw '..' ever reaches the server), piece_id arrives as a JSON body
    field -- a literal '../../etc' string reaches the handler intact and
    is joined directly into a filesystem path via paths.take_dir. Must be
    rejected before that join happens.
    """

    response = client.post(
        "/api/takes/t20260101T000000Z-0000/resolve",
        json={"piece_id": "../../etc", "movement": 2, "candidate_index": 0},
    )
    assert response.status_code == 422


def test_resolve_non_ambiguous_take_returns_422(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    start_response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start_response.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")  # captured, never aligned by the fake worker

    response = client.post(
        f"/api/takes/{take_id}/resolve",
        json={"piece_id": "chopin_op11", "movement": 2, "candidate_index": 0},
    )
    assert response.status_code == 422


def test_list_takes_skips_aligned_result_read_for_captured_takes(
    client: TestClient, fake_live: FakeLiveControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A captured (not yet aligned) take has no aligned.json on disk yet --
    _take_response must not pay for that disk read when listing takes.
    """

    start_response = client.post(
        "/api/takes/record",
        json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
    )
    take_id = start_response.json()["take_id"]
    client.post(f"/api/takes/{take_id}/stop")  # status stays "captured"

    calls: list[str] = []
    original_get_aligned_result = take_store.get_aligned_result

    def spy(piece_id: str, movement: int, take_id_arg: str):
        calls.append(take_id_arg)
        return original_get_aligned_result(piece_id, movement, take_id_arg)

    monkeypatch.setattr(take_store, "get_aligned_result", spy)

    response = client.get("/api/takes?piece_id=chopin_op11&movement=2")

    assert response.status_code == 200
    assert response.json()["takes"][0]["status"] == "captured"
    assert calls == []
