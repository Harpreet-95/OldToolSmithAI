"""
Data-leakage guard tests — SQL rebuild Phase 5, Step 9 / 11B / 11C.

The 15 CCPP golden evaluation questions (docs/validation/
ccpp_golden_cases_v2.py) and their SQL/expected-object definitions must
NEVER be inserted into the example index used to evaluate those same
cases. This test enforces that rule against
docs/validation/sql_example_training_fixtures.py (the only place
PROVENANCE_DEVELOPER_VERIFICATION_SET examples may be authored) and against
the reusable checker/population-guard functions themselves.

Step 11C: the actual detection/blocking implementation
(find_golden_leakage / find_close_copy_leakage /
find_golden_leakage_in_generated_examples / block_population_on_golden_
leakage / GoldenLeakagePopulationBlocked) has moved to
docs/validation/sql_example_population_guard.py — a live population script
imports THAT module directly, never this test file. This file now only
imports and exercises it; see that module's own docstring for why it lives
under docs/validation/ (a real, non-test validation tool) rather than here.

Pure/offline: no database, no OpenAI call, no live SQL Server.

Run:
    venv/Scripts/pytest tests/test_sql_example_golden_leakage.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VALIDATION_DIR = Path(__file__).resolve().parents[1] / "docs" / "validation"
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402
from sql_example_training_fixtures import SQL_EXAMPLE_TRAINING_FIXTURES  # noqa: E402
from sql_example_population_guard import (  # noqa: E402
    GoldenLeakagePopulationBlocked,
    _normalize_question,
    block_population_on_golden_leakage,
    find_close_copy_leakage,
    find_golden_leakage,
    find_golden_leakage_in_generated_examples,
)


def test_golden_cases_exist_and_are_the_authoritative_15():
    """Sanity check on the fixture this guard protects against — if this
    ever fails, the golden suite itself changed shape and the guard below
    needs re-review, not silent trust."""
    assert len(CCPP_GOLDEN_CASES) == 15
    assert len({c.case_id for c in CCPP_GOLDEN_CASES}) == 15


def test_training_fixtures_currently_empty_as_specified():
    """Phase 5, Step 9 explicitly does not populate the live/training
    example set — this locks that in as a structural fact, not just an
    assumption, so a future accidental population is caught here too."""
    assert SQL_EXAMPLE_TRAINING_FIXTURES == ()


def test_no_golden_leakage_in_training_fixtures():
    problems = find_golden_leakage(SQL_EXAMPLE_TRAINING_FIXTURES, CCPP_GOLDEN_CASES)
    assert problems == [], "\n".join(problems)


def test_leakage_checker_detects_exact_question_duplicate():
    golden_case = CCPP_GOLDEN_CASES[0]
    poisoned = ({"training_id": "TRAIN-001", "question_pattern": golden_case.question},)
    problems = find_golden_leakage(poisoned, CCPP_GOLDEN_CASES)
    assert len(problems) == 1
    assert golden_case.case_id in problems[0]


def test_leakage_checker_detects_case_insensitive_whitespace_variant():
    golden_case = CCPP_GOLDEN_CASES[0]
    variant = "   " + golden_case.question.upper() + "   "
    poisoned = ({"training_id": "TRAIN-002", "question_pattern": variant},)
    problems = find_golden_leakage(poisoned, CCPP_GOLDEN_CASES)
    assert len(problems) == 1


def test_leakage_checker_detects_embedded_case_id():
    golden_case = CCPP_GOLDEN_CASES[0]
    poisoned = ({"training_id": f"derived-from-{golden_case.case_id}", "question_pattern": "a genuinely different question"},)
    problems = find_golden_leakage(poisoned, CCPP_GOLDEN_CASES)
    assert len(problems) == 1


def test_leakage_checker_passes_genuinely_disjoint_training_set():
    disjoint = (
        {"training_id": "TRAIN-001", "question_pattern": "How many widgets were shipped last quarter?"},
        {"training_id": "TRAIN-002", "question_pattern": "List all active vendor contracts"},
    )
    assert find_golden_leakage(disjoint, CCPP_GOLDEN_CASES) == []


def test_no_source_specific_table_identifiers_in_training_fixtures_module():
    """The training-fixtures module documents its leakage rule in terms of
    the golden suite it protects against (a legitimate, expected reference
    to ccpp_golden_cases_v2.py/CCPP_GOLDEN_CASES by name — this file is a
    test fixture guard, not a production ranking/scoring module), but it
    must never smuggle in an actual CCPP TABLE/BUSINESS identifier ahead of
    a real example being added."""
    source = (_VALIDATION_DIR / "sql_example_training_fixtures.py").read_text(encoding="utf-8")
    for banned in ("ADF_Student", "ADF_Class", "ADF_Path", "ADF_ClassSignups", "CB_SUBSCRIPTION_PAYMENTS"):
        assert banned not in source


# ── SQL rebuild Phase 5, Step 11B/11C tests (against docs/validation.
#    sql_example_population_guard, imported above) ─────────────────────────


class _FakeCandidate:
    def __init__(self, question_pattern: str, content_hash: str):
        self.question_pattern = question_pattern
        self.content_hash = content_hash


class _FakeGeneratedExample:
    """Duck-types core.sqlservice.sql_example_generator.GeneratedSqlExample's
    own `.candidate.question_pattern`/`.candidate.content_hash` shape,
    without needing a real (guard-token-protected) TrustedSqlExampleCandidate
    instance — the module's own _entries_from_generated_examples() reads
    only those two fields."""

    def __init__(self, question_pattern: str, content_hash: str):
        self.candidate = _FakeCandidate(question_pattern, content_hash)


def test_close_copy_detector_catches_minor_wording_change():
    golden_case = CCPP_GOLDEN_CASES[0]
    words = golden_case.question.split()
    # Swap one word for a near-synonym-shaped placeholder -- close, but not
    # exact-after-normalization, so find_golden_leakage() alone misses it.
    words[-1] = words[-1].rstrip("?") + "s?" if words[-1].endswith("?") else words[-1] + "s"
    close_copy = " ".join(words)
    assert _normalize_question(close_copy) != _normalize_question(golden_case.question)

    entries = ({"training_id": "GEN-0", "question_pattern": close_copy},)
    problems = find_close_copy_leakage(entries, CCPP_GOLDEN_CASES)
    assert len(problems) == 1
    assert golden_case.case_id in problems[0]


def test_close_copy_detector_passes_genuinely_disjoint_questions():
    entries = (
        {"training_id": "GEN-0", "question_pattern": "How many widgets were shipped last quarter?"},
        {"training_id": "GEN-1", "question_pattern": "List all active vendor contracts"},
    )
    assert find_close_copy_leakage(entries, CCPP_GOLDEN_CASES) == []


def test_close_copy_detector_skips_entries_already_exact_matches():
    """An exact (post-normalization) match is find_golden_leakage()'s own
    job -- find_close_copy_leakage() must not ALSO report it, so a caller
    combining both checkers' output never double-counts one collision."""
    golden_case = CCPP_GOLDEN_CASES[0]
    entries = ({"training_id": "GEN-0", "question_pattern": golden_case.question},)
    assert find_close_copy_leakage(entries, CCPP_GOLDEN_CASES) == []


def test_generated_examples_adapter_reports_exact_leakage():
    golden_case = CCPP_GOLDEN_CASES[0]
    generated = [_FakeGeneratedExample(golden_case.question, "a" * 64)]
    problems = find_golden_leakage_in_generated_examples(generated, CCPP_GOLDEN_CASES)
    assert len(problems) == 1
    assert golden_case.case_id in problems[0]


def test_generated_examples_adapter_reports_close_copy_leakage():
    golden_case = CCPP_GOLDEN_CASES[0]
    words = golden_case.question.split()
    words[-1] = words[-1] + "-ish"
    close_copy = " ".join(words)
    generated = [_FakeGeneratedExample(close_copy, "b" * 64)]
    problems = find_golden_leakage_in_generated_examples(generated, CCPP_GOLDEN_CASES)
    assert len(problems) == 1


def test_generated_examples_adapter_clean_collection_reports_nothing():
    generated = [
        _FakeGeneratedExample("How many widgets were shipped last quarter?", "c" * 64),
        _FakeGeneratedExample("List all active vendor contracts", "d" * 64),
    ]
    assert find_golden_leakage_in_generated_examples(generated, CCPP_GOLDEN_CASES) == ()


def test_population_guard_blocks_on_any_detected_leakage():
    golden_case = CCPP_GOLDEN_CASES[0]
    generated = [
        _FakeGeneratedExample("a genuinely unrelated question", "e" * 64),
        _FakeGeneratedExample(golden_case.question, "f" * 64),  # the one poisoned entry
    ]
    with pytest.raises(GoldenLeakagePopulationBlocked) as excinfo:
        block_population_on_golden_leakage(generated, CCPP_GOLDEN_CASES)
    assert golden_case.case_id in str(excinfo.value)


def test_population_guard_blocks_entire_batch_not_just_offending_entries():
    """The guard's contract is all-or-nothing -- it must never expose a way
    to insert the clean entries while only dropping the poisoned one: the
    ONLY thing block_population_on_golden_leakage() ever returns is None
    (clean) or a raised exception (blocked) -- there is no third,
    filtered-result return path a caller could partially act on."""
    import inspect

    golden_case = CCPP_GOLDEN_CASES[0]
    generated = [
        _FakeGeneratedExample("a genuinely unrelated question", "e" * 64),
        _FakeGeneratedExample(golden_case.question, "f" * 64),
    ]
    with pytest.raises(GoldenLeakagePopulationBlocked):
        block_population_on_golden_leakage(generated, CCPP_GOLDEN_CASES)

    return_annotation = inspect.signature(block_population_on_golden_leakage).return_annotation
    assert return_annotation in (None, "None")


def test_population_guard_passes_clean_generated_examples():
    generated = [
        _FakeGeneratedExample("How many widgets were shipped last quarter?", "c" * 64),
        _FakeGeneratedExample("List all active vendor contracts", "d" * 64),
    ]
    assert block_population_on_golden_leakage(generated, CCPP_GOLDEN_CASES) is None


def test_leakage_guard_module_never_imported_by_production_modules():
    """The golden-leakage boundary this whole file enforces, checked from
    the PRODUCTION side too: no module under core/, data/, or api/ imports
    docs.validation.sql_example_population_guard (Step 11C's new home for
    the actual implementation), this test module, ccpp_golden_cases_v2, or
    any other docs/validation golden-fixture module. AST-based (not a raw
    substring scan), since production docstrings legitimately mention
    'golden'/'CCPP' in prose."""
    import ast

    project_root = Path(__file__).resolve().parents[1]
    production_dirs = [project_root / "core", project_root / "data", project_root / "api"]
    banned_module_substrings = (
        "test_sql_example_golden_leakage", "sql_example_population_guard",
        "ccpp_golden_cases", "golden_case_model", "golden_runner",
    )

    offending: list[str] = []
    for directory in production_dirs:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            imported_modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.append(node.module)
            for module in imported_modules:
                if module.startswith("tests") or module.startswith("tests."):
                    offending.append(f"{path}: imports {module!r}")
                if any(banned in module for banned in banned_module_substrings):
                    offending.append(f"{path}: imports {module!r}")

    assert offending == [], "\n".join(offending)
