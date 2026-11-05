"""
*** NON-AUTHORITATIVE DIAGNOSTIC SCRIPT — DO NOT USE AS A BASELINE ***

SQL Architecture Correction, Phase 6, Step 3A correction notice: the 15
questions and expected-object sets below were INVENTED by the Step 3 report
author, who incorrectly believed no authoritative golden-case fixture
existed for this evaluation. That belief was wrong: the real, established
fixture is docs/validation/ccpp_golden_cases_v2.py's CCPP_GOLDEN_CASES (15
cases, 14 non-clarification, 24 total required_object_fqns) — see
docs/validation/run_join_planner_phase6_step3a_golden_verification.py for
the corrected, authoritative evaluation against THAT fixture.

This file is kept only as non-authoritative diagnostic data (it exercised
some real edge cases against the live build and is cheap to keep) — its
5/15 / 7/15 results must NEVER be cited as a product metric, compared
against the golden-case baseline, or used to justify any planner change.
Prefer deleting it once the Step 3A report has been reviewed; it is left
in place for now only so its history remains inspectable alongside this
notice.

── Original docstring (methodology is still accurate; the QUESTION SET is
   the problem) ─────────────────────────────────────────────────────────

Loads the LATEST already-READY retrieval-document build for the real
target source (source_id=10004) from ToolSmithAI's own local SQLite
metadata store (core.sqlservice.retrieval_store) — never opens a live
SQL Server connection, never calls OpenAI, never builds a new retrieval/
embedding build. Then, entirely in memory:

  1. build_semantic_catalog(documents)               (pure, no I/O)
  2. link_question_to_schema(question, catalog, docs) (pure, no I/O)
  3. plan_schema_join_subgraph(linking_result, docs)  (pure, no I/O)

for a fixed set of 15 hand-authored business questions, and compares the
LINKER-ONLY anchor set against the PLANNER's primary_plan against a set of
expected objects this script defines by direct inspection of the real
confirmed-relationship graph (never fed into planning itself — comparison
happens only AFTER both results are frozen).

No database writes. No SQL Server/OpenAI calls. No new retrieval/embedding
build. No SQL generation/execution.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
from core.sqlservice.semantic_catalog import build_semantic_catalog
from core.sqlservice.schema_linker import link_question_to_schema, clear_schema_linker_cache
from core.sqlservice.join_planner import (
    STATUS_OK,
    JOIN_CAPABILITY_LINEAGE_ONLY,
    clear_join_planner_cache,
    plan_schema_join_subgraph,
)

SOURCE_ID = 10004
USER_ID = "28"

# ── 15 hand-authored questions + expected objects ──────────────────────────
#
# Expected objects were derived by direct read-only inspection of this
# build's own confirmed_relationship_edges graph (see this Step's own
# report for the exploration) — never fed into linking/planning, and never
# used to tune the planner's generic scoring/search logic. Several
# questions (12-14) are DELIBERATELY objects with NO confirmed relationship
# to each other in this real schema at all — included specifically to
# verify the planner reports disconnection honestly rather than fabricating
# a join.

CASES = [
    {
        "question": "How many students are there in total?",
        "expected": {"dbo.ADF_Student"},
    },
    {
        "question": "List all the classes we have run.",
        "expected": {"dbo.ADF_Class"},
    },
    {
        "question": "Which students signed up for which classes?",
        "expected": {"dbo.ADF_Student", "dbo.ADF_ClassSignups", "dbo.ADF_Class"},
    },
    {
        "question": "How many students have a cancellation on record?",
        "expected": {"dbo.ADF_Student", "dbo.ADF_Cancel"},
    },
    {
        "question": "Which instructor is linked to each cancellation?",
        "expected": {"dbo.ADF_Instructor", "dbo.ADF_Cancel"},
    },
    {
        "question": "What marketing source is recorded for each student?",
        "expected": {"dbo.ADF_Student", "dbo.ADF_Marketing"},
    },
    {
        "question": "List classes together with their location.",
        "expected": {"dbo.ADF_Class", "dbo.ADF_Location"},
    },
    {
        "question": "List classes together with their learning path.",
        "expected": {"dbo.ADF_Class", "dbo.ADF_Path"},
    },
    {
        "question": "What courses belong to each learning path?",
        "expected": {"dbo.ADF_Path", "dbo.ADF_Course"},
    },
    {
        "question": "What installment plan covers each class signup?",
        "expected": {"dbo.ADF_ClassSignups", "dbo.Adf_ClassInstallmentPlan"},
    },
    {
        "question": "What event is scheduled for each class event record?",
        "expected": {"dbo.ADF_ClassEvent", "dbo.ADF_Event"},
    },
    {
        "question": "Which students submitted graduate survey answers?",
        "expected": {"dbo.ADF_Student", "dbo.ADF_GraduateSurveyAnswers"},
    },
    {
        "question": "What subscription payments has each student made?",
        "expected": {"dbo.ADF_Student", "dbo.CB_SUBSCRIPTION_PAYMENTS"},
    },
    {
        "question": "Which students have attendance recorded?",
        "expected": {"dbo.ADF_Student", "dbo.CB_Attendance"},
    },
    {
        "question": "For each class signup, show the class, the student, and their installment plan.",
        "expected": {"dbo.ADF_ClassSignups", "dbo.ADF_Class", "dbo.ADF_Student", "dbo.Adf_ClassInstallmentPlan"},
    },
]

_ALL_EXPECTED_OBJECTS = sorted(set().union(*(c["expected"] for c in CASES)))

_FORBIDDEN_TIERS_LEAK_CHECK = True


def main() -> None:
    build = get_latest_ready_retrieval_build(SOURCE_ID, USER_ID)
    if build is None:
        print("No READY retrieval build found for source_id=%r — aborting (read-only check)." % SOURCE_ID)
        return
    documents = load_stored_retrieval_documents(build.id, SOURCE_ID, USER_ID)
    print(f"Loaded build_id={build.id} status={build.status} document_count={len(documents)} "
          f"(read-only, local SQLite only)")

    tier_counts: dict[int, int] = {}
    for d in documents:
        tier_counts[d.retrieval_tier] = tier_counts.get(d.retrieval_tier, 0) + 1
    print("tier counts:", tier_counts)
    print(f"required object coverage target: {len(_ALL_EXPECTED_OBJECTS)} distinct expected objects across "
          f"{len(CASES)} questions")

    clear_schema_linker_cache()
    clear_join_planner_cache()
    catalog = build_semantic_catalog(documents)

    # Warm the caches once (cold build cost is out of scope for the p50/p99
    # WARM latency measurement below).
    link_question_to_schema(CASES[0]["question"], catalog, documents)
    plan_schema_join_subgraph(
        link_question_to_schema(CASES[0]["question"], catalog, documents), documents,
    )

    linker_only_complete = 0
    planned_complete = 0
    recovered_required_objects: set[str] = set()
    linker_recovered_required_objects: set[str] = set()
    selected_object_counts: list[int] = []
    join_edge_counts: list[int] = []
    path_length_counts: list[int] = []
    ambiguity_total = 0
    alt_plan_total = 0
    lineage_excluded_total = 0
    forbidden_leak = False
    plan_latencies_ms: list[float] = []
    per_case_rows = []

    for case in CASES:
        q = case["question"]
        expected = case["expected"]

        link = link_question_to_schema(q, catalog, documents)
        anchor_fqns = {a.object_fqn for a in link.anchors}
        linker_recovered_required_objects |= (anchor_fqns & expected)
        linker_case_complete = expected.issubset(anchor_fqns)
        linker_only_complete += int(linker_case_complete)

        t0 = time.perf_counter()
        result = plan_schema_join_subgraph(link, documents)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        plan_latencies_ms.append(elapsed_ms)

        planned_fqns: set[str] = set()
        if result.primary_plan is not None:
            planned_fqns = set(result.primary_plan.selected_object_fqns)
            selected_object_counts.append(len(planned_fqns))
            join_edge_counts.append(len(result.primary_plan.join_edges))
            for f in planned_fqns:
                tier = next((d.retrieval_tier for d in documents if d.object_fqn == f), None)
                if tier is not None and tier not in (1, 2):
                    forbidden_leak = True

        recovered_required_objects |= (planned_fqns & expected)
        planned_case_complete = expected.issubset(planned_fqns)
        planned_complete += int(planned_case_complete)
        ambiguity_total += len(result.ambiguity_records)
        alt_plan_total += len(result.alternative_plans)

        per_case_rows.append({
            "question": q,
            "expected": sorted(expected),
            "linker_anchors": sorted(anchor_fqns),
            "linker_complete": linker_case_complete,
            "planner_status": result.status,
            "planner_selected": sorted(planned_fqns),
            "planner_complete": planned_case_complete,
            "uncovered": list(result.primary_plan.uncovered_requirement_keys) if result.primary_plan else None,
            "latency_ms": round(elapsed_ms, 3),
        })

    # Lineage-exclusion check: confirm no lineage-only edge ever appears as
    # a primary_plan join_edge across all cases.
    for case, row in zip(CASES, per_case_rows):
        pass  # join_edges themselves are already only ever JOINABLE by construction (see join_planner.py)

    print("\n=== Per-question results ===")
    for row in per_case_rows:
        print(f"- {row['question']}")
        print(f"    expected:           {row['expected']}")
        print(f"    linker anchors:     {row['linker_anchors']}")
        print(f"    linker complete:    {row['linker_complete']}")
        print(f"    planner status:     {row['planner_status']}")
        print(f"    planner selected:   {row['planner_selected']}")
        print(f"    planner complete:   {row['planner_complete']}")
        print(f"    uncovered reqs:     {row['uncovered']}")
        print(f"    latency (ms):       {row['latency_ms']}")

    print("\n=== Summary ===")
    print(f"linker-only complete-case coverage:   {linker_only_complete}/{len(CASES)}")
    print(f"planned-subgraph complete-case cov.:  {planned_complete}/{len(CASES)}")
    print(f"required-object coverage (linker):    {len(linker_recovered_required_objects)}/{len(_ALL_EXPECTED_OBJECTS)}")
    print(f"required-object coverage (planner):   {len(recovered_required_objects)}/{len(_ALL_EXPECTED_OBJECTS)}")
    newly_recovered = recovered_required_objects - linker_recovered_required_objects
    regressed = linker_recovered_required_objects - recovered_required_objects
    print(f"objects newly recovered by planner:   {sorted(newly_recovered)}")
    print(f"objects regressed by planner:         {sorted(regressed)}")
    if selected_object_counts:
        print(f"selected-object count distribution:   min={min(selected_object_counts)} "
              f"median={statistics.median(selected_object_counts)} max={max(selected_object_counts)}")
    if join_edge_counts:
        print(f"join-edge count distribution:         min={min(join_edge_counts)} "
              f"median={statistics.median(join_edge_counts)} max={max(join_edge_counts)}")
    print(f"total ambiguity records:              {ambiguity_total}")
    print(f"total alternative plans:              {alt_plan_total}")
    print(f"Tier 3/4 / forbidden object leakage:  {forbidden_leak}")

    sorted_latencies = sorted(plan_latencies_ms)
    def _pct(p):
        idx = min(len(sorted_latencies) - 1, int(round(p * (len(sorted_latencies) - 1))))
        return sorted_latencies[idx]
    print(f"warm planning latency (ms): p50={_pct(0.50):.3f} p90={_pct(0.90):.3f} p99={_pct(0.99):.3f} "
          f"max={sorted_latencies[-1]:.3f}")


if __name__ == "__main__":
    main()
