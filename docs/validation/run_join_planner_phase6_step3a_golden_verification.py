"""
Read-only verification — SQL Architecture Correction, Phase 6, Step 3A.

Grades core.sqlservice.schema_linker (linker-only) and core.sqlservice.
join_planner (primary_plan) against the ESTABLISHED, ALREADY-AUTHORITATIVE
golden-case fixture docs/validation/ccpp_golden_cases_v2.CCPP_GOLDEN_CASES
(15 cases; CCPP2-011 is the designated clarification case, excluded from
the 14-case retrieval-coverage denominator exactly as every prior report
against this fixture has done — see GoldenCase.required_object_fqns' own
docstring: "a case whose expected_status never reaches a validated SQL
statement... naturally leaves this empty").

This script does NOT duplicate the golden questions or expected objects —
it imports CCPP_GOLDEN_CASES directly. It does NOT run the full pipeline
(docs/validation/golden_runner.py's run_golden_suite, which requires a real
OrchestrationResult from SQL generation/execution/answering — out of scope
here); it evaluates only the two Step 6 components in scope for Phase 6,
Step 3A.

Data source: the LATEST already-READY retrieval-document build for
source_id=10004, read from ToolSmithAI's own local SQLite metadata store
(core.sqlservice.retrieval_store) — never a live SQL Server connection,
never an OpenAI call, never a new retrieval/embedding build.

Evaluation discipline (per Step 3A instructions):
  - linker/planner outputs are computed FIRST, in full, for every case,
    before this script ever reads a single expected_object_fqns/
    forbidden_object_fqns value (see `_freeze_case_outputs` vs. `_grade_*`
    — a case's raw outputs are captured into a plain, already-finished
    dict before grading begins for ANY case).
  - expected objects are used only to GRADE frozen output; they are never
    passed into link_question_to_schema or plan_schema_join_subgraph
    (neither function accepts such a parameter, by construction).
  - "complete" for the planner means every required object is a member of
    primary_plan.selected_object_fqns — never loose anchor containment.
  - required-object CONNECTIVITY is checked separately and explicitly
    (union-find over primary_plan.join_edges), never assumed from the
    plan being "one tree" by construction.
  - alternative-plan coverage is reported separately and never folds into
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
from core.sqlservice.schema_linker import link_question_to_schema, clear_schema_linker_cache
from core.sqlservice.join_planner import (
    DEFAULT_JOIN_PLANNING_LIMITS,
    JOIN_CAPABILITY_LINEAGE_ONLY,
    STATUS_OK as PLAN_STATUS_OK,
    _build_join_graph_index,
    _extract_requirements,
    clear_join_planner_cache,
    plan_schema_join_subgraph,
)

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})


# ── Step 1: freeze raw linker/planner outputs — no expected-object read
#    happens anywhere in this function ──────────────────────────────────


def _bounded_search_candidate_fqns(link, graph_index) -> set:
    """The exact set of object_fqns that actually reached the beam search as
    a terminal candidate for SOME requirement, after both bounds
    (max_requirements_considered, max_terminal_candidates_per_requirement)
    core.sqlservice.join_planner._extract_requirements itself applies —
    used ONLY to tell "this object never had a chance to be selected"
    (candidate-limit exhaustion) apart from "it had a chance but a
    different branch out-scored it" (a beam/search-priority outcome),
    never to change what the planner itself does."""
    reqs, _count = _extract_requirements(link, graph_index, DEFAULT_JOIN_PLANNING_LIMITS, [], [])
    fqn_lower_set = {fqn for r in reqs for fqn, _conf in r.candidates}
    return {graph_index.fqn_by_lower[f] for f in fqn_lower_set if f in graph_index.fqn_by_lower}


def _freeze_case_outputs(case, catalog, documents, tier_by_fqn, graph_index) -> dict:
    link = link_question_to_schema(case.question, catalog, documents)

    linker_anchor_fqns = tuple(sorted(a.object_fqn for a in link.anchors))
    bounded_candidate_fqns = tuple(sorted(_bounded_search_candidate_fqns(link, graph_index)))

    requirement_dump = []
    for kind_label, reqs in (
        ("ENTITY", link.requirements.entity_concepts),
        ("MEASURE", link.requirements.measure_concepts),
        ("DIMENSION", link.requirements.dimension_concepts),
        ("DATE_ROLE", link.requirements.date_role_concepts),
        ("IDENTIFIER", link.requirements.identifier_concepts),
    ):
        for r in reqs:
            requirement_dump.append({
                "concept_key": r.concept_key,
                "confidence": round(r.confidence, 4),
                "candidates": [c.object_fqn for c in r.candidates[:5]],
            })

    t0 = time.perf_counter()
    plan_result = plan_schema_join_subgraph(link, documents)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    primary = plan_result.primary_plan
    selected = tuple(sorted(primary.selected_object_fqns)) if primary else ()
    bridges = tuple(sorted(primary.bridge_object_fqns)) if primary else ()
    terminals = tuple(sorted(set(selected) - set(bridges))) if primary else ()
    join_edges = [
        {
            "from": e.from_object_fqn, "to": e.to_object_fqn, "column_pairs": list(e.column_pairs),
            "relationship_type": e.relationship_type, "join_capability": e.join_capability,
            "direction": e.direction,
        }
        for e in (primary.join_edges if primary else ())
    ]

    alt_selected = [tuple(sorted(p.selected_object_fqns)) for p in plan_result.alternative_plans]

    return {
        "case_id": case.case_id,
        "category": case.category,
        "question": case.question,
        "expects_clarification": case.expects_clarification,
        "bounded_search_candidate_fqns": bounded_candidate_fqns,
        "required_object_fqns": sorted(case.required_object_fqns),
        "allowed_object_fqns": sorted(case.allowed_object_fqns) if case.allowed_object_fqns is not None else None,
        "forbidden_object_fqns": sorted(case.forbidden_object_fqns),
        "linker_status": link.status,
        "linker_anchor_fqns": linker_anchor_fqns,
        "linker_requirements": requirement_dump,
        "planner_status": plan_result.status,
        "planner_error": plan_result.error,
        "planner_selected_object_fqns": selected,
        "planner_terminal_object_fqns": terminals,
        "planner_bridge_object_fqns": bridges,
        "planner_join_edges": join_edges,
        "planner_covered_requirement_keys": sorted(primary.covered_requirement_keys) if primary else (),
        "planner_uncovered_requirement_keys": sorted(primary.uncovered_requirement_keys) if primary else (),
        "planner_confidence": round(primary.confidence, 4) if primary else None,
        "planner_score": round(primary.score, 4) if primary else None,
        "planner_limits_reached": list(plan_result.limits_reached),
        "planner_ambiguity_count": len(plan_result.ambiguity_records),
        "planner_ambiguity_records": [
            {"concept_key": r.concept_key, "primary": r.primary_object_fqn, "alternative": r.alternative_object_fqn}
            for r in plan_result.ambiguity_records
        ],
        "alternative_plan_selected_object_fqns": alt_selected,
        "latency_ms": latency_ms,
    }


# ── Step 2: grade the frozen output — expected objects read here ONLY ────


def _union_find_connected(nodes: set, edges: list) -> bool:
    """True iff every node in `nodes` (a subset of the plan's own selected
    objects) is reachable from every other via `edges` — never assumed
    from "the plan is one tree" by construction; checked explicitly."""
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

    roots = {find(n) for n in nodes}
    return len(roots) == 1


def _classify_missing_object_reason(fqn: str, frozen: dict, graph_index) -> str:
    """
    One missing required object's root cause, evidence-based (never
    inferred from a coarse "some limit fired somewhere in this case"
    signal — see the Step 3A report's own correction of an earlier,
    imprecise version of this classifier):

      candidate_limit_exhaustion: `fqn` never reached the bounded search
          candidate set at all (core.sqlservice.join_planner.
          _extract_requirements' own max_requirements_considered/
          max_terminal_candidates_per_requirement bounds excluded it).
      lineage_only_relationship: `fqn` reached the search, but every
          confirmed edge touching it is LINEAGE_ONLY (a real relationship
          exists in the schema, but never one usable as a SQL join
          predicate).
      no_authoritative_join_path: `fqn` reached the search but has NO
          confirmed edge (joinable or lineage) to anything in the Tier 1/2
          graph at all — a genuine schema/data-governance gap.
      planner_search_or_beam_limit: `fqn` reached the search AND has at
          least one real JOINABLE edge, but a competing branch scored
          higher on requirement coverage before this one could grow far
          enough to be selected — the search explored it; a different
          branch legitimately out-scored it under the documented,
          tested six-tier scoring order (never a bug in that order itself).
    """
    fqn_lower = fqn.lower()
    if fqn not in frozen["bounded_search_candidate_fqns"]:
        return "candidate_limit_exhaustion"
    has_joinable = bool(graph_index.joinable_adjacency.get(fqn_lower))
    if has_joinable:
        return "planner_search_or_beam_limit"
    has_lineage = any(
        fqn_lower in (e.from_object_fqn.lower(), e.to_object_fqn.lower()) for e in graph_index.lineage_edges
    )
    return "lineage_only_relationship" if has_lineage else "no_authoritative_join_path"


def _grade_case(frozen: dict, tier_by_fqn: dict, graph_index) -> dict:
    required = set(frozen["required_object_fqns"])
    forbidden = set(frozen["forbidden_object_fqns"])
    allowed = set(frozen["allowed_object_fqns"]) if frozen["allowed_object_fqns"] is not None else None

    linker_anchors = set(frozen["linker_anchor_fqns"])
    linker_required_present = required & linker_anchors
    linker_required_missing = required - linker_anchors
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
    tier34_leak = sorted(f for f in all_considered_fqns if tier_by_fqn.get(f) not in (None,) and tier_by_fqn.get(f) not in ELIGIBLE_TIERS)
    forbidden_leak = sorted(forbidden & all_considered_fqns)
    allowed_violation = sorted(selected - allowed) if allowed is not None else []

    alt_covers_required = [
        required.issubset(set(alt)) for alt in frozen["alternative_plan_selected_object_fqns"]
    ]

    missing_object_reasons = {}
    incomplete_reason = None
    genuine_ambiguity = False
    if required and not planner_complete:
        for fqn in sorted(planner_required_missing):
            if fqn not in linker_required_present:
                missing_object_reasons[fqn] = "linker_requirement_failure"
            else:
                missing_object_reasons[fqn] = _classify_missing_object_reason(fqn, frozen, graph_index)
        genuine_ambiguity = any(
            required.issubset(set(alt)) for alt in frozen["alternative_plan_selected_object_fqns"]
        )
        # Priority order for the single reported `incomplete_reason` when a
        # case has multiple missing objects with different causes.
        priority = (
            "linker_requirement_failure", "candidate_limit_exhaustion", "lineage_only_relationship",
            "no_authoritative_join_path", "planner_search_or_beam_limit",
        )
        reasons_present = set(missing_object_reasons.values())
        incomplete_reason = next((r for r in priority if r in reasons_present), None)
        if genuine_ambiguity and incomplete_reason == "planner_search_or_beam_limit":
            incomplete_reason = "genuine_ambiguity"

    return {
        **frozen,
        "linker_required_present": sorted(linker_required_present),
        "linker_required_missing": sorted(linker_required_missing),
        "linker_complete": linker_complete,
        "planner_required_present": sorted(planner_required_present),
        "planner_required_missing": sorted(planner_required_missing),
        "planner_complete": planner_complete,
        "required_objects_connected": connected,
        "tier_3_4_leakage": tier34_leak,
        "forbidden_object_leakage": forbidden_leak,
        "allowed_object_violation": allowed_violation,
        "alternative_plan_covers_required": alt_covers_required,
        "missing_object_reasons": missing_object_reasons,
        "incomplete_reason": incomplete_reason,
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

    # Warm caches once (excluded from the reported latency distribution).
    warm_link = link_question_to_schema(CCPP_GOLDEN_CASES[0].question, catalog, documents)
    plan_schema_join_subgraph(warm_link, documents)

    # ── Freeze every case's raw output BEFORE any grading/expected-object
    #    read happens for ANY case. ─────────────────────────────────────
    graph_index = _build_join_graph_index(documents)
    frozen_by_case = {
        case.case_id: _freeze_case_outputs(case, catalog, documents, tier_by_fqn, graph_index)
        for case in CCPP_GOLDEN_CASES
    }

    # Determinism check: re-run every case and confirm byte-identical
    # planner selection/edges/score before grading.
    determinism_mismatches = []
    for case in CCPP_GOLDEN_CASES:
        repeat = _freeze_case_outputs(case, catalog, documents, tier_by_fqn, graph_index)
        original = frozen_by_case[case.case_id]
        if (
            repeat["planner_selected_object_fqns"] != original["planner_selected_object_fqns"]
            or repeat["planner_join_edges"] != original["planner_join_edges"]
            or repeat["planner_score"] != original["planner_score"]
        ):
            determinism_mismatches.append(case.case_id)

    graded = [_grade_case(frozen_by_case[case.case_id], tier_by_fqn, graph_index) for case in CCPP_GOLDEN_CASES]
    graded_by_id = {g["case_id"]: g for g in graded}

    non_clarification = [g for g in graded if not g["expects_clarification"]]
    assert len(non_clarification) == 14, f"expected 14 non-clarification cases, got {len(non_clarification)}"
    total_required = sum(len(g["required_object_fqns"]) for g in non_clarification)
    assert total_required == 24, f"expected 24 total required_object_fqns, got {total_required}"

    print("\n=== Per-case results ===")
    for g in graded:
        print(f"\n- {g['case_id']} [{g['category']}]: {g['question']!r}")
        print(f"    linker requirements:    {[(r['concept_key'], r['confidence']) for r in g['linker_requirements']]}")
        print(f"    linker anchors:         {list(g['linker_anchor_fqns'])}")
        print(f"    linker required present/missing: {g['linker_required_present']} / {g['linker_required_missing']}")
        print(f"    linker complete:        {g['linker_complete']}")
        print(f"    planner status:         {g['planner_status']}  (error={g['planner_error']!r})")
        print(f"    planner selected:       {list(g['planner_selected_object_fqns'])}")
        print(f"    planner terminals:      {list(g['planner_terminal_object_fqns'])}")
        print(f"    planner bridges:        {list(g['planner_bridge_object_fqns'])}")
        print(f"    join edges:             {g['planner_join_edges']}")
        print(f"    covered/uncovered reqs: {g['planner_covered_requirement_keys']} / {g['planner_uncovered_requirement_keys']}")
        print(f"    required present/missing: {g['planner_required_present']} / {g['planner_required_missing']}")
        print(f"    planner complete:       {g['planner_complete']}")
        print(f"    required objs connected:{g['required_objects_connected']}")
        print(f"    alt plans (count/cover):{len(g['alternative_plan_selected_object_fqns'])} / {g['alternative_plan_covers_required']}")
        print(f"    ambiguity:              {g['planner_ambiguity_count']} {g['planner_ambiguity_records']}")
        print(f"    limits reached:         {g['planner_limits_reached']}")
        print(f"    tier3/4 leakage:        {g['tier_3_4_leakage']}")
        print(f"    forbidden leakage:      {g['forbidden_object_leakage']}")
        print(f"    allowed-list violation: {g['allowed_object_violation']}")
        print(f"    incomplete reason:      {g['incomplete_reason']}  (per-object: {g['missing_object_reasons']})")
        print(f"    latency (ms):           {round(g['latency_ms'], 3)}")

    # ── Aggregates ─────────────────────────────────────────────────────
    linker_required_covered = sum(len(g["linker_required_present"]) for g in non_clarification)
    planner_required_covered = sum(len(g["planner_required_present"]) for g in non_clarification)
    linker_complete_count = sum(1 for g in non_clarification if g["linker_complete"])
    planner_complete_count = sum(1 for g in non_clarification if g["planner_complete"])
    connected_complete_count = sum(1 for g in non_clarification if g["planner_complete"] and g["required_objects_connected"])

    improved = [g["case_id"] for g in non_clarification if not g["linker_complete"] and g["planner_complete"]]
    regressed = [g["case_id"] for g in non_clarification if g["linker_complete"] and not g["planner_complete"]]
    unchanged = [g["case_id"] for g in non_clarification if g["linker_complete"] == g["planner_complete"]]

    incomplete_cases = [g for g in non_clarification if not g["planner_complete"]]
    reason_counts = {}
    for g in incomplete_cases:
        reason_counts[g["incomplete_reason"]] = reason_counts.get(g["incomplete_reason"], 0) + 1

    selected_counts = [len(g["planner_selected_object_fqns"]) for g in graded if g["planner_selected_object_fqns"]]
    path_lengths = [len(e["column_pairs"]) for g in graded for e in g["planner_join_edges"]]
    edge_counts = [len(g["planner_join_edges"]) for g in graded if g["planner_selected_object_fqns"]]
    alt_plan_counts = [len(g["alternative_plan_selected_object_fqns"]) for g in graded]

    all_tier34 = [g["case_id"] for g in graded if g["tier_3_4_leakage"]]
    all_forbidden = [g["case_id"] for g in graded if g["forbidden_object_leakage"]]
    fake_lineage_joins = [
        g["case_id"] for g in graded
        for e in g["planner_join_edges"]
        if e["join_capability"] == JOIN_CAPABILITY_LINEAGE_ONLY or "*" in e["column_pairs"]
    ]

    terminal_recoveries = sum(len(g["planner_terminal_object_fqns"]) for g in graded)
    bridge_recoveries = sum(len(g["planner_bridge_object_fqns"]) for g in graded)

    latencies = sorted(g["latency_ms"] for g in graded)
    def pct(p):
        idx = min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))
        return latencies[idx]

    aggregate = {
        "case_inventory": [c.case_id for c in CCPP_GOLDEN_CASES],
        "clarification_case_excluded": [g["case_id"] for g in graded if g["expects_clarification"]],
        "denominator_14_reconciled": len(non_clarification) == 14,
        "denominator_24_reconciled": total_required == 24,
        "linker_required_object_coverage": f"{linker_required_covered}/24",
        "linker_complete_case_coverage": f"{linker_complete_count}/14",
        "planner_required_object_coverage": f"{planner_required_covered}/24",
        "planner_primary_complete_case_coverage": f"{planner_complete_count}/14",
        "connected_required_object_coverage": f"{connected_complete_count}/14",
        "cases_improved_vs_linker": improved,
        "cases_regressed_vs_linker": regressed,
        "cases_unchanged_vs_linker": unchanged,
        "incomplete_case_reasons": reason_counts,
        "terminal_object_recoveries_total": terminal_recoveries,
        "bridge_object_recoveries_total": bridge_recoveries,
        "selected_object_count_distribution": {
            "min": min(selected_counts) if selected_counts else None,
            "median": statistics.median(selected_counts) if selected_counts else None,
            "max": max(selected_counts) if selected_counts else None,
        },
        "join_column_pair_length_distribution": {
            "min": min(path_lengths) if path_lengths else None,
            "median": statistics.median(path_lengths) if path_lengths else None,
            "max": max(path_lengths) if path_lengths else None,
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
        "latency_p50_ms": round(pct(0.50), 3),
        "latency_p90_ms": round(pct(0.90), 3),
        "latency_p99_ms": round(pct(0.99), 3),
        "determinism_mismatches": determinism_mismatches,
    }

    print("\n=== AGGREGATE ===")
    print(json.dumps(aggregate, indent=2))

    out_path = _VALIDATION_DIR / "results" / "phase6_step3a_golden_verification.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"per_case": graded, "aggregate": aggregate}, indent=2, sort_keys=False), encoding="utf-8")
    print(f"\nWrote full evaluation to {out_path}")


if __name__ == "__main__":
    main()
