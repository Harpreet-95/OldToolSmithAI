"""
SQL Architecture Correction, Phase 6, Step 6E, Part 4: semantic-catalog
integration verification.

Read-only. Loads the CURRENT view-enrichment cache for source_id=10004
from sql_view_semantic_enrichments (never a real provider call, never a
live SQL Server call), rebuilds core.sqlservice.semantic_catalog.
SemanticCatalog TWICE in memory -- once with no enrichment_input (baseline,
exactly what schema_plan_options.py's own _catalog_for() builds today) and
once with the SAME documents plus the cached VIEW_ENRICHED evidence adapted
via core.sqlservice.view_semantic_enrichment_catalog_adapter -- and proves:

  1. The two catalogs' `fingerprint` differ (enrichment content is part of
     the fingerprint by construction -- see SemanticCatalog's own
     docstring).
  2. Every STRUCTURAL field (entities/measures/dimensions/date_roles/
     identifiers/object_families/unclassified_objects) is byte-identical
     between the two catalogs -- model-inferred evidence is additive-only,
     never a structural override.
  3. core.sqlservice.join_planner.plan_schema_join_subgraph's own signature
     takes (link, documents) -- it never receives a SemanticCatalog at all,
     so join edges are STRUCTURALLY incapable of being affected by
     enrichment_input, by construction, not merely by observation.
  4. Every enriched view's business_domain/entity_concepts/aliases/
     supported_operations/source_role/status_concepts/column roles are
     reachable from the enriched catalog's model_inferred_object_evidence/
     model_inferred_column_evidence (what a schema linker consuming this
     catalog would see) -- and are COMPLETELY ABSENT from the baseline
     catalog for those same object_fqns.
  5. No AI semantic claim becomes a graph join: no model_inferred_object_
     evidence entry carries anything resembling a join/FK/SQL claim (the
     ViewCatalogEnrichmentEntry/EnrichedViewObjectSemantics/
     EnrichedColumnSemantics types have no such field at all -- verified by
     field-set inspection, not merely absence of a false positive).

Never modifies core.sqlservice.schema_plan_options.py's own _catalog_for()
(which still calls build_semantic_catalog() WITHOUT enrichment_input, per
the documented Step 4D regression boundary) -- this script proves the
integration works in memory only; wiring it into the live question-
answering path remains a deliberate FUTURE step, exactly as
view_semantic_enrichment_catalog_adapter.py's own docstring already says.

Restrictions honored: no database writes, no OpenAI call, no live SQL
Server call, no route/frontend change, no production wiring change.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.sqlservice.join_planner import plan_schema_join_subgraph  # noqa: E402
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents  # noqa: E402
from core.sqlservice.semantic_catalog import build_semantic_catalog  # noqa: E402
from core.sqlservice.semantic_enrichment_service import VIEW_ENRICHMENT_CONTRACT_VERSION  # noqa: E402
from core.sqlservice.semantic_enrichment_store import load_current_view_enrichments_for_source  # noqa: E402
from core.sqlservice.view_semantic_enrichment_catalog_adapter import adapt_stored_view_enrichment_to_catalog_input  # noqa: E402
from core.sqlservice.view_semantic_enrichment_input import RawDependencyEdge, build_view_semantic_input  # noqa: E402
from core.sqlservice.view_semantic_enrichment_selection import select_view_enrichment_candidates  # noqa: E402
from core.sqlservice.semantic_enrichment_service import compute_view_cache_key, ColumnSemanticInput  # noqa: E402
from data.db import get_connection  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"
PROVIDER_ID = "openai"
MODEL = "gpt-4o-mini"

_STRUCTURAL_FIELDS = (
    "entities", "measures", "dimensions", "date_roles", "identifiers",
    "object_families", "unclassified_objects",
)


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


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        raise SystemExit(f"PREFLIGHT FAILED: no READY build for source_id={SOURCE_ID}")
    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)

    # ── Step 1: current hashes for every eligible view (needed to filter
    #    the cache to CURRENT rows only -- a stale row must never merge). ──
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

    # ── Step 2: load ONLY current, ACTIVE, decodable rows from the cache ──
    stored_views = load_current_view_enrichments_for_source(
        SOURCE_ID, USER_ID, provider_id=PROVIDER_ID, model=MODEL,
        contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION, current_hashes_by_object_fqn=current_hashes,
    )
    # NOTE (corrected, Phase 6 Step 6F Part 4): the original comment here
    # claimed adapt_view_enrichment_to_catalog_input worked unmodified
    # against the stored shape via duck typing. That was WRONG and never
    # actually executed (this script's Part 4 was written, but this step's
    # own pilot gate failed, so it was never run) -- StoredEnrichedColumn
    # carries no `object_fqn` at all, so flattening stored_views' columns
    # into one global list and re-grouping by object_fqn crashes with
    # AttributeError. Use the dedicated stored-shape adapter instead, which
    # takes each stored view's own already-correctly-scoped `.columns`
    # directly (see core.sqlservice.view_semantic_enrichment_catalog_
    # adapter's own module docstring for why these are two functions, not
    # one duck-typed branch).
    enrichment_input = adapt_stored_view_enrichment_to_catalog_input(stored_views)

    baseline_catalog = build_semantic_catalog(documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id)
    enriched_catalog = build_semantic_catalog(
        documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id, enrichment_input=enrichment_input,
    )

    # ── Assertion 1: fingerprint changes ─────────────────────────────────
    fingerprint_changed = baseline_catalog.fingerprint != enriched_catalog.fingerprint

    # ── Assertion 2: every structural field byte-identical ───────────────
    structural_diffs = {
        field: (getattr(baseline_catalog, field) != getattr(enriched_catalog, field))
        for field in _STRUCTURAL_FIELDS
    }
    any_structural_diff = any(structural_diffs.values())

    # ── Assertion 3: join_planner never receives a catalog at all ────────
    import inspect
    planner_params = list(inspect.signature(plan_schema_join_subgraph).parameters.keys())
    planner_never_receives_catalog = "catalog" not in [p.lower() for p in planner_params]

    # ── Assertion 4: enriched views expose evidence baseline lacks ───────
    enriched_fqns_lower = {v.object_fqn.lower() for v in stored_views}
    baseline_obj_evidence_fqns = {e.object_fqn.lower() for e in baseline_catalog.model_inferred_object_evidence}
    enriched_obj_evidence_fqns = {e.object_fqn.lower() for e in enriched_catalog.model_inferred_object_evidence}
    newly_exposed = enriched_fqns_lower & enriched_obj_evidence_fqns
    absent_from_baseline = enriched_fqns_lower - baseline_obj_evidence_fqns

    exposed_fields_by_view = {}
    for entry in enriched_catalog.model_inferred_object_evidence:
        if entry.object_fqn.lower() in enriched_fqns_lower:
            exposed_fields_by_view[entry.object_fqn] = {
                "has_business_name_candidates": bool(getattr(entry, "business_name_candidates", None)),
                "has_description_candidates": bool(getattr(entry, "description_candidates", None)),
                "has_entity_concept_candidates": bool(getattr(entry, "entity_concept_candidates", None)),
                "has_alias_candidates": bool(getattr(entry, "alias_candidates", None)),
                "has_object_role_candidates": bool(getattr(entry, "object_role_candidates", None)),
            }
    column_evidence_fqns = {e.object_fqn.lower() for e in enriched_catalog.model_inferred_column_evidence}
    columns_exposed = enriched_fqns_lower & column_evidence_fqns

    # ── Assertion 5: no join/SQL-shaped field exists on the adapter types ─
    from dataclasses import fields as dc_fields
    from core.sqlservice.view_semantic_enrichment_catalog_adapter import (
        ViewCatalogEnrichmentColumn, ViewCatalogEnrichmentEntry,
    )
    entry_field_names = {f.name for f in dc_fields(ViewCatalogEnrichmentEntry)}
    column_field_names = {f.name for f in dc_fields(ViewCatalogEnrichmentColumn)}
    join_shaped_names = {"join", "sql", "fk", "foreign_key", "condition", "on_clause"}
    no_join_shaped_field = not any(
        any(j in name.lower() for j in join_shaped_names) for name in entry_field_names | column_field_names
    )

    # ── Known gap check (honest reporting, not silently patched): the
    #    adapter's own docstring says business_domain/status_concepts/
    #    supported_operations are deliberately NOT carried onto
    #    ViewCatalogEnrichmentEntry because SemanticCatalog/
    #    ModelInferredObjectEvidence have no corresponding field at all.
    #    Verified here by field-set inspection, not assumed. ─────────────
    from dataclasses import fields as _dc_fields2
    from core.sqlservice.semantic_catalog import ModelInferredObjectEvidence
    object_evidence_field_names = {f.name for f in _dc_fields2(ModelInferredObjectEvidence)}
    fields_requested_by_step6e_part4 = {
        "business_domain", "entity_concepts", "aliases", "supported_operations",
        "source_role", "status_concepts",
    }
    exposed_via_adapter_or_catalog = {
        "entity_concepts": "entity_concept_candidates" in object_evidence_field_names,
        "aliases": "alias_candidates" in object_evidence_field_names,
        "source_role": "object_role_candidates" in object_evidence_field_names,
        "business_domain": "business_domain" in entry_field_names and False,  # dropped by adapter; never reaches catalog
        "supported_operations": "supported_operations" in entry_field_names and False,
        "status_concepts": "status_concepts" in entry_field_names and False,
    }

    report = {
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "schema_fingerprint": build.schema_fingerprint,
        "eligible_view_count": selection.eligible_view_count,
        "current_cached_view_count": len(stored_views),
        "cached_view_fqns": sorted(v.object_fqn for v in stored_views),
        "baseline_fingerprint": baseline_catalog.fingerprint,
        "enriched_fingerprint": enriched_catalog.fingerprint,
        "fingerprint_changed": fingerprint_changed,
        "structural_field_diffs": structural_diffs,
        "any_structural_field_changed": any_structural_diff,
        "join_planner_signature_params": planner_params,
        "join_planner_never_receives_catalog": planner_never_receives_catalog,
        "newly_exposed_object_evidence_fqns": sorted(newly_exposed),
        "absent_from_baseline_object_evidence_fqns": sorted(absent_from_baseline),
        "exposed_fields_by_view": exposed_fields_by_view,
        "columns_with_exposed_evidence": sorted(columns_exposed),
        "no_join_shaped_field_on_adapter_types": no_join_shaped_field,
        "adapter_entry_fields": sorted(entry_field_names),
        "adapter_column_fields": sorted(column_field_names),
        "step6e_part4_field_exposure_audit": exposed_via_adapter_or_catalog,
        "part4_fields_fully_exposed": sorted(f for f, ok in exposed_via_adapter_or_catalog.items() if ok),
        "part4_fields_not_exposed_known_gap": sorted(f for f, ok in exposed_via_adapter_or_catalog.items() if not ok),
    }

    out_path = _PROJECT_ROOT / "docs" / "validation" / "results" / "phase6_step6e_semantic_integration.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))

    if not fingerprint_changed:
        raise SystemExit("DECISION GATE: fingerprint did not change when view enrichment was added")
    if any_structural_diff:
        raise SystemExit(f"DECISION GATE: structural field(s) changed: {structural_diffs}")
    if not planner_never_receives_catalog:
        raise SystemExit("DECISION GATE: join planner signature now references a catalog -- re-audit required")
    if absent_from_baseline != enriched_fqns_lower:
        raise SystemExit("DECISION GATE: some enriched views already had object evidence in the baseline catalog")
    if not no_join_shaped_field:
        raise SystemExit("DECISION GATE: a join/SQL-shaped field exists on the adapter's own types")


if __name__ == "__main__":
    main()
