"""
CCPP Golden Set runner — Emergency Presentation Stabilization (2026-08-09).

Reads docs/validation/ccpp_golden_business_questions.v1.json and, for every
question, calls the exact same production entry point the real AI Workspace
uses: core.orchestrator.agent.answer_business_question(). This is the SAME
shared entry point docs/validation/run_acceptance_suite.py (the locked
EBVS-98 suite) already drives -- reused deliberately, not reinvented.

What this runner grades that the EBVS runner does NOT:
    EBVS's own grading contract is "was real SQL generated?" (SUCCESS) or
    "did the pipeline safely refuse?" (REFUSED_SAFE/...). That contract
    cannot distinguish a materially WRONG answer that still executes
    successfully (e.g. filtering EndDate instead of StartDate) from a
    correct one -- exactly the class of bug this stabilization pass is
    fixing. This runner additionally inspects state.sql_plan's structured
    select/where/group_by/order_by rows (table_fqn/column_name pairs) and
    grades:
        - expected_primary_table actually appears in the resolved join graph
        - every expected_joins pair actually appears in the join graph
        - every expected_columns entry actually appears as a real
          table_fqn/column_name pair somewhere in the plan
    A question can have sql_generated=True and still be graded FAIL here if
    it picked the wrong table/column -- the whole point of this suite.

Known-gap tracking: each golden question may carry currently_blocked_by,
naming a defect ID from the 2026-08-09 root-cause matrix (see the suite
file's own defect_legend). A failing question with that field set is
reported as an EXPECTED_FAILURE (already diagnosed, tracked, not a
surprise); a failing question with currently_blocked_by=null is reported as
a REGRESSION (new, not yet explained -- investigate before proceeding). A
question that now PASSES despite carrying currently_blocked_by is reported
as IMPROVED (the underlying defect looks fixed -- update the suite file).

Follow-up questions (follow_up_of) are replayed through the same
ConversationContext round-trip production itself uses
(api/v1/agent_response_adapters.build_conversation_state ->
core.orchestrator.agent.ConversationContext.from_dict), not a separate
mechanism -- so a follow-up that doesn't match the agent's own strict
follow-up shape allow-list is graded exactly as a real user typing it would
experience it (treated as a fresh question, context ignored).

This script makes NO writes anywhere itself, but calling
answer_business_question() DOES run its governed LiveQueryEngine execution
step whenever SQL is generated (same as EBVS's runner) -- against
data_source_connections.id=<source-id>'s real, live-query-enabled
connection. Confirm you intend to hit the live CCPP source before running
this against source_id=1.

Usage:
    venv/Scripts/python docs/validation/run_golden_set.py \
        --source-id 1 --user-id 28 \
        [--output docs/validation/results/golden_after.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Same side-effect-import requirement as run_acceptance_suite.py: populates
# core.connectors.registry, normally done by importing api/v1/routes.py at
# app startup.
import core.connectors.relational.mssql       # noqa: E402,F401
import core.connectors.relational.postgresql  # noqa: E402,F401
import core.connectors.relational.mysql       # noqa: E402,F401

_SUITE_PATH = Path(__file__).parent / "ccpp_golden_business_questions.v1.json"

_REFUSAL_OUTCOMES = frozenset({
    "REFUSED_SAFE", "AMBIGUOUS", "CLARIFICATION_NEEDED", "NOT_SUPPORTED",
})

# Same governed-execution rate limit run_acceptance_suite.py already spaces
# for (execute_generated_query enforces a 2s window per user_id).
_EXECUTION_SPACING_S = 2.5


def _load_suite(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["questions"])


def _involved_tables(sql_plan: dict) -> set[str]:
    tables: set[str] = set()
    from_clause = sql_plan.get("from")
    if from_clause:
        # build_sql_plan's "from" is {"table_fqn": ..., "alias": ...}, not a
        # bare string (verified against data/sql_planning_service.py:733).
        tables.add(from_clause["table_fqn"] if isinstance(from_clause, dict) else from_clause)
    for j in sql_plan.get("joins") or []:
        if j.get("left_table"):
            tables.add(j["left_table"])
        if j.get("right_table"):
            tables.add(j["right_table"])
    return tables


def _plan_rows(sql_plan: dict) -> list[dict]:
    rows: list[dict] = []
    for key in ("select", "where", "group_by", "order_by"):
        rows.extend(sql_plan.get(key) or [])
    return rows


def _grade_material_correctness(q: dict, sql_plan: dict) -> tuple[bool, list[str]]:
    """Returns (material_pass, reasons_for_failure). Only meaningful when SQL
    was actually generated -- callers should treat a no-SQL case separately."""
    reasons: list[str] = []
    involved = _involved_tables(sql_plan)
    rows = _plan_rows(sql_plan)

    expected_table = q.get("expected_primary_table")
    if expected_table and expected_table not in involved:
        reasons.append(f"expected_primary_table '{expected_table}' not in resolved join graph {sorted(involved)}")

    for join_pair in q.get("expected_joins") or []:
        a, b = join_pair.split("->")
        if a not in involved or b not in involved:
            reasons.append(f"expected_joins '{join_pair}' not both present in resolved join graph {sorted(involved)}")

    for col in q.get("expected_columns") or []:
        if not any(r.get("column_name") == col and r.get("table_fqn") in involved for r in rows):
            actual_cols = sorted({f"{r.get('table_fqn')}.{r.get('column_name')}" for r in rows if r.get("column_name")})
            reasons.append(f"expected column '{col}' not found in plan; plan actually used {actual_cols}")

    return (len(reasons) == 0, reasons)


def _answer_summary(state) -> Optional[str]:
    answer = getattr(state, "answer", None)
    if answer is None:
        return None
    for attr in ("headline", "summary", "narrative", "text"):
        val = getattr(answer, attr, None)
        if val:
            return str(val)[:300]
    return None


def _grade_one(source_id: int, user_id: str, q: dict, prior_state) -> tuple[dict, object]:
    """Runs one golden question through the production agent path, grades
    it, and returns (result_entry, this_state) -- this_state is threaded to
    the next question if it declares follow_up_of this one. Never raises."""
    from core.orchestrator.agent import (
        AgentStatus, ConversationContext, answer_business_question,
    )

    entry: dict = {
        "id": q["id"], "category": q["category"], "question": q["question"],
        "follow_up_of": q.get("follow_up_of"),
        "expected_intent": q["expected_intent"], "expected_outcome": q["expected_outcome"],
        "currently_blocked_by": q.get("currently_blocked_by"),
        "actual_intent": None, "sql_generated": None,
        "intent_match": None, "outcome_pass": None, "material_pass": None,
        "material_fail_reasons": [], "overall_pass": False,
        "status": None,  # PASS / EXPECTED_FAILURE / REGRESSION / IMPROVED / ERROR
        "selected_tables": None, "generated_sql": None, "answer_summary": None,
        "execution_status": None, "execution_row_count": None,
        "agent_status": None, "stop_code": None, "stop_reason": None,
        "duration_ms": None,
    }

    conversation_context = None
    if q.get("follow_up_of") and prior_state is not None:
        from api.v1.agent_response_adapters import build_conversation_state
        cs = build_conversation_state(
            prior_state, conversation_id=f"golden-{q['follow_up_of']}",
            source_id=source_id, user_id=user_id, turn_number=2,
        )
        if cs is not None:
            conversation_context = ConversationContext.from_dict(cs)

    start = time.perf_counter()
    try:
        time.sleep(_EXECUTION_SPACING_S)
        state = answer_business_question(
            source_id, user_id, q["question"], conversation_context=conversation_context,
        )
        entry["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)

        entry["actual_intent"] = state.detected_intent.intent_type.value if state.detected_intent else None
        entry["intent_match"] = entry["actual_intent"] == q["expected_intent"]

        generated = state.generated_sql is not None
        entry["sql_generated"] = generated
        entry["selected_tables"] = state.selected_tables or []
        entry["generated_sql"] = state.generated_sql
        entry["answer_summary"] = _answer_summary(state)
        entry["agent_status"] = state.status.value if getattr(state, "status", None) else None
        entry["stop_code"] = getattr(state, "stop_code", None)
        entry["stop_reason"] = getattr(state, "stop_reason", None)

        if state.execution_result is not None:
            entry["execution_status"] = state.execution_result.get("status")
            entry["execution_row_count"] = state.execution_result.get("row_count")

        expected_outcome = q["expected_outcome"]
        if expected_outcome == "SUCCESS":
            entry["outcome_pass"] = generated
        elif expected_outcome == "KNOWN_DEFECT":
            entry["outcome_pass"] = True
        else:
            entry["outcome_pass"] = not generated

        if generated and expected_outcome == "SUCCESS":
            material_pass, reasons = _grade_material_correctness(q, state.sql_plan or {})
            entry["material_pass"] = material_pass
            entry["material_fail_reasons"] = reasons
        else:
            entry["material_pass"] = True  # nothing to grade materially

        entry["overall_pass"] = bool(entry["intent_match"] and entry["outcome_pass"] and entry["material_pass"])

        blocked = q.get("currently_blocked_by")
        if entry["overall_pass"]:
            entry["status"] = "IMPROVED" if blocked else "PASS"
        else:
            entry["status"] = "EXPECTED_FAILURE" if blocked else "REGRESSION"

        return entry, state

    except Exception as exc:  # noqa: BLE001 - never let one question abort the run
        entry["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
        entry["status"] = "ERROR"
        entry["material_fail_reasons"] = [f"ERROR: {exc}"]
        entry["_traceback"] = traceback.format_exc()
        return entry, prior_state


def run(source_id: int, user_id: str, suite_path: Path = _SUITE_PATH) -> dict:
    questions = _load_suite(suite_path)
    states_by_id: dict[str, object] = {}
    results: list[dict] = []
    for q in questions:
        prior_state = states_by_id.get(q.get("follow_up_of"))
        entry, state = _grade_one(source_id, user_id, q, prior_state)
        results.append(entry)
        states_by_id[q["id"]] = state
    return {"source_id": source_id, "user_id": user_id, "results": results}


def _print_report(run_result: dict) -> None:
    results = run_result["results"]
    by_status: dict[str, list[dict]] = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    print(f"\n=== CCPP Golden Set — source_id={run_result['source_id']} ===")
    print(f"Total: {len(results)}  |  "
          f"PASS: {len(by_status.get('PASS', []))}  |  "
          f"IMPROVED: {len(by_status.get('IMPROVED', []))}  |  "
          f"EXPECTED_FAILURE: {len(by_status.get('EXPECTED_FAILURE', []))}  |  "
          f"REGRESSION: {len(by_status.get('REGRESSION', []))}  |  "
          f"ERROR: {len(by_status.get('ERROR', []))}")

    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    print("\n| Category | Pass | Total |")
    print("|---|---|---|")
    for cat, items in sorted(by_cat.items()):
        passed = sum(1 for i in items if i["status"] in ("PASS", "IMPROVED"))
        print(f"| {cat} | {passed} | {len(items)} |")

    if by_status.get("REGRESSION"):
        print("\n!!! REGRESSIONS (not previously diagnosed — investigate) !!!")
        for r in by_status["REGRESSION"]:
            print(f"  [{r['id']}] {r['question']}")
            reasons = r["material_fail_reasons"] or (
                [f"stop_reason: {r['stop_reason']}"] if r.get("stop_reason") else
                [f"intent mismatch: expected {r['expected_intent']}, got {r['actual_intent']}"] if not r["intent_match"] else
                [r.get("_traceback", "")[:200]]
            )
            for reason in reasons:
                print(f"      - {reason}")

    if by_status.get("IMPROVED"):
        print("\nIMPROVED (previously-diagnosed defect no longer reproduces — update suite):")
        for r in by_status["IMPROVED"]:
            print(f"  [{r['id']}] {r['question']}  (was blocked by {r['currently_blocked_by']})")

    if by_status.get("EXPECTED_FAILURE"):
        print("\nEXPECTED_FAILURE (already tracked, matches root-cause matrix):")
        for r in by_status["EXPECTED_FAILURE"]:
            print(f"  [{r['id']}] {r['question']}  (blocked_by={r['currently_blocked_by']})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CCPP Golden Business Question Suite.")
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", type=str, required=True)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--suite-path", type=str, default=str(_SUITE_PATH))
    args = parser.parse_args()

    run_result = run(args.source_id, args.user_id, Path(args.suite_path))
    _print_report(run_result)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(run_result, indent=2, default=str), encoding="utf-8")
        print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
