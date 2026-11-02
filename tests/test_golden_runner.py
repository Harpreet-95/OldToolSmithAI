"""
Deterministic fake-pipeline tests for docs/validation/golden_runner.py —
SQL rebuild Phase 5, Step 1.

Proves the evaluation harness itself is correct BEFORE it is ever pointed
at the real pipeline: every OrchestrationResult here is hand-built by this
file's own factory helpers, with a controlled, fixed `durations` value —
zero real AI provider calls, zero real SQL Server, zero wall-clock
dependency in any assertion (see test_latency_classification_is_deterministic
for why the harness's own perf_counter wrapper is never asserted on
directly).

Run:
    venv/Scripts/pytest tests/test_golden_runner.py -v
"""
from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

_VALIDATION_DIR = Path(__file__).resolve().parent.parent / "docs" / "validation"
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from golden_case_model import GoldenCase, RowShapeExpectation
from golden_runner import (
    CATEGORY_CORRECTNESS,
    CATEGORY_GROUNDING,
    CATEGORY_PERFORMANCE,
    CATEGORY_RELIABILITY,
    CaseResult,
    EvaluationSummary,
    evaluate_case,
    run_golden_suite,
    summary_to_json,
)

from core.sqlservice.answer_generation_service import AnswerInsight, EvidenceColumnReference
from core.sqlservice.ai_first_result import (
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_GENERATION_FAILED,
    STATUS_OK,
    AiFirstOrchestrationResult as OrchestrationResult,
    AiFirstStageDurations,
)
from core.sqlservice.schema_context_retrieval import ContextDocument

SOURCE_ID = 1


# ── Fake-result factories (this file's own deterministic fixtures) ────────


def _durations(total_ms=1500, retrieval_ms=200, generation_ms=600, validation_ms=50, execution_ms=400, answer_ms=250):
    return AiFirstStageDurations(
        frontier_ms=retrieval_ms, selection_ms=None, relationship_ms=None, context_ms=None,
        ambiguity_guard_ms=None, generation_ms=generation_ms, validation_ms=validation_ms,
        execution_ms=execution_ms, answer_ms=answer_ms, total_ms=total_ms,
    )


def _insight(title="Trend", finding="Values rose.", row_numbers=(1,), columns=((1, "col_a"),)):
    return AnswerInsight(
        title=title, finding=finding,
        evidence_row_numbers=tuple(row_numbers),
        evidence_columns=tuple(EvidenceColumnReference(position=p, name=n) for p, n in columns),
    )


def _context_document(object_fqn):
    return ContextDocument(
        object_fqn=object_fqn, object_type="TABLE", retrieval_tier=1,
        document_hash="deadbeef", document_text="irrelevant schema prose",
        metadata={}, score=0.9, selection_reason="SELECTION_REASON_SEMANTIC",
    )


def _make_result(
    *,
    status=STATUS_OK,
    answer="There are 42 students.",
    insights=(),
    canonical_referenced_object_fqns=("dbo.ADF_Student",),
    retrieval_documents=None,
    columns=("count",),
    rows=((42,),),
    row_count=1,
    truncated=False,
    truncation_reason=None,
    durations=None,
    clarification_question=None,
    error=None,
    error_category=None,
    sql="SELECT COUNT(*) AS count FROM dbo.ADF_Student",
) -> OrchestrationResult:
    if retrieval_documents is None:
        retrieval_documents = tuple(_context_document(fqn) for fqn in canonical_referenced_object_fqns)
    return OrchestrationResult(
        status=status, source_id=SOURCE_ID, build_id=1, snapshot_id=1, schema_fingerprint="fp-1",
        answer=answer, insights=insights, caveats=(), follow_up_questions=(),
        clarification_question=clarification_question, interpretation="A student count.",
        sql=sql, canonical_referenced_object_fqns=tuple(canonical_referenced_object_fqns),
        columns=columns, rows=rows, row_count=row_count, truncated=truncated,
        truncation_reason=truncation_reason, retrieval_documents=retrieval_documents,
        frontier_size=0, selector_status=None, selected_object_fqns=(), connector_object_fqns=(),
        selector_provider_id="fake", selector_model="fake-selector",
        embedding_provider_id="fake", embedding_model="fake-embed", sql_provider_id="fake",
        sql_model="fake-sql", answer_provider_id="fake", answer_model="fake-answer",
        warnings=(), error=error, error_category=error_category,
        durations=durations if durations is not None else _durations(),
    )


def _case(**overrides) -> GoldenCase:
    fields = {
        "case_id": "GC-001",
        "category": "counts",
        "question": "How many students are there?",
        "expected_status": STATUS_OK,
        "required_object_fqns": {"dbo.ADF_Student"},
    }
    fields.update(overrides)
    return GoldenCase(**fields)


# ── 1. All-pass suite ───────────────────────────────────────────────────────


def test_all_pass_suite():
    cases = (
        _case(case_id="GC-01"),
        _case(case_id="GC-02", question="How many active classes?"),
    )

    def answer_question(case):
        return _make_result()

    summary = run_golden_suite(cases, answer_question)

    assert summary.total == 2
    assert summary.passed == 2
    assert summary.failed == 0
    assert summary.correctness_failures == 0
    assert summary.reliability_failures == 0
    assert summary.grounding_failures == 0
    assert all(r.passed for r in summary.case_results)


# ── 2. Multiple failures collected (never stops early) ─────────────────────


def test_multiple_failures_are_all_collected_and_every_case_runs():
    cases = (
        _case(case_id="GC-01", expected_status=STATUS_GENERATION_FAILED),  # will mismatch
        _case(case_id="GC-02", required_object_fqns={"dbo.NotThere"}),      # will mismatch
        _case(case_id="GC-03"),                                             # will pass
    )

    def answer_question(case):
        return _make_result()

    summary = run_golden_suite(cases, answer_question)

    assert summary.total == 3
    assert {r.case_id for r in summary.case_results} == {"GC-01", "GC-02", "GC-03"}
    assert summary.passed == 1
    assert summary.failed == 2
    by_id = {r.case_id: r for r in summary.case_results}
    assert not by_id["GC-01"].passed
    assert not by_id["GC-02"].passed
    assert by_id["GC-03"].passed


# ── 3. Status mismatch ──────────────────────────────────────────────────────


def test_status_mismatch_is_a_correctness_failure():
    case = _case(expected_status=STATUS_OK)
    result = evaluate_case(case, lambda c: _make_result(status=STATUS_GENERATION_FAILED, canonical_referenced_object_fqns=(), columns=(), rows=(), row_count=0))
    assert not result.passed
    assert any(f.check == "status" and f.category == CATEGORY_CORRECTNESS for f in result.failures)


# ── 4. Referenced-object mismatch ───────────────────────────────────────────


def test_missing_required_object_is_a_correctness_failure():
    case = _case(required_object_fqns={"dbo.ADF_Student", "dbo.ADF_Path"})
    result = evaluate_case(case, lambda c: _make_result(canonical_referenced_object_fqns=("dbo.ADF_Student",)))
    assert not result.passed
    failure = next(f for f in result.failures if f.check == "required_objects")
    assert failure.category == CATEGORY_CORRECTNESS
    assert "dbo.ADF_Path" in failure.message


def test_object_outside_allow_list_is_a_correctness_failure():
    case = _case(required_object_fqns=set(), allowed_object_fqns={"dbo.ADF_Student"})
    result = evaluate_case(case, lambda c: _make_result(canonical_referenced_object_fqns=("dbo.ADF_Student", "dbo.Unexpected")))
    assert not result.passed
    assert any(f.check == "allowed_objects" for f in result.failures)


# ── 5. Forbidden archive/system object detection ────────────────────────────


def test_forbidden_object_in_validated_sql_is_detected():
    case = _case(forbidden_object_fqns={"dbo.ADF_ClassEvent_old"})
    result = evaluate_case(
        case,
        lambda c: _make_result(canonical_referenced_object_fqns=("dbo.ADF_Student", "dbo.ADF_ClassEvent_old")),
    )
    assert not result.passed
    failure = next(f for f in result.failures if f.check == "forbidden_objects")
    assert failure.category == CATEGORY_CORRECTNESS
    assert "dbo.ADF_ClassEvent_old" in failure.message


def test_forbidden_object_merely_retrieved_but_not_validated_is_still_detected():
    """Leakage at retrieval time must be caught even if validation itself
    never included the forbidden table in the final SQL."""
    case = _case(forbidden_object_fqns={"dbo.CB_SUBSCRIPTION_PAYMENTS_backup_07062016"})
    result = evaluate_case(
        case,
        lambda c: _make_result(
            canonical_referenced_object_fqns=("dbo.ADF_Student",),
            retrieval_documents=(
                _context_document("dbo.ADF_Student"),
                _context_document("dbo.CB_SUBSCRIPTION_PAYMENTS_backup_07062016"),
            ),
        ),
    )
    assert not result.passed
    assert any(f.check == "forbidden_objects" for f in result.failures)


# ── 6. Result-shape mismatch ─────────────────────────────────────────────────


def test_exact_row_count_mismatch_is_a_correctness_failure():
    case = _case(expected_row_shape=RowShapeExpectation(exact_row_count=1))
    result = evaluate_case(case, lambda c: _make_result(row_count=0, rows=()))
    assert not result.passed
    assert any(f.check == "row_count" for f in result.failures)


def test_required_column_missing_is_a_correctness_failure():
    case = _case(expected_row_shape=RowShapeExpectation(required_column_names=("PathName",)))
    result = evaluate_case(case, lambda c: _make_result(columns=("count",)))
    assert not result.passed
    assert any(f.check == "required_columns" for f in result.failures)


def test_duplicate_columns_required_but_absent_is_a_correctness_failure():
    case = _case(expected_row_shape=RowShapeExpectation(expect_duplicate_column_names=True))
    result = evaluate_case(case, lambda c: _make_result(columns=("a", "b")))
    assert not result.passed
    assert any(f.check == "duplicate_columns" for f in result.failures)


def test_duplicate_columns_detected_when_present():
    case = _case(expected_row_shape=RowShapeExpectation(expect_duplicate_column_names=True))
    result = evaluate_case(case, lambda c: _make_result(columns=("Name", "name"), rows=(("a", "b"),)))
    assert result.passed


def test_unwanted_duplicate_columns_is_a_correctness_failure():
    case = _case(expected_row_shape=RowShapeExpectation(expect_duplicate_column_names=False))
    result = evaluate_case(case, lambda c: _make_result(columns=("Name", "name")))
    assert not result.passed
    assert any(f.check == "duplicate_columns" for f in result.failures)


def test_truncation_mismatch_is_a_correctness_failure():
    case = _case(expected_row_shape=RowShapeExpectation(expected_truncated=True))
    result = evaluate_case(case, lambda c: _make_result(truncated=False))
    assert not result.passed
    assert any(f.check == "truncation" for f in result.failures)


# ── 7. Missing/invalid insight evidence ──────────────────────────────────────


def test_requires_insights_but_none_produced_is_a_grounding_failure():
    case = _case(requires_insights=True)
    result = evaluate_case(case, lambda c: _make_result(insights=()))
    assert not result.passed
    failure = next(f for f in result.failures if f.check == "insights_presence")
    assert failure.category == CATEGORY_GROUNDING


def test_insight_missing_evidence_rows_is_a_grounding_failure():
    case = _case(requires_insights=True, requires_evidence=True)
    bad_insight = AnswerInsight(title="Bad", finding="No evidence.", evidence_row_numbers=(), evidence_columns=(EvidenceColumnReference(1, "count"),))
    result = evaluate_case(case, lambda c: _make_result(insights=(bad_insight,)))
    assert not result.passed
    failure = next(f for f in result.failures if f.check == "insight_evidence")
    assert failure.category == CATEGORY_GROUNDING


def test_insight_missing_evidence_columns_is_a_grounding_failure():
    case = _case(requires_insights=True, requires_evidence=True)
    bad_insight = AnswerInsight(title="Bad", finding="No columns.", evidence_row_numbers=(1,), evidence_columns=())
    result = evaluate_case(case, lambda c: _make_result(insights=(bad_insight,)))
    assert not result.passed
    assert any(f.check == "insight_evidence" for f in result.failures)


def test_well_grounded_insight_passes():
    case = _case(requires_insights=True, requires_evidence=True)
    result = evaluate_case(case, lambda c: _make_result(insights=(_insight(),)))
    assert result.passed


# ── 8. Clarification / zero-row cases ────────────────────────────────────────


def test_clarification_case_passes_when_status_matches():
    case = _case(
        expected_status=STATUS_CLARIFICATION_REQUIRED, expects_clarification=True,
        required_object_fqns=set(),
    )
    result = evaluate_case(
        case,
        lambda c: _make_result(
            status=STATUS_CLARIFICATION_REQUIRED, clarification_question="Which table do you mean?",
            canonical_referenced_object_fqns=(), columns=(), rows=(), row_count=0, sql="",
        ),
    )
    assert result.passed


def test_clarification_case_fails_when_pipeline_answers_instead():
    case = _case(
        expected_status=STATUS_CLARIFICATION_REQUIRED, expects_clarification=True,
        required_object_fqns=set(),
    )
    result = evaluate_case(case, lambda c: _make_result(status=STATUS_OK))
    assert not result.passed
    assert any(f.check == "status" for f in result.failures)
    assert any(f.check == "clarification_expected" for f in result.failures)


def test_zero_row_case_passes_when_row_count_is_zero():
    case = _case(expects_zero_rows=True, expected_row_shape=RowShapeExpectation(exact_row_count=0))
    result = evaluate_case(case, lambda c: _make_result(row_count=0, rows=()))
    assert result.passed


def test_zero_row_case_fails_when_rows_are_returned():
    case = _case(expects_zero_rows=True)
    result = evaluate_case(case, lambda c: _make_result(row_count=5, rows=tuple((i,) for i in range(5))))
    assert not result.passed
    assert any(f.check == "zero_rows_expected" for f in result.failures)


# ── 9. Stage and total latency classification ────────────────────────────────


def test_fast_case_is_not_flagged_slow():
    case = _case(latency_target_seconds=5.0)
    result = evaluate_case(case, lambda c: _make_result(durations=_durations(total_ms=1000)))
    assert result.passed
    assert result.latency.exceeded_target is False
    assert not any(f.category == CATEGORY_PERFORMANCE for f in result.failures)


def test_slow_case_is_flagged_but_still_passes_when_otherwise_correct():
    case = _case(latency_target_seconds=2.0)
    result = evaluate_case(case, lambda c: _make_result(durations=_durations(total_ms=8000)))
    assert result.latency.exceeded_target is True
    assert any(f.check == "latency" and f.category == CATEGORY_PERFORMANCE for f in result.failures)
    # A performance-only finding never flips `passed` — see module docstring.
    assert result.passed is True


def test_slow_case_contributes_to_performance_failures_not_failed_count():
    case = _case(latency_target_seconds=2.0)
    summary = run_golden_suite((case,), lambda c: _make_result(durations=_durations(total_ms=8000)))
    assert summary.performance_failures == 1
    assert summary.failed == 0
    assert summary.slow_case_ids == (case.case_id,)


def test_per_stage_durations_are_echoed_onto_the_latency_report():
    case = _case()
    durations = _durations(total_ms=1234, retrieval_ms=100, generation_ms=500, validation_ms=34, execution_ms=400, answer_ms=200)
    result = evaluate_case(case, lambda c: _make_result(durations=durations))
    assert result.latency.retrieval_ms == 100
    assert result.latency.generation_ms == 500
    assert result.latency.validation_ms == 34
    assert result.latency.execution_ms == 400
    assert result.latency.answer_ms == 200
    assert result.latency.total_ms == 1234
    assert isinstance(result.latency.harness_total_ms, int)
    assert result.latency.harness_total_ms >= 0


# ── 10. Exception isolation per case ─────────────────────────────────────────


def test_exception_in_one_case_is_isolated_and_reported_as_reliability_failure():
    def flaky_answer_question(case):
        if case.case_id == "GC-BOOM":
            raise RuntimeError("Server=tcp:sql.internal;Password=hunter2 connection failed")
        return _make_result()

    cases = (_case(case_id="GC-OK-1"), _case(case_id="GC-BOOM"), _case(case_id="GC-OK-2"))
    summary = run_golden_suite(cases, flaky_answer_question)

    assert summary.total == 3
    by_id = {r.case_id: r for r in summary.case_results}
    assert by_id["GC-OK-1"].passed
    assert by_id["GC-OK-2"].passed
    boom = by_id["GC-BOOM"]
    assert not boom.passed
    assert boom.raised_exception == "RuntimeError"
    failure = next(f for f in boom.failures if f.check == "exception")
    assert failure.category == CATEGORY_RELIABILITY
    assert "hunter2" not in failure.message
    assert "Password=[REDACTED]" in failure.message


def test_exception_case_result_has_safe_placeholder_fields():
    result = evaluate_case(_case(), lambda c: (_ for _ in ()).throw(ValueError("boom")))
    assert result.actual_status is None
    assert result.sql is None
    assert result.answer is None
    assert result.row_count is None
    assert result.canonical_referenced_object_fqns == ()
    assert result.insight_count == 0
    assert result.latency.total_ms is None
    assert result.latency.exceeded_target is False


# ── 11. Deterministic aggregate metrics ──────────────────────────────────────


def test_aggregate_metrics_are_deterministic_across_repeated_runs():
    cases = (
        _case(case_id="GC-01"),
        _case(case_id="GC-02", expected_status=STATUS_GENERATION_FAILED),
        _case(case_id="GC-03", requires_insights=True),
    )

    def answer_question(case):
        return _make_result(insights=())

    first = run_golden_suite(cases, answer_question)
    second = run_golden_suite(cases, answer_question)

    assert first.total == second.total
    assert first.passed == second.passed
    assert first.failed == second.failed
    assert first.correctness_failures == second.correctness_failures
    assert first.grounding_failures == second.grounding_failures
    assert first.reliability_failures == second.reliability_failures
    assert first.performance_failures == second.performance_failures
    assert first.slow_case_ids == second.slow_case_ids


# ── 12. JSON serialization and secret exclusion ──────────────────────────────


def test_summary_is_json_serializable_and_scrubs_secrets():
    def flaky_answer_question(case):
        raise RuntimeError("uid=sa;password=SuperSecret123;Server=tcp:10.0.0.5,1433")

    summary = run_golden_suite((_case(),), flaky_answer_question)
    payload = summary_to_json(summary)

    serialized = json.dumps(payload)  # must not raise
    assert "SuperSecret123" not in serialized
    assert "10.0.0.5" not in serialized
    assert "[REDACTED]" in serialized


def test_json_report_never_includes_raw_rows_or_document_text():
    result_with_pii_like_rows = _make_result(
        rows=(("student@example.com",),), columns=("Email",),
        retrieval_documents=(_context_document("dbo.ADF_Student"),),
    )
    summary = run_golden_suite((_case(),), lambda c: result_with_pii_like_rows)
    payload = summary_to_json(summary)
    serialized = json.dumps(payload)

    assert "student@example.com" not in serialized
    assert "irrelevant schema prose" not in serialized  # document_text must never leak
    # sql/answer ARE expected in the report (not secrets) — only rows/doc text are excluded
    assert payload["cases"][0]["sql"] is not None


def test_json_report_shape_has_expected_top_level_keys():
    summary = run_golden_suite((_case(),), lambda c: _make_result())
    payload = summary_to_json(summary)
    assert set(payload.keys()) == {
        "total", "passed", "failed", "correctness_failures", "reliability_failures",
        "grounding_failures", "performance_failures", "slow_case_ids", "cases",
    }
    assert set(payload["cases"][0].keys()) == {
        "case_id", "category", "passed", "actual_status", "raised_exception",
        "row_count", "insight_count", "canonical_referenced_object_fqns",
        "sql", "answer", "latency", "failures",
    }


# ── 13. Immutable results ────────────────────────────────────────────────────


def test_case_result_is_frozen():
    result = evaluate_case(_case(), lambda c: _make_result())
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_evaluation_summary_is_frozen():
    summary = run_golden_suite((_case(),), lambda c: _make_result())
    with pytest.raises(FrozenInstanceError):
        summary.total = 999  # type: ignore[misc]


def test_case_result_collections_are_tuples():
    result = evaluate_case(_case(), lambda c: _make_result())
    assert isinstance(result.failures, tuple)
    assert isinstance(result.canonical_referenced_object_fqns, tuple)


def test_evaluation_summary_collections_are_tuples():
    summary = run_golden_suite((_case(),), lambda c: _make_result())
    assert isinstance(summary.case_results, tuple)
    assert isinstance(summary.slow_case_ids, tuple)


def test_case_result_cannot_be_mutated_via_replace_without_new_instance():
    """dataclasses.replace() must return a NEW instance — the original is
    never mutated in place."""
    result = evaluate_case(_case(), lambda c: _make_result())
    copy = replace(result, passed=False)
    assert result.passed is True
    assert copy.passed is False
    assert copy is not result
