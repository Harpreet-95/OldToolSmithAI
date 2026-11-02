# Phase 7, Step 6E3B5 — final adjudication report

Scope: resolve as much of the remaining 57-view (28+2+27 reclassified since 6E3B4) non-executable backlog as is generically, deterministically possible, using only the complete definitions already captured and persisted in the isolated snapshot from Step 6E3B4. **No new live SQL Server connection was opened in this step** — every fix below is a pure-computation improvement to `core/sqlservice/view_profiler.py`/`core/sqlservice/schema_object_authority.py`, verified by rerunning the (already-local) isolated simulation. One further opportunity *would* require a new live, read-only, catalog-metadata-only connection — flagged explicitly at the end for approval, not executed.

## Part 1 — Reconciled 240-view state table

| state | count |
|---|---:|
| `COMPLETE` | 139 |
| `LIMITED_COMPLETE` | 44 |
| `LINEAGE_INCOMPLETE` | 21 |
| `UNSAFE` | 36 |
| `TYPED_FAILURE` | 0 |
| `MISSING` | 0 |
| `STALE` | 0 |
| **Total** | **240** |

**Executable: 183/240 (76.2%)**, up from 164/240 (68.3%) at the end of Step 6E3B4. Full per-view evidence (object FQN, state, typed reason, output-column count, dependency counts, restriction/grain status, semantic-enrichment presence, secret-exposure basis) was generated for all 240 views into scratch JSON (not reproduced row-by-row here per this step's own "do not display definition text or data values" instruction, and to keep this report a reasonable size) — summarized below. **No view's `definition_length` is quoted in this document**; lengths were already reported, by FQN, in Step 6E3B4's own report.

Of the 57 non-executable views: **49 have an ACTIVE semantic-enrichment record**, 8 do not. 240/240 views appear somewhere in this source's `query_execution_log` history — this project's own validation history is extensive enough (14,336 logged executions) that this particular signal doesn't discriminate between views and was not useful for adjudication; it's reported for completeness, not as a meaningful filter.

## Part 2 — Proof of the (now 36, down from 48) `UNSAFE` classifications

**Root fix (the actual technical core of this step)**: Step 6E3B3/6E3B4's `UNSAFE` check flagged a view whenever **any** table anywhere in its **entire transitive dependency closure** carried **any** secret-shaped column — regardless of whether that table had anything to do with the view's own actually-unresolved output columns. This conflated Category C (internal dependency only, never projected) with genuine risk, exactly the over-blocking this Part asked to eliminate.

**Fix**: `OutputColumnLineage` gained a `candidate_source_fqns` field — for each unresolved output column, the *real, bounded* set of tables it could plausibly come from (not the whole closure). Precise for ambiguous-unqualified/no-matching-column/ambiguous-star cases (the actual FROM/JOIN alias set); `None` ("genuinely opaque" — a scalar subquery, an unregistered alias) conservatively falls back to the full dependency closure *only when that closure actually contains a secret-shaped table*; `()` ("proven zero column reference") can never carry risk. `UNSAFE` now requires, for each unresolved column, that a secret-bearing table is actually in its own candidate set — an unrelated framework dependency elsewhere in the view no longer triggers it. Verified: **7 views moved out of `UNSAFE`** on this fix alone (48→41), confirmed by two new tests proving both directions (`test_unrelated_secret_bearing_dependency_does_not_block_limited_complete`, `test_secret_bearing_dependency_within_actual_candidate_set_still_blocks`).

**Secondary precision fix**: `secret_columns` (actual output exposure) now checks the column's real *source* name (via `source_refs`), not only its output alias — `SELECT PasswordHash AS Foo` is now caught even though "Foo" alone isn't secret-shaped (`test_output_alias_does_not_hide_a_restricted_source_column`).

### A/B/C categorization of all 36 remaining `UNSAFE` views

| category | count | views |
|---|---:|---|
| **A — actual output secret** | **2** | `dnnuser.vw_Portals`, `dnnuser.vw_PortalsDefaultLanguage` — both directly expose `ProcessorPassword` as a resolved, named output column (a DNN/DotNetNuke CMS portal payment-processor credential, not business data) |
| **A-adjacent — secret projection cannot be ruled out, lineage genuinely unresolved** | **34** | every other `UNSAFE` view — each has ≥1 output column whose *specific, bounded* candidate source set (not the whole closure) includes a table carrying a secret-shaped column (overwhelmingly `dnnuser.Users`/`dbo.aspnet_*` identity tables that these views genuinely JOIN directly, not a transitive accident) |
| **B — ordinary/sensitive PII alone** | **0** | none — PII alone was never sufficient under either the old or new logic; `core.dictionary.pii_detector`'s own restricted-category vocabulary already excludes name/email/phone/SSN/bank/card from "secret" |
| **C — internal dependency only, not projected** | **0 remaining** (7 corrected out of `UNSAFE` this step) | no longer possible by construction — this is exactly what Part 2's fix eliminated |

For the 34 A-adjacent views, the dominant unresolved-reason is `nested_subquery_column_source` (10) and `column_not_found_on_any_known_source` (14) — i.e., a scalar/correlated subquery whose internals this profiler deliberately never parses (opaque by design, per Step 6E3B3's own "never infer lineage... when multiple sources are possible" rule), or an unqualified column this module's structural metadata genuinely cannot attribute to any of the view's own FROM/JOIN tables. Both are real, typed, evidence-backed "cannot rule out" cases — not generic failures, and not ordinary-PII-driven.

## Part 3 — Resolved 7 of the 26 `LINEAGE_INCOMPLETE` views via a generic fix (15 moved to `UNSAFE`'s precision fix instead, net 26→21 after both)

**Root cause found**: `core.sqlservice.schema_object_authority._table_fqn` (shared by both the authority pass and the view-lineage resolver) returned a table reference's **bare name with no schema prefix** whenever the SQL omitted the schema (`FROM SomeTable` instead of `FROM dbo.SomeTable`) — valid, common T-SQL (SQL Server resolves an unqualified table to the connection's own default schema, `dbo` for this project). The resulting FQN (`"sometable"`, not `"dbo.sometable"`) could never match `known_columns_by_fqn_lower`'s catalog-derived keys, stranding every column reference through that table as unresolved regardless of how complete the real column metadata was — a parser-level generic defect, not a missing-evidence one. Confirmed directly: `dbo.vw_BasecampActivity`'s entire FROM clause (`FROM ADF_BasecampActivity`, no prefix) produced alias_map `{'adf_basecampactivity': 'adf_basecampactivity'}` instead of `{'adf_basecampactivity': 'dbo.adf_basecampactivity'}`.

**Fix**: `_table_fqn` now defaults to `dbo` when no schema is present (`schema = table.db or "dbo"`). Purely additive, two new direct unit tests (`test_unqualified_table_reference_defaults_to_dbo_schema`, `test_schema_qualified_table_reference_unaffected`), plus an end-to-end view-profiler test (`test_unqualified_from_table_defaults_to_dbo_and_resolves`).

**Measured impact**: `COMPLETE` rose from 124→139 (+15), `LINEAGE_INCOMPLETE` fell from 31→21 (and `UNSAFE` also fell 41→35 in the same pass, before Part 2's own fix further reduced it — several views were blocked by *both* defects simultaneously).

### The remaining 21 `LINEAGE_INCOMPLETE` views, by exact mechanism

| mechanism | count | recoverable from already-captured evidence? |
|---|---:|---|
| `column_not_found_on_any_known_source` | 19 (12 as sole reason) | **No, not without new evidence** — every FROM/JOIN table's real-world column metadata is already loaded; the specific missing column (verified directly for one case: `vw_ADF_InternshipProgram`'s `ListURL`) genuinely isn't in this build's structural column metadata for any of the view's own tables. The complete view *definition* was never the gap here — a base/sibling table's own *column list* is. See the flagged next step below. |
| `unsupported_expression_shape` | 5 | No — these are projections sqlglot's walk finds no column reference in at all (computed/opaque expressions); not an evidence gap |
| `unresolvable_table_alias` | 2 | No — a FROM/JOIN source shape this module doesn't register at all (not a derived table, CTE, or plain table — e.g. a table-valued function call) |
| `union_branch_lineage_unresolved` | 2 | No — propagates from one of the above within a UNION branch |
| `nested_subquery_column_source` | 2 | No — deliberately unparsed (Step 6E3B3's own scope decision) |
| `cte_reference_beyond_resolution_depth` | 2 | No — propagates from one of the above inside a CTE |
| `star_expansion_ambiguous_multi_table_source` | 1 | No — a `SELECT *` landing on a target this module can't enumerate columns for |

None of these 21 are resolvable by a generic profiler-logic change alone; each has a specific, typed, evidence-backed reason already recorded (never a bare `PARSE_ERROR`).

## Part 4 — Both parser failures resolved

**Root cause** (identical shape for both): a whitespace-separated comparison operator (`col > ='literal'`, `col < = 10`) that SQL Server's own tokenizer accepts (resolves to `>=`/`<=`) but sqlglot's T-SQL tokenizer does not — confirmed by identical sqlglot error signatures (`Required keyword: 'expression' missing for <...GT/LT>`) at the exact line/column containing the pattern in both views.

**Fix**: `_normalize_split_comparison_operators`, a bounded, string/comment/bracket-aware text pass (handles single-quoted strings with doubled `''` escaping, `[bracketed identifiers]`, `--` line comments, `/* */` block comments — a literal value containing `"> ="` as text is never touched), collapsing exactly `> =`→`>=`, `< =`→`<=`, `! =`→`!=`, `< =`→`<>`. Applied **only as a fallback retry** after the unmodified text fails to parse — never changes anything about SQL that already parses, and the fact that normalization was needed is recorded as typed evidence (`parsed_after_split_comparison_operator_normalization`), never silently hidden. Six new tests, including two specifically proving string-literal/comment content is never altered.

**Result**: `dbo.VW_TWC_MASTER_STUDENT_LIST` and `dbo.vw_QS_MetricsDashboard_ClassScoreManagement` both now parse; `TYPED_FAILURE` count is **0** (down from 2, down from 42 at the Step 6E3B3 baseline). Neither reached `COMPLETE` outright (both still have genuine, separately-typed lineage gaps once parsed — `VW_TWC_MASTER_STUDENT_LIST` landed `UNSAFE`, `ClassScoreManagement` landed executable) — this was never claimed as a full fix, only an honest, precise parser fix per this Part's own instruction not to accept a generic `PARSE_ERROR` when a more precise reason is knowable.

## Part 5 — Business-view completeness

Using semantic enrichment presence (195/240 active), dependency/output-column evidence, grain, filters, and the Part 2 proof of no-exposed-secret as the defining evidence (never object names alone):

- **Security/authentication views excluded from the legitimate-business-view count**: the 2 proven Category-A views (`dnnuser.vw_Portals`, `dnnuser.vw_PortalsDefaultLanguage`) plus every view whose profile state is `UNSAFE` (36 total) — these are, by this step's own proof, views where a real secret either is exposed or cannot be ruled out.
- **Legitimate business views (estimate)**: 240 − 36 = **204**.
- **Executable legitimate business views**: **183**.
- **Blocked legitimate business views**: **21** (exactly the `LINEAGE_INCOMPLETE` set — see Part 3's table for each one's specific, evidence-backed blocker).
- **Coverage: 183/204 ≈ 89.7%.**

Target of 100% is not yet reached. Every remaining blocker has a specific, typed, evidence-backed reason (Part 3's table) — none is a generic catch-all, and 19 of the 21 share one concrete, identified root cause (incomplete base-table column metadata for a specific set of tables) that is plausibly fixable with one further bounded, read-only catalog query — flagged for approval below, not executed.

## Part 6 — Re-run proof cases

1. **`vw_StudentOverview` works for names/enrolled classes**: `COMPLETE`, unchanged from Step 6E3B4. ✅
2. **Student names/contact fields return unmasked when requested**: unchanged — this module never masks a PII value; `VW_PAYPAL_DISTINCT_TRANSACTIONS` (`pii_categories` populated, `own_secret_columns=[]`) remains `COMPLETE`. ✅
3. **Naive student `COUNT(*)` over enrollment grain fails**: unchanged, pre-existing `sql_validation` mechanism, untouched by this step. ✅
4. **Correct `COUNT(DISTINCT ...)` succeeds**: unchanged. ✅
5. **Restricted-population views cannot answer unrestricted questions**: unchanged mechanism (`requested_population_filters`), now also covers more views since more are executable. ✅
6. **Aggregate views work only for compatible aggregates**: unchanged `ViewSuitability` machinery. ✅
7. **Internal framework dependency alone does not block a business view**: **directly proven this step** — `vw_StudentOverview` has `dependency_has_secret_shaped_column=True` (it depends, directly or transitively, on a table carrying a secret-shaped column) yet remains `COMPLETE`; `dnnuser.vw_Lists/Modules/Tabs/TabModules` (framework-backed, previously at risk of being swept into `UNSAFE` under the old blanket rule) are `LIMITED_COMPLETE` with zero dependency-secret-risk now correctly computed. ✅ — the headline proof of this step's Part 2 fix.
8. **Direct authentication-table access fails**: unchanged — Tier-3/4 governance re-check untouched. ✅
9. **Actual secret-bearing views fail**: `dnnuser.vw_Portals`/`vw_PortalsDefaultLanguage` (`ProcessorPassword`) and `dbo.vw_aspnet_MembershipUsers` (9 distinct password/PIN/security-answer columns, now more completely detected via the source-column-name fix) all remain blocked. ✅
10. **Ordinary PII does not trigger secret rejection**: unchanged — `is_restricted_category` never matches name/email/phone/SSN/bank/card; reconfirmed via `VW_PAYPAL_DISTINCT_TRANSACTIONS`. ✅
11. *(13 in the user's own numbering; 12 below)* **Missing/stale/incomplete profiles fail closed**: unchanged, pre-existing `classify_profile_state` precedence, untouched structurally by this step (only the eligibility computation feeding into it changed, not the fail-closed ladder itself). ✅
12. **No PII or full view definition enters logs or plan cache**: reconfirmed — this step added zero new logging paths; the Step 6E3B4 test (`test_no_definition_text_ever_appears_in_logs`) and the pre-existing `test_pii_secret_no_leak_sentinel.py` both still pass unmodified. ✅

## Part 7 — Final isolated simulation

```text
6E3B2:  91 executable (37.9%)
6E3B3: 149 executable (62.1%)
6E3B4: 164 executable (68.3%)
6E3B5: 183 executable (76.2%)
```

- **Executable percentage of all 240 views: 76.2%.**
- **Executable percentage of legitimate business views (204, Part 5): 89.7%.**
- Executable-for-purpose: detail 101, count 171, aggregation 122, join 40.
- Every one of the 57 blocked views has a specific, typed, evidence-backed reason (Parts 2 and 3's own tables) — no generic/unexplained block remains.

## Part 8 — Tests

Focused: `tests/test_sqlservice_view_profiler.py` grew from 50→62 tests this step (candidate-source precision ×4, dbo-default-schema ×2, operator-normalization ×6). `tests/test_sqlservice_schema_object_authority.py` grew by 2 (`_table_fqn` direct unit tests). All pass.

Directly/transitively affected suite (18 files, same set as Step 6E3B4's own run plus the two files touched this step): **382 passed, 2 failed** — both the identical pre-existing, unrelated failures reconfirmed across every run this entire engagement (`test_investigation_service.py::test_view_definition_sql_unsupported_dialect_returns_null`, `test_semantic_contract_service.py::test_discovery_returns_no_candidate_on_genuine_ambiguity`).

**Background full-repository baseline (Step 6E3B3, reported accurately, not re-claimed as "global green")**: **7,820 passed, 57 failed** — zero of the 57 attributed to Step 6E3B3 (verified by direct file-content inspection in that step).

**New failures introduced by Step 6E3B4 or 6E3B5: none.** Every failure observed in every targeted re-run this step (always exactly the same 2) was already present in the Step 6E3B3-era full-suite baseline, confirmed both by appearing verbatim in that 57-failure list and by inspecting that neither failing test's file or assertion touches anything either step changed.

The full 7,880-test suite was **not** re-run a third time in this step (the targeted 382-test set covers every file either step could plausibly affect, including everything newly added), consistent with this project's own guidance against repeated expensive full-suite runs.

## Part 9 — Final deployment preflight design (Windows, updated)

`docs/validation/view_profiling_step6e3c_windows_deployment_plan.md` needs one more addition for the required 16-step sequence (backend-serving-gap prevention) — summarized here rather than duplicating the full script a third time; the existing Step 2/2b/2c/2d (backup + checksum + config snapshot + code snapshot) and Step 4/5 (DB/code restore) sections already satisfy steps 5, 6 (partially), and 16 of the required sequence:

1. **Prevent backend auto-restart** — if a process manager/service wrapper is configured (Task Scheduler, NSSM, a systemd-equivalent), disable its auto-restart policy before Step 2; document the exact mechanism in use (not assumed here — environment-specific).
2. **Stop backend completely** — existing Step 1, unchanged.
3. **Verify no process holds the SQLite file** — new: `Get-Process | Where-Object { ... }` plus an explicit `sqlite3`-free check via Python (`sqlite3.connect(path, timeout=1)` + `PRAGMA wal_checkpoint` succeeding is itself proof nothing else holds a write lock, matching `migrate_data_dir.py`'s own existing check).
4. **Capture effective configuration** — existing Step 2c (`.env` snapshot + `git_status_at_backup.txt`), extended to also capture `core.config`'s own resolved values (feature flags, `VIEW_SEMANTIC_ENRICHMENT_GENERATION_ON_CONNECT_ENABLED`, etc.) via a one-off read-only script dump, never the raw `.env` secrets themselves in that dump.
5. **Create and verify a recoverable SQLite backup** — existing Step 2/2b, unchanged.
6. **Apply migration using a one-off process, never starting the API server** — new: `python -m data.models` (or an equivalent one-off `init_db()` invocation) run directly, never via `uvicorn`/`api.app`; this applies the two additive `view_semantic_evidence` columns (Step 6E3B4) without ever opening a listening port.
7. **Backfill all 240 profiles** — the (not-yet-written) live-connected equivalent of this step's own isolated-snapshot simulation, run against the real DB only after gates 1-6 pass.
8. **Verify every profile and typed failure** — re-run the same `docs/validation/run_view_profiling_phase7_step6e3b3_simulation.py` script against the now-real DB (read-only verification pass) and diff its state-count table against this report's own Part 1 table; any divergence blocks progression.
9. **Rebuild only changed retrieval documents** — gated on `document_hash` (content-addressed, already the existing mechanism — see Step 6E3B4's own Part 3).
10. **Re-embed only changed documents** — same gate, one layer up (`core.sqlservice.embedding_coordinator`).
11. **Verify mappings and business knowledge unchanged** — a read-only row-count/checksum comparison of `sql_view_semantic_enrichments`/business-knowledge tables before vs. after, asserting zero unintended writes.
12. **Run offline validation** — the existing acceptance-suite/golden-set scripts already in `docs/validation/`, in offline/read-only mode.
13. **Start backend** — only after 1-12 all pass.
14. **Confirm effective routing** — a bounded `/health`-style check plus one read-only confirmation query against the live app process (not this script).
15. **Run bounded cold/warm smoke tests** — a small, fixed question set (not the full acceptance suite) against the running backend.
16. **Restore the verified backup and prior runtime configuration if any gate fails** — existing Step 4/5 (DB + code restore via per-file checksum manifest), re-enable auto-restart (reverse of step 1) as the final restore action.

Still no `stash`/`reset`/`checkout` anywhere in this sequence; still PowerShell-only, `Get-Date`/`Get-FileHash`-based, no assumed `sqlite3`/`sha256sum` binary.

## Part 10 — Stop point

No migration, backfill, embedding generation, live database write, provider call, `.env` change, or backend restart was executed in this step. Every write this step made landed in the same disposable snapshot copy Step 6E3B4 created (`toolsmith_snapshot_for_live_read.db` in a scratch temp directory) — the real `data/toolsmith.db` was never opened for writing. All code changes remain local, uncommitted edits.

### One flagged next-step opportunity requiring approval (per this step's own instruction)

19 of the 21 remaining `LINEAGE_INCOMPLETE` views (and a further unknown number of the 34 A-adjacent `UNSAFE` views) share a single root cause: incomplete **base-table column metadata** (not view-definition truncation — already fixed — and not a profiler-logic gap) for a specific set of dependency tables, confirmed directly for one case (`dbo.ADF_CCS_BasecampProjects`'s `ListURL` column, verified present on a related-but-different table, absent from this build's own structural metadata for the table the view actually references). A single additional **bounded, read-only, catalog-metadata-only** query — `sys.columns` joined to `sys.tables`/`sys.views`/`sys.schemas`, mirroring the exact pattern already used for the Step 6E3B4 view-definition capture, scoped to the same 240-view dependency closure, no business-row access, no DDL/DML — would very plausibly resolve most of this remaining gap. **Not executed in this step**, per this step's own explicit instruction to stop and request approval before any further live connection.

Stopping here for explicit Step 6E3C approval, as instructed.

---

## Addendum — authorized follow-up (Step 6E3B6): the flagged sys.columns read, and what it actually found

The user authorized the one flagged live-read opportunity above. Executed with the identical pre-connection safety pattern as Step 6E3B4 Part 5 (owner/connector/schema-fingerprint verification, `DB_PATH` pinned to the existing isolated snapshot, asserted `!=` the live file) — one bounded, read-only query (`sys.columns` joined to `sys.objects`/`sys.schemas`/`sys.tables`, user tables only, no business row ever touched) captured **13,729 (schema, table, column) triples across 1,166 base tables** in a single round trip.

**Honest result: this hypothesis was wrong.** Merging the catalog-complete column set into `column_names_by_fqn_lower` **widened metadata for zero tables** — the existing retrieval-document structural metadata was already catalog-complete. Re-running the simulation with the merged data produced **no change at all** (183/240, identical state breakdown). This is reported as a genuine negative result, not hidden: the live read definitively ruled out "incomplete base-table column metadata" as the cause of the remaining `column_not_found_on_any_known_source` cases.

**The negative result pointed at the real cause.** Direct inspection of one case (`vw_ADF_InternshipProgram`'s unresolved `ListURL`) showed the real SQL Server text: `ListURL` is projected by the view's *outer* query, unqualified, from a `LEFT JOIN` to a **derived table** (`(SELECT d.ProjId, d.ListURL FROM ... WHERE ...) x`) — never through a real FROM/JOIN table at all. `_resolve_projection`'s unqualified-column path only ever checked real tables (`alias_map`) for a name match; it never considered a derived-table/CTE **scope** as a candidate source for an *unqualified* reference (only a qualified one, `x.ListURL`, was ever checked against scopes). This is a genuine, generic gap distinct from everything fixed earlier in this step.

**Fix**: `_resolve_projection`'s unqualified-column resolution now checks real tables **and** scopes together — if the column name is uniquely satisfied by exactly one combined candidate (a real table *or* a derived-table/CTE's own already-resolved output), that is resolved exactly like the existing qualified-scope path (propagating the scope's own `source_refs`/confidence); more than one combined candidate is `LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED` with the full combined candidate set recorded (never a guess either way). Two new tests (`test_unqualified_column_resolves_through_derived_table_scope`, `test_unqualified_column_ambiguous_between_real_table_and_scope_stays_unresolved`), both passing; full view-profiler suite now 64/64; targeted 384-test regression set: 384 pass, same 2 pre-existing/unrelated failures, zero new.

### Corrected final state (supersedes the Part 1/7 tables above)

| state | count |
|---|---:|
| `COMPLETE` | 161 |
| `LIMITED_COMPLETE` | 44 |
| `LINEAGE_INCOMPLETE` | 12 |
| `UNSAFE` | 23 |
| `TYPED_FAILURE` | 0 |
| **Total** | **240** |

```text
6E3B2:  91 executable (37.9%)
6E3B3: 149 executable (62.1%)
6E3B4: 164 executable (68.3%)
6E3B5: 205 executable (85.4%)
```

- **Executable percentage of all 240 views: 85.4%.**
- **Legitimate business views (240 − 23 proven-`UNSAFE`): 217. Executable: 205. Coverage: 205/217 ≈ 94.5%.**
- Executable-for-purpose: detail 111, count 185, aggregation 140, join 48.
- `UNSAFE` fell from 36→23 (still exactly 2 proven Category A — `dnnuser.vw_Portals`/`vw_PortalsDefaultLanguage`'s `ProcessorPassword`; the other 21 remain legitimate "cannot rule out, lineage genuinely unresolved" cases, dominated by scalar/correlated subqueries this profiler still deliberately never parses into).
- `LINEAGE_INCOMPLETE` fell from 21→12; remaining mechanisms: `column_not_found_on_any_known_source` (7, now genuinely a structural-reference gap, not a scope-resolution one), `cte_reference_beyond_resolution_depth` (3), `unqualified_column_multiple_candidate_sources` (2, the NEW, more precise ambiguity reason this fix introduces when a column genuinely has 2+ real combined candidates), `unresolvable_table_alias` (2), `union_branch_lineage_unresolved` (2), `nested_subquery_column_source` (1), `star_expansion_ambiguous_multi_table_source` (1).

No further live connection was used beyond the one authorized query. No write to the real `data/toolsmith.db` occurred — the sys.columns result was held in memory and in the isolated snapshot's own disposable scratch table only (`step6e3b6_catalog_columns_scratch`, inside the Step 6E3B4 snapshot copy, never the real database). Stopping here, final Step 6E3C approval still pending.
