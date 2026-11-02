"""
NEXT MILESTONE, Part 25/26 — smoke tests for docs/validation/run_golden_set.py's
_grade_material_correctness().

Root cause: GOLD-S-03 ("List classes that are not active") previously
material-passed with an actual WHERE clause of IsActive = 1 (the OPPOSITE of
what was asked) because the grading function only checked that a column named
"IsActive" appeared SOMEWHERE in the plan — never the operator/value it was
actually filtered to. expected_filters closes that gap: a hand-built sql_plan
dict (no database, no agent, no real question text) proves the harness itself
now catches a polarity mismatch instead of trusting it never regresses.

Run from the project root:
    venv/Scripts/pytest tests/test_golden_set_material_grading.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-golden-set-grading-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-golden-set-grading-salt-long-enough-1")

_SCRIPT_PATH = Path(__file__).parent.parent / "docs" / "validation" / "run_golden_set.py"
_spec = importlib.util.spec_from_file_location("run_golden_set", _SCRIPT_PATH)
runner = importlib.util.module_from_spec(_spec)
sys.modules["run_golden_set"] = runner
_spec.loader.exec_module(runner)


def _sql_plan(where):
    return {
        "select": [{"table_fqn": "dbo.ADF_Class", "column_name": "ClassID"}],
        "where": where,
        "group_by": [],
        "order_by": [],
        "joins": [],
        "from": {"table_fqn": "dbo.ADF_Class"},
    }


def test_negated_question_with_wrong_polarity_fails_material_grade():
    # The exact GOLD-S-03 regression scenario: the question asked for
    # IsActive = 0 (not active) but the plan's actual WHERE clause asserts
    # IsActive = 1 — must FAIL, never material_pass=True.
    q = {
        "expected_primary_table": "dbo.ADF_Class",
        "expected_columns": ["IsActive"],
        "expected_filters": [{"column": "IsActive", "table_fqn": "dbo.ADF_Class", "operator": "=", "value": 0}],
    }
    plan = _sql_plan([{"table_fqn": "dbo.ADF_Class", "column_name": "IsActive", "operator": "=", "value": 1}])

    material_pass, reasons = runner._grade_material_correctness(q, plan)

    assert material_pass is False
    assert any("expected filter" in r for r in reasons)


def test_negated_question_with_correct_polarity_passes_material_grade():
    q = {
        "expected_primary_table": "dbo.ADF_Class",
        "expected_columns": ["IsActive"],
        "expected_filters": [{"column": "IsActive", "table_fqn": "dbo.ADF_Class", "operator": "=", "value": 0}],
    }
    plan = _sql_plan([{"table_fqn": "dbo.ADF_Class", "column_name": "IsActive", "operator": "=", "value": 0}])

    material_pass, reasons = runner._grade_material_correctness(q, plan)

    assert material_pass is True
    assert reasons == []


def test_positive_question_with_correct_polarity_passes():
    q = {
        "expected_primary_table": "dbo.ADF_Class",
        "expected_columns": ["IsActive"],
        "expected_filters": [{"column": "IsActive", "table_fqn": "dbo.ADF_Class", "operator": "=", "value": 1}],
    }
    plan = _sql_plan([{"table_fqn": "dbo.ADF_Class", "column_name": "IsActive", "operator": "=", "value": 1}])

    material_pass, reasons = runner._grade_material_correctness(q, plan)

    assert material_pass is True


def test_question_without_expected_filters_graded_exactly_as_before():
    # expected_filters is opt-in — a question that doesn't specify it must
    # still be graded purely on expected_columns, byte-identical to before
    # this milestone.
    q = {"expected_primary_table": "dbo.ADF_Class", "expected_columns": ["IsActive"]}
    plan = _sql_plan([{"table_fqn": "dbo.ADF_Class", "column_name": "IsActive", "operator": "=", "value": 1}])

    material_pass, reasons = runner._grade_material_correctness(q, plan)

    assert material_pass is True
    assert reasons == []
