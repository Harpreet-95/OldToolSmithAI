"""
Business Learning Candidate Discovery milestone -- Part 25 (repeated 3x3).

3 independent OFF + 3 independent ON runs, reusing docs/validation/
run_business_learning_ab.py's own DOMAINS/run_domain_condition unchanged
(same isolation methodology as every prior session: fresh tmp dir/metadata
db/source_id every single run, never reused). Only run after Part 24's
single-pass gate (run_business_learning_candidate_discovery_gate.py) shows
zero newly-wrong-on-ON.

Run:
    python docs/validation/run_business_learning_candidate_discovery_repeated.py

Writes (incrementally, after every domain/condition pass):
    docs/validation/business_learning_candidate_discovery_repeated_results.json
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
OUT_JSON = OUT_DIR / "business_learning_candidate_discovery_repeated_results.json"
N_REPEATS = 3


def main() -> None:
    models.init_db()
    tmp_root_base = OUT_DIR / "_cd_repeated_tmp"
    tmp_root_base.mkdir(parents=True, exist_ok=True)

    all_results: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_repeats": N_REPEATS,
        "runs": {domain_key: {"OFF": [], "ON": []} for domain_key in DOMAINS},
    }

    started = time.monotonic()
    for rep in range(1, N_REPEATS + 1):
        for domain_key in DOMAINS:
            for condition in ("OFF", "ON"):
                tmp_root = tmp_root_base / f"rep{rep}"
                tmp_root.mkdir(parents=True, exist_ok=True)
                print(f"\n=== rep{rep} / {domain_key} / {condition} === (elapsed {time.monotonic() - started:.0f}s)", flush=True)
                result = run_domain_condition(domain_key, condition, tmp_root)
                counts = result.get("classification_counts", {})
                print(f"  -> counts: {counts}", flush=True)
                all_results["runs"][domain_key][condition].append(result)
                OUT_JSON.write_text(json.dumps(all_results, indent=2, default=str))

    print(f"\nWrote {OUT_JSON} (total {time.monotonic() - started:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
