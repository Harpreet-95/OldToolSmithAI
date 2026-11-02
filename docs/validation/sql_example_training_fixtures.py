"""
Developer verification/training set for dynamic few-shot SQL examples —
SQL rebuild Phase 5, Step 9.

This is the ONLY place PROVENANCE_DEVELOPER_VERIFICATION_SET examples may
be authored (see core/sqlservice/sql_example_model.py's trust policy —
this provenance is unconditionally trusted once it clears
core/sqlservice/sql_example_candidate.py's gate, so it is deliberately
kept in one small, reviewable, version-controlled module rather than
scattered insert_trusted_example() call sites).

DATA-LEAKAGE RULE (enforced by tests/test_sql_example_golden_leakage.py,
which fails the build if this rule is ever violated): no entry here may
share a question (exact text, case/whitespace-insensitive) or a
case_id-style tag with any case in
docs/validation/ccpp_golden_cases_v2.py's CCPP_GOLDEN_CASES — this set
exists to TRAIN example retrieval, never to answer the same questions the
golden suite EVALUATES it against. A training question inspired by a
golden case must always be a genuinely different, paraphrased or
differently-scoped question, never a near-duplicate.

Intentionally EMPTY as of Phase 5, Step 9 — this step builds the storage/
retrieval/prompt foundation only ("Do not populate the live example index
... in this step"). See the Step 9 report's "next step" recommendation for
how this set is meant to be populated (a disjoint, automatically-generated
or developer-authored set, never sourced from the golden questions
themselves or from unverified live traffic).

Each entry, once added, should be a plain dict carrying the raw SQL/
question fields (question_pattern, sql, canonical_object_fqns,
canonical_columns, join_path, result_shape) needed to build a real
core.sqlservice.sql_validation.SqlValidationResult (via
validate_sql_candidate() against the target source's actual schema
context) and then a core.sqlservice.sql_example_candidate.
TrustedSqlExampleCandidate (via build_trusted_example_candidate(),
provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET) for
core.sqlservice.sql_example_store.insert_trusted_example() — Step 9B
removed the raw-field insert_example() path entirely; a training fixture
can no longer be inserted without actually clearing the structural/read-
only/referenced-object/normalization gate, exactly like every other
trusted provenance. Plus a `training_id` (this module's own stable
identifier, analogous to GoldenCase.case_id, but in a clearly distinct
namespace/prefix so it can never be confused with a golden case_id).
"""

from __future__ import annotations

__all__ = ["SQL_EXAMPLE_TRAINING_FIXTURES"]

SQL_EXAMPLE_TRAINING_FIXTURES: tuple[dict, ...] = ()
