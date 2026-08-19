from __future__ import annotations

import mido
import numpy as np
import pytest

from aimusic.audio.orchestra_live_audition import _stream_part


class FakePlugin:
    def __init__(self) -> None:
        self.blocks: list[list[tuple[bytes, float]]] = []

    def process(self, messages, **_kwargs):
        self.blocks.append(messages)
        return np.ones((2, 10), dtype=np.float32)


class FakeStream:
    def __init__(self) -> None:
        self.blocks: list[np.ndarray] = []

    def write(self, block, _sample_rate) -> None:
        self.blocks.append(block.copy())


def test_stream_part_schedules_midi_offsets_and_preserves_plugin_state() -> None:
    plugin = FakePlugin()
    stream = FakeStream()
    events = [
        (0.005, mido.Message("note_on", note=69, velocity=80)),
        (0.015, mido.Message("note_off", note=69)),
    ]

    _stream_part(
        plugin,
        stream,
        events,
        duration_seconds=0.03,
        sample_rate=1_000,
        block_size=10,
        mix_gain=0.25,
    )

    assert len(plugin.blocks) == 3
    assert len(stream.blocks) == 3
    assert plugin.blocks[0][0][1] == pytest.approx(0.005)
    assert plugin.blocks[1][0][1] == pytest.approx(0.005)
    assert np.allclose(stream.blocks[0], 0.25)
