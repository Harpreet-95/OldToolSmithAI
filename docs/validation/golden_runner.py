"""
Declarative golden-case evaluation runner — SQL rebuild Phase 5, Step 1.

Grades a fixed suite of GoldenCase instances (docs/validation.
golden_case_model) against an INJECTED `answer_question` callable that,
given one GoldenCase, returns one core.sqlservice.question_answering_
ai_first_result.AiFirstOrchestrationResult. This module never calls a real AI provider or
a real SQL Server itself — how the callable produces its result (a fully
deterministic fake for this step, the real pipeline in a later milestone)
is entirely the caller's concern.

Design constraints (Phase 5, Step 1 requirements):
  - Every case runs independently; one case raising never stops the suite
    (see evaluate_case's own try/except) or skips any later case.
  - Never asserts exact generated SQL text — only structural outcomes:
    status, canonical_referenced_object_fqns, row/column shape, answer
    presence, and insight grounding (see the _check_* helpers below).
  - Every returned result (CaseResult, EvaluationSummary, LatencyReport,
    ValidationFailure) is a frozen dataclass with only tuple/frozenset
    collection fields — immutable end to end.
  - A latency-target overrun is always reported (see EvaluationSummary.
    slow_case_ids and CATEGORY_PERFORMANCE) but never flips `passed` to
    False on its own — wall-clock speed is machine/network-dependent and
    must never make a deterministic unit test flaky.
  - summary_to_json()'s output is the ONLY sanitized, JSON-serializable
    report surface this module produces: it never includes raw execution
    rows (only row_count/columns), retrieval-document text or metadata
    (only the object_fqn/tier facts needed for the forbidden-object check,
    and even those never reach the report), or unscrubbed error/exception
    text (see _scrub).
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from cryptography.fernet import Fernet

# Test-only defaults — never overrides a real value already present in the
# environment (see core.config's own hard-fail-if-missing validation for
# ENCRYPTION_KEY/JWT_SECRET/USER_ID_SALT). Mirrors the exact pattern every
# tests/test_sqlservice_*.py file already uses so this module can be
# imported standalone (a deterministic-fake run) without a real .env.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "golden-runner-non-production-secret-1")
os.environ.setdefault("USER_ID_SALT", "golden-runner-non-production-salt-1")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from golden_case_model import GoldenCase  # noqa: E402

from core.sqlservice.ai_first_result import (  # noqa: E402
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_OK,
)
from core.sqlservice.ai_first_result import AiFirstOrchestrationResult as OrchestrationResult  # noqa: E402

__all__ = [
    "CATEGORY_CORRECTNESS",
    "CATEGORY_RELIABILITY",
    "CATEGORY_GROUNDING",
    "CATEGORY_PERFORMANCE",
    "ValidationFailure",
    "LatencyReport",
    "CaseResult",
    "EvaluationSummary",
    "AnswerQuestionCallable",
    "evaluate_case",
    "run_golden_suite",
    "summary_to_json",
]

CATEGORY_CORRECTNESS = "correctness"
CATEGORY_RELIABILITY = "reliability"
CATEGORY_GROUNDING = "grounding"
CATEGORY_PERFORMANCE = "performance"

_ALL_CATEGORIES = frozenset({CATEGORY_CORRECTNESS, CATEGORY_RELIABILITY, CATEGORY_GROUNDING, CATEGORY_PERFORMANCE})

_REPORT_TEXT_LIMIT = 2000

AnswerQuestionCallable = Callable[[GoldenCase], OrchestrationResult]


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationFailure:
    """One failed check for one case. `check` is a short, stable machine
    name (e.g. "status", "required_objects", "latency"); `category` is one
    of the CATEGORY_* constants; `message` is a human-readable, already-
    sanitized description safe to include in the JSON report verbatim."""

    check: str
    category: str
    message: str

    def __post_init__(self) -> None:
        if self.category not in _ALL_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(_ALL_CATEGORIES)}, got {self.category!r}")


@dataclass(frozen=True)
class LatencyReport:
    """
    Per-case latency, in milliseconds. The five stage fields and total_ms
    are None-if-never-ran, echoed straight from OrchestrationResult.
    durations (see that module's own docstring: a stage that never ran is
    None, never 0). harness_total_ms is measured by THIS runner, around the
    injected callable — an independent cross-check on total_ms, since it
    also captures any overhead the pipeline's own clock does not (e.g. the
    callable's own dispatch/adapter cost). exceeded_target is computed
    against the case's own latency_target_seconds; a None total_ms (the
    callable raised before producing a result) is never considered exceeded
    — there is nothing to compare.
    """

    retrieval_ms: Optional[int]
    generation_ms: Optional[int]
    validation_ms: Optional[int]
    execution_ms: Optional[int]
    answer_ms: Optional[int]
    total_ms: Optional[int]
    harness_total_ms: int
    exceeded_target: bool


@dataclass(frozen=True)
class CaseResult:
    """
    Immutable outcome of grading one GoldenCase. `passed` is True only when
    every CORRECTNESS/RELIABILITY/GROUNDING failure is absent — a
    PERFORMANCE-only finding never flips this to False (see module
    docstring). `sql`/`answer` are bounded and scrubbed (see _bounded) —
    safe to serialize; `canonical_referenced_object_fqns` is sorted for
    deterministic comparison/reporting.
    """

    case_id: str
    category: str
    passed: bool
    actual_status: Optional[str]
    failures: tuple
    latency: LatencyReport
    raised_exception: Optional[str]
    sql: Optional[str]
    answer: Optional[str]
    row_count: Optional[int]
    canonical_referenced_object_fqns: tuple
    insight_count: int


@dataclass(frozen=True)
class EvaluationSummary:
    """Immutable aggregate over one full suite run. Every *_failures count
    is the number of ValidationFailure entries of that category across all
    cases (a single case can contribute to more than one count).
    slow_case_ids lists every case whose latency exceeded its own target,
    independent of `passed` — see module docstring."""

    total: int
    passed: int
    failed: int
    correctness_failures: int
    reliability_failures: int
    grounding_failures: int
    performance_failures: int
    slow_case_ids: tuple
    case_results: tuple


# ── Sanitization helpers (shared by CaseResult construction and the JSON
#    report) — never expose a connection string, credential, or unbounded
#    free-text blob ──────────────────────────────────────────────────────

_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(password|pwd)\s*=\s*[^;]+",
        r"(server|data source|addr(?:ess)?)\s*=\s*[^;]+",
        r"(uid|user id|user)\s*=\s*[^;]+",
        r"(api[_-]?key|authorization|bearer)\s*[:=]\s*\S+",
    )
)


def _scrub(text: str) -> str:
    scrubbed = text
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", scrubbed)
    return scrubbed


def _bounded(text: Optional[str], limit: int = _REPORT_TEXT_LIMIT) -> Optional[str]:
    if text is None:
        return None
    scrubbed = _scrub(text)
    return scrubbed if len(scrubbed) <= limit else scrubbed[:limit] + "...[truncated]"


# ── Per-check helpers — each returns a list of ValidationFailure, never
#    raises, and never short-circuits the others ───────────────────────────


def _check_status(case: GoldenCase, result: OrchestrationResult) -> list:
    failures = []
    if result.status != case.expected_status:
        failures.append(ValidationFailure(
            "status", CATEGORY_CORRECTNESS,
            f"expected status {case.expected_status!r}, got {result.status!r}",
        ))
    if case.expects_clarification and result.status != STATUS_CLARIFICATION_REQUIRED:
        failures.append(ValidationFailure(
            "clarification_expected", CATEGORY_CORRECTNESS,
            f"case expects clarification but status was {result.status!r}",
        ))
    if case.expects_zero_rows and result.status == STATUS_OK and result.row_count != 0:
        failures.append(ValidationFailure(
            "zero_rows_expected", CATEGORY_CORRECTNESS,
            f"case expects zero rows but row_count was {result.row_count!r}",
        ))
    return failures


def _check_object_selection(case: GoldenCase, result: OrchestrationResult) -> list:
    failures = []
    actual = set(result.canonical_referenced_object_fqns)
    retrieved = {doc.object_fqn for doc in result.retrieval_documents}

    if case.required_object_fqns:
        missing = case.required_object_fqns - actual
        if missing:
            failures.append(ValidationFailure(
                "required_objects", CATEGORY_CORRECTNESS,
                f"missing required object(s): {sorted(missing)}",
            ))

    if case.allowed_object_fqns is not None:
        disallowed = actual - case.allowed_object_fqns
        if disallowed:
            failures.append(ValidationFailure(
                "allowed_objects", CATEGORY_CORRECTNESS,
                f"referenced object(s) outside the allow-list: {sorted(disallowed)}",
            ))

    if case.forbidden_object_fqns:
        forbidden_hit = case.forbidden_object_fqns & (actual | retrieved)
        if forbidden_hit:
            failures.append(ValidationFailure(
                "forbidden_objects", CATEGORY_CORRECTNESS,
                f"forbidden object(s) referenced or retrieved: {sorted(forbidden_hit)}",
            ))

    return failures


def _check_row_shape(case: GoldenCase, result: OrchestrationResult) -> list:
    shape = case.expected_row_shape
    if shape is None:
        return []
    failures = []

    if shape.exact_row_count is not None:
        if result.row_count != shape.exact_row_count:
            failures.append(ValidationFailure(
                "row_count", CATEGORY_CORRECTNESS,
                f"expected exactly {shape.exact_row_count} row(s), got {result.row_count}",
            ))
    else:
        if shape.min_rows is not None and result.row_count < shape.min_rows:
            failures.append(ValidationFailure(
                "row_count", CATEGORY_CORRECTNESS,
                f"expected at least {shape.min_rows} row(s), got {result.row_count}",
            ))
        if shape.max_rows is not None and result.row_count > shape.max_rows:
            failures.append(ValidationFailure(
                "row_count", CATEGORY_CORRECTNESS,
                f"expected at most {shape.max_rows} row(s), got {result.row_count}",
            ))

    column_count = len(result.columns)
    if shape.min_columns is not None and column_count < shape.min_columns:
        failures.append(ValidationFailure(
            "column_count", CATEGORY_CORRECTNESS,
            f"expected at least {shape.min_columns} column(s), got {column_count}",
        ))
    if shape.max_columns is not None and column_count > shape.max_columns:
        failures.append(ValidationFailure(
            "column_count", CATEGORY_CORRECTNESS,
            f"expected at most {shape.max_columns} column(s), got {column_count}",
        ))

    if shape.required_column_names:
        lower_columns = {c.lower() for c in result.columns}
        missing_columns = [c for c in shape.required_column_names if c.lower() not in lower_columns]
        if missing_columns:
            failures.append(ValidationFailure(
                "required_columns", CATEGORY_CORRECTNESS,
                f"missing required column(s): {missing_columns}",
            ))

    if shape.expect_duplicate_column_names is not None:
        lower_names = [c.lower() for c in result.columns]
        has_duplicates = len(lower_names) != len(set(lower_names))
        if shape.expect_duplicate_column_names and not has_duplicates:
            failures.append(ValidationFailure(
                "duplicate_columns", CATEGORY_CORRECTNESS,
                "expected a duplicate column name in the result, but all column names were unique",
            ))
        if not shape.expect_duplicate_column_names and has_duplicates:
            failures.append(ValidationFailure(
                "duplicate_columns", CATEGORY_CORRECTNESS,
                f"expected unique column names, but found duplicates in {list(result.columns)}",
            ))

    if shape.expected_truncated is not None and result.truncated != shape.expected_truncated:
        failures.append(ValidationFailure(
            "truncation", CATEGORY_CORRECTNESS,
            f"expected truncated={shape.expected_truncated}, got {result.truncated}",
        ))

    return failures


def _check_answer(case: GoldenCase, result: OrchestrationResult) -> list:
    failures = []
    answer_text = result.answer or ""
    answer_lower = answer_text.lower()

    missing_terms = [term for term in case.required_answer_terms if term.lower() not in answer_lower]
    if missing_terms:
        failures.append(ValidationFailure(
            "answer_terms", CATEGORY_CORRECTNESS,
            f"answer is missing required term(s): {missing_terms}",
        ))

    unmatched_patterns = [p for p in case.required_answer_patterns if not re.search(p, answer_text)]
    if unmatched_patterns:
        failures.append(ValidationFailure(
            "answer_patterns", CATEGORY_CORRECTNESS,
            f"answer does not match required pattern(s): {unmatched_patterns}",
        ))

    return failures


def _check_insights(case: GoldenCase, result: OrchestrationResult) -> list:
    failures = []

    if case.requires_insights and len(result.insights) == 0:
        failures.append(ValidationFailure(
            "insights_presence", CATEGORY_GROUNDING,
            "case requires at least one insight, but none were produced",
        ))
        return failures  # nothing further to ground-check

    if case.requires_evidence:
        for index, insight in enumerate(result.insights):
            if not insight.evidence_row_numbers or not insight.evidence_columns:
                failures.append(ValidationFailure(
                    "insight_evidence", CATEGORY_GROUNDING,
                    f"insight #{index} ({insight.title!r}) has no grounded evidence "
                    f"(evidence_row_numbers={insight.evidence_row_numbers!r}, "
                    f"evidence_columns={insight.evidence_columns!r})",
                ))

    return failures


def _build_latency(case: GoldenCase, result: OrchestrationResult, harness_ms: int) -> LatencyReport:
    durations = result.durations
    target_ms = case.latency_target_seconds * 1000
    exceeded = durations.total_ms is not None and durations.total_ms > target_ms
    return LatencyReport(
        retrieval_ms=durations.frontier_ms,
        generation_ms=durations.generation_ms,
        validation_ms=durations.validation_ms,
        execution_ms=durations.execution_ms,
        answer_ms=durations.answer_ms,
        total_ms=durations.total_ms,
        harness_total_ms=harness_ms,
        exceeded_target=exceeded,
    )


# ── Public entry points ────────────────────────────────────────────────────


def evaluate_case(case: GoldenCase, answer_question: AnswerQuestionCallable) -> CaseResult:
    """
    Grade exactly one GoldenCase. Never raises: an exception from
    `answer_question` is caught and reported as a single RELIABILITY
    failure (check="exception") rather than propagating — see
    run_golden_suite's own "never stops after the first failure"
    requirement, which this function is what actually guarantees it for.
    """
    start = time.perf_counter()
    try:
        result = answer_question(case)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: isolate ANY case failure
        harness_ms = int((time.perf_counter() - start) * 1000)
        failure = ValidationFailure(
            "exception", CATEGORY_RELIABILITY,
            f"{type(exc).__name__}: {_scrub(str(exc))}",
        )
        latency = LatencyReport(
            retrieval_ms=None, generation_ms=None, validation_ms=None,
            execution_ms=None, answer_ms=None, total_ms=None,
            harness_total_ms=harness_ms, exceeded_target=False,
        )
        return CaseResult(
            case_id=case.case_id, category=case.category, passed=False,
            actual_status=None, failures=(failure,), latency=latency,
            raised_exception=type(exc).__name__, sql=None, answer=None,
            row_count=None, canonical_referenced_object_fqns=(), insight_count=0,
        )

    harness_ms = int((time.perf_counter() - start) * 1000)

    failures: list = []
    failures += _check_status(case, result)
    failures += _check_object_selection(case, result)
    failures += _check_row_shape(case, result)
    failures += _check_answer(case, result)
    failures += _check_insights(case, result)

    latency = _build_latency(case, result, harness_ms)
    if latency.exceeded_target:
        failures.append(ValidationFailure(
            "latency", CATEGORY_PERFORMANCE,
            f"total_ms={latency.total_ms} exceeds target "
            f"{int(case.latency_target_seconds * 1000)}ms",
        ))

    blocking = [f for f in failures if f.category != CATEGORY_PERFORMANCE]
    passed = not blocking

    return CaseResult(
        case_id=case.case_id, category=case.category, passed=passed,
        actual_status=result.status, failures=tuple(failures), latency=latency,
        raised_exception=None, sql=_bounded(result.sql), answer=_bounded(result.answer),
        row_count=result.row_count,
        canonical_referenced_object_fqns=tuple(sorted(result.canonical_referenced_object_fqns)),
        insight_count=len(result.insights),
    )


def run_golden_suite(cases: Sequence[GoldenCase], answer_question: AnswerQuestionCallable) -> EvaluationSummary:
    """
    Grade every case in `cases`, independently, in order. Never stops after
    a failing or raising case — every case in `cases` is always attempted
    exactly once, and the result set always has len(cases) entries.
    """
    case_results = tuple(evaluate_case(case, answer_question) for case in cases)

    def _count(category: str) -> int:
        return sum(1 for r in case_results for f in r.failures if f.category == category)

    passed = sum(1 for r in case_results if r.passed)
    slow_case_ids = tuple(r.case_id for r in case_results if r.latency.exceeded_target)

    return EvaluationSummary(
        total=len(case_results),
        passed=passed,
        failed=len(case_results) - passed,
        correctness_failures=_count(CATEGORY_CORRECTNESS),
        reliability_failures=_count(CATEGORY_RELIABILITY),
        grounding_failures=_count(CATEGORY_GROUNDING),
        performance_failures=_count(CATEGORY_PERFORMANCE),
        slow_case_ids=slow_case_ids,
        case_results=case_results,
    )


def summary_to_json(summary: EvaluationSummary) -> dict:
    """
    The one sanitized, JSON-serializable report surface this module
    produces. Every value is already a plain str/int/float/bool/None/
    list/dict — safe to pass straight to json.dumps(). Never includes raw
    execution rows, retrieval-document text/metadata, or embeddings —
    `sql`/`answer` are the only free-text fields, and both were already
    bounded and credential-scrubbed by evaluate_case (see _bounded).
    """
    return {
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "correctness_failures": summary.correctness_failures,
        "reliability_failures": summary.reliability_failures,
        "grounding_failures": summary.grounding_failures,
        "performance_failures": summary.performance_failures,
        "slow_case_ids": list(summary.slow_case_ids),
        "cases": [_case_result_to_json(r) for r in summary.case_results],
    }


def _case_result_to_json(result: CaseResult) -> dict:
    return {
        "case_id": result.case_id,
        "category": result.category,
        "passed": result.passed,
        "actual_status": result.actual_status,
        "raised_exception": result.raised_exception,
        "row_count": result.row_count,
        "insight_count": result.insight_count,
        "canonical_referenced_object_fqns": list(result.canonical_referenced_object_fqns),
        "sql": result.sql,
        "answer": result.answer,
        "latency": {
            "retrieval_ms": result.latency.retrieval_ms,
            "generation_ms": result.latency.generation_ms,
            "validation_ms": result.latency.validation_ms,
            "execution_ms": result.latency.execution_ms,
            "answer_ms": result.latency.answer_ms,
            "total_ms": result.latency.total_ms,
            "harness_total_ms": result.latency.harness_total_ms,
            "exceeded_target": result.latency.exceeded_target,
        },
        "failures": [
            {"check": f.check, "category": f.category, "message": f.message}
            for f in result.failures
        ],
    }
