"""The rewrites, exercised through the process boundary that production uses.

Every other equivalence test drives the follower in-process. This one goes
through `ProcessFollower` — real `mp.Queue`s, a spawned child, the gauge block —
because that is the code path a performance actually takes, and it previously
had no coverage at all.
"""

from __future__ import annotations

import time
from pathlib import Path

import mido
import pytest

pytest.importorskip("matchmaker", reason="Install with `uv sync --extra live`")

from aimusic.accompaniment.following import PerformedNote
from aimusic.realtime.follower_process import FollowerSpec, ProcessFollower

pytestmark = pytest.mark.matchmaker

TICKS_PER_BEAT = 480
PITCHES = [60, 62, 64, 65, 67, 69, 71, 72]


@pytest.fixture(scope="module")
def score_midi(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("procfollower") / "score.mid"
    midi = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    for pitch in PITCHES:
        track.append(mido.Message("note_on", note=pitch, velocity=64, time=0))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=TICKS_PER_BEAT))
    midi.save(path)
    return path


def _drain(follower: ProcessFollower, deadline: float = 5.0):
    """Collect the last position, which arrives after `observe` has returned."""

    latest, until = None, time.monotonic() + deadline
    while time.monotonic() < until:
        update = follower.poll_update()
        if update is not None:
            latest = update
            break
        time.sleep(0.01)
    return latest


def test_rewrites_apply_and_track_through_the_process_boundary(score_midi: Path) -> None:
    follower = ProcessFollower(FollowerSpec(score_file=str(score_midi)))
    try:
        for index, pitch in enumerate(PITCHES):
            follower.observe(PerformedNote(perf_time=index * 0.5, pitch=pitch, velocity=64))
            time.sleep(0.02)
        update = _drain(follower)
        assert update is not None, "no position ever surfaced from the child"
        assert update.raw_state["follower"] == "matchmaker"
        # Both rewrites must have been applied inside the child, not skipped.
        assert update.raw_state["transition_banding"]["backward_states"] == 64
        assert update.raw_state["observation_factorization"]["pitch_dimensions"] == 88
    finally:
        follower.close()


def test_gauges_record_real_hmm_work_and_a_sane_backlog(score_midi: Path) -> None:
    """The gauge must see the HMM's own cost, and depth must never go negative."""

    follower = ProcessFollower(FollowerSpec(score_file=str(score_midi)))
    try:
        for index, pitch in enumerate(PITCHES):
            follower.observe(PerformedNote(perf_time=index * 0.5, pitch=pitch, velocity=64))
            time.sleep(0.02)
        _drain(follower)
        sample = follower.gauges.sample().worker("follower")
        assert sample is not None
        assert sample.iterations > 0, "no HMM steps were timed"
        assert sample.last_work_ms > 0.0
        # Enqueue is counted before the put, so depth can never read negative.
        assert sample.queue_depth >= 0
        assert sample.queue_high_watermark >= 0
    finally:
        follower.close()
