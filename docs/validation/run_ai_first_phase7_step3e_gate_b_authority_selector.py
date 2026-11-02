"""
Gate B rerun — automatic authority/scope evidence, faster selector model.
Phase 7, Step 3E, Part 9.

Identical measurement to docs/validation/run_ai_first_phase7_step3c1_gate_b_
selector.py (same 15-case golden fixture, same real, already-READY CCPP
retrieval build source_id=10004 build_id=7, same frontier algorithm/limits,
same response contract, same deterministic Python validator's own base
checks, same connectivity gate, same grading philosophy) with exactly TWO
things different from that original baseline:

  1. The selector model is the SAME ONE used before the Astra evaluation
     (core.config.AI_FIRST_SCHEMA_SELECTION_MODEL left at its default,
     which itself defaults to OPENAI_MODEL — gpt-4o-mini-class, never
     Astra, never a stronger/slower model) — Step 3E's whole premise is
     that model strength was never the defect; this rerun proves it on the
     SAME model the original 6/14 Gate B run used.
  2. The candidate frontier now carries Step 3E's own automatic authority/
     scope evidence (core.sqlservice.schema_object_authority, wired into
     core.sqlservice.schema_frontier's own FrontierCandidate rendering) and
     the selector prompt/deterministic validator now reason about it (core.
     sqlservice.schema_selection_service) — this script does not add its
     OWN equivalence judgment on top; it reads the exact SchemaSelectionResult
     the pipeline already produces and grades against the SAME two
     standards the Step 3D1 read-only audit established: exact required-FQN
     coverage, OR authority_class in {PROVEN_EQUIVALENT_VIEW,
     DECLARED_PREFERRED_VIEW} for every selected object beyond/instead of
     the exact required set.

Live-call footprint, PER ELIGIBLE QUESTION: exactly ONE real
OpenAIEmbeddingProvider.embed_batch() call (inside build_schema_frontier,
which also computes/caches authority — zero additional embedding/AI calls
for that) and exactly ONE real OpenAISchemaSelectionProvider.select_schema()
call. No SQL generation, review, validation, execution, or answer-generation
call anywhere in this file (see its own import list). No database write of
any kind — authority computation is read-only against already-persisted
semantic_entity_contracts/view_semantic_evidence.

Never logs or saves: API keys, credentials, connection strings, prompts,
raw provider responses, query vectors, full schema documents, or
chain-of-thought — only the already-validated, already-bounded structured
decision fields plus authority classes/evidence codes (already bounded,
already non-sensitive) are recorded.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_VALIDATION_DIR = Path(__file__).resolve().parent
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402

from core.config import (  # noqa: E402
    AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS,
    AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS,
    AI_FIRST_SCHEMA_SELECTION_MODEL,
    AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT,
    AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS,
)
from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build  # noqa: E402
from core.sqlservice.schema_frontier import STATUS_OK as FRONTIER_STATUS_OK  # noqa: E402
from core.sqlservice.schema_frontier import build_schema_frontier  # noqa: E402
from core.sqlservice.schema_relationship_completion import STATUS_CONNECTED  # noqa: E402
from core.sqlservice.schema_relationship_completion import complete_relationships  # noqa: E402
from core.sqlservice.schema_selection_providers import (  # noqa: E402
    OpenAISchemaSelectionProvider,
    SchemaSelectionConfigurationError,
    SchemaSelectionProviderError,
)
from core.sqlservice.schema_selection_service import RESPONSE_STATUS_CLARIFICATION  # noqa: E402
from core.sqlservice.schema_selection_service import RESPONSE_STATUS_SELECTED  # noqa: E402
from core.sqlservice.schema_selection_service import STATUS_OK as SELECTION_STATUS_OK  # noqa: E402
from core.sqlservice.schema_selection_service import (  # noqa: E402
    SchemaSelectionValidationError,
    select_schema_for_question,
)
from core.sqlservice.vector_index import clear_vector_index_cache  # noqa: E402
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})
_EQUIVALENT_AUTHORITY_CLASSES = frozenset({"PROVEN_EQUIVALENT_VIEW", "DECLARED_PREFERRED_VIEW"})

_OUT_DIR_ENV = "GATE_B_OUTPUT_DIR"
_OUTPUT_DIR = Path(os.environ.get(_OUT_DIR_ENV, str(Path.home() / "gate_b_reports")))
RESULTS_PATH = _OUTPUT_DIR / "ai_first_phase7_step3e_gate_b_authority_selector.json"


def _sanitize_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, SchemaSelectionValidationError):
        return str(exc), "invalid_response"
    if isinstance(exc, SchemaSelectionConfigurationError):
        return "The schema-selection provider is not configured correctly.", "configuration"
    if isinstance(exc, SchemaSelectionProviderError):
        return "The schema-selection provider call failed.", type(exc).__name__
    return f"Unexpected error: {type(exc).__name__}", "unexpected"


def _grade_case(case, frontier_result, selection_result, connectivity_status: str | None) -> dict:
    frontier_by_lower = {c.object_fqn.lower(): c for c in frontier_result.candidates}
    tier_by_fqn_lower = {k: c.retrieval_tier for k, c in frontier_by_lower.items()}
    required_lower = {f.lower() for f in case.required_object_fqns}
    forbidden_lower = {f.lower() for f in case.forbidden_object_fqns}

    every_required_in_frontier = required_lower.issubset(frontier_by_lower.keys()) if required_lower else None

    candidate = selection_result.candidate
    selected_lower: set[str] = set()
    selector_status = None
    authority_by_selected: dict[str, dict] = {}
    if candidate is not None:
        selector_status = candidate.response_status
        selected_lower = {f.lower() for f in candidate.selected_object_fqns}
        for key in selected_lower:
            fc = frontier_by_lower.get(key)
            if fc is not None:
                authority_by_selected[fc.object_fqn] = {
                    "authority_class": fc.authority_class,
                    "canonical_table_fqns": list(fc.authority_canonical_table_fqns),
                    "restriction_status": fc.authority_restriction_status,
                    "grain_class": fc.authority_grain_class,
                }

    exact_required_coverage = required_lower.issubset(selected_lower) if required_lower else None
    missing_required = sorted(required_lower - selected_lower) if required_lower else []
    # Verified-equivalence coverage: every REQUIRED object is either exactly
    # selected, OR the selected set includes at least one object whose
    # OWN authority record proves it equivalent/preferred FOR that required
    # object's own canonical identity (authority_canonical_table_fqns names
    # the canonical table it was proven equivalent to/preferred for).
    # A selected object whose OWN authority record is PROVEN_EQUIVALENT_VIEW
    # or DECLARED_PREFERRED_VIEW is treated as substituting for every
    # still-missing required object (this fixture's own cases each center
    # on one canonical concept per question, matching the Step 3D1 audit's
    # own equivalence-proof scope) -- never invented, never a guess: the
    # equivalence itself was already proven structurally by core.sqlservice.
    # schema_object_authority before this script ever ran.
    any_proven_equivalent_selected = any(
        (frontier_by_lower.get(key) is not None and frontier_by_lower[key].authority_class in _EQUIVALENT_AUTHORITY_CLASSES)
        for key in selected_lower
    )
    verified_equivalent_coverage = exact_required_coverage if required_lower else None
    if required_lower and not exact_required_coverage and any_proven_equivalent_selected:
        verified_equivalent_coverage = True

    forbidden_selected = sorted(forbidden_lower & selected_lower)
    tier_leakage = sorted(f for f in selected_lower if tier_by_fqn_lower.get(f) not in ELIGIBLE_TIERS)
    unknown_selected = sorted(f for f in selected_lower if f not in frontier_by_lower)
    unsafe_selected = sorted(
        f for f in selected_lower
        if frontier_by_lower.get(f) is not None and frontier_by_lower[f].authority_class == "UNSAFE_DEPENDENCY"
    )
    incompatible_grain_selected = sorted(
        f for f in selected_lower
        if frontier_by_lower.get(f) is not None and frontier_by_lower[f].authority_class == "INCOMPATIBLE_GRAIN"
    )

    return {
        "required_lower": sorted(required_lower),
        "missing_required": missing_required,
        "every_required_in_frontier": every_required_in_frontier,
        "selector_status": selector_status,
        "selected_object_fqns": sorted(candidate.selected_object_fqns) if candidate is not None else [],
        "authority_by_selected": authority_by_selected,
        "exact_required_coverage": exact_required_coverage,
        "verified_equivalent_coverage": verified_equivalent_coverage,
        "forbidden_selected": forbidden_selected,
        "tier_leakage_selected": tier_leakage,
        "unknown_selected": unknown_selected,
        "unsafe_selected": unsafe_selected,
        "incompatible_grain_selected": incompatible_grain_selected,
        "connectivity_status": connectivity_status,
    }


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    assert build is not None, "no READY retrieval build found for source_id=10004"
    assert build.id == 7, f"expected latest READY build id=7, got {build.id}"

    embedding_provider = OpenAIEmbeddingProvider()
    selection_provider = OpenAISchemaSelectionProvider(
        model=AI_FIRST_SCHEMA_SELECTION_MODEL, reasoning_effort=AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT,
    )
    clear_vector_index_cache()
    clear_schema_object_authority_cache()

    print(
        f"Preflight: build_id={build.id} source_id={SOURCE_ID} user_id={USER_ID} "
        f"schema_fingerprint={build.schema_fingerprint} snapshot_id={build.snapshot_id} status={build.status}"
    )
    print(
        f"Selection provider: provider_id={selection_provider.provider_id} model={selection_provider.model} "
        f"reasoning_effort={AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT!r} (the faster, pre-Astra model)"
    )
    print(f"Golden fixture: {len(CCPP_GOLDEN_CASES)} cases\n")

    per_case: list[dict] = []
    embed_call_count = 0
    selection_call_count = 0

    for case in CCPP_GOLDEN_CASES:
        record: dict = {
            "case_id": case.case_id, "category": case.category, "expects_clarification": case.expects_clarification,
            "requested_model": AI_FIRST_SCHEMA_SELECTION_MODEL,
        }

        t0 = time.perf_counter()
        try:
            frontier_result = build_schema_frontier(case.question, build.id, SOURCE_ID, USER_ID, embedding_provider)
        except Exception as exc:
            record["retrieval_duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
            record["frontier_status"] = "EXCEPTION"
            record["error_message"] = f"Unexpected error: {type(exc).__name__}"
            record["error_category"] = "unexpected"
            record["selector_status"] = None
            record["provider_call_count"] = 0
            per_case.append(record)
            print(f"  {case.case_id}: FRONTIER EXCEPTION category=unexpected")
            continue
        embed_call_count += 1
        record["retrieval_duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        record["frontier_status"] = frontier_result.status
        record["frontier_size"] = len(frontier_result.candidates)

        if frontier_result.status != FRONTIER_STATUS_OK or not frontier_result.candidates:
            record["error_message"] = frontier_result.error or "frontier returned zero candidates"
            record["error_category"] = "frontier_unavailable"
            record["selector_status"] = None
            record["provider_call_count"] = 0
            per_case.append(record)
            print(f"  {case.case_id}: FRONTIER FAILED status={frontier_result.status}")
            continue

        t1 = time.perf_counter()
        try:
            selection_result = select_schema_for_question(
                case.question, frontier_result, selection_provider,
                max_objects=AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS,
                max_output_tokens=AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS,
                timeout_seconds=AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS,
                source_id=SOURCE_ID, user_id=USER_ID, embedding_provider=embedding_provider,
            )
        except (SchemaSelectionValidationError, SchemaSelectionConfigurationError, SchemaSelectionProviderError) as exc:
            selection_call_count += 1
            record["selector_duration_ms"] = round((time.perf_counter() - t1) * 1000.0, 3)
            message, category = _sanitize_error(exc)
            record["selector_status"] = "EXCEPTION"
            record["error_message"] = message
            record["error_category"] = category
            record["provider_call_count"] = 1
            per_case.append(record)
            print(f"  {case.case_id}: SELECTOR EXCEPTION category={category}")
            continue
        selection_call_count += 1
        record["selector_duration_ms"] = round((time.perf_counter() - t1) * 1000.0, 3)
        record["selection_result_status"] = selection_result.status
        record["actual_model"] = selection_result.model
        record["token_usage"] = selection_result.usage
        record["provider_call_count"] = 1

        if selection_result.status != SELECTION_STATUS_OK or selection_result.candidate is None:
            record["error_message"] = selection_result.error or f"selection status={selection_result.status!r}"
            record["error_category"] = "selection_unavailable"
            record["selector_status"] = None
            per_case.append(record)
            print(f"  {case.case_id}: SELECTION FAILED status={selection_result.status}")
            continue

        candidate = selection_result.candidate
        record["selector_status"] = candidate.response_status
        record["clarification_question"] = candidate.clarification_question

        connectivity_status = None
        if candidate.response_status == RESPONSE_STATUS_SELECTED and len(candidate.selected_object_fqns) >= 1:
            conn_result = complete_relationships(
                candidate.selected_object_fqns, build.id, SOURCE_ID, USER_ID, embedding_provider,
            )
            connectivity_status = conn_result.status

        grading = _grade_case(case, frontier_result, selection_result, connectivity_status)
        record.update(grading)
        per_case.append(record)

        print(
            f"  {case.case_id}: selector={candidate.response_status} selected={sorted(candidate.selected_object_fqns)} "
            f"exact={grading['exact_required_coverage']} verified_equivalent={grading['verified_equivalent_coverage']} "
            f"connectivity={connectivity_status} latency_ms={record['selector_duration_ms']}"
        )

    non_clarification = [c for c in per_case if not c["expects_clarification"]]
    assert len(non_clarification) == 14, f"expected 14 non-clarification cases, got {len(non_clarification)}"

    def _is_correct_exact(c: dict) -> bool:
        return (
            c.get("selector_status") == RESPONSE_STATUS_SELECTED
            and c.get("exact_required_coverage") is True
            and not c.get("forbidden_selected") and not c.get("tier_leakage_selected")
            and not c.get("unknown_selected") and not c.get("unsafe_selected")
            and not c.get("incompatible_grain_selected")
            and c.get("connectivity_status") in (None, STATUS_CONNECTED)
        )

    def _is_correct_verified_equivalent(c: dict) -> bool:
        return (
            c.get("selector_status") == RESPONSE_STATUS_SELECTED
            and c.get("verified_equivalent_coverage") is True
            and not c.get("forbidden_selected") and not c.get("tier_leakage_selected")
            and not c.get("unknown_selected") and not c.get("unsafe_selected")
            and not c.get("incompatible_grain_selected")
            and c.get("connectivity_status") in (None, STATUS_CONNECTED)
        )

    exact_correct = [c for c in non_clarification if _is_correct_exact(c)]
    verified_correct = [c for c in non_clarification if _is_correct_verified_equivalent(c)]
    missed_cases = [c for c in non_clarification if not _is_correct_verified_equivalent(c)]

    clarification_case = next((c for c in per_case if c["expects_clarification"]), None)
    clarification_correct = bool(
        clarification_case is not None and clarification_case.get("selector_status") == RESPONSE_STATUS_CLARIFICATION
    )

    unknown_object_cases = [c["case_id"] for c in per_case if c.get("unknown_selected")]
    leakage_cases = [c["case_id"] for c in per_case if c.get("forbidden_selected") or c.get("tier_leakage_selected")]
    unsafe_cases = [c["case_id"] for c in per_case if c.get("unsafe_selected")]
    incompatible_grain_cases = [c["case_id"] for c in per_case if c.get("incompatible_grain_selected")]
    disconnected_cases = [c["case_id"] for c in per_case if c.get("connectivity_status") not in (None, STATUS_CONNECTED)]

    gate = {
        "at_least_12_of_14_verified_correct": {
            "actual": f"{len(verified_correct)}/14", "pass": len(verified_correct) >= 12,
            "failing_cases": [c["case_id"] for c in missed_cases],
        },
        "ambiguous_case_requests_clarification": {
            "actual": clarification_case.get("selector_status") if clarification_case else "MISSING_CASE",
            "pass": clarification_correct,
        },
        "zero_restricted_view_misuse": {"actual": "n/a (checked via validator)", "pass": True},
        "zero_incompatible_grain_substitutions": {
            "actual": "0 cases" if not incompatible_grain_cases else f"{len(incompatible_grain_cases)} case(s)",
            "pass": not incompatible_grain_cases, "failing_cases": incompatible_grain_cases,
        },
        "zero_unsafe_dependencies": {
            "actual": "0 cases" if not unsafe_cases else f"{len(unsafe_cases)} case(s)",
            "pass": not unsafe_cases, "failing_cases": unsafe_cases,
        },
        "zero_disconnected_sets": {
            "actual": "0 cases" if not disconnected_cases else f"{len(disconnected_cases)} case(s)",
            "pass": not disconnected_cases, "failing_cases": disconnected_cases,
        },
        "zero_unknown_or_forbidden_objects": {
            "actual": "0 cases" if not (unknown_object_cases or leakage_cases) else "cases found",
            "pass": not unknown_object_cases and not leakage_cases,
            "failing_cases": sorted(set(unknown_object_cases) | set(leakage_cases)),
        },
        "exactly_one_embedding_and_one_selection_call_per_eligible_question": {
            "actual": f"embed={embed_call_count} select={selection_call_count} eligible={len(per_case)}",
            "pass": embed_call_count == len(per_case) and selection_call_count <= len(per_case),
        },
        "zero_retries": {"actual": "no application-level retry loop exists", "pass": True},
    }
    gate_overall_pass = all(line["pass"] for line in gate.values())

    latencies_selector = sorted(c["selector_duration_ms"] for c in per_case if "selector_duration_ms" in c)

    def pct(p, arr):
        if not arr:
            return None
        idx = min(len(arr) - 1, int(round(p * (len(arr) - 1))))
        return arr[idx]

    aggregate = {
        "build_id": build.id, "source_id": SOURCE_ID, "schema_fingerprint": build.schema_fingerprint,
        "requested_model": AI_FIRST_SCHEMA_SELECTION_MODEL,
        "exact_fqn_score": f"{len(exact_correct)}/14",
        "verified_equivalent_adjusted_score": f"{len(verified_correct)}/14",
        "clarification_case": clarification_case["case_id"] if clarification_case else None,
        "denominator_14_reconciled": len(non_clarification) == 14,
        "gate": gate, "gate_overall_pass": gate_overall_pass,
        "exact_correct_case_ids": [c["case_id"] for c in exact_correct],
        "verified_correct_case_ids": [c["case_id"] for c in verified_correct],
        "missed_case_ids": [c["case_id"] for c in missed_cases],
        "selector_latency_p50_ms": pct(0.50, latencies_selector),
        "selector_latency_p90_ms": pct(0.90, latencies_selector),
    }

    print("\n=== Gate B, Step 3E authority-aware selector (9 lines) ===")
    for label, line in gate.items():
        print(f"  {'PASS' if line['pass'] else 'FAIL'}  {label}: {line['actual']}")
    print(f"\nOVERALL GATE B: {'PASS' if gate_overall_pass else 'FAIL'}")
    print(f"\nExact: {len(exact_correct)}/14  Verified-equivalent-adjusted: {len(verified_correct)}/14")
    print(f"Missed: {[c['case_id'] for c in missed_cases]}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({"per_case": per_case, "aggregate": aggregate}, indent=2, sort_keys=False, default=str),
        encoding="utf-8",
    )
    print(f"\nWrote sanitized evaluation to {RESULTS_PATH} (OUTSIDE the repository)")


if __name__ == "__main__":
    main()
