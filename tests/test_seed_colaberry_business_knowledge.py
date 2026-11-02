"""
Colaberry seed script tests -- Phase 7, Step 3J, Part 6.

Covers:
  - the seed is idempotent (safe to rerun)
  - the owner-confirmed dbo.CB_SUBSCRIPTION_PAYMENTS mapping is stored
    without that literal string appearing in any shared production module
  - the confirmed mapping is skipped gracefully (not an error) when no
    schema snapshot exists yet, and picked up on a later rerun once one does
  - authoritative facts are never overwritten by a later, different
    rerun's text for a fact this script itself owns (upsert is stable)

Never touches the real source_id=10004 CCPP database -- uses the same
isolated SQLite fixture plumbing as the rest of the business-knowledge test
suite.
"""
from __future__ import annotations

import inspect
import shutil

import pytest

import data.business_knowledge_mapping_store as mapping_store_module
import data.business_knowledge_package_service as package_service_module
import data.db as data_db_module
import data.schema_service as schema_service_module

from scripts.seed_colaberry_business_knowledge import (
    apply_step3j1_sql_fee_disambiguation,
    apply_step3j2_adf_class_correction,
    seed_colaberry_package,
)
from tests.sqlservice_fixtures import (
    SOURCE_ID, USER_ID, _insert_snapshot, _insert_source, _make_connection,
    template_db_path,  # noqa: F401
)


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(package_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(mapping_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_service_module, "get_connection", lambda: _make_connection(path))
    return path


def test_seed_without_schema_snapshot_skips_confirmed_mapping_gracefully(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    summary = seed_colaberry_package(SOURCE_ID, USER_ID)
    assert summary["confirmed_subscription_mapping_written"] is False
    assert summary["concepts_written"] > 0
    assert summary["relationships_written"] > 0
    assert summary["facts_written"] > 0
    assert summary["activated"] is True


def test_seed_with_schema_snapshot_writes_confirmed_mapping(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    _insert_snapshot(db_path, snapshot_id=SOURCE_ID, source_id=SOURCE_ID)
    summary = seed_colaberry_package(SOURCE_ID, USER_ID)
    assert summary["confirmed_subscription_mapping_written"] is True

    package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    mappings = mapping_store_module.list_mappings(package["id"])
    confirmed = [m for m in mappings if m["mapping_role"] == "INSTALLMENT_PAYMENT_SOURCE"]
    assert len(confirmed) == 1
    assert confirmed[0]["object_fqn"] == "dbo.CB_SUBSCRIPTION_PAYMENTS"
    assert confirmed[0]["status"] == "VERIFIED"
    assert confirmed[0]["concept_id"] == "IPBC_INSTALLMENT_PLAN"


def test_seed_is_idempotent(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    _insert_snapshot(db_path, snapshot_id=SOURCE_ID, source_id=SOURCE_ID)

    first = seed_colaberry_package(SOURCE_ID, USER_ID)
    second = seed_colaberry_package(SOURCE_ID, USER_ID)
    assert first["concepts_written"] == second["concepts_written"]
    assert first["relationships_written"] == second["relationships_written"]
    assert first["facts_written"] == second["facts_written"]

    package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    concepts = package_service_module.list_concepts(package["id"])
    # No duplicate rows from the second run.
    assert len({c["concept_id"] for c in concepts}) == len(concepts)


def test_confirmed_mapping_object_name_absent_from_shared_production_modules():
    import core.sqlservice.business_knowledge_evidence as evidence_module
    import core.sqlservice.business_knowledge_mapper as mapper_module
    import core.sqlservice.business_knowledge_mapping_providers as providers_module

    forbidden = "cb_subscription_payments"
    for module in (evidence_module, mapper_module, providers_module,
                   package_service_module, mapping_store_module):
        assert forbidden not in inspect.getsource(module).lower(), (
            f"owner-confirmed object name leaked into shared production module {module.__name__}"
        )


def test_step3j1_update_requires_existing_active_package(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    with pytest.raises(ValueError):
        apply_step3j1_sql_fee_disambiguation(SOURCE_ID, USER_ID)


def test_step3j1_update_touches_only_named_concepts_never_creates_new_version(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    _insert_snapshot(db_path, snapshot_id=SOURCE_ID, source_id=SOURCE_ID)
    seed_colaberry_package(SOURCE_ID, USER_ID)

    before_package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    before_student = package_service_module.get_concept(before_package["id"], "STUDENT")

    summary = apply_step3j1_sql_fee_disambiguation(SOURCE_ID, USER_ID)

    assert summary["package_id"] == before_package["id"]
    assert summary["package_version"] == before_package["package_version"]  # no new version created
    assert set(summary["concepts_touched"]) == {"SQL_PROGRAM", "SQL_ENROLLED", "SQL_COURSE_FEE", "SQL_COMPLETED", "SQL_FEE_PAID"}
    assert summary["relationships_touched"] == 2
    assert summary["facts_touched"] == ["SQL_FEE_DEFINITION_VS_PAYMENT"]

    after_package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    assert after_package["package_version"] == before_package["package_version"]  # still v1, not superseded

    after_student = package_service_module.get_concept(after_package["id"], "STUDENT")
    assert after_student == before_student  # untouched concept unchanged, same updated_at etc.

    sql_fee_paid = package_service_module.get_concept(after_package["id"], "SQL_FEE_PAID")
    assert sql_fee_paid is not None
    assert "fee paid" in sql_fee_paid["aliases"]

    sql_course_fee = package_service_module.get_concept(after_package["id"], "SQL_COURSE_FEE")
    assert "regular fee" in sql_course_fee["aliases"]


def test_step3j1_update_is_idempotent(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    _insert_snapshot(db_path, snapshot_id=SOURCE_ID, source_id=SOURCE_ID)
    seed_colaberry_package(SOURCE_ID, USER_ID)

    first = apply_step3j1_sql_fee_disambiguation(SOURCE_ID, USER_ID)
    second = apply_step3j1_sql_fee_disambiguation(SOURCE_ID, USER_ID)
    assert first == second

    package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    concepts = package_service_module.list_concepts(package["id"])
    assert len({c["concept_id"] for c in concepts}) == len(concepts)  # no duplicates


def _fingerprint_hash(db_path):
    from data.schema_fingerprint_service import compute_fingerprint
    return compute_fingerprint(SOURCE_ID, USER_ID)["fingerprint_hash"]


def test_step3j2_correction_requires_active_package_and_snapshot(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    with pytest.raises(ValueError):
        apply_step3j2_adf_class_correction(SOURCE_ID, USER_ID)


def test_step3j2_correction_rejects_named_rows_and_adds_new_ones(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    _insert_snapshot(db_path, snapshot_id=SOURCE_ID, source_id=SOURCE_ID)
    seed_colaberry_package(SOURCE_ID, USER_ID)
    apply_step3j1_sql_fee_disambiguation(SOURCE_ID, USER_ID)

    package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    fp = _fingerprint_hash(db_path)

    # Seed the exact "before correction" scenario this function is meant to
    # fix -- a table mislabeled PREFERRED_BUSINESS_VIEW, and a semantically
    # conflicting cross-program FEE_SOURCE mapping.
    mapping_store_module.upsert_mapping(
        package["id"], "SQL_PROGRAM", "dbo.ADF_Class", "PREFERRED_BUSINESS_VIEW",
        columns=["classname"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["original AI evidence -- must survive the correction unchanged"],
        schema_fingerprint=fp, content_hash="a" * 64, provider_id="openai", model="fake-model-1",
    )
    mapping_store_module.upsert_mapping(
        package["id"], "SQL_COURSE_FEE", "dbo.IPBC_SubscriptionPlan", "FEE_SOURCE",
        columns=["price"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.85,
        evidence=["original AI evidence"], schema_fingerprint=fp, content_hash="b" * 64,
        provider_id="openai", model="fake-model-1",
    )
    # A correct, unrelated existing mapping that must never be touched.
    mapping_store_module.upsert_mapping(
        package["id"], "CLASS", "dbo.ADF_Class", "CANONICAL_ENTITY_SOURCE",
        columns=["classname"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["original AI evidence"], schema_fingerprint=fp, content_hash="c" * 64,
        provider_id="openai", model="fake-model-1",
    )

    summary = apply_step3j2_adf_class_correction(SOURCE_ID, USER_ID)

    rejected_keys = {(r["object_fqn"], r["concept_id"], r["mapping_role"]) for r in summary["rejected"]}
    assert ("dbo.ADF_Class", "SQL_PROGRAM", "PREFERRED_BUSINESS_VIEW") in rejected_keys
    assert ("dbo.IPBC_SubscriptionPlan", "SQL_COURSE_FEE", "FEE_SOURCE") in rejected_keys

    added_keys = {(a["object_fqn"], a["concept_id"], a["mapping_role"]) for a in summary["added"]}
    assert ("dbo.ADF_Path", "SQL_PROGRAM", "CANONICAL_ENTITY_SOURCE") in added_keys
    assert ("dbo.IPBC_SubscriptionPlan", "IPBC_INSTALLMENT_PLAN", "FEE_SOURCE") in added_keys

    all_mappings = mapping_store_module.list_mappings(package["id"])
    corrected = next(
        m for m in all_mappings
        if m["object_fqn"] == "dbo.ADF_Class" and m["concept_id"] == "SQL_PROGRAM"
        and m["mapping_role"] == "PREFERRED_BUSINESS_VIEW"
    )
    assert corrected["status"] == "REJECTED"
    assert corrected["rejection_reason"] == "preferred_view_requires_view_object"
    assert corrected["evidence"] == ["original AI evidence -- must survive the correction unchanged"]  # preserved
    assert corrected["confidence"] == pytest.approx(0.9)  # preserved, never silently reset

    conflict = next(
        m for m in all_mappings
        if m["object_fqn"] == "dbo.IPBC_SubscriptionPlan" and m["concept_id"] == "SQL_COURSE_FEE"
    )
    assert conflict["status"] == "REJECTED"
    assert conflict["rejection_reason"] == "payment_family_conflict"

    # Unrelated, correct existing mappings are never touched.
    class_canonical = next(
        m for m in all_mappings
        if m["object_fqn"] == "dbo.ADF_Class" and m["concept_id"] == "CLASS"
    )
    assert class_canonical["status"] == "VERIFIED"


def test_step3j2_correction_is_idempotent(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    _insert_snapshot(db_path, snapshot_id=SOURCE_ID, source_id=SOURCE_ID)
    seed_colaberry_package(SOURCE_ID, USER_ID)
    apply_step3j1_sql_fee_disambiguation(SOURCE_ID, USER_ID)

    package = package_service_module.get_active_package(SOURCE_ID, USER_ID)
    fp = _fingerprint_hash(db_path)
    mapping_store_module.upsert_mapping(
        package["id"], "SQL_PROGRAM", "dbo.ADF_Class", "PREFERRED_BUSINESS_VIEW",
        columns=[], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9, evidence=[],
        schema_fingerprint=fp, content_hash="a" * 64, provider_id="openai", model="fake-model-1",
    )

    first = apply_step3j2_adf_class_correction(SOURCE_ID, USER_ID)
    second = apply_step3j2_adf_class_correction(SOURCE_ID, USER_ID)
    assert first == second

    mappings = mapping_store_module.list_mappings(package["id"])
    # No duplicate rows from the second run.
    keys = [(m["object_fqn"], m["concept_id"], m["mapping_role"]) for m in mappings]
    assert len(keys) == len(set(keys))


def test_rejects_source_not_owned_by_user(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    with pytest.raises(ValueError):
        seed_colaberry_package(SOURCE_ID, "someone_else")
