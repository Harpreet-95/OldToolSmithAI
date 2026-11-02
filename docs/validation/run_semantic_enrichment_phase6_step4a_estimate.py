"""
Read-only estimate — SQL Architecture Correction, Phase 6, Step 4A.

Builds every semantic-enrichment input package/hash in memory for the
LATEST already-READY retrieval-document build for source_id=10004 (the same
CCPP data source and (source_id, user_id) pair every prior Phase 6, Step 3
read-only verification script in this directory uses), and reports:

  - eligible object count (Tier 1/2, per
    core.sqlservice.semantic_enrichment_service.ELIGIBLE_ENRICHMENT_TIERS)
  - deterministic batch count and the largest batch's character/column size
  - objects too large to fit the CONFIGURED batch limits (would raise
    SemanticEnrichmentBatchLimitError if actually run)
  - predicted cache-reuse behavior
  - an estimated provider-call count and a connection-time duration range,
    under clearly stated, unverified assumptions

This script makes NO OpenAI/provider call, NO database write, NO SQL Server
call, and never runs schema-linker/join-planner/SQL generation/execution/
answering or the locked Enterprise Acceptance Test Suite — golden accuracy
is explicitly out of scope for this estimate (per Phase 6 Step 4A
instructions). It only reads ToolSmithAI's own local SQLite metadata store
(core.sqlservice.retrieval_store) — never a live SQL Server connection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS,
)
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_enrichment_service import (
    ELIGIBLE_ENRICHMENT_TIERS,
    SKIP_REASON_INELIGIBLE_TIER,
    build_object_semantic_inputs,
    compute_object_semantic_input_hash,
)
from core.sqlservice.semantic_enrichment_service import _plan_batches  # noqa: E402  (internal, read-only inspection — same precedent as this directory's other Phase 6 Step 3 scripts importing join_planner internals)
from core.sqlservice.semantic_enrichment_service import _render_object_block  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"

# Unverified assumptions, stated explicitly (Phase 6 Step 4A instruction:
# "estimated connection-time duration using clearly stated assumptions").
# No real provider call is ever made anywhere in this script or this step.
ASSUMED_SECONDS_PER_BATCH_CALL_LOW = 3.0
ASSUMED_SECONDS_PER_BATCH_CALL_HIGH = 10.0


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        print(json.dumps({"status": "NO_READY_BUILD", "source_id": SOURCE_ID}, indent=2))
        return

    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)

    inputs, skipped = build_object_semantic_inputs(documents)
    ineligible_count = sum(1 for s in skipped if s.reason == SKIP_REASON_INELIGIBLE_TIER)

    # Per-object hash (the cache-key contract's own content-addressed
    # identity) — built for every eligible object, never persisted.
    hash_by_fqn = {obj.object_fqn: compute_object_semantic_input_hash(obj) for obj in inputs}

    # Objects too large for the CONFIGURED limits, scanned exhaustively
    # (never stopping at the first one, unlike _plan_batches itself, which
    # raises immediately — see that function's own docstring).
    oversized = []
    fitting_inputs = []
    for obj in inputs:
        block = _render_object_block(obj)
        block_chars, block_columns = len(block), len(obj.columns)
        if block_columns > SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH or block_chars > SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS:
            oversized.append({"object_fqn": obj.object_fqn, "columns": block_columns, "characters": block_chars})
        else:
            fitting_inputs.append(obj)

    truncated_by_total_cap = max(0, len(fitting_inputs) - SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS)
    batched_inputs = fitting_inputs[:SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS]

    batches = _plan_batches(
        batched_inputs,
        SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
    ) if batched_inputs else ()

    batch_sizes = [
        {"object_count": len(b), "column_count": sum(len(o.columns) for o in b),
         "character_count": sum(len(_render_object_block(o)) for o in b)}
        for b in batches
    ]
    largest_batch = max(batch_sizes, key=lambda s: s["character_count"], default=None)

    estimated_provider_calls = len(batches)  # exactly one call per planned batch (deterministic design)
    estimated_duration_low = estimated_provider_calls * ASSUMED_SECONDS_PER_BATCH_CALL_LOW
    estimated_duration_high = estimated_provider_calls * ASSUMED_SECONDS_PER_BATCH_CALL_HIGH

    report = {
        "status": "OK",
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "schema_fingerprint": build.schema_fingerprint if hasattr(build, "schema_fingerprint") else None,
        "total_documents_in_build": len(documents),
        "eligible_tiers": sorted(ELIGIBLE_ENRICHMENT_TIERS),
        "eligible_object_count": len(inputs),
        "ineligible_tier_object_count": ineligible_count,
        "objects_too_large_for_configured_limits": oversized,
        "objects_too_large_count": len(oversized),
        "truncated_by_max_total_objects": truncated_by_total_cap,
        "deterministic_batch_count": len(batches),
        "largest_batch": largest_batch,
        "unique_semantic_input_hashes": len(set(hash_by_fqn.values())),
        "cache_reuse_prediction": (
            "This is Step 4A: no persistence layer or cache table exists yet, so a REAL first run reuses "
            "nothing (reused_count=0 always today). Once a future step persists SemanticEnrichmentCacheKey "
            "rows, every object whose semantic_input_hash is unchanged across connects is fully reusable "
            "with zero provider calls for that object; only objects whose columns/types/confirmed "
            "relationships/dictionary content actually changed would be re-sent."
        ),
        "estimated_provider_call_count": estimated_provider_calls,
        "estimated_connection_time_duration_seconds": {
            "low": round(estimated_duration_low, 1),
            "high": round(estimated_duration_high, 1),
            "assumptions": (
                f"{ASSUMED_SECONDS_PER_BATCH_CALL_LOW}-{ASSUMED_SECONDS_PER_BATCH_CALL_HIGH} seconds per batch "
                "call (unverified — no real provider call is ever made in this step; based on typical "
                "structured-JSON chat-completion latency for a batch of up to "
                f"{SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH} objects), batches called strictly "
                "sequentially (no concurrency in this step's design), excluding SQL Server/network/document-"
                "build time already covered by the existing connection-time preparation stages."
            ),
        },
        "golden_accuracy_evaluated": False,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
