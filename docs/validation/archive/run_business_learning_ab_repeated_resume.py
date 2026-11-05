"""
Resume helper for Part 20's repeated 3x3 run -- loads the existing partial
results JSON (business_learning_ab_repeated2_results.json) and runs ONLY the
missing domain/condition/repeat passes, appending to the same file. Never
redoes a pass that already has N_REPEATS entries recorded.
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

    all_results = json.loads(OUT_JSON.read_text())

    missing = []
    for domain_key in DOMAINS:
        for condition in ("OFF", "ON"):
            have = len(all_results["runs"][domain_key][condition])
            for rep in range(have + 1, N_REPEATS + 1):
                missing.append((rep, domain_key, condition))

    print(f"Resuming: {len(missing)} missing passes: {missing}", flush=True)

    started = time.monotonic()
    for rep, domain_key, condition in missing:
        tmp_root = tmp_root_base / f"rep{rep}"
        tmp_root.mkdir(parents=True, exist_ok=True)
        print(f"\n=== RESUME rep{rep} / {domain_key} / {condition} === (elapsed {time.monotonic() - started:.0f}s)", flush=True)
        result = run_domain_condition(domain_key, condition, tmp_root)
        counts = result.get("classification_counts", {})
        print(f"  -> counts: {counts}", flush=True)
        all_results["runs"][domain_key][condition].append(result)
        OUT_JSON.write_text(json.dumps(all_results, indent=2, default=str))

    print(f"\nWrote {OUT_JSON} (resume total {time.monotonic() - started:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
