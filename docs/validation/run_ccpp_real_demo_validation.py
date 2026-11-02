"""
REAL CCPP Demo Validation + Executive Report Validation milestone.

Drives the ACTUAL production HTTP path — FastAPI TestClient against the real
api.app.app, POST /v1/composer/ask (the same route the frontend Ask
experience calls) and POST /v1/sources/{id}/executive-report — rather than
calling core.orchestrator.agent.answer_business_question() or
core.reporting.executive_report_service.generate_executive_report() directly.
Uses a real JWT built with the server's own configured JWT_SECRET
(auth.jwt_auth.create_access_token), exactly as a real logged-in session
would authenticate.

Read-only: every SQL statement the agent executes is governed
(execute_governed_query), and this script's own independent verification
queries are plain read-only SELECTs issued directly against the live MSSQL
connector — never used by production planning, test code only.

Usage:
    venv/Scripts/python docs/validation/run_ccpp_real_demo_validation.py \
        --source-id 1 --user-id 28 \
        --output docs/validation/results/ccpp_real_demo_validation.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.connectors.relational.mssql       # noqa: E402,F401
import core.connectors.relational.mysql       # noqa: E402,F401
import core.connectors.relational.postgresql  # noqa: E402,F401

_EXECUTION_SPACING_S = 2.5

# ---------------------------------------------------------------------------
# The 15 demo questions — see the milestone report for per-question
# justification (business purpose / expected entity / measure / dimension /
# supporting tables / why the CCPP schema confirms it's answerable).
# Reused verbatim from docs/validation/ccpp_golden_business_questions.v1.json
# wherever that suite already covers the category with a currently_blocked_by
# = null (or since-fixed) entry; two structural variants (#7, #13) reuse an
# already-confirmed-working dimension/measure in a new but non-hardcoded
# phrasing, never inventing a new capability.
# ---------------------------------------------------------------------------
DEMO_QUESTIONS = [
    {"slot": 1, "id": "DEMO-01", "question": "How many students are there?",
     "category": "simple_count"},
    {"slot": 2, "id": "DEMO-02", "question": "How many students started last year?",
     "category": "date_based_count"},
    {"slot": 3, "id": "DEMO-03", "question": "How many active classes are there?",
     "category": "active_status_filter"},
    {"slot": 4, "id": "DEMO-04", "question": "How many students are on a trial subscription?",
     "category": "trial_subscription_filter"},
    {"slot": 5, "id": "DEMO-05", "question": "How many classes are there by location?",
     "category": "class_location_grouping"},
    {"slot": 6, "id": "DEMO-06", "question": "Show enrollments between 2025-01-01 and 2025-06-30",
     "category": "enrollment_signup_count"},
    {"slot": 7, "id": "DEMO-07", "question": "What is the total amount of fees paid?",
     "category": "fee_payment_aggregation"},
    {"slot": 8, "id": "DEMO-08", "question": "Total fees paid by month",
     "category": "grouped_fee_aggregation"},
    {"slot": 9, "id": "DEMO-09", "question": "Show students and their enrolled classes",
     "category": "student_class_relationship"},
    {"slot": 10, "id": "DEMO-10", "question": "Show enrollment trend by month",
     "category": "enrollment_trend"},
    {"slot": 11, "id": "DEMO-11", "question": "How many classes started this month?",
     "category": "month_analysis"},
    {"slot": 12, "id": "DEMO-12", "question": "Compare enrollments this year vs last year",
     "category": "period_comparison"},
    {"slot": 13, "id": "DEMO-13", "question": "Which location has the most classes?",
     "category": "ranking"},
    {"slot": 14, "id": "DEMO-14a", "question": "How many students started last year?",
     "category": "follow_up_base"},
    {"slot": 14, "id": "DEMO-14b", "question": "What about this year?",
     "category": "follow_up_context", "follow_up_of": "DEMO-14a"},
    {"slot": 15, "id": "DEMO-15", "question": "Show classes at the Richardson location",
     "category": "executive_useful_list"},
]


def _make_client():
    from fastapi.testclient import TestClient
    from api.app import app
    return TestClient(app)


def _auth_headers(user_id: str) -> dict:
    from auth.jwt_auth import create_access_token
    token = create_access_token({"sub": user_id, "role": "user"})
    return {"Authorization": f"Bearer {token}"}


def _ask(client, headers, source_id: int, message: str, session_id: str,
         conversation_state: dict | None = None) -> dict:
    body = {
        "session_id": session_id,
        "message": message,
        "selected_data_source": source_id,
    }
    if conversation_state is not None:
        body["conversation_state"] = conversation_state
    time.sleep(_EXECUTION_SPACING_S)
    t0 = time.perf_counter()
    resp = client.post("/v1/composer/ask", json=body, headers=headers)
    wall_ms = (time.perf_counter() - t0) * 1000
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {"_raw_text": resp.text}
    payload["_http_status"] = resp.status_code
    payload["_wall_ms"] = round(wall_ms, 1)
    return payload


def _extract_live_query_evidence(payload: dict) -> dict:
    """Pulls the live_query evidence item's own data dict (selected tables,
    generated SQL, execution result, etc.) out of the serialized
    evidence_package, where present."""
    pkg = payload.get("evidence_package") or {}
    for item in pkg.get("evidence") or []:
        if item.get("evidence_id") == "live_query" or item.get("capability") == "live_query":
            return item.get("data") or {}
    return {}


def run_qna(client, headers, source_id: int) -> list[dict]:
    results: list[dict] = []
    state_by_id: dict[str, dict | None] = {}
    for q in DEMO_QUESTIONS:
        follow_of = q.get("follow_up_of")
        conv_state = state_by_id.get(follow_of) if follow_of else None
        session_id = f"demo-{q['slot']}"
        if follow_of:
            # Same session/turn thread as the question this follows up on.
            base = next(r for r in results if r["id"] == follow_of)
            session_id = base["session_id"]

        payload = _ask(client, headers, source_id, q["question"], session_id, conv_state)
        live = _extract_live_query_evidence(payload)
        entry = {
            "slot": q["slot"], "id": q["id"], "category": q["category"],
            "question": q["question"], "follow_up_of": follow_of,
            "session_id": session_id,
            "http_status": payload.get("_http_status"),
            "agent_status": payload.get("agent_status"),
            "business_answer": payload.get("business_answer"),
            "warnings": payload.get("warnings"),
            "errors": payload.get("errors"),
            "governance_state": payload.get("governance_state"),
            "wall_ms": payload.get("_wall_ms"),
            "execution_time_s": payload.get("execution_time"),
            "perf_trace": payload.get("perf_trace"),
            "live_query": live,
            "conversation_state": payload.get("conversation_state"),
        }
        state_by_id[q["id"]] = payload.get("conversation_state")
        results.append(entry)
        print(f"  [{q['id']}] {q['question']!r} -> agent_status={entry['agent_status']} "
              f"wall_ms={entry['wall_ms']}")
    return results


def run_executive_report(client, headers, source_id: int) -> dict:
    time.sleep(_EXECUTION_SPACING_S)
    t0 = time.perf_counter()
    resp = client.post(f"/v1/sources/{source_id}/executive-report", headers=headers)
    wall_ms = (time.perf_counter() - t0) * 1000
    payload = resp.json()
    payload["_http_status"] = resp.status_code
    payload["_wall_ms"] = round(wall_ms, 1)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", type=str, required=True)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    client = _make_client()
    headers = _auth_headers(args.user_id)

    print("=== Running 15-question (16-call) REAL CCPP demo set via /v1/composer/ask ===")
    qna_results = run_qna(client, headers, args.source_id)

    print("\n=== Running Executive Report via /v1/sources/{id}/executive-report ===")
    exec_report = run_executive_report(client, headers, args.source_id)
    print(f"  http_status={exec_report.get('_http_status')} wall_ms={exec_report.get('_wall_ms')}")

    out = {
        "source_id": args.source_id, "user_id": args.user_id,
        "qna_results": qna_results,
        "executive_report": exec_report,
    }
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
