"""
Phase 6, Direct-Schema Experiment Step 1 — Part 2: prompt feasibility
measurement.

Read-only. Zero provider calls, zero SQL Server calls, zero database
writes. Loads the real latest READY retrieval build for CCPP
(source_id=SOURCE_ID), builds the complete Tier 1/2 schema dump via
core.sqlservice.direct_schema_experiment, and measures whether the
resulting prompt fits the configured model's published context window —
using core.config.OPENAI_MODEL, the SAME model core.sqlservice.
sql_generation_providers.OpenAITextToSqlProvider would use for a real
call. Never truncates or removes anything to force a fit.

Run:
    venv/Scripts/python docs/validation/run_direct_schema_experiment_step1_feasibility.py
"""

from __future__ import annotations

import json
import os
import sys
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

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402

from core.config import OPENAI_MODEL, SQL_GENERATION_MAX_OUTPUT_TOKENS  # noqa: E402
from core.sqlservice.direct_schema_experiment import (  # noqa: E402
    ELIGIBLE_TIERS,
    KNOWN_MODEL_CONTEXT_WINDOWS,
    build_direct_schema_context,
    measure_prompt_feasibility,
)
from core.sqlservice.retrieval_store import (  # noqa: E402
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)

SOURCE_ID = 10004
USER_ID = "28"

_RESULTS_PATH = _PROJECT_ROOT / "docs" / "validation" / "results" / "direct_schema_experiment_step1_feasibility.json"


def _imported_module_names(py_file: Path) -> list[str]:
    """AST-based (not substring) import scan — a module's own docstring or
    comments may legitimately NAME another module in prose without
    importing it; only real `import`/`from ... import` statements count."""
    import ast

    text = py_file.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text, filename=str(py_file))
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _verify_zero_advanced_component_wiring() -> list[str]:
    """Same safety-boundary proof style as
    run_join_planner_phase6_step3d_full_evidence_evaluation.py's own
    _verify_zero_production_wiring — direct source scan, not a trusted
    flag default. Confirms this experiment's OWN module never imports any
    advanced selection component, and that nothing outside
    core/sqlservice/direct_schema_experiment.py + its own tests/docs
    imports IT (i.e. it is not wired into any route yet)."""
    offending: list[str] = []
    skip_dirs = {"tests", "docs", ".git", "venv", "node_modules", "__pycache__"}
    forbidden_tokens = (
        "schema_linker", "join_planner", "relationship_graph", "vector_index",
        "lexical_index", "canonical_scoring", "context_packing",
        "sql_example_retrieval", "sql_example_model", "sql_example_store",
        "sql_example_vector_index", "semantic_enrichment", "embedding_providers",
        "embedding_store", "embedding_coordinator",
    )
    for py_file in _PROJECT_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_PROJECT_ROOT)
        parts = rel.parts
        if parts[0] in skip_dirs:
            continue
        imported = _imported_module_names(py_file)
        if parts[:2] == ("core", "sqlservice") and parts[-1] == "direct_schema_experiment.py":
            for name in imported:
                if any(token in name for token in forbidden_tokens):
                    offending.append(f"{rel} (advanced component import {name!r} inside the experimental module itself)")
            continue
        for name in imported:
            if "direct_schema_experiment" in name:
                offending.append(f"{rel} (imports the experimental service {name!r} outside tests/docs)")
    return offending


def main() -> None:
    wiring_offenses = _verify_zero_advanced_component_wiring()

    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        raise SystemExit(f"FEASIBILITY CHECK FAILED: no READY retrieval build for source_id={SOURCE_ID}")

    all_documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    eligible_documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID, tiers=ELIGIBLE_TIERS)
    tier_counts = {}
    for doc in all_documents:
        tier_counts[doc.retrieval_tier] = tier_counts.get(doc.retrieval_tier, 0) + 1

    schema_context = build_direct_schema_context(build, eligible_documents)

    reports = []
    for case in CCPP_GOLDEN_CASES:
        report = measure_prompt_feasibility(
            case.question, schema_context, model=OPENAI_MODEL, max_output_tokens=SQL_GENERATION_MAX_OUTPUT_TOKENS,
        )
        reports.append((case.case_id, report))

    representative_case_id, representative = max(reports, key=lambda pair: pair[1].total_request_characters)

    result = {
        "source_id": SOURCE_ID,
        "user_id": USER_ID,
        "build_id": build.id,
        "build_created_at": build.created_at,
        "build_total_document_count": build.document_count,
        "tier_counts_in_build": tier_counts,
        "eligible_object_count": representative.eligible_object_count,
        "eligible_tiers": list(ELIGIBLE_TIERS),
        "column_count": representative.column_count,
        "schema_characters": representative.schema_characters,
        "estimated_schema_tokens": representative.estimated_schema_tokens,
        "largest_objects": [
            {"object_fqn": o.object_fqn, "column_count": o.column_count, "characters": o.characters}
            for o in representative.largest_objects
        ],
        "model": OPENAI_MODEL,
        "model_context_window": KNOWN_MODEL_CONTEXT_WINDOWS.get(OPENAI_MODEL),
        "max_output_tokens_reserved": SQL_GENERATION_MAX_OUTPUT_TOKENS,
        "per_question": [
            {
                "case_id": case_id,
                "question_characters": r.question_characters,
                "total_request_characters": r.total_request_characters,
                "estimated_total_input_tokens": r.estimated_total_input_tokens,
                "fits": r.fits,
            }
            for case_id, r in reports
        ],
        "worst_case_question": representative_case_id,
        "worst_case_total_request_characters": representative.total_request_characters,
        "worst_case_estimated_total_input_tokens": representative.estimated_total_input_tokens,
        "worst_case_fits": representative.fits,
        "worst_case_reason": representative.reason,
        "advanced_component_wiring_offenses": wiring_offenses,
        "rollout_flags_disabled": True,  # SQL_DIRECT_FULL_SCHEMA_EXPERIMENT_ENABLED default is False; this script never sets it
        "decision": (
            "PROCEED_TO_PART_7" if representative.fits and not wiring_offenses
            else "STOP_AND_REPORT — the complete Tier 1/2 schema does not fit safely; do not truncate, do not proceed to Part 7"
        ),
    }

    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(result, indent=2, sort_keys=False), encoding="utf-8")

    print(json.dumps(result, indent=2, sort_keys=False))
    print(f"\nWritten to {_RESULTS_PATH}")


if __name__ == "__main__":
    main()
