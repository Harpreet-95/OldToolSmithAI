"""
Phase 7, Step 3H, Part 1 — GPT-6 Sol schema-selection model availability
check.

Read-only, ONE bounded schema-selection call against the real, already-READY
CCPP retrieval build (source_id=10004, owner user_id="28", build_id=7),
using model="gpt-6-sol" at reasoning_effort="medium" — the EXACT unchanged
Step 3C-3F pipeline (core.sqlservice.schema_frontier.build_schema_frontier,
core.sqlservice.schema_selection_service.select_schema_for_question, the
SAME prompt, SAME structured-output contract, SAME deterministic validator)
with only the provider's own model/reasoning_effort constructor arguments
changed. Never touches core.config, .env, or any production default — the
model override lives ENTIRELY in this script's own
OpenAISchemaSelectionProvider(...) call, exactly the "evaluation-only
environment override" this step calls for.

Live-call footprint: exactly ONE OpenAIEmbeddingProvider.embed_batch() call
(inside build_schema_frontier) and exactly ONE
OpenAISchemaSelectionProvider.select_schema() call. No fallback to any other
model on failure — per this step's own Part 1 instruction, an unavailable
GPT-6 Sol must stop and report the sanitized provider error, never silently
substitute a different model.

No SQL generation, review, execution, or answer-generation call anywhere in
this file (see its own import list). No database write of any kind.

Never logs or saves: API keys, credentials, connection strings, prompts, raw
provider responses, or chain-of-thought. Records only: requested model,
actual response model, latency, token usage (if the SDK response safely
exposes it), and validation status.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import (  # noqa: E402
    AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS,
    AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS,
    AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS,
)
from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.metadata_loader import STATUS_READY  # noqa: E402
from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build  # noqa: E402
from core.sqlservice.schema_frontier import STATUS_OK as FRONTIER_STATUS_OK  # noqa: E402
from core.sqlservice.schema_frontier import build_schema_frontier  # noqa: E402
from core.sqlservice.schema_selection_providers import (  # noqa: E402
    OpenAISchemaSelectionProvider,
    SchemaSelectionConfigurationError,
    SchemaSelectionProviderError,
)
from core.sqlservice.schema_selection_service import STATUS_OK as SELECTION_STATUS_OK  # noqa: E402
from core.sqlservice.schema_selection_service import (  # noqa: E402
    SchemaSelectionValidationError,
    select_schema_for_question,
)
from core.sqlservice.vector_index import clear_vector_index_cache  # noqa: E402

SOURCE_ID = 10004
OWNER_USER_ID = "28"
EXPECTED_BUILD_ID = 7

# One fixed, low-ambiguity question -- this script measures MODEL
# AVAILABILITY + structured-output/reasoning-effort compatibility at the
# pipeline's own real prompt size, not selection quality (Part 4's job).
PROBE_QUESTION = "How many students are there?"

TARGET_MODEL = "gpt-6-sol"
TARGET_REASONING_EFFORT = "medium"


def _sanitized_sdk_detail(exc: Exception) -> dict:
    """Extracts only the SDK's own status_code/type -- never the exception's
    message text (which core.sqlservice.schema_selection_providers already
    keeps generic, but the wrapped __cause__ underneath it is a raw openai
    SDK exception whose own str() this function never surfaces)."""
    cause = exc.__cause__
    return {
        "cause_type": type(cause).__name__ if cause is not None else None,
        "status_code": getattr(cause, "status_code", None),
    }


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, OWNER_USER_ID)
    assert build is not None, f"no READY retrieval build found for source_id={SOURCE_ID}"
    assert build.id == EXPECTED_BUILD_ID, f"expected latest READY build id={EXPECTED_BUILD_ID}, got {build.id}"
    assert build.status == STATUS_READY, f"expected build status={STATUS_READY!r}, got {build.status!r}"
    print(
        f"Preflight OK: source_id={SOURCE_ID} owner_user_id={OWNER_USER_ID} build_id={build.id} "
        f"status={build.status} schema_fingerprint={build.schema_fingerprint}"
    )
    print(f"Target selector: model={TARGET_MODEL!r} reasoning_effort={TARGET_REASONING_EFFORT!r}")
    print(
        f"Selection bounds (unchanged): max_objects={AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS} "
        f"max_output_tokens={AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS} "
        f"timeout_seconds={AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS}"
    )

    embedding_provider = OpenAIEmbeddingProvider()
    clear_vector_index_cache()

    frontier_result = build_schema_frontier(PROBE_QUESTION, build.id, SOURCE_ID, OWNER_USER_ID, embedding_provider)
    assert frontier_result.status == FRONTIER_STATUS_OK and frontier_result.candidates, (
        f"frontier build failed or empty: status={frontier_result.status!r} error={frontier_result.error!r}"
    )
    print(f"Frontier OK: {len(frontier_result.candidates)} candidates for the probe question\n")

    provider = OpenAISchemaSelectionProvider(model=TARGET_MODEL, reasoning_effort=TARGET_REASONING_EFFORT)

    outcome: dict = {
        "source_id": SOURCE_ID, "owner_user_id": OWNER_USER_ID, "build_id": build.id,
        "schema_fingerprint": build.schema_fingerprint, "probe_question": PROBE_QUESTION,
        "frontier_size": len(frontier_result.candidates),
        "requested_model": TARGET_MODEL, "requested_reasoning_effort": TARGET_REASONING_EFFORT,
    }

    t0 = time.perf_counter()
    try:
        result = select_schema_for_question(
            PROBE_QUESTION, frontier_result, provider,
            max_objects=AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS,
            max_output_tokens=AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS,
            timeout_seconds=AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 -- classified and reported below, never re-raised raw
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        outcome["success"] = False
        outcome["latency_ms"] = latency_ms
        outcome["exception_type"] = type(exc).__name__
        outcome["sanitized_message"] = str(exc)
        outcome["sdk_detail"] = _sanitized_sdk_detail(exc) if isinstance(exc, SchemaSelectionProviderError) else None
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print(f"\n=== GPT-6 Sol availability: UNAVAILABLE ===")
        print(json.dumps(outcome, indent=2, default=str))
        if not isinstance(
            exc, (SchemaSelectionProviderError, SchemaSelectionConfigurationError, SchemaSelectionValidationError)
        ):
            raise
        return

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    outcome["success"] = result.status == SELECTION_STATUS_OK
    outcome["latency_ms"] = latency_ms
    outcome["actual_model"] = result.model
    outcome["validation_status"] = result.status
    outcome["selector_status"] = result.candidate.response_status if result.candidate else None
    outcome["token_usage"] = result.usage
    outcome["structured_output_valid"] = result.candidate is not None

    print(
        f"PASS: requested={TARGET_MODEL} actual={result.model} latency_ms={latency_ms} "
        f"validation_status={result.status} selector_status={outcome['selector_status']} "
        f"token_usage={result.usage}"
    )
    print(f"\n=== GPT-6 Sol availability: AVAILABLE ===")
    print(json.dumps(outcome, indent=2, default=str))


if __name__ == "__main__":
    main()
