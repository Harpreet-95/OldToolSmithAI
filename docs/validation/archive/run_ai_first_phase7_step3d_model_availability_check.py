"""
Phase 7, Step 3D, Part 5 — controlled model-availability check.

Read-only, ONE bounded schema-selection call against the real, already-READY
CCPP retrieval build (source_id=10004, owner user_id="28", build_id=7),
using the stronger reasoning model requested for this step
(AI_FIRST_SCHEMA_SELECTION_MODEL / AI_FIRST_SCHEMA_SELECTION_REASONING_
EFFORT, e.g. gpt-6-astra at reasoning effort "medium") — never the
production default model. Every rollout flag stays at its existing default
(this script imports nothing that reads or flips one); no route or
frontend code is touched or exercised.

Live-call footprint: exactly ONE OpenAIEmbeddingProvider.embed_batch() call
(inside build_schema_frontier, building one compact frontier for one fixed
question) and exactly ONE OpenAISchemaSelectionProvider.select_schema()
call. If that call fails with a model-not-found/permission-denied error,
this script makes exactly ONE further select_schema() call (same frontier,
same question, zero re-embedding) against the documented fallback model —
never more than that, never a silent credential change, never a retry of
Astra itself.

No SQL generation, review, execution, or answer-generation call anywhere in
this file (see its own import list — the structural proof of what it
cannot call). No database write of any kind.

Never logs or saves: API keys, credentials, connection strings, prompts,
raw provider responses, or chain-of-thought. Records only: requested model,
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
    AI_FIRST_SCHEMA_SELECTION_MODEL,
    AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT,
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

# One fixed, low-ambiguity question — this script measures MODEL
# AVAILABILITY, not selection quality (that is Part 6's job).
PROBE_QUESTION = "How many students are there?"

FALLBACK_MODEL = "gpt-6-sol"


def _model_unavailable(exc: Exception) -> bool:
    """True only for a model-not-found/permission-denied outcome — never for
    a generic provider failure (timeout, rate limit, malformed response,
    ...), which must stop and be diagnosed instead of triggering a
    fallback. Inspects the wrapped SDK exception's own status_code (never
    the sanitized message text, which core.sqlservice.
    schema_selection_providers deliberately keeps generic)."""
    cause = exc.__cause__
    status_code = getattr(cause, "status_code", None)
    return status_code in (404, 403)


def _attempt(model: str, frontier_result, embedding_provider) -> dict:
    provider = OpenAISchemaSelectionProvider(
        model=model, reasoning_effort=AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT,
    )
    t0 = time.perf_counter()
    record: dict = {"requested_model": model, "reasoning_effort": AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT}
    try:
        result = select_schema_for_question(
            PROBE_QUESTION, frontier_result, provider,
            max_objects=AI_FIRST_SCHEMA_SELECTION_MAX_OBJECTS,
            max_output_tokens=AI_FIRST_SCHEMA_SELECTION_MAX_OUTPUT_TOKENS,
            timeout_seconds=AI_FIRST_SCHEMA_SELECTION_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 -- classified below, never re-raised raw
        record["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        record["exception_type"] = type(exc).__name__
        record["model_unavailable"] = isinstance(exc, SchemaSelectionProviderError) and _model_unavailable(exc)
        record["success"] = False
        record["_exception"] = exc
        return record

    record["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    record["actual_model"] = result.model
    record["validation_status"] = result.status
    record["selector_status"] = result.candidate.response_status if result.candidate else None
    record["success"] = result.status == SELECTION_STATUS_OK
    record["_exception"] = None
    return record


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, OWNER_USER_ID)
    assert build is not None, f"no READY retrieval build found for source_id={SOURCE_ID}"
    assert build.id == EXPECTED_BUILD_ID, f"expected latest READY build id={EXPECTED_BUILD_ID}, got {build.id}"
    assert build.status == STATUS_READY, f"expected build status={STATUS_READY!r}, got {build.status!r}"
    print(
        f"Preflight OK: source_id={SOURCE_ID} owner_user_id={OWNER_USER_ID} build_id={build.id} "
        f"status={build.status} schema_fingerprint={build.schema_fingerprint}"
    )
    print(
        f"Configured selector: model={AI_FIRST_SCHEMA_SELECTION_MODEL!r} "
        f"reasoning_effort={AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT!r}"
    )

    embedding_provider = OpenAIEmbeddingProvider()
    clear_vector_index_cache()

    frontier_result = build_schema_frontier(PROBE_QUESTION, build.id, SOURCE_ID, OWNER_USER_ID, embedding_provider)
    assert frontier_result.status == FRONTIER_STATUS_OK and frontier_result.candidates, (
        f"frontier build failed or empty: status={frontier_result.status!r} error={frontier_result.error!r}"
    )
    print(f"Frontier OK: {len(frontier_result.candidates)} candidates for the probe question\n")

    astra_model = AI_FIRST_SCHEMA_SELECTION_MODEL
    print(f"--- Attempt 1: requested_model={astra_model!r} ---")
    astra_attempt = _attempt(astra_model, frontier_result, embedding_provider)
    astra_exc = astra_attempt.pop("_exception")

    outcome: dict = {
        "source_id": SOURCE_ID, "owner_user_id": OWNER_USER_ID, "build_id": build.id,
        "schema_fingerprint": build.schema_fingerprint, "probe_question": PROBE_QUESTION,
        "frontier_size": len(frontier_result.candidates),
        "attempts": [astra_attempt],
    }

    if astra_attempt["success"]:
        print(
            f"PASS: requested={astra_attempt['requested_model']} actual={astra_attempt['actual_model']} "
            f"latency_ms={astra_attempt['latency_ms']} validation_status={astra_attempt['validation_status']}"
        )
        outcome["decision"] = "PRIMARY_MODEL_AVAILABLE"
    elif astra_attempt.get("model_unavailable"):
        print(
            f"Astra unavailable ({astra_attempt['exception_type']}, model-not-found/permission-denied). "
            f"Attempting documented fallback once: {FALLBACK_MODEL!r} (no credential change)."
        )
        print(f"--- Attempt 2 (fallback): requested_model={FALLBACK_MODEL!r} ---")
        sol_attempt = _attempt(FALLBACK_MODEL, frontier_result, embedding_provider)
        sol_exc = sol_attempt.pop("_exception")
        outcome["attempts"].append(sol_attempt)
        if sol_attempt["success"]:
            print(
                f"FALLBACK PASS: requested={sol_attempt['requested_model']} actual={sol_attempt['actual_model']} "
                f"latency_ms={sol_attempt['latency_ms']} validation_status={sol_attempt['validation_status']}"
            )
            outcome["decision"] = "FALLBACK_MODEL_AVAILABLE"
        else:
            print(f"FALLBACK ALSO FAILED: {sol_attempt['exception_type']}")
            outcome["decision"] = "NEITHER_MODEL_AVAILABLE"
            if sol_exc is not None and not isinstance(
                sol_exc, (SchemaSelectionProviderError, SchemaSelectionConfigurationError, SchemaSelectionValidationError)
            ):
                raise sol_exc
    else:
        print(
            f"STOPPING: {astra_attempt['exception_type']} is not a model-not-found/permission-denied outcome -- "
            "diagnose safely rather than attempting the fallback."
        )
        outcome["decision"] = "OTHER_PROVIDER_FAILURE_STOP"
        if astra_exc is not None and not isinstance(
            astra_exc, (SchemaSelectionProviderError, SchemaSelectionConfigurationError, SchemaSelectionValidationError)
        ):
            raise astra_exc

    print(f"\n=== Decision: {outcome['decision']} ===")
    print(json.dumps(outcome, indent=2, default=str))


if __name__ == "__main__":
    main()
