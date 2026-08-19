from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from aimusic.accompaniment.following import PerformedNote
from aimusic.accompaniment.matchmaker_follower import MatchmakerStreamFollower


class FakePitchProcessor:
    def __init__(self, *, piano_range: bool) -> None:
        self.piano_range = piano_range


class FakeBytesMidiStream:
    def __init__(self, *, processor, data_queue, polling_period) -> None:
        self.processor = processor
        self.data_queue = data_queue
        self.polling_period = polling_period


class FakeMatchmaker:
    def __init__(self, score_file, **kwargs) -> None:
        self.score_file = score_file
        self.stream = kwargs["stream"]

    def run(self, *, verbose: bool):
        assert not verbose
        data = self.stream.data_queue.get(timeout=1)
        if data is not None:
            yield 7.25


def test_stream_adapter_bridges_observe_without_claiming_real_confidence(monkeypatch) -> None:
    package = types.ModuleType("matchmaker")
    package.Matchmaker = FakeMatchmaker
    features = types.ModuleType("matchmaker.features.midi")
    features.PitchProcessor = FakePitchProcessor
    midi_io = types.ModuleType("matchmaker.io.midi")
    midi_io.BytesMidiStream = FakeBytesMidiStream
    monkeypatch.setitem(sys.modules, "matchmaker", package)
    monkeypatch.setitem(sys.modules, "matchmaker.features.midi", features)
    monkeypatch.setitem(sys.modules, "matchmaker.io.midi", midi_io)

    follower = MatchmakerStreamFollower(
        "score.mxl",
        max_wait_seconds=0.2,
        provisional_confidence=0.4,
        minimum_lock_updates=1,
    )
    update = follower.observe(PerformedNote(perf_time=1.5, pitch=64, velocity=80))

    assert update is not None
    assert update.score_beat == 7.25
    assert update.confidence == 0.4
    assert update.raw_state["confidence_kind"] == "uncalibrated_policy_value"
    assert update.raw_state["locked"] is True
    assert update.raw_state["lock_kind"] == "observation_count_heuristic"
    follower.close()


def test_stream_adapter_times_out_instead_of_blocking(monkeypatch) -> None:
    class SilentMatchmaker(FakeMatchmaker):
        def run(self, *, verbose: bool):
            self.stream.data_queue.get(timeout=1)
            if False:
                yield 0.0

    package = types.ModuleType("matchmaker")
    package.Matchmaker = SilentMatchmaker
    features = types.ModuleType("matchmaker.features.midi")
    features.PitchProcessor = FakePitchProcessor
    midi_io = types.ModuleType("matchmaker.io.midi")
    midi_io.BytesMidiStream = FakeBytesMidiStream
    monkeypatch.setitem(sys.modules, "matchmaker", package)
    monkeypatch.setitem(sys.modules, "matchmaker.features.midi", features)
    monkeypatch.setitem(sys.modules, "matchmaker.io.midi", midi_io)

    follower = MatchmakerStreamFollower("score.mxl", max_wait_seconds=0.001)
    assert follower.observe(PerformedNote(perf_time=0, pitch=60, velocity=64)) is None
    follower.close()


def test_stream_adapter_centers_pthmm_prior_and_locks_on_second_onset(monkeypatch) -> None:
    class WarmMatchmaker(FakeMatchmaker):
        instance = None

        def __init__(self, score_file, **kwargs) -> None:
            super().__init__(score_file, **kwargs)
            self.score_follower = SimpleNamespace(
                state_space=np.arange(0.0, 12.5, 0.5),
                transition_model=SimpleNamespace(init_probabilities=None),
                forward_variable=np.ones(25),
            )
            WarmMatchmaker.instance = self

        def run(self, *, verbose: bool):
            assert not verbose
            for position in (7.0, 7.5):
                data = self.stream.data_queue.get(timeout=1)
                if data is None:
                    return
                yield position

    package = types.ModuleType("matchmaker")
    package.Matchmaker = WarmMatchmaker
    features = types.ModuleType("matchmaker.features.midi")
    features.PitchProcessor = FakePitchProcessor
    midi_io = types.ModuleType("matchmaker.io.midi")
    midi_io.BytesMidiStream = FakeBytesMidiStream
    monkeypatch.setitem(sys.modules, "matchmaker", package)
    monkeypatch.setitem(sys.modules, "matchmaker.features.midi", features)
    monkeypatch.setitem(sys.modules, "matchmaker.io.midi", midi_io)

    follower = MatchmakerStreamFollower(
        "score.mxl",
        initial_reference_beat=7.1,
        minimum_lock_updates=2,
        max_wait_seconds=0.2,
    )
    assert WarmMatchmaker.instance is not None
    hmm = WarmMatchmaker.instance.score_follower
    assert hmm.state_space[int(np.argmax(hmm.transition_model.init_probabilities))] == 7.0
    assert hmm.transition_model.init_probabilities.sum() == 1.0
    assert hmm.forward_variable is None

    follower.reposition_for_entry(score_beat=4.0, reference_beat=9.1)
    assert hmm.state_space[int(np.argmax(hmm.transition_model.init_probabilities))] == 9.0
    assert hmm.transition_model.init_probabilities.sum() == 1.0
    assert hmm.forward_variable is None

    # relocalize spreads the prior broadly but FORWARD of the current estimate
    # (9.0, from the reposition above) so recovery leaps ahead to catch up and
    # never jumps backward to an earlier restatement of the theme.
    follower.relocalize()
    probs = hmm.transition_model.init_probabilities
    assert len(probs) == len(hmm.state_space)
    assert probs.sum() == pytest.approx(1.0)
    assert probs[hmm.state_space < 6.5].sum() == 0.0  # opening excluded
    assert probs[hmm.state_space >= 9.0].min() > 0.0  # current and ahead included
    nonzero = probs[probs > 0]
    assert np.allclose(nonzero, nonzero[0])  # uniform across the forward band
    assert hmm.forward_variable is None

    first = follower.observe(PerformedNote(perf_time=1.0, pitch=60, velocity=70))
    second = follower.observe(PerformedNote(perf_time=1.5, pitch=62, velocity=70))

    assert first is not None and first.confidence == 0.0
    assert second is not None and second.confidence == 0.5
    assert second.raw_state["stable_update_count"] == 2
    assert second.raw_state["warm_start_reference_beat"] == 9.0
    follower.close()
