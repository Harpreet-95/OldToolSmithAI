"""
Read-only synthetic verification — SQL Architecture Correction, Phase 6,
Step 4B1, Part 8.

Uses FAKE (never real-provider-produced) validated model enrichment for a
small sample of REAL objects already present in CCPP's own latest READY
retrieval build (source_id=10004, build_id=6 — the same source_id/user_id
every prior Phase 6 read-only script in this directory uses) to prove,
against real production schema shape:

  - the merged SemanticCatalog's own fingerprint changes when usable model
    evidence is added;
  - core.sqlservice.schema_linker's own requirement_fingerprint for a
    representative sample of generic questions does NOT change (Part 1's
    invariant, verified against real data, not just synthetic fixtures);
  - the linker's own cached _LinkerIndex correctly grows a new entry
    (never reuses the un-enriched one);
  - at least one requirement's candidate set CAN change (a real,
    observable effect of the merge);
  - how many of build 6's eligible objects currently lack typed
    column_descriptors (Phase 6 Step 1A field) — reaffirming that live
    Step 4C must first create/reuse a CURRENT typed retrieval build before
    running real enrichment against this source.

No real OpenAI/provider call, no database write, no live retrieval build,
no SQL Server call, no golden/accuracy evaluation anywhere in this script.
Reads ONLY ToolSmithAI's own local SQLite metadata store (core.sqlservice.
retrieval_store).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.schema_linker import (
    clear_schema_linker_cache,
    link_question_to_schema,
    prepare_linker_index,
    schema_linker_cache_size,
)
from core.sqlservice.semantic_catalog import MODEL_INFERRED_PROVENANCE_KIND, build_semantic_catalog
from core.sqlservice.semantic_enrichment_service import ELIGIBLE_ENRICHMENT_TIERS

SOURCE_ID = 10004
USER_ID = "28"
SAMPLE_ENTITY_COUNT = 5


class _FakeEvidence:
    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail


_PROVENANCE = (_FakeEvidence(MODEL_INFERRED_PROVENANCE_KIND, "synthetic-estimate"),)


class _FakeEnrichment:
    """Duck-typed StoredSemanticEnrichment stand-in — never a real
    provider/persistence-layer object. Mirrors the exact shape
    core.sqlservice.semantic_catalog.build_semantic_catalog's own
    `enrichment_input` parameter expects (see that function's docstring)."""

    def __init__(self, object_fqn: str, *, business_name: str, confidence: float = 0.4) -> None:
        self.object_fqn = object_fqn
        self.business_name = business_name
        self.description = None
        self.object_role = "ENTITY"
        self.entity_concepts: tuple[str, ...] = ()
        self.aliases: tuple[str, ...] = ()
        self.supported_operations: tuple[str, ...] = ()
        self.confidence = confidence
        self.columns: tuple = ()
        self.provenance = _PROVENANCE


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        print(json.dumps({"status": "NO_READY_BUILD", "source_id": SOURCE_ID}, indent=2))
        return

    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    documents_by_fqn = {d.object_fqn: d for d in documents}

    objects_missing_typed_descriptors = sum(
        1 for d in documents
        if d.retrieval_tier in ELIGIBLE_ENRICHMENT_TIERS and not d.metadata.get("column_descriptors")
    )
    eligible_object_count = sum(1 for d in documents if d.retrieval_tier in ELIGIBLE_ENRICHMENT_TIERS)

    baseline_catalog = build_semantic_catalog(documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id)

    # ── Pick a real, deterministic sample of existing entity concepts, and
    #    for each, propose ONE additional REAL object (already in this
    #    build, not one of that entity's own real candidates) as a fake
    #    model-inferred business_name match — simulating "an opaque real
    #    table gains an alias for a concept the schema already expresses",
    #    without ever calling a provider or fabricating a nonexistent
    #    object/column. ──────────────────────────────────────────────────
    sample_entities = [e for e in baseline_catalog.entities if e.candidate_objects][:SAMPLE_ENTITY_COUNT]
    already_used_fqns = {c.object_fqn for e in sample_entities for c in e.candidate_objects}
    donor_pool = [
        d.object_fqn for d in documents
        if d.retrieval_tier in ELIGIBLE_ENRICHMENT_TIERS and d.object_fqn not in already_used_fqns
    ]

    enrichment_input = []
    sample_report = []
    for i, entity in enumerate(sample_entities):
        if i >= len(donor_pool):
            break
        donor_fqn = donor_pool[i]
        enrichment_input.append(_FakeEnrichment(donor_fqn, business_name=entity.normalized_name, confidence=0.4))
        sample_report.append({
            "entity_concept": entity.normalized_name,
            "existing_real_candidate": entity.candidate_objects[0].object_fqn,
            "fake_donor_object_gaining_alias": donor_fqn,
        })

    enriched_catalog = build_semantic_catalog(
        documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id,
        enrichment_input=tuple(enrichment_input),
    )

    fingerprint_changed = enriched_catalog.fingerprint != baseline_catalog.fingerprint

    # ── Requirement invariance + candidate-change proof, over a small,
    #    generic, representative sample of questions built from the SAME
    #    sample entities' own real normalized names (never a fabricated or
    #    dataset-specific phrase). ───────────────────────────────────────
    clear_schema_linker_cache()
    requirement_checks = []
    any_candidate_set_changed = False
    for entity in sample_entities:
        question = f"How many {entity.normalized_name} are there"
        baseline_result = link_question_to_schema(question, baseline_catalog, documents)
        enriched_result = link_question_to_schema(question, enriched_catalog, documents)

        baseline_req = next(
            (r for r in baseline_result.requirements.entity_concepts if r.normalized_label == entity.normalized_name),
            None,
        )
        enriched_req = next(
            (r for r in enriched_result.requirements.entity_concepts if r.normalized_label == entity.normalized_name),
            None,
        )
        baseline_candidates = {c.object_fqn for c in baseline_req.candidates} if baseline_req else set()
        enriched_candidates = {c.object_fqn for c in enriched_req.candidates} if enriched_req else set()
        if enriched_candidates != baseline_candidates:
            any_candidate_set_changed = True

        requirement_checks.append({
            "question": question,
            "requirement_fingerprint_unchanged": (
                baseline_result.requirement_fingerprint == enriched_result.requirement_fingerprint
            ),
            "candidate_set_changed": enriched_candidates != baseline_candidates,
            "added_candidates": sorted(enriched_candidates - baseline_candidates),
        })

    linker_cache_size_after_baseline = None
    linker_cache_size_after_enriched = None
    clear_schema_linker_cache()
    prepare_linker_index(baseline_catalog, documents)
    linker_cache_size_after_baseline = schema_linker_cache_size()
    prepare_linker_index(enriched_catalog, documents)
    linker_cache_size_after_enriched = schema_linker_cache_size()
    clear_schema_linker_cache()

    report = {
        "status": "OK",
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "eligible_object_count": eligible_object_count,
        "objects_missing_typed_descriptors": objects_missing_typed_descriptors,
        "sample": sample_report,
        "catalog_fingerprint_changed": fingerprint_changed,
        "linker_index_cache_size_after_baseline_prepare": linker_cache_size_after_baseline,
        "linker_index_cache_size_after_enriched_prepare": linker_cache_size_after_enriched,
        "linker_index_grew_a_new_entry": linker_cache_size_after_enriched == linker_cache_size_after_baseline + 1,
        "requirement_invariance_checks": requirement_checks,
        "all_requirement_fingerprints_unchanged": all(c["requirement_fingerprint_unchanged"] for c in requirement_checks),
        "at_least_one_candidate_set_changed": any_candidate_set_changed,
        "step_4c_prerequisite": (
            f"{objects_missing_typed_descriptors}/{eligible_object_count} eligible build-6 objects have NO "
            "column_descriptors at all -- every one of them falls back to core.sqlservice.semantic_enrichment_"
            "service's legacy column path (normalized_sql_type='OTHER' for every column, never guessed). "
            "Live Step 4C must first create or reuse a CURRENT retrieval build that actually populates "
            "column_descriptors for this source before running real enrichment against it -- otherwise every "
            "column-level model measure/date-role/identifier claim would be admitted only at the down-weighted "
            "'unknown type' confidence, never at full confidence, regardless of how accurate the model's real "
            "type judgment actually is."
        ),
        "golden_accuracy_evaluated": False,
        "real_provider_calls_made": 0,
        "database_writes_made": 0,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
