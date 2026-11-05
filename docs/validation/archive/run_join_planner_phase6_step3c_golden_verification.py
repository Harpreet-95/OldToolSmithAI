"""
Read-only verification — SQL Architecture Correction, Phase 6, Step 3C.

Grades the Step 3C-corrected core.sqlservice.schema_linker (requirement-
strength classification: CORE/CONSTRAINT/SUPPORTING/UNRESOLVED) and
core.sqlservice.join_planner (which now maps (concept_kind, strength) to
its own planner role) against the ESTABLISHED, ALREADY-AUTHORITATIVE
golden-case fixture docs/validation/ccpp_golden_cases_v2.CCPP_GOLDEN_CASES
(15 cases; CCPP2-011 is the designated clarification case, excluded from
the 14-case denominator).

Extends the Step 3B script with: separate linker/planner latency, raw vs.
CORE/CONSTRAINT/SUPPORTING/UNRESOLVED requirement-count reporting, and
anchor-count distribution (requirement 9's own observability list).

Data source: the LATEST already-READY retrieval-document build for
source_id=10004, read from ToolSmithAI's own local SQLite metadata store —
never a live SQL Server connection, never an OpenAI call, never a new
retrieval/embedding build.

Evaluation discipline (unchanged from Step 3A/3B):
  - linker/planner outputs are frozen BEFORE any expected-object read.
  - expected objects are used only to GRADE frozen output.
  - "complete" means every required object is a member of primary_plan.
    selected_object_fqns — never loose anchor containment.
  - required-object CONNECTIVITY is checked separately (union-find).
  - alternative-plan coverage is reported separately, never folded into
    primary-plan aggregates.
"""
from __future__ import annotations

import json
import statistics
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

from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_catalog import build_semantic_catalog
from core.sqlservice.schema_linker import (
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    STRENGTH_SUPPORTING,
    STRENGTH_UNRESOLVED,
    link_question_to_schema,
    clear_schema_linker_cache,
)
from core.sqlservice.join_planner import (
    JOIN_CAPABILITY_LINEAGE_ONLY,
    REASON_NO_CONFIRMED_JOIN_PATH,
    REASON_LINEAGE_ONLY_PATH,
    REASON_SEARCH_BUDGET_EXHAUSTED,
    clear_join_planner_cache,
    plan_schema_join_subgraph,
)

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})


# ── Step 1: freeze raw linker/planner outputs — no expected-object read
#    happens anywhere in this function ──────────────────────────────────


def _freeze_case_outputs(case, catalog, documents) -> dict:
    t0 = time.perf_counter()
    link = link_question_to_schema(case.question, catalog, documents)
    linker_latency_ms = (time.perf_counter() - t0) * 1000.0

    linker_anchor_fqns = tuple(sorted(a.object_fqn for a in link.anchors))
    all_reqs = (
        link.requirements.entity_concepts + link.requirements.measure_concepts
        + link.requirements.dimension_concepts + link.requirements.date_role_concepts
        + link.requirements.identifier_concepts
    )
    strength_counts = {
        STRENGTH_CORE: sum(1 for r in all_reqs if r.strength == STRENGTH_CORE),
        STRENGTH_CONSTRAINT: sum(1 for r in all_reqs if r.strength == STRENGTH_CONSTRAINT),
        STRENGTH_SUPPORTING: sum(1 for r in all_reqs if r.strength == STRENGTH_SUPPORTING),
        STRENGTH_UNRESOLVED: sum(1 for r in all_reqs if r.strength == STRENGTH_UNRESOLVED),
    }

    t1 = time.perf_counter()
    plan_result = plan_schema_join_subgraph(link, documents)
    planner_latency_ms = (time.perf_counter() - t1) * 1000.0

    primary = plan_result.primary_plan
    selected = tuple(sorted(primary.selected_object_fqns)) if primary else ()
    bridges = tuple(sorted(primary.bridge_object_fqns)) if primary else ()
    terminals = tuple(sorted(set(selected) - set(bridges))) if primary else ()
    join_edges = [
        {
            "from": e.from_object_fqn, "to": e.to_object_fqn, "column_pairs": list(e.column_pairs),
            "relationship_type": e.relationship_type, "join_capability": e.join_capability,
        }
        for e in (primary.join_edges if primary else ())
    ]
    reasons = [
        {"concept_key": r.concept_key, "role": r.role, "reason": r.reason, "detail": r.detail}
        for r in (primary.uncovered_requirement_reasons if primary else ())
    ]

    alt_selected = [tuple(sorted(p.selected_object_fqns)) for p in plan_result.alternative_plans]

    return {
        "case_id": case.case_id,
        "category": case.category,
        "question": case.question,
        "expects_clarification": case.expects_clarification,
        "required_object_fqns": sorted(case.required_object_fqns),
        "allowed_object_fqns": sorted(case.allowed_object_fqns) if case.allowed_object_fqns is not None else None,
        "forbidden_object_fqns": sorted(case.forbidden_object_fqns),
        "linker_status": link.status,
        "linker_anchor_fqns": linker_anchor_fqns,
        "linker_anchor_count": len(linker_anchor_fqns),
        "raw_requirement_count": len(all_reqs),
        "requirement_strength_counts": strength_counts,
        "substring_suppressed_count": link.substring_suppressed_count,
        "weak_single_token_demoted_count": link.weak_single_token_demoted_count,
        "planner_status": plan_result.status,
        "planner_error": plan_result.error,
        "planner_plan_kind": primary.plan_kind if primary else None,
        "planner_selected_object_fqns": selected,
        "planner_terminal_object_fqns": terminals,
        "planner_bridge_object_fqns": bridges,
        "planner_join_edges": join_edges,
        "planner_covered_requirement_keys": sorted(primary.covered_requirement_keys) if primary else (),
        "planner_uncovered_requirement_keys": sorted(primary.uncovered_requirement_keys) if primary else (),
        "planner_uncovered_requirement_reasons": reasons,
        "planner_confidence": round(primary.confidence, 4) if primary else None,
        "planner_score": round(primary.score, 4) if primary else None,
        "planner_limits_reached": list(plan_result.limits_reached),
        "planner_ambiguity_count": len(plan_result.ambiguity_records),
        "alternative_plan_selected_object_fqns": alt_selected,
        "alternative_plan_count": len(plan_result.alternative_plans),
        "linker_latency_ms": linker_latency_ms,
        "planner_latency_ms": planner_latency_ms,
        "combined_latency_ms": linker_latency_ms + planner_latency_ms,
    }


# ── Step 2: grade the frozen output — expected objects read here ONLY ────


def _union_find_connected(nodes: set, edges: list) -> bool:
    if len(nodes) <= 1:
        return True
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in edges:
        a, b = e["from"], e["to"]
        if a in parent and b in parent:
            union(a, b)
    return len({find(n) for n in nodes}) == 1


def _grade_case(frozen: dict, tier_by_fqn: dict) -> dict:
    required = set(frozen["required_object_fqns"])
    forbidden = set(frozen["forbidden_object_fqns"])
    allowed = set(frozen["allowed_object_fqns"]) if frozen["allowed_object_fqns"] is not None else None

    linker_anchors = set(frozen["linker_anchor_fqns"])
    linker_required_present = required & linker_anchors
    linker_complete = required.issubset(linker_anchors) if required else None

    selected = set(frozen["planner_selected_object_fqns"])
    planner_required_present = required & selected
    planner_required_missing = required - selected
    planner_complete = required.issubset(selected) if required else None

    connected = None
    if required and planner_complete:
        connected = _union_find_connected(required & selected, frozen["planner_join_edges"])

    all_considered_fqns = (
        linker_anchors | selected | {f for alt in frozen["alternative_plan_selected_object_fqns"] for f in alt}
    )
    tier34_leak = sorted(f for f in all_considered_fqns if tier_by_fqn.get(f) not in (None, *ELIGIBLE_TIERS))
    forbidden_leak = sorted(forbidden & all_considered_fqns)
    allowed_violation = sorted(selected - allowed) if allowed is not None else []
    alt_covers_required = [required.issubset(set(alt)) for alt in frozen["alternative_plan_selected_object_fqns"]]

    return {
        **frozen,
        "linker_required_present": sorted(linker_required_present),
        "linker_complete": linker_complete,
        "planner_required_present": sorted(planner_required_present),
        "planner_required_missing": sorted(planner_required_missing),
        "planner_complete": planner_complete,
        "required_objects_connected": connected,
        "tier_3_4_leakage": tier34_leak,
        "forbidden_object_leakage": forbidden_leak,
        "allowed_object_violation": allowed_violation,
        "alternative_plan_covers_required": alt_covers_required,
    }


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    assert build is not None, "no READY retrieval build found for source_id=10004"
    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    tier_by_fqn = {d.object_fqn: d.retrieval_tier for d in documents}

    print(f"Loaded build_id={build.id} status={build.status} document_count={len(documents)} (read-only, local SQLite only)")
    print(f"Golden fixture: {len(CCPP_GOLDEN_CASES)} cases from docs/validation/ccpp_golden_cases_v2.CCPP_GOLDEN_CASES")

    clear_schema_linker_cache()
    clear_join_planner_cache()
    catalog = build_semantic_catalog(documents)

    warm_link = link_question_to_schema(CCPP_GOLDEN_CASES[0].question, catalog, documents)
    plan_schema_join_subgraph(warm_link, documents)

    frozen_by_case = {case.case_id: _freeze_case_outputs(case, catalog, documents) for case in CCPP_GOLDEN_CASES}

    determinism_mismatches = []
    for case in CCPP_GOLDEN_CASES:
        repeat = _freeze_case_outputs(case, catalog, documents)
        original = frozen_by_case[case.case_id]
        if (
            repeat["planner_selected_object_fqns"] != original["planner_selected_object_fqns"]
            or repeat["planner_join_edges"] != original["planner_join_edges"]
            or repeat["planner_score"] != original["planner_score"]
        ):
            determinism_mismatches.append(case.case_id)

    graded = [_grade_case(frozen_by_case[c.case_id], tier_by_fqn) for c in CCPP_GOLDEN_CASES]

    non_clarification = [g for g in graded if not g["expects_clarification"]]
    assert len(non_clarification) == 14, f"expected 14 non-clarification cases, got {len(non_clarification)}"
    total_required = sum(len(g["required_object_fqns"]) for g in non_clarification)
    assert total_required == 24, f"expected 24 total required_object_fqns, got {total_required}"

    print("\n=== Per-case results ===")
    for g in graded:
        print(f"\n- {g['case_id']} [{g['category']}]: {g['question']!r}")
        print(f"    linker complete:        {g['linker_complete']}  anchors={g['linker_anchor_count']}")
        print(f"    raw/CORE/CONSTRAINT/SUPPORTING/UNRESOLVED: {g['raw_requirement_count']} / {g['requirement_strength_counts']}")
        print(f"    substring suppressed / weak single-token demoted: {g['substring_suppressed_count']} / {g['weak_single_token_demoted_count']}")
        print(f"    planner status/kind:    {g['planner_status']} / {g['planner_plan_kind']}")
        print(f"    planner selected:       {list(g['planner_selected_object_fqns'])}")
        print(f"    required present/missing: {g['planner_required_present']} / {g['planner_required_missing']}")
        print(f"    planner complete:       {g['planner_complete']}  connected={g['required_objects_connected']}")
        print(f"    alt plans (count/cover):{g['alternative_plan_count']} / {g['alternative_plan_covers_required']}")
        print(f"    ambiguity count:        {g['planner_ambiguity_count']}")
        print(f"    limits reached:         {g['planner_limits_reached']}")
        print(f"    tier3/4 / forbidden leakage: {g['tier_3_4_leakage']} / {g['forbidden_object_leakage']}")
        print(f"    reasons if incomplete:  {g['planner_uncovered_requirement_reasons']}")
        print(f"    latency linker/planner/combined (ms): {round(g['linker_latency_ms'],3)} / {round(g['planner_latency_ms'],3)} / {round(g['combined_latency_ms'],3)}")

    # ── Aggregates ─────────────────────────────────────────────────────
    linker_required_covered = sum(len(g["linker_required_present"]) for g in non_clarification)
    planner_required_covered = sum(len(g["planner_required_present"]) for g in non_clarification)
    linker_complete_count = sum(1 for g in non_clarification if g["linker_complete"])
    planner_complete_count = sum(1 for g in non_clarification if g["planner_complete"])
    connected_complete_count = sum(1 for g in non_clarification if g["planner_complete"] and g["required_objects_connected"])

    improved = [g["case_id"] for g in non_clarification if not g["linker_complete"] and g["planner_complete"]]
    regressed = [g["case_id"] for g in non_clarification if g["linker_complete"] and not g["planner_complete"]]
    unchanged = [g["case_id"] for g in non_clarification if g["linker_complete"] == g["planner_complete"]]

    plan_kind_counts: dict[str, int] = {}
    for g in graded:
        k = g["planner_plan_kind"]
        plan_kind_counts[k] = plan_kind_counts.get(k, 0) + 1
    non_partial_count = sum(v for k, v in plan_kind_counts.items() if k != "PARTIAL")

    raw_total = sum(g["raw_requirement_count"] for g in graded)
    core_total = sum(g["requirement_strength_counts"][STRENGTH_CORE] for g in graded)
    constraint_total = sum(g["requirement_strength_counts"][STRENGTH_CONSTRAINT] for g in graded)
    supporting_total = sum(g["requirement_strength_counts"][STRENGTH_SUPPORTING] for g in graded)
    unresolved_total = sum(g["requirement_strength_counts"][STRENGTH_UNRESOLVED] for g in graded)
    substring_suppressed_total = sum(g["substring_suppressed_count"] for g in graded)
    weak_demoted_total = sum(g["weak_single_token_demoted_count"] for g in graded)

    anchor_counts = [g["linker_anchor_count"] for g in graded]

    reason_counts: dict[str, int] = {}
    for g in graded:
        for r in g["planner_uncovered_requirement_reasons"]:
            reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
    no_path_count = reason_counts.get(REASON_NO_CONFIRMED_JOIN_PATH, 0) + reason_counts.get(REASON_LINEAGE_ONLY_PATH, 0)
    budget_exhausted_count = reason_counts.get(REASON_SEARCH_BUDGET_EXHAUSTED, 0)

    selected_counts = [len(g["planner_selected_object_fqns"]) for g in graded if g["planner_selected_object_fqns"]]
    single_object_count = sum(1 for c in selected_counts if c == 1)
    joined_count = sum(1 for c in selected_counts if c > 1)
    edge_counts = [len(g["planner_join_edges"]) for g in graded if g["planner_selected_object_fqns"]]
    alt_plan_counts = [g["alternative_plan_count"] for g in graded]

    all_tier34 = [g["case_id"] for g in graded if g["tier_3_4_leakage"]]
    all_forbidden = [g["case_id"] for g in graded if g["forbidden_object_leakage"]]
    fake_lineage_joins = [
        g["case_id"] for g in graded for e in g["planner_join_edges"]
        if e["join_capability"] == JOIN_CAPABILITY_LINEAGE_ONLY or "*" in e["column_pairs"]
    ]

    linker_latencies = sorted(g["linker_latency_ms"] for g in graded)
    planner_latencies = sorted(g["planner_latency_ms"] for g in graded)
    combined_latencies = sorted(g["combined_latency_ms"] for g in graded)

    def pct(values, p):
        idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
        return values[idx]

    aggregate = {
        "case_inventory": [c.case_id for c in CCPP_GOLDEN_CASES],
        "clarification_case_excluded": [g["case_id"] for g in graded if g["expects_clarification"]],
        "denominator_14_reconciled": len(non_clarification) == 14,
        "denominator_24_reconciled": total_required == 24,
        "raw_requirement_count_total": raw_total,
        "core_requirement_count_total": core_total,
        "constraint_requirement_count_total": constraint_total,
        "supporting_requirement_count_total": supporting_total,
        "unresolved_requirement_count_total": unresolved_total,
        "substring_suppressed_count_total": substring_suppressed_total,
        "weak_single_token_demoted_count_total": weak_demoted_total,
        "anchor_count_distribution": {
            "min": min(anchor_counts), "median": statistics.median(anchor_counts), "max": max(anchor_counts),
        },
        "linker_required_object_coverage": f"{linker_required_covered}/24",
        "linker_complete_case_coverage": f"{linker_complete_count}/14",
        "planner_required_object_coverage": f"{planner_required_covered}/24",
        "planner_primary_complete_case_coverage": f"{planner_complete_count}/14",
        "connected_required_object_coverage": f"{connected_complete_count}/14",
        "acceptance_primary_not_below_linker": planner_complete_count >= linker_complete_count,
        "acceptance_required_coverage_not_below_11": planner_required_covered >= 11,
        "acceptance_not_universally_partial": non_partial_count > 0,
        "cases_improved_vs_linker": improved,
        "cases_regressed_vs_linker": regressed,
        "cases_unchanged_vs_linker": unchanged,
        "plan_kind_distribution": plan_kind_counts,
        "single_object_plan_count": single_object_count,
        "joined_plan_count": joined_count,
        "uncovered_reason_distribution": reason_counts,
        "no_confirmed_or_lineage_only_path_count": no_path_count,
        "search_budget_exhausted_count": budget_exhausted_count,
        "cases_where_only_an_alternative_covers_required": [
            g["case_id"] for g in non_clarification
            if not g["planner_complete"] and any(g["alternative_plan_covers_required"])
        ],
        "selected_object_count_distribution": {
            "min": min(selected_counts) if selected_counts else None,
            "median": statistics.median(selected_counts) if selected_counts else None,
            "max": max(selected_counts) if selected_counts else None,
        },
        "join_edge_count_distribution": {
            "min": min(edge_counts) if edge_counts else None,
            "median": statistics.median(edge_counts) if edge_counts else None,
            "max": max(edge_counts) if edge_counts else None,
        },
        "alternative_plan_count_total": sum(alt_plan_counts),
        "tier_3_4_leakage_cases": all_tier34,
        "forbidden_object_leakage_cases": all_forbidden,
        "fake_lineage_join_cases": fake_lineage_joins,
        "linker_latency_p50_ms": round(pct(linker_latencies, 0.50), 3),
        "linker_latency_p90_ms": round(pct(linker_latencies, 0.90), 3),
        "linker_latency_p99_ms": round(pct(linker_latencies, 0.99), 3),
        "planner_latency_p50_ms": round(pct(planner_latencies, 0.50), 3),
        "planner_latency_p90_ms": round(pct(planner_latencies, 0.90), 3),
        "planner_latency_p99_ms": round(pct(planner_latencies, 0.99), 3),
        "combined_latency_p50_ms": round(pct(combined_latencies, 0.50), 3),
        "combined_latency_p90_ms": round(pct(combined_latencies, 0.90), 3),
        "combined_latency_p99_ms": round(pct(combined_latencies, 0.99), 3),
        "acceptance_combined_p99_under_300ms": pct(combined_latencies, 0.99) < 300.0,
        "determinism_mismatches": determinism_mismatches,
    }

    print("\n=== AGGREGATE ===")
    print(json.dumps(aggregate, indent=2))

    out_path = _VALIDATION_DIR / "results" / "phase6_step3c_golden_verification.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"per_case": graded, "aggregate": aggregate}, indent=2, sort_keys=False), encoding="utf-8")
    print(f"\nWrote full evaluation to {out_path}")


if __name__ == "__main__":
    main()
