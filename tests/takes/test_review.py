from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mido import Message, MidiFile, MidiTrack

from aimusic.accompaniment.offline_alignment import extract_note_events
from aimusic.accompaniment.oguri import OguriMovement
from aimusic.accompaniment.rehearsal_position import score_projection
from aimusic.core import paths
from aimusic.core.event_journal import event_journal_path
from aimusic.core.events import EventBroadcaster
from aimusic.core.time import utc_now
from aimusic.server import routes
from aimusic.server.app import create_app
from aimusic.server.schemas import (
    HardwareJobPhase,
    HardwareStatusEvent,
    LiveStatusResponse,
)
from aimusic.takes import review, store
from aimusic.takes.lifecycle import JobState
from aimusic.takes.models import AlignedResult, TimingMapPoint
from tests.fake_hardware import FakeLiveControl


@pytest.fixture()
def review_take(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, OguriMovement]:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    identity_projection = replace(score_projection("chopin_op11", 2), anchors=())
    monkeypatch.setattr(routes, "score_projection", lambda _piece, _movement: identity_projection)
    movement = OguriMovement(
        movement=2,
        title="Review fixture",
        source_url="fixture",
        local_path=tmp_path / "source.mid",
        derived_dir=tmp_path / "derived",
        expected_ticks_per_beat=480,
        expected_track_count=2,
        first_solo_entry_seconds=0.0,
        default_cue_seconds=1.0,
    )
    _write_notes(movement.solo_reference_path, [(0.0, 60), (1.0, 62), (2.0, 64)])
    _write_notes(
        movement.orchestra_accompaniment_path,
        [(0.25, 40), (0.75, 41), (1.5, 43), (2.5, 45)],
        channel=2,
    )
    monkeypatch.setattr(review, "oguri_movement", lambda _movement: movement)

    take_id = "t-review"
    take_source = tmp_path / "take.mid"
    _write_notes(take_source, [(0.5, 60), (1.5, 62), (3.0, 64)])
    store.save_take("chopin_op11", 2, take_source, take_id=take_id)
    store.write_aligned_result(
        "chopin_op11",
        2,
        take_id,
        AlignedResult(
            take_id=take_id,
            aligner="fixture",
            score_start_beat=1.0,
            score_end_beat=4.0,
            match_rate=1.0,
            ambiguous=False,
            matched_notes=3,
            extra_notes=0,
            missing_notes=0,
            timing_map=(
                TimingMapPoint(score_beat=1.0, take_seconds=0.5),
                TimingMapPoint(score_beat=2.0, take_seconds=1.5),
                TimingMapPoint(score_beat=4.0, take_seconds=3.0),
            ),
            candidates=(),
            cell_samples=(),
            edge_trim_beats=(0.0, 0.0),
        ),
    )
    store.update_take_status("chopin_op11", 2, take_id, store.ALIGNED)
    return take_id, movement


def _write_notes(path: Path, notes: list[tuple[float, int]], *, channel: int = 0) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    last_tick = 0
    for seconds, pitch in notes:
        tick = round(seconds * 960)
        track.append(
            Message(
                "note_on",
                note=pitch,
                velocity=80,
                channel=channel,
                time=max(0, tick - last_tick),
            )
        )
        track.append(Message("note_off", note=pitch, velocity=0, channel=channel, time=60))
        last_tick = tick + 60
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def test_take_review_crops_to_aligned_span_and_combines_solo(review_take) -> None:
    take_id, _movement = review_take

    solo, accompaniment, ensemble = review.render_take_review("chopin_op11", 2, take_id)

    assert solo.is_file()
    assert accompaniment.is_file()
    assert ensemble.is_file()
    accompaniment_pitches = [note.pitch for note in extract_note_events(accompaniment)]
    assert accompaniment_pitches == [41, 43]
    ensemble_pitches = [note.pitch for note in extract_note_events(ensemble)]
    assert {60, 62, 64}.issubset(ensemble_pitches)
    assert {41, 43}.issubset(ensemble_pitches)
    assert 40 not in ensemble_pitches
    assert 45 not in ensemble_pitches
    solo_notes = extract_note_events(solo)
    assert solo_notes[0].time_seconds == pytest.approx(0.0, abs=0.002)


def test_review_input_revision_hashes_unchanged_files_once(
    review_take, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_id, _movement = review_take
    review._cached_review_input_revision.cache_clear()
    original_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def counted_read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    first = review.review_input_revision("chopin_op11", 2, take_id)
    second = review.review_input_revision("chopin_op11", 2, take_id)

    assert first == second
    assert len(reads) == 2

    aligned_path = paths.take_dir("chopin_op11", 2, take_id, create=False) / "aligned.v2.json"
    aligned_path.write_text(aligned_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = review.review_input_revision("chopin_op11", 2, take_id)

    assert changed != first
    assert len(reads) == 4


def test_aligned_take_response_exposes_provisional_display_span(review_take) -> None:
    take_id, _movement = review_take

    response = routes._take_response(store.get_take("chopin_op11", 2, take_id))

    assert response.score_span is not None
    assert response.score_span.canonical_position is True
    assert response.score_span.mapping_review_state == "machine"
    assert response.score_span.start.score_tick < response.score_span.end.score_tick
    assert response.score_span.start.measure_index <= response.score_span.end.measure_index


def test_review_transport_uses_alignment_timing_anchors(review_take) -> None:
    take_id, _movement = review_take

    transport = routes._review_score_transport("chopin_op11", 2, take_id)

    assert transport.canonical_position is True
    assert transport.mapping_review_state == "machine"
    assert transport.anchors[0].elapsed_seconds == 0.0
    assert transport.anchors[-1].elapsed_seconds == pytest.approx(2.5625)
    assert transport.anchors[0].position.score_tick < transport.anchors[-1].position.score_tick
    assert transport.anchors[0].anchor_kind == "matched_onset"
    matched = transport.anchors[0]
    assert matched.anchor_kind == "matched_onset"
    assert matched.played_pitches == [60]
    assert matched.source_tick is not None
    assert transport.anchors[-1].anchor_kind == "stop"


def test_take_list_response_does_not_parse_midi_for_score_transport(
    review_take, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_id, _movement = review_take

    def unexpected_transport(*_args: object) -> None:
        raise AssertionError("take serialization must not build score transport")

    monkeypatch.setattr(routes, "_review_score_transport", unexpected_transport)

    response = routes._take_response(store.get_take("chopin_op11", 2, take_id))

    assert response.score_transport is None


def test_score_transport_detail_endpoint_supplies_browser_preview(review_take) -> None:
    take_id, _movement = review_take

    with TestClient(create_app()) as client:
        response = client.get(
            f"/api/takes/{take_id}/score-transport",
            params={"piece_id": "chopin_op11", "movement": 2},
        )

    assert response.status_code == 200
    anchors = response.json()["anchors"]
    assert anchors[0]["played_pitches"] == [60]
    assert anchors[-1]["anchor_kind"] == "stop"


def test_review_landmark_diagnostics_are_written_to_durable_event_journal(review_take) -> None:
    take_id, _movement = review_take
    transport = routes._review_score_transport("chopin_op11", 2, take_id)

    EventBroadcaster().publish(
        HardwareStatusEvent(
            type="hardware:status",
            status=LiveStatusResponse(
                phase=HardwareJobPhase.RUNNING,
                kind="playback",
                running=True,
                message="Playing review fixture",
                started_at=utc_now(),
                session_id=take_id,
                score_transport=transport,
            ),
        )
    )

    record = json.loads(event_journal_path().read_text(encoding="utf-8").splitlines()[-1])
    anchors = record["payload"]["status"]["score_transport"]["anchors"]
    matched = next(anchor for anchor in anchors if anchor["anchor_kind"] == "matched_onset")
    assert matched["source_tick"] is not None
    assert matched["played_pitches"] == [60]
    assert anchors[-1]["anchor_kind"] == "stop"


def test_review_worker_is_durable_and_idempotent(review_take) -> None:
    take_id, _movement = review_take
    worker = review.ReviewWorker()

    queued = worker.enqueue("chopin_op11", 2, take_id)
    deadline = time.monotonic() + 5
    current = store.get_job("chopin_op11", 2, queued.job_id)
    while current.state not in {JobState.SUCCEEDED, JobState.FAILED}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        current = store.get_job("chopin_op11", 2, queued.job_id)

    assert current.state == JobState.SUCCEEDED
    status = review.review_status("chopin_op11", 2, take_id)
    assert status is not None
    assert status.state == "ready"
    assert status.midi_url and status.midi_url.endswith(
        "/review/midi?piece_id=chopin_op11&movement=2"
    )
    repeated = worker.enqueue("chopin_op11", 2, take_id)
    assert repeated.job_id == queued.job_id


def test_review_worker_recovery_accepts_fresh_take_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "fresh-data"))

    assert review.ReviewWorker().recover_all() == 0


def test_review_uses_take_store_without_processed_session_copy(review_take) -> None:
    take_id, _movement = review_take
    assert not paths.processed_recording_dir(take_id).exists()
    review.render_take_review("chopin_op11", 2, take_id)
    assert review.review_midi_path(take_id).is_file()


def test_take_review_api_prepares_downloads_and_plays_on_yamaha(
    review_take, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_id, _movement = review_take
    worker = review.ReviewWorker()
    fake_live = FakeLiveControl()
    monkeypatch.setattr(routes, "review_worker", worker)
    monkeypatch.setattr(routes, "live_control", fake_live)

    with TestClient(create_app()) as client:
        response = client.post(
            f"/api/takes/{take_id}/review",
            json={"piece_id": "chopin_op11", "movement": 2},
        )
        assert response.status_code == 202

        deadline = time.monotonic() + 5
        while True:
            status = client.get(
                f"/api/takes/{take_id}/review",
                params={"piece_id": "chopin_op11", "movement": 2},
            )
            assert status.status_code == 200
            if status.json()["state"] in {"ready", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert status.json()["state"] == "ready"

        download = client.get(
            f"/api/takes/{take_id}/review/midi",
            params={"piece_id": "chopin_op11", "movement": 2},
        )
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("audio/midi")

        full_transport = routes._review_score_transport("chopin_op11", 2, take_id)
        selected_anchor = next(
            anchor for anchor in full_transport.anchors if anchor.elapsed_seconds == 1.0
        )
        playback = client.post(
            f"/api/takes/{take_id}/review/play",
            params={"piece_id": "chopin_op11", "movement": 2},
            json={
                "output_name": "Fake Synth",
                "volume": 0.75,
                "variant": "solo",
                "start_score_beat": selected_anchor.position.score_beat,
            },
        )
        assert playback.status_code == 200
        assert fake_live.play_file_args is not None
        assert fake_live.play_file_args["source_path"] == review.review_midi_path(take_id, "solo")
        assert fake_live.play_file_args["start_seconds"] == pytest.approx(1.0)
        assert playback.json()["score_transport"]["anchors"][0]["elapsed_seconds"] == 0.0
        assert playback.json()["score_transport"]["anchors"][0]["position"][
            "score_beat"
        ] == pytest.approx(selected_anchor.position.score_beat)

        # A playback adapter without score-transport support should not pay to
        # parse the take MIDI when no positioned start was requested.
        original_transport = routes._review_score_transport
        monkeypatch.setattr(fake_live, "attach_score_transport", None)

        def unexpected_transport(*_args: object) -> None:
            raise AssertionError("unpositioned playback must build transport lazily")

        monkeypatch.setattr(routes, "_review_score_transport", unexpected_transport)
        unpositioned = client.post(
            f"/api/takes/{take_id}/review/play",
            params={"piece_id": "chopin_op11", "movement": 2},
            json={"output_name": "Fake Synth", "volume": 0.75, "variant": "solo"},
        )
        assert unpositioned.status_code == 200
        del fake_live.attach_score_transport
        monkeypatch.setattr(routes, "_review_score_transport", original_transport)

        solo_download = client.get(
            f"/api/takes/{take_id}/review/midi",
            params={"piece_id": "chopin_op11", "movement": 2, "variant": "solo"},
        )
        assert solo_download.status_code == 200
        assert "take-only.mid" in solo_download.headers["content-disposition"]

        def missing_alignment(*_args: object) -> None:
            raise ValueError("take has no authoritative alignment")

        monkeypatch.setattr(routes, "_review_score_transport", missing_alignment)
        fake_live.play_file_args = None
        rejected = client.post(
            f"/api/takes/{take_id}/review/play",
            params={"piece_id": "chopin_op11", "movement": 2},
            json={"output_name": "Fake Synth", "volume": 0.75},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "take has no authoritative alignment"
        assert fake_live.play_file_args is None
