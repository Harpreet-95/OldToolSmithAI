# Phase 7, Step 6H1, Part 7 — Cross-Computer Package Content Audit

Read directly from `data/portable_knowledge_service.py`'s current implementation
(`build_package_from_source`, `export_signed_package`, `sanitize_package`,
`_TOP_LEVEL_KEYS`). No claim below is inferred — each is traced to the exact
function that would have to emit that record type if it were included.

## Exported today

| Record type | Included? | Source |
|---|---|---|
| Vocabulary | **Yes** | `_vocab_svc.get_generated_vocabulary` rows ≥ `min_confidence` |
| Entities (entity_assignments) | **Yes** | `_entity_svc.list_entity_assignments` rows ≥ `min_confidence` |
| Entity/semantic contracts | **Yes** | `entity_contracts` section, RESOLVED contracts only |
| Relationships | **Partial** | `trusted_relationships` — only AI-**inferred** relationships (`inference_method != 'declared_fk'`); declared FKs are deliberately excluded (rediscovered fresh every connect, never need to travel) |
| Schema fingerprint | **Yes** | `schema_fingerprint` section — structural identity only, no row data |

## NOT exported today (explicitly absent, not partially covered)

| Record type | Included? | Why |
|---|---|---|
| Business knowledge (concepts/relationships/facts) | **No** | `business_knowledge_packages` and its children are never read by this module |
| Authoritative mappings | **No** | `business_knowledge_mappings` is never read by this module |
| Executive Metrics Contracts | **No** | This concept does not exist anywhere in the codebase (confirmed by repository-wide search in Step 6H0) |
| View profiles | **No** | `sql_view_profiles` is never read by this module |
| Retrieval documents | **No** | `sql_retrieval_documents` is never read by this module |
| Embeddings | **No** | `sql_retrieval_embeddings`/`sql_example_embeddings` are never read by this module |
| Validated plans | **No** | `fast_path_durable_plans` (Step 6H's own durable store) is a completely separate system, never wired into `portable_knowledge_service.py` |
| Governance records | **No** | `governance_approval_events`/`governance_policies`/`governance_assignments`/etc. are never read by this module |
| Credentials | **No** (enforced, not just absent) | Allow-list (`_TOP_LEVEL_KEYS`/`_*_ROW_KEYS`) plus an independent `_FORBIDDEN_CONTRACT_KEYS` scan plus `audit_package_for_unsafe_content`'s second, independent key-marker scan |
| Result rows | **No** | Nothing in the export path ever queries a business-data row |
| PII | **No** | Same as result rows — the export path only reads schema-shape/vocabulary/entity metadata tables, never business row data |

**Conclusion: cross-computer transfer today moves structural/vocabulary
intelligence only — never business knowledge, metrics contracts, view
profiles, retrieval/embedding artifacts, validated plans, or governance
state.** A receiving installation gets a head start on term/entity/relationship
recognition; it does **not** inherit business-knowledge mappings, cached
plans, or anything else listed as absent above. This was true before Step
6H1 and remains true after it — Step 6H1 only added signing/verification
around the existing export, it did not expand what gets exported.

## Validated-plan compatibility identities (conditional requirement)

The spec asks: *"Verify that validated plans, if exported, retain all
compatibility identities and must be revalidated before live execution
after import."* Since validated plans are **not exported** (table above),
this is moot by non-applicability, not satisfied by omission — there is
nothing to verify. If a future step extends the package format to include
`fast_path_durable_plans` rows, the existing Step 6H1 Part 5 runtime
contract already guarantees the second half unconditionally regardless of
origin: `core.sqlservice.ai_first_fast_path_service` re-runs relationship
completion, exact-context construction, the ambiguity guard, SQL
validation, and live execution on **every** plan use, cache hit or not,
imported or not — there is no code path that executes a plan's SQL without
that full re-validation. The first half (retaining compatibility
identities: `schema_fingerprint`, `business_knowledge_package_id/version`,
`view_profile_state`, `secret_policy_version`, model/prompt/format version)
would need to be added to the package schema explicitly; it is not
automatic today because no plan data crosses the export boundary at all.
