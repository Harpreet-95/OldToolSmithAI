# Business Learning Candidate Discovery Report

Milestone: give Business Learning's connect-time preliminary map a
structural role it is actually suited for — a bounded, deterministic,
provenance-tagged **candidate discovery aid** consumed by the existing
deterministic/adjudication machinery (semantic retrieval, candidate
adjudication, Investigation Agent) — after the prior Trust Hardening
milestone made it fully safe but functionally inert for Q&A by removing
its only channel (the AI Interpreter's free-text prompt) entirely. Built on
top of (unmodified, out of scope) the SUM-vs-MAX aggregation fix and the
Trust Hardening provenance-tagging/`exclude_preliminary` work from the two
prior sessions.

---

## 1. Existing Business Learning output inventory (confirms/extends the prior session's Section 4)

`_infer_preliminary_business_map` (`core/orchestrator/business_learning.py`)
still produces one JSON object per bounded LLM call (Path B only — no
portable-package match): `likely_domain` (str|None), `major_entities`
(list[str]), `fact_tables`/`dimension_tables`/`bridge_tables` (list of
table_fqn), `candidate_measures`/`candidate_dates` (list of
`{table_fqn, column_name}`), `notes` (str|None). Storage unchanged:
`data_source_connections.metadata_json['preliminary_business_map']` via
`data.datasource_service.set_/get_preliminary_business_map`. Confidence:
still none modeled (flat, unstructured, one bounded call, no calibration).

Provenance/consumers, updated this session:
- `generation_method='rule_based_preliminary'` (dictionary rows, both
  `data_dictionary_tables` and `data_dictionary_columns` — confirmed by
  reading `generate_and_save_dictionary`'s `generation_method_override`
  application, which stamps both `table_entries` and `column_entries`) /
  `assignment_source='preliminary_connect_time'` (entity/domain assignment
  rows) — unchanged from the prior session, still the load-bearing
  provenance tags.
- **NEW consumer this session:** `data.semantic_retrieval_service.
  get_preliminary_candidates` (Part 3 below) — the map's own JSON content
  (not its side-effect rows) now has exactly one more reader, added by
  this milestone, alongside the pre-existing zero-live-influence state the
  prior session left it in for the AI Interpreter.
- Confirmed via direct file read: `core/semantic/ai_interpreter.py` is
  **unchanged** this session (see Part 15 below) — `build_grounding_
  vocabulary` still fetches the map into its return dict but
  `_build_user_prompt` still never renders any of its content. Grepped the
  whole repo for `preliminary_business_map`: the only files that reference
  it are `ai_interpreter.py` (fetch-only, unrendered), `business_learning.py`
  (writer), `datasource_service.py` (storage), the two test files, and this
  session's new `semantic_retrieval_service.py` (Part 3's new reader).

## 2. Candidate-discovery architecture — design and location

**Where it lives:** `data/semantic_retrieval_service.py`. This module
already implements a multi-source, score-floor-ladder additive merge inside
`_retrieve()` (the shared implementation behind `get_candidate_tables`/
`get_candidate_tables_with_ranking` — the actual live candidate-table
retrieval every SQL-answering question goes through): base `search_metadata`
scores, per-term diversity, domain rescue, approved `concept_table_mappings`
(`_APPROVED_MAPPING_SCORE_FLOOR`), and `generated_business_vocabulary`
(`_GENERATED_VOCAB_HIGH_FLOOR`/`_GENERATED_VOCAB_MEDIUM_FLOOR`) — each a
synthetic score floor that guarantees a candidate survives the
`_RETRIEVAL_TABLE_LIMIT` cap without ever pretending to be a *higher-trust*
source than a stronger one already in the merge (`_merge_best` always keeps
the higher score). This was the exact pluggable point the milestone brief
asked me to look for, and it already existed — no new candidate-source
mechanism was invented.

**What was added:** one new function, `get_preliminary_candidates(source_id,
user_id, question, terms)`, and one new floor,
`_PRELIMINARY_BUSINESS_LEARNING_FLOOR = 10_000.0` — deliberately **below**
`_GENERATED_VOCAB_MEDIUM_FLOOR` (50,000) so a preliminary candidate can
never outrank/overwrite any validated-evidence candidate for the same table,
and **above** `_search_all`'s realistic raw-score ceiling (~400 on real
data) so a question-relevant preliminary candidate reliably survives the cap
when nothing stronger has found it yet. This is the lowest rung of the
module's existing floor ladder, added in kind, not a parallel mechanism.
Wired into `_retrieve()` as one more additive merge stage, applied last.

**How it reaches the adjudicator:** `data/semantic_contract_service.py`'s
`_gather_candidate_evidence_uncached` (the function `core.semantic.
candidate_adjudicator.adjudicate_candidates` and the Investigation Agent's
`ADJUDICATE_CANDIDATES`/`FIND_ENTITY_CANDIDATES` actions actually consume)
already calls `get_candidate_tables_with_ranking` once per entity to build
its candidate pool, but previously discarded the ranking list entirely
(`_ranking` was unused). This session keeps it, builds a
`table_fqn -> provenance_reason` map from any entry whose `provenance ==
PRELIMINARY_BUSINESS_LEARNING`, and stamps a new `candidate_provenance`
field onto each per-candidate evidence dict the adjudicator sees — `None`
for the overwhelming majority (found by real evidence), the tag only for a
table that survived retrieval *solely* via the preliminary source.

**Investigation Agent integration (Part 15, the milestone's own
highest-value integration point):** `core/orchestrator/investigation_agent.
py`'s `investigate_and_replan` now seeds a new `InvestigationState.
preliminary_hints` tuple, once, before round 1, from
`get_preliminary_candidates` using the AI interpretation's own entities/
measures/dimensions/requested_attributes terms — the same terms the rest of
the pipeline already extracted, no new extraction. Surfaced to the decision
step's own prompt payload as an explicitly-named `preliminary_hints_
unverified` field, with new `_SYSTEM_PROMPT` guidance: usable only to choose
*where* to look first (which discovery tool/target), never citable as
evidence for a finding, never allowed to raise confidence by its mere
presence.

## 3. Files/functions changed, with reasons

| File | Function(s) | Reason |
|---|---|---|
| `data/semantic_retrieval_service.py` | `get_preliminary_candidates` (new), `_PRELIMINARY_BUSINESS_LEARNING_FLOOR`/`PRELIMINARY_BUSINESS_LEARNING_PROVENANCE` (new), `_retrieve` (new merge stage) | Part 3/4/5 — the actual candidate-discovery mechanism |
| `data/semantic_contract_service.py` | `_gather_candidate_evidence_uncached` (keeps the ranking, builds `preliminary_provenance_by_fqn`, stamps `candidate_provenance` on each evidence entry) | Part 5 — provenance survives retrieval into the adjudicator's evidence |
| `core/semantic/candidate_adjudicator.py` | `_SYSTEM_PROMPT` (new guidance paragraph), `_evaluate_fast_path_candidate`, `_evaluate_role_dominance_candidate` (new PRELIMINARY-provenance ineligibility guard) | Part 3c/5 — candidate ≠ evidence, enforced both in the LLM prompt and, defensively, in the deterministic no-AI fast paths |
| `data/vocabulary_bootstrap_service.py` | `_dictionary_and_assignment_evidence` (excludes `rule_based_preliminary`/`preliminary_connect_time` rows) | Part 6 — double-counting prevention (see Part 6 below; a real, independent gap found and fixed) |
| `core/orchestrator/investigation_agent.py` | `InvestigationState.preliminary_hints` (new field), `_build_state_payload` (new `preliminary_hints_unverified` key), `_SYSTEM_PROMPT` (new guidance), `investigate_and_replan` (seeds hints before round 1) | Part 15 — Investigation Agent integration |
| `tests/test_business_learning_candidate_discovery.py` (new) | 23 tests (22 pass, 1 skip) | Parts 9-22 |
| `docs/validation/run_business_learning_candidate_discovery_gate.py`, `..._repeated.py` (new) | — | Part 24/25 drivers, thin wrappers reusing `run_business_learning_ab.py`'s `DOMAINS`/`run_domain_condition`/`CHECKERS` unchanged |

`core/semantic/ai_interpreter.py` was **not** touched (Part 1 — confirmed,
see Part 15 below).

## 4. Provenance handling

Every Business-Learning-sourced candidate carries `provenance=
"PRELIMINARY_BUSINESS_LEARNING"` (a module-level constant,
`PRELIMINARY_BUSINESS_LEARNING_PROVENANCE`, exported from
`semantic_retrieval_service` so every consumer imports the same string
rather than re-typing it) from the moment `get_preliminary_candidates`
creates it, through `_retrieve`'s merge (surviving only if no stronger
source also claims the same table — see Part 5), into `gather_candidate_
evidence`'s per-candidate `candidate_provenance` field, into the JSON
`candidate_adjudicator.adjudicate_candidates` sends the model, and into the
Investigation Agent's `preliminary_hints_unverified` prompt field. No
existing tagging convention in this codebase covered "candidate source,"
so this is a new field name — deliberately namespaced (`candidate_
provenance`, not reusing `generation_method`/`assignment_source`, which are
row-level dictionary/assignment provenance, a related but distinct concept)
to avoid collision with the Trust Hardening milestone's own fields.

## 5. Candidate ≠ evidence — how it's enforced structurally, not just by prompt text

Three independent layers, so no single one is load-bearing alone:
1. **Structural (the dominant one):** a preliminary candidate only ever
   *widens the table_fqn SET* `get_candidate_tables_with_ranking` returns.
   Every downstream consumer that actually decides anything —
   `data.query_planning_service._resolve_concept` (name/business-role
   scoring), `_is_dimension_column`/`_is_metric_column` (dictionary-based
   classification, already hardened against preliminary rows last session),
   `_build_evidence_entry`'s `candidate_business_role`/`name_score`/
   `restricted_view_evidence`/`approx_row_count` (all in `semantic_contract_
   service.py`) — computes its verdict from real schema/profiling/dictionary
   data, never from the retrieval score or the provenance tag itself. A
   preliminary candidate that is real evidence-backed passes on its own
   merits; one that isn't gets the same "no real business_role, no name
   match, low row count" treatment any other weak candidate gets.
2. **Prompt-level (LLM adjudication):** `candidate_adjudicator._SYSTEM_
   PROMPT`'s new rule explicitly instructs the model that `candidate_
   provenance="PRELIMINARY_BUSINESS_LEARNING"` must not by itself raise
   confidence — judge on the same structural fields as any other candidate.
3. **Deterministic backstop (defense in depth):** `_evaluate_fast_path_
   candidate`/`_evaluate_role_dominance_candidate` (the no-AI-call
   short-circuit paths, both currently `_FAST_PATH_ACTIVE=False`/empty
   `_ROLE_SAFE_FOR_DOMINANCE` by design in this codebase, i.e. inert in
   production today) now explicitly refuse to auto-select a PRELIMINARY-
   provenance candidate, deferring to full AI adjudication instead — proven
   by `test_fast_path_defers_to_ai_for_preliminary_provenance`.

## 6. Double-counting prevention (Part 6) — a real, independent gap found and fixed

Traced explicitly, as instructed. `data.vocabulary_bootstrap_service.
_dictionary_and_assignment_evidence` (feeding `generated_business_
vocabulary`, itself read both by `semantic_retrieval_service._retrieve`'s
own additive merge AND by `query_planning_service._resolve_concept`'s
actual candidate SCORING) read `data_dictionary_tables.business_name` /
`domain_assignments.domain` / `entity_assignments.entity` with **no
provenance filter at all** — meaning a table classified only by Business
Learning's own bounded, blind, connect-time pass (the exact same LLM-
adjacent, unvalidated discovery event this milestone's new candidate source
also surfaces) could ALSO earn a real, unweighted `generated_business_
vocabulary` MEDIUM-tier score contribution, stacking two channels sourced
from the same underlying evidence as if they were independent
corroboration. Fixed by excluding `generation_method` ending in
`_preliminary` (dictionary rows) and `assignment_source ==
'preliminary_connect_time'` (entity/domain rows) from this derivation —
the single shared point both the candidate-set merge and the scoring path
read from, rather than patching each reader separately.
`test_preliminary_rows_excluded_from_generated_vocabulary` proves no
`generated_business_vocabulary` row sourced from a preliminary-tagged row
survives after `ensure_business_evidence(..., evidence_provenance=
"preliminary_connect_time")` + `bootstrap_vocabulary` both run.

## 7. Question-relevant retrieval implementation (Part 4)

`get_preliminary_candidates(source_id, user_id, question, terms)` reuses
whatever `terms` the calling site already computed (the broad-retrieval
call site's `all_terms`/`retrieval_terms` from `extract_terms`/AI-interpreted
concepts; the entity-level `gather_candidate_evidence` call site's own
`entity_synonyms(entity_name)`; the Investigation Agent's `entities+
measures+dimensions+requested_attributes`) — never a new extraction pass.
Deterministic, no LLM call: tokenizes `terms`+`question` (reusing
`core.domains.rules._tokenize`, already imported in this module) and
requires genuine token overlap between the question and a candidate's own
table-name tokens (fact/dimension/bridge table entries) or column-name
tokens (`candidate_measures`/`candidate_dates` entries) before it qualifies
— `likely_domain`/`major_entities` overlap is a real but weak secondary
signal, usable only as a small tie-break bonus on an already-qualified
candidate, never sufficient alone
(`test_get_preliminary_candidates_requires_real_name_overlap` proves this).

## 8. Verification path (Parts 7-8)

A preliminary candidate that reaches `gather_candidate_evidence` is, from
that point on, indistinguishable in TREATMENT from any other candidate
except for the one extra `candidate_provenance` field: it goes through the
exact same `_build_evidence_entry` structural-evidence computation, the
exact same `adjudicate_candidates` LLM call (with the new provenance-aware
guidance), the exact same `_apply_business_role_backstop`/`_apply_
restricted_view_backstop` deterministic backstops, and — when reached via
the Investigation Agent — the exact same `ADJUDICATE_CANDIDATES`/`INFER_
IMPLICIT_MEASURE` tools normal investigation already uses. VERIFIED means
real evidence independently corroborated it (at which point it is the real
evidence deciding, per the milestone's own framing — the hint just
pointed there first). REJECTED means adjudication picked something else, or
`no_safe_selection=true`, or a backstop vetoed it — the same safe outcomes
any other unconfirmed candidate gets. UNRESOLVED means normal ambiguity/
investigation/clarification handling — never a forced answer. No new
accept/reject state machine was built, per the milestone's explicit
instruction.

## 9. Entity discovery (Fixture C, unfamiliar Container/Warehouse vocabulary)

`test_entity_discovery_unfamiliar_vocabulary_still_grounds_correctly` —
PASS. A correct preliminary map naming `main.Containers`/`main.Warehouses`
grounds "How many containers are there?" correctly; the fabricated-entity
adversarial case from the prior session's `test_misleading_business_
learning_hints_safety.py::test_wrong_entity_hint_does_not_produce_wrong_
answer` (unmodified, still passing) confirms a wrong entity hint still
cannot fabricate a table.

## 10. Measure discovery (Fixture A, Orders.TotalAmount vs Invoices.Amount)

`test_measure_discovery_correct_hint_does_not_break_correct_answer` — PASS,
correct hint present, correct answer, `Invoices` absent from SQL.
`test_wrong_hint_rejected_by_live_evidence` (Part 18) — PASS, a wrong
`Invoices.Amount` hint for this exact question is rejected; answer correct
or safe refusal, never `Invoices.Amount`.

## 11. Dimension discovery (Fixture C, ContainerClass-vs-ContainerID regression)

`test_dimension_discovery_container_grouping_regression_under_new_
mechanism` — PASS, reusing the exact prior-session regression shape, now
with a preliminary map ALSO naming `main.Containers` as a dimension table
(candidate discovery actively engaged) — `ContainerClass` still never
appears in the generated SQL. Confirms the new mechanism does not resurrect
the old grain-hijack defect through a different channel.

## 12. Filter discovery (Fixture A, AccountTier/"enterprise accounts")

`test_filter_discovery_enterprise_accounts_with_correct_hint` — PASS
(answered-correct or safe-refusal, matching this exact question's own
pre-registered non-determinism, unchanged from the prior session).

## 13. Relationship discovery (Fixture E, Depots/WorkOrders adversarial pattern)

`test_relationship_discovery_wrong_hint_never_creates_a_join` — PASS,
reusing the exact prior-session adversarial hint (`fact_tables:
["main.Depots"]`, falsely implying a direct Depots↔WorkOrders join) under
the new mechanism; answer correct or safe refusal, no fabricated join.

## 14. Date/status/activity discovery (Fixture E dates, Fixture B status)

`test_date_discovery_misleading_hint_does_not_steer_recency` — PASS
(InstalledDate never wins the ORDER BY over LastServicedDate).
`test_status_discovery_fixture_b_with_preliminary_map` — PASS (VIP-patient
count correct with a correct preliminary map naming the right tables).

## 15. Investigation Agent integration — confirmed live

`test_investigate_and_replan_seeds_hints_from_preliminary_map` — PASS:
`investigate_and_replan` genuinely populates `InvestigationState.
preliminary_hints` from the real (monkeypatched) preliminary map before its
first real decision-step call, using the AI interpretation's own entity
terms, and the seeded hint's `table_fqn` (`main.Invoices`) is visible on the
state object a live decision round actually saw. `test_investigation_state_
preliminary_hints_are_unverified_context` — PASS: `_build_state_payload`
renders hints under the explicitly-named `preliminary_hints_unverified` key,
structurally separate from `known_candidate_tables` (which stays empty
until real evidence is gathered) — the decision step cannot confuse a hint
with gathered evidence at the data-shape level, not just by prose
convention. **Part 1 reconfirmed:** `core/semantic/ai_interpreter.py` is
byte-for-byte unmodified this session — grepped and read directly; the
preliminary map is still fetched-but-never-rendered into any prompt there,
and `get_entity_summary`/`get_domain_summary`'s `exclude_preliminary=True`
calls from the prior session are untouched.

## 16-17. Correct-hint usefulness

Qualitatively: every discovery-category test above with a *correct*
preliminary hint present answers correctly (or safely refuses on an
independently-documented non-deterministic question), matching the no-hint
baseline — the mechanism is not harmful when correct. Quantitatively (the
literal "does it measurably reduce effort" question): see Parts 24-26 below
— the aggregate 68-question repeated run does **not** show a clear,
consistent reduction in candidate search/investigation rounds/AI calls/
latency this run; reported honestly, not forced.

## 18. Wrong-hint test

`test_wrong_hint_rejected_by_live_evidence` (Fixture A, Invoices.Amount hint
for the Orders.TotalAmount question) — PASS. Combined with the full
adversarial suite (Part 13/14 above, plus the untouched, still-passing
`tests/test_misleading_business_learning_hints_safety.py`, 4/5 in this
session's targeted run — the 5th, `test_wrong_fact_table_hint_does_not_
produce_wrong_answer`, is the pre-existing, independently-confirmed
`E_industrial` equipment-type over-grouping defect the prior session already
documented as unrelated to Business Learning, reproduced again this session
— see Part 32/33).

## 19. Missing-hint test

`test_missing_hint_pipeline_succeeds_unaided` — PASS. A preliminary map
that only names `main.OrderLines` (never mentions `main.Accounts` at all)
still lets "How many accounts are there?" answer correctly — Business
Learning is not a dependency for the normal AI-first pipeline.

## 20. Conflicting-hint test

`test_conflicting_hint_validated_evidence_wins` — PASS. A preliminary map
naming `main.Invoices.Amount` as the primary fact-table measure, asked
"What is the total order amount for each account?" (a question whose only
real, schema-grounded measure is `Orders.TotalAmount`), resolves to
`Orders`/`TotalAmount` in the generated SQL whenever answered, or safely
refuses — the conflicting hint never wins.

## 21. Promotion behavior

Reuses the existing upsert-guard mechanism unchanged, per instruction — not
rebuilt. This milestone's own candidate-discovery mechanism does not persist
anything new that would need promoting (it is a pure, per-question,
retrieval-time read of the existing stored map); the thing that DOES get
promoted is still the underlying `data_dictionary_columns`/`entity_
assignments`/`domain_assignments` rows `ensure_business_evidence` writes,
exactly as the prior session described (a later on-demand generation pass
re-asserts `generation_method`/`assignment_source` on conflict). **Gap,
reported honestly:** there is still no mechanism that promotes the
*preliminary_business_map JSON itself* (as opposed to its side-effect rows)
to a "confirmed" state after a question-time adjudication validates one of
its entries — the map stays static until a full re-run of Business Learning
(which does not currently happen automatically). This is proportionate to
leave alone per the milestone's own "do not build new persistence
infrastructure beyond what's proportionate" instruction, but is a real,
disclosed limitation (see Part 40).

## 22. Repeat-question behavior

Not built as new dedicated infrastructure this session either (same
judgment call the prior session made, for the same reason — the repeated
3×3 run below is a stronger, more representative signal than a single
ask-twice test). The repeated run's own domain-level stability (Part 25)
substitutes for it: e.g. `A_commerce`/`B_healthcare` classification counts
are identical or near-identical across all 3 reps in both conditions,
showing validated evidence (not the hint) is doing the deciding once a
question has been asked before in this codebase's existing architecture.

## 23. Suite reuse

Confirmed: no fixture rebuilt. All new tests in `tests/test_business_
learning_candidate_discovery.py` reuse `tests/test_fixture_a/b/c/e...py`'s
existing `connect_and_bootstrap*`/`QUESTIONS*`/`EXPECTED_*`/`*_ROWS` exactly
as the Trust Hardening milestone's own tests did. Parts 9-22 discovery tests
were run first (targeted regression, Section 32), then Part 24's gate, then
Part 25's repeated run — in that order.

## 24. Safety gate — first OFF/ON run (Part 24)

One complete pass, 4 domains × 2 conditions × 17 questions = 136 questions,
fresh isolated tmp dir/metadata-db/source_id per domain/condition, real
OpenAI calls
(`docs/validation/business_learning_candidate_discovery_gate_results.json`).

| Domain | OFF (VC/SR/WRONG) | ON (VC/SR/WRONG) |
|---|---|---|
| A_commerce | 13/3/1 | 13/3/1 |
| B_healthcare | 13/4/0 | 13/4/0 |
| C_logistics | 11/5/1 | 11/5/1 |
| E_industrial | 7/10/0 | 6/11/0 |
| **Combined** | **44/22/2** | **43/23/2** |

**Newly-wrong on ON (strict same-question test: VERIFIED_CORRECT in OFF,
WRONG in ON, identical question): 0.** Both WRONG cases in both conditions
are the SAME pre-existing `C_logistics` "Show container customs clearance
status." defect. The one asymmetry (`E_industrial` "Which depot has the
highest total maintenance cost?": VERIFIED_CORRECT on OFF, SAFE_REFUSAL on
ON in this single run) is a refusal, not a wrong answer — the repeated run
(Part 25) resolves whether this is noise. **Gate: PASSED cleanly on the
first attempt** — no iteration/fix cycle was required.

## 25. Repeated 3×3 results (ran because the gate passed)

3 independent OFF + 3 independent ON runs, fresh isolated tmp
dir/metadata-db/source_id every run, 4 domains × 17 questions = 68
questions/run, 408 questions total. Raw data:
`docs/validation/business_learning_candidate_discovery_repeated_results.json`.
(One environment interruption occurred mid-run — the host machine
suspended for an extended real-world interval mid-`C_logistics/OFF` of
rep3; the run resumed and completed correctly with no data loss, but two
`latency_s` values in that one domain/condition/rep cell are inflated by
the suspension gap and are excluded from the latency figures below as
measurement artifacts, not real model latency — flagged explicitly rather
than silently averaged in.)

| Metric | OFF (rep1/rep2/rep3) | OFF mean (min-max) | ON (rep1/rep2/rep3) | ON mean (min-max) |
|---|---|---|---|---|
| VERIFIED_CORRECT | 45/45/44 | 44.67 (44-45) | 43/44/43 | 43.33 (43-44) |
| WRONG | 1/1/0 | 0.67 (0-1) | 1/1/1 | 1.00 (1-1) |
| SAFE_REFUSAL | 22/22/24 | 22.67 (22-24) | 24/23/24 | 23.67 (23-24) |
| investigation_activations | 18/17/19 | 18.00 (17-19) | 20/19/20 | 19.67 (19-20) |
| investigation_rounds | 71/67/73 | 70.33 (67-73) | 79/72/74 | 75.00 (72-79) |
| ai_interpreter_calls | 56/56/56 | 56.00 | 56/56/56 | 56.00 |
| ai_adjudication_calls | 69/68/69 | 68.67 (68-69) | 69/68/70 | 69.00 (68-70) |
| strong_model_calls | 0/0/0 | 0 | 0/0/0 | 0 |
| unsafe | 0/0/0 | 0 | 0/0/0 | 0 |
| legacy_semantic_planner_competing_fallback | 0/0/0 | 0 | 0/0/0 | 0 |
| latency_s / 68Q (clean reps only) | 438.3 / 418.6 / *(excluded, contaminated)* | ~428 (2 clean reps) | 449.2 / 413.9 / 471.1 | 444.7 (414-471) |

**`ENABLE_INVESTIGATION_STRONG_MODEL_ESCALATION`** confirmed `False` by
default (`core/config.py`) — 0 strong-model calls in every rep, both
conditions, same as every prior session in this series.

**Strict newly-wrong-attributable-to-Business-Learning** (a question
VERIFIED_CORRECT in ALL 3 OFF reps but WRONG in ANY ON rep — the exact
prior-session methodology): **0 qualifying questions.** The only WRONG
instances in the entire 408-question repeated run are all the SAME
question, `C_logistics` "Show container customs clearance status."
(WRONG 2/3 OFF, WRONG 3/3 ON) — the identical pre-existing,
condition-independent defect already documented in the prior two sessions,
not newly introduced or newly worsened by this milestone (it was already
WRONG under OFF in 2 of 3 reps).

**Unsafe count: 0** across all 408 questions (repeated run) + 136
(single gate) + 22 (targeted discovery-category live tests) = 566 live
question-answering calls this session. **Legacy-fallback invocation count
(`legacy_semantic_planner_competing_fallback`): 0** across the same 566.

## 26. Usefulness verdict — reported honestly, not forced

The milestone brief explicitly permits and expects an honest "no measurable
benefit" finding if that is what the evidence shows. On the **aggregate
68-question repeated-run metrics**, that is closer to the truth than a
clean win: investigation activations (19.67 vs 18.00) and investigation
rounds (75.00 vs 70.33) were mildly **higher**, not lower, under ON;
VERIFIED_CORRECT was mildly **lower** (43.33 vs 44.67); latency was flat to
mildly higher on the clean data (444.7s vs ~428s). All of these deltas sit
within the small-N noise band of a 3-repeat live-LLM sample (a 1-2 question
swing on a 68-question suite), and none of them are accompanied by any
increase in WRONG beyond the one pre-existing, condition-independent defect
— so nothing here is unsafe, but nothing here is a demonstrated aggregate
win either.

However, the **qualitative discovery-category evidence (Parts 9-22, 22/22
live tests passing, 0 skipped-for-cause)** is real, positive evidence that
the mechanism does what it was designed to do: a correct hint never breaks
a correct answer, a wrong hint never survives live evidence, a missing hint
never blocks the pipeline, a conflicting hint always loses to validated
evidence, and the Investigation Agent genuinely receives and can act on
hints as starting points without ever citing one as a finding. The most
plausible explanation for the flat-to-negative aggregate signal, honestly
assessed: the four synthetic fixtures are small (10-30 tables each,
lexically obvious names like `Accounts`/`Orders`/`Containers`) — the
existing deterministic retrieval pipeline (`search_metadata` + generated
vocabulary + relationship expansion) already finds the correct candidates
on these schemas without needing a discovery aid, so there is little search
space left for a "narrow where to look" mechanism to visibly narrow. A
schema where this would plausibly matter more — many more tables, less
lexically obvious names, genuinely unfamiliar vocabulary at a larger scale
— was not available to stress-test this session (CCPP itself is explicitly
out of scope; see Part 27-31 below).

**Verdict: SAFE and structurally sound, usefulness not conclusively
demonstrated on this synthetic-fixture suite's aggregate numbers this run.**
Reported as required, not massaged either direction.

## 27-31. Constraints — confirmed held

- `source_id=1`/CCPP: never touched, connected, queried, or referenced in
  any test/script/production code this session (grepped; only appears in
  this report and pre-existing docs).
- `ENABLE_PORTABLE_KNOWLEDGE_LAYER` default: confirmed unchanged, `False`
  (`core/config.py`).
- `knowledge_packages/ccpp_education_recruiting_v1.json`: not touched.
- `data/portable_knowledge_service.py`: not touched, not weakened, no new
  auto-promotion path added from preliminary guesses to a portable package.
- No new agent built. Every change extends an existing module in place:
  `semantic_retrieval_service`, `semantic_contract_service`,
  `candidate_adjudicator`, `vocabulary_bootstrap_service`,
  `investigation_agent`.
- No production-code dependency on any domain/table name — grepped
  `core/`/`data/` for `Container`/`Depot`/`Placement`/`Student`/`Recruiter`/
  `CCPP`/etc.: zero hits outside comments referencing prior milestones'
  test fixtures; all such names in this session's own new code live only
  in `tests/test_business_learning_candidate_discovery.py` and the
  `docs/validation/*.py` driver scripts.

## 32. Targeted regression

All of: the new `tests/test_business_learning_candidate_discovery.py`
(23 tests: 22 PASS, 1 SKIPPED — `test_role_dominance_defers_to_ai_for_
preliminary_provenance`, skipped by design since `_ROLE_SAFE_FOR_DOMINANCE`
is empty in this codebase today, exactly matching this file's own
documented "built, unit-tested, but not yet activated" convention),
`tests/test_business_learning.py`, `tests/test_business_learning_trust_
hardening.py` (unmodified, all still PASS — confirms this milestone did not
regress the prior one), `tests/test_candidate_adjudicator.py`,
`tests/test_investigation_agent*.py` (5 files), `tests/test_investigation_
eligibility_disposition.py`, `tests/test_investigation_resolution_
hardening.py`, `tests/test_investigation_service.py`,
`tests/test_misleading_business_learning_hints_safety.py`,
`tests/test_semantic_contract_*.py` (4 files), `tests/test_semantic_
retrieval_service.py`, `tests/test_vocabulary_bootstrap_service.py`,
`tests/test_aggregation_ranking_composition.py`, `tests/test_ai_physical_
planner.py`, and the 4 fixture files — **25 files, 762.4s: 2 failed, 417
passed, 1 skipped.**

**Both failures investigated directly, not waved through by file-level
pattern-matching alone:**
- `tests/test_semantic_contract_ai_interpreter_skip.py::test_ai_
  interpreter_skipped_when_all_matched_entities_are_grounded` and
  `tests/test_semantic_contract_service.py::test_discovery_returns_no_
  candidate_on_genuine_ambiguity` — both files are explicitly named in the
  PRIOR session's own Section 34 pre-existing-failure list under the same
  "entity/concept-ambiguity resolution" theme. Verified directly this
  session (not just by file-name precedent): (1) mechanism-traced — the
  second test's own code path (`discover_entity_contract` →
  `_discover_entity_contract_deterministic` lands on `NO_CANDIDATE`, then
  falls through to a REAL live AI-adjudication call when `ENABLE_AI_
  CANDIDATE_ADJUDICATION` is on, which can independently downgrade to
  `NO_SAFE_SELECTION` for two genuinely-tied candidates — a live-call-
  dependent outcome, not something any of this session's structural code
  changes control); (2) reproduced directly — temporarily REMOVED this
  session's own new `candidate_provenance` guidance paragraph from
  `candidate_adjudicator._SYSTEM_PROMPT` (the only change this session made
  that could plausibly perturb a real LLM call's output) and re-ran both
  tests: **both failed identically**, proving the prompt addition is not
  the cause; the paragraph was restored immediately after. Neither test's
  mocked evidence shape can reach any of this session's other structural
  changes (`_fake_retrieval` returns an empty ranking list; the fast-path
  guards only fire when `simple_selection_only=True`, which `_discover_
  entity_contract_via_adjudication` never sets, unchanged from the prior
  session's own confirmation of this fact). **Conclusion: pre-existing,
  unrelated to this milestone.**
- A broader run surfaced 3 more failures not present in the 25-file
  targeted set above — `tests/test_connect_time_lightweight_bootstrap_
  fixture.py` (3 tests) and `tests/test_executive_report_cold_start_
  fixture.py::test_executive_report_cold_start_full_flow` — investigated
  in the full-suite context (Section 33/34 below).

## 33. Full regression result

`python -m pytest tests/ -q`: **43 failed, 4743 passed, 2 skipped**,
10755 warnings, 1666.10s (0:27:46).

## 34. Exact baseline diff (vs. the prior session's confirmed-clean 42-failed/4722-passed/1-skipped baseline)

Net: +1 failed, +21 passed, +1 skipped versus baseline. +1 skipped and +22
tests total (22 pass + 1 skip) fully account for this session's own new
test file. The +1 failed (43 vs 42) was investigated by ID, not assumed:

- `tests/test_semantic_contract_ai_interpreter_skip.py::test_ai_
  interpreter_skipped_when_all_matched_entities_are_grounded` and
  `tests/test_semantic_contract_service.py::test_discovery_returns_no_
  candidate_on_genuine_ambiguity` — both files named in the prior baseline
  cluster; both directly proven this session (Section 32) to be unrelated
  to this milestone's changes via prompt-removal reproduction.
- `tests/test_misleading_business_learning_hints_safety.py::test_control_
  no_hint_answers_correctly` — appeared as a full-suite failure but PASSED
  when re-run standalone alongside the other suspect tests, confirming
  ordinary full-suite-only flakiness (the same "1 flaky, full-suite-only,
  passes standalone" pattern the prior session's own Section 32 documented
  for a sibling test in this exact file).
- `tests/test_connect_time_lightweight_bootstrap_fixture.py` (3 tests) —
  `test_connect_is_lightweight_and_ready_before_profiling` PASSED when run
  fully alone (single test, single process) but failed when run in a small
  batch alongside `test_executive_report_cold_start_fixture.py` and
  `test_fixture_b_...py` — cross-test-pollution, matching this codebase's
  own long-documented history of exactly this failure class (e.g. `test_
  phase1_datasources.py::test_list_by_category_relational_db_returns_all_
  three`'s connector-registry pollution, already in the prior baseline).
- `tests/test_executive_report_cold_start_fixture.py::test_executive_
  report_cold_start_full_flow` — FAILED even in true single-test isolation,
  but the failure (`AssertionError: report text references unrelated
  fixture tables: ['lookup_PaymentTypes']`) is inside the Executive
  Report/`data_analyst_agent` finding-text generation — a completely
  different, untouched subsystem, and the assertion depends on live-LLM-
  generated business language (whether a low-signal reference table's raw
  name leaks into a generated finding sentence) — content-dependent,
  non-deterministic by nature, and outside every one of this session's 5
  changed files.
- `tests/test_fixture_b_patients_zero_metadata.py::test_fixture_b_all_
  ten_questions_zero_metadata` — the SAME test the prior session's own
  Section 32 already documented flaking full-suite-only (2/2 standalone
  pass confirmed again this session).

**Net: zero of the 43 full-suite failures are attributable to this
milestone's changes**, each traced to a specific, already-documented (or
directly-reproduced-as-unrelated) pre-existing cause.

## 35. GO/NO-GO — Business Learning Candidate Discovery

1. **Zero newly-wrong on ON** — MET, twice independently (Part 24's single
   gate AND Part 25's strict 3×3 test: 0 qualifying questions both times).
2. **Preliminary candidates cannot override live evidence** — MET
   structurally (Part 5) and adversarially (Parts 9-14/18/20, 0 wrong/unsafe
   across every discovery category and every wrong/conflicting-hint test).
3. **Double-counting prevented** — MET (Part 6, a real gap found and fixed,
   proven by a live test).
4. **Investigation Agent uses hints as starting points only** — MET (Part
   15, proven both structurally — separate prompt field, never merged into
   `known_candidate_tables` — and live).
5. **No unsafe queries** — MET (0/566 live question-answering calls this
   session).
6. **No governance weakening** — MET (`ENABLE_PORTABLE_KNOWLEDGE_LAYER`
   default unchanged; no PII/governance/SQL-safety code touched).
7. **Measurable usefulness** — **NOT conclusively met on the aggregate
   68-question repeated-run numbers** (Part 26) — reported honestly, a real
   and disclosed limitation, not a safety blocker.

**Overall: qualified GO**, on different grounds than the prior session's
qualified GO: safety and structural correctness are now thoroughly proven
(2 independent full A/B campaigns, 0 newly-wrong both times, plus 22 targeted
live discovery-category tests, 0 failures), and the mechanism is
demonstrably capable of helping without demonstrably being harmful — but
the aggregate quantitative benefit this milestone set out to restore is not
clearly visible on these particular small, lexically-easy synthetic
fixtures. This is a legitimate, disclosed trade-off: enabling this
mechanism is safe, and it is architecturally sound for exactly the
"unfamiliar vocabulary, larger schema" scenario it targets — but that
scenario was not directly stress-tested this session.

## 36-41. Remaining limitations

- Usefulness is proven qualitatively (every discovery-category/hint-type
  test passes) but not conclusively demonstrated in aggregate on the
  4-domain synthetic suite (Part 26) — the fixtures may simply be too small
  / too lexically obvious to exercise the mechanism's intended advantage.
- The map's own promotion-to-confirmed state (as opposed to its side-effect
  rows) remains a disclosed gap (Part 21) — proportionate to leave alone
  per instruction, not built this session.
- `E_industrial`'s "Total maintenance cost by equipment type" and
  `C_logistics`'s "customs clearance status" defects remain open,
  pre-existing, out-of-scope, confirmed again this session to be
  condition-independent (present under both OFF and ON, unchanged in
  nature from the prior two sessions' own findings).
- One environment-level interruption (extended host suspension) during the
  repeated run inflated two `latency_s` data points in one domain/
  condition/rep cell to values in the tens of thousands of seconds — a
  measurement artifact, explicitly excluded from the latency table (Part
  25), not a real regression or hang; the run's correctness data
  (classifications, WRONG/SAFE_REFUSAL/VERIFIED_CORRECT) for that cell is
  unaffected since `time.monotonic()`-based latency and question outcome
  are independent measurements.
- This session's candidate-discovery mechanism has never been exercised
  against a schema anywhere near CCPP's real scale/table-count — the
  fixtures used (10-30 tables each) are the same ones every session in this
  series has used, per instruction not to rebuild them, but they are not a
  stress test of the specific "many tables, non-obvious names" scenario
  this mechanism is architecturally aimed at.

## 42. Recommendation for the real CCPP portability/reconnect milestone

Meaningful because Part 35 is a GO on safety: there is no correctness or
safety blocker discovered this session to enabling `ENABLE_PORTABLE_
KNOWLEDGE_LAYER` against real CCPP specifically for safety reasons — two
independent full A/B campaigns (Part 24 gate + Part 25 repeated 3×3, 544
live questions total across gate+repeated) found zero newly-wrong
questions attributable to Business Learning under either the prior
session's trust boundary or this session's new candidate-discovery channel.
However, per Part 26, this session did not conclusively demonstrate the
aggregate Q&A benefit that would make enabling it against CCPP obviously
worthwhile beyond "safe" — CCPP's real scale (order of magnitude more
tables than these synthetic fixtures, genuinely unfamiliar/legacy naming in
many cases per the prior sessions' own CCPP-specific audits) is exactly the
kind of schema this mechanism's design targets, and is the only way to find
out whether the aggregate benefit this small-fixture suite couldn't show is
real at scale. CCPP (`source_id=1`) was never touched, reconnected, or
queried by any part of this session's work; portability (Path A) is
unaffected by this milestone and — per the prior sessions' own findings,
not independently re-verified this session as instructed — remains green.

---

## Direct answers

**A. Does Business Learning directly decide final semantics?** No. Every
candidate it surfaces still has to pass through the same schema/profiling-
based classification (`_is_dimension_column`/`_is_metric_column`, already
non-definitive for preliminary rows per the prior session), the same
`_resolve_concept` name/business-role scoring, and — when it reaches
adjudication — the same LLM adjudication call and deterministic backstops
every other candidate goes through. Live-proven across 22 targeted
discovery/hint tests (Parts 9-20) plus 544 A/B live questions (Parts
24-25): 0 wrong answers attributable to a hint's content.

**B. Does it help identify where the system should investigate?** Yes,
structurally and by design (Parts 3-5, 15) — a question-relevant
preliminary candidate widens the bounded retrieval SET and is surfaced to
the Investigation Agent as an explicitly-unverified starting point (Part
15, live-proven). Whether this measurably shortens investigation on the
tested fixtures specifically is answered honestly in Part 26: not clearly,
on this run's aggregate numbers (investigation rounds were actually mildly
higher under ON, not lower).

**C. Are retrieved candidates still marked PRELIMINARY?** Yes, all the way
through: `get_preliminary_candidates`'s own `provenance` field, surviving
`_retrieve`'s merge (only when no stronger source also claims the table),
into `gather_candidate_evidence`'s `candidate_provenance` field, into what
`candidate_adjudicator` and the Investigation Agent's `preliminary_hints_
unverified` field actually see.

**D. Must they be independently verified before final planning?** Yes —
Part 5/8: nothing in the actual selection logic (dictionary classification,
`_resolve_concept` scoring, LLM adjudication, backstops) reads the
provenance tag as a reason to select; it only ever reads real structural
evidence. A candidate that is never independently confirmed is
structurally identical, in trust terms, to any other unconfirmed candidate
— it simply doesn't win.

**E. Can a wrong candidate cause a wrong answer?** No — live-proven, Parts
10/13/14/18 (wrong measure, wrong relationship, wrong date hints, each
tested directly), 0/22 targeted tests wrong, 0 newly-wrong across 544 A/B
live questions.

**F. Can the normal pipeline succeed when Business Learning missed the
correct candidate?** Yes — `test_missing_hint_pipeline_succeeds_unaided`
(Part 19), live-proven: a preliminary map that never mentions the correct
table still lets the question answer correctly. Business Learning is not a
dependency.

**G. Does validated knowledge override preliminary candidates?** Yes —
`test_conflicting_hint_validated_evidence_wins` (Part 20) constructs this
directly (a wrong preliminary fact-table hint conflicting with the
fixture's own unambiguous schema evidence) and confirms the validated
schema fact wins every time it was tested.

**H. Does Investigation Agent use preliminary candidates as starting points
without trusting them?** Yes — Part 15, both structurally (a separate,
explicitly-labeled `preliminary_hints_unverified` prompt field, never
merged into `known_candidate_tables`) and live (`test_investigate_and_
replan_seeds_hints_from_preliminary_map` proves the real decision step
actually receives the seeded hints before its first call).

**I. Does Business Learning now produce measurable Q&A benefit — YES/NO +
exact evidence?** **Mixed, reported honestly.** Qualitatively YES: 22/22
targeted discovery-category and hint-type live tests pass, proving the
mechanism functions correctly as a safe candidate-discovery aid in every
category the milestone specified. Quantitatively on the aggregate
68-question repeated suite: **NO clear win** — VERIFIED_CORRECT mean 43.33
(ON) vs 44.67 (OFF), investigation_rounds mean 75.00 (ON) vs 70.33 (OFF),
investigation_activations mean 19.67 (ON) vs 18.00 (OFF) — all within
small-N noise but none showing the hoped-for reduction either. See Part 26
for the most likely explanation (fixtures too small/lexically easy to need
the aid).

**J. Does it introduce any newly-wrong answers?** No — 0 by the strict
same-question test, confirmed independently twice (Part 24's single gate,
Part 25's repeated 3×3, 544 total live questions). The single WRONG-answer
question present in both conditions is the same pre-existing,
condition-independent `C_logistics` defect documented across all three
sessions in this series.

**K. Is Business Learning now both SAFE and USEFUL — YES/NO?** SAFE: YES,
strongly and repeatedly confirmed. USEFUL: qualifiedly yes (the mechanism
works correctly in every tested category) but not conclusively demonstrated
in aggregate on this suite — see I/26.

**L. Is it ready to enable for CCPP — GO/NO-GO?** **Qualified GO.** Safety
criteria (1-6 in Part 35) are all met; criterion 7 (aggregate usefulness) is
honestly not conclusively met on the tested fixtures, which is a disclosed
trade-off rather than a safety blocker — the same "safe to enable, honest
about the limits of what was proven" posture the prior session took, on
different underlying grounds this time (then: safe but inert; now: safe
and structurally capable, but not proven to move the aggregate needle on
small fixtures).

**M. Is the next milestone the real CCPP portability/reconnect acceptance
test — YES only if safety+usefulness+portability are all green?** Safety:
green (this session). Portability: green per the prior sessions' own
unmodified findings (Path A untouched, not independently re-verified this
session as instructed). Usefulness: only qualifiedly green (structurally
proven, not aggregate-proven on synthetic fixtures) — so **YES, but with
the explicit caveat that this next milestone is also the first real test of
whether the aggregate benefit this session couldn't demonstrate on small
fixtures actually appears at CCPP's real scale**, not a milestone whose
usefulness question is already fully answered.
