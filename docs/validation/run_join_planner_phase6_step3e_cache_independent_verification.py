"""
SQL Architecture Correction — Phase 6, Step 3E: cache-independent schema
linking / join planning verification.

Read-only evaluation. For each of the 15 exact established CCPP_GOLDEN_
CASES, and for each of four ablation modes (A: catalog only, B: +lexical,
C: +lexical+vector, D: +lexical+vector+trusted examples), runs BOTH a
COLD (cache cleared immediately before) and a WARM (cache already
populated) execution and asserts the structural (non-timing) output is
byte-identical between them — the direct regression proof for Step 3D's
Finding 1 (operation_time_budget_ms previously started before the join-
graph index was built, making cold/warm plans diverge).

Exactly ONE real question-embedding call is made per question (15 total)
— the SAME query vector is reused for vector schema ranking AND trusted
SQL-example retrieval, exactly as in Step 3D. Zero database writes, zero
SQL Server calls, zero SQL-generation/execution/answer-generation calls.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_VALIDATION_DIR = Path(__file__).resolve().parent
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402

from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.embedding_store import get_embedding_build_completeness  # noqa: E402
from core.sqlservice.join_planner import (  # noqa: E402
    JOIN_CAPABILITY_LINEAGE_ONLY,
    PLAN_KIND_AMBIGUOUS,
    PLAN_KIND_JOINED_SUBGRAPH,
    PLAN_KIND_PARTIAL,
    PLAN_KIND_SINGLE_OBJECT,
    STATUS_INDEX_BUILD_FAILED as PLANNER_STATUS_INDEX_BUILD_FAILED,
    clear_join_planner_cache,
    plan_schema_join_subgraph,
    prepare_schema_planning_indexes,
)
from core.sqlservice.lexical_index import (  # noqa: E402
    LexicalIndexIdentity,
    load_lexical_index,
    search_lexical_index,
    tokenize,
)
from core.sqlservice.retrieval_store import (  # noqa: E402
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.schema_linker import (  # noqa: E402
    STATUS_INDEX_BUILD_FAILED as LINKER_STATUS_INDEX_BUILD_FAILED,
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    STRENGTH_SUPPORTING,
    STRENGTH_UNRESOLVED,
    clear_schema_linker_cache,
    link_question_to_schema,
)
from core.sqlservice.semantic_catalog import build_semantic_catalog  # noqa: E402
from core.sqlservice.sql_example_model import TRUST_LEVEL_TRUSTED  # noqa: E402
from core.sqlservice.sql_example_retrieval import retrieve_sql_examples  # noqa: E402
from core.sqlservice.sql_example_store import list_examples_for_source  # noqa: E402
from core.sqlservice.sql_example_vector_index import load_sql_example_vector_index  # noqa: E402
from core.sqlservice.vector_index import (  # noqa: E402
    STATUS_OK as VECTOR_STATUS_OK,
    clear_vector_index_cache,
    load_vector_index,
    search_vector_index,
)

SOURCE_ID = 10004
USER_ID = "28"
ALLOWED_TIERS = frozenset({1, 2})
MODES = ("A_CATALOG_ONLY", "B_CATALOG_LEXICAL", "C_CATALOG_LEXICAL_VECTOR", "D_FULL_EVIDENCE")

_RESULTS_PATH = _PROJECT_ROOT / "docs" / "validation" / "results" / "phase6_step3e_cache_independent_verification.json"


def _verify_zero_production_wiring() -> list[str]:
    offending: list[str] = []
    skip_dirs = {"tests", "docs", ".git", "venv", "node_modules", "__pycache__"}
    for py_file in _PROJECT_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_PROJECT_ROOT)
        parts = rel.parts
        if parts[0] in skip_dirs:
            continue
        if parts[:2] == ("core", "sqlservice") and parts[-1] in ("schema_linker.py", "join_planner.py"):
            continue
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        if "schema_linker import" in text or "join_planner import" in text:
            offending.append(str(rel))
    return offending


def run_preflight() -> dict:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        raise SystemExit(f"PREFLIGHT FAILED: no READY build for source_id={SOURCE_ID} user_id={USER_ID!r}")
    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    if not documents:
        raise SystemExit(f"PREFLIGHT FAILED: build {build.id} returned zero documents")

    provider = OpenAIEmbeddingProvider()
    completeness = get_embedding_build_completeness(
        build.id, SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
    )
    if completeness.status != "OK" or not completeness.is_complete:
        raise SystemExit(f"PREFLIGHT FAILED: schema embedding incomplete — {completeness}")

    example_index_load = load_sql_example_vector_index(
        SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
    )
    trusted_active_examples = list_examples_for_source(SOURCE_ID, USER_ID, trust_level=TRUST_LEVEL_TRUSTED)
    offending_imports = _verify_zero_production_wiring()
    if offending_imports:
        raise SystemExit(f"DECISION GATE: schema_linker/join_planner imported outside tests/docs: {offending_imports}")

    preflight = {
        "source_id": SOURCE_ID, "user_id": USER_ID, "build_id": build.id, "build_status": build.status,
        "schema_fingerprint": build.schema_fingerprint, "document_count_in_build": build.document_count,
        "documents_loaded": len(documents),
        "embedding_provider": provider.provider_id, "embedding_model": provider.model,
        "embedding_dimensions": provider.dimensions,
        "schema_embedding_completeness_status": completeness.status,
        "schema_embedding_is_complete": completeness.is_complete,
        "example_vector_index_status": example_index_load.status,
        "example_vector_index_count": example_index_load.example_count,
        "trusted_active_example_count_in_store": len(trusted_active_examples),
        "rollout_flags_disabled": True,
        "production_wiring_scan_offending_files": offending_imports,
    }
    return {"preflight": preflight, "build": build, "documents": documents, "provider": provider}


class _CountingProvider:
    def __init__(self, inner):
        self._inner = inner
        self.call_count = 0
        self.call_log: list[str] = []

    @property
    def provider_id(self):
        return self._inner.provider_id

    @property
    def model(self):
        return self._inner.model

    @property
    def dimensions(self):
        return self._inner.dimensions

    def embed_batch(self, texts):
        self.call_count += 1
        self.call_log.append(f"call#{self.call_count}: {len(texts)} text(s)")
        return self._inner.embed_batch(texts)


def _lexical_rankings_for(question, documents, build_id, provider):
    identity = LexicalIndexIdentity(
        build_id=build_id, embedding_provider=provider.provider_id,
        embedding_model=provider.model, embedding_dimensions=provider.dimensions,
    )
    lexical_index = load_lexical_index(documents, identity)
    tokens = tokenize(question)
    if not tokens:
        return []
    ranked = search_lexical_index(lexical_index, tokens, top_k=lexical_index.document_count or 1)
    return [r for r in ranked if r.retrieval_tier in ALLOWED_TIERS]


def _vector_rankings_for(query_vector, vector_index):
    ranked = search_vector_index(vector_index, query_vector, top_k=len(vector_index.documents) or 1)
    return [r for r in ranked if r.retrieval_tier in ALLOWED_TIERS]


def _examples_for(query_vector, documents_by_fqn_lower, provider):
    result = retrieve_sql_examples(
        query_vector, SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
        documents_by_fqn_lower, allowed_tiers=ALLOWED_TIERS,
    )
    return result.examples if result.status == "OK" else ()


def _requirement_dict(req) -> dict:
    return {
        "concept_kind": req.concept_kind, "concept_key": req.concept_key,
        "normalized_label": req.normalized_label, "strength": req.strength, "ambiguous": req.ambiguous,
        "top_candidates": [{"object_fqn": c.object_fqn, "match_kind": c.match_kind} for c in req.candidates[:5]],
    }


def _plan_dict(plan) -> dict | None:
    if plan is None:
        return None
    return {
        "selected_object_fqns": list(plan.selected_object_fqns), "plan_kind": plan.plan_kind,
        "join_edges": [
            {"from": e.from_object_fqn, "to": e.to_object_fqn, "column_pairs": list(e.column_pairs),
             "join_capability": e.join_capability}
            for e in plan.join_edges
        ],
        "anchor_object_fqns": list(plan.anchor_object_fqns), "bridge_object_fqns": list(plan.bridge_object_fqns),
        "covered_requirement_keys": list(plan.covered_requirement_keys),
        "uncovered_requirement_keys": list(plan.uncovered_requirement_keys),
    }


def run_one(case, mode, ctx, evidence_cache: dict) -> dict:
    """Builds evidence ONCE per (case, mode) via evidence_cache, then runs
    link+plan. Structural output (everything except *_ms timing fields) is
    what cold/warm comparison uses."""
    documents = ctx["documents"]
    catalog = ctx["catalog"]
    provider = ctx["provider"]
    documents_by_fqn_lower = ctx["documents_by_fqn_lower"]
    vector_index = ctx.get("vector_index")

    cache_key = (case.case_id, mode)
    if cache_key not in evidence_cache:
        semantic_rankings: list = []
        lexical_rankings: list = []
        retrieved_examples: tuple = ()
        if mode in ("B_CATALOG_LEXICAL", "C_CATALOG_LEXICAL_VECTOR", "D_FULL_EVIDENCE"):
            lexical_rankings = _lexical_rankings_for(case.question, documents, ctx["build"].id, provider)
        if mode in ("C_CATALOG_LEXICAL_VECTOR", "D_FULL_EVIDENCE"):
            query_vector = ctx["query_vectors"][case.case_id]
            semantic_rankings = _vector_rankings_for(query_vector, vector_index)
        if mode == "D_FULL_EVIDENCE":
            query_vector = ctx["query_vectors"][case.case_id]
            retrieved_examples = _examples_for(query_vector, documents_by_fqn_lower, provider)
        evidence_cache[cache_key] = (semantic_rankings, lexical_rankings, retrieved_examples)
    semantic_rankings, lexical_rankings, retrieved_examples = evidence_cache[cache_key]

    t0 = time.perf_counter()
    link = link_question_to_schema(
        case.question, catalog, documents,
        semantic_rankings=semantic_rankings, lexical_rankings=lexical_rankings, retrieved_examples=retrieved_examples,
    )
    linker_wall_ms = (time.perf_counter() - t0) * 1000.0

    plan_result = None
    if link.status == "OK":
        plan_result = plan_schema_join_subgraph(link, documents)

    all_requirements = (
        link.requirements.entity_concepts + link.requirements.measure_concepts
        + link.requirements.dimension_concepts + link.requirements.date_role_concepts
        + link.requirements.identifier_concepts
    ) if link.status == "OK" else ()

    tier_leakage = [a.object_fqn for a in (link.anchors if link.status == "OK" else ()) if a.retrieval_tier not in ALLOWED_TIERS]
    fake_lineage_joins = []
    if plan_result is not None:
        for plan in (plan_result.primary_plan, *plan_result.alternative_plans):
            if plan is None:
                continue
            for edge in plan.join_edges:
                if edge.join_capability == JOIN_CAPABILITY_LINEAGE_ONLY:
                    fake_lineage_joins.append({"from": edge.from_object_fqn, "to": edge.to_object_fqn})

    structural = {
        "case_id": case.case_id, "mode": mode,
        "linker_status": link.status,
        "requirements": [_requirement_dict(r) for r in sorted(all_requirements, key=lambda r: r.concept_key)],
        "strength_counts": {
            STRENGTH_CORE: sum(1 for r in all_requirements if r.strength == STRENGTH_CORE),
            STRENGTH_CONSTRAINT: sum(1 for r in all_requirements if r.strength == STRENGTH_CONSTRAINT),
            STRENGTH_SUPPORTING: sum(1 for r in all_requirements if r.strength == STRENGTH_SUPPORTING),
            STRENGTH_UNRESOLVED: sum(1 for r in all_requirements if r.strength == STRENGTH_UNRESOLVED),
        },
        "anchors": sorted(a.object_fqn for a in (link.anchors if link.status == "OK" else ())),
        "anchor_count": len(link.anchors) if link.status == "OK" else 0,
        "planner_status": plan_result.status if plan_result is not None else None,
        "primary_plan": _plan_dict(plan_result.primary_plan) if plan_result is not None else None,
        "alternative_plans": [_plan_dict(p) for p in plan_result.alternative_plans] if plan_result is not None else [],
        "limits_reached": sorted(plan_result.limits_reached) if plan_result is not None else [],
        "tier_3_4_leakage": tier_leakage,
        "fake_lineage_joins": fake_lineage_joins,
    }
    timing = {
        "linker_index_load_or_build_ms": round(link.index_load_or_build_ms, 3) if link.status == "OK" else None,
        "linker_question_linking_ms": round(link.question_linking_ms, 3) if link.status == "OK" else None,
        "linker_wall_ms": round(linker_wall_ms, 3),
        "planner_index_load_or_build_ms": round(plan_result.index_load_or_build_ms, 3) if plan_result is not None else None,
        "planner_join_search_ms": round(plan_result.join_search_ms, 3) if plan_result is not None else None,
        "planner_total_ms": round(plan_result.total_ms, 3) if plan_result is not None else None,
    }
    return {"structural": structural, "timing": timing}


def grade_case(structural: dict, case) -> dict:
    required = set(case.required_object_fqns)
    forbidden = set(case.forbidden_object_fqns)
    primary = structural["primary_plan"]
    primary_selected = set(primary["selected_object_fqns"]) if primary else set()
    alt_selected_list = [set(p["selected_object_fqns"]) for p in structural["alternative_plans"]]
    primary_complete = required.issubset(primary_selected) if required else None
    any_alt_complete = any(required.issubset(s) for s in alt_selected_list) if required else False
    return {
        "case_id": case.case_id,
        "required_covered_count": len(required & primary_selected),
        "required_total_count": len(required),
        "primary_plan_complete": primary_complete,
        "connected_complete_coverage": bool(primary_complete) or any_alt_complete,
        "forbidden_leakage": sorted((forbidden & primary_selected) | (forbidden & set().union(*alt_selected_list, set()))),
    }


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
    return round(values[idx], 3)


def main() -> None:
    ctx = run_preflight()
    print("=== PREFLIGHT ===")
    print(json.dumps(ctx["preflight"], indent=2, default=str))

    documents = ctx["documents"]
    build = ctx["build"]
    catalog = build_semantic_catalog(documents)
    ctx["catalog"] = catalog
    ctx["documents_by_fqn_lower"] = {d.object_fqn.lower(): d for d in documents}

    real_provider = ctx["provider"]
    counting_provider = _CountingProvider(real_provider)
    ctx["provider"] = counting_provider

    clear_vector_index_cache()
    load_result = load_vector_index(
        build.id, SOURCE_ID, USER_ID, real_provider.provider_id, real_provider.model, real_provider.dimensions,
    )
    if load_result.status != VECTOR_STATUS_OK:
        raise SystemExit(f"DECISION GATE: vector index load failed with status={load_result.status}")
    ctx["vector_index"] = load_result.index

    query_vectors: dict[str, tuple] = {}
    print(f"\n=== Embedding {len(CCPP_GOLDEN_CASES)} questions (one call each) ===")
    for case in CCPP_GOLDEN_CASES:
        response = counting_provider.embed_batch([case.question])
        query_vectors[case.case_id] = tuple(float(x) for x in response.items[0].vector)
        print(f"  {case.case_id}: embedded")
    ctx["query_vectors"] = query_vectors
    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(f"DECISION GATE: expected {len(CCPP_GOLDEN_CASES)} embed calls, got {counting_provider.call_count}")

    # Connection-time preparation, exercised once against the real schema.
    prep_result = prepare_schema_planning_indexes(catalog, documents)
    print(f"\nprepare_schema_planning_indexes: status={prep_result.status} "
          f"eligible={prep_result.eligible_object_count} edges={prep_result.joinable_edge_count} "
          f"ms={round(prep_result.index_load_or_build_ms, 2)}")

    evidence_cache: dict = {}
    per_case_mode_warm: dict[str, dict] = {}
    structural_mismatches: list[str] = []

    def _structural_json(d: dict) -> str:
        return json.dumps(d, sort_keys=True)

    for mode in MODES:
        print(f"\n=== MODE {mode} ===")
        for case in CCPP_GOLDEN_CASES:
            # COLD: clear both caches immediately before.
            clear_schema_linker_cache()
            clear_join_planner_cache()
            cold = run_one(case, mode, ctx, evidence_cache)
            # WARM: caches now populated from the cold run above; call again.
            warm = run_one(case, mode, ctx, evidence_cache)

            if _structural_json(cold["structural"]) != _structural_json(warm["structural"]):
                structural_mismatches.append(f"{case.case_id}/{mode}")

            per_case_mode_warm[f"{case.case_id}/{mode}"] = warm
            grade = grade_case(warm["structural"], case)
            plan = warm["structural"]["primary_plan"]
            print(
                f"  {case.case_id}: cold==warm={_structural_json(cold['structural']) == _structural_json(warm['structural'])} "
                f"plan_kind={(plan or {}).get('plan_kind')} required={grade['required_covered_count']}/{grade['required_total_count']}"
            )

    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(f"DECISION GATE: embed call count changed — now {counting_provider.call_count}")

    non_clarification_cases = [c for c in CCPP_GOLDEN_CASES if not c.expects_clarification]
    assert len(non_clarification_cases) == 14
    assert sum(len(c.required_object_fqns) for c in non_clarification_cases) == 24

    aggregates: dict[str, dict] = {}
    all_tier_leaks: list[str] = []
    all_fake_lineage: list[str] = []
    all_forbidden_leaks: list[str] = []

    for mode in MODES:
        entries = [per_case_mode_warm[f"{c.case_id}/{mode}"] for c in non_clarification_cases]
        grades = [grade_case(e["structural"], c) for c, e in zip(non_clarification_cases, entries)]

        core_total = sum(e["structural"]["strength_counts"][STRENGTH_CORE] for e in entries)
        constraint_total = sum(e["structural"]["strength_counts"][STRENGTH_CONSTRAINT] for e in entries)
        supporting_total = sum(e["structural"]["strength_counts"][STRENGTH_SUPPORTING] for e in entries)
        unresolved_total = sum(e["structural"]["strength_counts"][STRENGTH_UNRESOLVED] for e in entries)

        anchor_counts = [e["structural"]["anchor_count"] for e in entries]
        plan_kinds = [(e["structural"]["primary_plan"] or {}).get("plan_kind") for e in entries]

        linker_index_ms = [e["timing"]["linker_index_load_or_build_ms"] for e in entries if e["timing"]["linker_index_load_or_build_ms"] is not None]
        linker_link_ms = [e["timing"]["linker_question_linking_ms"] for e in entries if e["timing"]["linker_question_linking_ms"] is not None]
        planner_index_ms = [e["timing"]["planner_index_load_or_build_ms"] for e in entries if e["timing"]["planner_index_load_or_build_ms"] is not None]
        planner_search_ms = [e["timing"]["planner_join_search_ms"] for e in entries if e["timing"]["planner_join_search_ms"] is not None]
        combined_warm_ms = [
            e["timing"]["linker_question_linking_ms"] + e["timing"]["planner_join_search_ms"]
            for e in entries
            if e["timing"]["linker_question_linking_ms"] is not None and e["timing"]["planner_join_search_ms"] is not None
        ]

        tier_leaks = [c.case_id for c, e in zip(non_clarification_cases, entries) if e["structural"]["tier_3_4_leakage"]]
        fake_lineage = [c.case_id for c, e in zip(non_clarification_cases, entries) if e["structural"]["fake_lineage_joins"]]
        forbidden_leaks = [c.case_id for c, g in zip(non_clarification_cases, grades) if g["forbidden_leakage"]]
        all_tier_leaks.extend(tier_leaks)
        all_fake_lineage.extend(fake_lineage)
        all_forbidden_leaks.extend(forbidden_leaks)

        aggregates[mode] = {
            "core_requirement_count_total": core_total,
            "constraint_requirement_count_total": constraint_total,
            "supporting_requirement_count_total": supporting_total,
            "unresolved_requirement_count_total": unresolved_total,
            "anchor_count_distribution": {
                "min": min(anchor_counts), "median": statistics.median(anchor_counts), "max": max(anchor_counts),
            },
            "required_object_coverage": f"{sum(g['required_covered_count'] for g in grades)}/24",
            "primary_complete_case_coverage": f"{sum(1 for g in grades if g['primary_plan_complete'])}/14",
            "connected_complete_coverage": f"{sum(1 for g in grades if g['connected_complete_coverage'])}/14",
            "plan_kind_distribution": {
                "SINGLE_OBJECT": plan_kinds.count(PLAN_KIND_SINGLE_OBJECT),
                "JOINED_SUBGRAPH": plan_kinds.count(PLAN_KIND_JOINED_SUBGRAPH),
                "PARTIAL": plan_kinds.count(PLAN_KIND_PARTIAL),
                "AMBIGUOUS": plan_kinds.count(PLAN_KIND_AMBIGUOUS),
                "NONE": plan_kinds.count(None),
            },
            "linker_index_load_or_build_ms_p50": pct(linker_index_ms, 0.50),
            "linker_index_load_or_build_ms_p99": pct(linker_index_ms, 0.99),
            "linker_question_linking_ms_p50": pct(linker_link_ms, 0.50),
            "linker_question_linking_ms_p99": pct(linker_link_ms, 0.99),
            "planner_index_load_or_build_ms_p50": pct(planner_index_ms, 0.50),
            "planner_index_load_or_build_ms_p99": pct(planner_index_ms, 0.99),
            "planner_join_search_ms_p50": pct(planner_search_ms, 0.50),
            "planner_join_search_ms_p99": pct(planner_search_ms, 0.99),
            "warm_combined_linker_planner_p50_ms": pct(combined_warm_ms, 0.50),
            "warm_combined_linker_planner_p99_ms": pct(combined_warm_ms, 0.99),
            "tier_3_4_leakage_cases": tier_leaks,
            "fake_lineage_join_cases": fake_lineage,
            "forbidden_object_leakage_cases": forbidden_leaks,
        }

    # CCPP2-009 / CCPP2-012 generic evidence-ordering diagnosis.
    special_diagnosis = {}
    for case_id in ("CCPP2-009", "CCPP2-012"):
        case = next(c for c in CCPP_GOLDEN_CASES if c.case_id == case_id)
        rows = {}
        for mode in MODES:
            e = per_case_mode_warm[f"{case_id}/{mode}"]
            plan = e["structural"]["primary_plan"]
            rows[mode] = {
                "primary_selected": (plan or {}).get("selected_object_fqns"),
                "plan_kind": (plan or {}).get("plan_kind"),
                "anchor_fqns": e["structural"]["anchors"],
            }
        special_diagnosis[case_id] = {
            "question": case.question, "required_object_fqns": sorted(case.required_object_fqns), "by_mode": rows,
        }

    # ADF_Path-style synonym-alternative recovery, checked generically: for
    # every case whose required objects are NOT fully covered by the
    # primary plan, is a required object present as at least an ANCHOR
    # (reachable) in mode D — i.e. does it now surface as a candidate at
    # all, regardless of whether the planner ultimately selects it.
    synonym_alternative_recovery = []
    for case in non_clarification_cases:
        e = per_case_mode_warm[f"{case.case_id}/D_FULL_EVIDENCE"]
        anchors = set(e["structural"]["anchors"])
        required = set(case.required_object_fqns)
        recovered_as_anchor = sorted(required & anchors)
        missing_entirely = sorted(required - anchors)
        if recovered_as_anchor or missing_entirely:
            synonym_alternative_recovery.append({
                "case_id": case.case_id, "recovered_as_anchor": recovered_as_anchor, "missing_entirely": missing_entirely,
            })

    result = {
        "preflight": ctx["preflight"],
        "provider_call_count": counting_provider.call_count,
        "prepare_schema_planning_indexes": {
            "status": prep_result.status, "eligible_object_count": prep_result.eligible_object_count,
            "joinable_edge_count": prep_result.joinable_edge_count,
        },
        "structural_cold_warm_mismatches": structural_mismatches,
        "aggregates": aggregates,
        "special_diagnosis_ccpp2_009_012": special_diagnosis,
        "synonym_alternative_recovery": synonym_alternative_recovery,
        "per_case_mode_warm": per_case_mode_warm,
    }
    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(result, indent=2, sort_keys=False), encoding="utf-8")

    print("\n=== AGGREGATES ===")
    print(json.dumps(aggregates, indent=2))
    print(f"\nstructural_cold_warm_mismatches = {structural_mismatches}")
    print(f"Wrote full results to {_RESULTS_PATH}")

    if structural_mismatches:
        raise SystemExit(f"DECISION GATE: cold/warm structural mismatch: {structural_mismatches}")
    if all_tier_leaks or all_fake_lineage or all_forbidden_leaks:
        raise SystemExit(
            f"DECISION GATE: leakage detected — tier={all_tier_leaks} fake_lineage={all_fake_lineage} "
            f"forbidden={all_forbidden_leaks}"
        )

    mode_d_primary = int(aggregates["D_FULL_EVIDENCE"]["primary_complete_case_coverage"].split("/")[0])
    if mode_d_primary < 7:
        raise SystemExit(f"DECISION GATE: mode D primary complete coverage {mode_d_primary}/14 below the 7/14 floor")


if __name__ == "__main__":
    main()
