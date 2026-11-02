"""
Enterprise Acceptance Suite runner -- Phase 8A1: drives the locked acceptance
questions through the REAL single question-answering route, the same function
POST /v1/composer/ask runs (api.v1.composer.composer_ask -> the AI-first fast
path). It replaces the retired agent-based runner (run_acceptance_suite.py,
now archived/non-runnable).

What it does NOT do (by design):
  - no retired imports, no fallback pipeline;
  - no prompt tuning, no retry or correction loop: one request per question;
  - no persistence of SQL, result rows, PII, credentials, secrets or prompts.
    Only: question id/text, status, timings, cache status, selected object
    names (FQNs), validation/execution outcome and a bucketed classification.

Classification (kept separate on purpose):
  ANSWERED                 -> fast path returned rows/KPI (agent_status "ok")
  CLARIFIED                -> asked a clarifying question (genuine ambiguity or
                              over-asking -- see `expected_outcome`)
  REFUSED_NON_DATA/UNSUPPORTED -> deliberately declined, no SQL
  REJECTED_BY_VALIDATION   -> deterministic validator blocked the SQL (safe)
  FAILED                   -> execution/selection/context/timeout failure
  NOT_PREPARED / NOT_ENABLED -> environment, not a quality signal

Grading (matches the locked suite's contract, nothing new invented):
  expected SUCCESS      -> PASS iff ANSWERED; CLARIFIED is reported as
                           UNEXPECTED_CLARIFICATION (not silently a pass)
  expected KNOWN_DEFECT -> always PASS; IMPROVED if safely not answered
  any refusal outcome   -> PASS iff NOT answered (CLARIFIED/REFUSED/REJECTED);
                           an ANSWERED result is ANSWERED_WHERE_AMBIGUITY_EXPECTED
                           and needs a human look at the recorded objects
Correctness of an ANSWERED result is NOT auto-graded (the suite carries no
expected objects/values); the recorded `selected_objects` are for review.

Usage:
    venv/Scripts/python docs/validation/run_acceptance_suite_fast_path.py \
        --source-id 10004 --user-id 28 --plan            # counts/cost exposure only, zero calls
    ... --smoke                                          # small fixed subset
    ... --ids EBVS-C-01,EBVS-C-02                        # explicit ids
    ... --all --confirm-full                             # all questions (paid)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SUITE_PATH = Path(__file__).parent / "enterprise_business_validation_suite.v1.json"
_REFUSAL_OUTCOMES = frozenset({"REFUSED_SAFE", "AMBIGUOUS", "CLARIFICATION_NEEDED", "NOT_SUPPORTED"})

# Fixed smoke subset: spans categories and every expected-outcome kind.
SMOKE_QUESTION_COUNT = 6


def load_suite(path: Path = _SUITE_PATH) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for cat in data["categories"]:
        for q in cat["questions"]:
            q = dict(q)
            q.setdefault("category", cat["name"])
            out.append(q)
    return out


def pick_smoke(questions: list, n: int = SMOKE_QUESTION_COUNT) -> list:
    """One question per distinct expected_outcome first, then fill by suite
    order, never more than n. Deterministic."""
    chosen, seen = [], set()
    for q in questions:
        if q.get("expected_outcome") not in seen:
            seen.add(q.get("expected_outcome")); chosen.append(q)
        if len(chosen) >= n:
            return chosen
    for q in questions:
        if q not in chosen:
            chosen.append(q)
        if len(chosen) >= n:
            break
    return chosen


def classify(agent_status: str, trace: list) -> str:
    if agent_status == "ok":
        return "ANSWERED"
    if agent_status == "clarification_required":
        return "CLARIFIED"
    if agent_status == "non_data":
        return "REFUSED_NON_DATA"
    if agent_status == "unsupported":
        return "REFUSED_UNSUPPORTED"
    if agent_status == "validation_failed":
        return "REJECTED_BY_VALIDATION"
    if agent_status == "not_prepared":
        return "NOT_PREPARED"
    if agent_status == "not_enabled":
        return "NOT_ENABLED"
    return "FAILED"


def grade(expected: str, bucket: str) -> tuple:
    """(verdict, flag). verdict: PASS/FAIL/ENV. flag: extra reviewer note."""
    if bucket in ("NOT_PREPARED", "NOT_ENABLED"):
        return "ENV", None
    answered = bucket == "ANSWERED"
    if expected == "SUCCESS":
        if answered:
            return "PASS", None
        if bucket == "CLARIFIED":
            return "FAIL", "UNEXPECTED_CLARIFICATION"
        return "FAIL", None
    if expected == "KNOWN_DEFECT":
        return "PASS", ("IMPROVED" if not answered else None)
    if expected in _REFUSAL_OUTCOMES:
        if not answered:
            return "PASS", None
        return "FAIL", "ANSWERED_WHERE_AMBIGUITY_EXPECTED"
    return "FAIL", "UNKNOWN_EXPECTED_OUTCOME"


def run_one(composer_ask, ComposerRequest, user, source_id: int, q: dict) -> dict:
    t0 = time.perf_counter()
    entry = {"id": q["id"], "category": q["category"], "question": q["question"],
             "expected_outcome": q.get("expected_outcome"), "expected_intent": q.get("expected_intent")}
    try:
        resp = composer_ask(
            ComposerRequest(session_id=str(uuid.uuid4()), message=q["question"], selected_data_source=source_id),
            user,
        )
    except Exception as exc:  # never persists the message -- type only
        entry.update(agent_status="exception", bucket="FAILED", verdict="FAIL", flag=type(exc).__name__,
                     wall_s=round(time.perf_counter() - t0, 2))
        return entry
    wall = round(time.perf_counter() - t0, 2)
    if not isinstance(resp, dict):
        entry.update(agent_status="http_error", bucket="FAILED", verdict="FAIL",
                     flag=f"http_{getattr(resp, 'status_code', '?')}", wall_s=wall)
        return entry
    ea = resp.get("enterprise_answer") or {}
    trace = [f"{s.get('tool')}:{s.get('status')}" for s in (resp.get("agent_trace") or [])]
    perf = resp.get("perf_trace") or {}
    status = resp.get("agent_status")
    bucket = classify(status, trace)
    verdict, flag = grade(q.get("expected_outcome"), bucket)
    validation = next((t.split(":")[1] for t in trace if t.startswith("sql_validation:")), "not_reached")
    execution = next((t.split(":")[1] for t in trace if t.startswith("sql_execution:")), "not_reached")
    clar = (ea.get("clarification") or {}).get("reason") if ea.get("clarification") else None
    entry.update(
        agent_status=status, pipeline_status=resp.get("pipeline_status"), bucket=bucket,
        verdict=verdict, flag=flag,
        plan_cache_status=resp.get("plan_cache_status"),
        selected_objects=list(ea.get("source_tables") or []),
        validation=validation, execution=execution,
        row_count=(ea.get("execution_summary") or {}).get("row_count"),
        has_kpi=bool(ea.get("kpi")), has_chart=bool(ea.get("chart")), n_insights=len(ea.get("insights") or []),
        clarification_text=(clar or "")[:200] or None,
        total_ms=perf.get("total_ms"), execution_ms=perf.get("execution_ms"), wall_s=wall,
        follow_up_resolution_type=(resp.get("follow_up_resolution") or {}).get("resolution_type"),
    )
    return entry


def summarize(results: list) -> dict:
    return {
        "total": len(results),
        "verdicts": dict(Counter(r["verdict"] for r in results)),
        "buckets": dict(Counter(r["bucket"] for r in results)),
        "flags": dict(Counter(r["flag"] for r in results if r.get("flag"))),
        "cache": dict(Counter(r.get("plan_cache_status") for r in results)),
        "by_category": {
            c: {"pass": sum(1 for r in results if r["category"] == c and r["verdict"] == "PASS"),
                "total": sum(1 for r in results if r["category"] == c)}
            for c in sorted({r["category"] for r in results})
        },
    }


def plan_report(questions: list) -> dict:
    n = len(questions)
    return {
        "questions": n,
        "worst_case_gpt_calls": n, "worst_case_embedding_calls": n,
        "live_sql_executions": f"<= {n} (only for questions the model answers)",
        "note": ("Each uncached question = 1 embedding + 1 combined GPT call (bounded context, "
                 "AI_FIRST_SQL_FAST_PATH_MAX_CONTEXT_CHARACTERS) + at most 1 live read-only SQL "
                 "execution; cached plans make 0 model calls. Multiply by your gpt-6-sol / embedding "
                 "rates for exposure."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--user-id", type=str, required=True)
    ap.add_argument("--suite-path", type=Path, default=_SUITE_PATH)
    ap.add_argument("--output", type=Path, default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="print expected call counts; make ZERO calls")
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--ids", type=str)
    g.add_argument("--all", action="store_true")
    ap.add_argument("--confirm-full", action="store_true", help="required with --all")
    ap.add_argument("--spacing", type=float, default=1.0, help="seconds between questions")
    args = ap.parse_args()

    questions = load_suite(args.suite_path)
    if args.plan:
        print(json.dumps({"full_suite": plan_report(questions),
                          "smoke_subset": plan_report(pick_smoke(questions)),
                          "smoke_ids": [q["id"] for q in pick_smoke(questions)]}, indent=2))
        return 0
    if args.all and not args.confirm_full:
        print("Refusing: --all makes paid model calls for every question. Re-run with --confirm-full.")
        return 2
    if args.smoke:
        selected = pick_smoke(questions)
    elif args.ids:
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        selected = [q for q in questions if q["id"] in wanted]
    else:
        selected = questions

    logging.disable(logging.CRITICAL)
    import api.app  # noqa: F401 -- registers connectors exactly as the server does
    from api.v1.composer import ComposerRequest, composer_ask
    from auth.api_key import AuthenticatedUser

    user = AuthenticatedUser(role="user", user_id=args.user_id)
    results = []
    for i, q in enumerate(selected, 1):
        r = run_one(composer_ask, ComposerRequest, user, args.source_id, q)
        results.append(r)
        print(f"[{i}/{len(selected)}] {r['id']:<12} {r['expected_outcome']:<20} -> {r['bucket']:<22} "
              f"{r['verdict']:<4} {r.get('flag') or '':<34} cache={r.get('plan_cache_status')} "
              f"{r.get('wall_s')}s objs={len(r.get('selected_objects') or [])}", flush=True)
        if i < len(selected):
            time.sleep(args.spacing)
    summary = summarize(results)
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"source_id": args.source_id, "summary": summary, "results": results},
                                          indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
