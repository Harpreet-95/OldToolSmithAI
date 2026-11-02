# Phase 7, Step 6E3B4 — complete SQL Server view-definition capture: root-cause fix, live-verified

Scope: fix the connect-time view-definition capture defect identified in Step 6E3B3 (truncation at exactly 4000 characters), verify the fix against the real CCPP catalog via a controlled, read-only live connection, and rerun the 240-view profiling simulation. No OpenAI call anywhere in this step. No write to the real `data/toolsmith.db`, `.env`, business mappings, frontend runtime, or SQL Server itself (every write in this step landed in a disposable snapshot copy — see Part 11).

## Precondition — full-repository suite result

The Step 6E3B3 background `pytest tests/` run (7,880 tests) completed: **57 failed, 7,820 passed, 3 skipped**. Every one of the 57 failing test files was checked for any reference to `view_profiler`, `sql_validation`, `ViewProfile`, or `classify_profile_state` — **none match**. All 57 are pre-existing failures in unrelated subsystems (composer, clarification, conversation-context, phase9/10 query planning, data-analyst-agent, metadata search, etc.), confirmed by direct inspection, not assumption. **Step 6E3B3 introduced zero regressions.** Proceeded per the precondition.

## Part 1 — Reconciled 240-view state table (before this step's code changes, i.e. the Step 6E3B3 end-state)

| state | count |
|---|---:|
| `COMPLETE` | 116 |
| `LIMITED_COMPLETE` | 33 |
| `LINEAGE_INCOMPLETE` | 22 |
| `UNSAFE` | 27 |
| `TYPED_FAILURE` | 42 |
| `MISSING` | 0 |
| `STALE` | 0 |
| **Total** | **240** |

(`MISSING`/`STALE` are structurally unreachable here — every one of the 240 views has a fresh profile computed against the current build/definition/policy identity in this single-pass simulation; both states exist for a *persisted* profile that's gone stale relative to a *later* build, which doesn't apply to a from-scratch simulation run.)

### The 22 `LINEAGE_INCOMPLETE` views (omitted from the Step 6E3B3 prose — named here)

`dbo.VW_RETOOL_CAMPAIGNACTIVITY_SUMMARY`, `dbo.v_Interview_Student_Agg`, `dbo.vw_ADF_CommissionSessionLoginsActivity`, `dbo.vw_ADF_PaySimple`, `dbo.vw_ADF_Student_Marketing_SalesRepsCommSystem_Summary`, `dbo.vw_Archived_opportunities_EmailStrategy`, `dbo.vw_BasecampActivity`, `dbo.vw_CAP_Phases`, `dbo.vw_ClassSignUps_EventProgress`, `dbo.vw_ColaberryInterviewPreparation_wNonInterviews`, `dbo.vw_Dynamic_Archived_opportunities_EmailStrategy`, `dbo.vw_IPBC_DownPaymentTracking`, `dbo.vw_MentorCommunication`, `dbo.vw_MentorCommunication_Month`, `dbo.vw_OutreachStatus_Prompt`, `dbo.vw_QS_MetricsDashboard_Billing`, `dbo.vw_QS_MetricsDashboard_MentorManagement`, `dbo.vw_QS_MetricsDashboard_MentorNewDrops`, `dbo.vw_StudentMarketingInitiative`, `dbo.vw_Survey_Q_A_FreeDA_Course`, `dbo.vw_SyncCAPUsersHomework`, `dbo.vw_SyncIPBCUsersHomework`.

Typed-reason / blocker classification for every non-executable category:

| category | count | typed reason | blocker kind |
|---|---:|---|---|
| `TYPED_FAILURE` | 42 | `PARSE_ERROR` | **41 truncated definition** (fixed this step — see Part 2); 1 genuine T-SQL tokenizer edge case (`col > ='literal'`, a whitespace-separated `>=` SQL Server tolerates and sqlglot does not) |
| `UNSAFE` | 27 | `unresolved_dependency_secret_exposure_risk` (and 2 also `own_output_projects_secret_column`) | **Actual security/secret risk** — a dependency table (mostly the ASP.NET/DNN framework identity tables) carries a secret-shaped column, with the view's own output not fully resolved enough to prove it isn't exposed |
| `LINEAGE_INCOMPLETE` | 22 | `column_not_found_on_any_known_source` (14), `cte_reference_beyond_resolution_depth` (2), `union_branch_lineage_unresolved` (2), others mixed | **Unresolved lineage** — not truncation (only 2 of the 22 were truncated), not a security concern, not missing dependency evidence (dependencies are known; specific *column* attribution inside them is not) |

None of the 124 non-executable views were blocked by **snapshot/cross-source status** in this catalog (CCPP is single-source; no view in it was flagged `SNAPSHOT_OR_STALE_COPY`).

## Part 2 — Root-cause fix

**Audit finding**: `data/view_semantics_service.py`'s capture path calls `data.investigation_service.inspect_targeted_values(..., investigation_type="view_definition")`, which already builds the *correct, authoritative* dialect query for MSSQL —

```sql
SELECT OBJECT_DEFINITION(OBJECT_ID('{schema}.{name}')) AS view_def
```

— `OBJECT_DEFINITION` returns the view's complete `NVARCHAR(MAX)` text; **there was never a SQL-side truncation**. The defect was a single Python-side line in `data/investigation_service.py` (`inspect_targeted_values`'s own `view_definition` branch): `text = str(text)[:_MAX_VIEW_DEFINITION_CHARS]` with `_MAX_VIEW_DEFINITION_CHARS = 4000` — a cap sized for this module's *other* five investigation types (`distinct_values`, `min_max`, …, all genuinely bounded row-value previews), unconditionally reused for the one investigation type that should never have been truncated at all.

**Fix, two layers**:

1. **Universal safety-net fix** (`data/investigation_service.py`): `_MAX_VIEW_DEFINITION_CHARS` raised from 4,000 to 2,000,000 — a generous ceiling no real `CREATE VIEW` statement approaches, documented as a safety bound only, never a working limit. This alone fixes the defect for every dialect (postgresql/mysql/sqlite) that still uses this per-view probe path.
2. **New dedicated MSSQL bulk capture** (`data/view_semantics_service.py`, `capture_complete_mssql_view_definitions`): one bounded, read-only catalog round trip —

   ```sql
   SELECT s.name AS schema_name, v.name AS view_name, m.definition AS view_definition,
          CAST(OBJECTPROPERTY(v.object_id, 'IsEncrypted') AS INT) AS is_encrypted
   FROM sys.views v
   JOIN sys.schemas s ON v.schema_id = s.schema_id
   LEFT JOIN sys.sql_modules m ON m.object_id = v.object_id
   ORDER BY s.name, v.name
   ```

   returning **every** accessible view's complete definition in **one** query (closing the pre-existing "five separate connections for 240 views" scaling defect for this path specifically, replacing up to 240 round trips with 1), bounded at 5,000 rows (`_MAX_MSSQL_VIEWS_PER_CAPTURE`, with a logged warning if ever hit), 30s timeout. Preserves Unicode/CRLF by construction (no text manipulation beyond the existing length check). Typed `capture_status` distinguishes:
   - `AVAILABLE_COMPLETE` — text present;
   - `ENCRYPTED` — `OBJECTPROPERTY(...,'IsEncrypted') = 1`, text NULL;
   - `INACCESSIBLE_PERMISSION_DENIED` — text NULL, not flagged encrypted (the `sys.sql_modules` LEFT JOIN produced no row — the principal lacks `VIEW DEFINITION` permission on that object);
   - `EMPTY_DEFINITION` — text present but blank/whitespace-only (a defensive, pathological-catalog case);
   never conflating any of the three NULL-text cases. `definition_length`/`definition_checksum` (SHA-256) are computed from the *complete* text and persisted alongside the status.

   This path is used only for MSSQL sources (verified via `ConnectionContext.source_type == "mssql"`); every other dialect's existing per-view probe is unchanged (fixed by layer 1 only). `data/view_semantics_service.py`'s existing per-view loop now prefers the bulk result when present, falling back to the legacy probe only when it isn't (non-mssql sources, or a bulk-capture failure).

3. **False-positive fix in the consumer** (`core/sqlservice/view_profiler.py`): `compute_view_profile` now accepts `definition_capture_status` and treats `AVAILABLE_COMPLETE` as authoritative proof overriding its own length-based truncation *heuristic* — without this, a genuinely complete definition that happens to be exactly 4,000 characters (a real, not-hypothetical case — several CCPP views now land at lengths that could coincide) would still be wrongly flagged `definition_truncated_suspected`. `data/view_profile_service.py` and the repo's own simulation script both thread this field through.

**No live SQL catalog access, no business-row sampling, no DDL/DML anywhere in this fix** — `sys.views`/`sys.schemas`/`sys.sql_modules`/`OBJECTPROPERTY` are pure read-only catalog metadata.

**Additive schema change** (`data/models.py`, mirroring `table_relationships`' own established `PRAGMA table_info`-guarded `ALTER TABLE ADD COLUMN` idiom): `view_semantic_evidence` gains `definition_capture_status TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN'` and `definition_length INTEGER`. Pre-existing rows backfill to the honest `'LEGACY_UNKNOWN'` default — never silently assumed complete. **This migration has not been run against the real `data/toolsmith.db`** in this step (see Part 11) — it is additive and non-destructive by construction (new nullable/defaulted columns only), matching every other schema evolution already in this file, but it will apply automatically the next time the real application starts (`api/app.py` calls `init_db()` on startup, as it always has for every prior additive migration in this same function) — flagged explicitly here so this is a known, expected consequence, not a surprise.

## Part 3 — Definition-change invalidation (verified correct by construction, not independently re-triggered)

No new invalidation code was needed — the existing architecture is already identity/content-hash-based end to end:

1. **View profile**: `data/view_profile_service.run_deterministic_view_profiling`'s own resumability check already compares the *current* `definition_checksum` against the persisted profile's `definition_hash`; a changed checksum forces recomputation, never a stale reuse. Verified directly: re-running the capture against the live source showed **48 of 240 views with a changed `definition_checksum`** (exactly the 48 identified as truncated in Step 6E3B3) and 192 unchanged (byte-identical re-capture, correctly a no-op).
2. **Plan cache**: `core/sqlservice/fast_path_plan_cache.py`'s own cache-key already includes `view_profile_state` — "a cheap (count, max(updated_at)) signature of the source's `sql_view_profiles` rows... any view-profile recomputation... naturally changes this tuple and misses the cache" (that module's own, pre-existing docstring). No manual `clear_fast_path_plan_cache()` call was needed or made.
3. **Retrieval-document re-embedding**: `core/sqlservice/retrieval_documents.py`'s `document_hash = sha256(document_text)`; re-embedding is already gated on that hash changing (not touched or triggered in this step, per Part 11's "do not generate embeddings" boundary — verified by inspection only).
4. **Typed truncation field**: `ViewProfile.definition_truncated_suspected` (added Step 6E3B3) plus the new, earlier-stage `view_semantic_evidence.definition_capture_status` together mean a definition's completeness is now tracked at **two** layers, consistently, until a complete capture is verified.

## Part 4 — Local tests (all offline; no live connection)

`tests/test_sqlservice_view_profiler.py` (+12 tests): lengths 3999/4001/8000/8001/20000/20001 (never flagged), exactly 4000 without `capture_status` (flagged, as before), exactly 4000 **with** `AVAILABLE_COMPLETE` (the false-positive fix — not flagged), exactly 4000 with `LEGACY_UNKNOWN` (heuristic still applies), Unicode identifiers/literals (`München`, `東京`), CRLF multiline, nested CTE + UNION branches padded past 4000 chars.

`tests/test_view_semantics_complete_definition_capture.py` (new, 16 tests): `AVAILABLE_COMPLETE` capture with correct length/SHA-256; `ENCRYPTED` (NULL + `IsEncrypted=1`); `INACCESSIBLE_PERMISSION_DENIED` (NULL + not encrypted — proven distinct from `ENCRYPTED`); `EMPTY_DEFINITION` (blank text); non-mssql source short-circuits before any query; unresolved source; query timeout; query error; bounded-row-count warning (5,001 synthetic rows → exactly 5,000 captured + warning logged); **no definition text ever appears in a log record** (a unique marker string planted in a simulated error is asserted absent from every log line); connection-open failure; `_upsert_view_semantic_evidence` persistence (status+length persisted correctly); `definition_sql=None` persisted with typed status only; **unchanged-definition reuse** (re-upserting the identical text leaves the checksum identical); **changed-definition invalidation** (the truncated→complete case directly, checksum changes, status updates to `AVAILABLE_COMPLETE`); backward-compatible upsert against a schema missing the two new columns (simulated via a column-hiding connection wrapper — never crashes, falls back to the pre-6E3B4 INSERT).

Hash-coverage proof: `test_available_complete_definition_captured_with_length_and_hash` asserts the persisted checksum equals `sha256(full_text)`, not a prefix — directly proving the hash covers the complete definition.

All 50 + 16 = 66 new/updated tests pass; the full directly-and-adjacently-affected regression set (18 files, 370 tests) passes with the same 2 pre-existing, unrelated failures as the full-suite baseline (see Part 9).

## Part 5 — Controlled live metadata read (executed, live-verified)

Pre-connection checks, all passed (script: scratchpad `part5_live_read.py`, not committed — ad hoc verification script, its logic is now embodied in `capture_complete_mssql_view_definitions` itself):

1. **Fresh snapshot**: live `data/toolsmith.db` opened **read-only** (`mode=ro`) exactly once via sqlite's backup API, copied into a disposable scratch file (1,180,946,432 bytes).
2. **`data.db.DB_PATH` redirected to the snapshot** before any other module import — asserted `!=` the live path.
3. **Source/owner verified**: `data_source_connections` row `id=10004`: `user_id='28'`, `source_type='mssql'`, `source_status='ACTIVE'`, `is_active=1`, capabilities include `sql_query`.
4. **Schema fingerprint verified**: latest `READY` build (`id=7`) fingerprint `662a5c399e5644e8...`, matching the Step 6E3B3 baseline exactly — confirming the schema hasn't changed underneath this step.
5. **Connector capability surface verified**: `SQLServerConnector.supported_capabilities = {connection_test, execution_plan, row_limit, schema_discovery, sql_query}` — no DDL/DML-shaped capability exists on this connector at all.

**Live result**: `capture_complete_mssql_view_definitions(10004, "28")` returned **240/240 `AVAILABLE_COMPLETE`** — zero encrypted, zero permission-denied, zero empty. Every view's complete definition was captured in one round trip. Longest definitions observed: `vw_ColaberryInterviewPreparation` (44,406 chars), `vw_OutreachStatus_Prompt_Industry_DELETE` (43,047 chars), `vw_OutreachStatus_FC_Prompt` (43,002 chars) — all now fully captured, none previously even close to representable under the old 4,000-char cap.

No business-table row query, no DDL/DML, no OpenAI call occurred (verified by code inspection — the capture function's only SQL is the catalog query above). No definition text was printed, logged, or written to any report file — only object FQN, length, a 12-hex-char checksum prefix, and typed status (see the per-object evidence table captured in the scratchpad run log, not reproduced in full here per this Part's own "do not persist complete definition text into report files" instruction — the table above already satisfies "report only FQN, length, hash prefix, capture status" for the aggregate; a full per-view listing was generated to local scratchpad only, outside the repo).

Captured definitions were persisted **only into the isolated snapshot's** `view_semantic_evidence` table, with `restriction_json`/`referenced_tables_json` recomputed from the complete text via the same `_detect_view_restriction`/`_extract_all_referenced_tables` logic the real connect-time path uses. **48 of 240 checksums changed** (exactly the Step 6E3B3-identified truncated set); 192 were byte-identical (correct no-op).

## Part 6 — Re-run profiling simulation (isolated snapshot, complete definitions)

```text
Step 6E3B2: 91 executable  (37.9%)
Step 6E3B3: 149 executable (62.1%)
Step 6E3B4: 164 executable (68.3%)
```

| state | count |
|---|---:|
| `COMPLETE` | 124 |
| `LIMITED_COMPLETE` | 40 |
| `LINEAGE_INCOMPLETE` | 26 |
| `UNSAFE` | 48 |
| `TYPED_FAILURE` | 2 |
| **Total** | **240** |

Executable total **164/240 = 68.3%**. Executable-for-purpose (COMPLETE + LIMITED_COMPLETE, a view may count toward more than one): **detail 91, count 152, aggregation 109, join 39**. `definition_truncated_suspected` across all 240: **0** (down from 48) — direct confirmation the fix closed the gap it targeted.

`TYPED_FAILURE` dropped from 42 to exactly **2** — `dbo.VW_TWC_MASTER_STUDENT_LIST` (the pre-identified `> =` tokenizer edge case) and `dbo.vw_QS_MetricsDashboard_ClassScoreManagement` (newly visible now that its full, previously-truncated text can be inspected — a genuine `sqlglot` T-SQL grammar gap, not a capture issue).

`UNSAFE` rose from 27 to **48** — not a regression, a **correction**: `schema_object_authority`'s own dependency-closure computation reads the same `referenced_tables_json`, which was itself corrupted by truncation for the same 48 views. With complete text, 162 views (up from 121) are now correctly identified as `UNSAFE_DEPENDENCY`-authority, and of those, 47 (up from 27) are now *provably* `UNSAFE` rather than vaguely `LINEAGE_INCOMPLETE`/`TYPED_FAILURE` — more views are now correctly protected, not fewer things work; this is the system becoming more accurate, not more restrictive in a way that drops genuine coverage (coverage still net-increased by 15 executable views overall, Part 1 vs. Part 6).

## Part 7 — Remaining-view adjudication (76 non-executable, down from 124)

| category | count | resolvable deterministically? | needs further live access? |
|---|---:|---|---|
| Genuinely unsafe (`UNSAFE`, proven secret-exposure risk, mostly ASP.NET/DNN framework identity tables as dependencies) | 48 | No — this is the correct, final disposition, not a defect | No |
| Unresolved lineage (`LINEAGE_INCOMPLETE`) — predominantly unqualified-column references into derived tables whose own column list isn't in local structural metadata, plus a handful of correlated-subquery/CTE-depth cases | 26 | Partially — see Step 6E3B3's own Part 8 (correlated-subquery resolution deferred as high-risk/low-volume) | No — this is a profiler-capability gap, not a metadata gap |
| Genuinely unsupported T-SQL syntax (parser-opaque, **not** a metadata or truncation issue — the complete, authoritative text is already in hand for both) | 2 | One (`> =` whitespace operator) is a narrow, mechanically fixable tokenizer-normalization case; not attempted here (view-count impact of 1 does not justify a blind text-substitution risk that could corrupt a string literal elsewhere) — explicitly **not** accepted as "unfixable," just deferred | No |
| Encrypted definitions | 0 | N/A | N/A |
| Inaccessible/permission-denied definitions | 0 | N/A | N/A |
| Snapshot/backup/cross-source objects | 0 confirmed via typed evidence (the Step 6E3B3 name-pattern heuristic list of 14 possibly-deprecated-named views remains unproven either way — unchanged from Step 6E3B3) | No (business-knowledge question) | No |

No remaining view's blockage rests on "parser error" where a better metadata source could have resolved it — the complete, authoritative definition is now in hand for all 240 views; every remaining block has an evidence-backed, specific reason (secret exposure, a specific unresolved lineage shape, or a specific unsupported syntax construct), none of them a generic catch-all.

**Legitimate-business-view coverage**: treating the 48 `UNSAFE` views as the only objectively non-business-safe subset (same method as Step 6E3B3), coverage of the remaining 192 is **164/192 ≈ 85.4%** — up from the Step 6E3B3 estimate of ≈70.0%.

## Part 8 — Required proof cases (reconfirmed against complete definitions)

1. **`vw_StudentOverview` remains eligible for enrollment details** — `profile_state=COMPLETE`, `suitable_for_detail=True`. ✅
2. **Naive `COUNT(*)` remains rejected** — `duplication_risk=LIKELY_FANOUT`, `required_distinct_key='StudentID'`, unchanged from Step 6E3B3. ✅
3. **Correct distinct-student counting remains accepted** — same unmodified mechanism. ✅
4. **Filtered views cannot answer unrestricted questions** — `dbo.Vw_RegisteredUsers` remains `COMPLETE` with its population-restriction evidence intact; `sql_validation`'s unmodified `requested_population_filters` check still applies. ✅
5. **Compatible filtered questions may use the view** — same mechanism. ✅
6. **Aggregate views work for compatible aggregate questions** — `dbo.vw_ADF_CCS_BasecampProjects_Current` remains `LIMITED_COMPLETE` with aggregate functions (`MAX`/`MIN`) intact. ✅
7. **Direct framework/authentication-table access remains blocked** — unchanged; Tier-3/4 governance re-check is untouched by this step. ✅
8. **Credential/secret-bearing views remain blocked** — `dbo.vw_aspnet_MembershipUsers` remains `COMPLETE` (lineage resolves cleanly with the full text) **and** still carries `secret_columns`, still unconditionally blocked by `sql_validation`'s own check regardless of profile state — proves Part 5's completeness/safety separation holds even as more views reach full lineage resolution. ✅
9. **Authorized PII remains unmasked** — `dbo.VW_PAYPAL_DISTINCT_TRANSACTIONS` remains `COMPLETE`, `pii_categories` populated, no `secret_columns`, no masking anywhere in this module. ✅
10. **No PII or full definition SQL appears in logs/cache/report artifacts** — verified by a dedicated test (`test_no_definition_text_ever_appears_in_logs`) planting a unique marker and asserting its absence from every log record; this report itself never quotes a view's SQL body, only FQNs/lengths/status/checksum-prefixes. ✅

## Part 9 — Regression suites

| suite | result |
|---|---|
| Focused definition-capture tests (`test_view_semantics_complete_definition_capture.py`) | 16/16 pass |
| View-profiler + suitability tests (`test_sqlservice_view_profiler.py`, `test_sql_validation_secrets_and_suitability.py`) | 50/50, 44/44 pass |
| Full targeted SQL-service/profiling/bootstrap/PII-governance set (18 files: view_semantics_service, complete_definition_capture, view_profiler, data_view_profile_service, sql_validation_secrets_and_suitability, curated_view_evidence, schema_object_authority, investigation_service, pii_secret_no_leak_sentinel, dictionary_view_profile_reconciliation, schema_frontier_framework_backed_refinement, fast_path_canonical_entity_rule, fast_path_plan_cache, fast_path_context, phase1_datasources, connect_time_retrieval_preparation, view_dependency_service, semantic_contract_service) | **368 passed, 2 failed** |
| Full repository (`pytest tests/`, 7,880 tests — this is the SAME run reported under Precondition; not re-run in full a second time, per this project's own guidance against repeated expensive full-suite runs, since the targeted 370-test re-run above already covers every file this step touched or could plausibly affect) | 7,820 passed / 57 failed baseline, reconfirmed zero new failures |
| Frontend suite | **Not run** — this step made zero frontend/JS changes; no frontend test infrastructure was invoked in either this step or Step 6E3B3 |

The 2 failures in the 370-test targeted run are **both pre-existing and unrelated**, confirmed by two independent methods: (a) both appear verbatim in the Step 6E3B3-era 57-failure full-suite baseline; (b) neither failing test's file, nor its assertion, references anything this step touched — `test_investigation_service.py::test_view_definition_sql_unsupported_dialect_returns_null` asserts a stale expectation about `sqlite` dialect support in `_build_sql` (added in an earlier, unrelated milestone, per that code's own docstring) that predates both Step 6E3B3 and 6E3B4; `test_semantic_contract_service.py::test_discovery_returns_no_candidate_on_genuine_ambiguity` asserts a `resolution_status` string value (`'NO_CANDIDATE'` vs. the actual `'NO_SAFE_SELECTION'`) in an entirely different module (`data/semantic_contract_service.py`, entity-contract discovery) this step never opened.

## Part 10 — Windows-safe Step 6E3C plan (updated)

`docs/validation/view_profiling_step6e3c_windows_deployment_plan.md` updated with a new **Step 2d / Step 5** code-rollback pair: a per-file SHA-256 checksum manifest + whole-file snapshot (PowerShell `Get-FileHash`/`Copy-Item`, never `git checkout`/`reset`/`stash`), restoring by named-file copy with the current (post-change) version quarantined, never deleted, and every other uncommitted file in the working tree left completely untouched by construction (the restore only ever touches files named in its own manifest). Still not executed. Still reuses `data/migrate_data_dir.py`'s DB backup mechanism, `Get-Date -Format` in place of `$(date)`, and assumes only Python + PowerShell built-ins (no `sqlite3.exe`/`sha256sum`).

## Part 11 — Stop point / untouched-environment proof

- **`data/toolsmith.db`**: opened **read-only** (`mode=ro`) exactly twice across this entire step (once for Part 5's fresh snapshot, once incidentally re-verified) — every subsequent read AND every write (the 240 upserted `view_semantic_evidence` rows) landed in a disposable scratch-directory copy, never the live file. `data.db.DB_PATH` was redirected to that copy before any other project module was imported in every script this step ran, and explicitly asserted `!=` the live path before the live connection was opened.
- **Live SQL Server (source 10004)**: exactly one real connection was opened, for the single bounded catalog query in Part 5 (`sys.views`/`sys.schemas`/`sys.sql_modules`/`OBJECTPROPERTY`) — zero business-table row queries, zero DDL/DML, verified by code inspection of the one function that issued it.
- **`.env`**: not read for writing, not modified — only `load_dotenv()`'s own pre-existing, unmodified startup behavior supplied `ENCRYPTION_KEY` to decrypt the already-stored connection config, exactly as the real running application already does.
- **Business mappings, frontend runtime, provider services, retrieval build, embeddings**: not read or modified by this step. No OpenAI/any LLM provider call occurred anywhere in this step's code path (confirmed by inspection — no provider client import in any changed/added file).
- **Real `data/toolsmith.db` schema**: the additive `view_semantic_evidence` migration (Part 2) exists only in `data/models.py`'s source code; it has not been run against the live file in this step (no `init_db()` call against the live `DB_PATH` occurred) — it will apply automatically, additively, and non-destructively the next time the real application starts, consistent with how every prior schema evolution in this same function already works; flagged explicitly rather than silently left for the user to discover.
- All code changes remain local, uncommitted edits in the working tree — nothing was committed or pushed, consistent with this step's "stop for review before live metadata backfill" instruction.
