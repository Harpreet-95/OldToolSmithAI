"""
Read-only planning report — SQL Architecture Correction, Phase 6, Step
4C2, Part 11.

Reads ONLY ToolSmithAI's own local SQLite metadata store (core.sqlservice.
retrieval_store) for source_id=10004 (the same CCPP data source every prior
Phase 6 read-only script in this directory uses), against the real,
already-committed build 7 (1,406 documents, column_descriptors on all of
them; 1,023 Tier 1/2 eligible objects). Never a live SQL Server connection,
never an OpenAI call, never a database write, never a new retrieval/
embedding/semantic-enrichment build or row.

Computes the exact NEW (Step 4C2) OBJECT_CORE informative-input-column
selection and output-budget-aware batch plan for every eligible object,
using the real production code (core.sqlservice.semantic_enrichment_
object_core_selection / the OBJECT_CORE section of core.sqlservice.
semantic_enrichment_service) -- not a re-implementation -- and compares it
against:
  - the ORIGINAL (Step 4A/4B) design: 60 oversized batches (see
    run_semantic_enrichment_phase6_step4a_estimate.py);
  - Step 4C1's compact-but-still-combined design: 186 batches (see
    run_semantic_enrichment_phase6_step4c1_estimate.py, re-run live below
    for an up-to-date figure against the same build);
  - Step 4C2's new OBJECT_CORE-only batch count.

Duration estimates use an EXPLICIT, DOCUMENTED per-call latency assumption
(never measured in this step -- no real provider call is ever made here):
each OBJECT_CORE batch still asks for a real business judgement (label,
role, concepts, aliases, description) across a median of ~30 objects per
batch at this build's real batch sizes -- not a trivial single-object call
-- so a structured-JSON chat-completion latency of 15-25 seconds per call
is assumed (still far below the 90-240+ second latencies Step 4C1's own
script documents as REAL OBSERVED evidence for the old, much larger
combined-contract batches, which additionally requested full per-column
records). This is a planning ASSUMPTION, clearly labeled as such, not an
empirical measurement.

Restrictions honored: no real provider calls, no live migration/database
writes, no live retrieval build, no SQL Server calls, no golden/accuracy
evaluation.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN,
    SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_INPUT_COLUMNS_PER_OBJECT,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MAX_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MIN_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
)
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_enrichment_service import (
    SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
    _OBJECT_CORE_INPUT_LIMITS,  # read-only planning internals -- no I/O, no provider call
    _OBJECT_CORE_OUTPUT_LIMITS,
    _plan_batches,
    _plan_object_core_batches,
    batch_estimated_output_tokens,
    build_object_semantic_inputs,
    object_core_batch_estimated_output_tokens,
)
from core.sqlservice.semantic_enrichment_object_core_selection import select_informative_input_columns

SOURCE_ID = 10004
USER_ID = "28"

# Step 4A's own real estimate for the ORIGINAL (pre-Step-4C1) design against
# THIS SAME 1,023-eligible-object schema -- reused for direct comparison,
# not recomputed (see run_semantic_enrichment_phase6_step4a_estimate.py).
ORIGINAL_DESIGN_BATCH_COUNT = 60

# Documented ASSUMPTION only -- see this module's own docstring. Never
# measured in this step (no real provider call is ever made here).
ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_LOW = 15.0
ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_HIGH = 25.0


def _percentile(values: list, pct: float) -> float:
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

    # ── Step 4C1 (COLUMN_DEFERRED machinery), re-run live for an up-to-date
    #    comparison figure against this exact build ──────────────────────
    step4c1_batches = _plan_batches(
        obj_inputs,
        SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
        SQL_SEMANTIC_ENRICHMENT_MAX_SELECTED_COLUMNS_PER_BATCH,
        SQL_SEMANTIC_ENRICHMENT_MAX_ESTIMATED_OUTPUT_TOKENS,
    )
    total_selected_columns_step4c1 = sum(len(o.selected_columns) for o in obj_inputs)

    # ── Step 4C2 OBJECT_CORE: informative-input-column selection + batching
    informative_columns_by_object = {
        obj.object_fqn: select_informative_input_columns(obj.columns, limits=_OBJECT_CORE_INPUT_LIMITS)
        for obj in obj_inputs
    }
    total_informative_columns = sum(len(v) for v in informative_columns_by_object.values())
    objects_with_all_columns_shown = sum(
        1 for obj in obj_inputs if len(informative_columns_by_object[obj.object_fqn]) == len(obj.columns)
    )

    object_core_batches = _plan_object_core_batches(
        obj_inputs,
        max_objects_per_batch=SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_OBJECTS_PER_BATCH,
        max_batch_characters=SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_BATCH_CHARACTERS,
        max_estimated_output_tokens=SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS,
        conservative_chars_per_token=SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN,
        input_limits=_OBJECT_CORE_INPUT_LIMITS, output_limits=_OBJECT_CORE_OUTPUT_LIMITS,
    )

    # Every eligible object covered exactly once (Part 3 requirement) --
    # asserted here as a read-only sanity check, not merely claimed.
    all_object_core_fqns = [item.obj_input.object_fqn for batch in object_core_batches for item in batch]
    assert sorted(f.lower() for f in all_object_core_fqns) == sorted(o.object_fqn.lower() for o in obj_inputs)
    assert len(all_object_core_fqns) == eligible_object_count

    objects_per_batch = [len(b) for b in object_core_batches]
    estimated_tokens_per_batch = [object_core_batch_estimated_output_tokens(b) for b in object_core_batches]
    input_chars_per_batch = [
        sum(len(item.obj_input.object_fqn) for item in batch) + sum(len(item.informative_columns) * 20 for item in batch)
        for batch in object_core_batches
    ]
    max_estimated_tokens = max(estimated_tokens_per_batch) if estimated_tokens_per_batch else 0

    sequential_seconds_low = len(object_core_batches) * ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_LOW
    sequential_seconds_high = len(object_core_batches) * ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_HIGH
    concurrent_rounds = math.ceil(len(object_core_batches) / SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY)
    concurrent_seconds_low = concurrent_rounds * ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_LOW
    concurrent_seconds_high = concurrent_rounds * ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_HIGH

    report = {
        "status": "OK",
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "contract_version": SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        "eligible_object_count": eligible_object_count,
        "object_core": {
            "max_input_columns_per_object": SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_INPUT_COLUMNS_PER_OBJECT,
            "total_informative_columns_selected": total_informative_columns,
            "objects_with_all_structural_columns_shown": objects_with_all_columns_shown,
            "batch_count": len(object_core_batches),
            "objects_per_batch": {
                "min": min(objects_per_batch) if objects_per_batch else 0,
                "median": statistics.median(objects_per_batch) if objects_per_batch else 0,
                "p90": _percentile(objects_per_batch, 0.90),
                "max": max(objects_per_batch) if objects_per_batch else 0,
            },
            "input_characters_per_batch_estimate": {
                "min": min(input_chars_per_batch) if input_chars_per_batch else 0,
                "median": statistics.median(input_chars_per_batch) if input_chars_per_batch else 0,
                "max": max(input_chars_per_batch) if input_chars_per_batch else 0,
            },
            "estimated_output_tokens_per_batch": {
                "min": min(estimated_tokens_per_batch) if estimated_tokens_per_batch else 0,
                "median": statistics.median(estimated_tokens_per_batch) if estimated_tokens_per_batch else 0,
                "p90": _percentile(estimated_tokens_per_batch, 0.90),
                "max": max_estimated_tokens,
            },
            "configured_ceilings": {
                "max_estimated_output_tokens": SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS,
                "provider_output_token_ceiling": SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
                "target_min_output_tokens": SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MIN_OUTPUT_TOKENS,
                "target_max_output_tokens": SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_TARGET_MAX_OUTPUT_TOKENS,
            },
            "largest_estimated_output_utilization_pct_of_configured_ceiling": round(
                100.0 * max_estimated_tokens / SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS, 1,
            ) if SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS else None,
            "largest_estimated_output_pct_of_hard_provider_ceiling": round(
                100.0 * max_estimated_tokens / SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING, 1,
            ) if SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING else None,
        },
        "duration_estimate": {
            "assumption": (
                f"{ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_LOW:.0f}-"
                f"{ASSUMED_SECONDS_PER_OBJECT_CORE_CALL_HIGH:.0f} seconds per compact OBJECT_CORE call -- "
                "a documented PLANNING ASSUMPTION, never measured (no real provider call is made in Step 4C2)"
            ),
            "sequential_seconds": {"low": sequential_seconds_low, "high": sequential_seconds_high},
            "sequential_minutes": {
                "low": round(sequential_seconds_low / 60, 1), "high": round(sequential_seconds_high / 60, 1),
            },
            "concurrent_default_concurrency": SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY,
            "concurrent_rounds": concurrent_rounds,
            "concurrent_seconds": {"low": concurrent_seconds_low, "high": concurrent_seconds_high},
            "concurrent_minutes": {
                "low": round(concurrent_seconds_low / 60, 1), "high": round(concurrent_seconds_high / 60, 1),
            },
        },
        "column_deferred_reference": {
            "note": (
                "COLUMN_DEFERRED reuses the pre-existing Step 4C1 machinery UNCHANGED, now phase-tagged; "
                "no COLUMN_DEFERRED provider call happens in Step 4C2 -- figures below are re-run live "
                "for comparison only, using the SAME real build 7 data."
            ),
            "batch_count": len(step4c1_batches),
            "total_selected_columns": total_selected_columns_step4c1,
        },
        "call_count_comparison": {
            "original_design_step4a_4b": ORIGINAL_DESIGN_BATCH_COUNT,
            "step4c1_combined_contract": len(step4c1_batches),
            "step4c2_object_core_only": len(object_core_batches),
        },
        "golden_accuracy_evaluated": False,
        "real_enrichment_run": False,
        "real_provider_calls_made": False,
        "live_database_writes_made": False,
    }
    print(json.dumps(report, indent=2, default=str))

    out_path = Path(r"C:\Users\harpr\OneDrive\Desktop\ToolSmithAI_db_backups\step4C2_estimate_report.json")
    try:
        out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote report to {out_path}")
    except OSError as exc:
        print(f"\n(could not write report file: {exc})")


if __name__ == "__main__":
    main()
