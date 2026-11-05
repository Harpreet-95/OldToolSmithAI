"""
Phase 6, Direct-Schema Experiment Step 1 — Part 7: controlled real
15-question generation evaluation.

Real OpenAI calls, gated hard:
  - Exactly ONE core.sqlservice.direct_schema_experiment.run_direct_schema_experiment()
    call per one of the 15 exact established CCPP_GOLDEN_CASES
    (docs/validation/ccpp_golden_cases_v2.py) — never more than
    len(CCPP_GOLDEN_CASES) calls, asserted below.
  - Each of those calls makes exactly one text-to-SQL provider call
    internally (core.sqlservice.sql_generation_service.generate_sql_candidate)
    — no question-embedding call, no answer-generation call, anywhere in
    this script.
  - Zero SQL Server execution. Zero database writes. The only database
    reads are the existing, already-built retrieval-store SELECTs
    (get_latest_ready_retrieval_build / load_stored_retrieval_documents).
  - A hard pre-flight feasibility gate (Part 2's own
    measure_prompt_feasibility, zero provider calls) refuses to spend a
    single real call if the worst-case prompt does not fit safely.
  - core.config.SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED is flipped True
    ONLY in this process's own memory (never written to .env, never
    committed) — the persisted default in core/config.py stays False, the
    same runtime-only override mechanism
    tests/test_direct_schema_experiment.py uses via monkeypatch.

Every case's generated SQL and validation result is captured into a
plain dict (`_freeze_case_result`) BEFORE any grading/aggregation code
runs, and grading never mutates or re-derives that dict — "freeze before
grading," per the task's own requirement. No per-case prompt tuning of
any kind happens during or between calls.

Run (spends real OpenAI budget — up to 15 calls):
    venv/Scripts/python docs/validation/run_direct_schema_experiment_step1_evaluation.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "direct-schema-experiment-non-production-secret-1")
os.environ.setdefault("USER_ID_SALT", "direct-schema-experiment-non-production-salt-1")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_VALIDATION_DIR = Path(__file__).resolve().parent
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402

import core.config as config_module  # noqa: E402
from core.sqlservice.direct_schema_experiment import (  # noqa: E402
    ELIGIBLE_TIERS,
    STATUS_OK as EXPERIMENT_STATUS_OK,
    build_direct_schema_context,
    measure_prompt_feasibility,
    run_direct_schema_experiment,
)
from core.sqlservice.retrieval_store import (  # noqa: E402
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.sql_generation_providers import (  # noqa: E402
    OpenAITextToSqlProvider,
    TextToSqlConfigurationError,
    TextToSqlProviderError,
)
from core.sqlservice.sql_generation_service import TextToSqlValidationError  # noqa: E402
from core.sqlservice.sql_validation import STATUS_OK as VALID_STATUS_OK  # noqa: E402

SOURCE_ID = 10004
USER_ID = "28"
MAX_CALLS = 15

# Baseline this experiment is compared against (SQL rebuild Phase 6, Step
# 3D full-evidence join-planner evaluation — the current architecture's
# own best-known numbers over the SAME 14-case/24-object baseline).
BASELINE_PRIMARY_COMPLETE = (6, 14)
BASELINE_CONNECTED_COMPLETE = (7, 14)

# Publicly published OpenAI pricing at the time of this codebase's model
# knowledge cutoff — NOT queried live, NOT guaranteed current. Reported
# only as an approximate order-of-magnitude cost estimate; verify current
# pricing before treating this as a budget figure.
_APPROX_USD_PER_1M_INPUT_TOKENS = {"gpt-4o-mini": 0.15}
_APPROX_USD_PER_1M_OUTPUT_TOKENS = {"gpt-4o-mini": 0.60}

_RESULTS_PATH = _PROJECT_ROOT / "docs" / "validation" / "results" / "direct_schema_experiment_step1_evaluation.json"


def _extract_referenced_tables(sql: str) -> list[str]:
    """Independent (not the validator's own) schema-INDEPENDENT parse of
    which tables the generated SQL text actually names — used only for
    this report's own "referenced/hallucinated objects" column, never fed
    back into validation."""
    if not sql or not sql.strip():
        return []
    try:
        trees = [t for t in sqlglot.parse(sql, read="tsql") if t is not None]
    except Exception:
        return []
    names: set[str] = set()
    for tree in trees:
        cte_names_lower = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
        for table in tree.find_all(exp.Table):
            if not table.db and table.name and table.name.lower() in cte_names_lower:
                continue  # a CTE/derived-table reference, not a real schema object
            fqn = f"{table.db}.{table.name}" if table.db else (table.name or "")
            if fqn:
                names.add(fqn)
    return sorted(names)


def _freeze_case_result(case, provider) -> dict:
    """Exactly one run_direct_schema_experiment() call. Captures every
    field this report needs into a plain dict — frozen the instant this
    function returns; nothing below this line is ever re-derived from a
    live object again."""
    call_start = time.monotonic()
    error_text = None
    result = None
    try:
        result = run_direct_schema_experiment(case.question, SOURCE_ID, USER_ID, provider)
    except (TextToSqlConfigurationError, TextToSqlProviderError, TextToSqlValidationError) as exc:
        error_text = f"{type(exc).__name__}: {exc}"
    wall_latency = time.monotonic() - call_start

    if result is None:
        return {
            "case_id": case.case_id, "category": case.category, "question": case.question,
            "experiment_status": "PROVIDER_OR_GENERATION_ERROR",
            "generation_status": None, "needs_clarification": None, "generated_sql": "",
            "declared_referenced_object_fqns": [], "referenced_tables_parsed_from_sql": [],
            "hallucinated_objects": [], "validation_status": None, "validation_error": None,
            "canonical_referenced_object_fqns": [], "validated_column_references": [],
            "required_object_fqns": sorted(case.required_object_fqns),
            "forbidden_object_fqns": sorted(case.forbidden_object_fqns),
            "expects_clarification": case.expects_clarification,
            "provider_latency_seconds": None, "total_latency_seconds": wall_latency,
            "build_id": None, "estimated_input_tokens": None, "error": error_text,
        }

    candidate = result.generation.candidate if result.generation else None
    sql_text = candidate.sql if candidate else ""
    referenced_in_sql = _extract_referenced_tables(sql_text)
    known_fqns_lower = set()  # filled in by caller via closure below

    return {
        "case_id": case.case_id, "category": case.category, "question": case.question,
        "experiment_status": result.status,
        "generation_status": result.generation.status if result.generation else None,
        "needs_clarification": candidate.needs_clarification if candidate else None,
        "clarification_question": candidate.clarification_question if candidate else None,
        "generated_sql": sql_text,
        "declared_referenced_object_fqns": list(candidate.referenced_object_fqns) if candidate else [],
        "referenced_tables_parsed_from_sql": referenced_in_sql,
        "hallucinated_objects": [],  # filled in by caller, which has known_fqns_lower
        "validation_status": result.validation.status if result.validation else None,
        "validation_error": result.validation.error if result.validation else None,
        "canonical_referenced_object_fqns": (
            list(result.validation.canonical_referenced_object_fqns) if result.validation else []
        ),
        "validated_column_references": (
            [{"object_fqn": c.object_fqn, "column_name": c.column_name} for c in result.validation.validated_column_references]
            if result.validation else []
        ),
        "required_object_fqns": sorted(case.required_object_fqns),
        "forbidden_object_fqns": sorted(case.forbidden_object_fqns),
        "expects_clarification": case.expects_clarification,
        "provider_latency_seconds": result.provider_latency_seconds,
        "total_latency_seconds": result.total_latency_seconds,
        "build_id": result.build_id,
        "estimated_input_tokens": (
            result.feasibility.estimated_total_input_tokens if result.feasibility else None
        ),
        "error": error_text,
    }


def _grade_case(frozen: dict, known_fqns_lower: set[str]) -> dict:
    """Read-only grading against the ALREADY-frozen dict — never mutates
    it, never re-runs anything."""
    required = set(frozen["required_object_fqns"])
    forbidden = set(frozen["forbidden_object_fqns"])
    canonical = set(frozen["canonical_referenced_object_fqns"])
    hallucinated = sorted(t for t in frozen["referenced_tables_parsed_from_sql"] if t.lower() not in known_fqns_lower)

    valid_sql = frozen["validation_status"] == VALID_STATUS_OK
    required_covered = required & canonical
    complete_table_selection = required.issubset(canonical) if required else None
    forbidden_leakage = forbidden & canonical
    correct_clarification = (
        bool(frozen["expects_clarification"]) and frozen["needs_clarification"] is True
    )

    return {
        "case_id": frozen["case_id"],
        "category": frozen["category"],
        "valid_sql": valid_sql,
        "required_object_fqns": sorted(required),
        "required_covered": sorted(required_covered),
        "required_covered_count": len(required_covered),
        "required_total_count": len(required),
        "complete_table_selection": complete_table_selection,
        "forbidden_leakage": sorted(forbidden_leakage),
        "hallucinated_objects": hallucinated,
        "expects_clarification": frozen["expects_clarification"],
        "correct_clarification": correct_clarification if frozen["expects_clarification"] else None,
    }


def main() -> None:
    # Process-local override only — see module docstring. The persisted
    # default in core/config.py is never touched.
    config_module.SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED = True

    cases = list(CCPP_GOLDEN_CASES)
    if len(cases) > MAX_CALLS:
        raise SystemExit(f"REFUSED: {len(cases)} golden cases exceeds the authorized maximum of {MAX_CALLS} calls")

    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        raise SystemExit(f"REFUSED: no READY retrieval build for source_id={SOURCE_ID}")
    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID, tiers=ELIGIBLE_TIERS)
    schema_context = build_direct_schema_context(build, documents)
    known_fqns_lower = {d.object_fqn.lower() for d in schema_context.documents}

    provider = OpenAITextToSqlProvider()

    # Part 2 gate, re-checked immediately before spending real budget.
    worst = max(
        (
            measure_prompt_feasibility(c.question, schema_context, model=provider.model)
            for c in cases
        ),
        key=lambda r: r.total_request_characters,
    )
    if not worst.fits:
        raise SystemExit(f"REFUSED: worst-case prompt does not fit safely — {worst.reason}")

    print(f"Feasibility OK ({worst.estimated_total_input_tokens} estimated input tokens). "
          f"Making up to {len(cases)} real OpenAI calls against model={provider.model!r}...")

    frozen_results: list[dict] = []
    for i, case in enumerate(cases, start=1):
        print(f"[{i}/{len(cases)}] {case.case_id}: {case.question!r}")
        frozen = _freeze_case_result(case, provider)
        frozen["hallucinated_objects"] = sorted(
            t for t in frozen["referenced_tables_parsed_from_sql"] if t.lower() not in known_fqns_lower
        )
        frozen_results.append(frozen)

    # ── Freeze complete — every call has been made. Nothing below this
    #    line performs a provider call or mutates a frozen_results entry. ──
    assert len(frozen_results) == len(cases) <= MAX_CALLS

    graded = [_grade_case(r, known_fqns_lower) for r in frozen_results]

    cases_with_required = [g for g in graded if g["required_total_count"] > 0]
    required_object_coverage_numerator = sum(g["required_covered_count"] for g in cases_with_required)
    required_object_coverage_denominator = sum(g["required_total_count"] for g in cases_with_required)
    complete_table_selection_count = sum(1 for g in cases_with_required if g["complete_table_selection"])
    valid_sql_count = sum(1 for g in graded if g["valid_sql"])
    hallucinated_object_total = sum(len(g["hallucinated_objects"]) for g in graded)
    forbidden_leakage_total = sum(len(g["forbidden_leakage"]) for g in graded)
    clarification_cases = [g for g in graded if g["expects_clarification"]]
    correct_clarification_count = sum(1 for g in clarification_cases if g["correct_clarification"])

    latencies = [r["total_latency_seconds"] for r in frozen_results if r["total_latency_seconds"] is not None]
    provider_latencies = [r["provider_latency_seconds"] for r in frozen_results if r["provider_latency_seconds"] is not None]

    def _percentile(data: list[float], pct: float) -> float | None:
        if not data:
            return None
        ordered = sorted(data)
        k = (len(ordered) - 1) * pct
        f, c = int(k), min(int(k) + 1, len(ordered) - 1)
        if f == c:
            return ordered[f]
        return ordered[f] + (ordered[c] - ordered[f]) * (k - f)

    estimated_input_tokens_total = sum(
        r["estimated_input_tokens"] for r in frozen_results if r["estimated_input_tokens"] is not None
    )
    estimated_output_tokens_total = sum(
        len(r["generated_sql"]) for r in frozen_results
    ) // 4  # crude, disclosed-as-estimate proxy — no real usage exposed by ProviderSqlGenerationResponse

    model = provider.model
    input_cost = (
        estimated_input_tokens_total / 1_000_000 * _APPROX_USD_PER_1M_INPUT_TOKENS[model]
        if model in _APPROX_USD_PER_1M_INPUT_TOKENS else None
    )
    output_cost = (
        estimated_output_tokens_total / 1_000_000 * _APPROX_USD_PER_1M_OUTPUT_TOKENS[model]
        if model in _APPROX_USD_PER_1M_OUTPUT_TOKENS else None
    )

    summary = {
        "source_id": SOURCE_ID,
        "build_id": build.id,
        "model": model,
        "total_calls_made": len(frozen_results),
        "max_authorized_calls": MAX_CALLS,
        "required_object_coverage": f"{required_object_coverage_numerator}/{required_object_coverage_denominator}",
        "complete_table_selection_coverage": f"{complete_table_selection_count}/{len(cases_with_required)}",
        "valid_sql_count": f"{valid_sql_count}/{len(graded)}",
        "hallucinated_object_count_total": hallucinated_object_total,
        "forbidden_object_leakage_count_total": forbidden_leakage_total,
        "clarification_cases_total": len(clarification_cases),
        "correct_clarification_count": correct_clarification_count,
        "latency_seconds": {
            "p50": _percentile(latencies, 0.50), "p90": _percentile(latencies, 0.90), "p99": _percentile(latencies, 0.99),
            "min": min(latencies) if latencies else None, "max": max(latencies) if latencies else None,
        },
        "provider_latency_seconds": {
            "p50": _percentile(provider_latencies, 0.50), "p90": _percentile(provider_latencies, 0.90),
            "p99": _percentile(provider_latencies, 0.99),
        },
        "tokens_estimated": {
            "total_input_tokens_estimated": estimated_input_tokens_total,
            "total_output_tokens_estimated": estimated_output_tokens_total,
            "note": (
                "ESTIMATES ONLY — core.sqlservice.sql_generation_providers.ProviderSqlGenerationResponse "
                "does not expose real provider token-usage counts (this experiment deliberately did not "
                "modify that shared production contract to preserve pipeline reuse purity). Input tokens "
                "are this module's own pre-flight character-based estimate; output tokens are a crude "
                "len(generated_sql)/4 proxy."
            ),
        },
        "estimated_cost_usd": {
            "input": input_cost, "output": output_cost,
            "total": (input_cost + output_cost) if (input_cost is not None and output_cost is not None) else None,
            "note": "Based on publicly published pricing, not queried live — verify current pricing before treating as a budget figure.",
        },
        "baseline_comparison": {
            "baseline_primary_complete": f"{BASELINE_PRIMARY_COMPLETE[0]}/{BASELINE_PRIMARY_COMPLETE[1]}",
            "baseline_connected_complete": f"{BASELINE_CONNECTED_COMPLETE[0]}/{BASELINE_CONNECTED_COMPLETE[1]}",
            "direct_schema_complete_table_selection": f"{complete_table_selection_count}/{len(cases_with_required)}",
        },
    }

    output = {"summary": summary, "per_case_graded": graded, "per_case_frozen": frozen_results}
    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(output, indent=2, sort_keys=False, default=str), encoding="utf-8")

    print("\n" + json.dumps(summary, indent=2, sort_keys=False))
    print(f"\nWritten to {_RESULTS_PATH}")


if __name__ == "__main__":
    main()
