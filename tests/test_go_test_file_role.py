"""Test code never masquerades as a changed production symbol.

Go's `*_test.go` (and every other language's test-file convention) is recognised by the one
shared predicate. Changed test functions do not enter production impact propagation (so they
are never unresolved production symbols and never AI-recovery targets) and do not seed
code-review related context; they stay available wherever tests are consumed on purpose.
"""

from __future__ import annotations

from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    classify_candidate_file_role,
    is_test_path,
)
from sydes.recovery.context import _changed_symbol_entities, _changed_symbols
from sydes.verify.analyzer import _changed_symbols_for_impact
from sydes.verify.models import ChangedFile, ChangedSymbol, ChangeSet, ChangeVerificationResult


def test_go_production_and_test_files_are_told_apart() -> None:
    assert not is_test_path("token/maker.go")
    assert classify_candidate_file_role("token/maker.go") == FILE_ROLE_SOURCE_ROUTE_CANDIDATE
    assert is_test_path("token/jwt_maker_test.go")
    assert classify_candidate_file_role("token/jwt_maker_test.go") == FILE_ROLE_TEST_USAGE_CANDIDATE
    # the same predicate covers the other conventions the duplicated regexes used to
    for path in ("api/tests/test_x.py", "src/a.spec.ts", "src/test/java/FooTest.java", "app/FooTest.java"):
        assert is_test_path(path)
    assert not is_test_path("api/main.py")


def _change() -> ChangeSet:
    return ChangeSet(base="main", files=[
        ChangedFile(repo="app", path="token/payload.go"),
        ChangedFile(repo="app", path="token/jwt_maker_test.go"),
    ], symbols=[
        ChangedSymbol(id="1", repo="app", file="token/payload.go", name="Valid", start_line=53),
        ChangedSymbol(id="2", repo="app", file="token/jwt_maker_test.go", name="TestJWTWrongTokenType", start_line=70),
    ])


def test_changed_test_functions_are_not_production_symbols_for_impact() -> None:
    assert [s["name"] for s in _changed_symbols_for_impact(_change())] == ["Valid"]


def test_changed_test_functions_are_not_ai_recovery_targets_but_remain_visible() -> None:
    result = ChangeVerificationResult(change=_change())
    assert [e.symbol for e in _changed_symbol_entities(result)] == ["Valid"]
    # the descriptive change list keeps tests: test recovery and the reader still see them
    assert any("TestJWTWrongTokenType" in line for line in _changed_symbols(result))
