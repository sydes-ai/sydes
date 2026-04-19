"""Tests for repository spec parsing helpers."""

import pytest

from sydes.ingest.repos import parse_repo_spec


def test_parse_repo_spec_valid() -> None:
    """Valid repo specs should map to RepoRef fields."""
    repo = parse_repo_spec("gateway=./gateway")

    assert repo.name == "gateway"
    assert repo.root == "./gateway"


def test_parse_repo_spec_rejects_missing_separator() -> None:
    """Repo specs without '=' should fail with a clear error."""
    with pytest.raises(ValueError, match="Expected format: name=path"):
        parse_repo_spec("gateway")
