# ToolSmithAI Handoff Report

This document records the verified state of the repository prepared for instructor review and case-study use. It describes the public repository, not Harpreet Kaur's private CCPP database, credentials, or local runtime data.

## Readiness decision

The repository is ready to share as a portfolio and case-study codebase. The supported application paths are documented, automated checks pass, dependency audits report no known vulnerabilities, runtime secrets and local data are excluded from Git, and historical validation material has been separated from current documentation.

The verified handoff-hardening baseline is commit `7364566`. The repository is
described as a production-oriented prototype suitable for a controlled pilot,
not as a complete production deployment.

## Verification evidence

Verified on November 9, 2026 using Python 3.12 and Node.js 20, after the API-surface consolidation described below. Both dependency audits and the CI smoke subset were re-run on this date.

| Check | Result |
| --- | --- |
| Full backend suite | 6,520 passed, 9 skipped, 0 failed |
| Frontend suite | 175 passed, 0 failed |
| Frontend production build | Passed |
| Frontend lint | Passed |
| Python dependency audit | No known vulnerabilities |
| npm dependency audit | 0 vulnerabilities |
| CI backend smoke subset | 154 passed |

Eight of the nine backend skips cover assertions for the retired direct query-planner architecture. The live product route uses the AI-first composer and never calls that planner. Those tests remain in place, clearly marked as historical behavior, instead of being rewritten to create a false success. The remaining skip is an environment-dependent check that needs symlink-creation permission on Windows.

GitHub Actions intentionally runs a fast 154-test backend smoke gate on every
push and pull request, plus the complete frontend test, lint, build, and both
dependency-audit checks. The 6,520-test backend result was produced locally
because running that entire suite on every portfolio commit would be
disproportionately slow. The workflow does not claim otherwise.

## API-surface consolidation

The backend exposes 76 routes, each called by the current frontend or required
for health monitoring. Manual Search, Dictionary, Governance, Lineage,
knowledge-graph, relationship-administration and direct query-planning endpoints
were removed; their internal services remain because connection-time discovery,
profiling and the Composer still use them. Database questions have one route,
`POST /v1/composer/ask`. Of the 9 skipped tests, 8 cover the retired direct
query planner as a library and 1 needs Windows symlink permission.

## Positive impact

ToolSmithAI gives non-technical users a safer path from a business question to
current data. It reduces dependence on manually written SQL for routine
questions, makes database evidence understandable through tables and visuals,
and keeps a deterministic validation boundary between an AI-generated proposal
and database execution. Clarification prevents silent guessing, while validated
plan reuse reduces repeated AI work without returning stale result rows.

## Claims mapped to implementation

| Public claim | Implementation evidence |
| --- | --- |
| One supported question route | `api/v1/composer.py` and `/v1/composer/ask` |
| Relevant metadata is retrieved with embeddings | `core/sqlservice/embedding_coordinator.py` and `retrieval_coordinator.py` |
| Generated SQL is checked before execution | `core/sqlservice/sql_validation.py` |
| Approved SQL executes against current source data | `core/sqlservice/sql_execution.py` |
| Ambiguous questions request clarification | `core/sqlservice/ambiguity_guard.py` and `ai_first_fast_path_service.py` |
| Repeat questions can avoid new model calls | `core/sqlservice/fast_path_plan_cache.py` and `durable_plan_store.py` |
| Cache hits still validate and execute live | `core/sqlservice/ai_first_fast_path_service.py` |
| Results become KPIs, insights, and charts when appropriate | `core/sqlservice/result_insight_builder.py`, `result_chart_builder.py`, and `core/answering/` |
| Follow-up context is integrity protected | `api/v1/sql_pipeline_composer_state.py` |

## Verification commands

```powershell
python -m pytest tests
python -m pip_audit -r requirements.txt
Set-Location frontend
npm test
npm run lint
npm run build
npm audit
```

The GitHub smoke subset is defined transparently in
`.github/workflows/quality.yml`.

## Supported product scope

- Connect and profile Microsoft SQL Server sources.
- Upload and work with CSV and Excel datasets.
- Retrieve relevant schema and business evidence using embeddings.
- Generate read-only SQL through the AI-first fast path.
- Apply deterministic validation before every SQL execution.
- Revalidate cached plans and re-execute SQL against current source data.
- Return tables, KPIs, grounded insights, and appropriate charts.
- Support clarification and follow-up questions.
- Export results to CSV, Excel, and PDF.
- Support optional scheduling and email delivery.

The old keyword intent router and legacy multi-agent SQL execution route are not part of the active composer path.

## Security and repository hygiene

- The backend rejects missing or weak JWT configuration.
- Conversation state is integrity-protected with HMAC and rejected when altered.
- Dataset uploads have a configurable size limit.
- Unexpected server errors return generic messages rather than internal details.
- SQL plans are isolated by source and user context; raw result rows and credentials are not stored in the durable plan cache.
- `.env`, SQLite runtime databases, credentials, logs, uploads, exports, backups, dependency folders, and local validation outputs are ignored by Git.
- The repository contains no committed CCPP credentials, student records, or machine-specific user paths.
- Historical architecture documents and retired experimental runners are retained under clearly labelled archive folders.

## Known limitations

- A fresh uncached AI question depends on the configured model and may take several seconds. Repeated supported questions can use the validated-plan cache, but SQL still executes live.
- Database question answering is disabled until a reviewer connects and prepares a source, then explicitly enables its displayed Source ID in `.env`.
- The private CCPP demonstration cannot be reproduced without separately authorized database/network access and private configuration.
- Containers run the backend only; the React frontend is built and served separately during development.
- Cross-computer transfer does not include credentials, runtime metadata databases, cached plans, embeddings, or private business data.
- Some deprecation warnings remain in third-party test integrations. They do not fail the verified suite and should be handled in a later dependency-modernization cycle rather than during handoff cleanup.
- Several central frontend and API modules are large and should be split during a future maintainability refactor; that refactor is intentionally not mixed into the handoff hardening work.
- Enterprise production use still requires managed secrets, SSO/RBAC review, centralized observability, backup/disaster-recovery procedures, deployment automation, and workload-specific performance/security validation.

## Reviewer quick start

1. Follow [README.md](README.md) to install Python 3.12 and Node.js 20 dependencies.
2. Copy `.env.example` to `.env` and replace every placeholder with local secrets.
3. Start the backend and frontend in separate terminals.
4. Upload a CSV/Excel dataset, or connect a non-production SQL Server source.
5. For SQL question answering, wait for preparation, find the Source ID on the Data Sources page, and enable that ID as described in the README.
6. Run the checks and manual smoke checklist in [TESTING.md](TESTING.md).

## Ownership and presentation

ToolSmithAI was built by Harpreet Kaur. The repository is supplied for authorized instructor review and case-study presentation. All rights are reserved; no open-source license is granted.
