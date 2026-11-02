"""
Executive Report Query Foundation milestone, Part 14 — ONE small end-to-end
executive report against real CCPP.

Calls core.reporting.executive_report_service.generate_executive_report()
(the SAME production entry point api/v1/routes.py's
POST /v1/sources/{source_id}/executive-report calls), which internally uses
ONLY core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan for
every KPI (never the legacy semantic planner) and executes through the same
governed data.query_execution_service.execute_governed_query path ordinary
SQL Q&A uses.

This is a READ-ONLY smoke check: no governance writes, no schema changes —
every SQL statement executed is validated read-only SELECT (build_sql_plan/
generate_sql's existing read_only guarantee, unchanged). Confirm you intend
to hit the live CCPP source before running against source_id=1.

Usage:
    venv/Scripts/python docs/validation/run_executive_report_demo.py \
        --source-id 1 --user-id 28 \
        [--output docs/validation/results/executive_report_demo.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.connectors.relational.mssql       # noqa: E402,F401
import core.connectors.relational.postgresql  # noqa: E402,F401
import core.connectors.relational.mysql       # noqa: E402,F401


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", type=str, required=True)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    from core.reporting.executive_report_service import generate_executive_report

    report = generate_executive_report(args.source_id, args.user_id, source_display_name="CCPP")
    data = report.to_dict()

    print(f"\n=== {data['title']} ===")
    print(f"Generated at: {data['generated_at']}")
    print(f"\nTimings (ms): {data['timings_ms']}")

    print(f"\nKPI cards: {len(data['kpi_cards'])}")
    for card in data["kpi_cards"]:
        line = f"  - {card['title']}: {card['value']}"
        if card.get("previous_value") is not None:
            line += f" (prev {card['previous_value']}, {card['percent_change']}%, {card['direction']})"
        print(line)

    print(f"\nCharts: {len(data['charts'])}")
    for chart in data["charts"]:
        print(f"  - {chart['title']} ({chart['chart_type']}): {len(chart['labels'])} point(s)")

    print(f"\nTables: {len(data['tables'])}")
    for table in data["tables"]:
        print(f"  - {table['title']}: {len(table['rows'])} row(s)")

    print(f"\nWarnings ({len(data['warnings'])}):")
    for w in data["warnings"]:
        print(f"  - {w}")

    print(f"\nNarrative:\n{data['narrative'] or '(none)'}")

    print(f"\n--- Trace ({len(data['trace'])} KPI plan(s)) ---")
    wrong_count = 0
    skipped_count = 0
    legacy_used_count = 0
    unsafe_takeover_count = 0
    for t in data["trace"]:
        print(f"  [{t['execution_status']}] {t['kpi_id']}: \"{t['business_question']}\"")
        print(f"      tables={t['selected_tables']} ai_plan_valid={t['ai_plan_valid']} "
              f"legacy_fallback_used={t['legacy_fallback_used']}")
        if t["sql"]:
            print(f"      sql: {t['sql'][:200]}")
        if t["skip_reason"]:
            print(f"      reason: {t['skip_reason']}")
        if t["execution_status"] == "skipped_unsafe":
            skipped_count += 1
        if t["legacy_fallback_used"]:
            legacy_used_count += 1

    print(f"\nSummary: kpi_cards={len(data['kpi_cards'])} charts={len(data['charts'])} "
          f"tables={len(data['tables'])} safe_skipped={skipped_count} "
          f"legacy_used={legacy_used_count} unsafe_takeover={unsafe_takeover_count} "
          f"wrong_metric_suspected={wrong_count}")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"\nSaved full report JSON to {out_path}")


if __name__ == "__main__":
    main()
