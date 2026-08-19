from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aimusic.accompaniment.runtime_io import MemoryTraceSink
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.audio.live_config import load_live_audio_config
from aimusic.audio.live_vst import LiveVstZoneWorker
from aimusic.mixing.models import MixProgram, MixRoute
from aimusic.mixing.policy import compile_mix_policy
from aimusic.mixing.zones import room_zones


@pytest.mark.hardware
def test_configured_live_vst_zone_streams_incremental_note_and_telemetry() -> None:
    """Opt-in bench smoke: this produces a short BBCSO note on the configured zone."""

    config_path = os.environ.get("RUBATO_HARDWARE_AUDIO_CONFIG")
    if not config_path:
        pytest.skip("set RUBATO_HARDWARE_AUDIO_CONFIG to the live-audio-zones JSON")
    audio_config = load_live_audio_config(Path(config_path))
    zone = next((item for item in audio_config.zones if item.zone_id == "room_center"), None)
    if zone is None:
        pytest.skip("room_center is not configured")
    program = MixProgram(
        program_id="hardware-smoke",
        name="Hardware smoke",
        piece_id="chopin_op11",
        movement=2,
        score_bundle_id="hardware-smoke",
        score_bundle_revision="1",
        timeline_digest="hardware-smoke",
        updated_at=datetime.now(timezone.utc),
        default_routes=(
            MixRoute(zone_id="room_center", stem_ids=("orchestra",), level=40),
        ),
    )
    policy = compile_mix_policy(program, room_zones(audio_config))
    trace = MemoryTraceSink()
    worker = LiveVstZoneWorker(zone, policy, trace_sink=trace)
    try:
        now = time.monotonic()
        worker.update_transport(acoustic_time=now, score_tick=0)
        worker.send(
            ScheduledAccompanimentEvent(
                event=ScoreEvent(
                    event_id="hardware-smoke-note",
                    measure=1,
                    beat=0,
                    part_id="hardware-smoke-unmapped",
                    role="accompaniment",
                    duration_beats=0.5,
                    pitch=60,
                    velocity=64,
                ),
                perf_time=now + 0.5,
                section_mode=AccompanimentMode.LEAD,
                tempo_bpm=60,
                duration_seconds=0.5,
                committed_at=now,
            ),
            sent_at=now,
        )
        deadline = now + 2.0
        while time.monotonic() < deadline:
            worker.update_transport(
                acoustic_time=time.monotonic(),
                score_tick=round((time.monotonic() - now) * 960),
            )
            time.sleep(0.01)
    finally:
        worker.close()

    windows = [
        row
        for row in trace.records
        if getattr(row, "type", None) == "audio_worker"
        and getattr(row, "action", None) == "window"
        and getattr(row, "instrument_id", None) is None
    ]
    assert windows
    assert windows[-1].render_ms_p95 is not None
    assert windows[-1].underruns == 0
