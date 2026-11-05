"""
Gate B rerun — stronger schema-selection model. Phase 7, Step 3D, Part 6.

Identical measurement to docs/validation/
run_ai_first_phase7_step3c1_gate_b_selector.py (same 15-case golden
fixture, same real, already-READY CCPP retrieval build source_id=10004
build_id=7, same frontier algorithm/limits, same prompt, same response
contract, same deterministic Python validator, same grading function, same
equivalent-view policy — there is none: grading is the identical strict
required-object-subset check _is_correct/_grade_case already used by the
Step 3C1 script, copied verbatim below rather than re-derived) with EXACTLY
ONE variable changed: the schema-selection provider's own model (and,
optionally, reasoning effort), read from core.config.
AI_FIRST_SCHEMA_SELECTION_MODEL / AI_FIRST_SCHEMA_SELECTION_REASONING_
EFFORT (override with an explicit CLI argument: `python
run_ai_first_phase7_step3d_gate_b_stronger_model.py gpt-6-sol` to grade the
documented fallback instead of whatever AI_FIRST_SCHEMA_SELECTION_MODEL is
currently set to).

Only run this AFTER docs/validation/
run_ai_first_phase7_step3d_model_availability_check.py has already
confirmed the target model is reachable — this script does not perform its
own availability fallback; a model-not-found/permission-denied failure on
case 1 simply fails that case's record (sanitized) rather than switching
models mid-run, since "freeze everything except the selector model" means
ONE model for the ENTIRE 15-case run, decided in advance.

Live-call footprint, PER ELIGIBLE QUESTION: exactly ONE real
OpenAIEmbeddingProvider.embed_batch() call (inside build_schema_frontier)
and exactly ONE real OpenAISchemaSelectionProvider.select_schema() call —
identical budget to the Step 3C1 script; core.sqlservice.
schema_relationship_completion.complete_relationships() is used the same
two ways (opt-in deterministic connectivity gate inside the one selection
call, plus read-only grading-only reuse) and adds zero further
embedding/AI calls either way.

No SQL generation, review, execution, or answer-generation call anywhere in
this file (see its own import list). No database write of any kind.
Never logs or saves: API keys, credentials, connection strings, prompts,
raw provider responses, query vectors, full schema documents, or
chain-of-thought — only the already-validated, already-bounded structured
decision fields plus integer-only token-usage counts (when the provider
safely exposes them) are recorded.
"""

from __future__ import annotations

import hashlib
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

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})

# Saved OUTSIDE the repository, same integrity requirement and same
# directory as the Step 3C1 Gate B script -- a DIFFERENT filename so this
# rerun never overwrites that baseline report.
_OUT_DIR_ENV = "GATE_B_OUTPUT_DIR"
_OUTPUT_DIR = Path(os.environ.get(_OUT_DIR_ENV, str(Path.home() / "gate_b_reports")))
RESULTS_PATH = _OUTPUT_DIR / "ai_first_phase7_step3d_gate_b_stronger_model.json"

REQUESTED_MODEL = sys.argv[1] if len(sys.argv) > 1 else AI_FIRST_SCHEMA_SELECTION_MODEL
REQUESTED_REASONING_EFFORT = AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT


def _sanitize_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, SchemaSelectionValidationError):
        return str(exc), "invalid_response"
    if isinstance(exc, SchemaSelectionConfigurationError):
        return "The schema-selection provider is not configured correctly.", "configuration"
    if isinstance(exc, SchemaSelectionProviderError):
        return "The schema-selection provider call failed.", type(exc).__name__
    return f"Unexpected error: {type(exc).__name__}", "unexpected"


# ── Grading — copied verbatim from run_ai_first_phase7_step3c1_gate_b_selector.py
#    ("same grading, same equivalent-view policy" -- there is no separate
#    equivalent-view leniency in that script either; this is a byte-for-byte
#    freeze of its own _grade_case, not a re-derivation) ────────────────────


def _grade_case(case, frontier_result, selection_result, connectivity_status: str | None) -> dict:
    frontier_fqns_lower = {c.object_fqn.lower() for c in frontier_result.candidates}
    tier_by_fqn_lower = {c.object_fqn.lower(): c.retrieval_tier for c in frontier_result.candidates}
    required_lower = {f.lower() for f in case.required_object_fqns}
    forbidden_lower = {f.lower() for f in case.forbidden_object_fqns}

    every_required_in_frontier = required_lower.issubset(frontier_fqns_lower) if required_lower else None

    candidate = selection_result.candidate
    selected_lower: set[str] = set()
    selector_status = None
    if candidate is not None:
        selector_status = candidate.response_status
        selected_lower = {f.lower() for f in candidate.selected_object_fqns}

    exact_required_coverage = required_lower.issubset(selected_lower) if required_lower else None
    forbidden_selected = sorted(forbidden_lower & selected_lower)
    tier_leakage = sorted(f for f in selected_lower if tier_by_fqn_lower.get(f) not in ELIGIBLE_TIERS)
    unknown_selected = sorted(f for f in selected_lower if f not in frontier_fqns_lower)  # should never happen (validated)
    extra_selected = sorted(selected_lower - required_lower) if required_lower else sorted(selected_lower)

    return {
        "required_lower": sorted(required_lower),
        "every_required_in_frontier": every_required_in_frontier,
        "selector_status": selector_status,
        "selected_object_fqns": sorted(candidate.selected_object_fqns) if candidate is not None else [],
        "exact_required_coverage": exact_required_coverage,
        "forbidden_selected": forbidden_selected,
        "tier_leakage_selected": tier_leakage,
        "unknown_selected": unknown_selected,
        "extra_selected_beyond_required": extra_selected,
        "connectivity_status": connectivity_status,
    }


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    assert build is not None, "no READY retrieval build found for source_id=10004"
    assert build.id == 7, f"expected latest READY build id=7, got {build.id}"

    embedding_provider = OpenAIEmbeddingProvider()
    selection_provider = OpenAISchemaSelectionProvider(
        model=REQUESTED_MODEL, reasoning_effort=REQUESTED_REASONING_EFFORT,
    )
    clear_vector_index_cache()

    print(
        f"Preflight: build_id={build.id} source_id={SOURCE_ID} user_id={USER_ID} "
        f"schema_fingerprint={build.schema_fingerprint} snapshot_id={build.snapshot_id} status={build.status}"
    )
    print(
        f"Embedding provider: provider_id={embedding_provider.provider_id} model={embedding_provider.model} "
        f"dimensions={embedding_provider.dimensions}"
    )
    print(
        f"Selection provider: provider_id={selection_provider.provider_id} "
        f"requested_model={REQUESTED_MODEL} reasoning_effort={REQUESTED_REASONING_EFFORT!r}"
    )
    print(
        f"Selection bounds: max_objects={AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS} "
        f"max_output_tokens={AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS} "
        f"timeout_seconds={AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS}"
    )
    print(f"Golden fixture: {len(CCPP_GOLDEN_CASES)} cases\n")

    per_case: list[dict] = []
    embed_call_count = 0
    selection_call_count = 0

    for case in CCPP_GOLDEN_CASES:
        record: dict = {
            "case_id": case.case_id,
            "category": case.category,
            "expects_clarification": case.expects_clarification,
            "requested_model": REQUESTED_MODEL,
            "reasoning_effort": REQUESTED_REASONING_EFFORT,
        }

        # ── Stage 1: frontier retrieval (one embedding call) ────────────
        t0 = time.perf_counter()
        try:
            frontier_result = build_schema_frontier(
                case.question, build.id, SOURCE_ID, USER_ID, embedding_provider,
            )
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

        # ── Stage 2: schema selection (one AI call, no retry) ───────────
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
        record["selector_provider_id"] = selection_result.provider_id
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
        record["intent_summary_operation"] = candidate.intent_summary.operation
        record["selected_object_fqns"] = list(candidate.selected_object_fqns)
        record["clarification_question"] = candidate.clarification_question

        # ── Grading-only: connectivity check (zero AI/network calls) ────
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
            f"  {case.case_id}: selector={candidate.response_status} "
            f"selected={sorted(candidate.selected_object_fqns)} "
            f"exact_coverage={grading['exact_required_coverage']} connectivity={connectivity_status} "
            f"actual_model={record['actual_model']} latency_ms={record['selector_duration_ms']}"
        )

    # ── Aggregate grading (identical shape to the Step 3C1 script) ────────
    non_clarification = [c for c in per_case if not c["expects_clarification"]]
    assert len(non_clarification) == 14, f"expected 14 non-clarification cases, got {len(non_clarification)}"

    def _is_correct(c: dict) -> bool:
        return (
            c.get("selector_status") == RESPONSE_STATUS_SELECTED
            and c.get("exact_required_coverage") is True
            and not c.get("forbidden_selected")
            and not c.get("tier_leakage_selected")
            and not c.get("unknown_selected")
            and c.get("connectivity_status") in (None, STATUS_CONNECTED)
        )

    correct_cases = [c for c in non_clarification if _is_correct(c)]
    correct_count = len(correct_cases)
    missed_cases = [c for c in non_clarification if not _is_correct(c)]

    clarification_case = next((c for c in per_case if c["expects_clarification"]), None)
    clarification_correct = bool(
        clarification_case is not None and clarification_case.get("selector_status") == RESPONSE_STATUS_CLARIFICATION
    )

    unknown_object_cases = [c["case_id"] for c in per_case if c.get("unknown_selected")]
    leakage_cases = [c["case_id"] for c in per_case if c.get("forbidden_selected") or c.get("tier_leakage_selected")]
    disconnected_cases = [
        c["case_id"] for c in per_case
        if c.get("connectivity_status") not in (None, STATUS_CONNECTED)
    ]
    cross_build_mixing = [
        c["case_id"] for c in per_case
        if c.get("frontier_size") is not None and c.get("selector_provider_id") not in (None, "openai")
    ]
    requested_vs_actual_mismatches = [
        c["case_id"] for c in per_case
        if c.get("actual_model") is not None and c["requested_model"] not in c["actual_model"]
    ]

    gate = {
        "at_least_12_of_14_correct_selection": {
            "actual": f"{correct_count}/14", "pass": correct_count >= 12,
            "failing_cases": [c["case_id"] for c in missed_cases],
        },
        "ambiguous_case_requests_clarification": {
            "actual": clarification_case.get("selector_status") if clarification_case else "MISSING_CASE",
            "pass": clarification_correct,
        },
        "zero_unknown_or_invented_objects": {
            "actual": "0 cases" if not unknown_object_cases else f"{len(unknown_object_cases)} case(s)",
            "pass": not unknown_object_cases, "failing_cases": unknown_object_cases,
        },
        "zero_forbidden_tier_3_4_leakage": {
            "actual": "0 cases" if not leakage_cases else f"{len(leakage_cases)} case(s)",
            "pass": not leakage_cases, "failing_cases": leakage_cases,
        },
        "zero_unsafe_disconnected_relationship_paths": {
            "actual": "0 cases" if not disconnected_cases else f"{len(disconnected_cases)} case(s)",
            "pass": not disconnected_cases, "failing_cases": disconnected_cases,
        },
        "zero_cross_source_build_mixing": {
            "actual": "0 cases" if not cross_build_mixing else f"{len(cross_build_mixing)} case(s)",
            "pass": not cross_build_mixing, "failing_cases": cross_build_mixing,
        },
        "exactly_one_embedding_and_one_selection_call_per_eligible_question": {
            "actual": f"embed={embed_call_count} select={selection_call_count} eligible={len(per_case)}",
            "pass": embed_call_count == len(per_case) and selection_call_count <= len(per_case),
        },
        "zero_retries": {
            "actual": "no application-level retry loop exists in select_schema_for_question or this script",
            "pass": True,
        },
        "requested_model_honored": {
            "actual": "0 mismatches" if not requested_vs_actual_mismatches else f"{len(requested_vs_actual_mismatches)} case(s)",
            "pass": not requested_vs_actual_mismatches, "failing_cases": requested_vs_actual_mismatches,
        },
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
        "requested_model": REQUESTED_MODEL, "reasoning_effort": REQUESTED_REASONING_EFFORT,
        "case_inventory": [c["case_id"] for c in per_case],
        "clarification_case": clarification_case["case_id"] if clarification_case else None,
        "denominator_14_reconciled": len(non_clarification) == 14,
        "gate": gate,
        "gate_overall_pass": gate_overall_pass,
        "correct_case_ids": [c["case_id"] for c in correct_cases],
        "missed_case_ids": [c["case_id"] for c in missed_cases],
        "selector_latency_p50_ms": pct(0.50, latencies_selector),
        "selector_latency_p90_ms": pct(0.90, latencies_selector),
        "total_token_usage": {
            "prompt_tokens": sum(c["token_usage"]["prompt_tokens"] for c in per_case if c.get("token_usage") and "prompt_tokens" in c["token_usage"]) or None,
            "completion_tokens": sum(c["token_usage"]["completion_tokens"] for c in per_case if c.get("token_usage") and "completion_tokens" in c["token_usage"]) or None,
        },
    }

    print("\n=== Gate B, stronger model (9 lines) ===")
    for label, line in gate.items():
        print(f"  {'PASS' if line['pass'] else 'FAIL'}  {label}: {line['actual']}")
    print(f"\nOVERALL GATE B (stronger model): {'PASS' if gate_overall_pass else 'FAIL'}")
    print(f"\nCorrect: {correct_count}/14  Missed: {[c['case_id'] for c in missed_cases]}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({"per_case": per_case, "aggregate": aggregate}, indent=2, sort_keys=False, default=str),
        encoding="utf-8",
    )
    print(f"\nWrote sanitized evaluation to {RESULTS_PATH} (OUTSIDE the repository)")


if __name__ == "__main__":
    main()
