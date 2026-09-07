"""Tests for Go standard-library `testing` support: case-name recognition
(`verify.test_index`) and runner detection (`verify.test_execution`).

Before this, a `_test.go` file counted as evidence only by its filename
stem; the real `func TestXxx(t *testing.T)` name was invisible, and
`test_frameworks_detected` was always `none` for a Go repository regardless
of what was actually configured.
"""

from __future__ import annotations

from pathlib import Path

from sydes.verify.source_files import RepoFiles, SourceFile
from sydes.verify.test_execution import FRAMEWORK_GO_TEST, detect_frameworks
from sydes.verify.test_index import _extract_cases_from_file, build_test_index

_TRANSFER_TEST_GO = '''package api

import "testing"

func TestTransferAPI(t *testing.T) {
\tt.Run("OK", func(t *testing.T) {})
}
'''

_HELPER_ONLY_GO = '''package api

func newTestServer() *Server {
\treturn nil
}
'''


def _go_file(path: str, text: str, *, role: str = "test_usage_candidate") -> SourceFile:
    return SourceFile(repo="app", path=path, text=text, role=role, extension=".go")


# --------------------------------------------------------------------------
# Case-name recognition (verify.test_index)
# --------------------------------------------------------------------------


def test_a_single_go_test_function_is_recognized_by_its_real_name() -> None:
    cases = _extract_cases_from_file(_go_file("api/transfer_test.go", _TRANSFER_TEST_GO))
    assert [c.name for c in cases] == ["TestTransferAPI"]
    assert cases[0].line == 5


def test_multiple_go_test_functions_in_one_file_are_all_found() -> None:
    text = (
        "package api\n\n"
        "func TestCreateUser(t *testing.T) {}\n\n"
        "func TestDeleteUser(t *testing.T) {}\n"
    )
    cases = _extract_cases_from_file(_go_file("api/user_test.go", text))
    assert [c.name for c in cases] == ["TestCreateUser", "TestDeleteUser"]


def test_a_non_test_go_function_is_not_treated_as_a_case() -> None:
    """A `_test.go` file with only helper functions (no `TestXxx`) still
    falls back to the whole-file pseudo-case — it must not silently
    disappear, and it must not be mistaken for a real named case either."""
    cases = _extract_cases_from_file(_go_file("api/main_test.go", _HELPER_ONLY_GO))
    assert len(cases) == 1
    assert cases[0].name == "main_test"  # the fallback, not a real Go test name


def test_lowercase_after_test_prefix_is_not_a_go_test_function() -> None:
    """`func Testify(...)` is not a real Go test per Go's own naming rule
    (the character after `Test` must not be lowercase) — it must not be
    mistaken for one, and the file falls back correctly instead."""
    text = "package api\n\nfunc Testify(x int) int {\n\treturn x\n}\n"
    cases = _extract_cases_from_file(_go_file("api/helpers_test.go", text))
    assert len(cases) == 1
    assert cases[0].name == "helpers_test"  # fallback: no real TestXxx found


def test_go_test_function_signature_variants_do_not_need_the_literal_t_name() -> None:
    text = "package api\n\nfunc TestWithRenamedParam(tt *testing.T) {}\n"
    cases = _extract_cases_from_file(_go_file("x_test.go", text))
    assert [c.name for c in cases] == ["TestWithRenamedParam"]


def test_build_test_index_reports_the_real_go_case_name_not_the_file_stem() -> None:
    files = RepoFiles(repo="app", root=Path("/repo"))
    files.files.append(_go_file("api/transfer_test.go", _TRANSFER_TEST_GO))
    index = build_test_index(files)
    assert [c.name for c in index.cases] == ["TestTransferAPI"]
    assert "test_files_found=1" in index.notes
    assert "test_cases_found=1" in index.notes


def test_python_case_recognition_is_unaffected_by_the_go_pattern() -> None:
    text = "def test_create_user():\n    assert True\n"
    scanned = SourceFile(repo="app", path="test_x.py", text=text, role="test_usage_candidate", extension=".py")
    cases = _extract_cases_from_file(scanned)
    assert [c.name for c in cases] == ["test_create_user"]


def test_js_case_recognition_is_unaffected_by_the_go_pattern() -> None:
    text = "it('creates a user', () => {\n  expect(true).toBe(true);\n});\n"
    scanned = SourceFile(repo="app", path="x.test.js", text=text, role="test_usage_candidate", extension=".js")
    cases = _extract_cases_from_file(scanned)
    assert [c.name for c in cases] == ["creates a user"]


def test_java_case_recognition_is_unaffected_by_the_go_pattern() -> None:
    text = "public void testCreatesUser() {\n    assertTrue(true);\n}\n"
    scanned = SourceFile(repo="app", path="XTest.java", text=text, role="test_usage_candidate", extension=".java")
    cases = _extract_cases_from_file(scanned)
    assert [c.name for c in cases] == ["testCreatesUser"]


# --------------------------------------------------------------------------
# Runner detection (verify.test_execution)
# --------------------------------------------------------------------------


def _repo_files_with(*paths_and_text: tuple[str, str]) -> RepoFiles:
    files = RepoFiles(repo="app", root=Path("/repo"))
    for path, text in paths_and_text:
        files.files.append(
            SourceFile(
                repo="app", path=path, text=text,
                role="test_usage_candidate" if "_test.go" in path else "source_route_candidate",
                extension=Path(path).suffix.lower(),
            )
        )
    return files


def test_go_mod_with_test_files_is_detected_as_go_test() -> None:
    files = _repo_files_with(
        ("go.mod", "module example.com/app\n\ngo 1.22\n"),
        ("api/transfer_test.go", _TRANSFER_TEST_GO),
    )
    detections = detect_frameworks(files)
    go_detections = [d for d in detections if d.framework == FRAMEWORK_GO_TEST]
    assert len(go_detections) == 1
    assert go_detections[0].language == "go"
    assert go_detections[0].runner_argv == ["go", "test"]


def test_go_mod_without_any_test_file_detects_nothing() -> None:
    files = _repo_files_with(("go.mod", "module example.com/app\n\ngo 1.22\n"))
    assert not [d for d in detect_frameworks(files) if d.framework == FRAMEWORK_GO_TEST]


def test_no_go_mod_at_all_detects_nothing_even_with_go_test_files() -> None:
    """A `.go` file alone (e.g. a vendored dependency) proves nothing about
    this repository's own test setup without a `go.mod` to anchor it."""
    files = _repo_files_with(("api/transfer_test.go", _TRANSFER_TEST_GO))
    assert not [d for d in detect_frameworks(files) if d.framework == FRAMEWORK_GO_TEST]


def test_existing_python_detection_is_unaffected_by_go_support() -> None:
    files = _repo_files_with(
        ("pyproject.toml", "[tool.pytest.ini_options]\n"),
    )
    detections = detect_frameworks(files)
    assert any(d.framework == "pytest" for d in detections)
    assert not [d for d in detections if d.framework == FRAMEWORK_GO_TEST]


def test_a_repo_with_both_go_and_node_reports_both() -> None:
    files = _repo_files_with(
        ("go.mod", "module example.com/app\n\ngo 1.22\n"),
        ("api/transfer_test.go", _TRANSFER_TEST_GO),
        ("frontend/package.json", '{"devDependencies": {"jest": "29.0.0"}}'),
    )
    frameworks = {d.framework for d in detect_frameworks(files)}
    assert FRAMEWORK_GO_TEST in frameworks
    assert "jest" in frameworks
