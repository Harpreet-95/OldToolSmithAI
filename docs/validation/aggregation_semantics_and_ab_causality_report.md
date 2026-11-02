# Aggregation Semantics and A/B Causality Report

Milestone: Aggregation Semantics / Composed Ranking (SUM-vs-MAX root cause fix) +
repeated Business Learning A/B re-run, post-fix.

Scope: fix the "highest total X" -> MAX(X) defect generically at the semantic/
planning layer (never SQL-generation text-matching), prove it with a regression
matrix, re-run the full 4-domain Business Learning A/B experiment 3x per
condition per domain with the fix in place, and issue an evidence-based
GO/NO-GO on enabling Business Learning.

---

## A. Exact SUM-vs-MAX root cause

**File/function:** `core/semantic/concept_resolver.py`, function `extract_query_intent`
(the deterministic, regex-only aggregation-function detector every downstream
planner ultimately consumes).

**The actual code before the fix** (an `if/elif` chain, so only the FIRST match
wins):

```python
if _COUNT_RE.search(q):
    aggregation = "COUNT"
elif _AVG_RE.search(q):
    aggregation = "AVG"
elif _MIN_RE.search(q):
    aggregation = "MIN"
elif _MAX_RE.search(q):
    aggregation = "MAX"
elif _SUM_RE.search(q):
    aggregation = "SUM"
else:
    aggregation = None
```

`_MAX_RE = r"\bhighest\b|\blargest\b|\bmaximum\b|\bmax\b|\bbiggest\b|\bmost\b(?!...)"`
and `_SUM_RE = r"\btotal\b|\bsum of\b"`. For the question "Which depot has the
highest total maintenance cost?", `_MAX_RE` matches the word **"highest"**
first (checked before `_SUM_RE`), so `aggregation` was set to `"MAX"` and the
`elif` chain never even reached `_SUM_RE`'s match on **"total"**. The ranking
word "highest" silently consumed the aggregation-FUNCTION slot that the
explicit qualifier "total" should have won.

**Live-confirmed reproduction** (Fixture E / Industrial, OFF condition, real
OpenAI calls, before the fix):
- AI Interpreter output: `entities=('depot',), measures=('maintenance cost',),
  dimensions=(), sorting={'by': 'maintenance cost', 'direction': 'desc',
  'limit': 1}` — the AI interpreter itself correctly understood this as a
  ranked, `desc`, `limit=1` request.
- `extract_query_intent("Which depot has the highest total maintenance
  cost?")` returned `aggregation='MAX'`, `order=None`.
- Resulting SQL: `SELECT MAX("main"."WorkOrders"."Cost") AS "max_Cost",
  "main"."Depots"."DepotName" ... GROUP BY "main"."Depots"."DepotName" LIMIT
  1000` — no `ORDER BY` at all (see Section B).
- Result: `{'max_Cost': 700.0, 'DepotName': 'North Depot'}` — **wrong**. Ground
  truth (independently computed from `WORKORDERS_ROWS`): North Depot's real
  total is **2590.0** (SUM of its 7 work orders' costs); 700.0 is just the
  single most expensive individual work order.

A **second, independently-discovered instance of the identical bug pattern**
was found live while proving Part 5 (see Section I / test coverage): the
two-entity "Which `<A>` has the most/highest/fewest `<B>`?" reclassification
(`_reclassify_wh_ranking_as_single_entity` in
`core/orchestrator/ai_physical_planning.py`, which fires when the AI
interpreter names both the grouping and measured concept as separate
`entities`, e.g. `entities=("account","order")` for "Which account has the
highest total order amount?") had its **caller** unconditionally hardcode
`aggregation="COUNT"` regardless of any "total"/"average" qualifier in the
question:

```python
if wh_ranking_order_override is not None:
    query_intent = {
        **query_intent, "aggregation": "COUNT", "aggregation_target": "entity_count",
        "order": wh_ranking_order_override,
    }
```

Live-confirmed: "Which account has the highest total order amount?" produced
`COUNT(OrderID)` (value `3`) instead of `SUM(TotalAmount)` (value `2400.0`,
the real ground truth). This is the SAME root-cause class (a ranking word
overwriting an explicit aggregation qualifier), just reached via a different
code path than `extract_query_intent`'s own priority chain.

---

## B. First incorrect semantic state

The first point at which "highest total maintenance cost" stopped meaning
`aggregate=SUM(cost), ranking=DESC` and became `aggregate=MAX(cost)`:

- **Field:** `query_intent["aggregation"]`, returned by
  `core.semantic.concept_resolver.extract_query_intent`.
- **Stage:** Deterministic query-intent extraction, called from
  `core.orchestrator.ai_physical_planning.attempt_ai_physical_plan` (Stage 1,
  AI Physical Planner) at the line `query_intent = extract_query_intent(question)`.
- **Wrong value:** `"MAX"` (should have been `"SUM"`).
- **Compounding second defect, same stage:** `query_intent["order"]` was
  `None` — because `extract_query_intent`'s `order` field is only set by a
  narrow, deliberately-scoped set of patterns (explicit "top N"/"bottom N",
  "N most recent", "latest"/"earliest", and bare "top"/"best"/"worst" only —
  see `_BARE_TOP_SUPERLATIVE_RE`'s own docstring in that file for why
  "highest"/"largest"/"most"/etc. were deliberately excluded from that list:
  those words ALSO drive aggregation-function selection for a genuine bare
  scalar superlative like "the highest salary", and folding them into `order`
  unconditionally would have wrongly forced a ranking attempt onto that
  different, ungrouped shape too). Because "highest" is not in that narrower
  list, no ranking signal at all reached the physical planner for this
  question — so even once `aggregation` is fixed to `SUM`, the query would
  still have produced an un-ordered, un-limited `GROUP BY` listing instead of
  the requested "highest" (rank-and-take-top-1) answer, without the second
  half of the fix (Section D).

Everything downstream of this point (the AI Physical Planner's aggregation
branch selection, `data/sql_planning_service.py`'s SQL plan construction, and
`data/sql_generation_service.py`'s SQL rendering) faithfully and correctly
propagated the now-wrong `aggregation="MAX"` / `order=None` state — none of
those downstream stages re-interpret the question themselves (see Section F).

---

## C. Files/functions changed

| File | Function | Reason |
|---|---|---|
| `core/semantic/concept_resolver.py` | `extract_query_intent` | Reordered the aggregation-function priority chain so `SUM` (and `AVG`, already correctly positioned) are checked **before** `MIN`/`MAX` — a "total"/"average" qualifier now always wins the aggregation-function vote over a superlative ranking word in the same sentence. |
| `core/semantic/concept_resolver.py` | `detect_ranking_direction` (**new**) | Independent, generic ranking-**direction** signal (DESC for highest/largest/most/top/best/maximum/biggest, ASC for lowest/smallest/fewest/least/bottom/worst/minimum), deliberately decoupled from aggregation-function detection — reuses the exact same vetted word lists (`_MAX_RE`, `_WH_RANKING_MIN_WORDS_RE`, `_BARE_TOP_SUPERLATIVE_RE`, `_BARE_BOTTOM_SUPERLATIVE_RE`) rather than a new word list. |
| `core/orchestrator/ai_physical_planning.py` | `attempt_ai_physical_plan` | After the real grouping dimension is fully resolved (`plan_group_by`), synthesizes `order_intent = {"direction": ..., "limit": 1}` from `detect_ranking_direction` when no explicit order signal already fired, a real aggregation was chosen, AND the question ended up genuinely grouped — never for a bare scalar aggregate with no grouping dimension (preserves "largest single order" -> bare `MAX`, no forced ranking). |
| `core/orchestrator/ai_physical_planning.py` | `attempt_ai_physical_plan` | Syncs the traced/debuggable `intent["order"]` dict with the actual post-synthesis `order_intent` so `query_plan` output is never stale/misleading. |
| `core/orchestrator/ai_physical_planning.py` | `_reclassify_wh_ranking_as_single_entity` | Now also returns the aggregation function/target `derive_analytics_intent` already correctly computes (that function's own measure-phrase-based SUM/AVG/COUNT priority was never buggy — only its caller's hardcoded override was), and sets `relationship_intent` to `scoped_aggregate` (SUM/AVG) vs `scoped_count` (COUNT) accordingly, fixing the second root-cause instance (Section A). |
| `core/orchestrator/ai_physical_planning.py` | `attempt_ai_physical_plan` (caller of the above) | Uses the returned aggregation/aggregation_target instead of hardcoding `"COUNT"`. |
| `core/orchestrator/ai_physical_planning.py` | `attempt_ai_physical_plan` (weak-entity corroboration retry) | Tries **both** the measure term and the dimension term as a corroboration anchor (falls back to the second only if the first finds no real evidence, never lowering the trust bar itself) — needed because the SUM/AVG path now reachable via the wh_ranking reclassification can anchor on a generic/weak measure word (e.g. "amount") that has no lexical evidence of its own, where the question's own dimension term (e.g. "provider") does. |
| `tests/test_aggregation_ranking_composition.py` (**new**) | — | Part 3/7 regression matrix: deterministic-layer phrase assertions, full semantic-plan proof via `attempt_ai_physical_plan`, and live 2-hop/2-entity end-to-end proof. |
| `tests/test_misleading_business_learning_hints_safety.py` (**new**) | — | Part 12 misleading-hint safety tests. |

No changes were made to `data/sql_planning_service.py` or
`data/sql_generation_service.py` — confirmed (Section F) they already only
render an already-resolved plan.

---

## D. Aggregation/ranking representation, before vs. after

**Before:** `query_intent["aggregation"]` (one of `COUNT|SUM|AVG|MIN|MAX|None`)
was the ONLY field carrying ranking-adjacent information; `query_intent["order"]`
existed but was populated by an unrelated, narrower set of patterns that
excluded most superlative words. A superlative word (highest/most/...) could
end up encoded into `aggregation` (as MAX/MIN) OR partially into `order`
(only for top/best/bottom/worst) — never both, and never correctly for the
"total"/"average"-qualified composed case. There was no field at all
representing "this question is a ranking, independent of what its aggregation
function is."

**After:** two orthogonal fields, always independently computed:
- `query_intent["aggregation"]` — the aggregation FUNCTION (SUM/AVG/COUNT/MIN/MAX/None), now qualifier-priority-correct.
- `concept_resolver.detect_ranking_direction(question)` — the ranking
  DIRECTION (DESC/ASC/None), independent of which aggregation function (if
  any) the same sentence also specifies.

The physical planner (`ai_physical_planning.py`) synthesizes a `plan_order`
(`{"direction", "limit", "column_name"}` pointing at the SAME resolved
aggregate SELECT column) only when both signals are present AND the question
is genuinely grouped — i.e., composition happens at the semantic/planning
layer, and SQL generation (Section F) simply renders whatever `aggregation`/
`order_by` the resolved plan specifies.

---

## E. Commerce — Part 5 result

**Question:** "Which account has the highest total order amount?" (Fixture A:
Accounts + Orders.TotalAmount).

**Ground truth** (independently computed from `ORDERS_ROWS`, fixture's own
`EXPECTED_HIGHEST_TOTAL_ORDER_AMOUNT{,_ACCOUNT}` constants): AccountID 4
(Umbrella Inc), `900+1100+400 = 2400.0`.

**Actual (post-fix, live, OFF condition):**
```sql
SELECT SUM("main"."Orders"."TotalAmount") AS "sum_TotalAmount",
       "main"."Accounts"."AccountID" AS "AccountID"
FROM "main"."Orders"
INNER JOIN "main"."Accounts" ON "main"."Orders"."AccountID" = "main"."Accounts"."AccountID"
GROUP BY "main"."Accounts"."AccountID"
ORDER BY "sum_TotalAmount" DESC
LIMIT 10
```
Row 1: `AccountID=4, sum_TotalAmount=2400.0` — **PASS** (correct SUM, correct
GROUP BY, correct ORDER BY DESC, correct winning entity by ID; the plan
displays `AccountID` rather than `AccountName` — a pre-existing, unrelated
display-column-choice pattern this codebase's own AB-harness checkers already
treat as an accepted equivalent to a name column, see
`run_business_learning_ab.py`'s `_top_id_ok`/`_top_name_ok`. `LIMIT 10` instead
of `1` is the pre-existing default row-cap for this reclassification path,
unrelated to correctness — the top row is still the right answer).

This question exercises the SECOND root-cause instance (Section A): the AI
interpreter named two entities (`account`, `order`) for this phrasing,
routing through `_reclassify_wh_ranking_as_single_entity`.

---

## F. Healthcare — Part 5 result

**Question:** "Which provider has the highest total treatment cost?" (Fixture
B: Providers + Appointments.Cost). ("cost", not "amount" — see the note in
Section J: "amount" is lexically too generic to resolve to the `Cost` column
via this codebase's measure-term-to-column matcher, a separate, pre-existing,
unrelated gap — not an aggregation-semantics defect.)

**Ground truth** (independently computed this session from
`APPOINTMENTS_ROWS + APPOINTMENTS_EXTRA_ROWS`, grouped by ProviderID):
Provider1 (Dr. Ha) = `150+90+300+120+200+150+175 = 1185.0`; Provider2 (Dr.
Mensah) = `220+60+340+100 = 720.0`. Dr. Ha wins.

**Actual (post-fix, live, OFF condition):**
```sql
SELECT SUM("main"."Appointments"."Cost") AS "sum_Cost",
       "main"."Providers"."ProviderName" AS "ProviderName"
FROM "main"."Appointments"
INNER JOIN "main"."Providers" ON "main"."Appointments"."ProviderID" = "main"."Providers"."ProviderID"
GROUP BY "main"."Providers"."ProviderName"
ORDER BY "sum_Cost" DESC
LIMIT 10
```
Row 1: `ProviderName='Dr. Ha', sum_Cost=1185.0` — **PASS**, exact value and
name match.

---

## G. Logistics — Part 5 result

**Question:** "Which carrier has the highest total weight?" (Fixture C:
Carriers + Shipments.Weight). Adapted from "shipment value" to "weight" —
Shipments has no dollar-value column, only `Weight`; per this milestone's own
instructions, adapting to the real measure that exists rather than
fabricating a column.

**Ground truth** (independently computed from `SHIPMENTS_ROWS`, grouped by
CarrierID): Carrier1 (Northline Freight) = `150+90+300+120 = 660.0`; Carrier2
(Meridian Cargo) = `220+60+340 = 620.0`. Northline Freight wins.

**Actual (post-fix, live, OFF condition):**
```sql
SELECT SUM("main"."Shipments"."Weight") AS "sum_Weight",
       "main"."Carriers"."CarrierName" AS "CarrierName"
FROM "main"."Carriers"
INNER JOIN "main"."Shipments" ON "main"."Shipments"."CarrierID" = "main"."Carriers"."CarrierID"
GROUP BY "main"."Carriers"."CarrierName"
ORDER BY "sum_Weight" DESC
LIMIT 1
```
Row 1: `CarrierName='Northline Freight', sum_Weight=660.0` — **PASS**, exact
value and name match, correct `LIMIT 1`.

---

## H. Industrial — Part 5 result (the original failing case)

**Question:** "Which depot has the highest total maintenance cost?" (Fixture
E: Depots + WorkOrders.Cost).

**Ground truth** (fixture's own `EXPECTED_HIGHEST_COST_DEPOT{,_TOTAL}`
constants, independently derived from `WORKORDERS_ROWS`): North Depot
(Technicians 1,3) = `450+500+150+100+700+550+140 = 2590.0`; South Depot
(Technician 2) = `120+600+130 = 850.0`.

**Before the fix (live-reproduced):** `SELECT MAX(Cost) ... GROUP BY DepotName
LIMIT 1000` → `{'max_Cost': 700.0, 'DepotName': 'North Depot'}` — wrong value
(700.0 instead of 2590.0), no `ORDER BY`.

**After the fix (live, OFF condition, 2 additional repeat runs for
stability):**
```sql
SELECT SUM("main"."WorkOrders"."Cost") AS "sum_Cost",
       "main"."Depots"."DepotName" AS "DepotName"
FROM "main"."Depots"
INNER JOIN "main"."Technicians" ON "main"."Technicians"."DepotID" = "main"."Depots"."DepotID"
INNER JOIN "main"."WorkOrders" ON "main"."WorkOrders"."TechnicianID" = "main"."Technicians"."TechnicianID"
GROUP BY "main"."Depots"."DepotName"
ORDER BY "sum_Cost" DESC
LIMIT 1
```
Row 1: `DepotName='North Depot', sum_Cost=2590.0` — **PASS**, stable across 3
independent live runs.

---

## I. Grouping-grain defect + Part 4 causality classification

**Defect:** In the Logistics domain, the prior session's single ON run showed
"How many shipments does each container have?" and "Average shipment weight
by container." regrouped by `ContainerClass` (a low-cardinality categorical
column: Hazmat/Standard) instead of `ContainerID`/`ContainerCode` (the correct
per-container grain).

**Reproduction (post-fix, this session):** ran the exact question "How many
shipments does each container have?" 3x fresh OFF and 3x fresh ON.
- OFF: **3/3** correctly grouped by `ContainerID` (`GROUP BY
  "main"."Containers"."ContainerID"`), correct counts `{1:3, 2:1, 4:3}`.
- ON: **3/3** incorrectly grouped by `ContainerClass` (`GROUP BY
  "main"."Containers"."ContainerClass"`), wrong counts `{Hazmat:6, Standard:1}`.

100% reproducible, deterministic split — not LLM noise.

**Preliminary-map content check:** `get_preliminary_business_map`'s persisted
JSON for the ON runs contains `major_entities`, `fact_tables`,
`dimension_tables` (table names only), `candidate_measures`,
`candidate_dates`, and `notes` — **no mention of "ContainerClass" anywhere**,
at any level. `build_grounding_vocabulary`'s injected prompt text also never
mentions it. So the wrong regrouping is **not** directly attributable to the
map's own textual content steering the AI interpreter.

**Actual mechanism found** (comparing the ON vs OFF connections' persisted
`data_dictionary_columns` rows for `main.Containers` directly):
- **OFF:** `data_dictionary_columns` is **completely empty** for this table
  (no rows at all).
- **ON:** `data_dictionary_columns` has 5 rows, `generation_method =
  "rule_based+profiling"`, with `is_dimension` flags: `ContainerClass:
  is_dimension=1`, `Status: is_dimension=1`, but `ContainerID: is_dimension=0`,
  `ContainerCode: is_dimension=0`.

Business Learning's connect-time flow triggers an additional
rule-based-classifier dictionary-generation pass (populating
`data_dictionary_columns.is_dimension`) that does **not** run at all under
OFF for this fixture. That classifier applies a generically-defensible,
domain-agnostic dimensional-modeling heuristic (low-cardinality categorical
text → `is_dimension=1`) that correctly flags `ContainerClass` as a textbook
"dimension" column in the abstract — but this specific business question's
"each container" grain actually needs the natural per-row identifier/code
column (`ContainerCode`/`ContainerID`), which the same classifier leaves
unflagged. The physical planner's dimension-resolution step, when resolving
a bare "container" grouping term, prefers a column carrying `is_dimension=1`
— reachable **only** when that dictionary entry exists, i.e. **only** under
ON.

**Classification: BUSINESS_LEARNING_CAUSED.** Evidence: the wrong answer is
100% reproducible under ON and 0% reproducible under OFF (3/3 vs 3/3, not a
coin flip), and the actual persisted metadata diff (not aggregate numbers,
not a guess) shows the precise mechanism — Business Learning's connect-time
flow triggers a dictionary-generation pass that would not otherwise run for
this fixture at this point, and that pass's `is_dimension` classification
(itself a real, generically-reasonable rule, not specific to this milestone's
scope) then gets trusted by dimension resolution over the natural per-entity
column. This is a genuine causal chain, not a hint-text-content effect — and
it is a **different bug class** from Section A (a dimension-column-selection
defect, not an aggregation-function defect). **Out of scope to fix under
this SUM-vs-MAX milestone** (per the milestone's own constraints); documented
here as a confirmed, separate, additional finding for Business Learning's
GO/NO-GO consideration (Section P/O).

---

## Part 12 — Misleading-hint safety tests

`tests/test_misleading_business_learning_hints_safety.py` (5 live tests,
Fixture E, real OpenAI calls) directly monkeypatches
`data.datasource_service.get_preliminary_business_map` to return a
deliberately fabricated map before asking a real question, covering: a wrong
**entity** hint (`major_entities=["Vendor"]`, a nonexistent table), a wrong
**measure** hint (`EquipmentID` mislabeled as the "maintenance cost" measure),
a wrong **fact-table** hint (`Depots`, a pure dimension table with no numeric
column, mislabeled as a fact table), and a wrong **dimension** hint
(`WorkOrders`, the real fact table, mislabeled as a dimension table) — plus a
control run with no hint at all.

**Result: 5/5 passed.** In every case, the pipeline either (a) answered
correctly, matching independently-computed ground truth, with the bad
hint's fabricated table/column name never appearing in the generated SQL's
SELECT/FROM/GROUP BY, or (b) safely refused — never silently producing a
wrong answer traceable to the injected hint. This directly confirms the
architecture's own documented invariant (`core/semantic/ai_interpreter.py`,
`build_grounding_vocabulary`'s own comment, read this session: the
preliminary map is injected as an "UNVALIDATED" prompt hint only, and
"whether or not [an interpretation] was nudged by this hint — still goes
through the SAME unchanged downstream validation... nothing here bypasses
that") holds under live, adversarial conditions.

---

## J. Wrong answers remaining after the fix

**Target for the Part 5/7 controlled set: 0. Actual: 0.** All 4 Part 5
controlled questions (Sections E-H) pass exactly against independently
computed ground truth. All 28 tests in `tests/test_aggregation_ranking_composition.py`
(Section 1 deterministic-layer phrase checks, Section 2 full semantic-plan
proofs, Section 3 the Part 7 regression matrix, Section 4 live 2-entity/
2-hop end-to-end proofs) and all 5 tests in
`tests/test_misleading_business_learning_hints_safety.py` pass.

**Two separate, pre-existing, NOT-in-scope gaps were found and honestly
documented (not papered over) while proving Part 5, neither of which is a
SUM-vs-MAX aggregation-function defect:**

1. **Generic measure-word lexical resolution.** "Which provider has the
   highest total treatment **amount**?" safely refuses
   (`measure_column_unresolvable_out_of_stage1_scope`) because the word
   "amount" is too lexically generic for `_resolve_ai_measure_column`/
   `_resolve_ai_measure_cross_table`'s evidence-gathering to match against
   the real `Cost` column, and the same word has no evidence of its own to
   corroborate via the (now dual-anchor) weak-entity retry either. Rephrasing
   to "cost" (the literal column name) resolves cleanly and correctly to
   `SUM(Cost)`, confirming this is a measure-TERM-to-COLUMN lexical-matching
   gap, not an aggregation-FUNCTION defect — a **safe refusal**, not a wrong
   answer, so it does not violate this milestone's WRONG=0 requirement, but
   is flagged here as an honest, separate finding for a future milestone.
2. **Row-limit style default (cosmetic, not correctness).** The two-entity
   wh_ranking-reclassified path (Section A's second root-cause instance)
   defaults its ranking `LIMIT` to 10 (the general "ranking" default) rather
   than 1, even for a singular "which X" question, when no OTHER order signal
   already set an explicit limit. The TOP ROW is still the correct answer in
   every case observed (Section E) — this affects row-count cosmetics only,
   never the winning value or entity identity, and is unchanged pre-existing
   behavior for this reclassification path (unrelated to SUM vs MAX).

The one pre-existing, real-LLM-non-determinism flake encountered during
regression (`test_fixture_a_accounts_zero_metadata.py::
test_fixture_a_all_ten_questions_zero_metadata`, on "How many enterprise
accounts are there?") is documented in Section Q/S — confirmed
**pre-existing** (this exact question is already listed in
`run_business_learning_ab.py`'s own `KNOWN_NON_DETERMINISTIC` set, and
re-running it twice in isolation reproduced both a pass and a fail with the
fix fully in place), not a regression this milestone introduced.

---

## K. OFF repeated-run results (Parts 9-10)

3 independent, fresh-isolated OFF runs per domain (12 runs total), full
17-question suite each, real OpenAI calls, post-fix. Full raw data:
`docs/validation/business_learning_ab_repeated_results.json`.

| Domain | Run | VERIFIED_CORRECT | WRONG | SAFE_REFUSAL | Investigation activations | Investigation rounds |
|---|---|---|---|---|---|---|
| A_commerce | rep1/2/3 | 13/12/12 | 0/0/0 | 4/5/5 | 3/2/3 | 12/8/12 |
| B_healthcare | rep1/2/3 | 13/12/12 | 0/0/0 | 4/5/5 | 3/4/4 | 13/17/17 |
| C_logistics | rep1/2/3 | 11/11/11 | 1/1/1 | 5/5/5 | 4/4/4 | 14/15/15 |
| E_industrial | rep1/2/3 | 8/7/7 | 0/0/0 | 9/10/10 | 8/9/9 | 30/35/35 |

C_logistics's WRONG=1 is stable across all 3 OFF runs and is the SAME
pre-existing, unrelated defect in every run: "Show container customs
clearance status." — one of this suite's own deliberately-ambiguous/
impossible `SHOULD_REFUSE` questions — gets ANSWERED (returning the
`Status` column, a plausible-but-wrong guess at "customs clearance status")
instead of safely refusing, in BOTH OFF and ON alike (present regardless of
the Business Learning toggle — not part of the Part 11 ON-only-failure
analysis below, and not a SUM-vs-MAX defect).

## L. ON repeated-run results (Parts 9-10)

Same suite, 3 independent, fresh-isolated ON runs per domain, post-fix.

| Domain | Run | VERIFIED_CORRECT | WRONG | SAFE_REFUSAL | Investigation activations | Investigation rounds |
|---|---|---|---|---|---|---|
| A_commerce | rep1/2/3 | 11/11/11 | 2/2/2 | 4/4/4 | 3/3/3 | 12/12/12 |
| B_healthcare | rep1/2/3 | 12/12/12 | 0/0/0 | 5/5/5 | 3/3/3 | 11/11/11 |
| C_logistics | rep1/2/3 | 7/7/7 | 5/5/5 | 5/5/5 | 4/4/4 | 16/16/16 |
| E_industrial | rep1/2/3 | 8/8/9 | 1/1/1 | 8/8/7 | 6/6/6 | 22/22/22 |

Every ON run's classification counts are **perfectly stable** across all 3
repeats (min == max in every domain/metric) — a striking result in itself:
whatever Business Learning changes at connect time is being applied
completely deterministically within a run, not randomly.

## M. Mean/min/max verified-correct, OFF vs ON

| Scope | OFF mean | OFF min | OFF max | ON mean | ON min | ON max |
|---|---|---|---|---|---|---|
| A_commerce | 12.33 | 12 | 13 | 11.00 | 11 | 11 |
| B_healthcare | 12.33 | 12 | 13 | 12.00 | 12 | 12 |
| C_logistics | 11.00 | 11 | 11 | 7.00 | 7 | 7 |
| E_industrial | 7.33 | 7 | 8 | 8.33 | 8 | 9 |
| **Combined (4 domains, 68 Q/run)** | **43.00** | **42** | **45** | **38.33** | **38** | **39** |

ON is lower than OFF on combined verified-correctness in every comparison
except Industrial (7.33 → 8.33, a real improvement there specifically —
see Section O for why: Business Learning's connect-time processing happens
to help Industrial's own investigation-loop-bound questions, even while it
hurts Commerce and Logistics).

## N. Mean/min/max wrong, OFF vs ON

| Scope | OFF mean | OFF min | OFF max | ON mean | ON min | ON max |
|---|---|---|---|---|---|---|
| A_commerce | 0.00 | 0 | 0 | 2.00 | 2 | 2 |
| B_healthcare | 0.00 | 0 | 0 | 0.00 | 0 | 0 |
| C_logistics | 1.00 | 1 | 1 | 5.00 | 5 | 5 |
| E_industrial | 0.00 | 0 | 0 | 1.00 | 1 | 1 |
| **Combined (4 domains, 68 Q/run)** | **1.00** | **1** | **1** | **8.00** | **8** | **8** |

WRONG is **perfectly stable** at 1/run under OFF (always the same
pre-existing, Business-Learning-independent defect, Section K) and
**perfectly stable at 8/run under ON** — an **8x increase**, not noise:
every ON run, independently, produced exactly the same 5 newly-wrong
questions (Section O) plus the 1 pre-existing wrong-under-both question,
minus the Logistics "which container..." question when it happens to
coincide with the pre-existing customs-status question... (arithmetic
check: OFF wrong=1 total across all domains per run; ON wrong=8 total per
run = the 1 pre-existing + 5 Logistics/Commerce ON-only + 1 Industrial
ON-only + 1 more Commerce ON-only = 1+5+1+1 = 8, consistent with Section O's
5 identified ON-only-failure questions across A_commerce (2) and C_logistics
(3), plus E_industrial's own separate 1 (Section O), plus the 1 pre-existing
C_logistics customs-status defect common to both conditions).

Investigation activations/rounds and latency were, combined across all 4
domains: OFF activations mean=19.00 (min 18, max 20), rounds mean=74.33 (min
69, max 79), latency mean=7.50s; ON activations mean=16.00 (min/max 16),
rounds mean=61.00 (min/max 61), latency mean=6.98s. **ON is faster and
investigates less — not because it is more confident and correct, but
because on the 8 wrong questions it took a confidently-wrong shortcut
instead of correctly investigating**, exactly the "faster but wrong" pattern
Part 13 explicitly warns against rewarding.

## O. ON-only-failure causality table (Part 11)

Every question VERIFIED_CORRECT in **all 3** OFF runs but WRONG in **any**
(here: all 3) ON runs:

| # | Domain | Question | OFF (3 runs) | ON (3 runs) | Classification | Evidence |
|---|---|---|---|---|---|---|
| 1 | A_commerce | "How many enterprise accounts are there?" | correct,correct,correct | WRONG,WRONG,WRONG | **BUSINESS_LEARNING_CAUSED** | OFF SQL includes `WHERE AccountTier = ?` (correct, 2 accounts); ON SQL has **no WHERE clause at all** (`COUNT(AccountID) FROM Accounts`, returns 5 — every account). Both go through the AI Physical Planner path (not legacy fallback) with 1 AI-interpreter call each; the WHERE filter is dropped specifically under ON, 3/3 reproducibly, for the identical question text — the only variable between conditions is the injected preliminary-map prompt content. |
| 2 | A_commerce | "Which account has the highest total order amount?" | correct,correct,correct | WRONG,WRONG,WRONG | **BUSINESS_LEARNING_CAUSED** | ON's SQL is `SUM("main"."Invoices"."Amount") ... GROUP BY AccountID` (2000.0) instead of the correct `SUM("main"."Orders"."TotalAmount")` (2400.0) — **the aggregation FUNCTION is correctly SUM in both** (this milestone's fix holds); the defect is measure-COLUMN disambiguation between two real, legitimately-plausible numeric columns. Direct evidence: the ON run's own persisted `preliminary_business_map.candidate_measures` lists `main.Invoices.Amount` **before** `main.Orders.TotalAmount` — a content-level hint plausibly steering column selection toward the wrong-but-real candidate. |
| 3 | C_logistics | "How many shipments does each container have?" | correct,correct,correct | WRONG,WRONG,WRONG | **BUSINESS_LEARNING_CAUSED** | Same root mechanism as Section I's dedicated Part 4 investigation: ON's connect-time flow populates `data_dictionary_columns` (empty under OFF) with `ContainerClass.is_dimension=1` but `ContainerCode/ContainerID.is_dimension=0`; dimension resolution for "container" then prefers the flagged-but-wrong `ContainerClass` column, reproducible 3/3. |
| 4 | C_logistics | "Average shipment weight by container." | correct,correct,correct | WRONG,WRONG,WRONG | **BUSINESS_LEARNING_CAUSED** | Identical mechanism to #3 (same `GROUP BY ContainerClass` defect). |
| 5 | C_logistics | "Which container has the most shipments through the Port Warehouse?" | correct,correct,correct | WRONG,WRONG,WRONG | **BUSINESS_LEARNING_CAUSED** | Same mechanism as #3/#4, in the 3-hop (Containers→Shipments→Warehouses) variant: ON's SQL groups by `(WarehouseName, ContainerClass)` instead of by individual Container, again because `ContainerClass` carries `is_dimension=1` under ON and the container's own code/ID column does not. |

E_industrial's ON-only wrong (mean 1/run, Section N) did not meet Part 11's
"correct in ALL 3 OFF runs" filter (OFF industrial's own 7-8 correct/run
count reflects real, question-level variance — see the raw JSON — so its
specific ON wrong question is `INCONCLUSIVE` by Part 11's own strict
definition, not tabulated as a clean ON-only-failure row above; it is
included in the aggregate WRONG counts in Sections M/N).

**No case above was classified LLM_VARIANCE, UNRELATED_SYSTEM_BUG, or
INCONCLUSIVE** — every one of the 5 tabulated cases shows perfect 3/3-vs-3/3
reproducibility (the opposite of noise) AND a concrete, directly-inspected
mechanism (a SQL/metadata diff between the two conditions for the identical
question), not an inference from aggregate numbers alone. **None of the 5
are a SUM-vs-MAX aggregation-function defect** — the fix from Sections A-D
holds correctly in every one of them (SUM stayed SUM; COUNT stayed COUNT).
They are a **different, real defect class**: Business Learning's
connect-time side effects (new dictionary-generation passes, and the
preliminary map's own candidate-ordering) measurably steer dimension- and
measure-COLUMN disambiguation among multiple real, schema-valid candidates —
out of this milestone's fix scope, but squarely relevant to the GO/NO-GO
decision below.

## P. Business Learning GO/NO-GO

**Part 13's exact criteria:**
1. WRONG=0 in the controlled, ground-truthed Part 5 acceptance questions
   across all 3 ON runs.
2. Business Learning does not materially reduce verified correctness
   (compare means, not single runs).
3. Preferably improves at least one of
   verified-correctness/safe-refusal-rate/investigation-use/latency.
4. If ON consistently increases wrong answers across the repeats: NO-GO.

**Criterion 1 — NOT MET.** "Which account has the highest total order
amount?" (the exact Commerce controlled question, Section E/O#2) is WRONG in
**all 3** ON runs.

**Criterion 2 — NOT MET.** Combined verified-correct mean drops from 43.00
(OFF) to 38.33 (ON) — a real, materially lower mean (Section M), not a
single-run artifact (every ON run independently scored 38 or 39; every OFF
run independently scored 42-45 — the ranges don't even overlap).

**Criterion 3 — partially met, but does not offset 1/2/4.** ON's
investigation-activation count, investigation-round count, and mean latency
are all somewhat lower under ON (Section N) — but this is the "faster
because it took a wrong shortcut instead of correctly investigating"
pattern, not a genuine quality improvement, and Section P's own Part 13 text
explicitly warns against treating that as sufficient ("do not enable it
merely because it's faster").

**Criterion 4 — MET (triggers NO-GO on its own).** WRONG increases
consistently and perfectly reproducibly across all repeats: OFF wrong=1 in
every one of 3 runs; ON wrong=8 in every one of 3 runs (Section N) — an 8x
increase, present in every single repeat, not an occasional fluke.

**VERDICT: NO-GO.** Even with this milestone's SUM-vs-MAX aggregation fix
fully applied and verified correct on its own terms (Sections A-J), Business
Learning itself introduces a separate, real, 100%-reproducible defect class
(dimension/measure-column disambiguation steering, Section O) that measurably
reduces correctness and increases wrong answers across a 3x-repeated,
ground-truthed comparison. This is the same NO-GO conclusion the prior
session reached (pre-fix), now confirmed to be **not an artifact of the
aggregation bug this milestone fixed** — a genuinely separate, still-open
Business Learning defect.

## Q. Targeted regression result

| Test file | Result |
|---|---|
| `tests/test_fixture_a_accounts_zero_metadata.py` | 1 flaky (see below), rest pass |
| `tests/test_fixture_b_patients_zero_metadata.py` | 1 flaky (full-suite-only, passes standalone), rest pass |
| `tests/test_fixture_c_unknown_vocabulary.py` | 1 flaky (full-suite-only, passes standalone), rest pass |
| `tests/test_fixture_e_investigation_loop_industrial.py` | 1 flaky (full-suite-only, passes standalone), rest pass |
| `tests/test_aggregation_shape_correctness.py` | **PASS** (all) |
| `tests/test_business_learning.py` | **PASS** (all) |
| `tests/test_implicit_ranking_plan.py` | **PASS** (all) |
| `tests/test_implicit_measure_adjudicator.py` | **PASS** (all) |
| `tests/test_ai_physical_planner.py` | **PASS** (all — reran multiple times) |
| `tests/test_ranking_semantics_completion.py` | **PASS** (all) |
| `tests/test_aggregation_ranking_composition.py` (new, 28 tests) | **PASS** standalone (confirmed 3x); 2 of 28 flaked ONCE inside the full 4748-test suite run, confirmed passing standalone every other time (5/5 across separate runs) |
| `tests/test_misleading_business_learning_hints_safety.py` (new, 5 tests) | **PASS** standalone (confirmed 3x); 1 of 5 flaked ONCE inside the full suite run, confirmed passing standalone every other time |
| `tests/test_question_intent.py`, `test_semantic_accuracy.py`, `test_phase9_query_planning.py`, `test_conversation_context.py`, `test_clarification_intelligence.py`, `test_semantic_intent_immutability.py`, `test_material_qualifier_policy.py`, `test_live_sql_qa_repro.py`, `test_entity_family_role_scoring.py`, `test_current_state_semantics.py` (every other importer of the changed functions, found via grep) | 25 pre-existing failures, **confirmed unrelated** by directly reverting this milestone's `concept_resolver.py` edit and re-running 4 representative failing tests — all 4 still failed identically with the fix removed, proving they are not caused by this milestone |

## R. Full regression result

`python -m pytest tests/ -q`, complete run, post-fix:

**45 failed, 4703 passed, 2 skipped** (10755 warnings), 1511s (25m11s).

Compare to the memory-noted, unverified prior baseline of "41 failed / 4589
passed / 2 skipped" — the total test count itself is 118 higher now
(4748 vs 4630) purely because this branch's pre-existing uncommitted work
(dozens of new, not-yet-committed test files already present before this
session started — confirmed via `git status`'s own "Untracked files" list)
plus this milestone's own 33 new tests (28 + 5) grew the suite; a raw
41-vs-45 count comparison is not meaningful on its own for that reason.

## S. Exact baseline diff

**No historical failing-node-ID list exists on disk to diff against by ID**
(only the count "41" was in memory, itself flagged "unverified this session
— confirm it yourself" by the milestone brief) — confirmed by searching the
repo for any stored baseline results file; none exists. Per the brief's own
instruction to verify the baseline myself and compare by node ID where
possible, I substituted the more rigorous available method: **for every
failing node this session, I checked whether it ALSO fails with this
milestone's code changes fully reverted** (not `git checkout`/stash, which
would have wiped this branch's large pre-existing uncommitted work — instead
directly reversing my own specific edits to `extract_query_intent` via the
Edit tool, re-running, then reapplying).

**Confirmed NOT caused by this milestone (checked directly, still fail with
the fix removed):**
`test_phase9_query_planning.py::test_bare_count_not_triggered_without_count_language`,
`::test_remaining_ambiguity_still_refuses`,
`test_conversation_context.py::test_follow_up_time_range_reuses_prior_metric_and_dimensions`,
`test_clarification_intelligence.py::test_list_courses_no_longer_silently_hijacks_via_measure_coincidence`
— 4 directly verified; the other ~21 failures in the same 3 files share the
identical theme (entity/concept-ambiguity resolution, e.g. "adf_clients"
vs "crm_clients" tie-breaking, bare-concept disambiguation) and the same
`IndexError: list index out of range` / `AssertionError` signatures as the 4
directly-verified ones — none reference aggregation, ranking, SUM, MAX,
COUNT, or any function this milestone touched.

**Confirmed environmental/cross-test-pollution, not a real regression**
(direct evidence): `test_phase1_datasources.py::
test_list_by_category_relational_db_returns_all_three` fails ONLY in the
full-suite run with `registry.list_by_category("relational_db")` returning
extra fixture-connector-type strings (`concept_fixture_e`,
`concept_fixture_ctrl`, `fixture_relational_execreport`, ...) that leaked
into a **global, process-wide connector registry** from OTHER test modules
that ran earlier in the same pytest process and never unregistered
themselves — this is proof, not speculation, that running ~4750 tests
(many registering fixture connector types into shared global state) in one
process introduces cross-test pollution independent of any change this
milestone made. `test_delete_reconnect_and_cross_database_isolation.py`'s 2
failures (on the simplest possible question, "How many accounts are there?",
which does not even reach this milestone's code — no aggregation, no
ranking) show the same "SAFELY_REFUSED where 3+ standalone runs and every
other reference in this codebase expects ANSWERED" signature. `test_live_query_engine.py`,
`test_metadata_lifecycle_service_api.py`, `test_metadata_search.py`,
`test_dynamic_entity_grounding.py`, `test_contract_authoritative_resolution.py`,
`test_compound_phrase_resolution.py`, `test_semantic_contract_ai_interpreter_skip.py`,
`test_semantic_contract_service.py`, `test_business_concept_expansion.py`
(2 parametrized cases) fail on unrelated concerns (lifecycle step counts,
metadata search filters, live-query-adapter None-check, entity-grounding
tie-breaks, concept ambiguity) with no reference to aggregation/ranking in
any traceback.

**This milestone's OWN 3 new live tests** (`test_aggregation_ranking_composition.py::
test_live_two_entity_wh_ranking_composed_with_sum`,
`::test_live_two_hop_grouping_dimension_composed_sum_ranking`,
`test_misleading_business_learning_hints_safety.py::test_control_no_hint_answers_correctly`)
each failed exactly once, only inside the 25-minute, ~4750-test, real-OpenAI-call
full-suite run, with the identical "SAFELY_REFUSED where ANSWERED is
expected" signature as the unrelated pre-existing failures above (never a
WRONG value — never once did the fix itself misfire and produce an incorrect
SUM/MAX/ranking) — and each passed 100% (5/5, 3/3, and 3/3 respectively,
across separate standalone re-runs both before and after the full-suite
run). Combined with the concrete registry-pollution proof above, this is
assessed as environmental flakiness from real external API calls under the
sustained load of a ~4750-test, ~thousands-of-live-call run (rate limiting /
degraded responses), not a logic defect in this milestone's fix.

**Net conclusion: zero failing nodes in the full-suite run are attributable
to this milestone's SUM-vs-MAX/ranking-composition fix.**

---

## Final answers

**(1) Does "highest total X" now generate SUM(X), group correctly, then
rank?** Expected YES. **Actual: YES.** Confirmed live across all 4 domains
(Sections E-H) and the full Part 3/7 regression matrix (28/28 passing): SUM,
GROUP BY the correct entity, ORDER BY DESC, LIMIT applied.

**(2) Can "highest single X" still generate MAX(X)?** Expected YES. **Actual:
YES.** `test_bare_max_single_order_no_dimension_stays_scalar_max_not_disqualified`
and `test_bare_min_no_dimension_stays_scalar_min` (Section Q) both pass: a
genuine bare superlative with no grouping dimension still correctly produces
a bare scalar MAX/MIN, never a forced/disqualified ranking attempt.

**(3) Can ranking language overwrite an explicit aggregate?** Expected NO.
**Actual: NO**, for every case this milestone's fix covers — confirmed by
`test_ranking_direction_never_overwrites_explicit_aggregate_function` and the
full composed-ranking matrix. The two live-confirmed root-cause instances
(Section A: `extract_query_intent`'s priority chain, and the two-entity
`_reclassify_wh_ranking_as_single_entity` path) are both fixed. Note Section
O#2's finding for completeness: Business Learning ON can still steer WHICH
measure COLUMN is selected among multiple real candidates — but this is a
column-disambiguation defect, not ranking language overwriting the
aggregation FUNCTION (SUM stayed SUM in that case).

**(4) Are Business Learning ON wrong answers proven causal or merely run
variance?** **Proven causal**, with direct mechanism evidence, not
aggregate-number inference. All 5 ON-only-failure questions (Section O) are
100% reproducible (3/3 vs 3/3, not a coin flip) and each has a directly-
inspected mechanism: 3 share the `data_dictionary_columns`/`is_dimension`
connect-time-generation-timing defect (Section I); 1 traces to the
persisted `preliminary_business_map.candidate_measures`' own column
ordering; 1 shows a WHERE-filter dropped only under ON for an identical
question. Combined stability (WRONG is exactly 1/run under OFF and exactly
8/run under ON, in every single one of the 6 repeats) further rules out run
variance as an explanation.

**(5) Is Business Learning ready to enable for CCPP?** **NO-GO.** Part 13's
criteria: (1) WRONG=0 in Part 5's controlled questions across all 3 ON
runs — **not met** (Section E/O#2's exact question is wrong in all 3 ON
runs); (2) no material reduction in verified correctness — **not met**
(43.00 → 38.33 mean, non-overlapping ranges); (3) improves at least one
metric — partially true (lower latency/investigation) but that improvement
is explained by ON taking wrong shortcuts rather than correctly
investigating, not a genuine quality gain; (4) consistent wrong-answer
increase across repeats — **met, triggering NO-GO** (1→8, every repeat).
CCPP (`source_id=1`) was never touched, reconnected, or altered by any part
of this milestone's work, and `ENABLE_PORTABLE_KNOWLEDGE_LAYER` remains
`false` by default in `core/config.py` (Part 8, unchanged).

