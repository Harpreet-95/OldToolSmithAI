"""
SQL Architecture Correction — Phase 6, Step 3D: controlled full-evidence
schema-linker and join-planner evaluation.

Read-only evaluation ONLY. Never modifies core.sqlservice.schema_linker or
core.sqlservice.join_planner ranking/admission/planning behavior — this
script only supplies evidence inputs those two pure, already-corrected
modules already accept (semantic_rankings / lexical_rankings /
retrieved_examples), exactly per their own public contracts.

Four ablation modes, run question -> semantic requirements -> relevance-
qualified anchors -> connected-subgraph plan, for each of the 15 exact
established CCPP_GOLDEN_CASES (docs/validation/ccpp_golden_cases_v2.py):

  A. Catalog evidence only
  B. Catalog + lexical
  C. Catalog + lexical + vector
  D. Catalog + lexical + vector + trusted examples

Exactly ONE real question-embedding call is made per question (15 total,
one per case) — the SAME query vector is reused for both vector schema
ranking (search_vector_index) and trusted SQL-example retrieval
(retrieve_sql_examples); modes C and D never call embed_batch a second
time for the same question, and modes A/B never call it at all.

Zero database writes anywhere in this script: every retrieval_store/
embedding_store/sql_example_store/sql_example_vector_index function used
here is a read-only SELECT-based accessor (see their own docstrings).
Zero SQL Server calls. Zero SQL-generation/execution/answer-generation
calls of any kind.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_VALIDATION_DIR = Path(__file__).resolve().parent
if str(_VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_VALIDATION_DIR))

from ccpp_golden_cases_v2 import CCPP_GOLDEN_CASES  # noqa: E402

from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider  # noqa: E402
from core.sqlservice.embedding_store import get_embedding_build_completeness  # noqa: E402
from core.sqlservice.join_planner import (  # noqa: E402
    JOIN_CAPABILITY_LINEAGE_ONLY,
    PLAN_KIND_AMBIGUOUS,
    PLAN_KIND_JOINED_SUBGRAPH,
    PLAN_KIND_PARTIAL,
    PLAN_KIND_SINGLE_OBJECT,
    clear_join_planner_cache,
    plan_schema_join_subgraph,
)
from core.sqlservice.lexical_index import (  # noqa: E402
    LexicalIndexIdentity,
    load_lexical_index,
    search_lexical_index,
    tokenize,
)
from core.sqlservice.retrieval_store import (  # noqa: E402
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.schema_linker import (  # noqa: E402
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    STRENGTH_SUPPORTING,
    STRENGTH_UNRESOLVED,
    clear_schema_linker_cache,
    link_question_to_schema,
)
from core.sqlservice.semantic_catalog import build_semantic_catalog  # noqa: E402
from core.sqlservice.sql_example_model import TRUST_LEVEL_TRUSTED  # noqa: E402
from core.sqlservice.sql_example_retrieval import retrieve_sql_examples  # noqa: E402
from core.sqlservice.sql_example_store import list_examples_for_source  # noqa: E402
from core.sqlservice.sql_example_vector_index import load_sql_example_vector_index  # noqa: E402
from core.sqlservice.vector_index import (  # noqa: E402
    STATUS_OK as VECTOR_STATUS_OK,
    clear_vector_index_cache,
    load_vector_index,
    search_vector_index,
)

SOURCE_ID = 10004
USER_ID = "28"
ALLOWED_TIERS = frozenset({1, 2})

MODES = ("A_CATALOG_ONLY", "B_CATALOG_LEXICAL", "C_CATALOG_LEXICAL_VECTOR", "D_FULL_EVIDENCE")

_RESULTS_PATH = _PROJECT_ROOT / "docs" / "validation" / "results" / "phase6_step3d_full_evidence_evaluation.json"


# ── Safety preflight: prove the new architecture has zero production wiring ─
#
# schema_linker.py / join_planner.py carry no rollout flag of their own
# (Step 3/3A/3B/3C never wired them into any live route or service) — the
# strongest form of "rollout flags remain disabled" is that NO import of
# either module exists outside tests/docs/core.sqlservice itself. Verified
# here by direct source scan, not by trusting a flag default.
def _verify_zero_production_wiring() -> list[str]:
    offending: list[str] = []
    skip_dirs = {"tests", "docs", ".git", "venv", "node_modules", "__pycache__"}
    for py_file in _PROJECT_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_PROJECT_ROOT)
        parts = rel.parts
        if parts[0] in skip_dirs:
            continue
        if parts[:2] == ("core", "sqlservice") and parts[-1] in ("schema_linker.py", "join_planner.py"):
            continue
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        if "schema_linker import" in text or "join_planner import" in text:
            offending.append(str(rel))
    return offending


def run_preflight() -> dict:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        raise SystemExit(f"PREFLIGHT FAILED: no READY build for source_id={SOURCE_ID} user_id={USER_ID!r}")

    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    if not documents:
        raise SystemExit(f"PREFLIGHT FAILED: build {build.id} returned zero documents")

    provider = OpenAIEmbeddingProvider()

    completeness = get_embedding_build_completeness(
        build.id, SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
    )
    if completeness.status != "OK" or not completeness.is_complete:
        raise SystemExit(
            f"PREFLIGHT FAILED: schema embedding build incomplete — status={completeness.status} "
            f"embedded={completeness.embedded_document_count}/{completeness.total_documents} "
            f"missing={completeness.missing_document_hashes[:5]!r}"
        )

    example_index_load = load_sql_example_vector_index(
        SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
    )
    trusted_active_examples = list_examples_for_source(SOURCE_ID, USER_ID, trust_level=TRUST_LEVEL_TRUSTED)

    offending_imports = _verify_zero_production_wiring()

    preflight = {
        "source_id": SOURCE_ID,
        "user_id": USER_ID,
        "build_id": build.id,
        "build_status": build.status,
        "schema_fingerprint": build.schema_fingerprint,
        "document_count_in_build": build.document_count,
        "documents_loaded": len(documents),
        "embedding_provider": provider.provider_id,
        "embedding_model": provider.model,
        "embedding_dimensions": provider.dimensions,
        "schema_embedding_completeness_status": completeness.status,
        "schema_embedding_is_complete": completeness.is_complete,
        "schema_embedding_total_documents": completeness.total_documents,
        "schema_embedding_embedded_count": completeness.embedded_document_count,
        "example_vector_index_status": example_index_load.status,
        "example_vector_index_count": example_index_load.example_count,
        "trusted_active_example_count_in_store": len(trusted_active_examples),
        "example_index_completeness": (
            f"{example_index_load.example_count}/{len(trusted_active_examples)} trusted+active examples embedded"
        ),
        "production_wiring_scan_offending_files": offending_imports,
        "rollout_flags_disabled": len(offending_imports) == 0,
    }

    if offending_imports:
        raise SystemExit(f"DECISION GATE: schema_linker/join_planner imported outside tests/docs: {offending_imports}")

    return {"preflight": preflight, "build": build, "documents": documents, "provider": provider}


# ── Per-question evidence construction (exactly one embed_batch per Q) ─────


class _CountingProvider:
    """Wraps a real EmbeddingProvider to prove exactly one embed_batch call
    is made per question — never bypasses or duplicates the real provider,
    only counts."""

    def __init__(self, inner):
        self._inner = inner
        self.call_count = 0
        self.call_log: list[str] = []

    @property
    def provider_id(self):
        return self._inner.provider_id

    @property
    def model(self):
        return self._inner.model

    @property
    def dimensions(self):
        return self._inner.dimensions

    def embed_batch(self, texts):
        self.call_count += 1
        self.call_log.append(f"call#{self.call_count}: {len(texts)} text(s)")
        return self._inner.embed_batch(texts)


def _lexical_rankings_for(question: str, documents, build_id: int, provider) -> list:
    identity = LexicalIndexIdentity(
        build_id=build_id, embedding_provider=provider.provider_id,
        embedding_model=provider.model, embedding_dimensions=provider.dimensions,
    )
    lexical_index = load_lexical_index(documents, identity)
    tokens = tokenize(question)
    if not tokens:
        return []
    ranked = search_lexical_index(lexical_index, tokens, top_k=lexical_index.document_count or 1)
    return [r for r in ranked if r.retrieval_tier in ALLOWED_TIERS]


def _vector_rankings_for(query_vector, vector_index) -> list:
    ranked = search_vector_index(vector_index, query_vector, top_k=len(vector_index.documents) or 1)
    return [r for r in ranked if r.retrieval_tier in ALLOWED_TIERS]


def _examples_for(query_vector, documents_by_fqn_lower, provider) -> tuple:
    result = retrieve_sql_examples(
        query_vector, SOURCE_ID, USER_ID, provider.provider_id, provider.model, provider.dimensions,
        documents_by_fqn_lower, allowed_tiers=ALLOWED_TIERS,
    )
    return result.examples if result.status == "OK" else ()


# ── Freezing one link+plan run into a plain, comparable dict ───────────────


def _requirement_dict(req) -> dict:
    return {
        "concept_kind": req.concept_kind,
        "concept_key": req.concept_key,
        "normalized_label": req.normalized_label,
        "matched_terms": list(req.matched_terms),
        "strength": req.strength,
        "ambiguous": req.ambiguous,
        "confidence": round(req.confidence, 4),
        "candidate_total_count": req.candidate_total_count,
        "top_candidates": [
            {"object_fqn": c.object_fqn, "column_name": c.column_name, "match_kind": c.match_kind,
             "confidence": round(c.confidence, 4), "informativeness": round(c.informativeness, 4)}
            for c in req.candidates[:5]
        ],
    }


def _anchor_dict(anchor, semantic_rank_by_key, lexical_rank_by_key, semantic_score_by_key, lexical_score_by_key) -> dict:
    key = anchor.object_fqn.lower()
    return {
        "object_fqn": anchor.object_fqn,
        "retrieval_tier": anchor.retrieval_tier,
        "combined_score": round(anchor.combined_score, 4),
        "catalog_score": round(anchor.catalog_score, 4),
        "semantic_score": round(anchor.semantic_score, 4) if anchor.semantic_score is not None else None,
        "lexical_score": round(anchor.lexical_score, 4) if anchor.lexical_score is not None else None,
        "example_score": round(anchor.example_score, 4),
        "canonical_score": round(anchor.canonical_score, 4),
        "vector_rank": semantic_rank_by_key.get(key),
        "lexical_rank": lexical_rank_by_key.get(key),
        "admission_reasons": list(anchor.admission_reasons),
        "supporting_concepts": list(anchor.supporting_concepts),
    }


def _plan_dict(plan) -> dict | None:
    if plan is None:
        return None
    return {
        "selected_object_fqns": list(plan.selected_object_fqns),
        "plan_kind": plan.plan_kind,
        "join_edges": [
            {"from": e.from_object_fqn, "to": e.to_object_fqn, "column_pairs": list(e.column_pairs),
             "join_capability": e.join_capability, "direction": e.direction, "relationship_type": e.relationship_type}
            for e in plan.join_edges
        ],
        "anchor_object_fqns": list(plan.anchor_object_fqns),
        "bridge_object_fqns": list(plan.bridge_object_fqns),
        "covered_requirement_keys": list(plan.covered_requirement_keys),
        "uncovered_requirement_keys": list(plan.uncovered_requirement_keys),
        "uncovered_requirement_reasons": [
            {"concept_key": r.concept_key, "role": r.role, "reason": r.reason, "detail": r.detail}
            for r in plan.uncovered_requirement_reasons
        ],
        "score": round(plan.score, 6),
        "confidence": round(plan.confidence, 4),
        "warnings": list(plan.warnings),
    }


def run_one(case, mode, ctx) -> dict:
    documents = ctx["documents"]
    build = ctx["build"]
    catalog = ctx["catalog"]
    provider = ctx["provider"]
    documents_by_fqn_lower = ctx["documents_by_fqn_lower"]
    vector_index = ctx.get("vector_index")

    semantic_rankings: list = []
    lexical_rankings: list = []
    retrieved_examples: tuple = ()
    query_vector = None

    if mode in ("B_CATALOG_LEXICAL", "C_CATALOG_LEXICAL_VECTOR", "D_FULL_EVIDENCE"):
        lexical_rankings = _lexical_rankings_for(case.question, documents, build.id, provider)

    if mode in ("C_CATALOG_LEXICAL_VECTOR", "D_FULL_EVIDENCE"):
        query_vector = ctx["query_vectors"][case.case_id]
        semantic_rankings = _vector_rankings_for(query_vector, vector_index)

    if mode == "D_FULL_EVIDENCE":
        retrieved_examples = _examples_for(query_vector, documents_by_fqn_lower, provider)

    semantic_rank_by_key = {r.object_fqn.lower(): i + 1 for i, r in enumerate(semantic_rankings)}
    semantic_score_by_key = {r.object_fqn.lower(): r.score for r in semantic_rankings}
    lexical_rank_by_key = {r.object_fqn.lower(): i + 1 for i, r in enumerate(lexical_rankings)}
    lexical_score_by_key = {r.object_fqn.lower(): r.score for r in lexical_rankings}

    clear_schema_linker_cache()
    clear_join_planner_cache()

    t0 = time.perf_counter()
    link = link_question_to_schema(
        case.question, catalog, documents,
        semantic_rankings=semantic_rankings, lexical_rankings=lexical_rankings, retrieved_examples=retrieved_examples,
    )
    linker_latency_ms = (time.perf_counter() - t0) * 1000.0

    plan_result = None
    planner_latency_ms = 0.0
    if link.status == "OK":
        t1 = time.perf_counter()
        plan_result = plan_schema_join_subgraph(link, documents)
        planner_latency_ms = (time.perf_counter() - t1) * 1000.0

    all_requirements = (
        link.requirements.entity_concepts + link.requirements.measure_concepts
        + link.requirements.dimension_concepts + link.requirements.date_role_concepts
        + link.requirements.identifier_concepts
    ) if link.status == "OK" else ()

    strength_counts = {
        STRENGTH_CORE: sum(1 for r in all_requirements if r.strength == STRENGTH_CORE),
        STRENGTH_CONSTRAINT: sum(1 for r in all_requirements if r.strength == STRENGTH_CONSTRAINT),
        STRENGTH_SUPPORTING: sum(1 for r in all_requirements if r.strength == STRENGTH_SUPPORTING),
        STRENGTH_UNRESOLVED: sum(1 for r in all_requirements if r.strength == STRENGTH_UNRESOLVED),
    }

    tier_leakage = [a.object_fqn for a in (link.anchors if link.status == "OK" else ()) if a.retrieval_tier not in ALLOWED_TIERS]
    fake_lineage_joins = []
    if plan_result is not None:
        for plan in (plan_result.primary_plan, *plan_result.alternative_plans):
            if plan is None:
                continue
            for edge in plan.join_edges:
                if edge.join_capability == JOIN_CAPABILITY_LINEAGE_ONLY:
                    fake_lineage_joins.append({"from": edge.from_object_fqn, "to": edge.to_object_fqn})

    # FROZEN OUTPUT — everything above is computed with zero reference to
    # case.required_object_fqns/forbidden_object_fqns. Grading (which reads
    # those) happens strictly AFTER this dict is built, in grade_case().
    frozen = {
        "case_id": case.case_id,
        "mode": mode,
        "question": case.question,
        "linker_status": link.status,
        "requirements": [_requirement_dict(r) for r in all_requirements],
        "strength_counts": strength_counts,
        "raw_requirement_count": len(all_requirements),
        "anchors": [
            _anchor_dict(a, semantic_rank_by_key, lexical_rank_by_key, semantic_score_by_key, lexical_score_by_key)
            for a in (link.anchors if link.status == "OK" else ())
        ],
        "anchor_count": len(link.anchors) if link.status == "OK" else 0,
        "ambiguities": [
            {"kind": amb.kind, "candidate_refs": list(amb.candidate_refs)}
            for amb in (link.ambiguities if link.status == "OK" else ())
        ],
        "vector_ranked": [{"object_fqn": r.object_fqn, "score": round(r.score, 4)} for r in semantic_rankings[:15]],
        "lexical_ranked": [{"object_fqn": r.object_fqn, "score": round(r.score, 4)} for r in lexical_rankings[:15]],
        "retrieved_examples": [
            {"example_id": ex.example.example_id, "similarity": round(ex.similarity, 4),
             "result_shape": ex.example.result_shape, "question_pattern": ex.example.question_pattern,
             "proposed_object_fqns": list(ex.proposed_object_fqns), "unavailable_object_fqns": list(ex.unavailable_object_fqns)}
            for ex in retrieved_examples
        ],
        "planner_status": plan_result.status if plan_result is not None else None,
        "primary_plan": _plan_dict(plan_result.primary_plan) if plan_result is not None else None,
        "alternative_plans": (
            [_plan_dict(p) for p in plan_result.alternative_plans] if plan_result is not None else []
        ),
        "ambiguity_records": (
            [{"concept_key": r.concept_key, "primary_object_fqn": r.primary_object_fqn,
              "alternative_object_fqn": r.alternative_object_fqn} for r in plan_result.ambiguity_records]
            if plan_result is not None else []
        ),
        "limits_reached": list(plan_result.limits_reached) if plan_result is not None else [],
        "considered_candidate_count": plan_result.considered_candidate_count if plan_result is not None else 0,
        "tier_3_4_leakage": tier_leakage,
        "fake_lineage_joins": fake_lineage_joins,
        "linker_latency_ms": round(linker_latency_ms, 3),
        "planner_latency_ms": round(planner_latency_ms, 3),
    }
    return frozen


def grade_case(frozen: dict, case) -> dict:
    """Strictly AFTER freezing — reads case.required_object_fqns/forbidden_
    object_fqns to compute coverage/leakage verdicts. Never mutates frozen."""
    required = set(case.required_object_fqns)
    forbidden = set(case.forbidden_object_fqns)

    primary = frozen["primary_plan"]
    primary_selected = set(primary["selected_object_fqns"]) if primary else set()
    alt_selected_list = [set(p["selected_object_fqns"]) for p in frozen["alternative_plans"]]

    primary_complete = required.issubset(primary_selected) if required else None
    any_alt_complete = any(required.issubset(s) for s in alt_selected_list) if required else False
    connected_complete = bool(primary_complete) or any_alt_complete

    anchor_fqns = {a["object_fqn"] for a in frozen["anchors"]}
    required_present_as_anchor = required & anchor_fqns
    required_in_primary_as_anchor = required & set(primary["anchor_object_fqns"]) if primary else set()
    required_in_primary_as_bridge = required & set(primary["bridge_object_fqns"]) if primary else set()

    forbidden_in_primary = forbidden & primary_selected
    forbidden_in_any_alt = forbidden & set().union(*alt_selected_list) if alt_selected_list else set()
    forbidden_in_anchors = forbidden & anchor_fqns

    return {
        "case_id": case.case_id,
        "mode": frozen["mode"],
        "category": case.category,
        "expects_clarification": case.expects_clarification,
        "required_object_fqns": sorted(required),
        "forbidden_object_fqns": sorted(forbidden),
        "required_present_as_anchor": sorted(required_present_as_anchor),
        "required_missing_from_anchors": sorted(required - anchor_fqns),
        "required_in_primary_as_anchor": sorted(required_in_primary_as_anchor),
        "required_in_primary_as_bridge": sorted(required_in_primary_as_bridge),
        "primary_plan_complete": primary_complete,
        "any_alternative_plan_complete": any_alt_complete,
        "connected_complete_coverage": connected_complete,
        "required_covered_count": len(required & primary_selected),
        "required_total_count": len(required),
        "forbidden_leakage_primary": sorted(forbidden_in_primary),
        "forbidden_leakage_alternatives": sorted(forbidden_in_any_alt),
        "forbidden_leakage_anchors": sorted(forbidden_in_anchors),
    }


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
    return round(values[idx], 3)


def main() -> None:
    ctx = run_preflight()
    print("=== PREFLIGHT ===")
    print(json.dumps(ctx["preflight"], indent=2, default=str))

    documents = ctx["documents"]
    build = ctx["build"]
    catalog = build_semantic_catalog(documents)
    ctx["catalog"] = catalog
    ctx["documents_by_fqn_lower"] = {d.object_fqn.lower(): d for d in documents}

    real_provider = ctx["provider"]
    counting_provider = _CountingProvider(real_provider)
    ctx["provider"] = counting_provider

    clear_vector_index_cache()
    load_result = load_vector_index(
        build.id, SOURCE_ID, USER_ID, real_provider.provider_id, real_provider.model, real_provider.dimensions,
    )
    if load_result.status != VECTOR_STATUS_OK:
        raise SystemExit(f"DECISION GATE: vector index load failed with status={load_result.status}")
    ctx["vector_index"] = load_result.index
    print(f"\nVector index loaded: {len(load_result.index.documents)} documents, status={load_result.status}")

    # ── Exactly ONE embed_batch call per question, up front, reused by
    # both mode C (vector schema ranking) and mode D (trusted example
    # retrieval) — see _vector_rankings_for/_examples_for above, neither of
    # which ever calls embed_batch itself.
    query_vectors: dict[str, tuple] = {}
    print(f"\n=== Embedding {len(CCPP_GOLDEN_CASES)} questions (one call each) ===")
    for case in CCPP_GOLDEN_CASES:
        response = counting_provider.embed_batch([case.question])
        vector = tuple(float(x) for x in response.items[0].vector)
        query_vectors[case.case_id] = vector
        print(f"  {case.case_id}: embedded ({len(vector)} dims)")
    ctx["query_vectors"] = query_vectors

    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(
            f"DECISION GATE: expected exactly {len(CCPP_GOLDEN_CASES)} embed_batch calls, "
            f"got {counting_provider.call_count}"
        )

    per_case_mode: dict[str, dict] = {}
    determinism_mismatches: list[str] = []

    _NON_DETERMINISTIC_FIELDS = ("linker_latency_ms", "planner_latency_ms")

    def _determinism_projection(frozen: dict) -> str:
        # Wall-clock latency is expected to vary run-to-run even when every
        # OTHER field is byte-identical — excluded from the determinism
        # comparison only; both raw latencies are still stored/aggregated.
        projected = {k: v for k, v in frozen.items() if k not in _NON_DETERMINISTIC_FIELDS}
        return json.dumps(projected, sort_keys=True)

    for mode in MODES:
        print(f"\n=== MODE {mode} ===")
        for case in CCPP_GOLDEN_CASES:
            frozen_1 = run_one(case, mode, ctx)
            frozen_2 = run_one(case, mode, ctx)
            s1 = _determinism_projection(frozen_1)
            s2 = _determinism_projection(frozen_2)
            if s1 != s2:
                determinism_mismatches.append(f"{case.case_id}/{mode}")
            per_case_mode[f"{case.case_id}/{mode}"] = frozen_1
            grade = grade_case(frozen_1, case)
            print(
                f"  {case.case_id}: status={frozen_1['linker_status']}/{frozen_1['planner_status']} "
                f"plan_kind={(frozen_1['primary_plan'] or {}).get('plan_kind')} "
                f"required={grade['required_covered_count']}/{grade['required_total_count']} "
                f"connected_complete={grade['connected_complete_coverage']}"
            )

    if counting_provider.call_count != len(CCPP_GOLDEN_CASES):
        raise SystemExit(
            f"DECISION GATE: embed_batch call count changed during evaluation — "
            f"expected {len(CCPP_GOLDEN_CASES)}, now {counting_provider.call_count}"
        )

    non_clarification_cases = [c for c in CCPP_GOLDEN_CASES if not c.expects_clarification]
    assert len(non_clarification_cases) == 14, f"expected 14 non-clarification cases, got {len(non_clarification_cases)}"
    total_required = sum(len(c.required_object_fqns) for c in non_clarification_cases)
    assert total_required == 24, f"expected 24 total required objects, got {total_required}"

    aggregates: dict[str, dict] = {}
    all_leakage: list[str] = []
    all_fake_lineage: list[str] = []

    for mode in MODES:
        grades = [grade_case(per_case_mode[f"{c.case_id}/{mode}"], c) for c in non_clarification_cases]
        frozen_list = [per_case_mode[f"{c.case_id}/{mode}"] for c in non_clarification_cases]

        required_covered_total = sum(g["required_covered_count"] for g in grades)
        primary_complete_count = sum(1 for g in grades if g["primary_plan_complete"])
        connected_complete_count = sum(1 for g in grades if g["connected_complete_coverage"])

        plan_kinds = [
            (f["primary_plan"] or {}).get("plan_kind") for f in frozen_list
        ]
        plan_kind_counts = {
            "SINGLE_OBJECT": plan_kinds.count(PLAN_KIND_SINGLE_OBJECT),
            "JOINED_SUBGRAPH": plan_kinds.count(PLAN_KIND_JOINED_SUBGRAPH),
            "PARTIAL": plan_kinds.count(PLAN_KIND_PARTIAL),
            "AMBIGUOUS": plan_kinds.count(PLAN_KIND_AMBIGUOUS),
            "NONE": plan_kinds.count(None),
        }

        anchor_counts = [f["anchor_count"] for f in frozen_list]
        selected_counts = [len((f["primary_plan"] or {}).get("selected_object_fqns", [])) for f in frozen_list]

        linker_latencies = [f["linker_latency_ms"] for f in frozen_list]
        planner_latencies = [f["planner_latency_ms"] for f in frozen_list]
        combined_latencies = [l + p for l, p in zip(linker_latencies, planner_latencies)]

        tier_leaks = [c.case_id for c, f in zip(non_clarification_cases, frozen_list) if f["tier_3_4_leakage"]]
        fake_lineage = [c.case_id for c, f in zip(non_clarification_cases, frozen_list) if f["fake_lineage_joins"]]
        forbidden_leaks = [
            c.case_id for c, g in zip(non_clarification_cases, grades)
            if g["forbidden_leakage_primary"] or g["forbidden_leakage_alternatives"]
        ]
        all_leakage.extend(tier_leaks)
        all_fake_lineage.extend(fake_lineage)

        improved_vs_prev = []
        regressed_vs_prev = []
        mode_index = MODES.index(mode)
        if mode_index > 0:
            prev_mode = MODES[mode_index - 1]
            for c in non_clarification_cases:
                prev_grade = grade_case(per_case_mode[f"{c.case_id}/{prev_mode}"], c)
                cur_grade = grade_case(per_case_mode[f"{c.case_id}/{mode}"], c)
                if not prev_grade["primary_plan_complete"] and cur_grade["primary_plan_complete"]:
                    improved_vs_prev.append(c.case_id)
                elif prev_grade["primary_plan_complete"] and not cur_grade["primary_plan_complete"]:
                    regressed_vs_prev.append(c.case_id)

        terminal_recovered = sum(len(g["required_in_primary_as_anchor"]) for g in grades)
        bridge_recovered = sum(len(g["required_in_primary_as_bridge"]) for g in grades)

        aggregates[mode] = {
            "required_object_coverage": f"{required_covered_total}/24",
            "primary_complete_case_coverage": f"{primary_complete_count}/14",
            "connected_complete_coverage": f"{connected_complete_count}/14",
            "cases_improved_vs_previous_mode": improved_vs_prev,
            "cases_regressed_vs_previous_mode": regressed_vs_prev,
            "plan_kind_distribution": plan_kind_counts,
            "anchor_count_distribution": {
                "min": min(anchor_counts) if anchor_counts else None,
                "median": statistics.median(anchor_counts) if anchor_counts else None,
                "max": max(anchor_counts) if anchor_counts else None,
            },
            "selected_object_distribution": {
                "min": min(selected_counts) if selected_counts else None,
                "median": statistics.median(selected_counts) if selected_counts else None,
                "max": max(selected_counts) if selected_counts else None,
            },
            "required_recovered_as_anchor_count": terminal_recovered,
            "required_recovered_as_bridge_only_count": bridge_recovered,
            "tier_3_4_leakage_cases": tier_leaks,
            "fake_lineage_join_cases": fake_lineage,
            "forbidden_object_leakage_cases": forbidden_leaks,
            "linker_latency_p50_ms": pct(linker_latencies, 0.50),
            "linker_latency_p90_ms": pct(linker_latencies, 0.90),
            "linker_latency_p99_ms": pct(linker_latencies, 0.99),
            "planner_latency_p50_ms": pct(planner_latencies, 0.50),
            "planner_latency_p90_ms": pct(planner_latencies, 0.90),
            "planner_latency_p99_ms": pct(planner_latencies, 0.99),
            "combined_latency_p50_ms": pct(combined_latencies, 0.50),
            "combined_latency_p90_ms": pct(combined_latencies, 0.90),
            "combined_latency_p99_ms": pct(combined_latencies, 0.99),
        }

    # ── CCPP2-009 / CCPP2-012 targeted diagnosis ────────────────────────────
    special_diagnosis = {}
    for case_id in ("CCPP2-009", "CCPP2-012"):
        case = next(c for c in CCPP_GOLDEN_CASES if c.case_id == case_id)
        rows = {}
        for mode in MODES:
            frozen = per_case_mode[f"{case_id}/{mode}"]
            grade = grade_case(frozen, case)
            rows[mode] = {
                "primary_selected": (frozen["primary_plan"] or {}).get("selected_object_fqns"),
                "plan_kind": (frozen["primary_plan"] or {}).get("plan_kind"),
                "required_covered": f"{grade['required_covered_count']}/{grade['required_total_count']}",
                "primary_plan_complete": grade["primary_plan_complete"],
                "anchor_fqns": [a["object_fqn"] for a in frozen["anchors"]],
            }
        special_diagnosis[case_id] = {
            "question": case.question,
            "required_object_fqns": sorted(case.required_object_fqns),
            "by_mode": rows,
        }

    result = {
        "preflight": ctx["preflight"],
        "provider_call_count": counting_provider.call_count,
        "provider_call_log": counting_provider.call_log,
        "determinism_mismatches": determinism_mismatches,
        "aggregates": aggregates,
        "special_diagnosis_ccpp2_009_012": special_diagnosis,
        "per_case_mode": per_case_mode,
    }

    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(result, indent=2, sort_keys=False), encoding="utf-8")

    print("\n=== AGGREGATES ===")
    print(json.dumps(aggregates, indent=2))
    print(f"\nprovider_call_count = {counting_provider.call_count} (expected {len(CCPP_GOLDEN_CASES)})")
    print(f"determinism_mismatches = {determinism_mismatches}")
    print(f"\nWrote full results to {_RESULTS_PATH}")

    if determinism_mismatches:
        raise SystemExit(f"DECISION GATE: nondeterminism detected: {determinism_mismatches}")
    if all_leakage:
        raise SystemExit(f"DECISION GATE: Tier 3/4 leakage detected: {all_leakage}")
    if all_fake_lineage:
        raise SystemExit(f"DECISION GATE: fake lineage join detected: {all_fake_lineage}")

    mode_d_primary_complete = int(aggregates["D_FULL_EVIDENCE"]["primary_complete_case_coverage"].split("/")[0])
    if mode_d_primary_complete < 7:
        raise SystemExit(
            f"DECISION GATE: mode D (full evidence) primary complete coverage {mode_d_primary_complete}/14 "
            f"is below the 7/14 floor — STOP and report."
        )


if __name__ == "__main__":
    main()
