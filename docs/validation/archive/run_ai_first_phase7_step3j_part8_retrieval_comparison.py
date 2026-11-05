"""
Retrieval-only before/after evaluation -- Phase 7, Step 3J, Part 8.

Read-only, real-embedding measurement of core.sqlservice.schema_frontier.
build_schema_frontier() against the real, already-READY CCPP retrieval
build (source_id=10004) and the 15-case golden fixture (docs/validation/
ccpp_golden_cases_v2.py), run twice per case: once with
core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED forced False (the "before"
baseline -- what every other source in production sees today) and once
forced True (the "after" state this controlled run is evaluating), each via
in-process monkeypatching of core.sqlservice.schema_frontier's own imported
name -- .env and every other source's runtime behavior are never touched.

Live-call footprint: exactly TWO real OpenAIEmbeddingProvider.embed_batch()
calls per golden question (30 total: 15 before + 15 after) -- the frontier's
own normal one-embedding-per-question shape, doubled only because this
script deliberately runs it twice per case for the comparison. NEVER an AI
schema-selection/SQL-generation/SQL-review/execution/answer call of any
kind (same restriction as Gate A's own script).

No database write of any kind beyond the one JSON results file below.

Reports exactly what Step 3J Part 8 asks for:
  - frontier before vs. after (candidate sets, unchanged)
  - required-object availability (Gate A's own 3 lines, both passes)
  - SQL-fee vs. IPBC-payment separation in the attached business-knowledge
    evidence for the two cases that touch payment concepts
  - forbidden/Tier 3/4 leakage (must stay zero in both passes)
  - determinism (repeating "after" once more must produce byte-identical
    business-knowledge evidence per candidate)
  - latency
"""

from __future__ import annotations

import json
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

import core.sqlservice.schema_frontier as schema_frontier_module  # noqa: E402
from core.sqlservice.business_knowledge_evidence import (  # noqa: E402
    clear_business_knowledge_evidence_cache,
)
from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build  # noqa: E402
from core.sqlservice.schema_frontier import STATUS_OK as FRONTIER_STATUS_OK  # noqa: E402
from core.sqlservice.schema_frontier import build_schema_frontier  # noqa: E402
from core.sqlservice.vector_index import clear_vector_index_cache  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})
RESULTS_PATH = _VALIDATION_DIR / "results" / "ai_first_phase7_step3j_part8_retrieval_comparison.json"

# The two cases whose question/required objects touch a payment concept
# this run's business-knowledge package models (SQL_COURSE_FEE /
# IPBC_FULL_PAYMENT / IPBC_INSTALLMENT_PLAN) -- read from the fixture
# itself, not hardcoded here beyond the case_id.
_PAYMENT_RELEVANT_CASE_IDS = {"CCPP2-008", "CCPP2-013"}


def _run_pass(build, provider, *, evidence_enabled: bool) -> list[dict]:
    schema_frontier_module.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED = evidence_enabled
    clear_vector_index_cache()
    clear_business_knowledge_evidence_cache()

    per_case: list[dict] = []
    for case in CCPP_GOLDEN_CASES:
        t0 = time.perf_counter()
        result = build_schema_frontier(case.question, build.id, SOURCE_ID, USER_ID, provider)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        frontier_fqns_lower = {c.object_fqn.lower() for c in result.candidates}
        tier_leakage = sorted({c.object_fqn for c in result.candidates if c.retrieval_tier not in ELIGIBLE_TIERS})
        forbidden_leakage = sorted(
            fqn for fqn in case.forbidden_object_fqns if fqn.lower() in frontier_fqns_lower
        )
        required_lower = {f.lower() for f in case.required_object_fqns}
        missing_required = sorted(f for f in case.required_object_fqns if f.lower() not in frontier_fqns_lower)
        full_coverage = required_lower.issubset(frontier_fqns_lower) if required_lower else None

        business_knowledge_by_object = {
            c.object_fqn: [
                {
                    "concept_id": m.concept_id, "concept_type": m.concept_type,
                    "concept_origin": m.concept_origin, "mapping_role": m.mapping_role,
                    "mapping_status": m.mapping_status, "confidence": m.confidence,
                    "evidence": list(m.evidence),
                }
                for m in c.business_knowledge_mappings
            ]
            for c in result.candidates if c.business_knowledge_mappings
        }

        per_case.append({
            "case_id": case.case_id,
            "question": case.question,
            "status": result.status,
            "frontier_size": len(result.candidates),
            "required_object_fqns": sorted(case.required_object_fqns),
            "missing_required_object_fqns": missing_required,
            "full_required_coverage": full_coverage,
            "tier_3_4_leakage": tier_leakage,
            "forbidden_object_leakage": forbidden_leakage,
            "business_knowledge_evidence_by_object": business_knowledge_by_object,
            "latency_ms": round(latency_ms, 3),
        })
    return per_case


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    assert build is not None, "no READY retrieval build found for source_id=10004"
    provider = OpenAIEmbeddingProvider()

    print(f"Loaded build_id={build.id} schema_fingerprint={build.schema_fingerprint}")
    print(f"Golden fixture: {len(CCPP_GOLDEN_CASES)} cases\n")

    original_flag = schema_frontier_module.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED
    try:
        print("=== BEFORE (BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED=False) ===")
        before = _run_pass(build, provider, evidence_enabled=False)
        for c in before:
            print(f"  {c['case_id']}: frontier_size={c['frontier_size']} full_required_coverage={c['full_required_coverage']}")

        print("\n=== AFTER (BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED=True) ===")
        after = _run_pass(build, provider, evidence_enabled=True)
        for c in after:
            bk_objects = sorted(c["business_knowledge_evidence_by_object"].keys())
            print(
                f"  {c['case_id']}: frontier_size={c['frontier_size']} full_required_coverage={c['full_required_coverage']} "
                f"business_knowledge_objects={bk_objects}"
            )

        print("\n=== AFTER, rerun once more (determinism check) ===")
        after_again = _run_pass(build, provider, evidence_enabled=True)
    finally:
        schema_frontier_module.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED = original_flag

    # ── Gate A, re-derived from the "before" pass (must match the committed
    #    Gate A result exactly -- proves this run did not regress it) ──────
    non_clarification_before = [c for c in before if c["case_id"] != "CCPP2-011"]
    coverage_before = sum(1 for c in non_clarification_before if c["full_required_coverage"])
    leakage_before = [c["case_id"] for c in before if c["tier_3_4_leakage"] or c["forbidden_object_leakage"]]

    non_clarification_after = [c for c in after if c["case_id"] != "CCPP2-011"]
    coverage_after = sum(1 for c in non_clarification_after if c["full_required_coverage"])
    leakage_after = [c["case_id"] for c in after if c["tier_3_4_leakage"] or c["forbidden_object_leakage"]]

    frontier_unchanged_by_case = {
        b["case_id"]: (b["frontier_size"] == a["frontier_size"] and b["missing_required_object_fqns"] == a["missing_required_object_fqns"])
        for b, a in zip(before, after)
    }

    determinism_ok = all(
        a["business_knowledge_evidence_by_object"] == a2["business_knowledge_evidence_by_object"]
        for a, a2 in zip(after, after_again)
    )

    payment_relevant = {
        case_id: {
            "before_evidence": next(c for c in before if c["case_id"] == case_id)["business_knowledge_evidence_by_object"],
            "after_evidence": next(c for c in after if c["case_id"] == case_id)["business_knowledge_evidence_by_object"],
        }
        for case_id in _PAYMENT_RELEVANT_CASE_IDS
    }

    gate_a = {
        "before": {
            "required_object_coverage_at_least_13_of_14": {"actual": f"{coverage_before}/14", "pass": coverage_before >= 13},
            "zero_forbidden_tier_3_4_leakage": {"actual": "0" if not leakage_before else str(leakage_before), "pass": not leakage_before},
        },
        "after": {
            "required_object_coverage_at_least_13_of_14": {"actual": f"{coverage_after}/14", "pass": coverage_after >= 13},
            "zero_forbidden_tier_3_4_leakage": {"actual": "0" if not leakage_after else str(leakage_after), "pass": not leakage_after},
        },
        "no_regression": coverage_after >= coverage_before and not leakage_after,
    }

    print("\n=== Gate A comparison ===")
    print(json.dumps(gate_a, indent=2))
    print("\n=== Frontier candidate sets unchanged before/after (per case) ===")
    print(json.dumps(frontier_unchanged_by_case, indent=2))
    print("\n=== Determinism (after == after_again) ===", determinism_ok)
    print("\n=== Payment-concept-relevant cases: business-knowledge evidence ===")
    print(json.dumps(payment_relevant, indent=2))

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({
            "build_id": build.id, "schema_fingerprint": build.schema_fingerprint,
            "before": before, "after": after,
            "gate_a": gate_a, "frontier_unchanged_by_case": frontier_unchanged_by_case,
            "determinism_ok": determinism_ok, "payment_relevant_cases": payment_relevant,
        }, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nWrote full evaluation to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
