"""
Read-only planning report — SQL Architecture Correction, Phase 6, Step 4B,
Part 8.

Extends docs/validation/run_semantic_enrichment_phase6_step4a_estimate.py
(unchanged, never edited — that script's own numbers are reused here, not
recomputed) with the Step 4B-specific persistence/preparation metrics:
current cache completeness, typed-descriptor availability, the predicted
retrieval-build document-set identity change once column_descriptors is
added, predicted embedding reuse, and first-vs-repeat preparation duration
estimates.

Reads ONLY ToolSmithAI's own local SQLite metadata store (core.sqlservice.
retrieval_store / core.sqlservice.embedding_store) for source_id=10004 (the
same CCPP data source every prior Phase 6 read-only script in this
directory uses) — never a live SQL Server connection, never an OpenAI
call, never a database write, never a new retrieval/embedding build, and
never core.sqlservice.semantic_enrichment_store (that table does not exist
in the live database yet — see "current cache completeness" below, which
is exactly why it is reported as zero rather than queried).

Restrictions honored: no real provider calls, no live migration/database
writes, no live retrieval build, no SQL Server calls, no golden/accuracy
evaluation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH
from core.sqlservice.embedding_store import get_embedding_build_completeness
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_enrichment_service import (
    ELIGIBLE_ENRICHMENT_TIERS,
    SKIP_REASON_INELIGIBLE_TIER,
    build_object_semantic_inputs,
)
from data.db import get_connection

SOURCE_ID = 10004
USER_ID = "28"

# Reused verbatim from the real run of
# docs/validation/run_semantic_enrichment_phase6_step4a_estimate.py against
# this exact build (see that script's own docstring/output; NOT recomputed
# here to avoid two scripts silently drifting on the same claim). If build 6
# changes, re-run that script first and update this comment together with
# the constants below.
STEP_4A_ESTIMATE = {
    "build_id": 6,
    "eligible_object_count": 1023,
    "deterministic_batch_count": 60,
    "estimated_provider_call_count": 60,
    "estimated_connection_time_duration_seconds": {"low": 180.0, "high": 600.0},
}

# First-run per-object/per-batch latency assumption, same figures Step 4A's
# own estimate already used (never a NEW, undocumented assumption).
ASSUMED_SECONDS_PER_BATCH_CALL_LOW = 3.0
ASSUMED_SECONDS_PER_BATCH_CALL_HIGH = 10.0
# A repeat run's per-object cost is a local hash computation plus one
# indexed SQLite lookup per eligible object — no network call at all.
ASSUMED_SECONDS_PER_OBJECT_REPEAT_LOOKUP = 0.0005


def _semantic_enrichments_table_exists() -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sql_semantic_enrichments'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        print(json.dumps({"status": "NO_READY_BUILD", "source_id": SOURCE_ID}, indent=2))
        return

    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    eligible_inputs, skipped = build_object_semantic_inputs(documents)
    eligible_object_count = len(eligible_inputs)
    objects_missing_typed_descriptors = sum(
        1 for doc in documents
        if doc.retrieval_tier in ELIGIBLE_ENRICHMENT_TIERS and not doc.metadata.get("column_descriptors")
    )
    ineligible_tier_count = sum(1 for s in skipped if s.reason == SKIP_REASON_INELIGIBLE_TIER)

    # ── Current cache completeness (Part 8, bullet 1) ───────────────────
    #
    # Migration 015 has NOT been applied to the live database in this step
    # (restriction: "No live migration or database writes") -- confirmed
    # directly rather than assumed.
    table_exists = _semantic_enrichments_table_exists()
    cache_completeness = {
        "table_exists_in_live_database": table_exists,
        "cached_current_count": 0,
        "requested_count": eligible_object_count,
        "missing_count": eligible_object_count,
        "note": (
            "The sql_semantic_enrichments table does not exist yet in the live database "
            "(migration 015 was intentionally never applied here) -- current cache "
            "completeness is therefore 0/{} by construction, not merely because no row "
            "happens to match.".format(eligible_object_count)
            if not table_exists else
            "Table exists but was not queried for source_id={} in this read-only report.".format(SOURCE_ID)
        ),
    }

    # ── Typed-descriptor availability (Part 5 / Part 8, bullet 2) ───────
    typed_descriptor_report = {
        "eligible_object_count": eligible_object_count,
        "objects_missing_typed_descriptors": objects_missing_typed_descriptors,
        "objects_with_typed_descriptors": eligible_object_count - objects_missing_typed_descriptors,
        "note": (
            "Build 6 predates core.sqlservice.retrieval_documents' column_descriptors field "
            "(Phase 6 Step 1A) entirely -- every eligible object falls back to "
            "core.sqlservice.semantic_enrichment_service._column_inputs_from_legacy_fields, "
            "reporting every column's type as 'OTHER' (never guessed)."
        ),
    }

    # ── Predicted document-set identity change (Part 8, bullet 3) ──────
    #
    # Analytically proven from core.sqlservice.retrieval_store.
    # compute_document_set_hash's own definition (hashes recursively-
    # canonicalized METADATA, which includes column_descriptors when
    # present) and empirically demonstrated at unit scale by
    # tests/test_sqlservice_semantic_enrichment_step4b_build_compatibility.py
    # ::test_adding_column_descriptors_creates_new_build_but_reuses_document_hash_and_embeddings
    # -- NOT run against build 6's real 1406 objects here, since that would
    # require a live SQL Server rebuild (explicitly out of scope).
    document_set_identity_prediction = {
        "would_create_new_build": True,
        "document_hash_unchanged": True,
        "reason": (
            "column_descriptors is metadata, not document_text -- document_hash "
            "(sha256 of document_text) is provably unaffected, while document_set_hash "
            "(which DOES fold in canonicalized metadata) provably changes the instant a "
            "single object's column_descriptors goes from absent to present."
        ),
        "verified_by": "tests/test_sqlservice_semantic_enrichment_step4b_build_compatibility.py",
    }

    # ── Predicted embedding reuse (Part 8, bullet 4) — REAL numbers ─────
    embedding_identities_conn = get_connection()
    try:
        embedding_identities = [
            tuple(r) for r in embedding_identities_conn.execute(
                "SELECT DISTINCT embedding_provider, embedding_model, embedding_dimensions FROM sql_retrieval_embeddings"
            ).fetchall()
        ]
    finally:
        embedding_identities_conn.close()

    embedding_reuse_predictions = []
    for provider_id, model, dimensions in embedding_identities:
        completeness = get_embedding_build_completeness(build.id, SOURCE_ID, USER_ID, provider_id, model, dimensions)
        embedding_reuse_predictions.append({
            "embedding_provider": provider_id, "embedding_model": model, "embedding_dimensions": dimensions,
            "total_documents": completeness.total_documents,
            "currently_missing": len(completeness.missing_document_hashes),
            "predicted_reused_after_column_descriptors_added": (
                completeness.total_documents - len(completeness.missing_document_hashes)
            ),
            "predicted_regenerated_after_column_descriptors_added": 0,
        })

    # ── Expected object/batch/provider-call count (Part 8, bullet 5) ───
    # Reused verbatim from the real Step 4A estimate run (see
    # STEP_4A_ESTIMATE's own comment) -- consistent with this report's own
    # freshly-recomputed eligible_object_count above (both read the same
    # live build 6).
    assert STEP_4A_ESTIMATE["eligible_object_count"] == eligible_object_count, (
        "build 6 has changed since the Step 4A estimate was last captured -- "
        "re-run run_semantic_enrichment_phase6_step4a_estimate.py and update STEP_4A_ESTIMATE"
    )

    # ── First vs. repeat preparation duration (Part 8, bullet 6) ────────
    first_run_duration = dict(STEP_4A_ESTIMATE["estimated_connection_time_duration_seconds"])
    repeat_run_duration_seconds = round(eligible_object_count * ASSUMED_SECONDS_PER_OBJECT_REPEAT_LOOKUP, 3)

    report = {
        "status": "OK",
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "eligible_object_count": eligible_object_count,
        "ineligible_tier_object_count": ineligible_tier_count,
        "current_cache_completeness": cache_completeness,
        "typed_descriptor_availability": typed_descriptor_report,
        "document_set_identity_prediction": document_set_identity_prediction,
        "embedding_reuse_prediction": embedding_reuse_predictions,
        "expected_object_batch_provider_call_count": {
            "eligible_object_count": STEP_4A_ESTIMATE["eligible_object_count"],
            "deterministic_batch_count": STEP_4A_ESTIMATE["deterministic_batch_count"],
            "estimated_provider_call_count": STEP_4A_ESTIMATE["estimated_provider_call_count"],
            "max_objects_per_batch_config": SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
        },
        "estimated_preparation_duration_seconds": {
            "first_run": {
                **first_run_duration,
                "assumptions": (
                    f"{ASSUMED_SECONDS_PER_BATCH_CALL_LOW}-{ASSUMED_SECONDS_PER_BATCH_CALL_HIGH}s per batch call "
                    f"({STEP_4A_ESTIMATE['deterministic_batch_count']} batches, sequential) -- identical to Step 4A's "
                    "own estimate, since a first run's provider-call cost is unchanged by adding persistence."
                ),
            },
            "repeat_run_unchanged_schema": {
                "seconds": repeat_run_duration_seconds,
                "assumptions": (
                    f"{eligible_object_count} eligible objects x ~{ASSUMED_SECONDS_PER_OBJECT_REPEAT_LOOKUP}s "
                    "each for a local semantic-input-hash computation plus one indexed sql_semantic_enrichments "
                    "lookup -- zero provider calls (every object's exact cache key already matches)."
                ),
            },
        },
        "golden_accuracy_evaluated": False,
        "real_enrichment_run": False,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
