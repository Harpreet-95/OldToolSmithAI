# Cross-Source Metadata Reuse — Design Note (not implemented)

**Status:** Design only. No code in this repository implements any part of
this document. Written as part of the Reconnect Reliability / Evidence-
Cascade milestone, in response to a brief that explicitly asked for a design
to be reported before any implementation, given the risk of silently
reusing wrong metadata across a schema that only looks similar.

## Why this exists

When a data source is deleted and reconnected (e.g. the CCPP source that
became `source_id 10004`), every source-scoped metadata table — schema
snapshots, profiling, dictionary, vocabulary, entity/domain assignments,
`table_relationships`, `semantic_entity_contracts` — is hard cascade-deleted
(`ON DELETE CASCADE` on every one of them, confirmed against the current
schema). The replacement source starts genuinely learning from zero, even
though its underlying database may be schema-identical to the one that was
deleted.

Persistence today is keyed only by `(source_id, table_fqn[, column_name])`.
The closest thing to a "has this changed" signature is
`semantic_contract_service.current_metadata_revision()`, which computes
`f"{schema_snapshot_id}:{profiling_snapshot_id}"` — this is a **per-source
cache-invalidation key** ("has *this* source's own schema/profiling moved
since I last computed this contract"), not a **cross-source matching
signature** ("does source A's schema look enough like source B's that A's
learned metadata is safe to reuse for B"). No cross-source fingerprinting
concept exists anywhere in the codebase today — this was confirmed by
direct inspection, not inferred from absence of a feature flag.

## Why this is genuinely risky to get wrong

Reusing another source's learned dictionary/entity/domain/relationship
metadata is only safe if the *physical shape* the metadata was learned
against still matches. A same-named table with a different column set, a
renamed/retyped column, or a relationship that no longer holds would let
stale metadata silently mis-answer a question with no visible symptom —
exactly the failure mode `[[dry_run_only_ccpp]]`-style caution exists to
prevent, just for metadata instead of live writes. This is why the original
brief asked for a design pass before any implementation, and why this
document stops short of writing code.

## What a safe fingerprint would need to prove

A cross-source metadata reuse candidate must be treated as **advisory only,
never auto-applied**, and must clear a fingerprint match at three
independent tiers before a specific piece of metadata is even offered as a
candidate:

1. **Table-shape fingerprint** — per `table_fqn`, a stable hash of
   `(sorted(column_name), sorted(declared_data_type))` from the schema
   snapshot. Two tables match at this tier only on an exact set match — no
   fuzzy/subset matching, since a missing or extra column changes what a
   COUNT/JOIN over that table means.
2. **Key/relationship-shape fingerprint** — per relationship, a hash of
   `(from_table_shape_fingerprint, from_column, to_table_shape_fingerprint,
   to_column)`. A relationship only transfers when both endpoint tables
   already cleared tier 1 independently.
3. **Source-family confirmation** — a human or an explicit prior signal
   (e.g. same `source_type` + same declared database/schema name, or an
   explicit "this is a reconnect of source X" action at connect time) must
   already indicate the two sources are plausibly related. Fingerprint
   matching alone, run promiscuously across every unrelated source in the
   system, is explicitly out of scope — this is meant to close the gap for
   *this exact reconnect* scenario, not to build a general schema-similarity
   search engine.

Only metadata whose owning table clears all three tiers is eligible to be
offered. Even then:

- **Dictionary/vocabulary/entity/domain rows**: copy as `is_approved=0`,
  `generation_method='cross_source_reuse'` (a new, explicit provenance
  value distinct from `'rule_based'`/`'ai_generated'`) into the new
  source_id — never auto-approved, so they flow through the exact same
  advisory-only evidence tier unapproved metadata already uses today (see
  the evidence-hierarchy table now documented at the top of
  `data/query_planning_service.py`).
- **Relationships**: copied only as `relationship_status='PENDING'`, never
  `'AUTO'` — a reused relationship must earn trust the same way a freshly
  discovered one does (structural auto-trust evidence or human approval),
  never inherit trust from the old source.
- **Semantic entity contracts**: not copied at all in v1 — a contract
  encodes richer judgment (grain, identity key, status semantics) than a
  fingerprint match can safely validate; regenerating via the existing
  `discover_entity_contract` path (deterministic first, AI-adjudicated
  fallback) is cheap enough and already correct.

## What would actually change in the system

- A new fingerprint-computation step, run once per schema snapshot (cheap —
  it's a hash over already-discovered column metadata, no new reads), and a
  small new table `schema_shape_fingerprints (source_id, table_fqn,
  fingerprint, computed_at)`.
- A new, explicit, user-initiated action — not an automatic background
  scan — e.g. "This looks like a reconnect of a previous source; import its
  unapproved metadata as suggestions?" surfaced only when a *fresh* source's
  own fingerprints substantially overlap with a *previous* source's stored
  fingerprints. Never triggered silently, never triggered for two sources
  that merely happen to share some fingerprints by coincidence (a minimum
  overlap threshold, e.g. >80% of tables, would need calibration against
  real reconnect cases before shipping).
- Every piece of reused metadata stays in exactly the same
  advisory/unapproved/PENDING state fresh AI-generated metadata already
  occupies today — this design deliberately adds a new metadata *source*,
  not a new *trust tier*.

## Recommendation

Do not build this now. The immediate problem this milestone was scoped to
fix (implicit ranking for "most X" questions) did not actually depend on
cross-source reuse — `10004` already self-populated its own structural
metadata at connect time, and the lazy on-demand path fills in the rest as
questions are asked. Revisit this design only if a future reconnect
scenario demonstrates the on-demand path is measurably too slow to be
usable, with real numbers from that scenario informing the overlap-
threshold calibration above.
