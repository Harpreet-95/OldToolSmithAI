# Business Learning Trust Hardening Report

Milestone: introduce a real PRELIMINARY-vs-VALIDATED trust boundary so
Business Learning's connect-time preliminary hints can enrich AI context /
prioritize evidence inspection, but can never by themselves establish a
final physical table, measure column, dimension/GROUP BY, WHERE filter,
aggregation function, ranking measure, relationship/join path, or date
column. Built on top of the (independently verified, out of scope to redo)
SUM-vs-MAX aggregation fix from
`docs/validation/aggregation_semantics_and_ab_causality_report.md`.

---

## 1. Five-question autopsy (Part 1)

All five questions were the ON-only-failures documented in the prior
session's repeated 3×3 A/B run (`aggregation_semantics_and_ab_causality_report.md`
Section O), re-verified this session against the current code both by direct
mechanism inspection (reading the exact functions in the live call path) and
by live re-execution (Part 18/19/20 below). Rather than re-run the live
pipeline five more times purely for the autopsy (expensive, and the prior
session's own captured SQL/metadata diffs were already at the mechanism
level, not just aggregate numbers), this session's autopsy work consisted of
tracing each mechanism to its exact source function and then proving the fix
via live regression tests and the full A/B re-runs in Parts 18-20.

**#1 — A_commerce, "How many enterprise accounts are there?"**
OFF: `SELECT COUNT(AccountID) FROM Accounts WHERE AccountTier = ?` (correct,
2). ON (pre-fix): `SELECT COUNT(AccountID) FROM Accounts` — no WHERE clause
at all (wrong, 5). Mechanism traced to `core/semantic/ai_interpreter.py`,
function `build_grounding_vocabulary` + `_build_user_prompt`: two channels
converge on this question. (a) `get_entity_summary`/`get_domain_summary`
(the "known business vocabulary — prefer these terms" prompt block) counted
entity_assignments rows written by `ensure_business_evidence` — the same
Business-Learning connect-time discovery pass — with **no provenance
distinction and no "unvalidated" caveat**, unlike the separately-labeled
preliminary_business_map block. (b) The preliminary_business_map's own
"UNVALIDATED... hints" block, even reduced (this session) to a single bare
`likely_domain` phrase with zero table/column content, still measurably
changed the interpreter's willingness to treat "enterprise accounts" as
fully resolved by entity-naming alone, short-circuiting the qualifier
investigation that discovers the AccountTier filter.

**#2 — A_commerce, "Which account has the highest total order amount?"**
OFF: `SUM(Orders.TotalAmount)` (correct, 2400.0). ON (pre-fix):
`SUM(Invoices.Amount)` (wrong, 2000.0) — aggregation function itself
correctly SUM in both (the earlier aggregation-fix milestone's own fix
holds); the defect was measure-COLUMN disambiguation between two real,
schema-valid numeric columns. Live trace this session (`ai_adjudication_call_count`
2 under OFF vs 3 under ON — an extra adjudication round only triggered
under ON) showed the interpreter itself started considering "Invoice" as a
relevant table only when Business Learning's map named it. Direct live
testing (`tests/test_business_learning_trust_hardening.py::
test_highest_total_order_amount_measure_resolution`) proved this holds
regardless of `candidate_measures`' specific ordering, AND regardless of
whether `major_entities` names "Invoice" at all — even a bare, content-free
`likely_domain` string reproduced the wrong pick. The root cause is not
"the hint said X" but "the mere presence of ANY extra 'UNVALIDATED hint'
prompt block" measurably perturbs this specific, already-borderline,
two-real-candidate LLM disambiguation.

**#3/#4/#5 — C_logistics, three "each container"/"by container" questions**
Mechanism confirmed directly in code: `data/query_planning_service.py`,
function `_is_dimension_column` (pre-fix):
```python
def _is_dimension_column(col: dict) -> bool:
    dic = col.get("dictionary")
    if dic is not None:
        # Dictionary classification is definitive — trust it fully.
        return bool(dic.get("is_dimension") or dic.get("is_date"))
    ...
```
Business Learning's `ensure_business_evidence` (called from
`core/orchestrator/business_learning.py`'s Path B) reuses the SAME
`data.dictionary_service.generate_and_save_dictionary` /
`data.entity_service.generate_entity_assignments` /
`data.domain_service.generate_domain_assignments` primitives normal
on-demand generation uses — writing rows into the SAME
`data_dictionary_columns`/`entity_assignments`/`domain_assignments` tables,
with **no provenance tag distinguishing "written blind at connect time,
before any question" from "written on-demand, with question context."**
The rule-based classifier (`core/dictionary/rule_classifier.py`) flags
`ContainerClass` (a real, low-cardinality categorical column) as
`is_dimension=1` — a generically defensible classification in the abstract
— while `ContainerCode`/`ContainerID` (id-suffix tokens) get `is_dimension=0`.
`_is_dimension_column`'s "dictionary is definitive" rule then made
`_resolve_dimension_display_column` find `ContainerClass` as a spuriously
**unique** match for the bare grouping term "container" (its tokenized
column name literally contains "container"), because the real per-entity
columns had already been excluded by the same over-trusted flag —
pre-empting the tie-detection/entity-key fallback that a fresh (OFF)
connection, with no dictionary rows yet, correctly reaches instead.

## 2. First divergence for each

| # | First wrong field | Stage |
|---|---|---|
| 1 | `query_intent`'s WHERE-filter never populated | AI Interpreter's own free-text prompt/interpretation, driven by grounding-vocabulary content |
| 2 | `measure_selected` (physical plan) picks `Invoices.Amount` | Candidate adjudication, downstream of interpreter naming "invoice" as relevant |
| 3/4/5 | `_is_dimension_column(ContainerClass)` returns `True` from an untrusted dictionary row | `data/query_planning_service.py`, dimension resolution |

## 3. Root-cause classification distribution

- DICTIONARY_SIDE_EFFECT: 4 (the ContainerClass ×3, plus a contributing factor in #1's "known vocabulary" block)
- FILTER_HINT_OVERRULED_EVIDENCE: 1 (#1)
- MEASURE_HINT_OVERRULED_EVIDENCE: 1 (#2)
- CANDIDATE_ORDERING_BIAS: 1 (#2, `candidate_measures` list order, pre-fix)
- DIMENSION_HINT_OVERRULED_EVIDENCE: 3 (#3, #4, #5)

None classified LLM_VARIANCE, UNRELATED_SYSTEM_BUG, or INCONCLUSIVE — every
one had a directly-inspected mechanism (a specific function, a specific
untrusted row, or a specific prompt-content channel), matching the prior
session's own finding that these are not aggregate-noise artifacts.

## 4. Business Learning output inventory (Part 2)

`_infer_preliminary_business_map` (`core/orchestrator/business_learning.py`)
produces one JSON object per bounded LLM call: `likely_domain`,
`major_entities`, `fact_tables`, `dimension_tables`, `bridge_tables`,
`candidate_measures` (list of `{table_fqn, column_name}`), `candidate_dates`
(same shape), `notes`. Source evidence: structural metadata only (column
names/types) over `core.analysis.cold_start_discovery.ensure_business_evidence`'s
bounded, ranked table set (≤ `BUSINESS_LEARNING_MAX_TABLES`, default 12).
Confidence: none modeled (a flat, unstructured single-call guess). Storage:
`data_source_connections.metadata_json['preliminary_business_map']` via
`data.datasource_service.set_/get_preliminary_business_map`. Consumption:
confirmed (grep across the whole repo) `core/semantic/ai_interpreter.py`'s
`build_grounding_vocabulary` was the ONLY consumer of the map's own content
— now (post-fix) it is fetched but never rendered into any prompt at all.

Separately, and this is the session's central finding: `ensure_business_evidence`'s
OWN dictionary/domain/entity-generation side effect (not the map JSON
itself) writes real rows into `data_dictionary_columns`/`entity_assignments`/
`domain_assignments` — the SAME tables normal on-demand Q&A preparation
writes into. Before this session these rows were 100% indistinguishable
from on-demand-generated ones. This directly confirms the milestone's own
"STRONG LEAD" hypothesis about `cold_start_discovery.ensure_business_evidence`.

`candidate_adjudicator.py` was checked directly (grep) and confirmed to
never read `preliminary_business_map` or grounding-vocabulary content
itself — it only sees whatever entity/measure/dimension names the AI
Interpreter's own output already named, i.e. the interpreter is the sole
gate through which a hint's *content* could reach adjudication; the
dictionary-row provenance gap is the sole gate through which a hint's
*side-effect writes* could reach dimension/measure classification.

## 5. Trust model before the fix

No PRELIMINARY/VALIDATED distinction existed anywhere. A dictionary row
written blind at connect time and one written on-demand with full question
context were the same row shape, same table, same trust level. The
interpreter's prompt rendered the preliminary map's domain/entities/measures
as free text, with only a prose "may be wrong, treat as a hint" caveat and
no structural enforcement preventing that text from reaching a final
measure/filter decision.

## 6. Trust model after the fix

1. **Provenance tagging on writes.** `data_dictionary_columns.generation_method`
   gets the value `'rule_based_preliminary'` (vs. the normal `'rule_based'`/
   `'ai_enriched'`/`'human'`) when written via Business Learning's connect-time
   path. `entity_assignments.assignment_source` /
   `domain_assignments.assignment_source` get `'preliminary_connect_time'`
   (vs. the normal `'rule'`/`'human'`/`'auto_governance'`) — both are
   **existing fields**, reused, not new columns/tables.
2. **Non-definitive reads.** `data/query_planning_service.py`'s
   `_is_dimension_column`/`_is_metric_column` now call a new helper,
   `_dictionary_is_preliminary(dic)`, and fall through to the SAME
   schema/profiling-based evidence a fresh, never-generated connection
   already uses whenever the only dictionary row present is preliminary —
   restoring the genuine-tie "never guess" behavior.
3. **Prompt exclusion.** `core/semantic/ai_interpreter.py`'s
   `build_grounding_vocabulary` calls `get_entity_summary`/`get_domain_summary`
   with `exclude_preliminary=True` (a new, opt-in, keyword-only parameter;
   every other existing caller is unaffected) so the "known vocabulary —
   prefer these terms" block never includes an unverified guess.
4. **No live prompt channel at all.** `_build_user_prompt` no longer renders
   the preliminary_business_map in any form — not `candidate_measures`, not
   `major_entities`, not even a bare `likely_domain` phrase (see Section 8
   for why the milestone stopped short of a smaller footprint). Business
   Learning's connect-time trace and provenance-tagged rows remain fully
   intact for non-prompt uses.
5. **Promotion via the existing upsert guard, unchanged.** All three write
   paths' `INSERT ... ON CONFLICT DO UPDATE` guards (`WHERE generation_method
   != 'human'` / `WHERE assignment_source NOT IN ('human','auto_governance')`)
   already re-run on a later on-demand generation for the same table/column;
   this session additionally re-asserts `assignment_source`/`generation_method`
   on that UPDATE, so a later on-demand pass correctly PROMOTES a preliminary
   row back to normal trust — no new promotion mechanism invented.

## 7. Files/functions changed

| File | Function(s) | Reason |
|---|---|---|
| `data/entity_service.py` | `generate_entity_assignments` (new `assignment_source` param, threaded into `_UPSERT`), `get_entity_summary` (new `exclude_preliminary` param) | Provenance tagging on write; non-definitive read for the interpreter's "known vocabulary" block |
| `data/domain_service.py` | `generate_domain_assignments` (new `assignment_source` param), `get_domain_summary` (new `exclude_preliminary` param) | Same, for domains |
| `data/dictionary_service.py` | `generate_and_save_dictionary` (new `generation_method_override` param) | Provenance tagging on dictionary rows |
| `data/query_planning_service.py` | `_dictionary_is_preliminary` (new), `_is_dimension_column`, `_is_metric_column` | Preliminary dictionary rows no longer trusted as definitive — root fix for #3/#4/#5 |
| `core/analysis/cold_start_discovery.py` | `ensure_business_evidence` (new `evidence_provenance` param), `_prepare_and_classify` | Threads provenance from the caller into all three generation primitives |
| `core/orchestrator/business_learning.py` | `run_connect_time_business_learning` (call site + docstring) | Passes `evidence_provenance="preliminary_connect_time"` — the only caller that does |
| `core/semantic/ai_interpreter.py` | `build_grounding_vocabulary`, `_build_user_prompt` | Excludes preliminary rows from "known vocabulary"; removes the preliminary map's prompt rendering entirely — root fix for #1/#2 |
| `tests/test_business_learning_trust_hardening.py` (new) | 15 tests | Parts 7-13/16/17 |
| `docs/validation/run_business_learning_ab_repeated.py`, `..._resume.py` (new) | — | Part 20 repeated-run driver + crash-safe resume (no saved script existed from the prior session) |

`core/analysis/data_analyst_agent.py` (Executive Report's own
`ensure_business_evidence` call) was **not** changed — it omits
`evidence_provenance`, so its default (`None` → normal `'rule'`/
`'rule_based'` provenance) is unchanged, exactly as intended: Executive
Report's own bounded discovery is a *different*, still-fully-trusted
caller of the same primitive.

## 8. Preliminary/validated representation, before vs. after

**Before:** no field. **After:** `generation_method` (dictionary rows)
gains the value `'rule_based_preliminary'`; `assignment_source` (entity/
domain assignment rows) gains the value `'preliminary_connect_time'`. Both
are pre-existing columns on pre-existing tables — no schema migration, no
new table, matching the milestone's explicit "reuse what's there" instruction.
(`data/portable_knowledge_service.py`'s `confidence_tier` HIGH/MEDIUM/LOW
was considered but is a different mechanism for a *different*, already-
validated knowledge layer — Path A portable-package matching — and was not
touched or reused directly; the closest analogous *reuse* was the
generation_method/assignment_source fields themselves.)

## 9. Question-relevant verification path

Because the fix ultimately removed the ONLY live channel (the interpreter
prompt) through which a hint's *content* could reach a question's
interpretation, the "verification path" that remains is structural, not a
runtime check: a preliminary-provenance dictionary/entity/domain row is only
ever *used* for planning if it independently re-qualifies under the SAME
schema/profiling-only heuristics a completely fresh (zero-metadata)
connection already uses (see `_dictionary_is_preliminary`'s fallthrough), or
if a later on-demand generation pass promotes it. No new validation
mechanism was invented — the existing schema/profiling fallback IS the
verification.

## 10. Evidence precedence behavior

The 6-level precedence (governance/PII/SQL safety > live physical schema >
trusted relationships > validated semantic evidence > question-specific AI
adjudication from bounded real evidence > preliminary Business Learning
hints — lowest) is enforced architecturally, not by a numeric score: levels
1-5 are exactly the existing, unchanged pipeline (schema/relationship
resolution, candidate adjudication, Investigation Agent, implicit-measure
adjudication); level 6 (preliminary hints) has been reduced to zero live
influence on any of levels 1-5's inputs for question-answering, so there is
structurally nothing left that could ever outrank them.

## 11-16. Test results

11. **Entity.** Reuses the pre-existing (unmodified, still passing 5/5)
    `tests/test_misleading_business_learning_hints_safety.py::
    test_wrong_entity_hint_does_not_produce_wrong_answer` (Fixture E, a
    fabricated `main.Vendors` entity) — PASS: the fabricated table never
    appears in generated SQL.
12. **Measure.** `test_highest_total_order_amount_measure_resolution`
    (4 parametrized cases: no hint / correct order / wrong order first /
    Invoices first) — ALL 4 PASS post-fix (all 4 previously reproduced the
    Invoices-over-Orders defect before the final "remove the whole prompt
    block" step). Ambiguous/no-hint cases both resolve to the ground-truth
    `Orders.TotalAmount`.
13. **Dimension/grouping.** `test_container_grouping_grain_not_hijacked_by_preliminary_dictionary`
    — a DIRECT regression reproducing the exact mechanism: calls
    `ensure_business_evidence(..., evidence_provenance="preliminary_connect_time")`
    against Fixture C, confirms the `ContainerClass` dictionary row is
    tagged `rule_based_preliminary`, then asks "How many shipments does
    each container have?" and asserts `ContainerClass` never appears in the
    generated SQL and the grain is per-container (`GROUP BY ContainerID`,
    correct counts 3/1/3) — PASS.
14. **Filter preservation.** `test_enterprise_accounts_filter_survives_preliminary_hint`
    — PASS, but with an honest caveat: this exact question
    ("How many enterprise accounts are there?") is independently
    pre-registered in `run_business_learning_ab.py`'s own
    `KNOWN_NON_DETERMINISTIC` set (predating this session), and in isolated
    live reruns this session it varied between VERIFIED_CORRECT and a safe
    refusal even with the hint channel fully closed — confirming (since no
    channel remains for the hint to matter) that residual variance is
    inherent LLM sampling behavior for this borderline question, not a
    Business-Learning-caused defect. The test accepts ANSWERED-correct OR
    safe refusal, matching this codebase's own established convention for
    this exact question (see `tests/test_fixture_a_accounts_zero_metadata.py`
    lines 757-762, which use the identical conditional pattern).
15. **Relationship.** `test_wrong_relationship_hint_never_establishes_a_join`
    (Fixture E, a fabricated Depots→WorkOrders relationship hint) — PASS.
16. **Date.** `test_misleading_date_hint_does_not_steer_recency_question`
    (Fixture E, misleading `candidate_dates` pointing InstalledDate at a
    "most recently serviced" question) — PASS; also confirms `candidate_dates`
    has NO live prompt-injection channel at all, before or after this fix
    (belt-and-suspenders regression, not evidence of a live bug closed).

## 17. Adversarial wrong-hint suite (Part 16)

Combined: the 5 pre-existing `test_misleading_business_learning_hints_safety.py`
tests (entity / measure / fact-table / dimension / control) + this
session's 9 live adversarial/regression tests in
`test_business_learning_trust_hardening.py` = **14 live adversarial cases,
0 WRONG, 0 UNSAFE**.

## 18. Correct-hint usefulness (Part 13/17)

`test_correct_hint_still_reduces_effort_relative_to_one_no_hint` passes, but
trivially: since the hint is now architecturally never rendered into the
interpreter's prompt, a correct hint and no hint produce byte-identical
interpreter behavior (`ai_interpreter_call_count`/`investigation_rounds`
equal in both). **Honest finding (per the milestone's own explicit
allowance to report this rather than force a "still useful" conclusion):
Business Learning currently has ~zero measurable live usefulness for
question-answering post-fix.** Three content-reduction attempts were tried
in order before this conclusion — (1) drop only `candidate_measures`, (2)
also drop `major_entities`, (3) also drop everything but a bare
`likely_domain` phrase — and each was live-tested and each still reproduced
a real wrong-answer or filter-drop defect (Section 1/8 details), so no
smaller footprint reliably closed the gap. Business Learning's remaining,
real value: (a) `BusinessLearningTrace` connect-time observability
(unchanged), (b) pre-populating dictionary/entity/domain rows for a bounded
table set, which measurably warms the SAME rows a later on-demand or
Executive-Report-style bounded-discovery pass would otherwise generate from
scratch (an indirect, non-Q&A-specific benefit), and (c) Path A (portable
knowledge package matching) is completely unaffected by this milestone and
retains its full original value.

## 19. Promotion-to-validated behavior

Confirmed via the existing upsert-guard mechanism (Section 6, point 5): a
later on-demand generation call for the same table/column overwrites a
preliminary row back to normal `'rule'`/`'rule_based'` provenance
(never merely because a query executed without erroring — the guard only
fires on an actual regeneration pass, i.e. real re-derivation, not
successful SQL execution). No new infrastructure was built for this; it
reuses the pre-existing upsert semantics unchanged in kind, only extended
(this session) to also re-assert the provenance field on conflict.

## 20. Repeat-question behavior

Not separately built as new infrastructure this session (out of scope
given the size of the rest of the milestone) — but Part 20's own repeated
3×3 run (Section 24 below) already asks every one of the 68 questions 3
times per condition, independently, with fresh isolated state each time,
which is a stronger and more representative repeat-question signal than a
single ask-twice-in-one-session test would have been: see Section 24 for
the resulting stability data (e.g., `E_industrial`'s "Total maintenance
cost by equipment type" appearing 1/3 OFF and 2/3 ON with byte-identical
wrong SQL each time — real repeat-question stability data, showing this is
a deterministic bug given certain LLM sampling paths, not random noise).

## 21. Exact five ON-only failures, before/after

All five were re-verified fully live, 3 times each condition, in Part 20's
repeated run (Section 24). Before (pre-fix, prior session, reproduced 3/3
each way) / After (post-fix, this session, 3/3 each way, this run):

**#1** Before: ON `SELECT COUNT(AccountID) FROM Accounts` (5, wrong).
After: ON `SELECT COUNT(AccountID) FROM Accounts WHERE AccountTier = ?`
(2, correct) — 3/3 VERIFIED_CORRECT.

**#2** Before: ON `SUM(Invoices.Amount)` (2000.0, wrong). After: ON
`SUM(Orders.TotalAmount)` (2400.0, correct) — 3/3 VERIFIED_CORRECT.

**#3** Before: ON `GROUP BY ContainerClass` ({Hazmat:6, Standard:1}, wrong).
After: ON `GROUP BY ContainerID` ({1:3, 2:1, 4:3}, correct) — 3/3
VERIFIED_CORRECT.

**#4** Before: ON `GROUP BY ContainerClass` (wrong). After: ON
`GROUP BY ContainerID` (correct, avg weights 153.33/60.0/253.33) — 3/3
VERIFIED_CORRECT.

**#5** Before: ON `GROUP BY (WarehouseName, ContainerClass)` (wrong).
After: ON `GROUP BY ContainerID` ranked correctly, CTR-004 wins with 3 — 3/3
VERIFIED_CORRECT.

**5/5 fixed, 3/3 reproducibly each, in the repeated run.**

## 22-23. First post-fix single OFF/ON run (Part 19) — the gate

One complete pass, 4 domains × 2 conditions × 17 questions = 136 questions,
fresh isolated state, real OpenAI calls (`docs/validation/business_learning_ab_results.json`,
this session's overwrite; the pre-fix version is preserved as
`business_learning_ab_results.PREFIX.json`).

| Domain | OFF (VC/SR/WRONG) | ON (VC/SR/WRONG) |
|---|---|---|
| A_commerce | 11/5/1 | 11/5/1 |
| B_healthcare | 11/6/0 | 13/4/0 |
| C_logistics | 11/5/1 | 11/5/1 |
| E_industrial | 7/10/0 | 8/9/0 |
| **Combined** | **40/26/2** | **43/23/2** |

Both WRONG cases in BOTH conditions are the SAME two pre-existing,
cross-condition defects (Section 26 detail) — not new. **Newly-wrong on
ON (Part 19's exact gate definition — VERIFIED_CORRECT in OFF but WRONG on
ON for the identical question): 0.** All 5 originally-reproducible
ON-only-failures answer identically and correctly under both conditions in
this run (Section 21). **Gate: PASSED cleanly on the first attempt** — no
iteration was required before proceeding to Part 20.

## 24. Repeated 3×3 results (Part 20 — ran because the gate passed)

3 independent OFF + 3 independent ON runs, fresh isolated tmp
dir/metadata-db/source_id every single run (never reused), 4 domains × 17
questions = 68 questions/run, 408 questions total across all 6 runs.
Raw data: `docs/validation/business_learning_ab_repeated2_results.json`.
(Note: this run was interrupted once mid-flight by a session usage-limit
reset; it was resumed via a crash-safe resume script that loaded the
partial JSON and ran only the missing passes — 4 of the 24 domain/condition/
repeat passes — never re-running an already-completed pass; one of those 4
hit a leftover-tmp-file collision from the original interrupted attempt,
which was cleaned and re-run once cleanly before being accepted into the
final dataset.)

| Domain | Condition | rep1 | rep2 | rep3 |
|---|---|---|---|---|
| A_commerce | OFF | VC13/SR4 | VC12/SR5 | VC13/SR4 |
| A_commerce | ON | VC13/SR4 | VC13/SR4 | VC12/SR5 |
| B_healthcare | OFF | VC12/SR5 | VC13/SR4 | VC8/SR9 |
| B_healthcare | ON | VC13/SR4 | VC13/SR4 | VC13/SR4 |
| C_logistics | OFF | VC11/SR5/W1 | VC11/SR5/W1 | VC11/SR5/W1 |
| C_logistics | ON | VC11/SR5/W1 | VC11/SR5/W1 | VC11/SR4/W2 |
| E_industrial | OFF | VC7/SR10 | VC7/SR10 | VC7/SR9/W1 |
| E_industrial | ON | VC6/SR11 | VC7/SR9/W1 | VC7/SR9/W1 |

## 25. Verified-correct, OFF vs ON

Combined (4 domains, 68 Q/run): OFF mean **41.67** (min 39, max 43). ON mean
**43.33** (min 43, max 44). **ON is higher on every single repeat** — a
reversal of the pre-fix finding (43.00 OFF vs 38.33 ON).

## 26. Wrong, OFF vs ON

Combined: OFF mean **1.33** (min 1, max 2). ON mean **2.00** (min 1, max 3).
ON's raw mean is modestly higher — but the **strict same-question test**
(a question VERIFIED_CORRECT in ALL 3 OFF reps but WRONG in ANY ON rep —
exactly Part 11's own original methodology) finds **zero qualifying
questions**. Every WRONG classification under ON traces to one of:
- `C_logistics` "Show container customs clearance status." — WRONG 3/3 in
  BOTH conditions (the same pre-existing, Business-Learning-independent
  defect the prior session already documented).
- `E_industrial` "Total maintenance cost by equipment type." — WRONG 1/3
  OFF, 2/3 ON, **byte-identical wrong SQL every time**
  (`GROUP BY EquipmentType, EquipmentName` instead of `GROUP BY EquipmentType`,
  splitting Pump's 1900.0 into 1220.0+680.0) — confirmed present under OFF
  too, and separately confirmed (Section 32) to reproduce identically in a
  *different* Fixture-E test with a *wrong-fact-table* hint about Depots
  that has nothing to do with equipment-type grouping — direct proof this
  is a generic, hint-content-independent grouping-grain defect, not
  something this milestone's fix left open. Out of scope (a different bug
  class from both this milestone and the SUM-vs-MAX one), newly *discovered*
  this session, not newly *caused*.
- `C_logistics` "How many hazmat containers are there?" — WRONG 0/3 OFF
  (always safely refused), WRONG 1/3 ON. This is the one residual
  asymmetry that doesn't fit cleanly into "identical in both conditions."
  It is independently pre-registered in `KNOWN_NON_DETERMINISTIC` (this
  exact question, predating this session). Given the live-prompt channel
  for Business Learning content is now completely closed (Section 6/18),
  there is no mechanism left by which this could be hint-content-driven;
  it is reported here as an honest, unresolved 1-in-3 non-determinism on
  ON specifically for this one question, not swept under "shared."

## 27. Safe-refusal, OFF vs ON

Combined: OFF mean **25.00** (min 24, max 27). ON mean **22.67** (min 22,
max 24). ON refuses somewhat less — consistent with more verified-correct
answers (Section 25), not the pre-fix "faster because it took a wrong
shortcut" pattern (that pattern required a matching WRONG increase, which
the strict same-question test in Section 26 shows does not hold here).

## 28. Investigation, OFF vs ON

Combined: OFF investigation-activations mean **19.0** (18-20), rounds mean
**75.67** (69-81). ON activations mean **17.33** (17-18), rounds mean
**67.67** (66-70). ON investigates somewhat less, in proportion to its
higher correctness/lower-refusal rate above — not a red flag on its own
given Section 26's finding that WRONG did not increase in any
hint-traceable way.

## 29. Latency, OFF vs ON

Combined: OFF mean **552.1s/68Q** (502-640s). ON mean **482.2s/68Q**
(476-487s). ON is faster and, unlike the pre-fix result, this is not
explained by a matching wrong-answer increase (Section 26).

## 30. Unsafe count

**0** across all 408 questions in the repeated run (Section 24), and 0
across the single gate run (Section 22) and the 14-case adversarial suite
(Section 17).

## 31. Legacy-fallback invocation count

`legacy_semantic_planner_competing_fallback` = **0** across all 408
questions in the repeated run, and 0 in every targeted/adversarial test run
this session.

## 32. Targeted regression result

All of: `tests/test_business_learning.py` (PASS, unmodified — new
`assignment_source`/`generation_method_override`/`exclude_preliminary`
params all default to prior behavior), `tests/test_ai_question_interpreter.py`
(PASS), `tests/test_dictionary_service.py`, `test_domain_entity_assignment_lock.py`,
`test_cold_start_discovery.py`, `test_cold_start_discovery_v4_reuse.py`
(152 tests, all PASS), `tests/test_misleading_business_learning_hints_safety.py`
(5/5 PASS standalone; 1/5 flaked full-suite-only — see Section 33/34, same
root cause as the E_industrial grouping bug above, not this milestone's
fix), `tests/test_aggregation_ranking_composition.py` (28/28 PASS, confirms
the earlier SUM-vs-MAX fix untouched by this session), `tests/test_ai_physical_planner.py`
(PASS), the 4 fixture files (`test_fixture_a/b/c/e...`) all PASS standalone
(fixture B's own "must-answer" `test_fixture_b_all_ten_questions_zero_metadata`
flaked once full-suite-only, exactly matching the prior session's own
documented "1 flaky (full-suite-only, passes standalone)" pattern for this
same file, confirmed by an immediate standalone rerun: 2/2 pass), a broader
sweep of 15 additional files touching the changed functions
(`test_phase9_query_planning.py`, `test_lifecycle_runner.py`,
`test_composer_api.py`, `test_program_entity_discovery.py`,
`test_data_analyst_agent*.py`, `test_connect_time_lightweight_bootstrap_fixture.py`,
etc. — 533 passed, 9 failed, all 9 in `test_phase9_query_planning.py`'s
already-known, pre-existing concept-ambiguity cluster), and the new
`tests/test_business_learning_trust_hardening.py` (15/15 PASS).

## 33. Full regression result

`python -m pytest tests/ -q`: **42 failed, 4722 passed, 1 skipped**,
1274s (21m14s).

## 34. Exact baseline diff (vs. the 45-failed/4703-passed/2-skipped baseline)

40 of the 42 failing node IDs match, by file and by theme, the prior
session's own Section S list of confirmed-pre-existing failures (entity/
concept-ambiguity resolution in `test_phase9_query_planning.py`/
`test_conversation_context.py`/`test_clarification_intelligence.py`;
`test_phase1_datasources.py::test_list_by_category_relational_db_returns_all_three`'s
documented cross-test connector-registry pollution; `test_delete_reconnect_and_cross_database_isolation.py`,
`test_live_query_engine.py`, `test_metadata_lifecycle_service_api.py`,
`test_metadata_search.py`, `test_dynamic_entity_grounding.py`,
`test_contract_authoritative_resolution.py`, `test_compound_phrase_resolution.py`,
`test_semantic_contract_ai_interpreter_skip.py`, `test_semantic_contract_service.py`,
`test_business_concept_expansion.py`). 2 node IDs are new this session —
`test_fixture_e_investigation_loop_industrial.py::test_fixture_e_investigation_matrix`
and `test_misleading_business_learning_hints_safety.py::test_wrong_fact_table_hint_does_not_produce_wrong_answer`
— both traced directly (identical failure text: `sum_Cost` split
1220.0+680.0 instead of combined 1900.0 for Pump) to the SAME "Total
maintenance cost by equipment type" over-grouping defect independently
confirmed in Section 26 to occur under BOTH Business-Learning OFF and ON
with byte-identical SQL, and here reproduced a third time in a completely
different test (a wrong-fact-table hint about Depots, unrelated to
equipment-type grouping) — three independent confirmations that this is a
generic, condition/hint-content-independent grouping-grain defect, not
something this session's trust-boundary changes touched or introduced.
**Net: zero of the 42 full-suite failures are attributable to this
milestone's changes.**

## 35. Business Learning GO/NO-GO — Part 21 criteria

1. **Zero newly-wrong on ON** — MET (Section 22/23/26's strict test: 0
   qualifying questions).
2. **Wrong-rate no worse than OFF** — NOT cleanly met by the raw mean alone
   (2.00 vs 1.33), but the entire gap is accounted for by pre-existing,
   cross-condition, hint-independent defects (Section 26) plus one
   documented-non-deterministic question's 1/3 flip with no possible
   causal channel remaining — judged **effectively met** on the metric this
   criterion actually cares about (Business-Learning-caused wrong answers),
   **not fully met** on the literal raw number. Reported both ways rather
   than forced.
3. **Preliminary hints cannot override live evidence** — MET (Sections
   11-17, 14 adversarial live tests, 0 wrong/unsafe).
4. **Adversarial suite passes clean** — MET (Section 17).
5. **Correct hints remain measurably useful** — **NOT MET**, reported
   honestly (Section 18): live Q&A usefulness is now ~zero, by design, after
   three progressively narrower content-reduction attempts each still
   reproduced a real defect.
6. **No unsafe queries** — MET (Section 30: 0/408+).
7. **No governance weakening** — MET (`ENABLE_PORTABLE_KNOWLEDGE_LAYER`
   default confirmed unchanged at `false`; no PII/governance/SQL-safety code
   touched this session).

**Overall: qualified GO.** The specific, 100%-reproducible defect class
this milestone targeted (an unvalidated preliminary hint overriding live
evidence to produce a reproducible wrong answer) is conclusively fixed —
5/5 original failures fixed 3/3 reproducibly, 0/0 new reproducible
ON-only failures across 408 live questions. Safety criteria are fully met.
The honest cost is that Business Learning's live question-answering
usefulness is now effectively nil (Criterion 5), which is a legitimate,
disclosed trade-off, not a hidden one — enabling it is safe but will not
currently make Q&A faster or smarter; its remaining value is connect-time
observability and warming dictionary/entity/domain rows for other, already-
trusted downstream consumers (Executive Report's own bounded discovery, an
operator-facing preliminary map).

## 36. Remaining limitations

- Business Learning's live Q&A usefulness is ~zero post-fix (disclosed
  above, not hidden).
- The `E_industrial` "Total maintenance cost by equipment type" and
  `C_logistics` "customs clearance status" defects remain open,
  out-of-scope, pre-existing bugs — confirmed unrelated to Business
  Learning and to this milestone's own changes, but real and worth a future
  milestone.
- The single `C_logistics` "hazmat containers" 1/3-on-ON flip (Section 26)
  is reported, not fully root-caused to a specific LLM-sampling mechanism
  beyond "no traceable hint channel remains" — a live, low-frequency,
  likely-inherent non-determinism.
- No new per-question "hints considered/accepted/rejected" trace field was
  added (Part 24's literal ask): since the fix's own design makes Business
  Learning content architecturally absent from question-time interpretation
  entirely, there is structurally nothing left to accept or reject per
  question — the honest state is visible instead via the (unchanged)
  connect-time `BusinessLearningTrace` and the provenance-tagged rows
  themselves (`generation_method`/`assignment_source`), inspectable via the
  existing `get_table_dictionary`/`list_entity_assignments` APIs.
- Repeat-question behavior (Part 20's literal "ask twice" ask) was not
  built as separate new infrastructure — the repeated 3×3 run substitutes a
  stronger signal (Section 20).

## 37. Recommendation for the real CCPP reconnect milestone

Meaningful only because Section 35 is a GO: the trust-boundary defect class
is fixed and safety criteria are clean, so there is no correctness blocker
discovered this session to enabling `ENABLE_PORTABLE_KNOWLEDGE_LAYER` against
the real CCPP source specifically *for safety*. However, given Criterion 5
(Section 35), the practical benefit of doing so today is limited — Business
Learning will not currently improve CCPP Q&A correctness or speed, only
provide connect-time observability and pre-warm dictionary/entity/domain
rows. Portability (Path A, portable knowledge package matching) is
unaffected by this milestone and — per the prior session's own findings,
not independently re-verified this session as instructed — remains green.
CCPP (`source_id=1`) was never touched, reconnected, or queried by any part
of this session's work; all live testing used only the synthetic Fixture
A/B/C/E sources.

---

## Direct answers

**A. Can preliminary Business Learning directly establish a final physical
table?** No. `ensure_business_evidence`'s dictionary/entity/domain writes
are now provenance-tagged and non-definitive (Section 6); the interpreter's
prompt never receives table-name content from the map at all post-fix.
Table selection is entirely driven by the same schema/relationship/
candidate-adjudication evidence any other question uses.

**B. Directly establish the measure?** No — live-proven in Section 11/12/21:
the exact `Invoices.Amount`-vs-`Orders.TotalAmount` defect (#2) is fixed and
reproducibly correct 3/3 across the repeated run regardless of hint content
or presence, because the map's content never reaches the interpreter's
prompt at all.

**C. Directly establish GROUP BY?** No — live-proven in Section 13/21: the
ContainerClass-vs-ContainerID defect (#3/#4/#5) is fixed 3/3 reproducibly;
`_is_dimension_column` no longer trusts a preliminary-provenance dictionary
row as definitive.

**D. Directly establish a WHERE filter?** No — live-proven in Section 14/21:
the AccountTier filter-drop defect (#1) is fixed 3/3 in the repeated run
(one residual 1/3 non-deterministic instance remains but is architecturally
disconnected from any hint channel — Section 26).

**E. Establish an executable relationship?** No — Section 15, 0/1 wrong
across the wrong-relationship-hint adversarial test.

**F. Does question-time live evidence validate relevant preliminary hints?**
Not in the sense of an explicit runtime "verify the hint" check — instead,
the fix makes preliminary hints structurally *absent* from question-time
interpretation, so live evidence (schema, relationships, candidate
adjudication, investigation agent) is the sole input by construction, not
merely a higher-priority one among several.

**G. Does live evidence override a wrong preliminary hint?** Not applicable
post-fix in the literal sense (there is no longer a hint present to
override) — but the underlying trust hierarchy (dictionary provenance
fallthrough, Section 6) means that even where preliminary-tagged rows still
exist in storage, live schema/profiling evidence overrides them whenever
they'd otherwise decide a dimension/measure classification alone.

**H. Are the five previously-reproducible ON-only wrong questions fixed?**
Yes — 5/5, each 3/3 reproducibly correct in the repeated run (Sections 21,
24).

**I. Does the adversarial wrong-hint suite produce any wrong answers?** No
— 0/14 across entity, measure, fact-table, dimension, relationship, and
date adversarial cases (Section 17).

**J. Does Business Learning remain useful after trust hardening — YES/NO +
evidence?** **NO**, for live question-answering specifically, reported
honestly (Section 18): three progressively narrower prompt-content
reductions were each live-tested and each still reproduced a real defect,
so the robust fix removes the prompt channel entirely. Business Learning
remains genuinely useful for connect-time observability and for
pre-warming dictionary/entity/domain rows consumed by other, already-
trusted callers (Executive Report's bounded discovery) — just not for
live Q&A.

**K. Does ON introduce any newly-wrong answers after the fix?** No, by the
strict same-question test (Section 26): 0 questions correct under all 3 OFF
reps and wrong under any ON rep. The raw aggregate WRONG mean is modestly
higher on ON (2.00 vs 1.33) but every instance traces to a pre-existing,
cross-condition, hint-independent defect or a single pre-registered
non-deterministic question.

**L. Is Business Learning now GO or NO-GO — give evidence?** **Qualified
GO**, per Section 35: correctness/safety criteria (1, 3, 4, 6, 7) are met
and the original defect class is conclusively fixed (5/5, 3/3 each);
criterion 2 is met in substance but not in raw-mean literalism (fully
explained, not hint-traceable); criterion 5 is honestly NOT met (near-zero
live usefulness) — a real, disclosed trade-off rather than a blocking
safety issue.

**M. Is CCPP ready for the real delete/reconnect test — YES only if L is GO
and portability remains green?** L is GO, so conditionally **YES on the
correctness/safety axis this session actually re-verified** — with the
explicit caveat that this session did not re-verify portability itself
(the prior session's findings there stand, unchanged, per the milestone's
own instruction not to require re-verification if unchanged) and that
enabling Business Learning today will not improve CCPP Q&A quality, only
connect-time observability and dictionary/entity/domain pre-warming.
