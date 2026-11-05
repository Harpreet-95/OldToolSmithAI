# ToolSmithAI validation assets

This directory contains the supported validation runners, versioned question
sets, and engineering evidence for the current AI-first architecture.

## Primary runners

- `run_acceptance_suite_fast_path.py` exercises the active AI-first SQL path.
- `run_golden_set.py` and `golden_runner.py` provide deterministic golden-case
  grading support.
- `run_performance_benchmark.py` measures bounded latency and cache behavior.
- `run_ccpp_real_demo_validation.py` is the authorized live-source demo check.

Some runners can make paid model calls or query an authorized live datasource.
Read each runner's command help and safety checks before execution. Automated
repository tests use fixtures and do not require the live CCPP database.

## Versioned evidence

- `enterprise_business_validation_suite.v1.json` is the original locked
  baseline.
- `enterprise_business_validation_suite.v1.1.0.json` is the current expanded
  suite.
- `ccpp_golden_cases_v2.py` contains the bounded CCPP golden cases used by
  regression tests.

Generated outputs, temporary diagnostics, local logs, and result files are not
version controlled. Never place credentials, database files, sensitive result
rows, or screenshots containing private data in this directory.

Older Phase 6/7 experimental runners are isolated under `archive/`. They are
historical evidence only and are not supported commands for a fresh clone.
