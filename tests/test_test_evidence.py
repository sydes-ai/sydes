"""Test evidence independent of route/flow resolution (5-language evaluation).

All five substantial PRs (romm #5014, superset #8052, halo #10329, weave-os/router
#1573, OpenResearch #508) changed tests alongside the implementation, and Sydes
indexed those changed test cases, yet the report said "Test asserting the changed
behavior: ❌ Not found" because no route/flow was recognized to attach them to.
These fixtures encode that structure generically: a changed implementation, a
test that changed with it, no flow at all."""

from __future__ import annotations

from sydes.verify.models import (
    TEST_EVIDENCE_LINKED,
    TEST_EVIDENCE_NONE_IN_SCOPE,
    TEST_EVIDENCE_NOT_SEARCHED,
    TEST_EVIDENCE_RELATED,
    AffectedFlow,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    Hunk,
    MappedTest,
    TestAssessment,
    VerificationObligation,
)
from sydes.verify.pr_semantic_analysis import parse_semantic_analysis
from sydes.verify.related_tests import (
    build_test_evidence,
    find_related_tests,
    link_runtime_tests,
    prompt_candidates,
)
from sydes.verify.test_index import ExistingTestIndex, LocatedTest


def _case(file: str, name: str, line: int, end: int, body: str, idents: set[str]) -> LocatedTest:
    return LocatedTest(repo="app", file=file, name=name, line=line, end_line=end,
                       body=body, identifiers=idents)


def _change(test_hunk: tuple[int, int] | None = (10, 20)) -> ChangeSet:
    files = [ChangedFile(repo="app", path="app/scan.py", hunks=[Hunk(start_line=1, end_line=40)])]
    if test_hunk:
        files.append(ChangedFile(repo="app", path="tests/test_tasks.py",
                                 hunks=[Hunk(start_line=test_hunk[0], end_line=test_hunk[1])]))
    return ChangeSet(base="b", files=files, symbols=[
        ChangedSymbol(id="1", repo="app", file="app/scan.py", name="queue_scan"),
        ChangedSymbol(id="2", repo="app", file="app/scan.py", name="update"),  # generic: never a reference
    ])


def _index() -> ExistingTestIndex:
    return ExistingTestIndex(repo="app", files=["tests/test_tasks.py", "tests/test_other.py"], cases=[
        _case("tests/test_tasks.py", "test_scan_conflict", 10, 18, "r = post('/scan'); assert r.status_code == 409",
              {"post", "status_code"}),
        _case("tests/test_tasks.py", "test_unrelated_old", 30, 35, "assert 1", set()),
        _case("tests/test_other.py", "test_queue_scan_directly", 1, 6, "queue_scan(payload); assert job",
              {"queue_scan", "payload"}),
        _case("tests/test_other.py", "test_update_profile", 8, 12, "update(x); assert x", {"update", "x"}),
    ])


def test_a_test_changed_with_the_code_is_related_even_with_no_flow() -> None:
    related = find_related_tests(_index(), _change())
    by_id = {t.id: t for t in related}
    assert by_id["tests/test_tasks.py::test_scan_conflict"].relation == "changed_in_diff"
    assert by_id["tests/test_other.py::test_queue_scan_directly"].references == ["queue_scan"]
    # an untouched case in a touched file is not "changed"; a generic name proves nothing
    assert "tests/test_tasks.py::test_unrelated_old" not in by_id
    assert "tests/test_other.py::test_update_profile" not in by_id

    evidence = build_test_evidence(index=_index(), related=related, flows=[])
    assert evidence.status == TEST_EVIDENCE_RELATED  # found, not linked -- never "none"
    assert evidence.searched_test_files == 2


def test_status_is_none_in_scope_only_when_searched_and_nothing_relates() -> None:
    change = _change(test_hunk=None)
    index = ExistingTestIndex(repo="app", files=["tests/test_x.py"], cases=[
        _case("tests/test_x.py", "test_a", 1, 3, "assert True", set()),
    ])
    evidence = build_test_evidence(index=index, related=find_related_tests(index, change), flows=[])
    assert evidence.status == TEST_EVIDENCE_NONE_IN_SCOPE


def test_no_test_files_is_not_searched_rather_than_none_found() -> None:
    evidence = build_test_evidence(index=ExistingTestIndex(repo="app"), related=[], flows=[])
    assert evidence.status == TEST_EVIDENCE_NOT_SEARCHED


def test_structural_and_runtime_links_raise_the_status() -> None:
    related = find_related_tests(_index(), _change())
    flow = AffectedFlow(id="f", entry_label="POST /scan", obligations=[VerificationObligation(
        id="o", flow_id="f", kind="validation", statement="s", origin="o", mapped_tests=[MappedTest(
            id="m", name="test_scan_conflict", file="tests/test_tasks.py")])])
    evidence = build_test_evidence(index=_index(), related=related, flows=[flow])
    assert evidence.status == TEST_EVIDENCE_LINKED
    assert {t.id: t.linked for t in evidence.tests}["tests/test_tasks.py::test_scan_conflict"] == "structural"

    unlinked = build_test_evidence(index=_index(), related=related, flows=[])
    link_runtime_tests(unlinked, ["tests/test_other.py::TestQueue::test_queue_scan_directly[a]"])
    assert unlinked.status == TEST_EVIDENCE_LINKED
    assert {t.id: t.linked for t in unlinked.tests}["tests/test_other.py::test_queue_scan_directly"] == "runtime"


def test_candidates_carry_the_test_text_for_assessment() -> None:
    candidates = prompt_candidates(_index(), find_related_tests(_index(), _change()))
    assert candidates[0]["id"] == "tests/test_tasks.py::test_scan_conflict"
    assert "409" in candidates[0]["text"]


def test_semantic_assessments_are_parsed_and_bounded_to_known_values() -> None:
    analysis = parse_semantic_analysis({"test_assessments": [
        {"test": "t.py::a", "targets": "targets_change", "assertion": "weak", "reason": "substring only"},
        {"test": "t.py::b", "targets": "maybe", "assertion": "great"},
        {"targets": "targets_change"},
    ]})
    assert analysis.test_assessments == [
        TestAssessment(test="t.py::a", targets="targets_change", assertion="weak", reason="substring only"),
        TestAssessment(test="t.py::b", targets="unclear", assertion="unclear", reason=""),
    ]
