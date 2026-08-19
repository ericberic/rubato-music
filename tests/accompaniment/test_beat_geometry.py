"""Beat -> pixel inference: the choices that fix real mis-placements."""

from __future__ import annotations

from aimusic.accompaniment.beat_geometry import (
    BeatGlyph,
    infer_measure_beats,
)


def _note(staff: str, onset: float, x: float, *, rest: bool = False, whole: bool = False):
    return BeatGlyph(staff=staff, onset_beats=onset, x=x, is_rest=rest, covers_measure=whole)


def test_a_clean_staff_beats_a_run_heavy_one() -> None:
    """The right hand's run must not decide the beats when a plain staff exists.

    The engraver aligns all staves vertically, so a left hand of quarter notes
    on 0/1/2/3 locates the beats; a right-hand run of 30 notes crammed near beat
    4 does not. The clean staff wins on confidence.
    """

    run = [_note("rh", onset, 0.1 + onset * 0.02) for onset in
           [0.0, 3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9]]
    clean = [_note("lh", float(b), 0.15 + b * 0.2) for b in range(4)]
    inf = infer_measure_beats(run + clean, box_x0=0.0, box_x1=1.0)
    assert inf.staff == "lh"
    assert inf.method == "staff"
    # beats increase and are well separated, not crammed toward one end
    xs = inf.beat_x
    assert xs == tuple(sorted(xs))
    assert min(b - a for a, b in zip(xs, xs[1:])) > 0.05


def test_only_one_anchor_per_beat_so_a_run_does_not_cram_markers() -> None:
    """A dense run between beats 3 and 4 must not pull beat 4 backward."""

    glyphs = [_note("rh", float(b), 0.1 + b * 0.25) for b in range(3)]
    # a 32nd-note run right after beat 3, all near x for beat 4
    glyphs += [_note("rh", 3.0 + k * 0.1, 0.85 + k * 0.005) for k in range(8)]
    inf = infer_measure_beats(glyphs, box_x0=0.0, box_x1=1.0)
    # beat 4 (index 3) lands near the beat-3-onset note, not dragged left by the
    # eight run notes that share its neighbourhood
    assert inf.beat_x[3] > inf.beat_x[2]
    assert inf.beat_x == tuple(sorted(inf.beat_x))


def test_a_sparse_measure_insets_beats_from_the_barlines() -> None:
    """A whole-measure rest must not leave beat 1 flush against the barline."""

    glyphs = [_note("rh", 0.0, 0.5, rest=True, whole=True)]
    inf = infer_measure_beats(glyphs, box_x0=0.0, box_x1=1.0)
    assert inf.method == "even_margins"
    # beat 1 sits inset (~half a beat-space), not at the left barline
    assert 0.08 < inf.beat_x[0] < 0.2
    # and beat 4 is inset from the right barline
    assert 0.8 < inf.beat_x[3] < 0.92


def test_a_new_system_pushes_the_fallback_beats_further_right() -> None:
    """Clef/key/time at a system start take space before beat 1."""

    glyphs = [_note("rh", 0.0, 0.5, rest=True, whole=True)]
    plain = infer_measure_beats(glyphs, box_x0=0.0, box_x1=1.0, new_system=False)
    system = infer_measure_beats(glyphs, box_x0=0.0, box_x1=1.0, new_system=True)
    assert system.beat_x[0] > plain.beat_x[0]


def test_confidence_is_full_when_every_beat_has_a_note() -> None:
    clean = [_note("lh", float(b), 0.15 + b * 0.2) for b in range(4)]
    inf = infer_measure_beats(clean, box_x0=0.0, box_x1=1.0)
    assert inf.confidence >= 0.9


def test_confidence_is_zero_on_the_even_spacing_fallback() -> None:
    glyphs = [_note("rh", 0.0, 0.5, rest=True, whole=True)]
    assert infer_measure_beats(glyphs, box_x0=0.0, box_x1=1.0).confidence == 0.0


def test_every_beat_stays_inside_the_box() -> None:
    glyphs = [_note("rh", float(b), 0.15 + b * 0.25) for b in range(4)]
    inf = infer_measure_beats(glyphs, box_x0=0.4, box_x1=0.6)
    assert all(0.4 - 1e-9 <= x <= 0.6 + 1e-9 for x in inf.beat_x)
