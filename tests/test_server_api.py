"""End-to-end tests for the FastAPI MIDI server."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mido import Message, MidiFile, MidiTrack

import aimusic.server.routes as routes
from aimusic.accompaniment.oguri import OguriMovement
from aimusic.accompaniment.score_fusion import load_human_corrections
from aimusic.core import paths
from aimusic.core.event_journal import event_journal_path
from aimusic.server.app import create_app


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    app = create_app()
    return TestClient(app)


def _build_midi_bytes() -> bytes:
    return _build_notes_midi_bytes([(0.0, 60)])


def _build_notes_midi_bytes(notes: list[tuple[float, int]], *, channel: int = 0) -> bytes:
    midi = MidiFile()
    track = MidiTrack()
    midi.tracks.append(track)
    current_ticks = 0
    for start_seconds, pitch in notes:
        start_ticks = int(round(start_seconds * midi.ticks_per_beat * 2))
        track.append(
            Message(
                "note_on",
                note=pitch,
                velocity=80,
                channel=channel,
                time=max(0, start_ticks - current_ticks),
            )
        )
        track.append(Message("note_off", note=pitch, velocity=0, channel=channel, time=120))
        current_ticks = start_ticks + 120
    buffer = io.BytesIO()
    midi.save(file=buffer)
    buffer.seek(0)
    return buffer.read()


def _write_note_file(path: Path, notes: list[tuple[float, int]], *, channel: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_notes_midi_bytes(notes, channel=channel))


def _fake_oguri_movement(tmp_path: Path) -> OguriMovement:
    return OguriMovement(
        movement=2,
        title="Synthetic movement",
        source_url="fixture",
        local_path=tmp_path / "source.mid",
        derived_dir=tmp_path / "derived",
        expected_ticks_per_beat=480,
        expected_track_count=2,
        first_solo_entry_seconds=0.0,
        default_cue_seconds=1.0,
    )


def test_session_lifecycle_via_file_upload(client: TestClient):
    resp = client.post("/api/sessions", json={"session_id": "session_a"})
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "session_a"

    midi_bytes = _build_midi_bytes()
    upload = client.post(
        "/api/sessions/session_a/upload",
        files={"file": ("take.mid", midi_bytes, "audio/midi")},
    )
    assert upload.status_code == 200
    data = upload.json()
    assert data["files"]["solo"] is True
    assert data["files"]["accompaniment"] is False

    # Download the stored file and confirm we persisted exactly what was uploaded.
    download = client.get("/api/sessions/session_a/midi/solo")
    assert download.status_code == 200
    assert download.content == midi_bytes

    raw_path = paths.recording_file_path("session_a")
    assert raw_path.exists()
    assert raw_path.read_bytes() == midi_bytes

    manifest = paths.recording_manifest_path()
    assert manifest.exists()

    listing = client.get("/api/sessions")
    assert listing.status_code == 200
    assert listing.json() == [{"session_id": "session_a", "label": "take.mid"}]


def test_recording_events_produce_midi(client: TestClient):
    client.post("/api/sessions", json={"session_id": "session_b"})
    payload = {
        "tempo_bpm": 90,
        "events": [
            {"type": "note_on", "note": 64, "velocity": 100, "time": 0.0},
            {"type": "note_off", "note": 64, "velocity": 0, "time": 0.5},
        ],
    }
    resp = client.post("/api/sessions/session_b/recordings", json=payload)
    assert resp.status_code == 200
    path = paths.session_dir("session_b") / "solo.mid"
    assert path.exists()
    assert path.stat().st_size > 0

    download = client.get("/api/sessions/session_b/midi/solo")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("audio/midi")


def test_render_offline_session_creates_accompaniment_variant(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    movement = _fake_oguri_movement(tmp_path)
    _write_note_file(movement.solo_reference_path, [(0.0, 60), (1.0, 62), (2.0, 64)])
    _write_note_file(movement.orchestra_accompaniment_path, [(0.5, 48), (1.5, 50)], channel=2)
    monkeypatch.setattr(routes, "oguri_movement", lambda _movement: movement)

    client.post("/api/sessions", json={"session_id": "render_slot"})
    upload = client.post(
        "/api/sessions/render_slot/upload",
        files={
            "file": (
                "take.mid",
                _build_notes_midi_bytes([(0.2, 60), (1.4, 62), (3.0, 64)]),
                "audio/midi",
            )
        },
    )
    assert upload.status_code == 200

    response = client.post(
        "/api/sessions/render_slot/render-offline",
        json={"movement": 2, "reference_track_marker": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["files"]["solo"] is True
    assert body["files"]["accompaniment"] is True
    assert body["metrics"]["pitch_match_count"] == 3
    assert body["metrics"]["output_url"] == "/api/sessions/render_slot/midi/accompaniment"
    assert any(item["variant"] == "accompaniment" for item in body["playback"])
    assert (paths.run_output_dir("render_slot") / "accompaniment.mid").exists()
    assert (paths.run_analysis_dir("render_slot") / "metrics.json").exists()

    download = client.get("/api/sessions/render_slot/midi/accompaniment")
    assert download.status_code == 200
    assert download.content


def test_list_sessions_includes_empty_and_recorded_sessions(client: TestClient):
    empty = client.post("/api/sessions", json={"session_id": "empty_slot"})
    assert empty.status_code == 200

    recorded = client.post("/api/sessions", json={"session_id": "recorded_slot"})
    assert recorded.status_code == 200
    upload = client.post(
        "/api/sessions/recorded_slot/upload",
        files={"file": ("take.mid", _build_midi_bytes(), "audio/midi")},
    )
    assert upload.status_code == 200

    listing = client.get("/api/sessions")
    assert listing.status_code == 200
    assert listing.json() == [
        {"session_id": "empty_slot", "label": "empty_slot"},
        {"session_id": "recorded_slot", "label": "take.mid"},
    ]


def test_unknown_variant_returns_404(client: TestClient):
    client.post("/api/sessions", json={"session_id": "session_c"})
    response = client.get("/api/sessions/session_c/midi/does-not-exist")
    assert response.status_code == 404


def test_get_score_pdf_returns_real_movement_1_bundle(client: TestClient):
    response = client.get("/api/scores/1/pdf?piece_id=chopin_op11")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:4] == b"%PDF"


def test_get_score_pdf_returns_registered_movement_2_reduction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    reduction = tmp_path / "joseffy-reduction.pdf"
    reduction.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(paths, "score_display_pdf_path", lambda _piece, _movement: reduction)

    response = client.get("/api/scores/2/pdf?piece_id=chopin_op11")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_get_score_pdf_unregistered_movement_returns_404(client: TestClient):
    response = client.get("/api/scores/3/pdf?piece_id=chopin_op11")
    assert response.status_code == 404


def test_get_score_pdf_rejects_invalid_piece_id(client: TestClient):
    response = client.get("/api/scores/1/pdf?piece_id=..%2F..%2Fetc")
    assert response.status_code == 422


def test_score_bundle_missing_artifacts_reports_unpulled_dvc_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "source").mkdir(parents=True)
    (bundle_dir / "derived").mkdir()
    # A pulled artifact: both the .dvc pointer and the real file exist.
    (bundle_dir / "source" / "present.mid.dvc").write_text("pointer")
    (bundle_dir / "source" / "present.mid").write_bytes(b"midi bytes")
    # An unpulled artifact: only the .dvc pointer exists.
    (bundle_dir / "derived" / "missing.mid.dvc").write_text("pointer")

    monkeypatch.setattr(paths, "score_bundle_dir", lambda _piece, _movement: bundle_dir)
    missing = paths.score_bundle_missing_artifacts("chopin_op11", 2)

    assert missing == ["derived/missing.mid"]


def test_get_score_bundle_health_returns_missing_artifacts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        paths,
        "score_bundle_missing_artifacts",
        lambda _piece, _movement: ["source/joseffy_reduction.pdf"],
    )
    response = client.get("/api/scores/2/health?piece_id=chopin_op11")
    assert response.status_code == 200
    assert response.json() == {
        "piece_id": "chopin_op11",
        "movement": 2,
        "missing_artifacts": ["source/joseffy_reduction.pdf"],
        # Consistency findings are suppressed while artifacts are unpulled:
        # every check would report absence rather than disagreement.
        "findings": [],
    }


def test_score_bundle_health_reports_remaining_mapping_gaps(client: TestClient):
    """The repaired real bundle reports only its remaining mapping weaknesses.

    The deterministic phantom-chord repair restored the note-level MusicXML to
    all 126 measures, so the old 113-vs-126 OMR disagreement must stay cleared.
    The final unmapped bar and low-evidence measures remain visible instead.

    Integration check over the fully-pulled bundle: the health endpoint
    suppresses every consistency finding while any artifact is unpulled, and the
    DVC remote is a local folder CI cannot reach. Skip there rather than commit
    the multi-MB OMR/PDF sources.
    """

    if paths.score_bundle_missing_artifacts("chopin_op11", 2):
        pytest.skip("Movement 2 DVC artifacts not pulled; health findings are suppressed")
    response = client.get("/api/scores/2/health?piece_id=chopin_op11")
    assert response.status_code == 200
    payload = response.json()
    codes = {finding["code"] for finding in payload["findings"]}
    assert "omr_pass_measure_count_disagreement" not in codes
    unmapped = next(
        finding
        for finding in payload["findings"]
        if finding["code"] == "beat_map_unmapped_measures"
    )
    assert unmapped["measures"] == ["126"]
    weak = next((f for f in payload["findings"] if f["code"] == "beat_map_weak_measures"), None)
    assert weak is not None
    assert "105" in weak["measures"]
    assert "104" not in weak["measures"]


def test_get_score_bundle_health_unregistered_movement_returns_404(client: TestClient):
    response = client.get("/api/scores/3/health?piece_id=chopin_op11")
    assert response.status_code == 404


def test_get_score_bundle_health_rejects_invalid_piece_id(client: TestClient):
    response = client.get("/api/scores/1/health?piece_id=..%2F..%2Fetc")
    assert response.status_code == 422


def test_get_measure_boxes_returns_real_movement_1_geometry(client: TestClient):
    response = client.get("/api/scores/1/measure_boxes?piece_id=chopin_op11")
    assert response.status_code == 200
    body = response.json()
    assert body["page_count"] == 98
    assert body["level"] == 1
    assert body["method"] == "musicxml-system-breaks+even-vertical-split"
    assert len(body["pages"]) == 98
    first_system = body["pages"][0]["systems"][0]
    assert 0.0 <= first_system["y0"] < first_system["y1"] <= 1.0
    assert first_system["first_measure"] <= first_system["last_measure"]


def test_get_measure_boxes_returns_machine_movement_2_geometry(client: TestClient):
    response = client.get("/api/scores/2/measure_boxes?piece_id=chopin_op11")
    assert response.status_code == 200
    body = response.json()
    assert body["page_count"] == 15
    assert body["review_state"] == "machine"
    assert (
        sum(len(system["measures"]) for page in body["pages"] for system in page["systems"]) == 126
    )


def test_get_measure_boxes_presents_canonical_display_map_labels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    display_map = tmp_path / "display_map.json"
    display_map.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mapping_id": "test-display-map",
                "source_id": "test-score",
                "timeline_id": "test-timeline",
                "kind": "display",
                "coordinate_system": "normalized_pdf_page",
                "page_count": 1,
                "boxes": [
                    {
                        "measure_index": 0,
                        "measure_label": "7",
                        "score_start_tick": 0,
                        "score_end_tick": 3840,
                        "confidence": 0.8,
                        "page": 1,
                        "system": 1,
                        "x0": 0.1,
                        "x1": 0.4,
                        "y0": 0.2,
                        "y1": 0.5,
                    },
                    {
                        "measure_index": 1,
                        "measure_label": "8",
                        "score_start_tick": 3840,
                        "score_end_tick": 7680,
                        "confidence": 0.8,
                        "page": 1,
                        "system": 1,
                        "x0": 0.4,
                        "x1": 0.9,
                        "y0": 0.2,
                        "y1": 0.5,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "score_display_geometry_path", lambda *_args: display_map)
    display_pdf = tmp_path / "performer-score.pdf"
    monkeypatch.setattr(paths, "score_display_pdf_path", lambda *_args: display_pdf)
    beat_map = tmp_path / "beat-map.json"
    beat_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mapping_id": "test-beat-map",
                "timeline_id": "test-timeline",
                "source_id": "test-midi",
                "source_midi_ppq": 240,
                "canonical_ppq": 960,
                "review_state": "machine",
                "anchors": [
                    {
                        "score_tick": 0,
                        "measure_index": 0,
                        "measure_label": "7",
                        "beat_in_measure": 0.0,
                        "source_midi_tick": 0,
                        "source_seconds": 0.0,
                        "confidence": 0.8,
                        "pdf_page": 1,
                        "pdf_system": 1,
                        "pdf_x": 0.1,
                    },
                    {
                        "score_tick": 960,
                        "measure_index": 0,
                        "measure_label": "7",
                        "beat_in_measure": 1.0,
                        "source_midi_tick": 240,
                        "source_seconds": 0.5,
                        "confidence": 0.8,
                        "pdf_page": 1,
                        "pdf_system": 1,
                        "pdf_x": 0.2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "score_performance_beat_map_path", lambda *_args: beat_map)

    response = client.get("/api/scores/2/measure_boxes?piece_id=chopin_op11")

    assert response.status_code == 200
    body = response.json()
    assert body["pdf"] == "performer-score.pdf"
    assert body["level"] == 2
    assert body["method"] == "canonical-display-map"
    system = body["pages"][0]["systems"][0]
    assert system["first_measure"] == 7
    assert system["last_measure"] == 8
    assert [
        {key: measure[key] for key in ("measure", "x0", "x1")} for measure in system["measures"]
    ] == [
        {"measure": 7, "x0": 0.1, "x1": 0.4},
        {"measure": 8, "x0": 0.4, "x1": 0.9},
    ]
    assert len(system["measures"][0]["beats"]) == 2
    # Measure 8 has no beat-map anchors, so the API fills it with evenly-spaced
    # markers (the noteless closing-tutti case) rather than leaving it blank:
    # four beats flagged unanchored / zero-confidence, no locating staff, each
    # inside the box and left-to-right.
    filler = system["measures"][1]["beats"]
    assert [beat["beat_in_measure"] for beat in filler] == [0.0, 1.0, 2.0, 3.0]
    assert all(beat["confidence"] == 0.0 and beat["anchored"] is False for beat in filler)
    assert all(0.4 <= beat["x"] <= 0.9 for beat in filler)
    assert [beat["x"] for beat in filler] == sorted(beat["x"] for beat in filler)
    assert system["measures"][1]["beat_staff"] is None


def test_score_alignment_correction_persists_sparse_anchor_and_journal(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/scores/2/alignment-corrections?piece_id=chopin_op11",
        json={
            "source_seconds": 96.3375,
            "measure": 18,
            "beat_in_measure": 0,
            "note": "audible downbeat",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["measure"] == 18
    assert body["measure_label"] == "18"
    assert body["beat_in_measure"] == 0
    assert body["mapping_id"].startswith("audiveris-oguri-symbolic-path-v2+human-")
    corrections = load_human_corrections(paths.score_alignment_corrections_path("chopin_op11", 2))
    assert corrections is not None
    assert corrections.corrections[0].correction_id == body["correction_id"]
    assert "score:alignment_corrected" in event_journal_path().read_text(encoding="utf-8")


def test_score_alignment_correction_reports_missing_beat_map(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        paths,
        "score_performance_beat_map_path",
        lambda *_args: tmp_path / "missing-performance-beat-map.json",
    )

    response = client.post(
        "/api/scores/2/alignment-corrections?piece_id=chopin_op11",
        json={"source_seconds": 1.0, "measure": 1, "beat_in_measure": 0},
    )

    assert response.status_code == 503
    assert "Performance beat map is not available" in response.json()["detail"]


def test_invalid_score_alignment_correction_is_rejected_before_write(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an invalid correction must not reach durable storage")

    monkeypatch.setattr(routes, "write_human_corrections", fail_write)

    response = client.post(
        "/api/scores/2/alignment-corrections?piece_id=chopin_op11",
        json={
            "source_seconds": 0,
            "measure": 18,
            "beat_in_measure": 0,
            "note": "intentionally non-monotonic",
        },
    )

    assert response.status_code == 422
    assert "non-monotonic" in response.json()["detail"]


def test_shutdown_without_server_handle_returns_503(client: TestClient):
    """A server started without `main()` (e.g. most test clients) has no
    `uvicorn.Server` on `app.state` -- the route must say so, not crash."""

    response = client.post("/api/server/shutdown")
    assert response.status_code == 503


def test_shutdown_with_server_handle_flips_should_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    app = create_app()

    class FakeUvicornServer:
        should_exit = False

    app.state.uvicorn_server = FakeUvicornServer()

    # `with` keeps the app's event loop alive across the request so the
    # shutdown route's deferred asyncio.create_task actually gets to run --
    # a bare `TestClient(app)` tears the loop down as soon as each request
    # returns, before the 0.3s delay elapses.
    with TestClient(app) as with_context_client:
        response = with_context_client.post("/api/server/shutdown")
        assert response.status_code == 200
        assert response.json() == {"message": "Shutting down"}
        time.sleep(0.6)
        assert app.state.uvicorn_server.should_exit is True
