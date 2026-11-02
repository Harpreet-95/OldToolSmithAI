"""
Tests for Business Lineage & Impact Analysis — Phase 5.

Scenario — FK chain:
    dbo.customers ← dbo.orders ← dbo.invoices ← dbo.payments
    (customers is the foundation; payments is the terminal)
    dbo.products — isolated, no FK connections

Upstream of dbo.payments  = [invoices(1), orders(2), customers(3)]
Downstream of dbo.customers = [orders(1), invoices(2), payments(3)]

Run from the project root:
    venv/Scripts/pytest tests/test_phase5_lineage.py -v
"""
import json
import os
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-lineage-phase5-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase5-long-enough-value-12345")

from data.lineage_service import (
    get_upstream_lineage,
    get_downstream_lineage,
    impact_analysis,
    critical_asset_analysis,
    lineage_summary,
)
from data.profiling_snapshot_resolver import (
    get_latest_profiling_snapshot as _real_get_latest_profiling_snapshot,
)


# ---------------------------------------------------------------------------
# Minimal DB schema (only tables the lineage service reads from)
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Test',
        source_type TEXT NOT NULL DEFAULT 'mssql',
        source_category TEXT NOT NULL DEFAULT 'RELATIONAL',
        encrypted_config_json TEXT NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        source_status TEXT NOT NULL DEFAULT 'ACTIVE',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE schema_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        source_type TEXT NOT NULL DEFAULT 'mssql',
        table_count INTEGER NOT NULL DEFAULT 0,
        view_count INTEGER NOT NULL DEFAULT 0,
        column_count INTEGER NOT NULL DEFAULT 0,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        discovered_at TEXT NOT NULL DEFAULT '2026-01-01',
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        schema_snapshot_id INTEGER NOT NULL DEFAULT 1,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        mode TEXT NOT NULL DEFAULT 'full',
        sample_rate REAL NOT NULL DEFAULT 1.0,
        profiling_rules_version TEXT NOT NULL DEFAULT '1.0.0',
        status TEXT NOT NULL DEFAULT 'COMPLETE',
        tables_total INTEGER NOT NULL DEFAULT 0,
        tables_profiled INTEGER NOT NULL DEFAULT 0,
        columns_total INTEGER NOT NULL DEFAULT 0,
        columns_profiled INTEGER NOT NULL DEFAULT 0,
        pii_columns_found INTEGER NOT NULL DEFAULT 0,
        tables_skipped INTEGER NOT NULL DEFAULT 0,
        tables_failed INTEGER NOT NULL DEFAULT 0,
        tables_timed_out INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at TEXT, completed_at TEXT, duration_seconds INTEGER,
        resumable_state_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_table_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        exact_row_count INTEGER,
        estimated_row_count INTEGER,
        row_count_tier TEXT,
        has_date_column INTEGER NOT NULL DEFAULT 0,
        date_column_name TEXT, earliest_record TEXT, latest_record TEXT,
        data_span_days INTEGER, data_currency TEXT,
        column_count INTEGER NOT NULL DEFAULT 0,
        pk_column_count INTEGER NOT NULL DEFAULT 0,
        fk_count INTEGER NOT NULL DEFAULT 0,
        referenced_by_count INTEGER NOT NULL DEFAULT 0,
        is_junction_table INTEGER NOT NULL DEFAULT 0,
        is_root_table INTEGER NOT NULL DEFAULT 0,
        is_leaf_table INTEGER NOT NULL DEFAULT 0,
        has_identity_column INTEGER NOT NULL DEFAULT 0,
        avg_null_percentage REAL, completeness_score REAL,
        table_class TEXT, classification_confidence REAL,
        classification_evidence_json TEXT, competing_classes_json TEXT,
        classification_rule_version TEXT,
        pii_column_count INTEGER NOT NULL DEFAULT 0,
        confirmed_pii_count INTEGER NOT NULL DEFAULT 0,
        profiling_depth TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        skip_reason TEXT, profiled_at TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_column_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL,
        column_name TEXT NOT NULL,
        data_type TEXT NOT NULL DEFAULT 'TEXT',
        raw_type TEXT,
        is_nullable INTEGER NOT NULL DEFAULT 1,
        is_primary_key INTEGER NOT NULL DEFAULT 0,
        is_identity INTEGER NOT NULL DEFAULT 0,
        ordinal_position INTEGER NOT NULL DEFAULT 1,
        null_percentage REAL, distinct_count INTEGER, distinct_percentage REAL,
        uniqueness_score REAL, cardinality_tier TEXT,
        min_value TEXT, max_value TEXT, avg_length REAL,
        mean_value REAL, std_deviation REAL, null_count INTEGER,
        populated_count INTEGER, populated_percentage REAL,
        empty_string_count INTEGER, zero_count INTEGER,
        min_length INTEGER, max_length_observed INTEGER,
        p5_value TEXT, p95_value TEXT,
        dominant_pattern TEXT, pattern_coverage REAL,
        email_match_rate REAL, phone_match_rate REAL, guid_match_rate REAL,
        date_string_rate REAL, numeric_string_rate REAL, masked_value_rate REAL,
        semantic_type TEXT, semantic_confidence REAL,
        semantic_evidence_json TEXT, semantic_rule_version TEXT,
        pii_name_heuristic INTEGER NOT NULL DEFAULT 0,
        pii_confirmed INTEGER NOT NULL DEFAULT 0,
        pii_signals_json TEXT, top_values_coverage REAL,
        profiling_depth TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_tables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        business_name TEXT, description TEXT, domain TEXT, grain TEXT,
        is_approved INTEGER NOT NULL DEFAULT 0,
        approved_by TEXT, approved_at TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_columns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, column_name TEXT NOT NULL,
        business_label TEXT, meaning TEXT, semantic_type TEXT,
        is_metric INTEGER NOT NULL DEFAULT 0, is_dimension INTEGER NOT NULL DEFAULT 0,
        is_date INTEGER NOT NULL DEFAULT 0, is_id INTEGER NOT NULL DEFAULT 0,
        pii_risk INTEGER NOT NULL DEFAULT 0,
        is_approved INTEGER NOT NULL DEFAULT 0,
        approved_by TEXT, approved_at TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE domain_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, domain TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_domains_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE entity_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, entity TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_entities_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE table_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL,
        from_schema TEXT NOT NULL, from_table TEXT NOT NULL,
        from_table_fqn TEXT NOT NULL, from_column TEXT NOT NULL,
        to_schema TEXT NOT NULL, to_table TEXT NOT NULL,
        to_table_fqn TEXT NOT NULL, to_column TEXT NOT NULL,
        relationship_name TEXT,
        relationship_type TEXT NOT NULL DEFAULT 'FOREIGN_KEY',
        confidence REAL NOT NULL DEFAULT 1.0,
        evidence_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        relationship_status TEXT NOT NULL DEFAULT 'AUTO'
    );
    CREATE UNIQUE INDEX idx_tr_uniq ON table_relationships
        (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
"""


def _create_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _seed(path: str) -> None:
    """
    Seed source=1 with the FK chain:
        customers ← orders ← invoices ← payments   (linear chain)
        products                                     (isolated)
    """
    conn = _db_conn(path)

    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-lin', 'Lineage Test', 'mssql', 'RELATIONAL')"
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 5)"
    )
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, tables_profiled, pii_columns_found) "
        "VALUES (1, 1, 1, 5, 2)"
    )

    # profiling table profiles
    # fk_count = number of outbound FKs; referenced_by_count = number of tables referencing this one
    profiles = [
        # fqn,             name,        fkc, rbc, pii, cls_conf, cls,           approved_dict
        ("dbo.customers", "customers",  0,   1,   0,   0.90, "Master",        True),
        ("dbo.orders",    "orders",     1,   1,   0,   0.85, "Transactional", False),
        ("dbo.invoices",  "invoices",   1,   1,   2,   0.80, "Transactional", False),
        ("dbo.payments",  "payments",   1,   0,   0,   0.75, "Transactional", False),
        ("dbo.products",  "products",   0,   0,   0,   0.70, "Reference",     False),
    ]
    for fqn, tbl, fkc, rbc, pii, cc, cls, _ in profiles:
        conn.execute(
            """INSERT INTO profiling_table_profiles
               (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name,
                fk_count, referenced_by_count, table_class, classification_confidence,
                pii_column_count, confirmed_pii_count, profiling_status)
               VALUES (1, 1, ?, ?, 'dbo', ?, ?, ?, ?, ?, 0, 'COMPLETE')""",
            (fqn, tbl, fkc, rbc, cls, cc, pii),
        )

    # PII columns for invoices
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, pii_name_heuristic) "
        "VALUES (1, 1, 'dbo.invoices', 'billing_address', 'TEXT', 1)"
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, pii_name_heuristic) "
        "VALUES (1, 1, 'dbo.invoices', 'bank_account_no', 'TEXT', 1)"
    )

    # FK chain
    fks = [
        ("orders",   "orders",   "customer_id", "dbo.customers", "dbo", "customers", "id",         "FK_orders_customers"),
        ("invoices", "invoices", "order_id",    "dbo.orders",    "dbo", "orders",    "id",         "FK_invoices_orders"),
        ("payments", "payments", "invoice_id",  "dbo.invoices",  "dbo", "invoices",  "id",         "FK_payments_invoices"),
    ]
    for from_tbl, tbl_name, from_col, to_fqn, to_schema, to_tbl, to_col, fk_name in fks:
        conn.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
            "from_column, to_schema, to_table, to_table_fqn, to_column, "
            "relationship_name, relationship_type, confidence) "
            "VALUES (1, 1, 'dbo', ?, ?, ?, ?, ?, ?, ?, ?, 'FOREIGN_KEY', 1.0)",
            (tbl_name, f"dbo.{from_tbl}", from_col, to_schema, to_tbl, to_fqn, to_col, fk_name),
        )

    # Domain assignments
    for fqn, dom, conf in [
        ("dbo.customers", "Sales",   0.90),
        ("dbo.orders",    "Sales",   0.85),
        ("dbo.invoices",  "Finance", 0.80),
        ("dbo.payments",  "Finance", 0.75),
        ("dbo.products",  "Sales",   0.65),
    ]:
        conn.execute(
            "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, confidence) "
            "VALUES (1, 1, ?, ?, ?)", (fqn, dom, conf)
        )

    # Entity assignments
    for fqn, ent, conf in [
        ("dbo.customers", "Customer", 0.92),
        ("dbo.orders",    "Order",    0.88),
        ("dbo.invoices",  "Invoice",  0.82),
        ("dbo.payments",  "Payment",  0.78),
        ("dbo.products",  "Product",  0.65),
    ]:
        conn.execute(
            "INSERT INTO entity_assignments (source_id, profiling_snapshot_id, table_fqn, entity, confidence) "
            "VALUES (1, 1, ?, ?, ?)", (fqn, ent, conf)
        )

    # Dictionary (customers is approved; others are generated only)
    for fqn, tbl, bname, approved in [
        ("dbo.customers", "customers", "Customer",    1),
        ("dbo.orders",    "orders",    "Sales Order", 0),
        ("dbo.invoices",  "invoices",  "Invoice",     0),
        ("dbo.payments",  "payments",  "Payment",     0),
        ("dbo.products",  "products",  "Product",     0),
    ]:
        conn.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, business_name, is_approved) "
            "VALUES (1, 1, ?, ?, ?, ?)", (fqn, tbl, bname, approved)
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# T1 — upstream: direct parent
def test_upstream_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "lin.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    assert result is not None
    assert result["table_fqn"] == "dbo.orders"
    fqns = [n["table_fqn"] for n in result["upstream"]]
    assert "dbo.customers" in fqns
    assert result["total_upstream"] == 1
    assert result["max_depth"] == 1
    assert result["has_cycle"] is False

    # Check first hop is a FK_PARENT
    first = result["upstream"][0]
    assert first["distance"] == 1
    assert first["relationship_type"] == "FK_PARENT"
    assert first["via_column"] == "customer_id"


# T2 — upstream: transitive ancestors
def test_upstream_transitive(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_up.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.payments")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["upstream"]}
    assert fqns == {"dbo.invoices", "dbo.orders", "dbo.customers"}
    assert result["total_upstream"] == 3
    assert result["max_depth"] == 3

    by_fqn = {n["table_fqn"]: n for n in result["upstream"]}
    assert by_fqn["dbo.invoices"]["distance"]  == 1
    assert by_fqn["dbo.orders"]["distance"]    == 2
    assert by_fqn["dbo.customers"]["distance"] == 3

    # Ancestors beyond distance-1 get FK_ANCESTOR relationship type
    assert by_fqn["dbo.customers"]["relationship_type"] == "FK_ANCESTOR"


# T3 — upstream: disconnected table returns empty
def test_upstream_disconnected(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_dc.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.products")

    assert result is not None
    assert result["upstream"] == []
    assert result["total_upstream"] == 0
    assert result["has_cycle"] is False


# T4 — downstream: direct child
def test_downstream_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_ds.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.invoices")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["downstream"]}
    assert "dbo.payments" in fqns
    assert result["total_downstream"] == 1

    first = result["downstream"][0]
    assert first["distance"] == 1
    assert first["relationship_type"] == "FK_CHILD"
    assert first["table_fqn"] == "dbo.payments"


# T5 — downstream: transitive descendants from foundation
def test_downstream_transitive(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_dstr.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["downstream"]}
    assert fqns == {"dbo.orders", "dbo.invoices", "dbo.payments"}
    assert result["total_downstream"] == 3
    assert result["max_depth"] == 3

    by_fqn = {n["table_fqn"]: n for n in result["downstream"]}
    assert by_fqn["dbo.orders"]["distance"]    == 1
    assert by_fqn["dbo.invoices"]["distance"]  == 2
    assert by_fqn["dbo.payments"]["distance"]  == 3
    assert by_fqn["dbo.payments"]["relationship_type"] == "FK_DESCENDANT"


# T6 — cycle handling: BFS does not infinite-loop
def test_cycle_no_infinite_loop(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_cycle.db")
    _create_db(db)
    _seed(db)

    # Inject a cycle: dbo.customers.fk → dbo.orders  (circular with the existing one)
    c = _db_conn(db)
    try:
        c.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
            "from_column, to_schema, to_table, to_table_fqn, to_column, "
            "relationship_name, relationship_type, confidence) "
            "VALUES (1, 1, 'dbo', 'customers', 'dbo.customers', "
            "'order_ref_id', 'dbo', 'orders', 'dbo.orders', 'id', "
            "'FK_cycle_test', 'FOREIGN_KEY', 0.5)"
        )
        c.commit()
    finally:
        c.close()

    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    # Must terminate (not loop forever)
    result_up = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")
    assert result_up is not None
    assert result_up["has_cycle"] is True

    result_dn = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")
    assert result_dn is not None


# T7 — unknown source returns None
def test_unknown_source_returns_none(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_none.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    assert get_upstream_lineage(99, "user-lin", "dbo.orders")          is None
    assert get_downstream_lineage(99, "user-lin", "dbo.orders")        is None
    assert impact_analysis(99, "user-lin", "dbo.orders")               is None
    assert critical_asset_analysis(99, "user-lin")                     is None
    assert lineage_summary(99, "user-lin")                             is None


# T8 — impact analysis: changing customers affects all 3 downstream tables
def test_impact_analysis_table(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_impact.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert result["table_fqn"] == "dbo.customers"
    assert result["total_affected_tables"] == 3
    affected_fqns = {t["table_fqn"] for t in result["affected_tables"]}
    assert affected_fqns == {"dbo.orders", "dbo.invoices", "dbo.payments"}

    # invoices has 2 PII columns — must appear in affected_pii_assets
    assert result["total_pii_columns"] == 2
    pii_fqns = {p["table_fqn"] for p in result["affected_pii_assets"]}
    assert "dbo.invoices" in pii_fqns

    # Domain spread: Sales (orders) + Finance (invoices, payments)
    assert "Sales"   in result["affected_domains"]
    assert "Finance" in result["affected_domains"]

    # Impact score > 0
    assert result["impact_score"] > 0.0
    assert result["impact_label"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")


# T9 — impact analysis: column-scoped propagation paths
def test_impact_analysis_column(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_imp_col.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(
        source_id=1, user_id="user-lin",
        table_fqn="dbo.customers", column_name="id",
    )

    assert result is not None
    assert result["column_name"] == "id"
    # FK: orders.customer_id → customers.id  → propagation path present
    paths = result["column_propagation_paths"]
    assert any(p["to_column"] == "id" or p["from_column"] == "id" for p in paths)


# T10 — critical asset analysis: categories are correct
def test_critical_asset_analysis(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_crit.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None

    # Foundation tables: have inbound FKs but no outbound = customers
    # (customers is referenced by orders; customers has no FK to anyone)
    foundation_fqns = {t["table_fqn"] for t in result["foundation_tables"]}
    assert "dbo.customers" in foundation_fqns

    # Terminal tables: have outbound FKs but nothing references them = payments
    terminal_fqns = {t["table_fqn"] for t in result["terminal_tables"]}
    assert "dbo.payments" in terminal_fqns

    # Hub tables: both inbound and outbound = orders and invoices
    hub_fqns = {t["table_fqn"] for t in result["hub_tables"]}
    assert "dbo.orders"   in hub_fqns
    assert "dbo.invoices" in hub_fqns

    # Summary counts
    assert result["summary"]["total_foundation"] >= 1
    assert result["summary"]["total_terminal"]   >= 1
    assert result["summary"]["total_hub"]        >= 2

    # Business critical: customers has approved dict + domain + entity
    biz_fqns = {t["table_fqn"] for t in result["business_critical"]}
    assert "dbo.customers" in biz_fqns


# T11 — lineage_summary: correct structural breakdown
def test_lineage_summary(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_sum.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert result["source"]["source_type"] == "mssql"

    # coverage: 4 tables in graph (customers, orders, invoices, payments)
    #           1 disconnected (products)
    assert result["coverage"]["tables_in_graph"] == 4
    assert result["coverage"]["total_schema_tables"] == 5

    # Root assets: foundation table(s)
    root_fqns = {t["table_fqn"] for t in result["root_assets"]}
    assert "dbo.customers" in root_fqns

    # Leaf assets: terminal table(s)
    leaf_fqns = {t["table_fqn"] for t in result["leaf_assets"]}
    assert "dbo.payments" in leaf_fqns

    # Hub assets: both inbound and outbound
    hub_fqns = {t["table_fqn"] for t in result["hub_assets"]}
    assert "dbo.orders" in hub_fqns

    # Disconnected: products
    disc_fqns = {t["table_fqn"] for t in result["disconnected_assets"]}
    assert "dbo.products" in disc_fqns

    # Longest chain: customers → orders → invoices → payments = 3 hops
    assert result["longest_chain"]["length"] == 3
    chain_path = result["longest_chain"]["path"]
    assert chain_path[0]  == "dbo.customers"
    assert chain_path[-1] == "dbo.payments"

    # Linear chain: customers(0) -> orders(1) -> invoices(2) -> payments(3); avg = 1.5
    assert result["average_depth"] == pytest.approx(1.5)


# T12 — empty graph (no table_relationships): all lists empty, graceful
def test_empty_graph_graceful(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_empty.db")
    _create_db(db)

    c = _db_conn(db)
    c.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (2, 'user-empty', 'mssql', 'RELATIONAL')"
    )
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (2, 2, 3)"
    )
    c.commit()
    c.close()

    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    # Phase 1C: _latest_profiling_snap_id now delegates to
    # data.profiling_snapshot_resolver, which holds its own independently
    # imported get_connection — must be patched too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    up  = get_upstream_lineage(2, "user-empty", "dbo.orders")
    dn  = get_downstream_lineage(2, "user-empty", "dbo.orders")
    imp = impact_analysis(2, "user-empty", "dbo.orders")
    crit = critical_asset_analysis(2, "user-empty")
    summ = lineage_summary(2, "user-empty")

    assert up  is not None and up["upstream"]    == []
    assert dn  is not None and dn["downstream"]  == []
    assert imp is not None and imp["affected_tables"] == []
    assert imp["impact_score"] == 0.0
    assert crit is not None and crit["foundation_tables"] == []
    assert summ is not None
    assert summ["coverage"]["tables_in_graph"]   == 0
    assert summ["coverage"]["tables_disconnected"] == 0
    assert summ["longest_chain"]["length"] == 0


# ---------------------------------------------------------------------------
# Phase 1C-2D.6 — resolver-migration regression guards
#
# _latest_profiling_snap_id() was retired in Phase 2D.6 after all five
# production consumers were migrated onto
# data.profiling_snapshot_resolver.get_latest_profiling_snapshot directly
# (Phases 2D.1-2D.5). That resolver's policy is status-independent (highest
# snapshot_version wins, regardless of status) — the per-consumer
# CANCELLED/RUNNING/no-snapshot/scoping tests throughout this file prove the
# migration preserved that exact pre-existing behavior at each call site.
# See test_lineage_service_has_no_legacy_snapshot_helper and
# test_all_lineage_consumers_still_use_shared_resolver near the end of this
# file for the structural replacement coverage of the retired wrapper.
# ---------------------------------------------------------------------------

def _seed_minimal_source(db: str, source_id: int = 1, user_id: str = "user-lin") -> None:
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (?, ?, 'mssql', 'RELATIONAL')",
        (source_id, user_id),
    )
    conn.commit()
    conn.close()


def _insert_profiling_snapshot(
    db: str, source_id: int, snapshot_version: int, status: str = "COMPLETE",
) -> int:
    conn = _db_conn(db)
    cur = conn.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (?, 1, ?, ?)",
        (source_id, snapshot_version, status),
    )
    conn.commit()
    snap_id = cur.lastrowid
    conn.close()
    return snap_id


def test_lineage_summary_makes_no_writes(tmp_path, monkeypatch):
    """lineage_summary is a pure read — it must not modify any row in any
    table it touches, including via its delegated snapshot lookup."""
    db = str(tmp_path / "lin_p1c_g.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _db_conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
    )
    before = {t: _dump(t) for t in tables}

    lineage_summary(source_id=1, user_id="user-lin")
    lineage_summary(source_id=999999, user_id="user-lin")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# ---------------------------------------------------------------------------
# Phase 2D.1 — get_upstream_lineage() call-site cleanup
#
# The wrapper _latest_profiling_snap_id() already delegated to
# get_latest_profiling_snapshot() (Phase 1C) — this phase removes the
# unnecessary indirection from get_upstream_lineage()'s call site only. The
# other four lineage callers (get_downstream_lineage, impact_analysis,
# critical_asset_analysis, lineage_summary) still call the wrapper
# (untouched). No behavior or connection-count change is expected: the
# wrapper was already opening its own resolver connection via
# get_latest_profiling_snapshot(); calling that function directly is a pure
# rename at this call site.
# ---------------------------------------------------------------------------

# 1 — get_latest_profiling_snapshot() called exactly once
def test_upstream_lineage_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_a.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_upstream_lineage_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_b.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by get_upstream_lineage()
#
# Phase 2D.6 deleted _latest_profiling_snap_id()'s definition outright — it
# had zero remaining production call sites after Phases 2D.1-2D.5 moved all
# five lineage functions onto the shared resolver directly. The prior
# version of this test proved the wrapper wasn't called by monkeypatching it
# to raise; that mechanism no longer works once the attribute doesn't exist
# (monkeypatch on a missing attribute raises AttributeError by default). See
# test_lineage_service_has_no_legacy_snapshot_helper and
# test_all_lineage_consumers_still_use_shared_resolver near the end of this
# file for the structural replacement coverage.


# 4 — response shape unchanged
def test_upstream_lineage_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_d.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.payments")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "table_fqn", "upstream", "total_upstream", "max_depth", "has_cycle",
    }
    assert result["source_id"] == 1
    assert result["table_fqn"] == "dbo.payments"


# 5 — nodes and edges (distances, relationship types) unchanged
def test_upstream_lineage_nodes_and_edges_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_e.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.payments")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["upstream"]}
    assert fqns == {"dbo.invoices", "dbo.orders", "dbo.customers"}
    assert result["total_upstream"] == 3
    assert result["max_depth"] == 3
    assert result["has_cycle"] is False

    by_fqn = {n["table_fqn"]: n for n in result["upstream"]}
    assert by_fqn["dbo.invoices"]["distance"]  == 1
    assert by_fqn["dbo.orders"]["distance"]    == 2
    assert by_fqn["dbo.customers"]["distance"] == 3
    assert by_fqn["dbo.invoices"]["relationship_type"]  == "FK_PARENT"
    assert by_fqn["dbo.customers"]["relationship_type"] == "FK_ANCESTOR"

    # Node ordering: sorted by (distance, table_fqn)
    assert [n["table_fqn"] for n in result["upstream"]] == [
        "dbo.invoices", "dbo.orders", "dbo.customers",
    ]


# 6 — enrichment (domain/entity/dictionary/profiling) unchanged
def test_upstream_lineage_enrichment_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_f.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    assert result is not None
    customers = next(n for n in result["upstream"] if n["table_fqn"] == "dbo.customers")
    assert customers["domain"] == "Sales"
    assert customers["entity"] == "Customer"
    assert customers["business_name"] == "Customer"
    assert customers["dict_approved"] is True
    assert customers["table_class"] == "Master"
    assert customers["classification_confidence"] == 0.90
    assert customers["referenced_by_count"] == 1
    assert customers["fk_count"] == 0


# 7 — no-snapshot behavior unchanged: nodes/edges present, profiling fields absent
def test_upstream_lineage_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_g.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["upstream"]}
    assert fqns == {"dbo.customers"}
    customers = result["upstream"][0]
    assert "table_class" not in customers
    assert "pii_column_count" not in customers


# 8 — snapshot policy unchanged (status-independent, highest version wins)
def _seed_upstream_policy_fixture(db: str, source_id: int = 1, user_id: str = "user-lin") -> None:
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (?, ?, 'mssql', 'RELATIONAL')",
        (source_id, user_id),
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, ?, 2)",
        (source_id,),
    )
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (?, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)",
        (source_id,),
    )
    conn.commit()
    conn.close()


def _insert_profiling_table_profile(db: str, snapshot_id: int, source_id: int, table_class: str) -> None:
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (?, ?, 'dbo.customers', 'customers', 'dbo', ?)",
        (snapshot_id, source_id, table_class),
    )
    conn.commit()
    conn.close()


def test_upstream_lineage_snapshot_policy_complete_precedence(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_h.db")
    _create_db(db)
    _seed_upstream_policy_fixture(db)
    old_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_profiling_table_profile(db, old_id, source_id=1, table_class="Old")
    cancelled_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")
    _insert_profiling_table_profile(db, cancelled_id, source_id=1, table_class="New")
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    # Status-aware selection: the older COMPLETE snapshot ("Old") wins over
    # the newer CANCELLED one ("New") — a newer non-COMPLETE snapshot must
    # never shadow older, usable COMPLETE evidence.
    customers = result["upstream"][0]
    assert customers["table_class"] == "Old"


def test_upstream_lineage_snapshot_policy_running_precedence(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_i.db")
    _create_db(db)
    _seed_upstream_policy_fixture(db)
    old_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_profiling_table_profile(db, old_id, source_id=1, table_class="Old")
    running_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="RUNNING")
    _insert_profiling_table_profile(db, running_id, source_id=1, table_class="New")
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    # Status-aware selection: the older COMPLETE snapshot ("Old") wins over
    # the newer RUNNING one ("New").
    customers = result["upstream"][0]
    assert customers["table_class"] == "Old"


# 9 — read-only behavior preserved
def test_upstream_lineage_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_j.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _db_conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
    )
    before = {t: _dump(t) for t in tables}

    get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.payments")
    get_upstream_lineage(source_id=999, user_id="user-lin", table_fqn="dbo.orders")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 10 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_upstream_lineage_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d1_k.db")
    _create_db(db)
    _seed(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.lineage_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2D.2 — get_downstream_lineage() call-site cleanup
#
# The wrapper _latest_profiling_snap_id() already delegated to
# get_latest_profiling_snapshot() (Phase 1C) — this phase removes the
# unnecessary indirection from get_downstream_lineage()'s call site only.
# The remaining three lineage callers (impact_analysis,
# critical_asset_analysis, lineage_summary) still call the wrapper
# (untouched). No behavior or connection-count change is expected: the
# wrapper was already opening its own resolver connection via
# get_latest_profiling_snapshot(); calling that function directly is a pure
# rename at this call site.
# ---------------------------------------------------------------------------

# 1 — get_latest_profiling_snapshot() called exactly once
def test_downstream_lineage_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_a.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.invoices")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_downstream_lineage_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_b.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.invoices")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by get_downstream_lineage()
#
# See the equivalent note above test 3 in the Phase 2D.1 upstream section:
# this was one of the last production callers of the now-deleted wrapper.
# Structural replacement coverage lives in
# test_lineage_service_has_no_legacy_snapshot_helper and
# test_all_lineage_consumers_still_use_shared_resolver near the end of this
# file.


# 4 — response shape unchanged
def test_downstream_lineage_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_d.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "table_fqn", "downstream", "total_downstream", "max_depth", "has_cycle",
    }
    assert result["source_id"] == 1
    assert result["table_fqn"] == "dbo.customers"


# 5 — nodes and edges (distances, relationship types) unchanged
def test_downstream_lineage_nodes_and_edges_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_e.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["downstream"]}
    assert fqns == {"dbo.orders", "dbo.invoices", "dbo.payments"}
    assert result["total_downstream"] == 3
    assert result["max_depth"] == 3
    assert result["has_cycle"] is False

    by_fqn = {n["table_fqn"]: n for n in result["downstream"]}
    assert by_fqn["dbo.orders"]["distance"]    == 1
    assert by_fqn["dbo.invoices"]["distance"]  == 2
    assert by_fqn["dbo.payments"]["distance"]  == 3
    assert by_fqn["dbo.orders"]["relationship_type"]   == "FK_CHILD"
    assert by_fqn["dbo.payments"]["relationship_type"] == "FK_DESCENDANT"

    # Node ordering: sorted by (distance, table_fqn)
    assert [n["table_fqn"] for n in result["downstream"]] == [
        "dbo.orders", "dbo.invoices", "dbo.payments",
    ]


# 6 — enrichment (domain/entity/dictionary/profiling) unchanged
def test_downstream_lineage_enrichment_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_f.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    orders = next(n for n in result["downstream"] if n["table_fqn"] == "dbo.orders")
    assert orders["domain"] == "Sales"
    assert orders["entity"] == "Order"
    assert orders["business_name"] == "Sales Order"
    assert orders["dict_approved"] is False
    assert orders["table_class"] == "Transactional"
    assert orders["classification_confidence"] == 0.85
    assert orders["referenced_by_count"] == 1
    assert orders["fk_count"] == 1


# 7 — no-snapshot behavior unchanged: nodes/edges present, profiling fields absent
def test_downstream_lineage_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_g.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["downstream"]}
    assert fqns == {"dbo.orders"}
    orders = result["downstream"][0]
    assert "table_class" not in orders
    assert "pii_column_count" not in orders


# 8 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_downstream_lineage_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_h.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'CANCELLED')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.orders', 'orders', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1,
    # table_class="Transactional" from _seed) wins over the newer CANCELLED
    # snapshot (version 2, table_class="New") — a newer non-COMPLETE
    # snapshot must never shadow older, usable COMPLETE evidence.
    orders = next(n for n in result["downstream"] if n["table_fqn"] == "dbo.orders")
    assert orders["table_class"] == "Transactional"


def test_downstream_lineage_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_i.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'RUNNING')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.orders', 'orders', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    orders = next(n for n in result["downstream"] if n["table_fqn"] == "dbo.orders")
    assert orders["table_class"] == "Transactional"


# 9 — read-only behavior preserved
def test_downstream_lineage_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_j.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _db_conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
    )
    before = {t: _dump(t) for t in tables}

    get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")
    get_downstream_lineage(source_id=999, user_id="user-lin", table_fqn="dbo.orders")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 10 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_downstream_lineage_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d2_k.db")
    _create_db(db)
    _seed(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.lineage_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2D.3 — impact_analysis() call-site cleanup
#
# The wrapper _latest_profiling_snap_id() already delegated to
# get_latest_profiling_snapshot() (Phase 1C) — this phase removes the
# unnecessary indirection from impact_analysis()'s call site only. The
# remaining two lineage callers (critical_asset_analysis, lineage_summary)
# still call the wrapper (untouched). No behavior or connection-count
# change is expected: the wrapper was already opening its own resolver
# connection via get_latest_profiling_snapshot(); calling that function
# directly is a pure rename at this call site.
# ---------------------------------------------------------------------------

# 1 — get_latest_profiling_snapshot() called exactly once
def test_impact_analysis_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_a.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_impact_analysis_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_b.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by impact_analysis()
#
# See the equivalent note above test 3 in the Phase 2D.1 upstream section:
# this was one of the last production callers of the now-deleted wrapper.
# Structural replacement coverage lives in
# test_lineage_service_has_no_legacy_snapshot_helper and
# test_all_lineage_consumers_still_use_shared_resolver near the end of this
# file.


# 4 — response shape unchanged
def test_impact_analysis_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_d.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "table_fqn", "column_name",
        "affected_tables", "affected_domains", "affected_entities",
        "affected_business_definitions", "affected_pii_assets",
        "affected_governed_assets", "affected_reports",
        "column_propagation_paths", "impact_score", "impact_label",
        "total_affected_tables", "total_pii_columns", "total_governed_assets",
    }
    assert result["source_id"] == 1
    assert result["table_fqn"] == "dbo.customers"
    assert result["column_name"] is None


# 5 — impacted-asset selection unchanged
def test_impact_analysis_impacted_assets_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_e.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    affected_fqns = {t["table_fqn"] for t in result["affected_tables"]}
    assert affected_fqns == {"dbo.orders", "dbo.invoices", "dbo.payments"}
    assert result["total_affected_tables"] == 3
    assert result["total_pii_columns"] == 2
    pii_fqns = {p["table_fqn"] for p in result["affected_pii_assets"]}
    assert pii_fqns == {"dbo.invoices"}
    assert result["affected_domains"] == ["Finance", "Sales"]


# 6 — ordering unchanged: affected_tables sorted by (distance, table_fqn)
def test_impact_analysis_ordering_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_f.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert [t["table_fqn"] for t in result["affected_tables"]] == [
        "dbo.orders", "dbo.invoices", "dbo.payments",
    ]
    by_fqn = {t["table_fqn"]: t for t in result["affected_tables"]}
    assert by_fqn["dbo.orders"]["distance"]   == 1
    assert by_fqn["dbo.invoices"]["distance"] == 2
    assert by_fqn["dbo.payments"]["distance"] == 3


# 7 — impact score and severity label unchanged
def test_impact_analysis_scores_and_severity_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_g.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    # breadth(0.21) + pii(0.25) + governed(0.0) + domains(0.10) + class(0.0) = 0.56
    assert result["impact_score"] == 0.56
    assert result["impact_label"] == "HIGH"


# 8 — enrichment (domain/entity/dictionary/profiling) unchanged
def test_impact_analysis_enrichment_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_h.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    invoices = next(t for t in result["affected_tables"] if t["table_fqn"] == "dbo.invoices")
    assert invoices["domain"] == "Finance"
    assert invoices["entity"] == "Invoice"
    assert invoices["business_name"] == "Invoice"
    assert invoices["dict_approved"] is False
    assert invoices["table_class"] == "Transactional"
    assert invoices["classification_confidence"] == 0.80
    assert invoices["pii_column_count"] == 2
    assert invoices["referenced_by_count"] == 1
    assert invoices["fk_count"] == 1


# 9 — no-snapshot behavior unchanged: assets present, profiling fields absent
def test_impact_analysis_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_i.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    affected_fqns = {t["table_fqn"] for t in result["affected_tables"]}
    assert affected_fqns == {"dbo.orders"}
    orders = result["affected_tables"][0]
    assert "table_class" not in orders
    assert result["affected_pii_assets"] == []
    assert result["total_pii_columns"] == 0


# 10 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_impact_analysis_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_j.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'CANCELLED')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.orders', 'orders', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1,
    # table_class="Transactional" from _seed) wins over the newer CANCELLED
    # snapshot (version 2, table_class="New") — a newer non-COMPLETE
    # snapshot must never shadow older, usable COMPLETE evidence.
    orders = next(t for t in result["affected_tables"] if t["table_fqn"] == "dbo.orders")
    assert orders["table_class"] == "Transactional"


def test_impact_analysis_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_k.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'RUNNING')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.orders', 'orders', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    orders = next(t for t in result["affected_tables"] if t["table_fqn"] == "dbo.orders")
    assert orders["table_class"] == "Transactional"


# 11 — read-only behavior preserved
def test_impact_analysis_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_l.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _db_conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
    )
    before = {t: _dump(t) for t in tables}

    impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")
    impact_analysis(source_id=999, user_id="user-lin", table_fqn="dbo.orders")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 12 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_impact_analysis_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d3_m.db")
    _create_db(db)
    _seed(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.lineage_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2D.4 — critical_asset_analysis() call-site cleanup
#
# The wrapper _latest_profiling_snap_id() already delegated to
# get_latest_profiling_snapshot() (Phase 1C) — this phase removes the
# unnecessary indirection from critical_asset_analysis()'s call site only.
# The remaining lineage caller (lineage_summary) still calls the wrapper
# (untouched). No behavior or connection-count change is expected: the
# wrapper was already opening its own resolver connection via
# get_latest_profiling_snapshot(); calling that function directly is a pure
# rename at this call site.
#
# critical_asset_analysis() has no numeric "impact_score"/"impact_label"
# fields like impact_analysis() does — its quantitative outputs are the
# summary counts (total_foundation/total_terminal/total_hub/
# total_disconnected) and the table_class enrichment label attached to each
# categorized asset. Tests below map the spec's generic
# "scores"/"labels"/"score components" items onto those concrete fields.
# ---------------------------------------------------------------------------

def _seed_highly_referenced_fixture(db: str, source_id: int = 1, user_id: str = "user-lin") -> None:
    """Minimal fixture with distinct referenced_by_count values (no FK edges
    needed — the highly_referenced query reads purely from
    profiling_table_profiles) so ORDER BY ... DESC LIMIT 10 has no ties."""
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (?, ?, 'mssql', 'RELATIONAL')",
        (source_id, user_id),
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, ?, 3)",
        (source_id,),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (1, ?, 1, 1, 'COMPLETE')",
        (source_id,),
    )
    for fqn, name, rbc in [
        ("dbo.tbl_a", "tbl_a", 5),
        ("dbo.tbl_b", "tbl_b", 3),
        ("dbo.tbl_c", "tbl_c", 1),
    ]:
        conn.execute(
            "INSERT INTO profiling_table_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, referenced_by_count) "
            "VALUES (1, ?, ?, ?, 'dbo', ?)",
            (source_id, fqn, name, rbc),
        )
    conn.commit()
    conn.close()


# 1 — get_latest_profiling_snapshot() called exactly once
def test_critical_asset_analysis_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_a.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_critical_asset_analysis_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_b.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        critical_asset_analysis(source_id=1, user_id="user-lin")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by critical_asset_analysis()
#
# See the equivalent note above test 3 in the Phase 2D.1 upstream section:
# this was one of the last production callers of the now-deleted wrapper.
# Structural replacement coverage lives in
# test_lineage_service_has_no_legacy_snapshot_helper and
# test_all_lineage_consumers_still_use_shared_resolver near the end of this
# file.


# 4 — response shape unchanged
def test_critical_asset_analysis_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_d.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "foundation_tables", "terminal_tables", "hub_tables",
        "highly_referenced", "business_critical", "governance_critical", "summary",
    }
    assert set(result["summary"].keys()) == {
        "total_foundation", "total_terminal", "total_hub", "total_disconnected",
    }
    assert result["source_id"] == 1


# 5 — asset membership unchanged
def test_critical_asset_analysis_asset_membership_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_e.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    assert {t["table_fqn"] for t in result["foundation_tables"]} == {"dbo.customers"}
    assert {t["table_fqn"] for t in result["terminal_tables"]}   == {"dbo.payments"}
    assert {t["table_fqn"] for t in result["hub_tables"]}        == {"dbo.orders", "dbo.invoices"}
    assert {t["table_fqn"] for t in result["business_critical"]} == {"dbo.customers"}
    assert "dbo.invoices" in {t["table_fqn"] for t in result["governance_critical"]}


# 6 — highly_referenced ordering unchanged (distinct referenced_by_count, no ties)
def test_critical_asset_analysis_ordering_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_f.db")
    _create_db(db)
    _seed_highly_referenced_fixture(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    assert [r["table_fqn"] for r in result["highly_referenced"]] == [
        "dbo.tbl_a", "dbo.tbl_b", "dbo.tbl_c",
    ]
    assert [r["referenced_by_count"] for r in result["highly_referenced"]] == [5, 3, 1]


# 7 — summary counts ("scores") unchanged
def test_critical_asset_analysis_scores_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_g.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    assert result["summary"] == {
        "total_foundation": 1, "total_terminal": 1, "total_hub": 2, "total_disconnected": 1,
    }


# 8 — table_class labels attached per category unchanged
def test_critical_asset_analysis_labels_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_h.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    foundation = {t["table_fqn"]: t for t in result["foundation_tables"]}
    hub        = {t["table_fqn"]: t for t in result["hub_tables"]}
    terminal   = {t["table_fqn"]: t for t in result["terminal_tables"]}
    assert foundation["dbo.customers"]["table_class"] == "Master"
    assert hub["dbo.orders"]["table_class"]           == "Transactional"
    assert hub["dbo.invoices"]["table_class"]         == "Transactional"
    assert terminal["dbo.payments"]["table_class"]    == "Transactional"


# 9 — governance/highly-referenced score component fields unchanged
def test_critical_asset_analysis_score_components_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_i.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    gov_by_fqn = {r["table_fqn"]: r for r in result["governance_critical"]}
    invoices_gov = gov_by_fqn["dbo.invoices"]
    assert invoices_gov["pii_column_count"] == 2
    assert invoices_gov["confirmed_pii_count"] == 0
    assert invoices_gov["missing_domain"] == 0
    assert invoices_gov["missing_entity"] == 0
    assert invoices_gov["missing_dict"] == 1

    hr_by_fqn = {r["table_fqn"]: r for r in result["highly_referenced"]}
    customers_hr = hr_by_fqn["dbo.customers"]
    assert customers_hr["fk_count"] == 0
    assert customers_hr["classification_confidence"] == 0.90
    assert customers_hr["pii_column_count"] == 0


# 10 — enrichment (domain/entity/dictionary/profiling) unchanged
def test_critical_asset_analysis_enrichment_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_j.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    customers = next(t for t in result["foundation_tables"] if t["table_fqn"] == "dbo.customers")
    assert customers["domain"] == "Sales"
    assert customers["entity"] == "Customer"
    assert customers["business_name"] == "Customer"
    assert customers["dict_approved"] is True
    assert customers["table_class"] == "Master"
    assert customers["classification_confidence"] == 0.90
    assert customers["referenced_by_count"] == 1
    assert customers["fk_count"] == 0


# 11 — no-snapshot behavior unchanged: FK categorization present, profiling-derived sections empty
def test_critical_asset_analysis_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_k.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    assert {t["table_fqn"] for t in result["foundation_tables"]} == {"dbo.customers"}
    assert {t["table_fqn"] for t in result["terminal_tables"]}   == {"dbo.orders"}
    customers = result["foundation_tables"][0]
    assert "table_class" not in customers
    assert result["highly_referenced"] == []
    assert result["governance_critical"] == []
    assert result["summary"]["total_disconnected"] == 0


# 12 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_critical_asset_analysis_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_l.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'CANCELLED')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.customers', 'customers', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1,
    # table_class="Master" from _seed) wins over the newer CANCELLED
    # snapshot (version 2, table_class="New") — a newer non-COMPLETE
    # snapshot must never shadow older, usable COMPLETE evidence.
    customers = next(t for t in result["foundation_tables"] if t["table_fqn"] == "dbo.customers")
    assert customers["table_class"] == "Master"


def test_critical_asset_analysis_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_m.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'RUNNING')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.customers', 'customers', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    customers = next(t for t in result["foundation_tables"] if t["table_fqn"] == "dbo.customers")
    assert customers["table_class"] == "Master"


# 13 — read-only behavior preserved
def test_critical_asset_analysis_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_n.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _db_conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
    )
    before = {t: _dump(t) for t in tables}

    critical_asset_analysis(source_id=1, user_id="user-lin")
    critical_asset_analysis(source_id=999, user_id="user-lin")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 14 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_critical_asset_analysis_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d4_o.db")
    _create_db(db)
    _seed(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.lineage_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2D.5 — lineage_summary() call-site cleanup (final Lineage caller)
#
# The wrapper _latest_profiling_snap_id() already delegated to
# get_latest_profiling_snapshot() (Phase 1C) — this phase removes the
# unnecessary indirection from lineage_summary()'s call site, the last
# production caller in this module. No behavior or connection-count change
# is expected: the wrapper was already opening its own resolver connection
# via get_latest_profiling_snapshot(); calling that function directly is a
# pure rename at this call site. The wrapper definition itself is not
# deleted in this phase.
# ---------------------------------------------------------------------------

# 1 — get_latest_profiling_snapshot() called exactly once
def test_lineage_summary_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_a.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_lineage_summary_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_b.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.lineage_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        lineage_summary(source_id=1, user_id="user-lin")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by lineage_summary()
#
# See the equivalent note above test 3 in the Phase 2D.1 upstream section:
# this was the last production caller of the now-deleted wrapper. Structural
# replacement coverage lives in
# test_lineage_service_has_no_legacy_snapshot_helper and
# test_all_lineage_consumers_still_use_shared_resolver near the end of this
# file.


# 4 — response shape unchanged
def test_lineage_summary_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_d.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "source", "root_assets", "leaf_assets", "hub_assets",
        "disconnected_assets", "longest_chain", "average_depth", "coverage",
    }
    assert set(result["source"].keys()) == {"display_name", "source_type"}
    assert set(result["longest_chain"].keys()) == {"length", "path"}
    assert set(result["coverage"].keys()) == {
        "total_schema_tables", "tables_in_graph", "tables_disconnected",
        "foundation_count", "terminal_count", "hub_count", "relationship_coverage",
    }
    assert result["source_id"] == 1


# 5 — summary metric values unchanged
def test_lineage_summary_metric_values_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_e.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert result["longest_chain"]["length"] == 3
    assert result["longest_chain"]["path"] == [
        "dbo.customers", "dbo.orders", "dbo.invoices", "dbo.payments",
    ]
    assert result["average_depth"] == pytest.approx(1.5)
    assert result["coverage"]["relationship_coverage"] == 0.8


# 6 — relationship counts unchanged
def test_lineage_summary_relationship_counts_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_f.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert result["coverage"] == {
        "total_schema_tables": 5,
        "tables_in_graph": 4,
        "tables_disconnected": 1,
        "foundation_count": 1,
        "terminal_count": 1,
        "hub_count": 2,
        "relationship_coverage": 0.8,
    }


# 7 — classification counts (table_class labels on enriched assets) unchanged
def test_lineage_summary_classification_counts_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_g.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    root_by_fqn = {a["table_fqn"]: a for a in result["root_assets"]}
    hub_by_fqn  = {a["table_fqn"]: a for a in result["hub_assets"]}
    leaf_by_fqn = {a["table_fqn"]: a for a in result["leaf_assets"]}
    disc_by_fqn = {a["table_fqn"]: a for a in result["disconnected_assets"]}
    assert root_by_fqn["dbo.customers"]["table_class"] == "Master"
    assert hub_by_fqn["dbo.orders"]["table_class"]     == "Transactional"
    assert hub_by_fqn["dbo.invoices"]["table_class"]   == "Transactional"
    assert leaf_by_fqn["dbo.payments"]["table_class"]  == "Transactional"
    assert disc_by_fqn["dbo.products"]["table_class"]  == "Reference"


# 8 — PII counts (per-asset pii_column_count enrichment) unchanged
def test_lineage_summary_pii_counts_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_h.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    hub_by_fqn = {a["table_fqn"]: a for a in result["hub_assets"]}
    assert hub_by_fqn["dbo.invoices"]["pii_column_count"] == 2
    assert hub_by_fqn["dbo.orders"]["pii_column_count"]   == 0


# 9 — disconnected-table counts unchanged
def test_lineage_summary_disconnected_counts_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_i.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert result["coverage"]["tables_disconnected"] == 1
    assert {a["table_fqn"] for a in result["disconnected_assets"]} == {"dbo.products"}


# 10 — no-snapshot behavior unchanged: FK categorization present, profiling-derived sections empty
def test_lineage_summary_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_j.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert {a["table_fqn"] for a in result["root_assets"]} == {"dbo.customers"}
    assert {a["table_fqn"] for a in result["leaf_assets"]} == {"dbo.orders"}
    assert result["disconnected_assets"] == []
    assert result["coverage"]["tables_disconnected"] == 0
    customers = result["root_assets"][0]
    assert "table_class" not in customers


# 11 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_lineage_summary_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_k.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'CANCELLED')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.customers', 'customers', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1,
    # table_class="Master" from _seed) wins over the newer CANCELLED
    # snapshot (version 2, table_class="New") — a newer non-COMPLETE
    # snapshot must never shadow older, usable COMPLETE evidence.
    root_by_fqn = {a["table_fqn"]: a for a in result["root_assets"]}
    assert root_by_fqn["dbo.customers"]["table_class"] == "Master"


def test_lineage_summary_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_l.db")
    _create_db(db)
    _seed(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (2, 1, 1, 2, 'RUNNING')"
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (2, 1, 'dbo.customers', 'customers', 'dbo', 'New')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    root_by_fqn = {a["table_fqn"]: a for a in result["root_assets"]}
    assert root_by_fqn["dbo.customers"]["table_class"] == "Master"


# 12 — read-only behavior preserved
def test_lineage_summary_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_m.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _db_conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
    )
    before = {t: _dump(t) for t in tables}

    lineage_summary(source_id=1, user_id="user-lin")
    lineage_summary(source_id=999, user_id="user-lin")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 13 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_lineage_summary_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_p2d5_n.db")
    _create_db(db)
    _seed(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.lineage_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2D.6 — Lineage wrapper retirement
#
# _latest_profiling_snap_id() had zero remaining production call sites after
# Phases 2D.1-2D.5 moved all five lineage functions
# (get_upstream_lineage, get_downstream_lineage, impact_analysis,
# critical_asset_analysis, lineage_summary) onto get_latest_profiling_snapshot()
# directly. This phase deletes the dead wrapper's definition outright.
# Mirrors the Phase 2B.8 precedent in tests/test_phase6_semantic.py for
# data.semantic_layer_service's identically-named (but separately scoped)
# wrapper, and the Phase 2C.3 precedent in
# tests/test_phase3_business_knowledge.py for
# data.business_knowledge_service's copy.
# ---------------------------------------------------------------------------

# 1 — lineage_service_has_no_legacy_snapshot_helper
def test_lineage_service_has_no_legacy_snapshot_helper():
    import data.lineage_service as lineage_module
    assert not hasattr(lineage_module, "_latest_profiling_snap_id")


# 2 — lineage_service_source_has_no_legacy_helper_reference
def test_lineage_service_source_has_no_legacy_helper_reference():
    import inspect
    import data.lineage_service as lineage_module

    source = inspect.getsource(lineage_module)
    assert "_latest_profiling_snap_id" not in source


# 3 — all_lineage_consumers_still_use_shared_resolver
#
# Narrow inventory assertion (not a duplicate of every prior delegation
# test): confirms, via source inspection, that each of the five migrated
# functions still references the shared resolver by name. Full behavioral
# delegation coverage (spy call counts, correct source_id, snapshot policy,
# etc.) already exists per-function earlier in this file — this test only
# guards against a future edit silently reverting one of the five call
# sites back to the deleted local helper without any test noticing.
def test_all_lineage_consumers_still_use_shared_resolver():
    import inspect
    import data.lineage_service as lineage_module

    migrated = [
        lineage_module.get_upstream_lineage,
        lineage_module.get_downstream_lineage,
        lineage_module.impact_analysis,
        lineage_module.critical_asset_analysis,
        lineage_module.lineage_summary,
    ]
    for fn in migrated:
        source = inspect.getsource(fn)
        assert "get_latest_profiling_snapshot(" in source, (
            f"{fn.__name__} no longer appears to call the shared resolver"
        )
        assert "_latest_profiling_snap_id(" not in source, (
            f"{fn.__name__} appears to reference the retired local helper"
        )
