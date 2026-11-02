"""
CCPP-specific golden cases for the new SQL pipeline — SQL rebuild Phase 5,
Step 1.

Dataset-specific expectations live here, and ONLY here — never in
core/sqlservice, and never baked into docs/validation/golden_case_model.py
or docs/validation/golden_runner.py (both of those are pipeline/dataset-
agnostic; see their own module docstrings). This module is the fixture.

Provenance: every table/column name below was already confirmed against
the real CCPP metadata cache and recorded in docs/validation/
ccpp_golden_business_questions.v1.json (read live from data/toolsmith.db,
data_source_connections.id=1, display_name='CCPP SQL Server', on
2026-08-09) and cross-checked directly against knowledge_packages/
ccpp_education_recruiting_v1.json's own column listing for this session
(2026-09-05). No table or column name here is invented. That prior suite
targets the OLD pipeline's contract (expected_primary_table/expected_joins,
graded against SQL-planning internals); this suite targets the NEW
pipeline's OrchestrationResult contract (canonical_referenced_object_fqns,
graded against the pipeline's own public, structural result) — the two are
deliberately not merged.

The retrieval-tier classification referenced in a few cases' notes
(Tier 1..4, Tier 4 = backup/archive/history/staging/temp/test/deprecated by
generic token, never a hardcoded table name) is core.sqlservice.
retrieval_documents.classify_retrieval_tier's own existing, dataset-
agnostic rule — this fixture only supplies the CONFIRMED REAL table names
that rule already classifies as Tier 4 for this source; it adds no new
production rule of its own.

This module is data only — no procedural logic beyond simple case
construction. It is never executed against a real database or a real AI
provider by anything in this file (see docs/validation/golden_runner.py
and tests/test_ccpp_golden_cases_v2.py for how it is consumed).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "golden-runner-non-production-secret-1")
os.environ.setdefault("USER_ID_SALT", "golden-runner-non-production-salt-1")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from golden_case_model import GoldenCase, RowShapeExpectation  # noqa: E402

from core.sqlservice.ai_first_result import (  # noqa: E402
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_OK,
)

__all__ = ["CCPP_GOLDEN_CASES"]

# ── Confirmed real object FQNs (see module docstring for provenance) ──────

_STUDENT = "dbo.ADF_Student"
_CLASS = "dbo.ADF_Class"
_CLASS_SIGNUPS = "dbo.ADF_ClassSignups"
_PATH = "dbo.ADF_Path"
_CLASS_EVENT = "dbo.ADF_ClassEvent"
_CLASS_EVENT_OLD = "dbo.ADF_ClassEvent_old"  # Tier 4: "old" token
_SUBSCRIPTION_PAYMENTS = "dbo.CB_SUBSCRIPTION_PAYMENTS"
_SUBSCRIPTION_PAYMENTS_BACKUP = "dbo.CB_SUBSCRIPTION_PAYMENTS_backup_07062016"  # Tier 4: "backup" token
_GRADUATE_SURVEY_ANSWERS = "dbo.ADF_GraduateSurveyAnswers"

CCPP_GOLDEN_CASES = (
    GoldenCase(
        case_id="CCPP2-001",
        category="simple_count",
        question="How many students are there?",
        expected_status=STATUS_OK,
        required_object_fqns={_STUDENT},
        expected_row_shape=RowShapeExpectation(exact_row_count=1),
        latency_target_seconds=5.0,
        notes="Bare entity count on the canonical Student table (StudentID PK). A COUNT(*) query always returns exactly one row.",
    ),
    GoldenCase(
        case_id="CCPP2-002",
        category="filtered_count",
        question="How many active classes are there?",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS},
        expected_row_shape=RowShapeExpectation(exact_row_count=1),
        latency_target_seconds=5.0,
        notes="dbo.ADF_Class.IsActive is a confirmed real boolean flag column (dictionary semantic_type='flag').",
    ),
    GoldenCase(
        case_id="CCPP2-003",
        category="grouped_aggregation",
        question="How many students are in each program?",
        expected_status=STATUS_OK,
        required_object_fqns={_STUDENT, _CLASS_SIGNUPS, _CLASS, _PATH},
        expected_row_shape=RowShapeExpectation(min_rows=1, required_column_names=("PathName",)),
        requires_insights=True,
        requires_evidence=True,
        latency_target_seconds=6.0,
        notes=(
            "Join chain Student->ClassSignups->Class->Path is fully AUTO/trusted end to end per the "
            "confirmed relationship audit; PathName is the real program-label column on dbo.ADF_Path."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-004",
        category="date_filtering",
        question="How many students started last year?",
        expected_status=STATUS_OK,
        required_object_fqns={_STUDENT},
        expected_row_shape=RowShapeExpectation(exact_row_count=1),
        latency_target_seconds=5.0,
        notes=(
            "dbo.ADF_Student has StartDate/EndDate/InsertDate; a correct answer binds 'started' to "
            "StartDate, never EndDate — the confirmed date-verb disambiguation case from the audit."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-005",
        category="trend_time_comparison",
        question="Show enrollment trend by month",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS_SIGNUPS},
        expected_row_shape=RowShapeExpectation(min_rows=1, required_column_names=("InsertDate",)),
        requires_insights=True,
        requires_evidence=True,
        latency_target_seconds=6.0,
        notes="Trend shape over dbo.ADF_ClassSignups.InsertDate (the signup's own insert timestamp).",
    ),
    GoldenCase(
        case_id="CCPP2-006",
        category="student_class_program_joins",
        question="Show students and their enrolled classes",
        expected_status=STATUS_OK,
        required_object_fqns={_STUDENT, _CLASS_SIGNUPS, _CLASS},
        expected_row_shape=RowShapeExpectation(min_rows=1, required_column_names=("ClassName",)),
        latency_target_seconds=6.0,
        notes="Fully trusted (AUTO) two-hop join chain — positive control for ordinary multi-table traversal.",
    ),
    GoldenCase(
        case_id="CCPP2-007",
        category="course_path_relationships",
        question="Show classes and their program",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS, _PATH},
        expected_row_shape=RowShapeExpectation(min_rows=1, required_column_names=("PathName",)),
        latency_target_seconds=5.0,
        notes="dbo.ADF_Class.PathID -> dbo.ADF_Path.PathID is a confirmed AUTO/trusted relationship.",
    ),
    GoldenCase(
        case_id="CCPP2-008",
        category="payment_aggregation",
        question="Total fees paid by month",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS_SIGNUPS},
        expected_row_shape=RowShapeExpectation(min_rows=1, required_column_names=("FeePaid",)),
        requires_insights=True,
        requires_evidence=True,
        latency_target_seconds=6.0,
        notes="dbo.ADF_ClassSignups.FeePaid is a confirmed real metric column; grouped by InsertDate month.",
    ),
    GoldenCase(
        case_id="CCPP2-009",
        category="graduate_survey_analysis",
        question="How many graduate survey answers have been submitted?",
        expected_status=STATUS_OK,
        required_object_fqns={_GRADUATE_SURVEY_ANSWERS},
        expected_row_shape=RowShapeExpectation(exact_row_count=1),
        latency_target_seconds=5.0,
        notes=(
            "dbo.ADF_GraduateSurveyAnswers (GraduateSurveyAnswersID, GraduateSurveyDateEntered, "
            "GraduateSurveyClassSignupsID, GraduateSurveyQuestionsID) is a confirmed real table in the "
            "knowledge-package schema dump — a simple count over it."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-010",
        category="zero_row_result",
        question="List students enrolled in the Antarctic Winter Studies program",
        expected_status=STATUS_OK,
        required_object_fqns={_STUDENT, _CLASS_SIGNUPS, _CLASS, _PATH},
        expected_row_shape=RowShapeExpectation(exact_row_count=0),
        expects_zero_rows=True,
        latency_target_seconds=5.0,
        notes=(
            "'Antarctic Winter Studies' is not a real dbo.ADF_Path.PathName value — a correct run still "
            "resolves and joins every real table/column, executes successfully, and legitimately returns "
            "zero data rows (a LIST shape, unlike a COUNT — zero rows here means the result set itself is "
            "empty, not a row containing 0)."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-011",
        category="ambiguous_clarification",
        question="Show me the data",
        expected_status=STATUS_CLARIFICATION_REQUIRED,
        expects_clarification=True,
        latency_target_seconds=5.0,
        notes="No entity, metric, or table is identifiable from this phrasing — a correct pipeline must ask for clarification rather than guess.",
    ),
    GoldenCase(
        case_id="CCPP2-012",
        category="archive_backup_avoidance",
        question="Show upcoming class events",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS_EVENT},
        forbidden_object_fqns={_CLASS_EVENT_OLD},
        expected_row_shape=RowShapeExpectation(min_rows=0),
        latency_target_seconds=5.0,
        notes=(
            "dbo.ADF_ClassEvent (ClassEventID, DueDate, EndDate, ...) is the confirmed real, active table; "
            "dbo.ADF_ClassEvent_old is a confirmed real, similarly-named copy that must never be selected "
            "for an ordinary business question about class events."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-013",
        category="tier_avoidance",
        question="Show subscription payment records",
        expected_status=STATUS_OK,
        required_object_fqns={_SUBSCRIPTION_PAYMENTS},
        forbidden_object_fqns={_SUBSCRIPTION_PAYMENTS_BACKUP},
        expected_row_shape=RowShapeExpectation(min_rows=0, required_column_names=("AmountPaid",)),
        latency_target_seconds=5.0,
        notes=(
            "dbo.CB_SUBSCRIPTION_PAYMENTS (AmountDue, AmountPaid, ClassSignupsID, DueDate, ...) is the "
            "confirmed real, active table. dbo.CB_SUBSCRIPTION_PAYMENTS_backup_07062016 is a confirmed "
            "real dated backup copy — core.sqlservice.retrieval_documents.classify_retrieval_tier "
            "classifies it Tier 4 by its own generic 'backup' token rule (no CCPP-specific rule was added "
            "for this case)."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-014",
        category="duplicate_columns",
        question="Show classes along with their program name and description",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS, _PATH},
        expected_row_shape=RowShapeExpectation(min_rows=1, required_column_names=("PathName",)),
        latency_target_seconds=5.0,
        notes=(
            "Same trusted Class->Path join as CCPP2-007. This case deliberately does NOT assert "
            "expect_duplicate_column_names either way — whether a specific generated SELECT list happens "
            "to repeat a column name is a property of that one query, not of this question; the harness's "
            "OWN ability to detect/require a duplicate-column shape is proven deterministically in "
            "tests/test_golden_runner.py, independent of any live CCPP result."
        ),
    ),
    GoldenCase(
        case_id="CCPP2-015",
        category="truncation_behavior",
        question="List all enrollments",
        expected_status=STATUS_OK,
        required_object_fqns={_CLASS_SIGNUPS},
        expected_row_shape=RowShapeExpectation(min_rows=0),
        latency_target_seconds=8.0,
        notes=(
            "An unfiltered list over dbo.ADF_ClassSignups may plausibly exceed the execution layer's own "
            "row/character caps and come back truncated=True. This case deliberately does NOT assert "
            "expected_truncated either way (real CCPP row volume for this table was not measured as part "
            "of this audit); the harness's own ability to detect/require a specific truncation outcome is "
            "proven deterministically in tests/test_golden_runner.py."
        ),
    ),
)
