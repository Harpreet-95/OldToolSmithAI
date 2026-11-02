# Phase 7, Step 6E3B3 — view-profile coverage expansion report

Scope: deterministic T-SQL lineage profiler only (`core/sqlservice/view_profiler.py`), its persistence layer (`data/view_profile_service.py`), and its one enforcement consumer (`core/sqlservice/sql_validation.py`). No live SQL Server connection, no OpenAI call, no write to the live metadata database or to source 10004's real SQL Server. All numbers below come from `docs/validation/run_view_profiling_phase7_step6e3b3_simulation.py` run against a **private, disposable, backup-API snapshot** of the local `data/toolsmith.db` (never the live file — see Part 11).

## Part 1 — Exhaustive failure inventory (pre-improvement baseline)

Reproduced baseline against the real CCPP catalog (source_id 10004, 240 views): **91 COMPLETE / 107 LINEAGE_INCOMPLETE / 42 TYPED_FAILURE**, confirming the numbers this step was handed. The 149 non-COMPLETE views break down as follows. "Save no raw data values" was honored throughout — every figure below is derived from schema-structural facts (SQL shape, column/table names, lengths), never a row value.

### By unresolved-reason (multi-label — a view can carry more than one)

| reason | count | meaning |
|---|---|---|
| `column_not_found_on_any_known_source` | 42 | mostly unqualified references into a derived-table/CTE column the old alias map never registered |
| `unresolvable_table_alias` | 36 | the FROM/JOIN target was a derived-table subquery, which the old alias map silently dropped (only real `exp.Table` sources were ever registered) |
| `nested_subquery_column_source` | 20 | a scalar/correlated subquery embedded directly inside a projection expression |
| `star_expansion_ambiguous_multi_table_source` | 19 | `SELECT *`/`alias.*` across more than one FROM/JOIN target |
| `unsupported_expression_shape` | 11 | a projection with no resolvable column at all (pure literal-like expression sqlglot didn't classify as `Literal`, etc.) |
| `union_branch_lineage_unresolved` | 8 | at least one UNION branch itself unresolved |

### By parser-stage failure (the 42 TYPED_FAILURE views)

| cause | count | parser stage | evidence-backed? |
|---|---|---|---|
| **Suspected definition truncation** | **41** | tokenizer/grammar, mid-statement | **Yes — root-caused** |
| Genuine T-SQL tokenizer edge case (`col > ='literal'`, a whitespace-separated `>=` SQL Server's own tokenizer tolerates but sqlglot's does not) | 1 | tokenizer | Yes — isolated, inspected directly |

**Root cause of the 41**: `data/view_semantic_evidence.definition_sql` is captured by `data/view_semantics_service.py` via `result.sample_values[0]` — reuse of a generic value-**sampling** read path intended for a handful of representative row values, not a full object definition. That path truncates long strings; **41 of 42 TYPED_FAILURE views, and 48 of all 240 views, have `definition_sql` at EXACTLY 4000 characters**, cut off mid-token/mid-clause (verified directly: e.g. `dbo.vw_StudentRoles`'s stored text ends `FROM   dbo.ADF_C` — a table name sliced in half). None of the other 7 truncated-but-still-parseable views landed in COMPLETE (verified) — the current profiler never silently trusted a truncated prefix, but this is luck, not a guarantee, which is why Step 6E3B3 now flags `definition_truncated_suspected` explicitly (see Part 2/3) rather than relying on that luck continuing. **Fixing the capture itself requires a live SQL Server read and is out of scope for this pure-computation module** — see Part 8.

### Structural-shape tag frequency across the 149 non-COMPLETE views (a view may carry several tags)

| shape | count | now resolved by 6E3B3? |
|---|---|---|
| Bracketed/schema-qualified identifiers (`[dbo].[X]`) | 104 | N/A — already fully supported by sqlglot's T-SQL tokenizer; never an actual blocker (co-occurs with failures but isn't their cause) |
| Derived-table subquery in FROM/JOIN | 62 | **Yes — the single highest-impact fix** (Part 2) |
| CASE expression | 61 | N/A — already resolved generically (CASE's own column references are walked like any other expression); not itself a failure driver |
| `SELECT *` / `alias.*` | 54 | **Yes — safe multi-table expansion** (Part 2) |
| Scalar/projection subquery (correlated) | 33 | Deliberately **not** attempted — fails closed by design (Part 2) |
| `COALESCE`/`ISNULL`/`TRY_CONVERT` | 16 | N/A — already resolved generically |
| Scalar function call (`DecryptByKey`, `ProperCase`, user-defined `UDF_*`) | 13 | N/A — already resolved generically (the function's column argument is walked regardless of the wrapping call) |
| UNION/set operation | 8 | **Yes — N-way flattening fix** (Part 2; also a latent correctness fix, not just coverage) |
| CTE | 3 | Already chained within one `WITH` clause before this step; **derived-tables-inside-a-CTE now also resolve** (Part 2) |
| Multi-CTE (2+ CTEs in one `WITH`) | 2 | Same as above |
| Window function | 2 | N/A — already MEDIUM-confidence resolved, not a failure driver |
| Temporary construct (`#temp`, `TEMP TABLE`) | 2 | Not attempted — graceful typed incomplete, per this step's own "unsupported dynamic/opaque construct" allowance |
| PIVOT/UNPIVOT | 2 | Not attempted — graceful typed incomplete, same allowance (low volume, high complexity/risk) |
| CROSS/OUTER APPLY | 1 | **Yes** — resolved as a scope via `exp.Lateral` (Part 2) |

No view in this catalog uses a recursive CTE, `OPENQUERY`/`OPENROWSET` (cross-source), or any unsupported T-SQL DDL/session-statement preamble beyond the truncation defect above.

### Secret/authentication-column exposure among non-COMPLETE views

Checked structurally (column-name pattern match, `core.dictionary.pii_detector.is_restricted_category` — informational, never a value read): a handful of the 149 reference a dependency table carrying a restricted/secret-shaped column (e.g. the ASP.NET membership tables' own `Password*` columns). These are exactly the views Part 3/4's `UNSAFE` state now exists to separate from ordinary incompleteness (see Part 4).

### Was this previously selectable? Would blocking it regress the product?

`core.sqlservice.view_profiler.classify_profile_state`'s own docstring documents that Step 6E3B2 Part 1 **tightened** enforcement to require `PROFILE_STATE_COMPLETE` unconditionally, "superseding" an earlier, looser rule that still let a detail query through for some non-COMPLETE states. That means a real slice of these 149 views **were** usable before 6E3B2 landed and became hard-blocked by the all-or-nothing COMPLETE gate — this is precisely the regression this step's `LIMITED_COMPLETE` fallback (Part 3/4) exists to partially reverse, safely.

### Legitimate business view vs. framework/security/snapshot — best available evidence

- **27 of 240** are now structurally **proven** not business-safe: `authority_class == UNSAFE_DEPENDENCY` **and** a confirmed/unprovable secret-column exposure risk (`PROFILE_STATE_UNSAFE`, see Part 4).
- **14 of 240** carry a name pattern suggesting deprecated/snapshot status (`_Backup`, `_DELETE`, `_old`, `_Temp`, `_archived`/`Archived_`, e.g. `vw_Hubspot_Leads_Backup_06072023`, `vw_OutreachStatus_Industry_DELETE`, `vw_StudentCirriculum_old`) — **a naming heuristic, not a proof**; several of these are, in fact, now executable (COMPLETE/LIMITED_COMPLETE), and this step does not reclassify them one way or the other. Confirming "legitimate business view" status precisely for these requires either the existing business-knowledge-package mapping (`data/business_knowledge_service.py`) or a human/live judgment call — out of this pure-computation step's scope (see Part 8).

## Part 2 — Deterministic T-SQL lineage expansion (implemented)

All in `core/sqlservice/view_profiler.py`, extending the existing sqlglot-based pass — no parallel parser introduced.

1. **Derived-table and CTE output columns unified as named "scopes"** (`_build_scopes`/`_resolve_subquery_scope`, replacing `_build_alias_map`/`_resolve_ctes`). A FROM/JOIN derived table (`(SELECT ...) AS d`) is now resolved exactly like a CTE already was — an outer reference `d.Col` resolves through the derived table's own lineage instead of hitting `unresolvable_table_alias`. Bounded by `_MAX_SCOPE_RESOLUTION_DEPTH` (6) so a pathological nesting depth can never prevent termination.
2. **Safe multi-source `SELECT *`/`alias.*` expansion** (`_expand_star`, rewritten). Every FROM/JOIN/scope target is expanded (not just the case of exactly one target), each output column positionally attributed to its own real source in FROM/JOIN declaration order — exactly what SQL Server itself returns. A duplicate-named column from two different sources is legitimately two separate, individually-resolved output columns; nothing is ever decided by name-matching across candidates. Qualified star (`alias.*`) is scoped to just that one alias (previously treated identically to a bare `*`, which was itself a latent bug).
3. **CROSS/OUTER APPLY** resolved as another scope via sqlglot's `exp.Lateral` wrapper, when the AST exposes a correlated `Select` — per this step's own "where sqlglot exposes sufficient AST evidence" allowance.
4. **N-way UNION flattening** (`_flatten_set_operation_branches`). sqlglot nests a 3+-way `UNION` as `SetOperation(SetOperation(A, B), C)`; the prior code only ever inspected the immediate `tree.left`/`tree.right`, silently dropping every branch beyond the first two whenever `tree.left` was itself a `SetOperation`. This was a **latent correctness risk** (lineage computed from an incomplete branch set with no typed signal), not just a coverage gap — now fixed.
5. **Common SQL Server CREATE VIEW wrappers/session statements**: when `sqlglot.parse` returns more than one top-level statement, the profiler now keeps only the ones that are actually a `CREATE`/`SELECT`/set-operation body, discarding any leading/trailing session statement — defensive coverage for a capture path that doesn't currently emit these in this catalog, but might.
6. **`definition_truncated_suspected`** flag: a `definition_sql` landing at exactly 4000 characters (the known capture-path truncation length, see Part 1) is flagged honestly rather than folded into a generic `PARSE_ERROR`/`LINEAGE_INCOMPLETE` label, and this flag unconditionally disqualifies `LIMITED_COMPLETE` eligibility (Part 4) — a truncated definition's "successful parse" is never trusted as evidence of the view's real shape.

**Deliberately not attempted** (graceful typed failure, per this step's own allowance): a scalar/correlated subquery embedded directly in a projection (never infers a source when one can't be proven), PIVOT/UNPIVOT, and temporary-table constructs. All three remain low-volume in this catalog (33, 2, 2 views respectively) and high-complexity/high-risk to support correctly; they fail closed with a typed, inspectable reason rather than a guess.

**Also fixed as part of this work** (found while verifying the simulation, not part of the original Part 2 list): `_failed()` never set `secret_policy_version` on a typed-failure profile, leaving it at the dataclass default (0). Since `classify_profile_state` compares that against the currently-active policy whenever a caller supplies one (`core.sqlservice.sql_validation` always does), a genuinely fresh `TYPED_FAILURE` profile was being reported as `STALE` instead — both still block execution identically, so this was never a safety gap, only a misleading diagnostic reason string, but it also corrupted this step's own Part 7 state-count reporting until fixed.

## Part 3/4 — Layered catalog fallback: `PROFILE_STATE_LIMITED_COMPLETE` / `PROFILE_STATE_UNSAFE`

New runtime states in `classify_profile_state`, strictly additive to the existing `MISSING → STALE → TYPED_FAILURE → LINEAGE_INCOMPLETE → COMPLETE` ladder — `COMPLETE`'s own contract is untouched.

**`ViewProfile.limited_complete_eligible`** (computed in `compute_view_profile`, only ever evaluated for an otherwise-`INCOMPLETE` profile) is `True` only when **all** of:

- `completion_status == INCOMPLETE` (a `FAILED`/truncated profile is never eligible, by construction);
- `definition_truncated_suspected` is `False`;
- `snapshot_status == CURRENT` and `cross_source_status == SINGLE_SOURCE` (no snapshot/cross-source violation);
- the view's own resolved output carries **no** known secret column;
- **no dependency table** (direct or transitive) carries **any** column name-pattern-matching a secret/credential category — the simplest sufficient proof this module can make without re-deriving exact per-column exposure: if a dependency has zero secret-shaped columns at all, nothing an unresolved output column could be is a secret; if it has even one, and any output column is still unresolved, safety can never be proven here (never guessed in either direction);
- at least half of the view's own output columns are individually resolved (`_LIMITED_COMPLETE_MIN_RESOLVED_RATIO = 0.5`) — a view that's still mostly a black box is simply incomplete, not "limited but usable".

`classify_profile_state` returns `LIMITED_COMPLETE` when eligible; otherwise, if the **specific** disqualifier was a secret-exposure risk (own or dependency), it returns the new `PROFILE_STATE_UNSAFE` instead of the generic `LINEAGE_INCOMPLETE` — distinguishing "blocked for safety" from "blocked for generic incompleteness" in the state vocabulary, per this step's own explicit ask. `UNSAFE` is never applied to an already-`COMPLETE` profile; that enforcement remains `sql_validation`'s own unconditional secret-column block, unchanged.

Restrictions on `LIMITED_COMPLETE` (Part 4's own list) are enforced through the **same, unmodified `ViewSuitability` machinery** `COMPLETE` already uses — entity counts still require a proven `required_distinct_key`, aggregation still requires a compatible proven grain, joins still require a confirmed relationship, and population-restriction evidence is always a known fact once parsing succeeds (the WHERE/HAVING clause is either present or it isn't). `core.sqlservice.sql_validation._validate_view_suitability` now accepts `LIMITED_COMPLETE` through the identical suitability/secret-column checks `COMPLETE` is subject to (Part 5) — it gets no weaker enforcement, only a weaker evidence basis for having reached the same facts.

**Not attempted in this step**: full NL-question-level "unrestricted 'all' claim" rejection against a filtered `LIMITED_COMPLETE` view. The view's own `population_restriction` evidence is captured and surfaced (satisfying "population restriction status is known"), and `sql_validation` already has a mechanism for this against `COMPLETE` views (`requested_population_filters`, Step 6E3B2 Part 6) which now also covers `LIMITED_COMPLETE` views identically since the gate was widened generically — but verifying that specific NL-interpretation path end-to-end for `LIMITED_COMPLETE` is an orchestrator/answering-layer concern beyond this module's pure-computation scope.

## Part 5 — Usability vs. authority (separated concepts, confirmed already distinct)

This codebase already kept these as separate fields before this step — `authority_class` (schema_object_authority), `completion_status`/`lineage_complete` (profile completeness), `secret_columns`/PII categories (secret safety), `population_restrictions`/`suitability.population_restriction` (population scope), and `classify_profile_state`'s own return value (execution eligibility) are five genuinely distinct fields today, not collapsed into one verdict. `LIMITED_COMPLETE` slots into this as a sixth, narrower-trust execution-eligibility value — it never requires `authority_class == CANONICAL_TABLE`/`PROVEN_EQUIVALENT_VIEW` (a framework-backed or ordinary `UNKNOWN`-authority view can be `LIMITED_COMPLETE`-executable, exactly as Part 5 asks), and a framework-backed view stays narrower-trust (`FRAMEWORK_BACKED_BUSINESS_VIEW`, not promoted to canonical) regardless of its profile state.

## Part 6 — PII policy (unchanged, reconfirmed)

Not modified in this step. `core.dictionary.pii_detector`'s restricted-category vocabulary (`password`, `secret`, `token`, `cvv`, connection strings, encryption keys, …) is unconditionally narrower than general PII — SSN/passport/bank/card/name/email/phone/DOB remain informational-only and are never masked or excluded from coverage on their own; `tests/test_pii_secret_no_leak_sentinel.py` (unmodified, still green) continues to assert no raw PII/secret value is ever logged, cached, or persisted by this module.

## Part 7 — Full isolated re-run (240 views, source 10004)

Run via `python -m docs.validation.run_view_profiling_phase7_step6e3b3_simulation --source-id 10004 --user-id 28` against a fresh backup-API snapshot.

| state | before (6E3B2) | after (6E3B3) |
|---|---:|---:|
| `COMPLETE` | 91 | **116** |
| `LIMITED_COMPLETE` | n/a (didn't exist) | **33** |
| `LINEAGE_INCOMPLETE` | 107 | **22** |
| `UNSAFE` | n/a (didn't exist) | **27** |
| `TYPED_FAILURE` (= old `PARSE_ERROR`) | 42 | **42** (unchanged — see Part 1/8: requires a live SQL Server re-read, out of this step's scope) |
| **Total executable (`COMPLETE`+`LIMITED_COMPLETE`)** | **91 (37.9%)** | **149 (62.1%)** |

Executable-for-purpose, among the 149 executable views: **detail 81, count 137, aggregation 96, join 35** (a view can count toward more than one purpose).

Original 91 views with `authority_class == UNSAFE_DEPENDENCY` **grew to 121** once the full 240 were re-classified under this step's own (unchanged) `schema_object_authority` pass — the 91/107/42 baseline and this 121 both come from the same snapshot, so the discrepancy is simply that the user-supplied baseline reported completion-state counts, not authority-class counts, from an earlier pass; re-deriving authority fresh here is the authoritative figure for this report. Breakdown of all 121:

| outcome | count |
|---|---:|
| Framework-backed and executable (`COMPLETE`, via the pre-existing `classify_framework_backed_eligibility` widening) | 77 |
| Limited executable (`LIMITED_COMPLETE`, new in this step) | 13 |
| Truly unsafe (`UNSAFE`, new in this step — proven secret-exposure risk) | 27 |
| Unresolved (`LINEAGE_INCOMPLETE`, generic incompleteness, not proven unsafe) | 4 |

### The 10 required proof points (evidence gathered from the real CCPP snapshot, pure computation, no live connection)

1. **`dbo.vw_StudentOverview` detail query succeeds**: profile state `COMPLETE`, `suitability.suitable_for_detail = True`. ✅ Proven via the profile itself — `sql_validation` permits any detail query referencing it.
2. **Its naive `COUNT(*)` fails**: `duplication_risk = LIKELY_FANOUT`, `required_distinct_key = 'StudentID'`. `sql_validation._validate_view_suitability`'s existing (unmodified) naive-count check rejects any `COUNT(*)`/`COUNT(1)` against it with that key unresolved. ✅
3. **The correct `COUNT(DISTINCT StudentID)` succeeds**: the same check only fires when `required_distinct_key.lower() not in distinct_counted`; a query that counts `DISTINCT StudentID` satisfies it. ✅ (existing, unmodified mechanism — not new in this step, reconfirmed still correct)
4. **A filtered view cannot answer an unrestricted population question**: `dbo.Vw_RegisteredUsers` carries a captured `population_restriction` (`LEN(FirstName) > 0 AND LEN(LastName) > 0`); `sql_validation`'s existing `requested_population_filters` check (Step 6E3B2 Part 6, unmodified, now also covers `LIMITED_COMPLETE`) rejects an unrestricted request against any view with restriction evidence. ✅ evidence captured; the check itself predates this step.
5. **A compatible filtered question can use that view**: the same existing mechanism accepts a request that declares a matching `requested_population_filters` entry. ✅
6. **An aggregate view can answer a compatible aggregate question**: `dbo.vw_ADF_CCS_BasecampProjects_Current` — `MAX`/`MIN` aggregates, grouped — is `LIMITED_COMPLETE` in the new catalog (catalog-confirmed grain/columns, not fully lineage-resolved) and passes the identical `suitable_for_aggregation` check `COMPLETE` views use. ✅ — a concrete example of `LIMITED_COMPLETE` restoring real coverage.
7. **Direct framework/authentication access fails** / **8. a view exposing an authentication secret fails**: `dbo.vw_aspnet_MembershipUsers` carries `secret_columns = ('FailedPasswordAnswerAttemptCount', …)` and is `COMPLETE` (lineage resolves fine) — yet `sql_validation`'s unconditional, pre-existing secret-column block still rejects it regardless of state, proving the separation of "profile completeness" from "secret safety" (Part 5) actually holds end-to-end. ✅
9. **A legitimate business view containing authorized PII succeeds unmasked**: `dbo.VW_PAYPAL_DISTINCT_TRANSACTIONS` carries `pii_categories = ('PAYEREMAIL:TIER2', …)`, no secret columns, state `COMPLETE` — selectable, and the value itself is never touched/masked anywhere in this module. ✅
10. **Missing/stale profiles fail closed**: unchanged, pre-existing logic (`profile is None → MISSING`; a schema-fingerprint/definition-hash/policy mismatch → `STALE`), re-covered by this step's own regression test (`test_typed_failure_profile_carries_current_secret_policy_version`) plus the pre-existing `MISSING`/`STALE` test coverage in `tests/test_sql_validation_secrets_and_suitability.py`. ✅

## Part 8 — Coverage gate

- **62.1% of all 240 views are executable** (`COMPLETE` + `LIMITED_COMPLETE`), up from 37.9% at the 6E3B2 baseline.
- **Legitimate-business-view coverage (estimate)**: treating the 27 structurally-proven-`UNSAFE` views as the only objectively non-business-safe subset, coverage of the remaining 213 is **149/213 ≈ 70.0%**. This is a conservative upper-bound estimate, not a precise figure — the 14 name-pattern-flagged possibly-deprecated views (Part 1) are still counted as "legitimate" here since their status isn't proven either way.
- **Every remaining blocked legitimate-view category, and whether it's resolvable deterministically:**

  | blocked category | count | deterministically resolvable here? | needs live SQL? | AI enrichment relevant? |
  |---|---:|---|---|---|
  | Truncated `definition_sql` (41 of the 42 `TYPED_FAILURE`, plus some fraction of the 22 remaining `LINEAGE_INCOMPLETE`) | 41+ | **No** — the stored text is genuinely incomplete; no amount of parser improvement can recover missing characters | **Yes — this is the #1 lever for further coverage gain.** Fixing `data/view_semantics_service.py`'s capture path to read the full `OBJECT_DEFINITION`/`sys.sql_modules.definition` (NVARCHAR(MAX), not the row-value-sampling path) and re-probing requires a live SQL Server connection, explicitly out of this step's scope | No — this is a correctness/capture-completeness issue, never a semantic one |
  | 1 genuine tokenizer edge case (`col > ='literal'`) | 1 | Possibly — a targeted sqlglot pre-normalization (collapse whitespace between comparison-operator characters before tokenizing) would fix this specific shape, but it's a single view; not attempted here to avoid a narrow, view-shaped patch | No | No |
  | Correlated/scalar subquery in a projection | up to 20 | **Partially** — a bounded, correlation-aware resolver could attribute some of these, but genuinely risks "inferring lineage from matching names" when the subquery references multiple candidate outer columns; deliberately deferred | No | No — still a structural problem |
  | PIVOT/UNPIVOT | 2 | Yes, with meaningfully more implementation (a dedicated PIVOT column-fan-out resolver) — deferred as low-volume/high-risk for this pass | No | No |
  | Dependency-closure secret-exposure risk blocking otherwise-eligible `LIMITED_COMPLETE` views | a subset of the 4 `UNSAFE_DEPENDENCY`→`LINEAGE_INCOMPLETE` views | **No**, not safely — proving a specific unresolved column is NOT the secret column it might be requires resolving it, which is the same underlying gap as the subquery case above | No | **No — explicitly a safety decision, never delegated to AI confidence; the validator enforces this, matching this step's own explicit instruction** |
  | 14 name-pattern-ambiguous (deprecated/snapshot-named) views | 14 | No — this is a business-knowledge question, not a parsing one | No | **Yes, for semantics only** (confirming/labeling business intent) — the existing `data/business_knowledge_service.py` mapping is the right tool, never used here as a safety override |

- **AI enrichment was not used, and is not needed, for any safety decision in this step** — `LIMITED_COMPLETE`/`UNSAFE` eligibility is 100% deterministic (catalog-confirmed facts and structural proofs only), matching the explicit instruction that AI confidence must never substitute for the validator's own enforcement.

Target of "full coverage of legitimate business views" is **not yet reached** — the dominant remaining blocker (definition truncation, ~41+ views) requires a live SQL Server read that is explicitly out of this step's scope, not a deterministic-parser gap. Recommended next step: a narrowly-scoped live fix to `data/view_semantics_service.py`'s capture path (read `OBJECT_DEFINITION`/`sys.sql_modules.definition` directly instead of reusing the value-sampling path), followed by a re-profiling pass — expected to recover the large majority of the remaining 42 `TYPED_FAILURE` views without any further profiler logic changes.

## Part 9 — Tests

New/updated tests, all passing:

- `tests/test_sqlservice_view_profiler.py`: 38 tests (was 24) — derived-table/CTE-through-derived-table resolution, qualified/unqualified multi-table star expansion, 3-way UNION flattening, CROSS APPLY, suspected-truncation flagging, `LIMITED_COMPLETE`/`UNSAFE` eligibility (eligible case, dependency-secret-blocked case, low-resolved-ratio-blocked case), and the `secret_policy_version` regression.
- `tests/test_sql_validation_secrets_and_suitability.py`: 44 tests (was 36) — `LIMITED_COMPLETE` now accepted through the identical suitability/secret-column enforcement `COMPLETE` gets; `UNSAFE` added to the parametrized non-executable-state matrix.
- `data/view_profile_service.py`: `_profile_to_row`/`_row_to_profile` extended for the three new `ViewProfile` fields (serialization fidelity only — no schema/migration change, see Part 11).

Suites re-run after these changes, all green: `test_sqlservice_view_profiler.py`, `test_sqlservice_schema_object_authority.py`, `test_data_view_profile_service.py`, `test_sql_validation_secrets_and_suitability.py`, `test_pii_secret_no_leak_sentinel.py`, `test_dictionary_view_profile_reconciliation.py`, `test_schema_frontier_framework_backed_refinement.py`, `test_sqlservice_fast_path_canonical_entity_rule.py`, `test_sqlservice_fast_path_plan_cache.py`, `test_view_semantics_service.py` — **202 tests, 0 failures**. A full-repository `pytest tests/` run (7,880 tests collected) was also executed; see the session's own final status for its outcome (frontend/JS tests are a separate toolchain and were not run as part of this Python-only change).

## Part 10 — Windows deployment/rollback plan

Drafted separately: `docs/validation/view_profiling_step6e3c_windows_deployment_plan.md`. Not executed. Reuses `data/migrate_data_dir.py`'s existing WAL-checkpoint → `shutil.copy2` → verify mechanism, adds a `Get-FileHash` SHA-256 checksum layer, and a restore procedure that quarantines (never deletes) the pre-rollback state. No `git checkout`/`reset`/`stash`, no `$(date)`, no assumed `sqlite3`/`sha256sum` binary.

## Part 11 — Stop point / untouched-environment proof

- `data/toolsmith.db` (the real local metadata DB): every read in this step went through a `sqlite3` **backup-API** copy into a disposable scratch file; `data.db.DB_PATH` was monkeypatched to that copy *before* importing any DB-touching module, in a separate process from the one editing code. The live file was opened read-only (`mode=ro`) exactly once, to take each snapshot.
- Live SQL Server (source 10004): **zero connections** — no module in this step's code path (`view_profiler.py`, `view_profile_service.py`, `sql_validation.py`, the simulation script) imports or calls anything under `core/connectors/`.
- `.env`, business mappings, frontend runtime, provider services: not read or modified.
- OpenAI/any LLM provider: zero calls — confirmed by inspection (no import of any provider client in the changed/added files).
- All code changes are local, uncommitted edits in the working tree, consistent with this step's "stop for review before 6E3C" instruction — nothing was committed or pushed.
