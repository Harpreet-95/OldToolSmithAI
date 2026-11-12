# ToolSmithAI: Build Narrative

## 1. The problem

I built ToolSmithAI for business and operations leaders who need answers from organizational data but cannot write SQL. Their usual choices are to wait for an analyst or trust a text-to-SQL tool that may choose the wrong table, invent a join, or generate an unsafe statement. ToolSmithAI lets them ask questions in everyday language while treating model-generated SQL as untrusted code that must be checked before it reaches the database.

## 2. What I built

A web app where a signed-in user connects a Microsoft SQL Server database or uploads a CSV/Excel file, then asks questions. For a connected database, the system profiles the schema, retrieves the relevant tables with embeddings, and has a model draft one read-only query. It checks that query, runs it against the live database, and returns a table, KPIs, insights and a chart when one fits. It asks a clarifying question when a request is ambiguous, handles follow-ups, and exports to CSV, Excel and PDF. Uploaded files go through a separate report pipeline. Scheduling and email delivery exist but are optional.

Database questions are off by default. A source must be prepared and its ID listed in `.env` before it answers (README, "Enable database question answering").

## 3. How it works

```text
React app (frontend/src)
  -> POST /v1/composer/ask            api/v1/composer.py
  -> SQL source -> core/sqlservice/ai_first_fast_path_service.py
       1. embedding retrieval of schema   embedding_coordinator.py, retrieval_coordinator.py
       2. ONE model call: intent + tables + SQL   fast_path_generation_service.py
       3. join completion, ambiguity check        schema_relationship_completion.py, ambiguity_guard.py
       4. AST validation (final authority)        sql_validation.py
       5. read-only execution                     sql_execution.py
       6. table/KPI/insight/chart                 result_insight_builder.py, result_chart_builder.py
  -> CSV/Excel dataset -> report pipeline  core/reporting/, core/intelligence/
```

Supporting pieces: SQLite metadata store (`data/`, schema built in `data/models.py`), validated-plan caches (`fast_path_plan_cache.py`, `durable_plan_store.py`), follow-up state signed with HMAC (`api/v1/sql_pipeline_composer_state.py`), connection-time profiling (`core/profiling/`), auth (`auth/`).

## 4. The decisions

**1. One AI-first route instead of a deterministic planner.** The project started as a keyword task interpreter (`78c2b26`). The database side grew into a keyword-and-rules intent router plus an orchestrator and agent (`core/orchestrator/`, `core/execution/`). I replaced them with a single route and deleted the old modules (`7a0d30e`). The tests now assert there is no fallback: a source that is not enabled gets a "not enabled" answer, never another pipeline (`tests/test_composer_single_route.py`).

**2. One model call, with a deterministic validator that is never skipped.** The earlier AI-first design used three model calls (select schema, generate SQL, review). I collapsed them into one combined call and kept every downstream check unchanged (`ai_first_fast_path_service.py` docstring). A failed validation returns a sanitized failure; there is no repair or retry loop. The validator parses T-SQL into an AST with sqlglot and states that it is "never regex-only" (`sql_validation.py`). I also tried a simpler variant that sends the whole schema to one call (`direct_schema_experiment.py`). That module is not on the live route.

**3. Separate measured metadata enrichment from trusted business knowledge.** I built a connect-time pass that inferred possible business meaning from schema metadata. I did not treat those guesses as authoritative. In A/B evaluation, enabling the inferred evidence made answers worse, so I removed that evidence from the live prompt and left the portable knowledge layer disabled by default (`ENABLE_PORTABLE_KNOWLEDGE_LAYER`, `core/config.py`). The system still uses profiled schema, relationships, view definitions, and explicitly approved mappings; the decision was to exclude unproven inferred labels, not to discard metadata retrieval.

**4. Cache plans, never result rows.** The cache stores validated structure and SQL. Every hit is revalidated and re-executed, so answers stay current. The durable cache is keyed by an HMAC of tenant, source type and host/port/database, not by the numeric source ID, so a reconnect to the same database can still hit it (`docs/validation/phase7_step6h_durable_plan_design.md`). The cost is that SQL literals are stored, so the metadata database needs protection (README).

**5. Remove surface instead of rewriting tests to look green.** For handoff I cut the backend routes down to those the frontend uses. I retained eight tests for the retired planner as clearly labeled historical skips rather than changing their expectations to create a false success (HANDOFF.md).

## 5. What broke, and what fixed it

**"Highest total cost" returned the single largest row.** `extract_query_intent` used an `if/elif` chain, so "highest" matched MAX before "total" could match SUM. On a test question the answer was 700.0 where the true total was 2590.0. I found it by tracing the first wrong field in the plan, then reordered the chain, added `detect_ranking_direction`, and added a 28-test regression file (named in the report; it is not under `tests/` now). It shipped inside the 439-file commit `7a0d30e`, so its exact date is not separable.

**Business Learning made answers worse.** Across three repeated runs the aggregation report records wrong answers at 1 per run OFF and 8 per run ON, so I called NO-GO. The cause for one case: the connect-time pass wrote `is_dimension` flags that only existed under ON, so "each container" grouped by `ContainerClass` rather than the container ID. The fix tagged connect-time rows as preliminary, ignored them as evidence, and removed them from the prompt. After the fix, newly wrong answers attributable to it were zero across 408 questions. I also fixed a double-counting path.

**Coverage showed 107%.** The profiling counter was incremented by hand and drifted when a batch window ran twice. The report records 1501 against 1401 true tables on the demo source. The fix counts distinct rows (`10e1e09`). A separate bug left `columns_total` at 0 for batch snapshots; I fixed it and added a startup backfill (`da27650`).

**Expired sessions counted as attacks.** Per the code comment, a burst of expired-token 401s could trip the brute-force limiter, so I limited counting to login and register (`b6ec20a`). Admin routes also rejected JWT admins because the role check was tied to API keys (`a8a27f7`).

**The repository did not stand alone.** A run of "Repository reproducibility" commits (`58f1a76` through `1c00f0f`, plus `90b6875`) added missing modules and 148 lines of missing schema migrations. The commit messages do not record how I found them.

**Leaks found in hardening.** Raw exception text went to clients, and uploads were read without a size cap. I returned generic errors and added a limit (`7364566`). In the same commit I replaced `python-jose` with PyJWT.

**Measurement problems.** A usage-limit reset interrupted one A/B run, and a host suspend inflated two latency values. I resumed without rerunning completed passes and excluded the bad latencies instead of averaging them in.

## 6. Evidence

| Claim | Proof |
| --- | --- |
| Started as keyword task interpreter | `78c2b26`, first README |
| Single composer route, no fallback | `tests/test_composer_single_route.py`; `api/v1/composer.py` |
| Old orchestrator/execution modules deleted | `7a0d30e` stat (`core/orchestrator/agent.py` -1090, `core/execution/*`) |
| Three calls collapsed to one; validator final; no retry | `ai_first_fast_path_service.py` docstring |
| AST validation, not regex | `sql_validation.py` docstring |
| Direct-schema variant not on live route | `direct_schema_experiment.py`; README architecture |
| Retrieval, ambiguity, execution, charts exist | `core/sqlservice/` files named in section 3; HANDOFF.md claims table |
| Cache stores plans, re-executes SQL | README "Data and security rules"; `durable_plan_store.py` |
| Identity-key design | `phase7_step6h_durable_plan_design.md` Part 2 |
| Follow-up state HMAC-signed | HANDOFF.md "Security"; `sql_pipeline_composer_state.py` |
| Upload limit, generic errors, PyJWT | `7364566` diff |
| Routes cut; 8 tests skipped as historical | HANDOFF.md; API-surface consolidation commit |
| MAX/SUM bug, 700.0 vs 2590.0, fix list | `aggregation_semantics_and_ab_causality_report.md` A, C |
| Business Learning 1 vs 8 wrong, NO-GO | same report, sections N, P |
| `is_dimension` mechanism | same report, section I |
| Trust fix, 0 newly wrong in 408, usefulness ~nil | `business_learning_trust_hardening_report.md` 6, 35 |
| Double counting fixed | `business_learning_candidate_discovery_report.md` 6 |
| Disabled by default | `core/config.py` line 776 |
| 107% coverage, 1501 vs 1401 | `tests/test_structural_coverage_authoritative_count.py` docstring; `10e1e09` |
| `columns_total` bug | `da27650`; `tests/test_batch_profiling_columns_total.py` |
| Rate limiter, admin JWT fixes | `b6ec20a`, `a8a27f7` diffs |
| Missing modules/migrations | `58f1a76`, `3385c86` stats |
| Interrupted run, latency excluded | candidate discovery report, section 25 |
| Reported test and audit results | HANDOFF.md table; I did not rerun them |

## 7. What is unfinished

**Not built:** live answering for anything but SQL Server; a bundled frontend in the Docker image; production controls the README lists (managed secrets, SSO, monitoring, backups).

**Built but not proven:** the full backend result is produced locally rather than in CI, which runs a smaller smoke subset. The repository does not measure real-world answer accuracy, and the A/B suites used four small synthetic schemas. The inferred business-learning layer was not tested on a large schema. Two question-level defects (customs clearance status and maintenance cost by equipment type) remain open in the validation reports.

**With two more weeks:** build a larger, reviewed accuracy set from real business questions; turn approved business definitions into versioned mappings; split the oversized `routes.py` and `AIWorkspace.jsx` (HANDOFF names both); and add the frontend to the deployment image.
