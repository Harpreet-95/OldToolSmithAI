"""
Business Learning Trust Hardening milestone -- Part 20.

Repeated 3x OFF + 3x ON A/B run, reusing docs/validation/run_business_learning_ab.py's
own run_domain_condition/DOMAINS/CHECKERS unchanged (same isolation methodology:
fresh tmp dir per domain/condition/repeat, never reused). Prints progress per
question so this never goes silent for more than a few seconds at a time.

Run:
    python docs/validation/run_business_learning_ab_repeated.py
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
OUT_JSON = OUT_DIR / "business_learning_ab_repeated2_results.json"
N_REPEATS = 3


def main() -> None:
    models.init_db()
    tmp_root_base = OUT_DIR / "_ab_repeated2_tmp"
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
                # Persist incrementally so a long run's progress is never lost
                # even if interrupted, and so output never appears "silent".
                OUT_JSON.write_text(json.dumps(all_results, indent=2, default=str))

    print(f"\nWrote {OUT_JSON} (total {time.monotonic() - started:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
