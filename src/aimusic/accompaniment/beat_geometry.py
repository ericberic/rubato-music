"""Infer where each beat sits horizontally on the page, from note geometry.

This is the one place beat -> pixel inference lives. It exists because a beat
has no glyph of its own: its x is read off the notes that sound on it. Doing
that well is more than "median x of the notes at that onset" -- see the choices
below, each of which came from a real mis-placement:

- **Pick the clearest staff.** A Chopin right hand is full of runs and
  flourishes whose notes do not land on the beat grid; the left hand or an
  orchestra reduction voice is often plain quarters/eighths that the engraver
  aligned exactly under the beats. So every staff is scored for how cleanly its
  onsets sample the beat grid, and the best one is used. (The engraver
  vertically aligns all staves, so any clean staff locates the beats for all.)

- **One anchor per beat, not every note.** Only the note nearest each integer
  beat anchors that beat; the 30 notes of a run between beats 3 and 4 are not
  knots. Using them all is what crammed a later beat marker on top of an earlier
  one.

- **Engraving margins.** Notes are not flush to the barlines: a beat sits inset
  by roughly half a beat-space, and the space after a clef/key change at a new
  system pushes beat 1 further right. Observed note x's carry these margins for
  free; the sparse-measure fallback models them explicitly rather than spreading
  beats barline-to-barline.

- **A confidence.** Each measure's placement carries a [0,1] confidence -- how
  well the chosen staff actually pinned the beats -- so weak measures can be
  flagged rather than silently trusted.

Coordinate frames (docs/concepts/score-coordinate-systems.md): input note x is
MEASURE-LOCAL (MusicXML tenths from the left barline); output is PAGE-NORMALIZED
[0,1], projected into the display box's page span. The beat->pixel map is
piecewise-linear through the anchors, NOT a single linear stretch of the box --
that difference is the "pixel vs timestamp" transform, and it is why an evenly
spaced set of beats in time is unevenly spaced in pixels.
"""

from __future__ import annotations

import statistics
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BEATS_PER_MEASURE = 4
# How close (in beats) an onset must be to an integer beat to anchor it.
ANCHOR_TOLERANCE_BEATS = 0.3
# A rest covering at least this fraction of the measure is a whole-measure rest,
# drawn centred and therefore NOT a beat anchor.
WHOLE_REST_FRACTION = 0.85
# Below this confidence the chosen staff is not trusted; fall back to the
# evenly-spaced-with-margins model.
MIN_TRUSTED_CONFIDENCE = 0.35
# Beat inset from each barline, as a fraction of the measure, for the fallback.
# Half a beat-space on a 4-beat bar is 1/8 of the width.
EDGE_MARGIN_FRACTION = 0.5 / DEFAULT_BEATS_PER_MEASURE
# Extra left inset when a new system restates clef/key/time before the music.
NEW_SYSTEM_LEFT_PAD_FRACTION = 0.10


@dataclass(frozen=True)
class BeatGlyph:
    """A note or rest, with its rhythmic onset and measure-local x."""

    staff: str
    onset_beats: float
    x: float
    is_rest: bool
    covers_measure: bool  # a whole-measure rest, centred, not a beat anchor


@dataclass(frozen=True)
class BeatInference:
    """Where each beat sits (page-normalized x), and how sure we are."""

    beat_x: tuple[float, ...]
    confidence: float
    staff: str | None
    method: str  # "staff" | "even_margins"
    # Per beat: True if a real note anchored it, False if it was interpolated /
    # placed by the even-spacing fallback. Lets the validation view distinguish
    # measured positions from inferred ones.
    anchored: tuple[bool, ...]


def _median_x_by_onset(glyphs: list[BeatGlyph]) -> list[tuple[float, float]]:
    by_onset: dict[float, list[float]] = {}
    for glyph in glyphs:
        by_onset.setdefault(round(glyph.onset_beats, 3), []).append(glyph.x)
    return sorted((onset, statistics.median(xs)) for onset, xs in by_onset.items())


def _anchor_beats(
    points: list[tuple[float, float]], beats_per_measure: int
) -> dict[int, tuple[float, float]]:
    """For each integer beat, the nearest onset within tolerance: {beat: (x, err)}."""

    anchors: dict[int, tuple[float, float]] = {}
    for beat in range(beats_per_measure):
        best: tuple[float, float] | None = None
        for onset, x in points:
            error = abs(onset - beat)
            if error <= ANCHOR_TOLERANCE_BEATS and (best is None or error < best[1]):
                best = (x, error)
        if best is not None:
            anchors[beat] = best
    return anchors


def _staff_confidence(
    anchors: dict[int, tuple[float, float]],
    point_count: int,
    beats_per_measure: int,
) -> float:
    if not anchors:
        return 0.0
    coverage = len(anchors) / beats_per_measure
    beats = sorted(anchors)
    xs = [anchors[b][0] for b in beats]
    monotonic = all(right >= left for left, right in zip(xs, xs[1:]))
    mean_error = sum(err for _, err in anchors.values()) / len(anchors)
    closeness = max(0.3, 1.0 - mean_error / ANCHOR_TOLERANCE_BEATS)
    # A staff crowded with ornamental notes is a weaker beat locator even when it
    # happens to cover the grid, because a stray note can win an anchor slot.
    clean = min(1.0, (beats_per_measure * 3) / max(point_count, 1))
    return coverage * (1.0 if monotonic else 0.4) * closeness * max(0.5, clean)


def _project(fraction: float, box_x0: float, box_x1: float) -> float:
    return box_x0 + max(0.0, min(1.0, fraction)) * (box_x1 - box_x0)


def _even_margins(beats_per_measure: int, *, new_system: bool) -> tuple[float, ...]:
    """Beat FRACTIONS evenly spaced inside the measure, inset from both barlines.

    Half a beat-space before beat 1 and after the last beat, so nothing sits on
    a barline -- the sparse-measure convention. A new system adds left padding
    for the clef/key/time it restates before the music begins.
    """

    left = EDGE_MARGIN_FRACTION + (NEW_SYSTEM_LEFT_PAD_FRACTION if new_system else 0.0)
    usable = 1.0 - left - EDGE_MARGIN_FRACTION
    step = usable / max(1, beats_per_measure - 1)
    return tuple(left + step * beat for beat in range(beats_per_measure))


def _fill_from_anchors(
    anchors: dict[int, tuple[float, float]], beats_per_measure: int
) -> tuple[float, ...]:
    """Anchored beat FRACTIONS keep their x; gaps and ends are linearly filled."""

    known = sorted(anchors)
    xs: list[float] = []
    for beat in range(beats_per_measure):
        if beat in anchors:
            xs.append(anchors[beat][0])
            continue
        lower = max((b for b in known if b < beat), default=None)
        upper = min((b for b in known if b > beat), default=None)
        if lower is not None and upper is not None:
            x_lo, x_hi = anchors[lower][0], anchors[upper][0]
            ratio = (beat - lower) / (upper - lower)
            xs.append(x_lo + ratio * (x_hi - x_lo))
        elif lower is not None and lower - 1 in anchors:
            slope = anchors[lower][0] - anchors[lower - 1][0]
            xs.append(anchors[lower][0] + slope * (beat - lower))
        elif upper is not None and upper + 1 in anchors:
            slope = anchors[upper + 1][0] - anchors[upper][0]
            xs.append(anchors[upper][0] + slope * (beat - upper))
        elif known:
            xs.append(anchors[known[0]][0])
        else:  # pragma: no cover - only called when anchors is non-empty
            xs.append(0.0)
    # Never let a beat retreat or escape the measure.
    clamped: list[float] = []
    running = 0.0
    for x in xs:
        running = max(running, min(max(x, 0.0), 1.0))
        clamped.append(running)
    return tuple(clamped)


def infer_measure_beats(
    glyphs: list[BeatGlyph],
    *,
    box_x0: float,
    box_x1: float,
    beats_per_measure: int = DEFAULT_BEATS_PER_MEASURE,
    new_system: bool = False,
) -> BeatInference:
    """Best-staff, engraving-aware beat positions for one measure.

    ``glyphs`` are every note/rest of the measure, across staves. Beat x's are
    normalized (measure-local ``x`` divided by ``measure_width`` is expected to
    already be applied by the caller so ``glyph.x`` is in the same fraction space
    as the box) -- see ``read_beat_glyphs``.
    """

    usable = [g for g in glyphs if not g.covers_measure]
    by_staff: dict[str, list[BeatGlyph]] = {}
    for glyph in usable:
        by_staff.setdefault(glyph.staff, []).append(glyph)

    best: tuple[float, str, dict[int, tuple[float, float]]] | None = None
    for staff, staff_glyphs in by_staff.items():
        points = _median_x_by_onset(staff_glyphs)
        anchors = _anchor_beats(points, beats_per_measure)
        confidence = _staff_confidence(anchors, len(points), beats_per_measure)
        if best is None or confidence > best[0]:
            best = (confidence, staff, anchors)

    if best is None or best[0] < MIN_TRUSTED_CONFIDENCE:
        fractions = _even_margins(beats_per_measure, new_system=new_system)
        return BeatInference(
            beat_x=tuple(_project(f, box_x0, box_x1) for f in fractions),
            confidence=best[0] if best else 0.0,
            staff=None,
            method="even_margins",
            anchored=tuple(False for _ in range(beats_per_measure)),
        )
    confidence, staff, anchors = best
    fractions = _fill_from_anchors(anchors, beats_per_measure)
    return BeatInference(
        beat_x=tuple(_project(f, box_x0, box_x1) for f in fractions),
        confidence=confidence,
        staff=staff,
        method="staff",
        anchored=tuple(beat in anchors for beat in range(beats_per_measure)),
    )


def _musicxml_root(path: Path) -> ET.Element:
    if path.suffix.lower() in {".mxl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            member = next(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".xml") and not name.startswith("META-INF/")
                ),
                None,
            )
            if member is None:
                raise ValueError(f"No MusicXML document found in {path}")
            return ET.fromstring(archive.read(member))
    return ET.fromstring(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class MeasureGlyphs:
    """Every note/rest of one measure across staves, plus layout facts."""

    glyphs: tuple[BeatGlyph, ...]
    new_system: bool


def read_beat_glyphs(path: Path | str) -> dict[int, MeasureGlyphs]:
    """Notes AND rests with staff and measure-fraction x, keyed by measure number.

    Merges every part/staff for a measure -- the beat inference picks the best
    staff across all of them. ``x`` is the note's ``default-x`` divided by the
    measure width, so it is a fraction in [0, 1] of the measure regardless of the
    note pass's units. Rests are kept (they sit on beats too); a rest spanning
    the whole measure is marked ``covers_measure`` because it is drawn centred
    and must not anchor a beat.
    """

    root = _musicxml_root(Path(path))
    per_measure: dict[int, list[BeatGlyph]] = {}
    new_system: dict[int, bool] = {}
    for part in root.findall("{*}part"):
        divisions = 1
        for ordinal, measure in enumerate(part.findall("{*}measure"), start=1):
            try:
                number = int(measure.attrib.get("number", ordinal))
            except ValueError:
                number = ordinal
            width = _optional_float(measure.attrib.get("width")) or 0.0
            beats_per_measure = DEFAULT_BEATS_PER_MEASURE
            measure_ticks = divisions * beats_per_measure
            print_el = measure.find("{*}print")
            if print_el is not None and print_el.attrib.get("new-system") == "yes":
                new_system[number] = True
            cursor = 0
            previous_onset = 0
            for child in measure:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "attributes":
                    value = child.findtext("{*}divisions")
                    if value:
                        divisions = max(1, int(value))
                        measure_ticks = divisions * beats_per_measure
                elif tag == "backup":
                    cursor -= int(child.findtext("{*}duration") or 0)
                elif tag == "forward":
                    cursor += int(child.findtext("{*}duration") or 0)
                elif tag == "note":
                    duration = int(child.findtext("{*}duration") or 0)
                    is_chord = child.find("{*}chord") is not None
                    onset = previous_onset if is_chord else cursor
                    if not is_chord:
                        previous_onset = onset
                        cursor += duration
                    default_x = _optional_float(child.attrib.get("default-x"))
                    if default_x is None or width <= 0:
                        continue
                    staff = child.findtext("{*}staff") or "1"
                    is_rest = child.find("{*}rest") is not None
                    per_measure.setdefault(number, []).append(
                        BeatGlyph(
                            staff=f"{part.attrib.get('id', '')}:{staff}",
                            onset_beats=onset / divisions,
                            x=default_x / width,
                            is_rest=is_rest,
                            covers_measure=is_rest and duration >= WHOLE_REST_FRACTION * measure_ticks,
                        )
                    )
    return {
        number: MeasureGlyphs(tuple(glyphs), new_system.get(number, False))
        for number, glyphs in per_measure.items()
    }


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
