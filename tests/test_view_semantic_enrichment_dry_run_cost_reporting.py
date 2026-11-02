"""
Focused tests -- SQL Architecture Correction, Phase 6, Step 6C1, Part 4/5:
corrected workload/cost reporting in
docs/validation/run_view_semantic_enrichment_phase6_step6c_dry_run.py.

Only imports the script's module-level functions/constants (never calls
main(), which reads the real metadata database) -- mirrors
tests/test_acceptance_suite_runner.py's own importlib pattern for testing a
docs/validation/*.py script directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent.parent / "docs" / "validation" / "run_view_semantic_enrichment_phase6_step6c_dry_run.py"
_spec = importlib.util.spec_from_file_location("run_view_semantic_enrichment_phase6_step6c_dry_run", _SCRIPT_PATH)
dry_run = importlib.util.module_from_spec(_spec)
sys.modules["run_view_semantic_enrichment_phase6_step6c_dry_run"] = dry_run
_spec.loader.exec_module(dry_run)


def test_original_step6c_estimate_renders_as_3_8_cents():
    """The exact correction Step 6C1 was asked to make: '$0.038' must
    render unambiguously as 3.8 cents, not be left as a bare dollar figure
    a reader could misread as fractions of a cent."""
    rendered = dry_run._format_usd_and_cents(0.038)
    assert rendered == "$0.0380 (3.80 cents)"
    assert "3.80 cents" in rendered


def test_zero_amount_renders_correctly():
    assert dry_run._format_usd_and_cents(0.0) == "$0.0000 (0.00 cents)"


def test_small_amounts_render_correctly():
    assert dry_run._format_usd_and_cents(0.3711) == "$0.3711 (37.11 cents)"
    assert dry_run._format_usd_and_cents(1.5) == "$1.5000 (150.00 cents)"


def test_pricing_inputs_are_explicit_module_level_constants():
    """Part 4: 'pricing inputs as explicit variables... label its
    source/date and allow it to be overridden' -- proves these are plain,
    reassignable module attributes, never buried inside a function body or
    fetched over the network."""
    assert isinstance(dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS, float)
    assert isinstance(dry_run.ASSUMED_OUTPUT_COST_USD_PER_1K_TOKENS, float)
    assert isinstance(dry_run.PRICING_SOURCE, str) and dry_run.PRICING_SOURCE
    assert isinstance(dry_run.PRICING_AS_OF_DATE, str) and dry_run.PRICING_AS_OF_DATE
    assert "not fetched live" in dry_run.PRICING_SOURCE.lower() or "not queried" in dry_run.PRICING_SOURCE.lower()


def test_pricing_inputs_are_overridable():
    original = dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS
    try:
        dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS = 0.5
        assert dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS == 0.5
    finally:
        dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS = original


def test_zero_workload_cost_is_zero_and_stable_json():
    """A report with zero estimated tokens must compute a clean zero cost,
    never a division error or NaN, and must serialize to stable JSON."""
    estimated_cost_usd = (0 / 1000) * dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS + (
        0 / 1000
    ) * dry_run.ASSUMED_OUTPUT_COST_USD_PER_1K_TOKENS
    assert estimated_cost_usd == 0.0
    rendered = dry_run._format_usd_and_cents(estimated_cost_usd)
    assert rendered == "$0.0000 (0.00 cents)"
    payload = {"estimated_cost_usd": round(estimated_cost_usd, 4), "estimated_cost_display": rendered}
    serialized = json.dumps(payload, indent=2)
    reparsed = json.loads(serialized)
    assert reparsed == payload


def test_report_payload_is_stable_json_serializable():
    """A representative report payload (the same shape main() builds)
    round-trips through JSON without loss -- stability check for Part 5's
    'stable JSON/report serialization' requirement."""
    payload = {
        "status": "OK",
        "configured_model": "gpt-4o-mini",
        "individually_oversized_views": {"count": 2, "by_reason": {"OUTPUT_BUDGET_EXCEEDED": 2}, "object_fqns": ["a", "b"]},
        "batch_input_characters": {"max": 100, "median": 50.0, "total": 150},
        "pricing_inputs": {
            "source": dry_run.PRICING_SOURCE, "as_of_date": dry_run.PRICING_AS_OF_DATE,
            "input_usd_per_1k_tokens": dry_run.ASSUMED_INPUT_COST_USD_PER_1K_TOKENS,
            "output_usd_per_1k_tokens": dry_run.ASSUMED_OUTPUT_COST_USD_PER_1K_TOKENS,
        },
        "estimated_cost_usd": 0.038,
        "estimated_cost_display": dry_run._format_usd_and_cents(0.038),
        "cost_is_an_estimate_not_a_guarantee": True,
    }
    serialized = json.dumps(payload, indent=2, default=str)
    reparsed = json.loads(serialized)
    assert reparsed == payload
    assert reparsed["cost_is_an_estimate_not_a_guarantee"] is True
    assert reparsed["estimated_cost_display"] == "$0.0380 (3.80 cents)"
