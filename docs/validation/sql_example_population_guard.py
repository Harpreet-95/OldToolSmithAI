"""
Offline SQL-example golden-leakage guard — SQL rebuild Phase 5, Step 11C.

Relocated here from tests/test_sql_example_golden_leakage.py (where it was
first built, Step 11B). A live example-population run is not a test — it is
a script that must import a real validation module and run it BEFORE ever
calling core.sqlservice.sql_example_store.insert_trusted_example() on a
generated batch, exactly like docs/validation/golden_runner.py is the real
(non-test) home for other pipeline-evaluation logic. Tests may still import
this module freely (see tests/test_sql_example_golden_leakage.py, which now
imports it rather than defining these functions itself); production code
under core/, data/, or api/ must never import it — see that same test
file's own AST-based boundary test, which asserts this module's name never
appears in any import statement under those three trees.

Dataset-agnostic by construction (mirrors docs/validation/
golden_case_model.py's own "dataset-agnostic, the fixture supplies the
data" split): `golden_cases` is always a caller-supplied sequence of
objects exposing `.question` (str) and `.case_id` (str) — this module has
no import of, and no opinion about, any particular golden-case fixture
module (e.g. ccpp_golden_cases_v2.CCPP_GOLDEN_CASES). No golden question,
SQL, expected object, ID, or note is ever hardcoded here.

Pure/offline: no database connection, no OpenAI/embedding-provider call, no
live SQL Server connection, no filesystem access beyond this file itself.
"""

from __future__ import annotations

import difflib
import re
from typing import Sequence

__all__ = [
    "CLOSE_COPY_SIMILARITY_THRESHOLD",
    "GoldenLeakagePopulationBlocked",
    "find_golden_leakage",
    "find_close_copy_leakage",
    "find_golden_leakage_in_generated_examples",
    "block_population_on_golden_leakage",
]

CLOSE_COPY_SIMILARITY_THRESHOLD = 0.90


def _normalize_question(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def find_golden_leakage(training_examples, golden_cases) -> list[str]:
    """
    Generic, reusable checker (import this from any example-generation or
    population script and call it BEFORE inserting a batch) — returns a
    list of human-readable problem descriptions, empty when no leakage is
    found. Checks both:
      1. an exact (normalized) question-text match against any golden
         case's own question, and
      2. a training_id that collides with (or embeds) a golden case_id.
    """
    problems: list[str] = []
    golden_questions_normalized = {_normalize_question(c.question): c.case_id for c in golden_cases}
    golden_case_ids = {c.case_id for c in golden_cases}

    for entry in training_examples:
        question = entry.get("question_pattern", "")
        normalized = _normalize_question(question)
        if normalized in golden_questions_normalized:
            problems.append(
                f"training entry {entry.get('training_id')!r} question matches golden case "
                f"{golden_questions_normalized[normalized]!r} verbatim: {question!r}"
            )
        training_id = str(entry.get("training_id", ""))
        for case_id in golden_case_ids:
            if case_id and case_id in training_id:
                problems.append(
                    f"training entry {training_id!r} embeds golden case_id {case_id!r}"
                )
    return problems


def find_close_copy_leakage(
    training_examples, golden_cases, *, threshold: float = CLOSE_COPY_SIMILARITY_THRESHOLD,
) -> list[str]:
    """
    A genuine near-duplicate ("close copy") check, additive to
    find_golden_leakage()'s own exact-after-normalization rule: flags a
    training entry whose normalized question_pattern is HIGHLY similar
    (difflib.SequenceMatcher ratio >= `threshold`) to a golden case's own
    question, even after minor wording changes that survive normalization
    (a word substituted, reordered, or lightly rephrased) — the case
    find_golden_leakage's own documented boundary explicitly does not
    cover. An entry find_golden_leakage() already flags as an EXACT match
    is skipped here (ratio 1.0 would always also clear `threshold`,
    reporting the same collision twice under two different messages), so
    the two checkers' outputs are complementary, never overlapping.
    """
    problems: list[str] = []
    golden_by_normalized = {_normalize_question(c.question): c for c in golden_cases}

    for entry in training_examples:
        question = entry.get("question_pattern", "")
        normalized = _normalize_question(question)
        if normalized in golden_by_normalized:
            continue  # exact match — already reported by find_golden_leakage()
        best_ratio = 0.0
        best_case = None
        for golden_normalized, case in golden_by_normalized.items():
            ratio = difflib.SequenceMatcher(None, normalized, golden_normalized).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_case = case
        if best_case is not None and best_ratio >= threshold:
            problems.append(
                f"training entry {entry.get('training_id')!r} question is a close copy "
                f"(similarity={best_ratio:.3f}) of golden case {best_case.case_id!r}: {question!r}"
            )
    return problems


def _entries_from_generated_examples(generated_examples: Sequence) -> tuple[dict, ...]:
    """
    Adapts a REAL generated-candidate collection — an iterable of
    core.sqlservice.sql_example_generator.GeneratedSqlExample, or anything
    exposing an equivalent `.candidate.question_pattern`/`.candidate.
    content_hash` shape — into the same {"training_id", "question_pattern"}
    dict shape find_golden_leakage()/find_close_copy_leakage() already
    accept. `training_id` is the candidate's own content_hash (a stable,
    already-computed identity field on every TrustedSqlExampleCandidate —
    never invented here, never a golden id).
    """
    entries = []
    for generated in generated_examples:
        candidate = generated.candidate
        entries.append({
            "training_id": candidate.content_hash,
            "question_pattern": candidate.question_pattern,
        })
    return tuple(entries)


def find_golden_leakage_in_generated_examples(generated_examples: Sequence, golden_cases) -> tuple[str, ...]:
    """
    The offline helper a live example-population run must call on the FULL
    generated candidate collection (core.sqlservice.sql_example_generator.
    SqlExampleGenerationResult.candidates) before ever calling
    core.sqlservice.sql_example_store.insert_trusted_example() on any of
    them — combines find_golden_leakage()'s exact-after-normalization +
    id-embedding checks with find_close_copy_leakage()'s near-duplicate
    check. Returns an empty tuple when nothing is found; never raises
    itself (see block_population_on_golden_leakage() for the blocking
    entry point a population script should actually call).
    """
    entries = _entries_from_generated_examples(generated_examples)
    problems = find_golden_leakage(entries, golden_cases) + find_close_copy_leakage(entries, golden_cases)
    return tuple(problems)


class GoldenLeakagePopulationBlocked(RuntimeError):
    """Raised by block_population_on_golden_leakage() — a live example-
    population run must let this propagate and insert NOTHING, not catch
    it and drop only the offending entries."""


def block_population_on_golden_leakage(generated_examples: Sequence, golden_cases) -> None:
    """
    Fail-closed gate for a live population run: raises
    GoldenLeakagePopulationBlocked (never returns a partial/filtered list
    for the caller to selectively apply) when
    find_golden_leakage_in_generated_examples() finds ANYTHING at all —
    blocking the ENTIRE batch, never silently dropping just the offending
    collisions and inserting the rest. Returns None (no-op) when the
    collection is clean.
    """
    problems = find_golden_leakage_in_generated_examples(generated_examples, golden_cases)
    if problems:
        raise GoldenLeakagePopulationBlocked(
            f"{len(problems)} golden-leakage problem(s) detected — population blocked entirely:\n"
            + "\n".join(problems)
        )
