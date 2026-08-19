"""Pydantic v2 models for persisted take artifacts (roadmap items 2-5).

Schema & Validation Architecture (docs/design/SCHEMA_VALIDATION_ARCH.md,
issue #85) diagnosed the persisted-artifact round-trip as the main backend
validation gap: `take.json`/`aligned.json`/`profile.json`/`coverage.json`
were written from typed objects but read back as raw `dict[str, object]`,
so every consumer re-derived structure with its own null/isinstance/key-
presence guard. These models are the "parse, don't validate" fix -- the
single typed shape for each artifact, both directions:

- Read: `Model.model_validate_json(path.read_text())`. This raises
  `pydantic.ValidationError` loudly on anything malformed -- no silent
  defaults, no `.get(..., fallback)`. A guard belongs at a boundary the
  outside world writes (MusicXML/MIDI parsing); these files are boundaries
  *we* write, so a schema is the correct fix, not a defensive read.
- Write: `model.model_dump_json(indent=2)`.

`extra="ignore"` (via `ArtifactModel`) makes old readers tolerant of a
newer writer's additional fields; declared field *types* stay strict --
this is forward-compatibility, not laxity.

No data migration: every model mirrors the on-disk shape the dataclasses
and dict literals it replaces already produced. The one intentional
widening is `LocalizationCandidate.start_position: int | None = None`,
replacing the stringly `"start_position" not in candidate` version-skew
probe in `aligner.py` with a real optional field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TakeStatus = Literal[
    "captured",
    "aligning",
    "aligned",
    "ambiguous",
    "unalignable",
    "discarded",
]


class ArtifactModel(BaseModel):
    """Shared config for every persisted-artifact model.

    Frozen: these are read once and treated as immutable value objects,
    matching the frozen dataclasses (`AlignedTake`) they replace.
    `extra="ignore"`: an old reader tolerates a newer writer's extra field.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")


class TakeCue(ArtifactModel):
    """Localization hint recorded for a cued take.

    docs/design/RECORDING_FLOW_REDESIGN.md §4.1: written at
    `/hardware/record-with-cue/start` time and attached to the take when it
    stops. Entry and cue-start beats are canonical score anchors; seconds are
    retained only as the derived recording-relative entry offset.
    """

    kind: Literal["from_top", "from_position"]
    target_beat: float
    cue_start_beat: float | None = None
    cue_seconds: float
    output_name: str


class TakePlacementHint(ArtifactModel):
    """Performer-selected score entry, independent of an orchestra cue."""

    target_beat: float
    source: Literal["selected_passage"] = "selected_passage"


class TakeRecord(ArtifactModel):
    """The `take.json` contract (roadmap item 2)."""

    take_id: str
    piece_id: str
    movement: int
    recorded_at: datetime
    duration_seconds: float
    note_on_count: int
    input_name: str | None = None
    cue: TakeCue | None = None
    placement_hint: TakePlacementHint | None = None
    status: TakeStatus
    midi_path: str


class TimingMapPoint(ArtifactModel):
    """One matched anchor in `AlignedResult.timing_map`."""

    score_beat: float
    take_seconds: float


class LocalizationCandidate(ArtifactModel):
    """One candidate score position from stage-1 localization voting."""

    start_beat: float
    score: float
    # Pre-existing version skew: takes aligned before this field existed
    # only persisted {start_beat, score}. `None` means "aligned with an
    # older aligner version, no reference index to re-align at" -- the
    # resolve path (aligner.py `_validate_resolve_candidate`) turns that
    # into an explicit, typed `ValueError` instead of a stringly `in` probe.
    start_position: int | None = None


class CellSample(ArtifactModel):
    """One profile-grid cell sampled from a take's real MIDI (design doc §2.3)."""

    beat: float
    period_s: float
    velocity: float
    pedal: float
    quality: float


class AlignedResult(ArtifactModel):
    """The `aligned.json` contract (roadmap item 3, design doc §2.3)."""

    take_id: str
    aligner: str
    score_start_beat: float
    score_end_beat: float
    match_rate: float
    ambiguous: bool
    matched_notes: int
    extra_notes: int
    missing_notes: int
    timing_map: tuple[TimingMapPoint, ...]
    candidates: tuple[LocalizationCandidate, ...]
    cell_samples: tuple[CellSample, ...]
    edge_trim_beats: tuple[float, float]


class PerformanceProfileCell(ArtifactModel):
    """Robust rehearsal evidence at one canonical score location.

    Absolute tempo and dimensionless rubato are deliberately both retained.
    ``seconds_per_quarter`` answers how fast Eric played; ``rubato_ratio``
    answers how this location dilated or compressed relative to that take's
    own baseline tempo.  Keeping those concepts separate lets the performer
    change the global orchestra pace without erasing the learned phrase shape.
    """

    score_tick: int
    seconds_per_quarter: float
    seconds_per_quarter_mad: float
    rubato_ratio: float
    rubato_ratio_mad: float
    velocity: float
    velocity_mad: float
    pedal: float
    pedal_mad: float
    support: int
    mean_quality: float


class Interpretation(ArtifactModel):
    """The fitted per-cell tempo/dynamics model learned from the selects.

    Conceptually this is Eric's *Interpretation* of the movement -- his
    repeatable rubato shape (expected tempo + dispersion per cell), fitted from
    the curated (kept + included) takes. It is persisted as the canonical
    performance profile v2 (on disk as `profile.json`, a stable artifact name).

    Version 1 used float ``beat`` keys and an ambiguous ``period_s`` field.
    Version 2 has one integer coordinate, an explicit grid, and named timing
    units.  This is an intentionally breaking one-time migration; runtime
    readers do not carry a v1 fallback.
    """

    schema_version: Literal[2] = 2
    piece_id: str
    movement: int
    coordinate_system: Literal["canonical_score"] = "canonical_score"
    canonical_ppq: Literal[960] = 960
    grid_step_ticks: Literal[480] = 480
    take_count: int
    updated: datetime
    base_seconds_per_quarter: float | None = None
    cells: tuple[PerformanceProfileCell, ...]
    # Reserved for future annotated rehearsal moments; always empty today.
    moments: tuple[dict[str, Any], ...] = ()
    revision: int = 0
    input_revision: str = "legacy"
    included_take_ids: tuple[str, ...] = ()


CoverageState = Literal["tutti", "covered", "touched", "uncovered"]
CoverageScope = Literal["solo", "tutti", "unknown"]


class CoverageMeasure(ArtifactModel):
    measure: int
    start_beat: float
    end_beat: float
    solo: bool
    min_n: int | None = None
    min_quality: float | None = None
    mean_quality: float | None = None
    state: CoverageState
    scope: CoverageScope | None = None
    observed: bool = False
    # Score-derived accompaniment demand is independent of how much solo
    # rehearsal evidence exists. False means green/ready is honest even when
    # the pianist has never recorded this measure: the orchestra has no note
    # there to miss.
    accompaniment_required: bool | None = None


class CoverageSummary(ArtifactModel):
    solo_measures: int
    covered: int
    touched: int
    uncovered: int
    percent_covered: float
    observable_measures: int = 0
    observed_measures: int = 0
    percent_observed: float = 0.0


class CoverageDoc(ArtifactModel):
    """The `coverage.json` contract (roadmap item 4/5)."""

    piece_id: str
    movement: int
    computed_at: datetime
    n_target: int
    quality_target: float = 0.5
    measures: tuple[CoverageMeasure, ...]
    summary: CoverageSummary
    revision: int = 0
    profile_revision: int = 0
    mapping_review_state: Literal["machine", "reviewed"] = "reviewed"
    canonical_positions: bool = True
    algorithm_revision: str = "coverage-v1"


class Anchor(ArtifactModel):
    """One human-marked beat where the orchestra should lock to the pianist.

    The pianist marks a strong beat (e.g. a deep bass octave). Live, the instant
    a pianist note lands on this beat's score position, the orchestra's chord for
    that beat fires reactively -- eliminating the tracking latency at moments the
    performer cares most about. ``score_tick`` (canonical 960-PPQ) is the stable
    identity and the natural key for delete; ``measure`` is display-only.
    """

    score_tick: int = Field(ge=0)
    measure: int = Field(ge=1)
    label: str = ""


class AnchorSet(ArtifactModel):
    """The `anchors.json` contract: a piece/movement's structural beat anchors.

    A human annotation layer that persists independently of the take-derived
    Interpretation, so rebuilding the profile never disturbs it.
    """

    schema_version: Literal[1] = 1
    piece_id: str
    movement: int = Field(ge=1)
    updated: datetime
    anchors: tuple[Anchor, ...] = ()


__all__ = [
    "TakeStatus",
    "ArtifactModel",
    "Anchor",
    "AnchorSet",
    "TakeCue",
    "TakeRecord",
    "TimingMapPoint",
    "LocalizationCandidate",
    "CellSample",
    "AlignedResult",
    "PerformanceProfileCell",
    "Interpretation",
    "CoverageState",
    "CoverageScope",
    "CoverageMeasure",
    "CoverageSummary",
    "CoverageDoc",
]
