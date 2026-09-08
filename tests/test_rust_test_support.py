"""Tests for Rust `#[test]` case-name recognition (`verify.test_index`).

Before this, a `.rs` file was invisible to Sydes entirely (`.rs` was absent
from every extension gate a Rust file must pass through: `file_roles.py`'s
role classification, `source_files.py`'s own separate source-extension set,
and `route_index.py`'s), so `test_files_found`/`test_cases_found` were
always `0` for a Rust repository regardless of what tests actually existed.
Rust's own rule is unlike every other language already supported here: a
case is named by an attribute (`#[test]`, or an async-runtime variant like
`#[tokio::test]`) on the line above `fn`, never by a naming convention on
the function name itself — and that attribute is commonly stacked with
others (`#[should_panic]`, `#[ignore]`), which must not cancel the pending
match before the `fn` line it actually applies to.
"""

from __future__ import annotations

from pathlib import Path

from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    classify_candidate_file_role,
)
from sydes.verify.source_files import RepoFiles, SourceFile
from sydes.verify.test_index import _extract_cases_from_file, build_test_index


def _rust_file(path: str, text: str, *, role: str = FILE_ROLE_TEST_USAGE_CANDIDATE) -> SourceFile:
    return SourceFile(repo="app", path=path, text=text, role=role, extension=".rs")


def test_a_single_rust_test_function_is_recognized_by_its_attribute() -> None:
    text = "#[test]\nfn rejects_zero_size() {\n    assert!(true);\n}\n"
    cases = _extract_cases_from_file(_rust_file("src/tests.rs", text))
    assert [c.name for c in cases] == ["rejects_zero_size"]
    assert cases[0].line == 2


def test_multiple_rust_test_functions_in_one_file_are_all_found() -> None:
    text = (
        "#[test]\nfn creates_user() {}\n\n"
        "#[test]\nfn deletes_user() {}\n"
    )
    cases = _extract_cases_from_file(_rust_file("src/tests.rs", text))
    assert [c.name for c in cases] == ["creates_user", "deletes_user"]


def test_a_stacked_attribute_does_not_cancel_the_pending_test_match() -> None:
    """The real shape from RS-S-01: `#[test]` followed by `#[should_panic]`
    before the `fn` line — the second attribute must not reset the state
    that `#[test]` set."""
    text = (
        '#[test]\n#[should_panic(expected = "must be greater than 0")]\n'
        "fn new_rejects_zero_size() {\n    PasteId::new(0);\n}\n"
    )
    cases = _extract_cases_from_file(_rust_file("src/tests.rs", text))
    assert [c.name for c in cases] == ["new_rejects_zero_size"]
    assert cases[0].line == 3


def test_tokio_test_attribute_variant_is_recognized() -> None:
    text = "#[tokio::test]\nasync fn fetches_page() {}\n"
    cases = _extract_cases_from_file(_rust_file("src/tests.rs", text))
    assert [c.name for c in cases] == ["fetches_page"]


def test_a_plain_function_with_no_test_attribute_is_not_treated_as_a_case() -> None:
    """A helper function beside real tests, and no recognizable case at
    all — still falls back to the whole-file pseudo-case rather than
    silently disappearing."""
    text = "fn helper() -> i32 {\n    42\n}\n"
    cases = _extract_cases_from_file(_rust_file("src/tests.rs", text))
    assert len(cases) == 1
    assert cases[0].name == "tests"  # the fallback: file stem, no real case found


def test_build_test_index_reports_the_real_rust_case_name_not_the_file_stem() -> None:
    files = RepoFiles(repo="app", root=Path("/repo"))
    files.files.append(_rust_file("src/tests.rs", "#[test]\nfn new_rejects_zero_size() {}\n"))
    index = build_test_index(files)
    assert [c.name for c in index.cases] == ["new_rejects_zero_size"]
    assert "test_files_found=1" in index.notes
    assert "test_cases_found=1" in index.notes


def test_go_case_recognition_is_unaffected_by_the_rust_pattern() -> None:
    text = "package api\n\nfunc TestTransferAPI(t *testing.T) {}\n"
    scanned = SourceFile(repo="app", path="x_test.go", text=text, role=FILE_ROLE_TEST_USAGE_CANDIDATE, extension=".go")
    cases = _extract_cases_from_file(scanned)
    assert [c.name for c in cases] == ["TestTransferAPI"]


def test_python_case_recognition_is_unaffected_by_the_rust_pattern() -> None:
    text = "def test_create_user():\n    assert True\n"
    scanned = SourceFile(repo="app", path="test_x.py", text=text, role=FILE_ROLE_TEST_USAGE_CANDIDATE, extension=".py")
    cases = _extract_cases_from_file(scanned)
    assert [c.name for c in cases] == ["test_create_user"]


# --------------------------------------------------------------------------
# File-role classification (ingest.file_roles) — the gate every one of the
# above needs to pass through first.
# --------------------------------------------------------------------------


def test_rust_source_file_role() -> None:
    assert classify_candidate_file_role("src/paste_id.rs") == FILE_ROLE_SOURCE_ROUTE_CANDIDATE


def test_rust_tests_rs_role() -> None:
    assert classify_candidate_file_role("src/tests.rs") == FILE_ROLE_TEST_USAGE_CANDIDATE
