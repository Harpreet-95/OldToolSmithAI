# Phase 7, Step 6E3B6 — final read-only adjudication report

Scope: final adjudication of every remaining non-executable/unsafe view before Step 6E3C approval, using only the already-captured complete definitions in the isolated snapshot. No write to the real `data/toolsmith.db`, `.env`, live mappings, business knowledge, retrieval documents, embeddings, or the running backend. No business-row SQL, no OpenAI call. This step's own three generic fixes (Part 3) were each verified against the isolated snapshot only — no new live connection was opened in this step.

## Part 1 — Reconciled population (240 views, mutually exclusive)

| category | count |
|---|---:|
| Executable legitimate business views (`COMPLETE` + `LIMITED_COMPLETE`) | **217** |
| Legitimate business views still `LINEAGE_INCOMPLETE` | **3** |
| Views proven unsafe because they expose an actual authentication/system secret | **2** |
| Views marked `UNSAFE` for another reason (unresolved lineage touching a secret-shaped dependency; see Part 2) | **18** |
| **Total** | **240** |

(`TYPED_FAILURE`/`MISSING`/`STALE`: 0 each — resolved in Steps 6E3B4/6E3B5; unreachable here for the same reason explained in the Step 6E3B5 report.)

### The 3 remaining `LINEAGE_INCOMPLETE` legitimate views — full required detail

| field | `dbo.VW_RETOOL_CAMPAIGNACTIVITY_SUMMARY` | `dbo.vw_ColaberryInterviewPreparation_wNonInterviews` | `dbo.vw_OutreachStatus_Prompt_Industry_DELETE` |
|---|---|---|---|
| Unresolved columns | 6 of 7 (`UserID`, `LastCallCampaignName`, `LastEmailDate`, `LastEmailCampaignName`, `LastNoteDate`, `LastNoteCampaignName`) | 28 of 37 | 22 of 22 |
| Exact unresolved reason | `cte_reference_beyond_resolution_depth` | `union_branch_lineage_unresolved` | `column_not_found_on_any_known_source` (2), `cte_reference_beyond_resolution_depth` (18), `nested_subquery_column_source` (2) |
| Missing lineage edge | The column is itself a `MAX(...)` aggregate projected *inside* a CTE that is built from an inner UNION of 3 branches (`RETOOLCALLENGAGEMENT`/`RetoolEmailEngagement`/`RetoolNoteEngagement`); each branch's own derived table resolves, but the CTE's own `GROUP BY`-shaped aggregate projection on top of that union is the edge this module does not trace through | A UNION where at least one branch's own projection is unresolved (candidate_source_fqns proven `()` for the failing branch — it has a column with no identifiable source in that specific branch), so the position-matched merged column is marked mismatched | The view's own 4-level CTE chain nests a further UNION and at least one scalar/correlated subquery; two columns reference a dependency whose column genuinely isn't found anywhere in this build's structural metadata |
| Definition complete? | Yes (`AVAILABLE_COMPLETE`, Step 6E3B4) | Yes | Yes |
| All referenced table/view columns available? | Yes for the base tables; the blocker is an aggregate-over-union-inside-a-CTE shape, not missing column metadata | Yes for resolved branches; the failing branch's own projection has no column reference this module can attribute | Yes for most; 2 columns have no match anywhere in the view's own FROM/JOIN closure |
| Blocker category | **Parser/lineage-capability limitation** — an aggregate function's argument sourced from a UNION-shaped CTE body is not yet traced to its underlying branch columns | **Genuinely unknowable from static analysis** — the specific branch projection has no column reference (proven `()`, not a guess) | **Mixed**: 20 of 22 are capability-limitation (nested UNION/CTE depth); 2 are a genuine missing-reference case |
| Suitable for detail/count/aggregation/join | detail=False, count=False, aggregation=True, join=False | detail/count/aggregation/join: see Step 6E3B5 report (unchanged this step) | detail=False for all four (too incomplete to trust any purpose) |
| Generic deterministic correction possible? | **Partially** — would require extending aggregate-function-argument resolution to trace through a UNION-shaped CTE body (a further, bounded capability extension); not attempted this step (diminishing-returns judgment call, not a blocked attempt) | **No** — the specific branch's own projection is proven to have no identifiable column reference; this is a correct, final answer, not a gap | **Partially**, for the 20 capability-limited columns; **No** for the 2 genuinely-missing-reference ones |

No object-name guesses or CCPP-specific exceptions were used anywhere in this adjudication — every determination above traces to the AST evidence already computed by the (fully generic) profiler.

## Part 2 — Revalidation of all 20 `UNSAFE` views

Applying the product-owner policy exactly: ordinary/sensitive PII never disqualifies alone, a framework dependency alone never disqualifies, and a view is rejected only for a proven risk or a genuinely-unresolvable "cannot rule out" case — never from a transitive dependency alone (this exact over-broad behavior was the Step 6E3B5 Part 2 fix; reconfirmed still correct here).

| category | count | views |
|---|---:|---|
| **1. Proven secret-exposing** | **2** | `dnnuser.vw_Portals`, `dnnuser.vw_PortalsDefaultLanguage` — both directly project `ProcessorPassword` as a resolved, named output column (a DNN/DotNetNuke CMS payment-processor credential) |
| **2. Merely framework-backed (not itself unsafe)** | **0** | None remain in `UNSAFE` for this reason alone — eliminated by the Step 6E3B5 Part 2 candidate-narrowing fix (7 views moved out) plus this step's three further fixes (13 more moved out: 48→41→36→23→20 across the two steps) |
| **3. Ordinary/sensitive PII only** | **0** | PII alone has never been sufficient under this module's logic (`is_restricted_category` structurally excludes name/email/phone/SSN/bank/card) |
| **4. Unresolved dependency/lineage evidence (secret cannot be ruled out)** | **18** | every other `UNSAFE` view (see table below) |
| **5. Real unrelated security risk** | **0** | none found beyond category 1 |

### The 18 "cannot rule out" views — exact supporting output column/behavior

Each has at least one output column whose own `candidate_source_fqns` (the bounded, real set of tables it could come from — never the whole dependency closure, per the Step 6E3B5 fix) includes a table carrying a secret-shaped column (overwhelmingly `dnnuser.Users`, this catalog's CMS identity table) — or is `None` (genuinely opaque, a scalar/correlated subquery), in which case the fallback is the view's own full dependency closure, which does contain such a table:

| view | unresolved reason(s) | exact basis |
|---|---|---|
| `VW_TWC_MASTER_STUDENT_LIST` | `nested_subquery_column_source` (12), `cte_reference_beyond_resolution_depth` (2) | 14 opaque (scalar-subquery/CTE) columns; dependency closure includes `dnnuser.Users` |
| `vw_ADF_CredlyBadgeEligible` | `union_branch_lineage_unresolved` | 4 unresolved union-merged columns; dependency closure includes `dbo.AspNetUsers`, `dnnuser.Users` |
| `vw_ADF_EnrollmentInfo` / `vw_ADF_Enrollments` / `vw_ADF_HS_Enrollments` | `nested_subquery_column_source` | 2 opaque scalar-subquery columns each (`Reenrolled`, `ReferralEmail`); dependency closure includes `dbo.AspNetUsers`, `dnnuser.Users` |
| `vw_ADF_GraduateSurveyAnswersFlatened` | `cte_reference_beyond_resolution_depth` | 60 of 75 columns unresolved (a very deep CTE/star-schema chain — `dim_users`, `fact_userlogins`, etc.); dependency closure includes `dnnuser.Users` |
| `vw_ADF_GraduateSurveySummary` | `column_not_found_on_any_known_source`, `cte_reference_beyond_resolution_depth`, `nested_subquery_column_source` | mixed; dependency closure includes `dbo.AspNetUsers`, `dnnuser.Users` |
| `vw_ADF_Student_Marketing_SalesRepsIPBC_Signups` | `nested_subquery_column_source` | 5 opaque payment-calculation scalar subqueries; dependency closure includes `dnnuser.Users` |
| `vw_ColaberryChatResponses_COE_Participation` | `cte_reference_beyond_resolution_depth` | 5 columns; dependency closure includes `dnnuser.Users` |
| `vw_ColaberryInterviewPreparation` | `nested_subquery_column_source` | 5 opaque scalar subqueries; dependency closure includes `dnnuser.Users` |
| `vw_QS_MetricsDashboard_GradSurvey` | `cte_reference_beyond_resolution_depth` | 51 of 57 columns (another deep CTE chain); dependency closure includes `dnnuser.Users` |
| `vw_QS_MetricsDashboard_GradedActivity` | `cte_reference_beyond_resolution_depth`, `union_branch_lineage_unresolved` | 8 columns; dependency closure includes `dnnuser.Users` |
| `vw_QS_MetricsDashboard_MentorAppReporting` (+`_12Wk`, `_TestTEMP`) | `union_branch_lineage_unresolved` | 3-5 columns each; dependency closure includes `dnnuser.Users` |
| `vw_StudentEnrolledNotCompleted` | `nested_subquery_column_source` | 3 opaque scalar subqueries; dependency closure includes `dnnuser.Users` |
| `vw_SyncCAPUsersInfo` / `vw_SyncIPBCUsers` | `nested_subquery_column_source` | 1 and 3 opaque scalar subqueries respectively; dependency closure includes `dnnuser.Users` |

No view in this list is unsafe "solely from a transitive dependency" — each has a specific, genuinely-unresolved own output column, and the policy requires treating "cannot rule out" as a valid reason to withhold trust (per this task's own Part 2 instruction) rather than guessing the column is safe.

## Part 3 — Generic corrections (three found and fixed this step; all verified against the isolated snapshot, no CCPP-specific exceptions)

All three are pure `core/sqlservice/view_profiler.py` logic fixes, each targeting a generic SQL *shape*, none referencing any object name:

1. **Unqualified-column-through-scope resolution** (already covered in the prior addendum; carried forward). An unqualified projection could only ever resolve through a real table, never a derived-table/CTE scope, even when exactly one such scope was the obvious, unique, checkable source.
2. **CTE scope leakage across sibling CTEs**. A 4-CTE chain where the final `SELECT` only ever joins the *last* CTE (`FROM final_cte`, bare, no alias) was wrongly treated as if all 4 CTEs were simultaneously in scope at that point, creating false "ambiguous unqualified column" results whenever two sibling CTEs happened to pass through a same-named column neither was actually joined alongside. Fixed in `_build_scopes`: a bare `FROM cte_name` reference is now recognized and routed to a local scope (never a fake real-table alias), and the scopes returned to a given SELECT are now exactly the ones *that select's own* FROM/JOIN introduces — never every sibling CTE `outer_scopes` carries forward for unrelated, deeper recursive purposes.
3. **UNION bodies inside a derived table/CTE/APPLY, and a `WITH` clause attached to a top-level UNION**. Two related gaps, both found by direct inspection of real failing views: (a) sqlglot attaches a `WITH cte AS (...) SELECT ... UNION ALL SELECT ...` clause to the *outer* `SetOperation` node, never to either branch — the prior code looked for it on each branch and always found nothing; (b) a derived table/CTE whose own body is itself a UNION (`(SELECT ... UNION ALL SELECT ...) AS alias`) was entirely unregistered as a scope (silently dropped), because the registration check required a plain `exp.Select`. Both fixed: the WITH clause is now resolved once from the real top-level node and threaded into every branch; a new shared `_resolve_union_or_select_lineage`/`_merge_union_branch_columns` pair (also now used by the top-level view-body resolution itself, replacing a near-duplicate inline block) handles a UNION-shaped body identically wherever it appears — top-level, inside a CTE, inside a derived table, or inside a CROSS/OUTER APPLY target.

**Constraints honored**: no object names or CCPP-specific rules anywhere in the fix code (verified: `test_no_domain_specific_tokens_in_production_module`-style scrutiny applied by inspection); no SQL-validation weakening (zero changes to `core/sqlservice/sql_validation.py` this step); no weakening of actual-secret rejection (the `secret_columns`/`is_restricted_category` checks are untouched); no blanket admission of incomplete views (every fix either proves a column fully resolved via real, checkable evidence, or leaves it precisely typed-unresolved — nothing was guessed into `COMPLETE`/`LIMITED_COMPLETE`); no AI-based adjudication anywhere; fails closed throughout (a genuinely ambiguous or opaque case still produces `LINEAGE_INCOMPLETE`/`UNSAFE`, never a silent pass).

**Tests**: 5 new tests added (`test_bare_cte_from_reference_scoped_to_itself_not_every_sibling_cte`, `test_bare_cte_reference_with_explicit_alias_still_resolves`, `test_derived_table_whose_own_body_is_a_union_resolves`, `test_cte_defined_before_a_top_level_union_is_visible_to_every_branch`, plus the two from the prior addendum). `tests/test_sqlservice_view_profiler.py`: 64 → 68 tests, all passing.

**Measured impact, rerun after each fix against the isolated snapshot**:

```text
start of this step:        205 executable (85.4%)
+ CTE scope-leakage fix:   208 executable (86.7%)
+ union/WITH-clause fixes: 217 executable (90.4%)
```

## Part 4 — Final readiness measurement

| metric | value |
|---|---:|
| Total views | 240 |
| Legitimate business views (240 − 2 proven-secret) | **238** |
| Executable legitimate business views | **217** |
| **Legitimate coverage** | **217 / 238 ≈ 91.2%** |
| Proven secret views | 2 |
| Remaining blocked legitimate views | 21 (3 `LINEAGE_INCOMPLETE` + 18 `UNSAFE`-for-unresolved-lineage) |
| `COMPLETE` | 163 |
| `LIMITED_COMPLETE` | 54 |
| `LINEAGE_INCOMPLETE` | 3 |
| `UNSAFE` | 20 |
| `TYPED_FAILURE` | 0 |
| **Reconciliation check** | 163+54+3+20+0 = **240** ✓ |

```text
6E3B2:  91 executable (37.9% of all 240)
6E3B3: 149 executable (62.1%)
6E3B4: 164 executable (68.3%)
6E3B5: 205 executable (85.4%)
6E3B6: 217 executable (90.4% of all 240; 91.2% of the 238 legitimate business views)
```

### Test-wording clarification (as requested)

Step 6E3B5's "384 pass... same 2 pre-existing failures" referred to **one specific targeted run**, not the full-repository baseline — the two numbers are from genuinely different suites and should never be read as the same measurement:

- **The full-repository baseline** ("7,820 passed, 57 failed") is `pytest tests/` run across the **entire** `tests/` directory (7,880 tests collected, every test file in the repository), executed once in the background during Step 6E3B3 and never re-run in full since (per this project's own guidance against repeating expensive full-suite runs). Those 57 failures span many files, most of them nothing to do with view profiling at all (composer, clarification, conversation-context, phase9/10 planning, etc.).
- **The "384 passed, 2 failed" figure** (reconfirmed again this step as "386/388/388 passed, 2 failed" after each successive fix) is from a **targeted 18-file regression suite** — an explicit, named list of every test file directly or plausibly-adjacently affected by the view-profiling/definition-capture work (`test_view_semantics_service.py`, `test_view_semantics_complete_definition_capture.py`, `test_sqlservice_view_profiler.py`, `test_data_view_profile_service.py`, `test_sql_validation_secrets_and_suitability.py`, `test_curated_view_evidence.py`, `test_sqlservice_schema_object_authority.py`, `test_investigation_service.py`, `test_pii_secret_no_leak_sentinel.py`, `test_dictionary_view_profile_reconciliation.py`, `test_schema_frontier_framework_backed_refinement.py`, `test_sqlservice_fast_path_canonical_entity_rule.py`, `test_sqlservice_fast_path_plan_cache.py`, `test_sqlservice_fast_path_context.py`, `test_phase1_datasources.py`, `test_connect_time_retrieval_preparation.py`, `test_view_dependency_service.py`, `test_semantic_contract_service.py`) — **18 files, not 7,880 tests' worth**.
- **The relationship between the two numbers**: both of the targeted suite's 2 failures are a **subset** of the full-repository baseline's 57 — confirmed by direct string-match against that baseline's own failure list, not assumed. Concretely: `test_investigation_service.py::test_view_definition_sql_unsupported_dialect_returns_null` lives in `test_investigation_service.py` (one of the 18 targeted files); `test_semantic_contract_service.py::test_discovery_returns_no_candidate_on_genuine_ambiguity` lives in `test_semantic_contract_service.py` (a different one of the 18). Both are pre-existing (present before Steps 6E3B3 through 6E3B6 touched anything), both are unrelated to any change made across this whole engagement (confirmed by inspecting that neither failing test's own file or assertion references `view_profiler`, `sql_validation`, `ViewProfile`, or `classify_profile_state`). The other 55 of the 57 full-repository failures are in files **outside** the 18-file targeted list entirely, and were never expected to be affected or re-checked by this engagement's own targeted runs.
- **No claim of global green was ever made or is made now.** The repository's honest, current state is: 7,820/7,880 tests passing repository-wide (as of the Step 6E3B3 baseline, not re-measured since), and — specifically for every file this engagement's four steps (6E3B3 through 6E3B6) could plausibly have affected — 386 (now 388, after this step's own last two fixes) of 388 tests passing, with the exact same 2 pre-existing, unrelated failures throughout. **Zero new failures were introduced by any of Steps 6E3B4, 6E3B5, or 6E3B6.**

### Approval recommendation

Per the stated approval rule — approve only if every legitimate business view is executable, or each exception is individually demonstrated technically impossible to profile safely with available complete metadata — **this report does not recommend unconditional approval**: 21 legitimate business views remain blocked. Part 1's table demonstrates, for all 3 `LINEAGE_INCOMPLETE` cases, a specific technical mechanism (aggregate-over-union-inside-CTE tracing, a branch with a genuinely unidentifiable column reference, or a mix); Part 2's table demonstrates, for all 18 "cannot rule out" `UNSAFE` cases, that each has a specific opaque or genuinely-unresolved output column whose real dependency closure contains a secret-shaped table. None of the 21 is blocked by a generic, unexplained, or CCPP-specific reason — every one has a traced, typed, evidence-backed mechanism. Whether that rises to "technically impossible... with available complete metadata" (as opposed to "would require a further, bounded capability extension — e.g., tracing an aggregate argument through a UNION-shaped CTE body, or deliberately parsing into a correlated subquery, neither attempted this step") is a judgment call for the approver, not asserted unilaterally here.

Stopping after this report. No migration, backfill, embedding generation, live database write, provider call, `.env` change, or backend restart occurred in this step — requesting explicit Step 6E3C approval.
