from __future__ import annotations

from aimusic.accompaniment.following import (
    FollowerUpdate,
    PerformedNote,
    RelockingFollower,
)


class _ScriptedFollower:
    """Inner follower that emits a scripted (score_beat) per note, so the
    watchdog's stall detection can be exercised deterministically."""

    def __init__(self, beats: list[float]) -> None:
        self._beats = beats
        self._index = 0
        self.repositioned: list[tuple[float, float | None]] = []
        self.closed = False

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        beat = self._beats[min(self._index, len(self._beats) - 1)]
        self._index += 1
        return FollowerUpdate(perf_time=note.perf_time, score_beat=beat, confidence=0.5)

    def reposition_for_entry(self, *, score_beat: float, reference_beat: float | None) -> None:
        self.repositioned.append((score_beat, reference_beat))

    def close(self) -> None:
        self.closed = True


def _feed(follower: RelockingFollower, times: list[float]) -> None:
    for i, t in enumerate(times):
        follower.observe(PerformedNote(perf_time=t, pitch=60 + (i % 5), velocity=70))


def test_stall_under_sustained_playing_triggers_relocalize() -> None:
    relocs: list[int] = []
    # Position barely moves (48.0..48.4) across 30 notes over 6s -> stalled.
    inner = _ScriptedFollower([48.0 + 0.01 * i for i in range(30)])
    focus = RelockingFollower(
        inner,
        relocalize=lambda: relocs.append(1),
        window_seconds=5.0,
        min_updates=20,
        min_advance_beats=4.0,
        cooldown_seconds=3.0,
    )
    _feed(focus, [i * 0.2 for i in range(30)])  # 5 notes/s
    assert focus.relocalizations == 1
    assert relocs == [1]


def test_normal_forward_progress_never_relocalizes() -> None:
    relocs: list[int] = []
    # Advances ~1 beat/note -> far past the min advance every window.
    inner = _ScriptedFollower([48.0 + 1.0 * i for i in range(40)])
    focus = RelockingFollower(
        inner, relocalize=lambda: relocs.append(1), window_seconds=5.0, min_updates=20
    )
    _feed(focus, [i * 0.2 for i in range(40)])
    assert focus.relocalizations == 0
    assert relocs == []


def test_rests_and_holds_do_not_trip_the_watchdog() -> None:
    relocs: list[int] = []
    # A long hold on one beat, but the notes are sparse (1 every 2s): the window
    # never accumulates min_updates, so a stalled position is not enough alone.
    inner = _ScriptedFollower([50.0] * 10)
    focus = RelockingFollower(
        inner, relocalize=lambda: relocs.append(1), window_seconds=5.0, min_updates=20
    )
    _feed(focus, [i * 2.0 for i in range(10)])  # 0.5 notes/s
    assert focus.relocalizations == 0


def test_cooldown_prevents_immediate_re_triggering() -> None:
    relocs: list[int] = []
    inner = _ScriptedFollower([48.0] * 80)  # permanently stalled
    focus = RelockingFollower(
        inner,
        relocalize=lambda: relocs.append(1),
        window_seconds=2.0,
        min_updates=10,
        min_advance_beats=1.0,
        cooldown_seconds=5.0,
    )
    _feed(focus, [i * 0.1 for i in range(80)])  # 8s of dense stalled playing
    # Without a cooldown this would fire many times; the 5s guard bounds it.
    assert focus.relocalizations <= 2


def test_reposition_clears_stall_evidence_and_forwards() -> None:
    relocs: list[int] = []
    inner = _ScriptedFollower([48.0] * 40)
    focus = RelockingFollower(
        inner,
        relocalize=lambda: relocs.append(1),
        window_seconds=5.0,
        min_updates=15,
        min_advance_beats=1.0,
        cooldown_seconds=0.0,
    )
    _feed(focus, [i * 0.2 for i in range(14)])  # just under min_updates
    focus.reposition_for_entry(score_beat=44.0, reference_beat=140.0)
    assert inner.repositioned == [(44.0, 140.0)]
    # Evidence was cleared, so the next few notes cannot instantly relocalize.
    _feed(focus, [10.0 + i * 0.2 for i in range(3)])
    assert focus.relocalizations == 0


def test_close_forwards_to_inner() -> None:
    inner = _ScriptedFollower([1.0])
    focus = RelockingFollower(inner, relocalize=lambda: None)
    focus.close()
    assert inner.closed is True
