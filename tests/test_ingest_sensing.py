"""Tests for shallow repo inventory and sensing helpers."""

from pathlib import Path

import pytest

from sydes.core.models import RepoRef
from sydes.ingest.inventory import build_repo_inventory
from sydes.ingest.repos import summarize_repo_roots, validate_repo_roots
from sydes.ingest.sense import sense_repo


def test_build_repo_inventory_skips_junk_and_collects_relative_paths(tmp_path: Path) -> None:
    """Inventory should collect regular files and skip known junk directories."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("x\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x\n", encoding="utf-8")

    inventory = build_repo_inventory("api", tmp_path)

    assert inventory.repo == "api"
    paths = {item.path for item in inventory.files}
    assert "src/app.py" in paths
    assert "node_modules/lib.js" not in paths
    assert ".git/config" not in paths


def test_validate_repo_roots_rejects_missing_root(tmp_path: Path) -> None:
    """Repo validation should fail when a configured root does not exist."""
    repos = [RepoRef(name="api", root=str(tmp_path / "missing"))]
    with pytest.raises(ValueError, match="does not exist"):
        validate_repo_roots(repos)


def test_summarize_repo_roots_returns_normalized_paths(tmp_path: Path) -> None:
    """Repo root summaries should include validated absolute root paths."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    summaries = summarize_repo_roots([RepoRef(name="api", root=str(repo_root))])

    assert len(summaries) == 1
    assert summaries[0].startswith("api: ")
    assert str(repo_root.resolve()) in summaries[0]


def test_sense_repo_captures_shallow_signals(tmp_path: Path) -> None:
    """Sense summary should include manifests, language hints, and backend signals."""
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "routes.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    inventory = build_repo_inventory("api", tmp_path)
    summary = sense_repo("api", str(tmp_path), inventory)

    assert "pyproject.toml" in summary.manifests
    assert "python" in summary.likely_language_families
    assert ".py" in summary.dominant_extensions
    assert "api" in summary.backend_signals
