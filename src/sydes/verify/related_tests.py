"""Test evidence for a change, independent of route/flow resolution.

The structural test mapping (`sydes.verify.test_mapping`) only links a test to
an obligation of an `AffectedFlow`, and a flow exists only when an entry point
was recognized. When it was not (a custom route decorator, a functional router,
a CLI), the tests that visibly changed alongside the implementation used to
disappear from the result and the report concluded "no test". This module
keeps what Sydes did establish, at the strength it established it:

- tests the diff itself changed (the test case's own lines are in a hunk);
- unchanged tests whose body names a changed, discriminating symbol;
- which of those structural mapping or runtime evidence linked to the change;
- the semantic pass's read of whether each targets the change and how strongly
  it asserts it (a model judgment, recorded as such).

`status` is the strongest claim the evidence supports; a weaker one never
reads as "not found".
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sydes.verify.models import (
    TEST_EVIDENCE_LINKED,
    TEST_EVIDENCE_NONE_IN_SCOPE,
    TEST_EVIDENCE_NOT_SEARCHED,
    TEST_EVIDENCE_RELATED,
    TEST_RELATION_CHANGED,
    TEST_RELATION_REFERENCES,
    AffectedFlow,
    ChangeSet,
    RelatedTest,
    TestAssessment,
    TestEvidence,
)
from sydes.verify.test_index import ExistingTestIndex, LocatedTest
from sydes.verify.test_mapping import case_changed_in_diff

MAX_CHANGED_TESTS = 40
MAX_REFERENCING_TESTS = 20
#: A changed symbol named by more test cases than this is too common to say
#: anything about one test in particular (a helper used everywhere).
MAX_TESTS_PER_SYMBOL = 25
MIN_SYMBOL_CHARS = 4
MAX_PROMPT_TESTS = 20
MAX_SNIPPET_CHARS = 700

# Names that say nothing about which behavior a test is about.
_GENERIC_NAMES = {
    "create", "update", "delete", "get", "set", "list", "find", "save", "add", "remove",
    "init", "__init__", "main", "run", "start", "stop", "close", "open", "read", "write",
    "handle", "handler", "process", "execute", "call", "apply", "build", "parse", "load",
    "render", "validate", "value", "data", "result", "config", "setup", "test", "new",
}


def case_id(case: LocatedTest) -> str:
    return f"{case.file}::{case.name}"


def _short(name: str) -> str:
    """`pkg.Class.method` / `Class::method` -> `method`."""
    return re.split(r"[.:#]+", name)[-1]


def _changed_source_names(change: ChangeSet, test_files: set[str]) -> set[str]:
    names: set[str] = set()
    for symbol in change.symbols:
        if symbol.file in test_files:
            continue
        for raw in (symbol.name, symbol.qualified_name):
            if not raw:
                continue
            short = _short(raw)
            if len(short) >= MIN_SYMBOL_CHARS and short.lower() not in _GENERIC_NAMES:
                names.add(short)
    return names


def _references(case: LocatedTest, names: set[str]) -> list[str]:
    found = {name for name in names if name in case.identifiers}
    if not found and case.body:
        found = {name for name in names if re.search(rf"\b{re.escape(name)}\b", case.body)}
    return sorted(found)


def find_related_tests(index: ExistingTestIndex, change: ChangeSet) -> list[RelatedTest]:
    """Changed test cases first, then unchanged tests naming a discriminating
    changed symbol (ranked by how many, then how rare). Bounded."""
    test_files = set(index.files)
    hunks = {item.path: item.hunks for item in change.files}
    changed_files = set(hunks)
    names = _changed_source_names(change, test_files)

    usage: Counter[str] = Counter()
    refs_by_case: dict[int, list[str]] = {}
    for position, case in enumerate(index.cases):
        refs = _references(case, names)
        refs_by_case[position] = refs
        usage.update(refs)
    discriminating = {name for name, count in usage.items() if count <= MAX_TESTS_PER_SYMBOL}

    changed: list[RelatedTest] = []
    referencing: list[tuple[tuple[int, int], RelatedTest]] = []
    for position, case in enumerate(index.cases):
        refs = refs_by_case[position]
        if case.file in changed_files and case_changed_in_diff(case, hunks.get(case.file)):
            changed.append(RelatedTest(
                id=case_id(case), file=case.file, name=case.name, line=case.line,
                relation=TEST_RELATION_CHANGED, references=refs,
            ))
            continue
        strong = [r for r in refs if r in discriminating]
        if strong:
            rank = (-len(strong), min(usage[r] for r in strong))
            referencing.append((rank, RelatedTest(
                id=case_id(case), file=case.file, name=case.name, line=case.line,
                relation=TEST_RELATION_REFERENCES, references=strong,
            )))
    referencing.sort(key=lambda item: (item[0], item[1].id))
    return changed[:MAX_CHANGED_TESTS] + [t for _, t in referencing[:MAX_REFERENCING_TESTS]]


def prompt_candidates(index: ExistingTestIndex, related: list[RelatedTest]) -> list[dict[str, Any]]:
    """What the semantic pass is shown: id, how Sydes found it, and the test's
    own text (bounded), so it can judge target and assertion strength."""
    by_id = {case_id(case): case for case in index.cases}
    out: list[dict[str, Any]] = []
    for test in related[:MAX_PROMPT_TESTS]:
        case = by_id.get(test.id)
        body = (case.body if case else "") or ""
        out.append({
            "id": test.id,
            "relation": test.relation,
            "names_changed_symbols": test.references,
            "text": body[:MAX_SNIPPET_CHARS],
        })
    return out


def _structurally_mapped(flows: list[AffectedFlow]) -> dict[str, tuple[str, int | None]]:
    """`file::name` of every test mapped to an obligation -> (file, line)."""
    mapped: dict[str, tuple[str, int | None]] = {}
    for flow in flows:
        for obligation in flow.obligations:
            for test in obligation.mapped_tests:
                name = test.case_name or test.name
                if test.file and name:
                    mapped[f"{test.file}::{name}"] = (test.file, test.line)
    return mapped


def build_test_evidence(
    *,
    index: ExistingTestIndex | None,
    related: list[RelatedTest],
    flows: list[AffectedFlow],
    assessments: list[TestAssessment] | None = None,
) -> TestEvidence:
    if index is None or not index.files:
        return TestEvidence(
            status=TEST_EVIDENCE_NOT_SEARCHED,
            notes=["No test files were recognized in this repository, so no test was searched."],
        )
    tests = [test.model_copy() for test in related]
    by_id = {test.id: test for test in tests}
    for test_key, (file, line) in _structurally_mapped(flows).items():
        if test_key in by_id:
            by_id[test_key].linked = "structural"
        else:
            name = test_key.split("::", 1)[1]
            linked = RelatedTest(
                id=test_key, file=file, name=name, line=line,
                relation=TEST_RELATION_REFERENCES, linked="structural",
            )
            tests.append(linked)
            by_id[test_key] = linked
    evidence = TestEvidence(
        searched_test_files=len(index.files),
        searched_test_cases=len(index.cases),
        tests=tests,
        assessments=list(assessments or []),
    )
    evidence.status = _status(evidence)
    return evidence


def _status(evidence: TestEvidence) -> str:
    if any(test.linked for test in evidence.tests):
        return TEST_EVIDENCE_LINKED
    if evidence.tests:
        return TEST_EVIDENCE_RELATED
    return TEST_EVIDENCE_NONE_IN_SCOPE


def link_runtime_tests(evidence: TestEvidence | None, runtime_test_ids: list[str]) -> None:
    """Mark tests runtime evidence observed running the changed code. Runtime
    ids may carry a class (`file::Class::test`); match on file and test name."""
    if evidence is None or not runtime_test_ids:
        return
    for runtime_id in runtime_test_ids:
        file, _, rest = runtime_id.partition("::")
        name = rest.rsplit("::", 1)[-1].split("[", 1)[0]
        match = next(
            (t for t in evidence.tests if t.file == file and t.name.split("[", 1)[0] == name),
            None,
        )
        if match is not None:
            match.linked = match.linked or "runtime"
        else:
            evidence.tests.append(RelatedTest(
                id=f"{file}::{name}", file=file, name=name,
                relation=TEST_RELATION_REFERENCES, linked="runtime",
            ))
    evidence.status = _status(evidence)
