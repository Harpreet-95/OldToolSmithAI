"""Offline tests for docs/validation/run_acceptance_suite_fast_path.py (no model/DB calls)."""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docs" / "validation"))
import run_acceptance_suite_fast_path as runner  # noqa: E402


def test_suite_loads_98_questions():
    qs = runner.load_suite()
    assert len(qs) == 98 and all("id" in q and "question" in q for q in qs)


def test_smoke_subset_is_small_deterministic_and_covers_outcomes():
    qs = runner.load_suite()
    a, b = runner.pick_smoke(qs), runner.pick_smoke(qs)
    assert [q["id"] for q in a] == [q["id"] for q in b]
    assert len(a) == runner.SMOKE_QUESTION_COUNT
    assert len({q["expected_outcome"] for q in a}) >= 5


@pytest.mark.parametrize("status,bucket", [
    ("ok", "ANSWERED"), ("clarification_required", "CLARIFIED"), ("non_data", "REFUSED_NON_DATA"),
    ("unsupported", "REFUSED_UNSUPPORTED"), ("validation_failed", "REJECTED_BY_VALIDATION"),
    ("execution_failed", "FAILED"), ("timeout", "FAILED"), ("not_enabled", "NOT_ENABLED"),
])
def test_classify(status, bucket):
    assert runner.classify(status, []) == bucket


def test_grading_separates_ambiguity_from_wrong_answers():
    g = runner.grade
    assert g("SUCCESS", "ANSWERED") == ("PASS", None)
    assert g("SUCCESS", "CLARIFIED") == ("FAIL", "UNEXPECTED_CLARIFICATION")
    assert g("SUCCESS", "FAILED") == ("FAIL", None)
    assert g("AMBIGUOUS", "CLARIFIED") == ("PASS", None)
    assert g("REFUSED_SAFE", "REJECTED_BY_VALIDATION") == ("PASS", None)
    assert g("AMBIGUOUS", "ANSWERED") == ("FAIL", "ANSWERED_WHERE_AMBIGUITY_EXPECTED")
    assert g("KNOWN_DEFECT", "CLARIFIED") == ("PASS", "IMPROVED")
    assert g("SUCCESS", "NOT_PREPARED")[0] == "ENV"


def test_full_run_requires_explicit_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["x", "--source-id", "1", "--user-id", "1", "--all"])
    assert runner.main() == 2
    assert "confirm-full" in capsys.readouterr().out


def test_plan_mode_makes_no_calls(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["x", "--source-id", "1", "--user-id", "1", "--plan"])
    assert runner.main() == 0
    assert '"questions": 98' in capsys.readouterr().out


def test_runner_has_no_retired_imports_or_persisted_sql():
    src = (Path(runner.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    for banned in ("core.orchestrator", "intent_resolver", "agent_response_adapters", "question_answering_service"):
        assert not any(banned in (m or "") for m in mods), banned
    # persisted entry fields never include sql / rows / prompt
    assert '"sql"' not in src and "result_preview" not in src and "prompt" not in src.split('"""', 2)[2]


def test_run_one_records_no_rows_or_sql():
    class _Req:
        def __init__(self, **k): pass
    resp = {"agent_status": "ok", "pipeline_status": "OK", "plan_cache_status": "HIT",
            "agent_trace": [{"tool": "sql_validation", "status": "ok"}, {"tool": "sql_execution", "status": "ok"}],
            "perf_trace": {"total_ms": 5, "execution_ms": 3},
            "enterprise_answer": {"source_tables": ["dbo.t"], "result_preview": [{"Name": "Alice Example"}],
                                  "sql": "SELECT secret", "execution_summary": {"row_count": 1}}}
    entry = runner.run_one(lambda body, user: resp, _Req, None, 1,
                           {"id": "X-1", "category": "c", "question": "q", "expected_outcome": "SUCCESS"})
    blob = repr(entry)
    assert "Alice Example" not in blob and "SELECT secret" not in blob
    assert entry["verdict"] == "PASS" and entry["selected_objects"] == ["dbo.t"] and entry["validation"] == "ok"
