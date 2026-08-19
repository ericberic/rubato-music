from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import mido
import pytest

from aimusic.accompaniment.runtime_contracts import MixStateTrace, TelemetryLevel
from aimusic.accompaniment.runtime_io import MemoryTraceSink
from aimusic.mixing.models import (
    EnvelopePoint,
    Gesture,
    MixProgram,
    MixRegion,
    MixRoute,
)
from aimusic.mixing.policy import compile_mix_policy
from aimusic.realtime.harness import (
    CapturingMidiPort,
    MixProbeRouter,
    score_run,
    virtual_mix_zones,
)


def _mix_policy():
    program = MixProgram(
        program_id="replay-mix",
        name="Replay mix",
        piece_id="piece",
        movement=1,
        score_bundle_id="bundle",
        score_bundle_revision="1",
        timeline_digest="digest",
        updated_at=datetime.now(timezone.utc),
        default_routes=(
            MixRoute(zone_id="yamaha_anchor", stem_ids=("orchestra",), level=25),
            MixRoute(zone_id="room_center", stem_ids=("orchestra",), level=10),
        ),
        regions=(
            MixRegion(
                region_id="swell",
                start_tick=0,
                end_tick=961,
                gesture=Gesture.SWELL,
                routes=(
                    MixRoute(
                        zone_id="room_center",
                        stem_ids=("orchestra",),
                        envelope=(
                            EnvelopePoint(score_tick=0, level=10),
                            EnvelopePoint(score_tick=960, level=90),
                        ),
                    ),
                ),
            ),
        ),
    )
    return compile_mix_policy(program, virtual_mix_zones())


def test_capturing_midi_port_is_an_in_process_software_sink() -> None:
    port = CapturingMidiPort()

    port.send(mido.Message("note_on", note=60, velocity=80))
    port.send(mido.Message("control_change", control=7, value=32))

    assert [(message.type, message.dict()) for message in port.messages] == [
        ("note_on", mido.Message("note_on", note=60, velocity=80).dict()),
        ("control_change", mido.Message("control_change", control=7, value=32).dict()),
    ]


def test_mix_probe_samples_a_swell_and_scorecard_verifies_unit_signal(
    tmp_path: Path,
) -> None:
    sink = MemoryTraceSink()
    probe = MixProbeRouter(
        _mix_policy(),
        sink,
        telemetry_level=TelemetryLevel.TRACE,
        sample_interval_seconds=0.01,
    )

    probe.set_master_volume(0.8, sent_at=0.0)
    probe.apply_mix_automation(0, sent_at=0.0)
    probe.apply_mix_automation(480, sent_at=0.1)
    probe.apply_mix_automation(960, sent_at=0.2)

    rows = [record for record in sink.records if isinstance(record, MixStateTrace)]
    assert [row.score_tick_start for row in rows] == [0, 480, 960]
    assert [row.route_gain_end for row in rows] == pytest.approx([0.1, 0.5, 0.9])
    assert [row.output_rms for row in rows] == pytest.approx([0.08, 0.4, 0.72])

    trace = tmp_path / "runtime.jsonl"
    trace.write_text(
        "\n".join(record.model_dump_json() for record in sink.records) + "\n",
        encoding="utf-8",
    )
    card = score_run(trace, require_mix=True, require_mix_change=True)

    assert card.passed, card.report()
    assert any(check.name == "mix.observed_gain_change" for check in card.checks)


def test_mix_probe_can_be_disabled_for_a_time_critical_run() -> None:
    sink = MemoryTraceSink()
    probe = MixProbeRouter(
        _mix_policy(),
        sink,
        telemetry_level=TelemetryLevel.OFF,
    )

    probe.apply_mix_automation(480, sent_at=1.0)

    assert sink.records == []
