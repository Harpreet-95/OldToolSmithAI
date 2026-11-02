# Phase 7, Step 6H — Durable Plan Cache + Safe Reconnect: Design

Builds directly on the Step 6H0 audit (`docs/validation` has no committed copy of
that audit; see session memory `phase7_step6h0_cache_persistence_portability_audit`).
Audit verdict: nothing existing is reusable unchanged; build a new minimal
persistent store, copying `fast_path_plan_cache.py`'s key/value shape and the
embeddings tables' content-addressed (non-`source_id`) keying idiom.

## Part 1 — Architecture for the four durability requirements

| Requirement | Mechanism |
|---|---|
| Persistent across process/computer restart | New SQLite table `fast_path_durable_plans` (file-backed, same `data/toolsmith.db`), additive idempotent migration in `data/models.py`. |
| Same-physical-database reconnection | Lookup key uses a new **stable `datasource_identity_key`** (Part 2) instead of the numeric `source_id`, so a reconnect that reuses the same row (Part 3) — or even a new row with the same identity — still hits. |
| Explicit permanent deletion | Unchanged `delete_data_source` (hard `DELETE`, cascades); durable plans are NOT FK'd to `data_source_connections.id` at all (mirrors `sql_retrieval_embeddings`), so they are never silently destroyed by a source-row delete — they age out only via TTL/LRU or an explicit admin purge. A deleted source's plans become unreachable in practice (no identity key will ever look them up again after permanent delete, since the row and its identity are gone), but they are not actively scrubbed in this step — flagged in Part 9 as an intentional, documented limitation, not an oversight. |
| Future cross-computer transfer | Local SQLite does **not** itself replicate between machines. Cross-computer reuse is a *separate*, explicit, signed export/import flow (Part 8) built on `data/portable_knowledge_service.py`'s existing pattern — never implied by local persistence. |

### User actions, precisely distinguished

| Action | Effect on credentials | Effect on metadata | Effect on `source_id` | New function |
|---|---|---|---|---|
| Temporarily disconnect | retained, untouched | fully preserved | unchanged (same row) | `disconnect_data_source` |
| Remove saved credentials | credential fields stripped from `encrypted_config_json`; locator fields (host/port/database) kept | fully preserved | unchanged (same row) | `remove_datasource_credentials` |
| Reconnect the same database | new credentials attached | fully preserved; incompatible artifacts (schema-fingerprint-keyed) naturally miss, nothing is deleted | **unchanged** — reattaches to the existing row when its stable identity matches an existing DISCONNECTED/CREDENTIALS_REMOVED row for this owner | `reconnect_data_source` |
| Refresh / rebuild metadata | untouched | re-runs discovery/profiling; human-authored rows (`AUTHORITATIVE` business-knowledge, approved entity/semantic-contract rows) are never overwritten — this already holds today via the existing `'human'`/`AUTHORITATIVE` write locks in `entity_service`/`business_knowledge_package_service` | unchanged | existing `run_metadata_job` / `run_connect_bootstrap_job`, untouched |
| Permanently delete | destroyed with the row | cascade-deleted (`ON DELETE CASCADE`), exactly as today | row gone; a future reconnect of the same physical DB allocates a brand-new row/identity-key pairing | existing `delete_data_source`, now also writes an audit record |

### Source-scoped tables affected by deletion (inventory, from `data/models.py` FK scan)

Every one of the ~27 tables `REFERENCES data_source_connections(id) ON DELETE CASCADE`
is destroyed by a permanent delete today, unchanged by this step. Recomputable
(derived, safe to lose) vs. human-authored/expensive (must never be silently
destroyed by ordinary disconnect/reconnect — and today is preserved by those,
since neither new action issues a `DELETE`):

- **Recomputable / derived** (schema_snapshots, generated_business_vocabulary,
  table_relationships (AUTO rows), sql_retrieval_builds/documents, sql_view_profiles,
  sql_example_* for this source) — rebuilt lazily by the existing bootstrap/refresh
  path, zero new code needed to "recover" these after a reconnect since they were
  never deleted by disconnect/reconnect in the first place.
- **Human-authored / expensive / must survive** — `business_knowledge_packages`
  and children (`_concepts`, `_relationships`, `_facts`, `_mappings`), `AUTHORITATIVE`-origin
  `entity_assignments`/`semantic_entity_contracts` rows, approved `table_relationships`
  rows. These are preserved by construction: disconnect/credential-removal/reconnect
  never call `DELETE FROM data_source_connections`, so no `ON DELETE CASCADE` ever
  fires for them. Only the pre-existing, unchanged `delete_data_source` destroys them,
  and only on explicit permanent delete.

## Part 2 — Stable datasource identity

New module `core/secrets/datasource_identity.py`:

```
identity_key = "v1:" + HMAC-SHA256(derive_key(), canonical_json({
    "tenant": user_id, "source_type": source_type,
    "locator": {host (lowercased), port (int), database (as-entered)}
})).hexdigest()
```

- **Never plaintext credentials, never reversible**: HMAC, not encryption — the
  key cannot be decoded back to a server/database name. Only locator fields
  (host/port/database) enter the input; `username`/`password`/`token`/`auth_type`
  never do, so **credential rotation never changes identity** (requirement met
  by construction — rotation only ever touches `encrypted_config_json`, never
  the locator fields the identity is computed from, unless the user also moves
  to a different host/database, which is correctly treated as a different
  identity).
- **Schema fingerprint is a secondary *compatibility* check, never identity**:
  `data/schema_fingerprint_service.py` (pre-existing) is still consulted on
  reconnect to classify EXACT/COMPATIBLE_DRIFTED/NO_MATCH, but two databases
  that happen to share a schema get two different `identity_key`s because their
  `host`/`database` locator differs — satisfying "do not use schema fingerprint
  alone."
- **Not the numeric `source_id`**: stored as a new column on the existing row,
  looked up independently; durable plans and lifecycle matching key off it, not
  off the autoincrement id.
- **Key material / rotation**: reads `DATASOURCE_IDENTITY_HMAC_KEY` if an operator
  sets it; otherwise derives deterministically from the existing `ENCRYPTION_KEY`
  (zero required new configuration for existing installs). Rotating
  `DATASOURCE_IDENTITY_HMAC_KEY` (or `ENCRYPTION_KEY`, if no dedicated key is set)
  changes every identity key in that installation — on the next reconnect, no
  existing row will match by identity, so the system falls back to creating a new
  row (today's existing behavior), not a crash or data loss. This is a documented,
  acceptable cost of rotation, not silently hidden.
- **Cross-installation implication**: because the key is a local secret, the same
  physical database connected from two different installations gets two
  *different* identity keys. This is intentional — identity keys are **never**
  exported in a portable package (Part 8); cross-computer matching instead uses
  the schema fingerprint plus an explicit human confirmation, exactly as
  `portable_knowledge_service.py` already does today.
- **Collision handling**: two unrelated databases collide only if they share the
  literal (tenant, source_type, host, port, database) tuple — which means they
  *are* the same connection target by definition — or via a SHA-256 collision
  (cryptographically negligible). Two databases with merely similar *schemas*
  never collide, since schema shape is not part of the input.

## Part 4 — Durable plan store schema (`fast_path_durable_plans`)

Mirrors `fast_path_plan_cache.FastPathPlanCacheKey`/`FastPathCachedPlan` exactly,
with two deliberate departures demanded by the "never persist PII/raw prompts"
rule: the raw question text and the model's `resolved_standalone_question` are
**not** stored — only `normalized_question_hash` (SHA-256) is, used purely for
key matching. On a durable hit, the current turn's own question text (already
in hand) stands in for display; nothing textual from a *previous* session's
input is ever read back off disk.

Lookup key = SHA-256 of canonical JSON of: `datasource_identity_key`, `user_id`,
`normalized_question_hash`, `schema_fingerprint`, `retrieval_build_id`,
`business_knowledge_package_id/version`, `view_profile_state`,
`secret_policy_version`, `fast_path_model`, `reasoning_effort`, `prompt_version`,
`plan_format_version`. `relationship_state_digest` and `metrics_contract_version`
are stored as audit-only columns (not part of the lookup key) because, like the
in-memory cache, nothing about relationship completion is known until *after*
a candidate is already selected — gating lookup on it is impossible by
construction, and Part 5's hierarchy already re-runs relationship completion
unconditionally on every hit regardless.

No `source_id` FK. No result rows, no PII, no credentials, no embeddings, no raw
prompts — only structural plan metadata, matching the audited-safe shape of the
existing in-memory cache. Full column list and the "never persist" exclusions
are implemented verbatim from the user's own Part 4 spec.

## Part 9 — Migration safety

Additive only: two new nullable columns on `data_source_connections`
(`datasource_identity_key`, `connection_state` with `DEFAULT 'ACTIVE'`), two new
tables (`fast_path_durable_plans`, `datasource_lifecycle_audit`) — all via the
existing idempotent `PRAGMA table_info` / `CREATE TABLE IF NOT EXISTS` convention
already used throughout `data/models.py`. A one-time best-effort backfill computes
`datasource_identity_key` for pre-existing rows from their already-decryptable
config; a row whose config fails to decrypt is skipped (logged), never fatal to
`init_db()`. No existing column is dropped, renamed, or retyped. No row is
deleted by this migration. Before this migration is ever applied against the
real metadata database (`data/toolsmith.db`, not a disposable test fixture), a
checksummed backup will be taken first, per `dry_run_only_ccpp`/migration-rigor
convention — see the implementation report for the actual backup taken.
