"""
Gate A — frontier availability. Phase 7, Step 3C, Part 9.

Read-only, real-embedding measurement of core.sqlservice.schema_frontier.
build_schema_frontier() against the real, already-READY CCPP retrieval
build (source_id=10004) and the 15-case golden fixture (docs/validation/
ccpp_golden_cases_v2.py).

Live-call footprint: exactly ONE real OpenAIEmbeddingProvider.embed_batch()
call PER golden question (15 total) -- the normal, single-question shape
build_schema_frontier()'s own public interface uses in production (matching
how answer_question_ai_first() itself would call it once per real
question), never a second embedding call per question, and NEVER an AI
schema-selection/SQL-generation/SQL-review/execution/answer call of any
kind -- this script imports nothing from core.sqlservice.schema_selection_
service, core.sqlservice.sql_generation_service, core.sqlservice.
sql_review_service, core.sqlservice.sql_execution, or core.sqlservice.
answer_generation_service.

No database write of any kind. Nothing under core/ or data/ is modified.

Required gate lines (per the authorizing task):
  - at least 13/14 non-clarification cases have every required_object_fqn
    present in their 100-object frontier
  - dbo.ADF_Path present in every one of the 4 applicable program-question
    frontiers (CCPP2-003, CCPP2-007, CCPP2-010, CCPP2-014)
  - zero forbidden/Tier-3/4/archive/system object leakage into any frontier
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_VALIDATION_DIR = Path(__file__).resolve().parent
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402

from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build  # noqa: E402
from core.sqlservice.schema_frontier import STATUS_OK as FRONTIER_STATUS_OK  # noqa: E402
from core.sqlservice.schema_frontier import build_schema_frontier  # noqa: E402
from core.sqlservice.vector_index import clear_vector_index_cache  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})
RESULTS_PATH = _VALIDATION_DIR / "results" / "ai_first_phase7_step3c_gate_a_frontier.json"

_PATH_FQN = "dbo.ADF_Path"
_PATH_APPLICABLE_CASE_IDS = {"CCPP2-003", "CCPP2-007", "CCPP2-010", "CCPP2-014"}

_APPROX_CHARS_PER_TOKEN = 4.0
_TEXT_EMBEDDING_3_SMALL_PRICE_PER_1M_INPUT_TOKENS_USD = 0.02


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    assert build is not None, "no READY retrieval build found for source_id=10004"
    provider = OpenAIEmbeddingProvider()
    clear_vector_index_cache()

    print(
        f"Loaded build_id={build.id} snapshot_id={build.snapshot_id} "
        f"schema_fingerprint={build.schema_fingerprint} (read-only, local SQLite only)"
    )
    print(f"Golden fixture: {len(CCPP_GOLDEN_CASES)} cases")
    print(
        f"Embedding provider: provider_id={provider.provider_id} model={provider.model} "
        f"dimensions={provider.dimensions}\n"
    )

    per_case: list[dict] = []
    embed_call_count = 0
    total_input_characters = 0
    t_total0 = time.perf_counter()

    for case in CCPP_GOLDEN_CASES:
        t0 = time.perf_counter()
        result = build_schema_frontier(case.question, build.id, SOURCE_ID, USER_ID, provider)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        embed_call_count += 1
        total_input_characters += len(case.question)

        frontier_fqns_lower = {c.object_fqn.lower() for c in result.candidates}
        tier_leakage = sorted({c.object_fqn for c in result.candidates if c.retrieval_tier not in ELIGIBLE_TIERS})
        forbidden_leakage = sorted(
            fqn for fqn in case.forbidden_object_fqns if fqn.lower() in frontier_fqns_lower
        )
        required_lower = {f.lower() for f in case.required_object_fqns}
        missing_required = sorted(f for f in case.required_object_fqns if f.lower() not in frontier_fqns_lower)
        full_coverage = required_lower.issubset(frontier_fqns_lower) if required_lower else None
        path_present = _PATH_FQN.lower() in frontier_fqns_lower

        per_case.append({
            "case_id": case.case_id,
            "category": case.category,
            "question": case.question,
            "expects_clarification": case.expects_clarification,
            "status": result.status,
            "frontier_size": len(result.candidates),
            "frontier_truncated": result.truncated,
            "frontier_total_characters": result.total_characters,
            "warnings": list(result.warnings),
            "required_object_fqns": sorted(case.required_object_fqns),
            "missing_required_object_fqns": missing_required,
            "full_required_coverage": full_coverage,
            "adf_path_applicable": case.case_id in _PATH_APPLICABLE_CASE_IDS,
            "adf_path_present_in_frontier": path_present,
            "tier_3_4_leakage": tier_leakage,
            "forbidden_object_leakage": forbidden_leakage,
            "latency_ms": round(latency_ms, 3),
        })
        status_word = "OK" if result.status == FRONTIER_STATUS_OK else result.status
        print(
            f"  {case.case_id}: status={status_word} frontier_size={len(result.candidates)} "
            f"full_required_coverage={full_coverage} adf_path={'n/a' if case.case_id not in _PATH_APPLICABLE_CASE_IDS else path_present} "
            f"latency_ms={round(latency_ms, 1)}"
        )

    total_wall_ms = (time.perf_counter() - t_total0) * 1000.0

    non_clarification = [c for c in per_case if not c["expects_clarification"]]
    assert len(non_clarification) == 14, f"expected 14 non-clarification cases, got {len(non_clarification)}"

    coverage_count = sum(1 for c in non_clarification if c["full_required_coverage"])
    path_cases = [c for c in per_case if c["adf_path_applicable"]]
    assert len(path_cases) == 4, f"expected 4 ADF_Path-applicable cases, got {len(path_cases)}"
    path_present_count = sum(1 for c in path_cases if c["adf_path_present_in_frontier"])
    leakage_cases = [c["case_id"] for c in per_case if c["tier_3_4_leakage"] or c["forbidden_object_leakage"]]

    gate = {
        "required_object_coverage_at_least_13_of_14": {
            "actual": f"{coverage_count}/14", "pass": coverage_count >= 13,
            "failing_cases": [c["case_id"] for c in non_clarification if not c["full_required_coverage"]],
        },
        "adf_path_present_in_all_4_applicable_frontiers": {
            "actual": f"{path_present_count}/4", "pass": path_present_count == 4,
            "failing_cases": [c["case_id"] for c in path_cases if not c["adf_path_present_in_frontier"]],
        },
        "zero_forbidden_tier_3_4_leakage": {
            "actual": "0 cases" if not leakage_cases else f"{len(leakage_cases)} case(s)",
            "pass": not leakage_cases, "failing_cases": leakage_cases,
        },
    }
    gate_overall_pass = all(line["pass"] for line in gate.values())

    approx_input_tokens = math.ceil(total_input_characters / _APPROX_CHARS_PER_TOKEN)
    approx_cost_usd = (approx_input_tokens / 1_000_000.0) * _TEXT_EMBEDDING_3_SMALL_PRICE_PER_1M_INPUT_TOKENS_USD
    cost_accounting = {
        "embedding_api_call_count": embed_call_count,
        "embedding_model": provider.model,
        "embedding_dimensions": provider.dimensions,
        "questions_embedded": len(CCPP_GOLDEN_CASES),
        "total_input_characters": total_input_characters,
        "approx_input_tokens_method": (
            f"ceil(total_input_characters / {_APPROX_CHARS_PER_TOKEN}) -- OpenAI's published "
            "~4-characters-per-English-token rule of thumb; embed_batch() does not surface a real "
            "token-usage field, so this is an approximation, not an exact billed count."
        ),
        "approx_input_tokens": approx_input_tokens,
        "published_price_usd_per_1m_input_tokens": _TEXT_EMBEDDING_3_SMALL_PRICE_PER_1M_INPUT_TOKENS_USD,
        "approx_cost_usd": round(approx_cost_usd, 8),
    }

    latencies = sorted(c["latency_ms"] for c in per_case)

    def pct(p, arr):
        idx = min(len(arr) - 1, int(round(p * (len(arr) - 1))))
        return arr[idx]

    aggregate = {
        "build_id": build.id,
        "source_id": SOURCE_ID,
        "case_inventory": [c["case_id"] for c in per_case],
        "clarification_case_excluded": [c["case_id"] for c in per_case if c["expects_clarification"]],
        "denominator_14_reconciled": len(non_clarification) == 14,
        "cost_accounting": cost_accounting,
        "gate": gate,
        "gate_overall_pass": gate_overall_pass,
        "total_wall_ms": round(total_wall_ms, 1),
        "latency_p50_ms": round(pct(0.50, latencies), 3),
        "latency_p90_ms": round(pct(0.90, latencies), 3),
        "latency_p99_ms": round(pct(0.99, latencies), 3),
    }

    print("\n=== Gate A (3 lines) ===")
    for label, line in gate.items():
        print(f"  {'PASS' if line['pass'] else 'FAIL'}  {label}: {line['actual']}")
    print(f"\nOVERALL GATE A: {'PASS' if gate_overall_pass else 'FAIL'}")
    print(f"\nCost accounting: {json.dumps(cost_accounting, indent=2)}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({"per_case": per_case, "aggregate": aggregate}, indent=2, sort_keys=False, default=str),
        encoding="utf-8",
    )
    print(f"\nWrote full evaluation to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
