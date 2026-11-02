"""
Declarative golden-case evaluation model — SQL rebuild Phase 5, Step 1.

Pure data: a `GoldenCase` describes one representative business question and
the STRUCTURAL outcome a correct answer must produce against the new SQL
pipeline (core.sqlservice.ai_first_result.AiFirstOrchestrationResult) —
never the exact generated SQL text (see docs/validation/golden_runner.py's
own scope note for why: exact-SQL assertions are brittle against any
equivalent rewrite, so this suite grades outcomes, not wording).

This module owns only the SHAPE of a case and its expected-result
constraints. It has zero opinion about any particular data source, never
imports anything CCPP-specific, and is never imported by core/sqlservice —
dataset-specific expectations belong in a fixture module (e.g.
docs/validation/ccpp_golden_cases_v2.py), never here and never in
production code.

Every collection field is normalized to an immutable type (frozenset or
tuple) in __post_init__, so a GoldenCase, once constructed, cannot be
mutated later through a list/set reference the caller still holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

DEFAULT_LATENCY_TARGET_SECONDS = 5.0

__all__ = [
    "DEFAULT_LATENCY_TARGET_SECONDS",
    "RowShapeExpectation",
    "GoldenCase",
]


def _as_frozenset(value: Optional[Iterable[str]]) -> frozenset:
    if value is None:
        return frozenset()
    return frozenset(value)


def _as_tuple(value: Optional[Iterable[str]]) -> tuple:
    if value is None:
        return ()
    return tuple(value)


@dataclass(frozen=True)
class RowShapeExpectation:
    """
    Structural constraints on an OrchestrationResult's execution shape —
    never a constraint on the SQL text that produced it.

    min_rows/max_rows/exact_row_count: row_count bounds. When
        exact_row_count is set, min_rows/max_rows are ignored by the runner
        (both may still be set for documentation, but exact_row_count wins).
    min_columns/max_columns: len(columns) bounds.
    required_column_names: column names (case-insensitive) that must all
        appear somewhere in `columns` — the structural analogue of an
        "expected columns" list, checked against the actual EXECUTED result
        shape rather than the SQL text.
    expect_duplicate_column_names: True requires at least one repeated
        (case-insensitive) name in `columns`; False requires all names
        unique; None (the default) means "don't care" — most questions have
        no opinion either way.
    expected_truncated: True/False asserts SqlExecutionResult.truncated;
        None (the default) means "don't care" — truncation is a data-volume
        artifact of the live result, not a property of the question itself,
        for most cases.
    """

    min_rows: Optional[int] = None
    max_rows: Optional[int] = None
    exact_row_count: Optional[int] = None
    min_columns: Optional[int] = None
    max_columns: Optional[int] = None
    required_column_names: tuple = ()
    expect_duplicate_column_names: Optional[bool] = None
    expected_truncated: Optional[bool] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_column_names", _as_tuple(self.required_column_names))
        for name, value in (
            ("min_rows", self.min_rows), ("max_rows", self.max_rows),
            ("exact_row_count", self.exact_row_count),
            ("min_columns", self.min_columns), ("max_columns", self.max_columns),
        ):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative int or None, got {value!r}")
        if self.min_rows is not None and self.max_rows is not None and self.min_rows > self.max_rows:
            raise ValueError(f"min_rows ({self.min_rows}) must be <= max_rows ({self.max_rows})")
        if self.min_columns is not None and self.max_columns is not None and self.min_columns > self.max_columns:
            raise ValueError(f"min_columns ({self.min_columns}) must be <= max_columns ({self.max_columns})")
        for name, value in (
            ("expect_duplicate_column_names", self.expect_duplicate_column_names),
            ("expected_truncated", self.expected_truncated),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be a bool or None, got {value!r}")


@dataclass(frozen=True)
class GoldenCase:
    """
    One declarative golden business question and the structural outcome a
    correct answer must produce. See docs/validation/golden_runner.py for
    how each field is checked against an actual OrchestrationResult.

    case_id: stable, unique identifier — never reused, never renumbered
        (same immutability discipline as docs/validation/
        enterprise_business_validation_suite.v1.json's own `id` policy).
    category: a free-text grouping label for per-category reporting — never
        validated against a fixed enum here, so a fixture file can
        introduce new categories without touching this module.
    question: the literal natural-language question text.
    expected_status: the OrchestrationResult.status value a correct run
        must produce (e.g. "OK", "CLARIFICATION_REQUIRED"). A plain string —
        this module carries no import of core.sqlservice at all, so it
        never depends on any particular pipeline version's exact constant
        object; the runner is the layer that compares it against the real
        STATUS_* constants.
    required_object_fqns: object_fqn values that MUST all appear in
        OrchestrationResult.canonical_referenced_object_fqns for the case to
        pass. Only enforced when non-empty — a case whose expected_status
        never reaches a validated SQL statement (e.g.
        CLARIFICATION_REQUIRED) naturally leaves this empty.
    allowed_object_fqns: when set, every canonical_referenced_object_fqns
        entry must be a member of this set (nothing outside the allow-list)
        — None means "no restriction beyond required/forbidden".
    forbidden_object_fqns: object_fqn values that must NEVER appear in
        canonical_referenced_object_fqns OR in retrieval_documents (catches
        leakage even when validation itself would have pruned it) — the
        structural check behind "similarly named archive/backup-table
        avoidance" and "Tier 3/4 avoidance".
    required_answer_terms: substrings that must all appear in `answer`
        (case-insensitive) — a coarse relevance check that never depends on
        exact wording.
    required_answer_patterns: regex patterns that must all `re.search`-match
        `answer`.
    expected_row_shape: see RowShapeExpectation; None means no shape
        constraint beyond whatever expects_zero_rows implies.
    requires_insights: True requires at least one AnswerInsight in
        OrchestrationResult.insights.
    requires_evidence: True additionally requires every one of those
        insights to carry non-empty evidence_row_numbers AND
        evidence_columns — the structural check behind "missing/invalid
        insight evidence". Requires requires_insights=True (enforced below
        — evidence without insights is a contradiction).
    expects_clarification: documents that this case is a clarification
        scenario. By fixture convention expected_status should then be the
        pipeline's clarification-required status string — this dataclass
        does not enforce that pairing (it has no production import to
        compare against); the runner checks the actual result directly.
    expects_zero_rows: documents that a correct run legitimately returns
        zero rows (row_count == 0) — distinct from a failure.
    latency_target_seconds: the total-latency budget this case is graded
        against for REPORTING purposes only. Exceeding it is a performance
        finding, never a correctness/CI failure, since wall-clock speed is
        machine/network-dependent.
    notes: free-text context — grounding evidence for the expected fields,
        known caveats, or why a category is only lightly exercised by this
        particular case.
    """

    case_id: str
    category: str
    question: str
    expected_status: str
    required_object_fqns: frozenset = field(default_factory=frozenset)
    allowed_object_fqns: Optional[frozenset] = None
    forbidden_object_fqns: frozenset = field(default_factory=frozenset)
    required_answer_terms: tuple = ()
    required_answer_patterns: tuple = ()
    expected_row_shape: Optional[RowShapeExpectation] = None
    requires_insights: bool = False
    requires_evidence: bool = False
    expects_clarification: bool = False
    expects_zero_rows: bool = False
    latency_target_seconds: float = DEFAULT_LATENCY_TARGET_SECONDS
    notes: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("case_id", self.case_id), ("category", self.category),
            ("question", self.question), ("expected_status", self.expected_status),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")

        object.__setattr__(self, "required_object_fqns", _as_frozenset(self.required_object_fqns))
        object.__setattr__(self, "forbidden_object_fqns", _as_frozenset(self.forbidden_object_fqns))
        if self.allowed_object_fqns is not None:
            object.__setattr__(self, "allowed_object_fqns", _as_frozenset(self.allowed_object_fqns))
        object.__setattr__(self, "required_answer_terms", _as_tuple(self.required_answer_terms))
        object.__setattr__(self, "required_answer_patterns", _as_tuple(self.required_answer_patterns))

        overlap = self.required_object_fqns & self.forbidden_object_fqns
        if overlap:
            raise ValueError(f"required_object_fqns and forbidden_object_fqns overlap: {sorted(overlap)}")

        if self.requires_evidence and not self.requires_insights:
            raise ValueError("requires_evidence=True requires requires_insights=True")

        if (
            not isinstance(self.latency_target_seconds, (int, float))
            or isinstance(self.latency_target_seconds, bool)
            or self.latency_target_seconds <= 0
        ):
            raise ValueError(
                f"latency_target_seconds must be a positive number, got {self.latency_target_seconds!r}"
            )

        if self.expected_row_shape is not None and not isinstance(self.expected_row_shape, RowShapeExpectation):
            raise ValueError(
                f"expected_row_shape must be a RowShapeExpectation or None, got {self.expected_row_shape!r}"
            )
