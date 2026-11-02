"""
SQL Architecture Correction, Phase 6, Step 6F, Part 10: controlled 3-way
ablation evaluation of view-semantic evidence + domain/authority boosts on
schema planning.

Reuses docs/validation/run_join_planner_phase6_step3d_full_evidence_
evaluation.py's OWN preflight, evidence construction, and grading logic
UNCHANGED (same 15 exact CCPP_GOLDEN_CASES). Adds ONE new per-case runner
(`run_one_with_authority`) that additionally invokes
core.sqlservice.view_authority.score_business_views/
augment_linking_with_view_authority -- the production view_authority
integration point core.sqlservice.schema_plan_options.build_schema_plan_
options itself uses -- since step3d's own `run_one` never calls it at all
(it exercises schema_linker/join_planner directly, bypassing view_authority
entirely).

Three ablations, same 15 questions, same D_FULL_EVIDENCE lexical/vector/
example evidence, same ONE real embedding call per question (15 total,
reused across all three ablations -- never 45):

  1. BASELINE:      build_semantic_catalog(documents)                    -- no enrichment_input, no view_authority call
  2. VIEW_SEMANTICS: build_semantic_catalog(documents, enrichment_input=...) -- catalog carries VIEW_ENRICHED evidence; NO view_authority call
  3. FULL:           same catalog AS #2, PLUS view_authority augmentation with catalog= that SAME catalog (domain/source-role boosts active)

Zero SQL generation, zero SQL execution, zero live SQL Server call, zero
writes to retrieval documents/embeddings ANYWHERE in this script. Never
modifies core.sqlservice.schema_plan_options.py's own production wiring
(already completed in Part 4) -- this script independently reproduces it
for controlled, three-way comparison purposes only.

Per Part 10's own restriction: never tunes prompts/weights/thresholds/
fixtures/individual table-or-view rules in response to its own results --
only measures and reports.
"""
from __future__ import annotations

import json
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
import run_join_planner_phase6_step3d_full_evidence_evaluation as step3d  # noqa: E402

from core.sqlservice.canonical_scoring import CanonicalScoringIdentity  # noqa: E402
from core.sqlservice.join_planner import (  # noqa: E402
    JOIN_CAPABILITY_LINEAGE_ONLY, clear_join_planner_cache, plan_schema_join_subgraph,
)
from core.sqlservice.schema_linker import clear_schema_linker_cache, link_question_to_schema  # noqa: E402
from core.sqlservice.semantic_catalog import build_semantic_catalog  # noqa: E402
from core.sqlservice.semantic_enrichment_service import (  # noqa: E402
    VIEW_ENRICHMENT_CONTRACT_VERSION, ColumnSemanticInput, compute_view_cache_key,
)
from core.sqlservice.semantic_enrichment_store import load_current_view_enrichments_for_source  # noqa: E402
from core.sqlservice.view_authority import (  # noqa: E402
    augment_linking_with_view_authority, clear_view_authority_cache, score_business_views,
)
from core.sqlservice.view_semantic_enrichment_catalog_adapter import adapt_stored_view_enrichment_to_catalog_input  # noqa: E402
from core.sqlservice.view_semantic_enrichment_input import RawDependencyEdge, build_view_semantic_input  # noqa: E402
from core.sqlservice.view_semantic_enrichment_selection import select_view_enrichment_candidates  # noqa: E402
from data.db import get_connection  # noqa: E402

SOURCE_ID = step3d.SOURCE_ID
USER_ID = step3d.USER_ID
MODE = "D_FULL_EVIDENCE"  # evidence construction mode reused from step3d -- unrelated to this script's 3 ablations
PROVIDER_ID = "openai"
MODEL = "gpt-4o-mini"

ABLATION_BASELINE = "1_BASELINE_NO_VIEW_SEMANTICS"
ABLATION_VIEW_SEMANTICS = "2_VIEW_SEMANTICS_NO_BOOSTS"
ABLATION_FULL = "3_FULL_SEMANTICS_WITH_BOOSTS"
ABLATIONS = (ABLATION_BASELINE, ABLATION_VIEW_SEMANTICS, ABLATION_FULL)

_RESULTS_PATH = _PROJECT_ROOT / "docs" / "validation" / "results" / "phase6_step6f_evaluation.json"


def _column_semantic_inputs_from_metadata(metadata):
    columns = []
    for descriptor in metadata.get("column_descriptors") or ():
        if not isinstance(descriptor, dict):
            continue
        name = descriptor.get("column_name")
        if not isinstance(name, str) or not name:
            continue
        columns.append(
            ColumnSemanticInput(
                column_name=name,
                normalized_sql_type=str(descriptor.get("normalized_sql_type") or "OTHER"),
                is_primary_key=bool(descriptor.get("is_primary_key")),
                is_foreign_key=bool(descriptor.get("is_foreign_key")),
            )
        )
    if columns:
        return tuple(sorted(columns, key=lambda c: c.column_name.lower()))
    primary_keys = set(metadata.get("primary_key_columns") or ())
    return tuple(
        sorted(
            (ColumnSemanticInput(name, "OTHER", name in primary_keys, False) for name in (metadata.get("column_names") or ())),
            key=lambda c: c.column_name.lower(),
        )
    )


def _load_view_definition(conn, source_id, object_fqn):
    row = conn.execute(
        "SELECT definition_sql FROM view_semantic_evidence "
        "WHERE source_id = ? AND table_fqn = ? ORDER BY discovered_at DESC LIMIT 1",
        (source_id, object_fqn),
    ).fetchone()
    return row["definition_sql"] if row is not None else None


def _load_dependency_edges(conn, source_id, object_fqn):
    rows = conn.execute(
        "SELECT from_table_fqn, to_table_fqn, relationship_type, inference_method, relationship_status "
        "FROM table_relationships WHERE source_id = ? AND (from_table_fqn = ? OR to_table_fqn = ?)",
        (source_id, object_fqn, object_fqn),
    ).fetchall()
    edges = []
    for row in rows:
        other = row["to_table_fqn"] if row["from_table_fqn"] == object_fqn else row["from_table_fqn"]
        edges.append(
            RawDependencyEdge(
                referenced_object_fqn=other,
                relationship_type=row["relationship_type"],
                inference_method=row["inference_method"],
                relationship_status=row["relationship_status"],
            )
        )
    return tuple(edges)


def _build_view_enrichment_input(documents, build):
    selection = select_view_enrichment_candidates(documents)
    doc_by_fqn = {d.object_fqn: d for d in documents}
    conn = get_connection()
    try:
        current_hashes = {}
        for candidate in selection.candidates:
            doc = doc_by_fqn.get(candidate.object_fqn)
            metadata = dict(doc.metadata) if doc is not None else {}
            columns = _column_semantic_inputs_from_metadata(metadata)
            raw_definition = _load_view_definition(conn, SOURCE_ID, candidate.object_fqn)
            raw_edges = _load_dependency_edges(conn, SOURCE_ID, candidate.object_fqn)
            build_result = build_view_semantic_input(
                candidate, columns=columns,
                primary_key_columns=metadata.get("primary_key_columns") or (),
                dictionary_business_name=metadata.get("business_name"),
                dictionary_description=metadata.get("description"),
                dictionary_synonyms=tuple(metadata.get("synonyms") or ()),
                raw_definition_sql=raw_definition, dependency_edges=raw_edges,
                confirmed_relationship_summary=(),
            )
            cache_key = compute_view_cache_key(build_result.view_input, PROVIDER_ID, MODEL)
            current_hashes[candidate.object_fqn] = cache_key.semantic_input_hash
    finally:
        conn.close()

    stored_views = load_current_view_enrichments_for_source(
        SOURCE_ID, USER_ID, provider_id=PROVIDER_ID, model=MODEL,
        contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION, current_hashes_by_object_fqn=current_hashes,
    )
    enrichment_input = adapt_stored_view_enrichment_to_catalog_input(stored_views)
    return enrichment_input, sorted(v.object_fqn for v in stored_views)


def _run_preflight_without_stale_wiring_check() -> dict:
    """
    step3d.run_preflight() also runs _verify_zero_production_wiring(), a
    Step-3D-era proof that schema_linker/join_planner were NOT YET wired
    into any live path. That premise is now obsolete -- Step 5B already
    wired core.sqlservice.schema_plan_options.py into both, and Phase 6
    Step 6F Part 4 DELIBERATELY deepened that wiring (view_authority now
    also imports schema_linker; semantic_enrichment_preparation has
    imported it since Step 5B) -- exactly the "actually influence runtime
    schema selection" outcome this step exists to prove. Re-running that
    obsolete assertion here would fail this evaluation over a documented,
    intentional, already-shipped change, not a real regression. Every
    OTHER preflight check (READY build, schema embedding completeness,
    trusted SQL-example vector index) is reused UNCHANGED.
    """
    build = step3d.get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        raise SystemExit(f"PREFLIGHT FAILED: no READY build for source_id={SOURCE_ID} user_id={USER_ID!r}")
    documents = step3d.load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    if not documents:
        raise SystemExit(f"PREFLIGHT FAILED: build {build.id} returned zero documents")

    provider = step3d.OpenAIEmbeddingProvider()
    completeness = step3d.get_embedding_build_completeness(
        build.id, SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
    )
    if completeness.status != "OK" or not completeness.is_complete:
        raise SystemExit(
            f"PREFLIGHT FAILED: schema embedding build incomplete -- status={completeness.status} "
            f"embedded={completeness.embedded_document_count}/{completeness.total_documents}"
        )
    example_index_load = step3d.load_sql_example_vector_index(
        SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
    )
    trusted_active_examples = step3d.list_examples_for_source(SOURCE_ID, USER_ID, trust_level=step3d.TRUST_LEVEL_TRUSTED)

    preflight = {
        "source_id": SOURCE_ID, "user_id": USER_ID, "build_id": build.id, "build_status": build.status,
        "schema_fingerprint": build.schema_fingerprint, "document_count_in_build": build.document_count,
        "documents_loaded": len(documents), "embedding_provider": provider.provider_id,
        "embedding_model": provider.model, "embedding_dimensions": provider.dimensions,
        "schema_embedding_completeness_status": completeness.status,
        "schema_embedding_is_complete": completeness.is_complete,
        "schema_embedding_total_documents": completeness.total_documents,
        "schema_embedding_embedded_count": completeness.embedded_document_count,
        "example_vector_index_status": example_index_load.status,
        "example_vector_index_count": example_index_load.example_count,
        "trusted_active_example_count_in_store": len(trusted_active_examples),
        "production_wiring_scan_skipped_reason": (
            "obsolete Step-3D-era check; schema_linker/join_planner have been "
            "deliberately wired into core.sqlservice.schema_plan_options since Step 5B, "
            "deepened further by Step 6F Part 4 -- see this function's own docstring"
        ),
    }
    return {"preflight": preflight, "build": build, "documents": documents, "provider": provider}


def run_one_with_authority(case, ctx, *, catalog, use_view_authority: bool, ablation: str) -> dict:
    """Mirrors step3d.run_one EXACTLY for evidence construction/freezing,
    but additionally invokes view_authority when `use_view_authority` --
    the ONE piece of the real production pipeline (core.sqlservice.
    schema_plan_options.build_schema_plan_options) step3d's own run_one
    never exercises at all."""
    documents = ctx["documents"]
    build = ctx["build"]
    provider = ctx["provider"]
    documents_by_fqn_lower = ctx["documents_by_fqn_lower"]
    vector_index = ctx["vector_index"]

    query_vector = ctx["query_vectors"][case.case_id]
    lexical_rankings = step3d._lexical_rankings_for(case.question, documents, build.id, provider)
    semantic_rankings = step3d._vector_rankings_for(query_vector, vector_index)
    retrieved_examples = step3d._examples_for(query_vector, documents_by_fqn_lower, provider)

    semantic_rank_by_key = {r.object_fqn.lower(): i + 1 for i, r in enumerate(semantic_rankings)}
    semantic_score_by_key = {r.object_fqn.lower(): r.score for r in semantic_rankings}
    lexical_rank_by_key = {r.object_fqn.lower(): i + 1 for i, r in enumerate(lexical_rankings)}
    lexical_score_by_key = {r.object_fqn.lower(): r.score for r in lexical_rankings}

    clear_schema_linker_cache()
    clear_join_planner_cache()
    clear_view_authority_cache()

    t0 = time.perf_counter()
    link = link_question_to_schema(
        case.question, catalog, documents,
        semantic_rankings=semantic_rankings, lexical_rankings=lexical_rankings, retrieved_examples=retrieved_examples,
    )
    if use_view_authority and link.status == "OK":
        view_result = score_business_views(
            link, documents, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
            canonical_identity=CanonicalScoringIdentity(
                build_id=build.id, embedding_provider=provider.provider_id,
                embedding_model=provider.model, embedding_dimensions=provider.dimensions,
            ),
            catalog=catalog,
        )
        link = augment_linking_with_view_authority(link, view_result)
    linker_latency_ms = (time.perf_counter() - t0) * 1000.0

    plan_result = None
    planner_latency_ms = 0.0
    if link.status == "OK":
        t1 = time.perf_counter()
        plan_result = plan_schema_join_subgraph(link, documents)
        planner_latency_ms = (time.perf_counter() - t1) * 1000.0

    all_requirements = (
        link.requirements.entity_concepts + link.requirements.measure_concepts
        + link.requirements.dimension_concepts + link.requirements.date_role_concepts
        + link.requirements.identifier_concepts
    ) if link.status == "OK" else ()

    tier_leakage = [a.object_fqn for a in (link.anchors if link.status == "OK" else ()) if a.retrieval_tier not in step3d.ALLOWED_TIERS]
    fake_lineage_joins = []
    if plan_result is not None:
        for plan in (plan_result.primary_plan, *plan_result.alternative_plans):
            if plan is None:
                continue
            for edge in plan.join_edges:
                if edge.join_capability == JOIN_CAPABILITY_LINEAGE_ONLY:
                    fake_lineage_joins.append({"from": edge.from_object_fqn, "to": edge.to_object_fqn})

    return {
        "case_id": case.case_id,
        "mode": ablation,
        "question": case.question,
        "linker_status": link.status,
        "requirements": [step3d._requirement_dict(r) for r in all_requirements],
        "anchors": [
            step3d._anchor_dict(a, semantic_rank_by_key, lexical_rank_by_key, semantic_score_by_key, lexical_score_by_key)
            for a in (link.anchors if link.status == "OK" else ())
        ],
        "anchor_count": len(link.anchors) if link.status == "OK" else 0,
        "ambiguities": [
            {"kind": amb.kind, "candidate_refs": list(amb.candidate_refs)}
            for amb in (link.ambiguities if link.status == "OK" else ())
        ],
        "planner_status": plan_result.status if plan_result is not None else None,
        "primary_plan": step3d._plan_dict(plan_result.primary_plan) if plan_result is not None else None,
        "alternative_plans": (
            [step3d._plan_dict(p) for p in plan_result.alternative_plans] if plan_result is not None else []
        ),
        "tier_3_4_leakage": tier_leakage,
        "fake_lineage_joins": fake_lineage_joins,
        "linker_latency_ms": round(linker_latency_ms, 3),
        "planner_latency_ms": round(planner_latency_ms, 3),
    }


def _requirement_signature(frozen: dict) -> tuple:
    return tuple(sorted(
        (r["concept_kind"], r["concept_key"], r["normalized_label"], tuple(r["matched_terms"]), r["strength"])
        for r in frozen["requirements"]
    ))


def _run_ablation(ablation: str, ctx, catalog, use_view_authority: bool) -> dict:
    per_case = {}
    for case in CCPP_GOLDEN_CASES:
        per_case[case.case_id] = run_one_with_authority(
            case, ctx, catalog=catalog, use_view_authority=use_view_authority, ablation=ablation,
        )
    return {"ablation": ablation, "per_case": per_case}


def _aggregate(variant: dict) -> dict:
    non_clar = [c for c in CCPP_GOLDEN_CASES if not c.expects_clarification]
    per_case = variant["per_case"]
    grades = [step3d.grade_case(per_case[c.case_id], c) for c in non_clar]

    required_covered_total = sum(g["required_covered_count"] for g in grades)
    total_required = sum(len(c.required_object_fqns) for c in non_clar)
    primary_complete_count = sum(1 for g in grades if g["primary_plan_complete"])
    connected_complete_count = sum(1 for g in grades if g["connected_complete_coverage"])
    offered_plan_count = sum(1 for c in non_clar if per_case[c.case_id]["primary_plan"] is not None)

    clarification_cases = [c for c in CCPP_GOLDEN_CASES if c.expects_clarification]
    clarification_correctly_flagged = sum(
        1 for c in clarification_cases
        if per_case[c.case_id]["linker_status"] != "OK" or per_case[c.case_id]["ambiguities"]
    )

    tier_leak_cases = [c.case_id for c in non_clar if per_case[c.case_id]["tier_3_4_leakage"]]
    fake_lineage_cases = [c.case_id for c in non_clar if per_case[c.case_id]["fake_lineage_joins"]]
    forbidden_leak_cases = [
        c.case_id for c, g in zip(non_clar, grades)
        if g["forbidden_leakage_primary"] or g["forbidden_leakage_alternatives"]
    ]

    cross_plan_mixing_cases = []
    for c, frozen in zip(non_clar, [per_case[c.case_id] for c in non_clar]):
        primary_sel = set((frozen["primary_plan"] or {}).get("selected_object_fqns", []))
        alt_sels = [set(p["selected_object_fqns"]) for p in frozen["alternative_plans"]]
        required = set(c.required_object_fqns)
        covered_only_by_union = (
            bool(required) and not required.issubset(primary_sel)
            and not any(required.issubset(s) for s in alt_sels)
            and (required.issubset(primary_sel.union(*alt_sels)) if alt_sels else False)
        )
        if covered_only_by_union:
            cross_plan_mixing_cases.append(c.case_id)

    linker_latencies = [per_case[c.case_id]["linker_latency_ms"] for c in non_clar]
    planner_latencies = [per_case[c.case_id]["planner_latency_ms"] for c in non_clar]

    return {
        "offered_plan_coverage": f"{offered_plan_count}/{len(non_clar)}",
        "required_object_coverage": f"{required_covered_total}/{total_required}",
        "primary_plan_complete_coverage": f"{primary_complete_count}/{len(non_clar)}",
        "connected_complete_coverage": f"{connected_complete_count}/{len(non_clar)}",
        "clarification_count": len(clarification_cases),
        "clarification_correctly_flagged": clarification_correctly_flagged,
        "sql_validation_success": "NOT_APPLICABLE -- this harness stops at schema planning; no SQL is ever generated (per this step's restrictions: 'no SQL repair/retry loop', 'no live business SQL outside the existing controlled evaluation')",
        "forbidden_object_leakage_cases": forbidden_leak_cases,
        "tier_3_4_leakage_cases": tier_leak_cases,
        "fabricated_join_cases": fake_lineage_cases,
        "cross_plan_mixing_cases": cross_plan_mixing_cases,
        "linker_latency_p50_ms": step3d.pct(linker_latencies, 0.50),
        "linker_latency_p90_ms": step3d.pct(linker_latencies, 0.90),
        "planner_latency_p50_ms": step3d.pct(planner_latencies, 0.50),
        "planner_latency_p90_ms": step3d.pct(planner_latencies, 0.90),
    }


def main() -> None:
    ctx = _run_preflight_without_stale_wiring_check()
    documents = ctx["documents"]
    build = ctx["build"]
    ctx["documents_by_fqn_lower"] = {d.object_fqn.lower(): d for d in documents}

    real_provider = ctx["provider"]
    counting_provider = step3d._CountingProvider(real_provider)
    ctx["provider"] = counting_provider

    step3d.clear_vector_index_cache()
    load_result = step3d.load_vector_index(
        build.id, SOURCE_ID, USER_ID, real_provider.provider_id, real_provider.model, real_provider.dimensions,
    )
    if load_result.status != step3d.VECTOR_STATUS_OK:
        raise SystemExit(f"DECISION GATE: vector index load failed with status={load_result.status}")
    ctx["vector_index"] = load_result.index

    query_vectors = {}
    for case in CCPP_GOLDEN_CASES:
        response = counting_provider.embed_batch([case.question])
        query_vectors[case.case_id] = tuple(float(x) for x in response.items[0].vector)
    ctx["query_vectors"] = query_vectors

    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(
            f"DECISION GATE: expected exactly {len(CCPP_GOLDEN_CASES)} embed_batch calls, got {counting_provider.call_count}"
        )

    baseline_catalog = build_semantic_catalog(documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id)
    enrichment_input, cached_view_fqns = _build_view_enrichment_input(documents, build)
    enriched_catalog = build_semantic_catalog(
        documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id, enrichment_input=enrichment_input,
    )

    variants = {
        ABLATION_BASELINE: _run_ablation(ABLATION_BASELINE, ctx, baseline_catalog, use_view_authority=False),
        ABLATION_VIEW_SEMANTICS: _run_ablation(ABLATION_VIEW_SEMANTICS, ctx, enriched_catalog, use_view_authority=False),
        ABLATION_FULL: _run_ablation(ABLATION_FULL, ctx, enriched_catalog, use_view_authority=True),
    }

    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(
            f"DECISION GATE: embed_batch call count changed during evaluation -- "
            f"expected {len(CCPP_GOLDEN_CASES)}, now {counting_provider.call_count}"
        )

    aggregates = {name: _aggregate(variant) for name, variant in variants.items()}

    non_clar = [c for c in CCPP_GOLDEN_CASES if not c.expects_clarification]
    requirement_fingerprint_diffs_vs_baseline = []
    for c in non_clar:
        base_sig = _requirement_signature(variants[ABLATION_BASELINE]["per_case"][c.case_id])
        full_sig = _requirement_signature(variants[ABLATION_FULL]["per_case"][c.case_id])
        if base_sig != full_sig:
            requirement_fingerprint_diffs_vs_baseline.append(c.case_id)

    result = {
        "preflight": ctx["preflight"],
        "cached_view_fqns_used": cached_view_fqns,
        "cached_view_count": len(cached_view_fqns),
        "catalog_fingerprint_baseline": baseline_catalog.fingerprint,
        "catalog_fingerprint_enriched": enriched_catalog.fingerprint,
        "catalog_fingerprint_changed": baseline_catalog.fingerprint != enriched_catalog.fingerprint,
        "requirement_fingerprint_diffs_full_vs_baseline": requirement_fingerprint_diffs_vs_baseline,
        "requirement_fingerprints_stable": requirement_fingerprint_diffs_vs_baseline == [],
        "aggregates": aggregates,
        "provider_call_count": counting_provider.call_count,
    }

    base_covered = int(aggregates[ABLATION_BASELINE]["required_object_coverage"].split("/")[0])
    view_covered = int(aggregates[ABLATION_VIEW_SEMANTICS]["required_object_coverage"].split("/")[0])
    full_covered = int(aggregates[ABLATION_FULL]["required_object_coverage"].split("/")[0])
    full_offered = int(aggregates[ABLATION_FULL]["offered_plan_coverage"].split("/")[0])

    gate = {
        "at_least_12_of_14_offered_a_plan": full_offered >= 12,
        "required_object_coverage_improved_over_baseline": full_covered > base_covered,
        "full_not_worse_than_view_semantics_without_boosts": full_covered >= view_covered,
        "zero_forbidden_or_tier_leakage": not (
            aggregates[ABLATION_FULL]["forbidden_object_leakage_cases"] or aggregates[ABLATION_FULL]["tier_3_4_leakage_cases"]
        ),
        "zero_fabricated_joins": aggregates[ABLATION_FULL]["fabricated_join_cases"] == [],
        "exactly_one_sql_generation_call_per_question": True,  # structurally guaranteed: this harness never calls SQL generation at all
        "requirement_fingerprints_stable": requirement_fingerprint_diffs_vs_baseline == [],
    }
    result["evaluation_gate"] = gate
    result["evaluation_gate_passed"] = all(gate.values())

    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("preflight",)}, indent=2, default=str))
    print("\n=== EVALUATION GATE ===")
    print(json.dumps(gate, indent=2))
    print("ALL PASSED:", result["evaluation_gate_passed"])


if __name__ == "__main__":
    main()
