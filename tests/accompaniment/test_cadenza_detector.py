"""Cadenza handoff detection, pinned against the failures that motivated it."""

from __future__ import annotations

import numpy as np
import pytest

from aimusic.accompaniment.cadenza_detector import (
    CHROMA_BINS,
    CadenzaDetector,
    CadenzaModel,
    CadenzaState,
    DecayingChroma,
)

TAUS = (0.25, 1.0, 4.0)
NF = len(TAUS) * CHROMA_BINS


def _model(**kw) -> CadenzaModel:
    """A hand-built model that fires on C-natural content and nothing else."""

    coef = np.zeros(NF)
    coef[0] = 12.0  # shortest tau, pitch class 0
    params = dict(
        taus=TAUS,
        coef=coef,
        intercept=-6.0,
        mean=np.zeros(NF),
        scale=np.ones(NF),
        region_start_beat=400.0,
        handoff_beat=410.0,
        hand_back_beat=411.0,
        label_lead_beats=1.0,
    )
    params.update(kw)
    return CadenzaModel(**params)


# ------------------------------------------------------------------- features


def test_decaying_chroma_is_tempo_invariant() -> None:
    """The property the whole design rests on: ratios do not care about speed.

    Clustering was abandoned because no gap threshold segments this passage
    stably under tempo change. These features need no segmentation, so a warped
    time axis leaves them identical.
    """

    fast = DecayingChroma(TAUS)
    slow = DecayingChroma(TAUS)
    for i, pitch in enumerate((60, 64, 67, 72)):
        a = fast.observe(pitch, i * 0.5)
        b = slow.observe(pitch, i * 1.0)
    # same notes, half the tempo -- the distribution over pitch classes is the
    # same shape even though the decay constants differ in absolute terms
    assert a.sum() == pytest.approx(b.sum())
    assert np.argmax(a[:CHROMA_BINS]) == np.argmax(b[:CHROMA_BINS])


def test_chroma_is_octave_invariant() -> None:
    low, high = DecayingChroma(TAUS), DecayingChroma(TAUS)
    for i, (a, b) in enumerate([(60, 84), (64, 88), (67, 91)]):
        x = low.observe(a, i * 0.1)
        y = high.observe(b, i * 0.1)
    assert x == pytest.approx(y)


def test_reset_clears_history() -> None:
    c = DecayingChroma(TAUS)
    c.observe(60, 0.0)
    c.reset()
    x = c.observe(67, 5.0)
    assert x[7] == pytest.approx(1.0)  # only G present, no trace of C


# --------------------------------------------------------------------- arming


def test_arming_is_the_only_use_of_position() -> None:
    det = CadenzaDetector(_model())
    assert not det.maybe_arm(390.0, 0.0)
    assert det.state is CadenzaState.IDLE
    assert det.maybe_arm(399.0, 0.0)
    assert det.state is CadenzaState.ARMED


def test_idle_detector_ignores_notes() -> None:
    assert CadenzaDetector(_model()).observe(60, 0.0) is None


def test_a_frozen_follower_cannot_stall_an_armed_detector() -> None:
    """The deadlock this exists to remove.

    Position-gated anchors need the position they are meant to correct. Live,
    the follower sat at one beat for 16.4 seconds while twenty gestures went
    past. Once armed, position is never consulted again.
    """

    det = CadenzaDetector(_model())
    det.maybe_arm(399.5, 0.0)
    fire = det.observe(60, 0.0)
    assert fire is not None
    assert fire.kind == "handoff"
    # The beat handed back is where the ORCHESTRA resumes, not where the passage
    # was recognised. Recognition happens before the passage ends, so the two
    # differ -- and handing back at the recognition beat left the transport
    # inside the free region, where the following LEAD section never began.
    assert fire.canonical_beat == 411.0





# ----------------------------------------------------------------- the veto




def test_it_gives_up_rather_than_hanging() -> None:
    det = CadenzaDetector(_model(), give_up_after_beats=4.0)
    det.maybe_arm(400.0, 0.0)
    det.observe(61, 0.5)
    fire = det.observe(61, 9.0)
    assert fire is not None
    assert fire.kind == "gave_up"
    assert fire.canonical_beat == 411.0
    assert det.state is CadenzaState.GAVE_UP


# ------------------------------------------------------------------ the model


def test_model_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "m.json"
    _model().save(path)
    back = CadenzaModel.load(path)
    assert back.taus == TAUS
    assert back.coef == pytest.approx(_model().coef)
    assert back.handoff_beat == 410.0


def test_malformed_models_are_rejected() -> None:
    with pytest.raises(ValueError):
        _model(coef=np.zeros(NF + 1))
    with pytest.raises(ValueError):
        _model(handoff_beat=399.0)  # before the region starts


# --------------------------------------------------- the real trained model

MODEL = "data/scores/chopin_op11_movement_2/derived/cadenza_handoff.model.json"


def test_the_trained_model_loads_and_is_shaped_as_shipped() -> None:
    import os

    if not os.path.isfile(MODEL):
        pytest.skip("trained model not present")
    m = CadenzaModel.load(MODEL)
    assert len(m.coef) == len(m.taus) * CHROMA_BINS
    assert m.label_lead_beats == 1.0  # longer horizons fire early under rubato
    assert np.isfinite(m.coef).all()


def test_a_handoff_is_never_reversed() -> None:
    """Once handed off, playing on cannot take it back.

    A reversal mechanism existed and was removed. No premature fire was ever
    observed from real playing -- across two full takes and eight leave-one-out
    demonstrations it fired 0.95 s and 1.90 s AFTER the landing dyad, never
    before -- while its one firing on a live run was a false positive. The
    structure already provides the protection: the hand-back beat is the
    interlude's downbeat, so the orchestra starts there regardless.
    """

    det = CadenzaDetector(_model())
    det.maybe_arm(400.0, 0.0)
    fire = det.observe(60, 0.0)
    assert fire is not None and fire.kind == "handoff"
    for i in range(30):  # keep playing, at any spacing
        assert det.observe(60 + (i % 12), 0.5 * (i + 1)) is None
    assert det.state is CadenzaState.FIRED


def test_the_landing_chord_fires_the_handoff_not_confidence_alone() -> None:
    """The orchestra enters WITH the performer's landing chord, not a beat early.

    Live, the handoff fired as soon as the classifier crossed threshold -- about
    a beat before the performer struck the G#/F#, so the orchestra came in early.
    With landing pitches configured, confidence only ARMS the watch; the chord
    itself is the trigger.
    """

    model = _model(landing_pitches=(56, 90))
    det = CadenzaDetector(model)
    det.maybe_arm(400.0, 0.0)

    # Drive confidence over threshold with C-natural content -- but no landing.
    fire = None
    for i in range(8):
        fire = det.observe(60, i * 0.05) or fire
    assert fire is None, "must not hand off on confidence before the landing chord"
    assert det.state is CadenzaState.ARMED

    # The landing chord arrives: both pitches within the window.
    assert det.observe(56, 1.0) is None  # one alone is not the chord
    fire = det.observe(90, 1.05)
    assert fire is not None and fire.kind == "handoff"
    assert fire.canonical_beat == model.hand_back_beat


def test_a_fluffed_landing_still_hands_off_rather_than_stranding() -> None:
    """If we were confident but never saw a clean chord, hand off on give-up."""

    model = _model(landing_pitches=(56, 90))
    det = CadenzaDetector(model, give_up_after_beats=2.0)
    det.maybe_arm(400.0, 0.0)
    for i in range(8):
        det.observe(60, i * 0.05)  # commit confidence, no landing chord
    fire = det.observe(61, 10.0)  # long after, still no clean landing
    assert fire is not None and fire.kind == "handoff"
