"""
Fixture sanity tests for docs/validation/ccpp_golden_cases_v2.py — SQL
rebuild Phase 5, Step 1.

These tests check that the CCPP fixture is well-formed (unique ids, valid
categories, internally consistent fields). They deliberately do NOT run
any case against a real or fake `answer_question` pipeline — per the
Phase 5, Step 1 scope, this suite is not executed against the real
database in this step (see docs/validation/golden_runner.py's own tests
for the deterministic pipeline-execution proof, and a later milestone for
wiring this fixture to the live pipeline).

Run:
    venv/Scripts/pytest tests/test_ccpp_golden_cases_v2.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VALIDATION_DIR = Path(__file__).resolve().parent.parent / "docs" / "validation"
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES
from golden_case_model import GoldenCase

_EXPECTED_CATEGORIES = {
    "simple_count", "filtered_count", "grouped_aggregation", "date_filtering",
    "trend_time_comparison", "student_class_program_joins", "course_path_relationships",
    "payment_aggregation", "graduate_survey_analysis", "zero_row_result",
    "ambiguous_clarification", "archive_backup_avoidance", "tier_avoidance",
    "duplicate_columns", "truncation_behavior",
}


def test_suite_is_non_empty():
    assert len(CCPP_GOLDEN_CASES) > 0


def test_every_entry_is_a_golden_case():
    assert all(isinstance(c, GoldenCase) for c in CCPP_GOLDEN_CASES)


def test_case_ids_are_unique():
    ids = [c.case_id for c in CCPP_GOLDEN_CASES]
    assert len(ids) == len(set(ids))


def test_every_required_category_is_represented():
    categories = {c.category for c in CCPP_GOLDEN_CASES}
    missing = _EXPECTED_CATEGORIES - categories
    assert not missing, f"missing categories: {sorted(missing)}"


def test_every_case_has_grounding_notes():
    for case in CCPP_GOLDEN_CASES:
        assert case.notes.strip(), f"{case.case_id} has no grounding notes"


def test_clarification_cases_carry_no_required_objects():
    for case in CCPP_GOLDEN_CASES:
        if case.expects_clarification:
            assert not case.required_object_fqns, (
                f"{case.case_id} expects clarification but declares required_object_fqns"
            )


def test_forbidden_and_required_objects_never_overlap():
    # Already enforced by GoldenCase.__post_init__ at construction time —
    # this test documents/re-confirms the invariant holds for every fixture
    # entry, not just at construction.
    for case in CCPP_GOLDEN_CASES:
        assert not (case.required_object_fqns & case.forbidden_object_fqns)


def test_object_fqns_look_like_schema_qualified_names():
    for case in CCPP_GOLDEN_CASES:
        for fqn in case.required_object_fqns | case.forbidden_object_fqns:
            assert "." in fqn, f"{case.case_id}: {fqn!r} is not schema-qualified"


def test_latency_targets_are_within_a_sane_reporting_bound():
    for case in CCPP_GOLDEN_CASES:
        assert 0 < case.latency_target_seconds <= 15.0, (
            f"{case.case_id} has an unreasonable latency_target_seconds={case.latency_target_seconds}"
        )


@pytest.mark.parametrize("case", CCPP_GOLDEN_CASES, ids=[c.case_id for c in CCPP_GOLDEN_CASES])
def test_case_question_is_non_trivial_text(case):
    assert len(case.question.strip()) >= 5
