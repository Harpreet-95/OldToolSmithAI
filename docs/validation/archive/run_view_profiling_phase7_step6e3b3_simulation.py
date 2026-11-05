"""
Phase 7, Step 6E3B3 -- isolated, offline, read-only view-profiling
simulation.

Snapshots the LOCAL sqlite metadata DB (data/toolsmith.db) via sqlite3's
own backup API into a private, throwaway copy, then profiles every VIEW
document in a source's latest READY retrieval build using
core.sqlservice.view_profiler.compute_view_profile directly -- the exact
same inputs data.view_profile_service.run_deterministic_view_profiling
uses -- but NEVER persists anything back, so this is safe to run
repeatedly with zero side effects on the real database.

No SQL Server connection. No OpenAI call. No write to the live
data/toolsmith.db (data.db.DB_PATH is monkeypatched to the snapshot copy
BEFORE any module that calls get_connection() is imported).

Usage:
    python -m docs.validation.run_view_profiling_phase7_step6e3b3_simulation \
        --source-id 10004 --user-id 28

    # Reuse an existing snapshot instead of taking a fresh one:
    python -m docs.validation.run_view_profiling_phase7_step6e3b3_simulation \
        --source-id 10004 --user-id 28 --snapshot-path C:\\path\\to\\snapshot.db --skip-snapshot
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter


def _take_snapshot(live_db_path: str, dest_path: str) -> None:
    src_conn = sqlite3.connect(f"file:{live_db_path}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(dest_path)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--snapshot-path", default=None, help="Where to write/read the isolated snapshot copy.")
    parser.add_argument("--skip-snapshot", action="store_true", help="Reuse --snapshot-path as-is instead of taking a fresh copy.")
    parser.add_argument("--out", default=None, help="Optional path to write per-view JSON detail.")
    args = parser.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, repo_root)

    import data.db as _db_module  # imported AFTER sys.path insert, BEFORE any other project module

    snapshot_path = args.snapshot_path or os.path.join(tempfile.gettempdir(), f"toolsmith_view_profiling_snapshot_source{args.source_id}.db")
    if not args.skip_snapshot:
        live_db_path = str(_db_module.DB_PATH)
        print(f"Snapshotting {live_db_path} -> {snapshot_path} (read-only source open; this is a COPY, never a write to the live file)")
        _take_snapshot(live_db_path, snapshot_path)

    _db_module.DB_PATH = snapshot_path  # every subsequent get_connection() call in this process now targets ONLY the snapshot

    from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
    from core.sqlservice import schema_object_authority as authority_module
    from core.sqlservice.view_profiler import compute_view_profile, classify_profile_state
    import core.sqlservice.view_profiler as vp
    from data.view_profile_service import _load_view_semantic_evidence
    from core.dictionary.pii_detector import SECRET_POLICY_VERSION

    build = get_latest_ready_retrieval_build(args.source_id, args.user_id)
    if build is None:
        print(f"No READY retrieval build found for source_id={args.source_id}, user_id={args.user_id!r} in the snapshot.")
        return 1

    documents = load_stored_retrieval_documents(build.id, args.source_id, args.user_id)
    view_docs = [d for d in documents if d.object_type == "VIEW"]
    evidence_by_fqn_lower = _load_view_semantic_evidence(args.source_id)
    authority_result = authority_module.load_schema_object_authority(
        documents, build.id, args.source_id, build.schema_fingerprint, use_cache=False,
    )
    cols, pks = {}, {}
    for doc in documents:
        key = doc.object_fqn.lower()
        cols[key] = frozenset(str(c).lower() for c in (doc.metadata.get("column_names") or ()))
        pks[key] = tuple(doc.metadata.get("primary_key_columns") or ())

    state_counts = Counter()
    purpose_counts = Counter()
    truncated_count = 0
    detail = []

    for doc in view_docs:
        key = doc.object_fqn.lower()
        evidence = evidence_by_fqn_lower.get(key)
        authority_obj = authority_result.authorities_by_fqn_lower.get(key)
        direct_deps = evidence.get("referenced_tables") if evidence else []
        transitive_deps = []
        if authority_obj is not None:
            transitive_deps = [d for d in authority_obj.dependency_fqns if d.lower() not in {x.lower() for x in (direct_deps or [])}]
        profile = compute_view_profile(
            doc.object_fqn, evidence.get("definition_sql") if evidence else None,
            source_id=args.source_id, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
            direct_dependencies=direct_deps or [], transitive_dependencies=transitive_deps,
            authority=authority_obj, column_names_by_fqn_lower=cols, primary_key_columns_by_fqn_lower=pks,
            now="simulation",
            definition_capture_status=(evidence.get("definition_capture_status") if evidence else None),
        )
        if profile.definition_truncated_suspected:
            truncated_count += 1
        state = classify_profile_state(
            profile, schema_fingerprint=build.schema_fingerprint,
            current_definition_hash=(evidence.get("definition_checksum") if evidence else None),
            current_secret_policy_version=SECRET_POLICY_VERSION,
        )
        state_counts[state] += 1
        if state in (vp.PROFILE_STATE_COMPLETE, vp.PROFILE_STATE_LIMITED_COMPLETE) and profile.suitability:
            s = profile.suitability
            for purpose, flag in (("detail", s.suitable_for_detail), ("count", s.suitable_for_count),
                                   ("aggregation", s.suitable_for_aggregation), ("join", s.suitable_for_join)):
                if flag:
                    purpose_counts[purpose] += 1
        detail.append({
            "object_fqn": doc.object_fqn,
            "profile_state": state,
            "completion_status": profile.completion_status,
            "failure_reason": profile.failure_reason,
            "unresolved_reasons": sorted({c.unresolved_reason for c in profile.output_columns if c.unresolved_reason}),
            "authority_class": authority_obj.authority_class if authority_obj else None,
            "definition_truncated_suspected": profile.definition_truncated_suspected,
            "limited_complete_reasons": list(profile.limited_complete_reasons),
        })

    total = len(view_docs)
    executable = state_counts[vp.PROFILE_STATE_COMPLETE] + state_counts[vp.PROFILE_STATE_LIMITED_COMPLETE]
    print(f"\nbuild_id={build.id} schema_fingerprint={build.schema_fingerprint[:16]}... total_view_docs={total}")
    print("\n=== classify_profile_state ===")
    for k, v in state_counts.most_common():
        print(f"  {k}: {v}")
    print(f"\nexecutable (COMPLETE + LIMITED_COMPLETE): {executable}/{total} ({100 * executable / total:.1f}%)" if total else "no views")
    print(f"definition_truncated_suspected: {truncated_count}")
    print("\n=== executable-for-purpose (COMPLETE + LIMITED_COMPLETE only) ===")
    for k, v in purpose_counts.most_common():
        print(f"  {k}: {v}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(detail, f, indent=2)
        print(f"\nwrote per-view detail to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
