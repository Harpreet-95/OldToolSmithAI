"""
Business Learning Candidate Discovery milestone -- Part 24 (safety gate).

ONE complete OFF pass + ONE complete ON pass, reusing docs/validation/
run_business_learning_ab.py's own DOMAINS/run_domain_condition/CHECKERS
UNCHANGED (same 4 domains, 68 questions/condition, same isolation
methodology: fresh tmp dir/metadata db/source_id per domain/condition).
ON now additionally exercises this milestone's new candidate-discovery
mechanism (data.semantic_retrieval_service.get_preliminary_candidates,
merged into _retrieve; provenance threaded into gather_candidate_evidence;
Investigation Agent preliminary_hints) automatically -- no separate flag,
since it activates whenever a preliminary map exists, exactly like the
prior Trust Hardening milestone's own gate run did for its own changes.

Run:
    python docs/validation/run_business_learning_candidate_discovery_gate.py

Writes:
    docs/validation/business_learning_candidate_discovery_gate_results.json
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import data.models as models  # noqa: E402
from docs.validation.run_business_learning_ab import DOMAINS, run_domain_condition  # noqa: E402

OUT_DIR = REPO_ROOT / "docs" / "validation"
OUT_JSON = OUT_DIR / "business_learning_candidate_discovery_gate_results.json"


def main() -> None:
    models.init_db()
    tmp_root = OUT_DIR / "_cd_gate_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

    all_results: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "domains": {}}
    started = time.monotonic()
    for domain_key in DOMAINS:
        all_results["domains"][domain_key] = {}
        for condition in ("OFF", "ON"):
            print(f"\n=== {domain_key} / {condition} === (elapsed {time.monotonic() - started:.0f}s)", flush=True)
            result = run_domain_condition(domain_key, condition, tmp_root)
            counts = result.get("classification_counts", {})
            print(f"  -> counts: {counts}", flush=True)
            all_results["domains"][domain_key][condition] = result
            OUT_JSON.write_text(json.dumps(all_results, indent=2, default=str))

    print(f"\nWrote {OUT_JSON} (total {time.monotonic() - started:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
