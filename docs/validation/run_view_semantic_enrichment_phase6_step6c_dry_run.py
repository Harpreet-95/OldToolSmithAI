"""
Read-only planning report -- SQL Architecture Correction, Phase 6, Step
6C1, Part 6 (supersedes the Step 6C, Part 10 version of this same script).

Reads ONLY ToolSmithAI's own local SQLite metadata store (data/toolsmith.db)
for source_id=10004 (the same CCPP data source every prior Phase 6 read-only
script in this directory uses), against the latest already-committed READY
retrieval build. Never a live SQL Server connection, never an OpenAI call,
never a database write, never a new retrieval/embedding/semantic-enrichment
build or row. Hashes the metadata database file before and after and prints
proof the hash is unchanged (see `main`'s own final section).

Step 6C1 changes from the original Step 6C version of this script:
  - "already-current" is now counted against `sql_view_semantic_enrichments`
    (a dedicated table -- see migrations/017_create_view_semantic_
    enrichments.sql and this milestone's Part 1/2 persistence audit), never
    `sql_semantic_enrichments` (whose `phase` column can never legally hold
    'VIEW_ENRICHED' -- that was the entire finding of this audit).
  - The batch plan now uses the REAL, current core.sqlservice.
    semantic_enrichment_service._plan_view_batches, which enforces BOTH an
    input character bound AND a conservative output-TOKEN safety bound
    (Part 3) -- individually oversized views are reported separately, never
    silently folded into "planned batches".
  - Cost is reported correctly in BOTH dollars and cents, with the pricing
    inputs as explicit, labeled, overridable module-level constants (never
    fetched from the network, never hardcoded into any production runtime
    path -- these constants exist ONLY in this validation script).

Restrictions honored: no real provider calls, no live migration/database
writes, no live retrieval build, no SQL Server calls, no golden/accuracy
evaluation, no population of sql_semantic_enrichments or
sql_view_semantic_enrichments.

Run:
    python docs/validation/run_view_semantic_enrichment_phase6_step6c_dry_run.py
"""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import (
    OPENAI_MODEL,
    SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN,
    SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
    SQL_SEMANTIC_ENRICHMENT_VIEW_DEFAULT_CONCURRENCY,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO,
)
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_enrichment_service import (
    SKIP_REASON_INPUT_TOO_LARGE,
    SKIP_REASON_OUTPUT_BUDGET_EXCEEDED,
    ColumnSemanticInput,
    compute_view_semantic_input_hash,
    view_batch_estimated_output_tokens,
    _plan_view_batches,  # read-only planning internal -- no I/O, no provider call
)
from core.sqlservice.view_semantic_enrichment_input import RawDependencyEdge, build_view_semantic_input
from core.sqlservice.view_semantic_enrichment_selection import select_view_enrichment_candidates
from data.db import DB_PATH, get_connection

SOURCE_ID = 10004
USER_ID = "28"

# Documented ASSUMPTION only, never measured (no real provider call is ever
# made here) -- a deep VIEW_ENRICHED prompt asks for materially more
# structured judgement per object (domain/purpose/entities/status/aliases/
# operations/source-role plus a bounded per-column pass over the view's own
# SQL definition text) than the compact OBJECT_CORE contract, so a wider
# latency band is assumed, anchored to the SAME real observed OBJECT_CORE
# range documented in run_semantic_enrichment_phase6_step4c2_estimate.py.
ASSUMED_SECONDS_PER_VIEW_BATCH_LOW = 20.0
ASSUMED_SECONDS_PER_VIEW_BATCH_HIGH = 45.0

# ---------------------------------------------------------------------------
# Pricing inputs -- EXPLICIT, LABELED, OVERRIDABLE. Used ONLY by this
# read-only validation/estimation script; never referenced by any
# production runtime code path (core.sqlservice.semantic_enrichment_*
# never imports or hardcodes a price). Never fetched from the network --
# Step 6C1 explicitly forbids any network call in this script.
#
# Source: OpenAI's published list pricing for gpt-4o-mini, as recorded by
# this script's author on 2026-09-16 (the date this script was last
# updated) -- NOT queried live, and may drift from OpenAI's current
# pricing at any later date this script is run. Override both constants
# below (or the PRICING_SOURCE/PRICING_AS_OF labels) if actual pricing
# has since changed, or if a different model is configured via
# core.config.OPENAI_MODEL.
# ---------------------------------------------------------------------------
PRICING_SOURCE = "OpenAI published list pricing (gpt-4o-mini), recorded manually -- not fetched live"
PRICING_AS_OF_DATE = "2026-09-16"
ASSUMED_INPUT_COST_USD_PER_1K_TOKENS = 0.000150
ASSUMED_OUTPUT_COST_USD_PER_1K_TOKENS = 0.000600


def _format_usd_and_cents(amount_usd: float) -> str:
    """Renders a small USD amount in BOTH dollars and cents, unambiguously
    -- Step 6C1, Part 4's own correction target: '$0.038' alone reads to
    many people as "38 thousandths of a dollar" without the cents
    conversion spelled out; this function always spells it out."""
    cents = amount_usd * 100
    return f"${amount_usd:.4f} ({cents:.2f} cents)"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _column_semantic_inputs_from_metadata(metadata: dict) -> tuple[ColumnSemanticInput, ...]:
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
    # Legacy fallback -- mirrors semantic_enrichment_service.py's own
    # _column_inputs_from_legacy_fields for a document that predates
    # column_descriptors.
    primary_keys = set(metadata.get("primary_key_columns") or ())
    return tuple(
        sorted(
            (ColumnSemanticInput(name, "OTHER", name in primary_keys, False) for name in (metadata.get("column_names") or ())),
            key=lambda c: c.column_name.lower(),
        )
    )


def _load_view_definition(conn, source_id: int, object_fqn: str) -> str | None:
    row = conn.execute(
        "SELECT definition_sql FROM view_semantic_evidence "
        "WHERE source_id = ? AND table_fqn = ? ORDER BY discovered_at DESC LIMIT 1",
        (source_id, object_fqn),
    ).fetchone()
    return row["definition_sql"] if row is not None else None


def _load_dependency_edges(conn, source_id: int, object_fqn: str) -> tuple[RawDependencyEdge, ...]:
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


def _svse_table_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sql_view_semantic_enrichments'"
    ).fetchone() is not None


def main() -> None:
    if not Path(DB_PATH).exists():
        print(json.dumps({"status": "NO_METADATA_DB", "db_path": str(DB_PATH)}, indent=2))
        return

    hash_before = _sha256_file(Path(DB_PATH))

    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        print(json.dumps({"status": "NO_READY_BUILD", "source_id": SOURCE_ID}, indent=2))
        return

    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    selection = select_view_enrichment_candidates(documents)

    conn = get_connection()
    try:
        if _svse_table_exists(conn):
            already_active_view_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM sql_view_semantic_enrichments WHERE source_id = ? AND status = 'ACTIVE'",
                (SOURCE_ID,),
            ).fetchone()["n"]
        else:
            already_active_view_rows = 0  # table not yet created on this database (never applied here)

        doc_by_fqn = {d.object_fqn: d for d in documents}
        definition_truncation_count = 0
        views_lacking_dependency_evidence = 0
        views_lacking_definition_evidence = 0
        hashes_run1: dict[str, str] = {}
        hashes_run2: dict[str, str] = {}
        view_inputs = []

        for candidate in selection.candidates:
            doc = doc_by_fqn.get(candidate.object_fqn)
            metadata = dict(doc.metadata) if doc is not None else {}
            columns = _column_semantic_inputs_from_metadata(metadata)
            raw_definition = _load_view_definition(conn, SOURCE_ID, candidate.object_fqn)
            if not raw_definition:
                views_lacking_definition_evidence += 1
            raw_edges = _load_dependency_edges(conn, SOURCE_ID, candidate.object_fqn)
            if not raw_edges:
                views_lacking_dependency_evidence += 1

            build_result = build_view_semantic_input(
                candidate,
                columns=columns,
                primary_key_columns=metadata.get("primary_key_columns") or (),
                dictionary_business_name=metadata.get("business_name"),
                dictionary_description=metadata.get("description"),
                dictionary_synonyms=tuple(metadata.get("synonyms") or ()),
                raw_definition_sql=raw_definition,
                dependency_edges=raw_edges,
                confirmed_relationship_summary=(),
            )
            if build_result.definition_truncated:
                definition_truncation_count += 1
            view_inputs.append(build_result.view_input)

        # Run 1
        for vi in view_inputs:
            hashes_run1[vi.object_fqn] = compute_view_semantic_input_hash(vi)
        # Run 2 -- prove determinism (Part 6: "deterministic hashes across two runs")
        for vi in view_inputs:
            hashes_run2[vi.object_fqn] = compute_view_semantic_input_hash(vi)
        hashes_match = hashes_run1 == hashes_run2

        # Real production batch planner -- enforces BOTH the character
        # bound and the conservative output-token safety bound (Part 3).
        batches, oversized_skips = _plan_view_batches(
            view_inputs,
            max_objects_per_batch=SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH,
            max_batch_characters=SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS,
            max_estimated_output_tokens=SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS,
        )
        batch_count = len(batches)

        input_chars_per_batch = []
        output_tokens_per_batch = []
        total_input_chars = 0
        total_output_tokens = 0
        for batch in batches:
            from core.sqlservice.semantic_enrichment_service import _render_view_block  # read-only internal
            batch_chars = sum(len(_render_view_block(item)) for item in batch)
            batch_tokens = view_batch_estimated_output_tokens(batch)
            input_chars_per_batch.append(batch_chars)
            output_tokens_per_batch.append(batch_tokens)
            total_input_chars += batch_chars
            total_output_tokens += batch_tokens

        estimated_input_tokens = int(total_input_chars / SQL_SEMANTIC_ENRICHMENT_CONSERVATIVE_CHARS_PER_TOKEN)

        oversized_by_reason: dict[str, int] = {}
        for s in oversized_skips:
            oversized_by_reason[s.reason] = oversized_by_reason.get(s.reason, 0) + 1

        estimated_cost_usd = (
            (estimated_input_tokens / 1000) * ASSUMED_INPUT_COST_USD_PER_1K_TOKENS
            + (total_output_tokens / 1000) * ASSUMED_OUTPUT_COST_USD_PER_1K_TOKENS
        )

        report = {
            "status": "OK",
            "source_id": SOURCE_ID,
            "build_id": build.id,
            "configured_model": OPENAI_MODEL,
            "total_view_documents": selection.total_documents,
            "eligible_tier_1_2_views": selection.eligible_view_count,
            "excluded_views": selection.excluded_count,
            "excluded_reasons": sorted({e.reason for e in selection.excluded}),
            "selected_views": selection.selected_count,
            "truncated_by_max_total_views": selection.truncated_count,
            "already_current_view_enriched_rows_in_store": already_active_view_rows,
            "missing_or_stale_views": max(0, len(view_inputs) - already_active_view_rows),
            "definition_truncation_count": definition_truncation_count,
            "views_lacking_definition_evidence": views_lacking_definition_evidence,
            "views_lacking_dependency_evidence": views_lacking_dependency_evidence,
            "individually_oversized_views": {
                "count": len(oversized_skips),
                "by_reason": oversized_by_reason,
                "object_fqns": sorted(s.object_fqn for s in oversized_skips),
            },
            "planned_batch_count": batch_count,
            "batch_size_config": {
                "max_objects_per_batch": SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH,
                "max_batch_characters": SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS,
                "provider_output_token_ceiling": SQL_SEMANTIC_ENRICHMENT_PROVIDER_OUTPUT_TOKEN_CEILING,
                "output_safety_ratio": SQL_SEMANTIC_ENRICHMENT_VIEW_OUTPUT_SAFETY_RATIO,
                "max_estimated_output_tokens": SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS,
                "default_concurrency": SQL_SEMANTIC_ENRICHMENT_VIEW_DEFAULT_CONCURRENCY,
            },
            "batch_input_characters": {
                "max": max(input_chars_per_batch) if input_chars_per_batch else 0,
                "median": statistics.median(input_chars_per_batch) if input_chars_per_batch else 0,
                "total": total_input_chars,
            },
            "batch_output_tokens_estimate": {
                "max": max(output_tokens_per_batch) if output_tokens_per_batch else 0,
                "median": statistics.median(output_tokens_per_batch) if output_tokens_per_batch else 0,
                "total": total_output_tokens,
            },
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": total_output_tokens,
            "estimated_openai_call_count": batch_count,
            "pricing_inputs": {
                "source": PRICING_SOURCE,
                "as_of_date": PRICING_AS_OF_DATE,
                "input_usd_per_1k_tokens": ASSUMED_INPUT_COST_USD_PER_1K_TOKENS,
                "output_usd_per_1k_tokens": ASSUMED_OUTPUT_COST_USD_PER_1K_TOKENS,
            },
            "estimated_cost_usd": round(estimated_cost_usd, 4),
            "estimated_cost_display": _format_usd_and_cents(estimated_cost_usd),
            "cost_is_an_estimate_not_a_guarantee": True,
            "estimated_duration_seconds_low": round(batch_count * ASSUMED_SECONDS_PER_VIEW_BATCH_LOW, 1),
            "estimated_duration_seconds_high": round(batch_count * ASSUMED_SECONDS_PER_VIEW_BATCH_HIGH, 1),
            "deterministic_input_hashes_match_across_two_runs": hashes_match,
            "selection_warnings": list(selection.warnings),
        }
        print(json.dumps(report, indent=2, default=str))
    finally:
        conn.close()

    hash_after = _sha256_file(Path(DB_PATH))
    print(json.dumps({
        "db_hash_before": hash_before,
        "db_hash_after": hash_after,
        "db_unchanged": hash_before == hash_after,
    }, indent=2))


if __name__ == "__main__":
    main()
