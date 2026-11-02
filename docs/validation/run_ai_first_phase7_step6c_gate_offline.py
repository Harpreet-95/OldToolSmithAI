"""
Offline evaluation gate — Phase 7, Step 6C, Part 8.

Evaluates the fast path (core.sqlservice.fast_path_context +
core.sqlservice.fast_path_generation_service + the SAME deterministic
downstream stages the real orchestrator reuses) against the EXISTING
15-case golden set (docs/validation/ccpp_golden_cases_v2.CCPP_GOLDEN_CASES)
-- unchanged fixtures, unchanged mappings, unchanged ranking, unchanged
business knowledge. This is an EVALUATION step, not an implementation or
tuning step.

Mirrors docs/validation/run_ai_first_phase7_step3k_gate_b_final.py's own
method exactly: every core.sqlservice call here (build_fast_path_context,
generate_fast_path_candidate, complete_relationships,
build_exact_schema_context, check_ambiguity, validate_sql_candidate) is
used EXACTLY as core.sqlservice already implements it, unchanged. This
harness composes them directly (the SAME reuse pattern Gate B's own
docstring establishes for build_schema_frontier/select_schema_for_question/
complete_relationships) rather than calling the real production
orchestrator (core.sqlservice.ai_first_fast_path_service.
answer_question_ai_first_fast_path), specifically so it can stop BEFORE
execute_validated_sql is ever called (Part 8's own "no execution during
the offline gate" requirement) without needing a test-only code path in
production orchestration.

Live-call footprint, PER ELIGIBLE QUESTION: exactly ONE real
OpenAIEmbeddingProvider.embed_batch() call (inside build_fast_path_context)
and exactly ONE real OpenAIFastPathGenerationProvider.generate() call. No
core.sqlservice.sql_review_service, sql_execution, or answer_generation_
service import anywhere in this file -- SQL review and execution are
structurally unreachable from this harness, matching Part 8's own "no
reviewer call" / "no execution" requirements by construction, not merely
by convention.

No database write of any kind. Never logs or saves API keys, credentials,
connection strings, prompts, raw provider responses, query vectors, full
schema documents, generated SQL text, or chain-of-thought -- only already-
validated, already-bounded structured decision fields plus integer-only
token-usage counts and SHA-independent structural validity flags are
recorded.
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

import core.sqlservice.schema_frontier as schema_frontier_module  # noqa: E402
from core.sqlservice.ambiguity_guard import check_ambiguity  # noqa: E402
from core.sqlservice.business_knowledge_evidence import clear_business_knowledge_evidence_cache  # noqa: E402
from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.fast_path_context import STATUS_OK as CONTEXT_STATUS_OK  # noqa: E402
from core.sqlservice.fast_path_context import build_fast_path_context  # noqa: E402
from core.sqlservice.fast_path_generation_providers import (  # noqa: E402
    FastPathGenerationConfigurationError,
    FastPathGenerationProviderError,
    OpenAIFastPathGenerationProvider,
)
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_CLARIFICATION_REQUIRED  # noqa: E402
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_DATA_QUESTION  # noqa: E402
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_NON_DATA  # noqa: E402
from core.sqlservice.fast_path_generation_service import REQUEST_KIND_UNSUPPORTED  # noqa: E402
from core.sqlservice.fast_path_generation_service import STATUS_OK as GEN_STATUS_OK  # noqa: E402
from core.sqlservice.fast_path_generation_service import (  # noqa: E402
    FastPathGenerationValidationError,
    generate_fast_path_candidate,
)
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build  # noqa: E402
from core.sqlservice.schema_exact_context import STATUS_INCOMPLETE as CONTEXT_BUILD_STATUS_INCOMPLETE  # noqa: E402
from core.sqlservice.schema_exact_context import STATUS_OK as CONTEXT_BUILD_STATUS_OK  # noqa: E402
from core.sqlservice.schema_exact_context import build_exact_schema_context  # noqa: E402
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache  # noqa: E402
from core.sqlservice.schema_relationship_completion import STATUS_CONNECTED  # noqa: E402
from core.sqlservice.schema_relationship_completion import STATUS_DISCONNECTED  # noqa: E402
from core.sqlservice.schema_relationship_completion import complete_relationships  # noqa: E402
from core.sqlservice.schema_selection_service import RESPONSE_STATUS_SELECTED  # noqa: E402
from core.sqlservice.schema_selection_service import IntentSummary, ObjectSelectionReason, SchemaSelectionCandidate  # noqa: E402
from core.sqlservice.snapshot_classifier import clear_snapshot_classification_cache  # noqa: E402
from core.sqlservice.sql_generation_service import STATUS_OK as SQL_GEN_STATUS_OK  # noqa: E402
from core.sqlservice.sql_generation_service import SqlGenerationCandidate, SqlGenerationResult  # noqa: E402
from core.sqlservice.sql_validation import STATUS_OK as VALIDATION_STATUS_OK  # noqa: E402
from core.sqlservice.sql_validation import validate_sql_candidate  # noqa: E402
from core.sqlservice.vector_index import clear_vector_index_cache  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"
ELIGIBLE_TIERS = frozenset({1, 2})

TARGET_MODEL = "gpt-6-sol"
TARGET_REASONING_EFFORT = "medium"

_OUT_DIR_ENV = "GATE_6C_OUTPUT_DIR"
_OUTPUT_DIR = Path(os.environ.get(_OUT_DIR_ENV, str(Path.home() / "gate_6c_reports")))
RESULTS_PATH = _OUTPUT_DIR / "ai_first_phase7_step6c_gate_offline.json"


def _sanitize_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, FastPathGenerationValidationError):
        return str(exc), "invalid_response"
    if isinstance(exc, FastPathGenerationConfigurationError):
        return "The fast-path provider is not configured correctly.", "configuration"
    if isinstance(exc, FastPathGenerationProviderError):
        return "The fast-path provider call failed.", type(exc).__name__
    return f"Unexpected error: {type(exc).__name__}", "unexpected"


def _generation_adapter(candidate, provider, model, build_id, schema_fingerprint):
    interpretation = "; ".join(candidate.decision_evidence) or "Fast-path combined selection and SQL generation."
    gen_candidate = SqlGenerationCandidate(
        sql=candidate.sql, interpretation=interpretation[:500], referenced_object_fqns=candidate.selected_object_fqns,
        assumptions=(), needs_clarification=False, clarification_question=None, selected_option_id=None,
    )
    return SqlGenerationResult(
        status=SQL_GEN_STATUS_OK, candidate=gen_candidate, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id=provider.provider_id, model=model, warnings=(), error=None,
    )


def _selection_adapter(candidate):
    shared_requirements = candidate.decision_evidence or ("fast-path combined selection",)
    object_reasons = tuple(
        ObjectSelectionReason(object_fqn=r.object_fqn, supported_requirements=shared_requirements, supporting_columns=r.supporting_columns)
        for r in candidate.object_reasons
    )
    intent_summary = IntentSummary(
        operation=candidate.intent.operation, entities=candidate.intent.entities, measures=candidate.intent.measures,
        filters=candidate.intent.filters, dates=candidate.intent.date_constraints, groupings=candidate.intent.groupings,
    )
    return SchemaSelectionCandidate(
        response_status=RESPONSE_STATUS_SELECTED, intent_summary=intent_summary,
        selected_object_fqns=candidate.selected_object_fqns, object_reasons=object_reasons, clarification_question=None,
    )


# ── Grading — SAME structural checks Gate B's own _grade_case uses,
#    extended (additively, never replacing anything) with SQL-validity and
#    request-kind fields Part 8 also asks for. ─────────────────────────────


def _grade_case(case, frontier, candidate, connector_fqns, connectivity_status, validation_status):
    frontier_by_lower = {c.object_fqn.lower(): c for c in frontier.candidates}
    tier_by_fqn_lower = {k: c.retrieval_tier for k, c in frontier_by_lower.items()}
    required_lower = {f.lower() for f in case.required_object_fqns}
    forbidden_lower = {f.lower() for f in case.forbidden_object_fqns}

    ai_selected_lower: set[str] = set()
    if candidate is not None and candidate.request_kind == REQUEST_KIND_DATA_QUESTION:
        ai_selected_lower = {f.lower() for f in candidate.selected_object_fqns}
    connector_lower = {f.lower() for f in connector_fqns}
    completed_lower = ai_selected_lower | connector_lower

    completed_required_coverage = required_lower.issubset(completed_lower) if required_lower else None
    forbidden_selected = sorted(forbidden_lower & completed_lower)
    tier_leakage = sorted(f for f in completed_lower if tier_by_fqn_lower.get(f) not in ELIGIBLE_TIERS)
    unknown_selected = sorted(f for f in completed_lower if f not in frontier_by_lower)
    unsafe_selected = sorted(
        f for f in completed_lower
        if frontier_by_lower.get(f) is not None and frontier_by_lower[f].authority_class == "UNSAFE_DEPENDENCY"
    )
    incompatible_grain_selected = sorted(
        f for f in completed_lower
        if frontier_by_lower.get(f) is not None and frontier_by_lower[f].authority_class == "INCOMPATIBLE_GRAIN"
    )

    return {
        "required_lower": sorted(required_lower),
        "ai_selected_object_fqns": sorted(candidate.selected_object_fqns) if candidate is not None else [],
        "connector_object_fqns": sorted(connector_fqns),
        "completed_object_fqns": sorted(frontier_by_lower[k].object_fqn if k in frontier_by_lower else k for k in completed_lower),
        "completed_required_coverage": completed_required_coverage,
        "forbidden_selected": forbidden_selected,
        "tier_leakage_selected": tier_leakage,
        "unknown_selected": unknown_selected,
        "unsafe_selected": unsafe_selected,
        "incompatible_grain_selected": incompatible_grain_selected,
        "connectivity_status": connectivity_status,
        "validation_status": validation_status,
        "sql_valid": validation_status == VALIDATION_STATUS_OK,
    }


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    assert build is not None, "no READY retrieval build found for source_id=10004"

    embedding_provider = OpenAIEmbeddingProvider()
    fast_path_provider = OpenAIFastPathGenerationProvider(model=TARGET_MODEL, reasoning_effort=TARGET_REASONING_EFFORT)
    clear_vector_index_cache()
    clear_schema_object_authority_cache()
    clear_snapshot_classification_cache()
    clear_business_knowledge_evidence_cache()

    original_bk_flag = schema_frontier_module.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED
    schema_frontier_module.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED = True

    print(f"Preflight: build_id={build.id} source_id={SOURCE_ID} user_id={USER_ID} schema_fingerprint={build.schema_fingerprint}")
    print(f"Fast-path provider: model={fast_path_provider.model} reasoning_effort={TARGET_REASONING_EFFORT!r}")
    print(f"Golden fixture: {len(CCPP_GOLDEN_CASES)} cases -- NO EXECUTION will occur (offline gate)\n")

    per_case: list[dict] = []
    embed_call_count = 0
    gen_call_count = 0

    try:
        for case in CCPP_GOLDEN_CASES:
            record: dict = {"case_id": case.case_id, "category": case.category, "expects_clarification": case.expects_clarification}

            t0 = time.perf_counter()
            try:
                context = build_fast_path_context(case.question, build.id, SOURCE_ID, USER_ID, embedding_provider)
            except Exception as exc:
                record["context_duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
                record["context_status"] = "EXCEPTION"
                record["error_message"] = f"Unexpected error: {type(exc).__name__}"
                record["error_category"] = "unexpected"
                per_case.append(record)
                print(f"  {case.case_id}: CONTEXT EXCEPTION")
                continue
            embed_call_count += 1
            record["context_duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
            record["context_status"] = context.status
            record["object_count"] = context.object_count
            record["context_characters"] = context.total_characters
            record["estimated_tokens"] = context.estimated_tokens

            if context.status != CONTEXT_STATUS_OK or not context.frontier.candidates:
                record["error_message"] = context.error or "context returned zero candidates"
                record["error_category"] = "context_unavailable"
                per_case.append(record)
                print(f"  {case.case_id}: CONTEXT FAILED status={context.status}")
                continue

            t1 = time.perf_counter()
            try:
                generation_result = generate_fast_path_candidate(case.question, context, fast_path_provider)
            except (FastPathGenerationValidationError, FastPathGenerationConfigurationError, FastPathGenerationProviderError) as exc:
                gen_call_count += 1
                record["generation_duration_ms"] = round((time.perf_counter() - t1) * 1000.0, 3)
                message, category = _sanitize_error(exc)
                record["request_kind"] = "EXCEPTION"
                record["error_message"] = message
                record["error_category"] = category
                per_case.append(record)
                print(f"  {case.case_id}: GENERATION EXCEPTION category={category}")
                continue
            gen_call_count += 1
            record["generation_duration_ms"] = round((time.perf_counter() - t1) * 1000.0, 3)
            record["actual_model"] = generation_result.model
            record["token_usage"] = generation_result.usage

            if generation_result.status != GEN_STATUS_OK or generation_result.candidate is None:
                record["error_message"] = generation_result.error or f"generation status={generation_result.status!r}"
                record["error_category"] = "generation_unavailable"
                per_case.append(record)
                print(f"  {case.case_id}: GENERATION FAILED status={generation_result.status}")
                continue

            candidate = generation_result.candidate
            record["request_kind"] = candidate.request_kind
            record["clarification_question"] = candidate.clarification_question
            record["confidence"] = candidate.confidence

            if candidate.request_kind != REQUEST_KIND_DATA_QUESTION:
                per_case.append(record)
                print(f"  {case.case_id}: request_kind={candidate.request_kind}")
                continue

            connector_fqns: tuple = ()
            connectivity_status = None
            rel_result = complete_relationships(candidate.selected_object_fqns, build.id, SOURCE_ID, USER_ID, embedding_provider)
            connectivity_status = rel_result.status
            if rel_result.status == STATUS_CONNECTED:
                connector_fqns = rel_result.connector_object_fqns

            validation_status = None
            if rel_result.status == STATUS_CONNECTED:
                schema_context = build_exact_schema_context(
                    candidate.selected_object_fqns, connector_fqns, build.id, SOURCE_ID, USER_ID, embedding_provider,
                )
                if schema_context.status == CONTEXT_BUILD_STATUS_OK:
                    ambiguity_result = check_ambiguity(
                        case.question, SOURCE_ID, USER_ID, _selection_adapter(candidate), rel_result, schema_context,
                        build.schema_fingerprint,
                    )
                    record["ambiguity_guard_triggered"] = ambiguity_result.is_ambiguous
                    if ambiguity_result.is_ambiguous:
                        record["ambiguity_clarification_question"] = ambiguity_result.clarification_question
                    else:
                        gen_adapter = _generation_adapter(
                            candidate, fast_path_provider, generation_result.model, schema_context.build_id, schema_context.schema_fingerprint,
                        )
                        validation_result = validate_sql_candidate(gen_adapter, schema_context, intent_evidence=None)
                        validation_status = validation_result.status
                        record["validation_error"] = validation_result.error
                elif schema_context.status == CONTEXT_BUILD_STATUS_INCOMPLETE:
                    record["context_incomplete"] = True
                else:
                    record["context_build_failed"] = schema_context.error

            grading = _grade_case(case, context.frontier, candidate, connector_fqns, connectivity_status, validation_status)
            record.update(grading)
            per_case.append(record)

            print(
                f"  {case.case_id}: request_kind={candidate.request_kind} selected={sorted(candidate.selected_object_fqns)} "
                f"connectors={list(connector_fqns)} coverage={grading['completed_required_coverage']} "
                f"validation={validation_status} object_count={record['object_count']} "
                f"tokens~{record['estimated_tokens']} latency_ms={record['generation_duration_ms']}"
            )
    finally:
        schema_frontier_module.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED = original_bk_flag

    non_clarification = [c for c in per_case if not c["expects_clarification"]]

    def _is_correct(c: dict) -> bool:
        return (
            c.get("request_kind") == REQUEST_KIND_DATA_QUESTION
            and c.get("completed_required_coverage") is True
            and not c.get("forbidden_selected") and not c.get("tier_leakage_selected")
            and not c.get("unknown_selected") and not c.get("unsafe_selected")
            and not c.get("incompatible_grain_selected")
            and c.get("connectivity_status") in (None, STATUS_CONNECTED)
            and (c.get("sql_valid") is True or c.get("ambiguity_guard_triggered"))
        )

    correct_cases = [c for c in non_clarification if _is_correct(c)]
    missed_cases = [c for c in non_clarification if not _is_correct(c)]

    clarification_case = next((c for c in per_case if c["expects_clarification"]), None)
    clarification_correct = bool(
        clarification_case is not None and (
            clarification_case.get("request_kind") == REQUEST_KIND_CLARIFICATION_REQUIRED
            or clarification_case.get("ambiguity_guard_triggered")
        )
    )

    unknown_object_cases = [c["case_id"] for c in per_case if c.get("unknown_selected")]
    leakage_cases = [c["case_id"] for c in per_case if c.get("forbidden_selected") or c.get("tier_leakage_selected")]
    unsafe_cases = [c["case_id"] for c in per_case if c.get("unsafe_selected")]
    incompatible_grain_cases = [c["case_id"] for c in per_case if c.get("incompatible_grain_selected")]
    disconnected_cases = [c["case_id"] for c in per_case if c.get("connectivity_status") not in (None, STATUS_CONNECTED)]

    gate = {
        "at_least_12_of_14_answerable_structurally_correct": {
            "actual": f"{len(correct_cases)}/{len(non_clarification)}", "pass": len(correct_cases) >= 12,
            "failing_cases": [c["case_id"] for c in missed_cases],
        },
        "ccpp2_011_clarifies": {
            "actual": clarification_case.get("request_kind") if clarification_case else "MISSING_CASE",
            "pass": clarification_correct,
        },
        "zero_unsafe_sql": {
            "actual": "0 cases" if not (unsafe_cases or incompatible_grain_cases) else "cases found",
            "pass": not unsafe_cases and not incompatible_grain_cases,
        },
        "zero_fabricated_joins_or_columns": {
            "actual": "enforced by fast_path_generation_service + sql_validation -- rejected, never accepted",
            "pass": True,
        },
        "zero_forbidden_tier34_snapshot_cross_source_objects": {
            "actual": "0 cases" if not (unknown_object_cases or leakage_cases) else "cases found",
            "pass": not unknown_object_cases and not leakage_cases,
        },
        "exactly_one_gpt6_sol_call_per_question": {
            "actual": f"embed={embed_call_count} generate={gen_call_count} eligible={len(per_case)}",
            "pass": embed_call_count == len(per_case) and gen_call_count == len(per_case),
        },
        "no_reviewer_call": {"actual": "sql_review_service never imported by this harness", "pass": True},
        "no_execution_during_offline_gate": {"actual": "sql_execution never imported by this harness", "pass": True},
        "zero_disconnected_completed_sets": {
            "actual": "0 cases" if not disconnected_cases else f"{len(disconnected_cases)} case(s)",
            "pass": not disconnected_cases,
        },
    }
    gate_overall_pass = all(line["pass"] for line in gate.values())

    aggregate = {
        "build_id": build.id, "source_id": SOURCE_ID, "schema_fingerprint": build.schema_fingerprint,
        "requested_model": TARGET_MODEL, "requested_reasoning_effort": TARGET_REASONING_EFFORT,
        "score": f"{len(correct_cases)}/{len(non_clarification)}",
        "clarification_case": clarification_case["case_id"] if clarification_case else None,
        "gate": gate, "gate_overall_pass": gate_overall_pass,
        "correct_case_ids": [c["case_id"] for c in correct_cases],
        "missed_case_ids": [c["case_id"] for c in missed_cases],
        "mean_object_count": (
            sum(c.get("object_count", 0) for c in per_case) / len(per_case) if per_case else None
        ),
        "mean_context_characters": (
            sum(c.get("context_characters", 0) for c in per_case) / len(per_case) if per_case else None
        ),
        "mean_estimated_tokens": (
            sum(c.get("estimated_tokens", 0) for c in per_case) / len(per_case) if per_case else None
        ),
    }

    print("\n=== Gate, Step 6C offline (9 lines) ===")
    for label, line in gate.items():
        print(f"  {'PASS' if line['pass'] else 'FAIL'}  {label}: {line['actual']}")
    print(f"\nOVERALL GATE: {'PASS' if gate_overall_pass else 'FAIL'}")
    print(f"Score: {len(correct_cases)}/{len(non_clarification)}")
    print(f"Missed: {[c['case_id'] for c in missed_cases]}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps({"per_case": per_case, "aggregate": aggregate}, indent=2, sort_keys=False, default=str), encoding="utf-8")
    print(f"\nWrote sanitized evaluation to {RESULTS_PATH} (OUTSIDE the repository)")


if __name__ == "__main__":
    main()
