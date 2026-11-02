"""
Tests for docs/validation/golden_case_model.py — SQL rebuild Phase 5,
Step 1.

Pure dataclass tests: construction, normalization to immutable collection
types, and constructor-time validation. No pipeline, no database, no AI
provider anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_golden_case_model.py -v
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

_VALIDATION_DIR = Path(__file__).resolve().parent.parent / "docs" / "validation"
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from golden_case_model import DEFAULT_LATENCY_TARGET_SECONDS, GoldenCase, RowShapeExpectation


def _minimal_case(**overrides) -> GoldenCase:
    fields = {
        "case_id": "T-001",
        "category": "counts",
        "question": "How many things are there?",
        "expected_status": "OK",
    }
    fields.update(overrides)
    return GoldenCase(**fields)


# ── GoldenCase construction / normalization ────────────────────────────────


def test_minimal_case_has_expected_defaults():
    case = _minimal_case()
    assert case.required_object_fqns == frozenset()
    assert case.allowed_object_fqns is None
    assert case.forbidden_object_fqns == frozenset()
    assert case.required_answer_terms == ()
    assert case.required_answer_patterns == ()
    assert case.expected_row_shape is None
    assert case.requires_insights is False
    assert case.requires_evidence is False
    assert case.expects_clarification is False
    assert case.expects_zero_rows is False
    assert case.latency_target_seconds == DEFAULT_LATENCY_TARGET_SECONDS
    assert case.notes == ""


def test_collections_are_normalized_to_immutable_types():
    case = _minimal_case(
        required_object_fqns=["dbo.A", "dbo.B"],
        forbidden_object_fqns=["dbo.C"],
        allowed_object_fqns=["dbo.A", "dbo.B", "dbo.D"],
        required_answer_terms=["students", "count"],
        required_answer_patterns=[r"\d+"],
    )
    assert isinstance(case.required_object_fqns, frozenset)
    assert case.required_object_fqns == {"dbo.A", "dbo.B"}
    assert isinstance(case.forbidden_object_fqns, frozenset)
    assert isinstance(case.allowed_object_fqns, frozenset)
    assert isinstance(case.required_answer_terms, tuple)
    assert isinstance(case.required_answer_patterns, tuple)


def test_golden_case_is_frozen():
    case = _minimal_case()
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.case_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field_name", ["case_id", "category", "question", "expected_status"])
def test_empty_required_string_field_rejected(field_name):
    with pytest.raises(ValueError):
        _minimal_case(**{field_name: "   "})


def test_required_and_forbidden_overlap_rejected():
    with pytest.raises(ValueError):
        _minimal_case(required_object_fqns={"dbo.A"}, forbidden_object_fqns={"dbo.A"})


def test_requires_evidence_without_requires_insights_rejected():
    with pytest.raises(ValueError):
        _minimal_case(requires_evidence=True, requires_insights=False)


def test_requires_evidence_with_requires_insights_accepted():
    case = _minimal_case(requires_evidence=True, requires_insights=True)
    assert case.requires_evidence is True


@pytest.mark.parametrize("bad_value", [0, -1.0, "5", None])
def test_non_positive_latency_target_rejected(bad_value):
    with pytest.raises(ValueError):
        _minimal_case(latency_target_seconds=bad_value)


def test_expected_row_shape_type_enforced():
    with pytest.raises(ValueError):
        _minimal_case(expected_row_shape="not a shape")  # type: ignore[arg-type]


# ── RowShapeExpectation ─────────────────────────────────────────────────────


def test_row_shape_defaults():
    shape = RowShapeExpectation()
    assert shape.min_rows is None
    assert shape.exact_row_count is None
    assert shape.required_column_names == ()
    assert shape.expect_duplicate_column_names is None
    assert shape.expected_truncated is None


def test_row_shape_is_frozen():
    shape = RowShapeExpectation(min_rows=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        shape.min_rows = 2  # type: ignore[misc]


def test_row_shape_normalizes_required_column_names_to_tuple():
    shape = RowShapeExpectation(required_column_names=["A", "B"])
    assert shape.required_column_names == ("A", "B")


@pytest.mark.parametrize("bad_kwargs", [
    {"min_rows": -1},
    {"max_rows": -1},
    {"exact_row_count": -1},
    {"min_columns": -1},
    {"max_columns": -1},
    {"min_rows": 1.5},
    {"min_rows": True},
])
def test_row_shape_rejects_bad_numeric_bounds(bad_kwargs):
    with pytest.raises(ValueError):
        RowShapeExpectation(**bad_kwargs)


def test_row_shape_rejects_min_greater_than_max_rows():
    with pytest.raises(ValueError):
        RowShapeExpectation(min_rows=5, max_rows=1)


def test_row_shape_rejects_min_greater_than_max_columns():
    with pytest.raises(ValueError):
        RowShapeExpectation(min_columns=5, max_columns=1)


def test_row_shape_rejects_non_bool_duplicate_flag():
    with pytest.raises(ValueError):
        RowShapeExpectation(expect_duplicate_column_names="yes")  # type: ignore[arg-type]


def test_golden_case_accepts_a_valid_row_shape():
    shape = RowShapeExpectation(min_rows=1, max_rows=100, required_column_names=("PathName",))
    case = _minimal_case(expected_row_shape=shape)
    assert case.expected_row_shape is shape
