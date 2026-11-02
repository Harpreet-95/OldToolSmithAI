"""
Colaberry (CCPP, source 10004) business-knowledge package seed -- Phase 7,
Step 3J, Part 3.

This is the ONLY place in the codebase where Colaberry-specific business
facts appear. Every fact below is an owner-confirmed statement from the
approved Step 3J scope -- it is data for ONE customer's package, not an
application rule: nothing in core/ or data/ (data.business_knowledge_
package_service, core.sqlservice.business_knowledge_mapper, core.sqlservice.
business_knowledge_evidence) contains, imports, or depends on any of these
literal strings. A different customer gets a different seed script; the
shared production modules are unaffected either way.

This script does not decide when it runs. Calling seed_colaberry_package()
performs LIVE writes (business_knowledge_packages/_concepts/_relationships/
_facts, plus one owner-confirmed direct mapping row) against source_id's
metadata database and, if `activate=True` (the default), makes that package
the ACTIVE one core.sqlservice.schema_frontier will start reading (once
core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED is also turned on). Running
it against the real CCPP source is a live-preparation action gated by Phase
7 Step 3J Part 7's own controlled rollout checklist (backup/checksum/diff
review before any live write) -- this file only implements what that
checklist runs, it does not run itself.

Usage (after Part 7's pre-flight checks pass):
    python -m scripts.seed_colaberry_business_knowledge --source-id 10004 --user-id <owner_user_id>
"""
from __future__ import annotations

import argparse
import logging

import data.business_knowledge_mapping_store as mapping_store
import data.business_knowledge_package_service as package_service
from data.schema_fingerprint_service import compute_fingerprint

logger = logging.getLogger(__name__)

# Owner-confirmed, per Step 3J's approved scope. The ONLY concrete
# object-level fact in this entire package: dbo.CB_SUBSCRIPTION_PAYMENTS
# represents IPBC subscription/installment-plan payments. Every other
# object/column mapping is left for core.sqlservice.business_knowledge_
# mapper's AI-proposal + deterministic-verification pass -- this script
# never guesses at any other table/view name.
_CONFIRMED_INSTALLMENT_PAYMENT_OBJECT_FQN = "dbo.CB_SUBSCRIPTION_PAYMENTS"

# ---------------------------------------------------------------------------
# Concepts. Each tuple: (concept_id, concept_type, display_name, description, aliases)
# ---------------------------------------------------------------------------
_CONCEPTS: list[tuple[str, str, str, str, list[str]]] = [
    ("TRAINING_OPERATIONS", "DOMAIN", "Training Operations",
     "Colaberry's school/training operations and related student activity: students, programs, "
     "classes, enrollments, fees, mentors, sign-ins, Basecamp updates, homework, interview "
     "preparation, hiring/outcomes, clients, and website/account activity.",
     []),

    ("STUDENT", "ENTITY", "Student", "A person enrolled in one or more Colaberry training programs.", []),
    ("MENTOR", "ENTITY", "Mentor", "Supports students during training and preparation.", []),
    ("CLIENT", "ENTITY", "Client", "A hiring/business client associated with student outcomes.", []),
    ("CLASS", "ENTITY", "Class", "A scheduled cohort/section a student enrolls into for a program.", []),

    ("SQL_PROGRAM", "PROGRAM", "Basic SQL Training",
     "The basic SQL training program. Lasts approximately three months; Colaberry generally starts "
     "a new SQL class approximately every three months.",
     ["SQL training", "SQL course", "basic SQL", "data analytics SQL program"]),
    ("IPBC_PROGRAM", "PROGRAM", "IPBC Training",
     "IPBC training, generally entered after SQL training but not strictly requiring it -- a "
     "student with sufficient SQL background may enter IPBC directly.",
     ["IPBC"]),
    ("CAP_PROGRAM", "PROGRAM", "CAP Program",
     "A post-IPBC-training program. Free after IPBC training.", ["CAP"]),
    ("INTERVIEW_PREP_PROGRAM", "PROGRAM", "Interview Preparation",
     "Post-training interview preparation. Free.", ["interview prep"]),

    ("SQL_ENROLLED", "LIFECYCLE_STAGE", "Enrolled in SQL Program",
     "A student currently enrolled in the SQL program.",
     ["SQL signup", "class signup", "course enrollment"]),
    ("SQL_COMPLETED", "LIFECYCLE_STAGE", "Completed SQL Program",
     "A student who has completed the SQL program.",
     ["completed SQL training", "course completion", "class completion"]),
    ("IPBC_ENROLLED", "LIFECYCLE_STAGE", "Enrolled in IPBC", "A student currently enrolled in IPBC training.", []),
    ("IPBC_COMPLETED", "LIFECYCLE_STAGE", "Completed IPBC",
     "A student who has completed IPBC training; may lead to CAP eligibility and/or interview preparation.", []),
    ("CAP_ELIGIBLE", "LIFECYCLE_STAGE", "CAP Eligible", "Eligible for CAP participation after completing IPBC.", []),
    ("HIRING_OUTCOME", "LIFECYCLE_STAGE", "Hiring Outcome", "A recorded hiring/placement outcome for a student.", []),

    # Step 3J1 -- fee DEFINITION (the scheduled/listed price a program
    # charges) is a deliberately separate concept from fee PAYMENT (what a
    # specific enrolled student actually paid): the two are related but
    # never conflated. Both currently map under the existing FEE_SOURCE
    # role (this source's live business_knowledge_mappings table predates
    # the generic FEE_DEFINITION_SOURCE/FEE_PAYMENT_SOURCE role split --
    # see data.business_knowledge_mapping_store's own module docstring);
    # the distinction is carried by these being two separate concepts, not
    # by the role.
    ("SQL_COURSE_FEE", "PAYMENT_CONCEPT", "SQL Course Fee (listed price)",
     "The SQL training program's own SCHEDULED/LISTED course fee (what the program charges) -- "
     "distinct from IPBC payment concepts AND from SQL_FEE_PAID (what a student actually paid). A "
     "question mentioning SQL tuition, SQL course fees, class fees, or enrollment fees should not "
     "automatically be interpreted as an IPBC subscription payment.",
     ["SQL tuition", "tuition", "course fee", "class fee", "enrollment fee", "regular fee", "additional fee"]),
    ("SQL_FEE_PAID", "PAYMENT_CONCEPT", "SQL Fee Paid (actual payment)",
     "The ACTUAL amount a student paid toward their SQL enrollment/signup -- distinct from "
     "SQL_COURSE_FEE (the program's own scheduled/listed price) and from every IPBC payment "
     "concept. A vague question like 'total fees paid' may genuinely be ambiguous between this and "
     "SQL_COURSE_FEE (or an IPBC payment concept) absent surrounding program/enrollment evidence.",
     ["paid tuition", "fee paid", "amount paid", "student payment"]),
    ("IPBC_FULL_PAYMENT", "PAYMENT_CONCEPT", "IPBC Full Payment",
     "The full IPBC fee amount, paid upfront by a student who does not use the installment plan.", []),
    ("IPBC_INSTALLMENT_PLAN", "PAYMENT_CONCEPT", "IPBC Installment/Subscription Plan",
     "An IPBC subscription/installment-plan involving a down payment and subsequent payments or "
     "financing. Owner-confirmed to be represented by dbo.CB_SUBSCRIPTION_PAYMENTS. A question "
     "mentioning IPBC, subscription plans, installments, financing, loans, or down payments should "
     "not automatically be interpreted as an ordinary SQL course fee, and vice versa. A vague "
     "question such as 'total fees paid' may be genuinely ambiguous absent surrounding program or "
     "enrollment evidence.",
     ["subscription payment", "installment payment", "down payment", "financing"]),

    ("SIGN_IN", "ACTIVITY_CONCEPT", "Sign-In", "A student sign-in activity event.", []),
    ("BASECAMP_UPDATE", "ACTIVITY_CONCEPT", "Basecamp Update", "A student Basecamp update/activity event.", []),
    ("HOMEWORK_SUBMISSION", "ACTIVITY_CONCEPT", "Homework Submission", "A student homework submission event.", []),
    ("INTERVIEW_PREP_ACTIVITY", "ACTIVITY_CONCEPT", "Interview Preparation Activity",
     "A student's interview-preparation activity, provided free after training.", []),

    ("STUDENT_STATUS", "STATUS", "Student Status", "A student's current enrollment/lifecycle status.", []),
]

# ---------------------------------------------------------------------------
# Relationships. Each tuple: (from_concept_id, relationship_type, to_concept_id)
# ---------------------------------------------------------------------------
_RELATIONSHIPS: list[tuple[str, str, str]] = [
    ("STUDENT", "PARTICIPATES_IN", "SQL_PROGRAM"),
    ("STUDENT", "PARTICIPATES_IN", "IPBC_PROGRAM"),
    ("STUDENT", "PARTICIPATES_IN", "CAP_PROGRAM"),
    ("STUDENT", "PARTICIPATES_IN", "INTERVIEW_PREP_PROGRAM"),
    ("SQL_PROGRAM", "HAS_FEE", "SQL_COURSE_FEE"),
    ("SQL_ENROLLED", "HAS_PAYMENT", "SQL_FEE_PAID"),
    ("SQL_FEE_PAID", "SETTLES", "SQL_COURSE_FEE"),
    ("IPBC_PROGRAM", "HAS_FEE", "IPBC_FULL_PAYMENT"),
    ("IPBC_PROGRAM", "HAS_FEE", "IPBC_INSTALLMENT_PLAN"),
    ("IPBC_PROGRAM", "MAY_FOLLOW", "SQL_PROGRAM"),
    ("SQL_COMPLETED", "MAY_LEAD_TO", "IPBC_ENROLLED"),
    ("IPBC_COMPLETED", "MAY_LEAD_TO", "CAP_ELIGIBLE"),
    ("IPBC_COMPLETED", "MAY_LEAD_TO", "INTERVIEW_PREP_PROGRAM"),
    ("CAP_PROGRAM", "IS_FREE_AFTER", "IPBC_PROGRAM"),
    ("INTERVIEW_PREP_PROGRAM", "IS_FREE_AFTER", "IPBC_PROGRAM"),
    ("MENTOR", "SUPPORTS", "STUDENT"),
    ("MENTOR", "SUPPORTS", "SQL_PROGRAM"),
    ("MENTOR", "SUPPORTS", "IPBC_PROGRAM"),
    ("STUDENT", "PERFORMS", "SIGN_IN"),
    ("STUDENT", "PERFORMS", "BASECAMP_UPDATE"),
    ("STUDENT", "PERFORMS", "HOMEWORK_SUBMISSION"),
    ("STUDENT", "PERFORMS", "INTERVIEW_PREP_ACTIVITY"),
]

# ---------------------------------------------------------------------------
# Authoritative facts. Each tuple: (fact_key, fact_text, related_concept_id)
# ---------------------------------------------------------------------------
_FACTS: list[tuple[str, str, str | None]] = [
    ("ORG_SCOPE",
     "The database represents Colaberry's school/training operations and related student activity: "
     "students, programs, classes, enrollments, fees, mentors, sign-ins, Basecamp updates, homework, "
     "interview preparation, hiring/outcomes, clients, and website/account activity.", None),
    ("CONSOLIDATED_VIEWS_NOTE",
     "Consolidated business information is often exposed through views; underlying tables often store "
     "detailed transactions and activities. A view is not automatically authoritative -- restrictions, "
     "grain, dependencies, and hidden filters must still be checked.", None),
    ("SQL_PROGRAM_DURATION",
     "The SQL program lasts approximately three months.", "SQL_PROGRAM"),
    ("SQL_CLASS_CADENCE",
     "Colaberry generally starts new SQL classes approximately every three months.", "SQL_PROGRAM"),
    ("SQL_TO_IPBC_PATH",
     "After SQL training, a student may continue into IPBC training. A student who already has "
     "sufficient SQL background may enter IPBC directly without taking the basic SQL program. SQL "
     "completion is a common path into IPBC, but not an absolute prerequisite.", "IPBC_PROGRAM"),
    ("FEE_SEPARATION",
     "The SQL training program has its own course fee. IPBC training has a separate fee. SQL course "
     "tuition and IPBC subscription payments are related student-payment concepts but are not "
     "interchangeable.", "SQL_COURSE_FEE"),
    ("SQL_FEE_DEFINITION_VS_PAYMENT",
     "The SQL course's scheduled/listed price (e.g. columns such as CourseFee/CourseFeeEarlyBird on "
     "the program/class object) is a distinct concept from what a specific student actually paid "
     "toward their SQL enrollment (e.g. columns such as FeePaid/RegularFee/AddlFee on the "
     "enrollment/signup object). A generated report or answer must not conflate the listed price "
     "with the amount paid.", "SQL_FEE_PAID"),
    ("IPBC_PAYMENT_OPTIONS",
     "An IPBC student may pay the full IPBC amount upfront, or use a subscription/installment plan "
     "involving a down payment and subsequent payments or financing.", "IPBC_INSTALLMENT_PLAN"),
    ("SUBSCRIPTION_TABLE_CONFIRMED",
     f"{_CONFIRMED_INSTALLMENT_PAYMENT_OBJECT_FQN} is owner-confirmed as representing IPBC "
     "subscription/installment-plan payments.", "IPBC_INSTALLMENT_PLAN"),
    ("FEE_QUESTION_AMBIGUITY",
     "Questions mentioning SQL tuition, SQL course fees, class fees, or enrollment fees should not "
     "automatically be interpreted as IPBC subscription payments. Questions mentioning IPBC, "
     "subscription plans, installments, financing, loans, or down payments should not automatically "
     "be interpreted as ordinary SQL course fees. A vague question such as 'total fees paid' may be "
     "genuinely ambiguous unless surrounding program or enrollment evidence establishes the intended "
     "fee type.", None),
    ("CAP_FREE",
     "CAP programs are free after IPBC training.", "CAP_PROGRAM"),
    ("INTERVIEW_PREP_FREE",
     "Interview preparation is provided after training and is free. CAP participation and interview "
     "preparation must not be treated as paid subscriptions merely because they occur after a paid "
     "program.", "INTERVIEW_PREP_PROGRAM"),
    ("MENTOR_ROLE",
     "Mentors support students during training and preparation.", "MENTOR"),
    ("ACTIVITY_NOTE",
     "Student engagement/activity can include sign-ins, Basecamp updates, homework submissions, "
     "interview preparation, and related actions. Consolidated student views may provide current "
     "student information; detailed activity counts and events may remain in underlying activity "
     "tables.", None),
]


def seed_colaberry_package(
    source_id: int, user_id: str, *, created_by: str = "colaberry_seed_script",
    activate: bool = True, seed_confirmed_subscription_mapping: bool = True,
) -> dict:
    """Idempotent: safe to run more than once (every write below is an
    upsert keyed on natural business keys, and the authoritative-write
    guard means a rerun never regresses a since-corrected concept back to
    this script's own text). Returns a summary dict for Part 7 reporting."""
    package = package_service.create_or_get_draft_package(source_id, user_id, created_by)
    if package is None:
        raise ValueError(f"source_id={source_id} is not owned by user_id={user_id!r}")

    concept_results = [
        package_service.upsert_concept(
            package["id"], concept_id, concept_type, display_name,
            origin="AUTHORITATIVE", description=description, aliases=aliases,
        )
        for concept_id, concept_type, display_name, description, aliases in _CONCEPTS
    ]
    relationship_results = [
        package_service.upsert_relationship(
            package["id"], from_id, rel_type, to_id, origin="AUTHORITATIVE",
        )
        for from_id, rel_type, to_id in _RELATIONSHIPS
    ]
    fact_results = [
        package_service.store_authoritative_fact(
            package["id"], fact_key, fact_text, related_concept_id=related_concept_id, created_by=created_by,
        )
        for fact_key, fact_text, related_concept_id in _FACTS
    ]

    confirmed_mapping = None
    if seed_confirmed_subscription_mapping:
        fingerprint = compute_fingerprint(source_id, user_id)
        if fingerprint is None:
            logger.warning(
                "no schema_snapshots row for source_id=%s yet -- skipping the owner-confirmed "
                "IPBC_INSTALLMENT_PLAN mapping; run schema discovery first, then rerun this "
                "function to seed it.", source_id,
            )
        else:
            confirmed_mapping = mapping_store.upsert_mapping(
                package["id"], "IPBC_INSTALLMENT_PLAN", _CONFIRMED_INSTALLMENT_PAYMENT_OBJECT_FQN,
                "INSTALLMENT_PAYMENT_SOURCE", columns=[], mapping_kind="DIRECT", status="VERIFIED",
                confidence=1.0, evidence=["owner-confirmed object meaning (Step 3J, Part 3)"],
                schema_fingerprint=fingerprint["fingerprint_hash"],
                content_hash="0" * 64,  # owner-confirmed, not tied to any AI generation's content hash
                provider_id=None, model=None,
            )

    activated_package = None
    if activate:
        activated_package = package_service.activate_package(source_id, user_id, package["package_version"])

    return {
        "package_id": package["id"],
        "package_version": package["package_version"],
        "activated": activated_package is not None,
        "concepts_written": len(concept_results),
        "relationships_written": len(relationship_results),
        "facts_written": len(fact_results),
        "confirmed_subscription_mapping_written": confirmed_mapping is not None,
    }


# ---------------------------------------------------------------------------
# Step 3J1 -- incremental SQL fee-definition/fee-payment disambiguation
# update. Deliberately does NOT call seed_colaberry_package()/create_or_get_
# draft_package(): the package is already ACTIVE (version 1), and creating a
# new draft version would start that version with ZERO concepts/mappings,
# forcing a full remap of everything -- exactly what this update must avoid.
# Instead this writes directly against the ALREADY-ACTIVE package_id,
# touching only the specific concept_ids/relationships/fact_keys this update
# actually changes (read from the SAME _CONCEPTS/_RELATIONSHIPS/_FACTS lists
# above -- never duplicated text), leaving every other concept's row (and
# therefore its own per-concept content hash) completely untouched.
# ---------------------------------------------------------------------------

_STEP_3J1_TOUCHED_CONCEPT_IDS = frozenset({
    "SQL_PROGRAM", "SQL_ENROLLED", "SQL_COURSE_FEE", "SQL_COMPLETED", "SQL_FEE_PAID",
})
_STEP_3J1_TOUCHED_RELATIONSHIP_KEYS = frozenset({
    ("SQL_ENROLLED", "HAS_PAYMENT", "SQL_FEE_PAID"),
    ("SQL_FEE_PAID", "SETTLES", "SQL_COURSE_FEE"),
})
_STEP_3J1_TOUCHED_FACT_KEYS = frozenset({"SQL_FEE_DEFINITION_VS_PAYMENT"})


def apply_step3j1_sql_fee_disambiguation(source_id: int, user_id: str, *, created_by: str = "colaberry_seed_script") -> dict:
    """Idempotent incremental update against the source's already-ACTIVE
    business-knowledge package. Raises ValueError if source_id has no
    ACTIVE package yet (run seed_colaberry_package first)."""
    package = package_service.get_active_package(source_id, user_id)
    if package is None:
        raise ValueError(
            f"source_id={source_id} has no ACTIVE business-knowledge package for user_id={user_id!r} -- "
            "run seed_colaberry_package() first."
        )

    concept_results = [
        package_service.upsert_concept(
            package["id"], concept_id, concept_type, display_name,
            origin="AUTHORITATIVE", description=description, aliases=aliases,
        )
        for concept_id, concept_type, display_name, description, aliases in _CONCEPTS
        if concept_id in _STEP_3J1_TOUCHED_CONCEPT_IDS
    ]
    relationship_results = [
        package_service.upsert_relationship(package["id"], from_id, rel_type, to_id, origin="AUTHORITATIVE")
        for from_id, rel_type, to_id in _RELATIONSHIPS
        if (from_id, rel_type, to_id) in _STEP_3J1_TOUCHED_RELATIONSHIP_KEYS
    ]
    fact_results = [
        package_service.store_authoritative_fact(
            package["id"], fact_key, fact_text, related_concept_id=related_concept_id, created_by=created_by,
        )
        for fact_key, fact_text, related_concept_id in _FACTS
        if fact_key in _STEP_3J1_TOUCHED_FACT_KEYS
    ]

    return {
        "package_id": package["id"],
        "package_version": package["package_version"],
        "concepts_touched": [c["concept_id"] for c in concept_results],
        "relationships_touched": len(relationship_results),
        "facts_touched": [f["fact_key"] for f in fact_results],
    }


# ---------------------------------------------------------------------------
# Step 3J2 -- owner-confirmed correction of dbo.ADF_Class's real business
# meaning (and its knock-on effects on dbo.ADF_Path/dbo.IPBC_SubscriptionPlan/
# dbo.CB_SUBSCRIPTION_PAYMENTS). Pure DB corrections -- no AI provider call.
# Every mapping-role/status change below is applied via data.business_
# knowledge_mapping_store.upsert_mapping directly (the SAME idempotent,
# natural-key upsert the AI mapper itself uses), never a rebuild of the live
# table, and never a silent delete: a rejected row's original evidence/
# columns/confidence/provider attribution are preserved unchanged -- only
# status and rejection_reason change.
# ---------------------------------------------------------------------------

_STEP_3J2_FACTS: list[tuple[str, str, str | None]] = [
    ("ADF_CLASS_MEANING",
     "dbo.ADF_Class contains Colaberry class offerings and historical class information: class "
     "names, start dates, course/listed fees, enrollment counts, and IPBC-enrollment counts. It "
     "covers current and historical classes, including online classes and earlier in-person "
     "classes.", "CLASS"),
    ("ADF_CLASS_MATERIALS_ACCESS",
     "Students retain access to class study materials for approximately one year after their "
     "class start date.", "CLASS"),
    ("ADF_CLASS_END_DATE_NOTE",
     "The absence of a conventional class end date on a class record must not automatically be "
     "interpreted as missing or invalid data.", "CLASS"),
    ("ADF_CLASS_NOT_ENROLLMENT_OR_PAYMENT",
     "dbo.ADF_Class represents a class offering/history, not an individual student enrollment and "
     "not an IPBC installment transaction. Individual student enrollment and actual fee-payment "
     "information belongs to dbo.ADF_ClassSignups, not dbo.ADF_Class.", "CLASS"),
    ("ADF_PATH_MEANING",
     "dbo.ADF_Path represents the program/path definition connected to classes through real schema "
     "relationships such as PathID.", "SQL_PROGRAM"),
    ("ADF_CLASSSIGNUPS_MEANING",
     "dbo.ADF_ClassSignups represents individual student enrollment/signup information and actual "
     "student fee-payment information such as FeePaid.", "SQL_ENROLLED"),
    ("IPBC_SUBSCRIPTIONPLAN_MEANING",
     "dbo.IPBC_SubscriptionPlan represents IPBC payment-plan definitions and must not be treated as "
     "a SQL-course-fee source.", "IPBC_INSTALLMENT_PLAN"),
]

# Each tuple: (object_fqn, concept_id, mapping_role, new_rejection_reason)
_STEP_3J2_REJECTIONS: list[tuple[str, str, str, str]] = [
    ("dbo.ADF_Class", "SQL_PROGRAM", "PREFERRED_BUSINESS_VIEW", "preferred_view_requires_view_object"),
    ("dbo.ADF_Enrollment_Config", "SQL_PROGRAM", "PREFERRED_BUSINESS_VIEW", "preferred_view_requires_view_object"),
    ("dbo.CB_RPT_IPBC_DASHBOARD", "IPBC_PROGRAM", "PREFERRED_BUSINESS_VIEW", "preferred_view_requires_view_object"),
    ("dbo.CB_SUBSCRIPTION_PAYMENTS", "IPBC_INSTALLMENT_PLAN", "PREFERRED_BUSINESS_VIEW", "preferred_view_requires_view_object"),
    ("dbo.IPBC_SubscriptionPlan", "IPBC_INSTALLMENT_PLAN", "PREFERRED_BUSINESS_VIEW", "preferred_view_requires_view_object"),
    ("dbo.VW_TWC_MASTER_STUDENT_LIST_IPBC", "IPBC_PROGRAM", "PREFERRED_BUSINESS_VIEW", "preferred_view_requires_declared_or_proven_authority"),
    ("dbo.IPBC_SubscriptionPlan", "SQL_COURSE_FEE", "FEE_SOURCE", "payment_family_conflict"),
]

# Each tuple: (concept_id, object_fqn, mapping_role, columns, evidence)
_STEP_3J2_ADDITIONS: list[tuple[str, str, str, list[str], list[str]]] = [
    ("SQL_PROGRAM", "dbo.ADF_Path", "CANONICAL_ENTITY_SOURCE", ["PathID", "PathName", "PathDesc", "ProgramType"],
     ["owner-confirmed: ADF_Path is the program/path definition table",
      "structural: dbo.ADF_Class.PathID -> dbo.ADF_Path.PathID is a confirmed FOREIGN_KEY relationship",
      "structural: existing semantic-entity-contract classifies dbo.ADF_Path CANONICAL_TABLE for the Program entity"]),
    ("IPBC_INSTALLMENT_PLAN", "dbo.IPBC_SubscriptionPlan", "FEE_SOURCE",
     ["initialdownpayment", "postplacementmonthlypayment", "totalcost"],
     ["owner-confirmed: IPBC_SubscriptionPlan holds IPBC payment-plan DEFINITION terms, "
      "not a preferred consolidated business view (it is a physical table)"]),
]


def apply_step3j2_adf_class_correction(source_id: int, user_id: str, *, created_by: str = "colaberry_seed_script") -> dict:
    """Idempotent. Requires an ACTIVE package and an existing schema
    fingerprint (run seed_colaberry_package first). Makes zero provider
    calls -- every change here is a direct, owner-confirmed correction to
    already-computed mapping rows, never a new AI proposal."""
    package = package_service.get_active_package(source_id, user_id)
    if package is None:
        raise ValueError(
            f"source_id={source_id} has no ACTIVE business-knowledge package for user_id={user_id!r} -- "
            "run seed_colaberry_package() first."
        )
    fingerprint = compute_fingerprint(source_id, user_id)
    if fingerprint is None:
        raise ValueError(f"source_id={source_id} has no schema_snapshots row yet -- run schema discovery first.")
    schema_fingerprint = fingerprint["fingerprint_hash"]

    fact_results = [
        package_service.store_authoritative_fact(
            package["id"], fact_key, fact_text, related_concept_id=related_concept_id, created_by=created_by,
        )
        for fact_key, fact_text, related_concept_id in _STEP_3J2_FACTS
    ]

    rejected_results = []
    for object_fqn, concept_id, mapping_role, reason in _STEP_3J2_REJECTIONS:
        existing = next(
            (m for m in mapping_store.list_mappings(package["id"], object_fqn=object_fqn)
             if m["concept_id"] == concept_id and m["mapping_role"] == mapping_role),
            None,
        )
        if existing is None:
            logger.warning(
                "Step 3J2 correction: no existing mapping found for object_fqn=%r concept_id=%r "
                "mapping_role=%r -- nothing to reject (already absent or never proposed).",
                object_fqn, concept_id, mapping_role,
            )
            continue
        rejected_results.append(mapping_store.upsert_mapping(
            package["id"], concept_id, object_fqn, mapping_role,
            columns=existing["columns"], mapping_kind="UNRESOLVED", status="REJECTED",
            confidence=existing["confidence"], evidence=existing["evidence"],
            schema_fingerprint=existing["schema_fingerprint"], content_hash=existing["content_hash"],
            provider_id=existing["provider_id"], model=existing["model"],
            contract_version=existing["contract_version"], rejection_reason=reason,
        ))

    added_results = [
        mapping_store.upsert_mapping(
            package["id"], concept_id, object_fqn, mapping_role,
            columns=columns, mapping_kind="DIRECT", status="VERIFIED", confidence=1.0, evidence=evidence,
            schema_fingerprint=schema_fingerprint, content_hash="0" * 64, provider_id=None, model=None,
        )
        for concept_id, object_fqn, mapping_role, columns, evidence in _STEP_3J2_ADDITIONS
    ]

    return {
        "package_id": package["id"],
        "facts_touched": [f["fact_key"] for f in fact_results],
        "rejected": [
            {"object_fqn": r["object_fqn"], "concept_id": r["concept_id"], "mapping_role": r["mapping_role"],
             "rejection_reason": r["rejection_reason"]}
            for r in rejected_results
        ],
        "added": [
            {"object_fqn": a["object_fqn"], "concept_id": a["concept_id"], "mapping_role": a["mapping_role"]}
            for a in added_results
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--user-id", type=str, required=True)
    parser.add_argument("--no-activate", action="store_true")
    parser.add_argument("--skip-confirmed-mapping", action="store_true")
    args = parser.parse_args()

    summary = seed_colaberry_package(
        args.source_id, args.user_id,
        activate=not args.no_activate,
        seed_confirmed_subscription_mapping=not args.skip_confirmed_mapping,
    )
    print(summary)


if __name__ == "__main__":
    main()
