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
