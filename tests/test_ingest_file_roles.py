"""Tests for lightweight discovery candidate file-role classification."""

from sydes.ingest.file_roles import (
    FILE_ROLE_DOCS_CANDIDATE,
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    FILE_ROLE_UNKNOWN,
    classify_candidate_file_role,
)


def test_classify_source_route_candidate_paths() -> None:
    assert classify_candidate_file_role("app/routes.py") == FILE_ROLE_SOURCE_ROUTE_CANDIDATE
    assert classify_candidate_file_role("main.py") == FILE_ROLE_SOURCE_ROUTE_CANDIDATE
    assert classify_candidate_file_role("src/routes/users.ts") == FILE_ROLE_SOURCE_ROUTE_CANDIDATE


def test_classify_test_usage_candidate_paths() -> None:
    assert classify_candidate_file_role("tests/test_app.py") == FILE_ROLE_TEST_USAGE_CANDIDATE
    assert classify_candidate_file_role("test/test_routes.py") == FILE_ROLE_TEST_USAGE_CANDIDATE
    assert classify_candidate_file_role("src/users.test.ts") == FILE_ROLE_TEST_USAGE_CANDIDATE
    assert classify_candidate_file_role("src/users.spec.ts") == FILE_ROLE_TEST_USAGE_CANDIDATE


def test_classify_docs_candidate_paths() -> None:
    assert classify_candidate_file_role("README.md") == FILE_ROLE_DOCS_CANDIDATE
    assert classify_candidate_file_role("docs/api.md") == FILE_ROLE_DOCS_CANDIDATE


def test_classify_unknown_when_extension_not_supported() -> None:
    assert classify_candidate_file_role("data/schema.sql") == FILE_ROLE_UNKNOWN


def test_classify_is_case_and_separator_robust() -> None:
    assert classify_candidate_file_role("Tests\\Test_App.PY") == FILE_ROLE_TEST_USAGE_CANDIDATE
    assert classify_candidate_file_role("DOCS\\API.RST") == FILE_ROLE_DOCS_CANDIDATE


def test_go_test_go_basename_is_a_test_candidate_with_no_directory_needed() -> None:
    """Go's own toolchain convention: `go test` only considers a file whose
    basename ends in `_test.go` — this must classify as test regardless of
    directory, matching the exact real-PR shape `selfservice/flow/logout/
    handler_test.go` (no `tests/`-named directory anywhere in the path)."""
    assert (
        classify_candidate_file_role("selfservice/flow/logout/handler_test.go")
        == FILE_ROLE_TEST_USAGE_CANDIDATE
    )
    assert classify_candidate_file_role("handler_test.go") == FILE_ROLE_TEST_USAGE_CANDIDATE


def test_go_production_file_beside_a_test_file_is_still_source() -> None:
    assert (
        classify_candidate_file_role("selfservice/flow/logout/handler.go")
        == FILE_ROLE_SOURCE_ROUTE_CANDIDATE
    )


def test_rust_source_files_are_source_route_candidates() -> None:
    assert (
        classify_candidate_file_role("examples/pastebin/src/paste_id.rs")
        == FILE_ROLE_SOURCE_ROUTE_CANDIDATE
    )
    assert classify_candidate_file_role("src/main.rs") == FILE_ROLE_SOURCE_ROUTE_CANDIDATE


def test_rust_tests_rs_basename_is_a_test_candidate() -> None:
    """`tests.rs`/`test.rs` is the common convention when a `mod tests;`
    module (Rust's own dominant style is an inline `#[cfg(test)] mod tests`,
    invisible to a path-only classifier) is split into its own file."""
    assert (
        classify_candidate_file_role("examples/pastebin/src/tests.rs")
        == FILE_ROLE_TEST_USAGE_CANDIDATE
    )
    assert classify_candidate_file_role("src/test.rs") == FILE_ROLE_TEST_USAGE_CANDIDATE


def test_rust_integration_test_under_tests_dir_is_a_test_candidate() -> None:
    """Cargo's own convention: any `.rs` file directly under a crate's
    `tests/` directory is compiled as a separate integration-test binary —
    already covered by the generic `TEST_DIR_MARKERS` check, no Rust-specific
    rule needed."""
    assert classify_candidate_file_role("tests/api.rs") == FILE_ROLE_TEST_USAGE_CANDIDATE
