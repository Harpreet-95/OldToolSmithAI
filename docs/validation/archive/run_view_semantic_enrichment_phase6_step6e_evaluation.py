"""
SQL Architecture Correction, Phase 6, Step 6E, Part 5: controlled
before/after evaluation of view-semantic evidence on schema planning.

Reuses docs/validation/run_join_planner_phase6_step3d_full_evidence_
evaluation.py's OWN preflight, per-case runner, and grading functions
UNCHANGED (same 15 exact CCPP_GOLDEN_CASES, same D_FULL_EVIDENCE evidence
construction, same grade_case logic) -- the ONLY variable this script
changes is which SemanticCatalog is passed into ctx before running: the
prior step's own baseline build_semantic_catalog(documents) call, versus
the SAME call plus the current view-enrichment cache's enrichment_input
(loaded read-only from sql_view_semantic_enrichments via
core.sqlservice.view_semantic_enrichment_catalog_adapter, exactly as Part 4
of this same step already verified in isolation).

Exactly ONE real question-embedding call per question (15 total) is made,
ONCE, and REUSED for both catalog variants -- never 30. Zero SQL
generation, zero SQL execution, zero live SQL Server call, zero writes to
retrieval documents/embeddings. Never modifies core.sqlservice.
schema_plan_options.py's own production catalog construction.

Per Part 5's own restriction: this script never tunes prompts, weights,
thresholds, fixtures, or individual table/view rules in response to its
own results -- it only measures and reports.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_VALIDATION_DIR = Path(__file__).resolve().parent
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402
import run_join_planner_phase6_step3d_full_evidence_evaluation as step3d  # noqa: E402

from core.sqlservice.semantic_catalog import build_semantic_catalog  # noqa: E402
from core.sqlservice.semantic_enrichment_service import (  # noqa: E402
    VIEW_ENRICHMENT_CONTRACT_VERSION, ColumnSemanticInput, compute_view_cache_key,
)
from core.sqlservice.semantic_enrichment_store import load_current_view_enrichments_for_source  # noqa: E402
from core.sqlservice.view_semantic_enrichment_catalog_adapter import adapt_view_enrichment_to_catalog_input  # noqa: E402
from core.sqlservice.view_semantic_enrichment_input import RawDependencyEdge, build_view_semantic_input  # noqa: E402
from core.sqlservice.view_semantic_enrichment_selection import select_view_enrichment_candidates  # noqa: E402
from data.db import get_connection  # noqa: E402

SOURCE_ID = step3d.SOURCE_ID
USER_ID = step3d.USER_ID
MODE = "D_FULL_EVIDENCE"
PROVIDER_ID = "openai"
MODEL = "gpt-4o-mini"

_RESULTS_PATH = _PROJECT_ROOT / "docs" / "validation" / "results" / "phase6_step6e_evaluation.json"


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


def _build_view_enriched_catalog(documents, build):
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
    stored_columns = [c for v in stored_views for c in v.columns]
    enrichment_input = adapt_view_enrichment_to_catalog_input(stored_views, stored_columns)
    catalog = build_semantic_catalog(
        documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id, enrichment_input=enrichment_input,
    )
    return catalog, sorted(v.object_fqn for v in stored_views)


def _requirement_signature(frozen: dict) -> tuple:
    """Coarse, catalog-content-independent signature of the QUESTION-
    PARSING stage alone (what concepts this question needs) -- deliberately
    excludes `candidate_total_count`/`top_candidates`, which enrichment is
    EXPECTED to change (more/better candidate evidence for the SAME
    concept). Used to prove "requirement fingerprints remain stable where
    semantics should only affect candidate evidence" (Part 5's evaluation
    gate) as an explicit, reproducible measurement, not an assumption."""
    return tuple(sorted(
        (r["concept_kind"], r["concept_key"], r["normalized_label"], tuple(r["matched_terms"]), r["strength"])
        for r in frozen["requirements"]
    ))


def _run_variant(ctx, label: str) -> dict:
    per_case = {}
    for case in CCPP_GOLDEN_CASES:
        frozen = step3d.run_one(case, MODE, ctx)
        per_case[case.case_id] = frozen
    return {"label": label, "per_case": per_case}


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

    # "Cross-plan mixing": grade_case only ever tests `required.issubset()`
    # against ONE plan's own selected_object_fqns at a time (primary, or
    # each alternative independently) -- it is structurally incapable of
    # combining objects from two different plans into one coverage claim.
    # Verified here by re-deriving the same check with an explicit assertion
    # rather than merely trusting the prior sentence.
    cross_plan_mixing_cases = []
    for c, frozen in zip(non_clar, [per_case[c.case_id] for c in non_clar]):
        primary_sel = set((frozen["primary_plan"] or {}).get("selected_object_fqns", []))
        alt_sels = [set(p["selected_object_fqns"]) for p in frozen["alternative_plans"]]
        required = set(c.required_object_fqns)
        covered_only_by_union_across_plans = (
            required and not required.issubset(primary_sel)
            and not any(required.issubset(s) for s in alt_sels)
            and required.issubset(primary_sel.union(*alt_sels)) if alt_sels else False
        )
        if covered_only_by_union_across_plans:
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
        "sql_validation_success": "NOT_APPLICABLE -- this harness stops at schema planning; no SQL is ever generated or executed (per this step's own restrictions)",
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
    ctx = step3d.run_preflight()
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

    # ── Baseline: exactly what schema_plan_options.py builds today ───────
    baseline_catalog = build_semantic_catalog(documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id)
    ctx["catalog"] = baseline_catalog
    baseline_variant = _run_variant(ctx, "baseline_no_view_semantics")

    # ── Enriched: baseline + current view-enrichment cache ────────────────
    enriched_catalog, cached_view_fqns = _build_view_enriched_catalog(documents, build)
    ctx["catalog"] = enriched_catalog
    enriched_variant = _run_variant(ctx, "view_semantics_enabled")

    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(
            f"DECISION GATE: embed_batch call count changed during evaluation -- "
            f"expected {len(CCPP_GOLDEN_CASES)}, now {counting_provider.call_count}"
        )

    baseline_agg = _aggregate(baseline_variant)
    enriched_agg = _aggregate(enriched_variant)

    # ── Requirement fingerprint stability (Part 5 evaluation gate) ───────
    non_clar = [c for c in CCPP_GOLDEN_CASES if not c.expects_clarification]
    requirement_fingerprint_diffs = []
    for c in non_clar:
        base_sig = _requirement_signature(baseline_variant["per_case"][c.case_id])
        enr_sig = _requirement_signature(enriched_variant["per_case"][c.case_id])
        if base_sig != enr_sig:
            requirement_fingerprint_diffs.append(c.case_id)

    result = {
        "preflight": ctx["preflight"],
        "cached_view_fqns_used": cached_view_fqns,
        "cached_view_count": len(cached_view_fqns),
        "catalog_fingerprint_baseline": baseline_catalog.fingerprint,
        "catalog_fingerprint_enriched": enriched_catalog.fingerprint,
        "catalog_fingerprint_changed": baseline_catalog.fingerprint != enriched_catalog.fingerprint,
        "requirement_fingerprint_diffs": requirement_fingerprint_diffs,
        "requirement_fingerprints_stable": requirement_fingerprint_diffs == [],
        "baseline": baseline_agg,
        "view_semantics_enabled": enriched_agg,
        "provider_call_count": counting_provider.call_count,
    }

    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))

    # ── Evaluation gate ────────────────────────────────────────────────
    base_covered = int(baseline_agg["required_object_coverage"].split("/")[0])
    enr_covered = int(enriched_agg["required_object_coverage"].split("/")[0])
    offered_count = int(enriched_agg["offered_plan_coverage"].split("/")[0])

    gate = {
        "required_object_coverage_improved": enr_covered > base_covered,
        "offered_plan_coverage_at_least_12_of_14": offered_count >= 12,
        "no_forbidden_or_tier_leakage": not (enriched_agg["forbidden_object_leakage_cases"] or enriched_agg["tier_3_4_leakage_cases"]),
        "no_fabricated_joins": enriched_agg["fabricated_join_cases"] == [],
        "requirement_fingerprints_stable": requirement_fingerprint_diffs == [],
    }
    result["evaluation_gate"] = gate
    result["evaluation_gate_passed"] = all(gate.values())
    _RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print("\n=== EVALUATION GATE ===")
    print(json.dumps(gate, indent=2))
    print("ALL PASSED:", result["evaluation_gate_passed"])


if __name__ == "__main__":
    main()
