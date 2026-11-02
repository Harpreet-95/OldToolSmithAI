"""
Read-only planning report — SQL Architecture Correction, Phase 6, Step
4C1, Part 9.

Reads ONLY ToolSmithAI's own local SQLite metadata store (core.sqlservice.
retrieval_store) for source_id=10004 (the same CCPP data source every prior
Phase 6 read-only script in this directory uses), against the real, already-
committed build 7 (the typed retrieval build created in Step 4C, Part 2 --
1,406 documents, column_descriptors on all of them). Never a live SQL Server
connection, never an OpenAI call, never a database write, never a new
retrieval/embedding/semantic-enrichment build or row.

Computes the exact NEW (Step 4C1) column-selection and output-budget-aware
batch plan for every eligible object, using the real production selection/
estimation code (core.sqlservice.semantic_enrichment_selection /
core.sqlservice.semantic_enrichment_service) -- not a re-implementation --
and compares it against the OLD (Step 4A/4B) design's batch count and total
column volume.

Restrictions honored: no real provider calls, no live migration/database
writes, no live retrieval build, no SQL Server calls, no golden/accuracy
evaluation.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_OUTPUT_SAFETY_RATIO,
    SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
)
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_enrichment_selection import SELECTION_REASON_PRIORITY
from core.sqlservice.semantic_enrichment_service import (
    SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
    _plan_batches,  # read-only planning internals -- no I/O, no provider call
    batch_estimated_output_tokens,
    build_object_semantic_inputs,
)

SOURCE_ID = 10004
USER_ID = "28"

# Step 4A's own real estimate for the OLD (pre-Step-4C1) design, against
# THIS SAME 1,023-eligible-object schema (see run_semantic_enrichment_
# phase6_step4a_estimate.py) -- reused for direct comparison, not
# recomputed.
OLD_DESIGN = {"batch_count": 60, "max_objects_per_batch": 20, "max_columns_per_batch": 300}

# The four REAL Step 4C provider attempts against this exact schema's
# worst-case (17-object/299-column) OLD-design batch, used as the only
# empirical timing evidence available for Part 9's duration estimate.
STEP_4C_OBSERVED_ATTEMPTS = [
    {"max_output_tokens": 4000, "timeout_seconds": 10, "outcome": "APITimeoutError before any response"},
    {"max_output_tokens": 4000, "timeout_seconds": 90, "outcome": "response returned, truncated mid-JSON (~4000 tokens worth)"},
    {"max_output_tokens": 16000, "timeout_seconds": 120, "outcome": "APITimeoutError (generation not finished by 120s)"},
    {"max_output_tokens": 16000, "timeout_seconds": 240, "outcome": "response returned in <=240s, ~69,700 chars, still truncated mid-JSON"},
]


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        print(json.dumps({"status": "NO_READY_BUILD", "source_id": SOURCE_ID}, indent=2))
        return

    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    obj_inputs, _ineligible = build_object_semantic_inputs(documents)

    eligible_object_count = len(obj_inputs)
    total_columns = sum(len(o.columns) for o in obj_inputs)
    total_selected_columns = sum(len(o.selected_columns) for o in obj_inputs)
    object_level_only_count = sum(1 for o in obj_inputs if not o.selected_columns)

    reason_counts: dict[str, int] = {r: 0 for r in SELECTION_REASON_PRIORITY}
    for obj in obj_inputs:
        for col in obj.selected_columns:
            for reason in col.reasons:
                reason_counts[reason] += 1

    batches = _plan_batches(
        obj_inputs,
        SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
        SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS,
    )

    objects_per_batch = [len(b) for b in batches]
    selected_per_batch = [sum(len(o.selected_columns) for o in b) for b in batches]
    estimated_tokens_per_batch = [batch_estimated_output_tokens(b) for b in batches]
    max_estimated_tokens = max(estimated_tokens_per_batch) if estimated_tokens_per_batch else 0

    report = {
        "status": "OK",
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "contract_version": SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        "eligible_object_count": eligible_object_count,
        "total_structural_columns": total_columns,
        "total_selected_columns": total_selected_columns,
        "selected_columns_by_reason": reason_counts,
        "object_level_only_count": object_level_only_count,
        "new_deterministic_batch_count": len(batches),
        "objects_per_batch": {
            "min": min(objects_per_batch) if objects_per_batch else 0,
            "median": statistics.median(objects_per_batch) if objects_per_batch else 0,
            "p90": _percentile(objects_per_batch, 0.90),
            "max": max(objects_per_batch) if objects_per_batch else 0,
        },
        "selected_columns_per_batch": {
            "min": min(selected_per_batch) if selected_per_batch else 0,
            "median": statistics.median(selected_per_batch) if selected_per_batch else 0,
            "p90": _percentile(selected_per_batch, 0.90),
            "max": max(selected_per_batch) if selected_per_batch else 0,
        },
        "estimated_output_tokens_per_batch": {
            "min": min(estimated_tokens_per_batch) if estimated_tokens_per_batch else 0,
            "median": statistics.median(estimated_tokens_per_batch) if estimated_tokens_per_batch else 0,
            "p90": _percentile(estimated_tokens_per_batch, 0.90),
            "max": max_estimated_tokens,
        },
        "configured_ceilings": {
            "max_estimated_output_tokens": SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS,
            "provider_output_token_ceiling": SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
            "output_safety_ratio": SQL_SEMANTIC_ENRICHMENT_OUTPUT_SAFETY_RATIO,
        },
        "largest_estimated_output_utilization_pct": round(
            100.0 * max_estimated_tokens / SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS, 1,
        ) if SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS else None,
        "largest_estimated_output_pct_of_hard_provider_ceiling": round(
            100.0 * max_estimated_tokens / SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING, 1,
        ) if SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING else None,
        "predicted_provider_call_count": len(batches),
        "old_design_comparison": {
            "old_batch_count": OLD_DESIGN["batch_count"],
            "new_batch_count": len(batches),
            "old_requested_column_records_estimate": total_columns,
            "new_requested_column_records": total_selected_columns,
            "column_volume_reduction_pct": round(
                100.0 * (1 - (total_selected_columns / total_columns)), 1,
            ) if total_columns else None,
        },
        "step_4c_observed_attempts_evidence": STEP_4C_OBSERVED_ATTEMPTS,
        "golden_accuracy_evaluated": False,
        "real_enrichment_run": False,
    }
    print(json.dumps(report, indent=2, default=str))

    out_path = Path(r"C:\Users\harpr\OneDrive\Desktop\ToolSmithAI_db_backups\step4C1_estimate_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote report to {out_path}")


if __name__ == "__main__":
    main()
