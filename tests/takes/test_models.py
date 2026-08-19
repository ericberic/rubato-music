"""Round-trip and fail-loud tests for the persisted-artifact Pydantic models.

Issue #85 / docs/design/SCHEMA_VALIDATION_ARCH.md §2.1, §5: every artifact
model must round-trip (write -> read -> equal) and must reject a malformed
file with a loud `pydantic.ValidationError` -- never a silent default, never
`None`/`NaN` standing in for missing data. This is the core regression guard
for the whole issue: it is what makes the deleted `.get(..., fallback)` /
`isinstance` probes throughout `aligner.py`/`coverage.py`/`profile.py`/
`routes.py` unnecessary in the first place.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aimusic.takes.models import (
    AlignedResult,
    CellSample,
    CoverageDoc,
    CoverageMeasure,
    CoverageSummary,
    Interpretation,
    LocalizationCandidate,
    TakeCue,
    TakeRecord,
    TimingMapPoint,
)

NOW = datetime(2026, 7, 9, 20, 0, 0, tzinfo=timezone.utc)


def _sample_take_record(**overrides: object) -> TakeRecord:
    fields: dict[object, object] = dict(
        take_id="t20260709T200000Z-0001",
        piece_id="chopin_op11",
        movement=2,
        recorded_at=NOW,
        duration_seconds=12.5,
        note_on_count=42,
        input_name="CLP-795GP USB",
        cue=None,
        status="captured",
        midi_path="take.mid",
    )
    fields.update(overrides)
    return TakeRecord(**fields)


def _sample_aligned_result(**overrides: object) -> AlignedResult:
    fields: dict[object, object] = dict(
        take_id="t20260709T200000Z-0001",
        aligner="seeded-local-v1",
        score_start_beat=10.0,
        score_end_beat=14.0,
        match_rate=0.9,
        ambiguous=False,
        matched_notes=8,
        extra_notes=1,
        missing_notes=0,
        timing_map=(TimingMapPoint(score_beat=10.0, take_seconds=5.0),),
        candidates=(
            LocalizationCandidate(start_beat=10.0, score=0.8, start_position=500),
        ),
        cell_samples=(
            CellSample(beat=10.0, period_s=0.5, velocity=80.0, pedal=0.2, quality=0.9),
        ),
        edge_trim_beats=(1.0, 0.5),
    )
    fields.update(overrides)
    return AlignedResult(**fields)


def _sample_profile_doc(**overrides: object) -> Interpretation:
    fields: dict[object, object] = dict(
        piece_id="chopin_op11",
        movement=2,
        take_count=3,
        updated=NOW,
        cells=(),
        moments=(),
    )
    fields.update(overrides)
    return Interpretation(**fields)


def _sample_coverage_doc(**overrides: object) -> CoverageDoc:
    fields: dict[object, object] = dict(
        piece_id="chopin_op11",
        movement=1,
        computed_at=NOW,
        n_target=3,
        measures=(
            CoverageMeasure(measure=1, start_beat=0.0, end_beat=4.0, solo=False, state="tutti"),
            CoverageMeasure(
                measure=2,
                start_beat=4.0,
                end_beat=8.0,
                solo=True,
                min_n=2,
                mean_quality=0.85,
                state="covered",
            ),
        ),
        summary=CoverageSummary(
            solo_measures=1, covered=1, touched=0, uncovered=0, percent_covered=100.0
        ),
    )
    fields.update(overrides)
    return CoverageDoc(**fields)


# --- Round-trip: write -> read -> equal ---


def test_take_record_round_trips() -> None:
    take = _sample_take_record(
        cue=TakeCue(kind="from_top", target_beat=0.0, cue_seconds=8.0, output_name="Fake Synth")
    )
    restored = TakeRecord.model_validate_json(take.model_dump_json())
    assert restored == take


def test_take_record_round_trips_with_no_cue() -> None:
    take = _sample_take_record(cue=None)
    restored = TakeRecord.model_validate_json(take.model_dump_json())
    assert restored == take
    assert restored.cue is None


def test_aligned_result_round_trips() -> None:
    aligned = _sample_aligned_result()
    restored = AlignedResult.model_validate_json(aligned.model_dump_json())
    assert restored == aligned


def test_profile_doc_round_trips() -> None:
    profile_doc = _sample_profile_doc()
    restored = Interpretation.model_validate_json(profile_doc.model_dump_json())
    assert restored == profile_doc


def test_coverage_doc_round_trips() -> None:
    coverage_doc = _sample_coverage_doc()
    restored = CoverageDoc.model_validate_json(coverage_doc.model_dump_json())
    assert restored == coverage_doc


# --- The one intentional widening: start_position is optional ---


def test_localization_candidate_start_position_defaults_to_none() -> None:
    """Pre-migration aligned.json files only ever persisted {start_beat,
    score} -- `start_position` must default to `None` when the key is
    entirely absent from the JSON, not raise or coerce to 0.
    """

    candidate = LocalizationCandidate.model_validate_json('{"start_beat": 1.0, "score": 0.5}')
    assert candidate.start_position is None


# --- extra="ignore": old readers tolerate a newer writer's extra field ---


def test_take_record_ignores_unknown_extra_fields() -> None:
    take = _sample_take_record()
    payload = take.model_dump_json()
    # Splice in a field a hypothetical future writer added.
    injected = payload[:-1] + ', "future_field": "unknown to this reader"}'
    restored = TakeRecord.model_validate_json(injected)
    assert restored == take


# --- Malformed file: loud ValidationError, never a silent default ---


def test_take_record_malformed_status_raises_validation_error() -> None:
    payload = _sample_take_record().model_dump_json()
    corrupted = payload.replace('"captured"', '"not_a_real_status"')
    with pytest.raises(ValidationError):
        TakeRecord.model_validate_json(corrupted)


def test_take_record_missing_required_field_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        TakeRecord.model_validate_json("{}")


def test_aligned_result_malformed_candidates_raises_validation_error() -> None:
    """Candidates shaped as a dict instead of a list (the historical bug
    class the whole issue is about) must fail loudly, not silently coerce
    to an empty list or raise an unhandled TypeError downstream.
    """

    payload = _sample_aligned_result().model_dump_json()
    corrupted = payload.replace('"candidates":[{', '"candidates":{"bad": {')
    with pytest.raises(ValidationError):
        AlignedResult.model_validate_json(corrupted)


def test_aligned_result_non_numeric_match_rate_raises_validation_error() -> None:
    """A hand-edited or truncated write leaving `match_rate` as a string
    must not silently become 0.0 (the old `.get("match_rate", 0.0)`
    default) -- it must fail loudly instead.
    """

    payload = _sample_aligned_result().model_dump_json()
    corrupted = payload.replace('"match_rate":0.9', '"match_rate":"not-a-number"')
    with pytest.raises(ValidationError):
        AlignedResult.model_validate_json(corrupted)


def test_profile_doc_malformed_updated_timestamp_raises_validation_error() -> None:
    payload = _sample_profile_doc().model_dump_json()
    corrupted = re.sub(r'"updated":"[^"]*"', '"updated":"not-a-timestamp"', payload)
    assert corrupted != payload
    with pytest.raises(ValidationError):
        Interpretation.model_validate_json(corrupted)


def test_coverage_doc_malformed_state_raises_validation_error() -> None:
    payload = _sample_coverage_doc().model_dump_json()
    corrupted = payload.replace('"tutti"', '"not_a_real_state"')
    with pytest.raises(ValidationError):
        CoverageDoc.model_validate_json(corrupted)


def test_coverage_doc_missing_summary_raises_validation_error() -> None:
    payload = _sample_coverage_doc().model_dump()
    del payload["summary"]
    with pytest.raises(ValidationError):
        CoverageDoc.model_validate(payload)


# --- Models are frozen: immutable value objects, matching the dataclasses
# they replace ---


def test_take_record_is_frozen() -> None:
    take = _sample_take_record()
    with pytest.raises(ValidationError):
        take.status = "aligned"  # type: ignore[misc]


def test_aligned_result_is_frozen() -> None:
    aligned = _sample_aligned_result()
    with pytest.raises(ValidationError):
        aligned.match_rate = 0.0  # type: ignore[misc]
