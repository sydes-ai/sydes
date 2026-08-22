"""Guards against relying on Python APIs newer than Sydes' declared floor.

`pyproject.toml` declares `requires-python = ">=3.11"`, but `pathlib.Path.walk()`
only exists from 3.12. Every repository walker must therefore use `os.walk`.

These tests delete `Path.walk` before exercising each walker, so a 3.12-only
dependency fails loudly on *any* interpreter rather than only under 3.11.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

import pytest

from sydes.core.models import RepoRef
from sydes.discover.discovery_cache import collect_repo_file_snapshot
from sydes.discover.repo_map import build_repo_map
from sydes.discover.route_index import build_route_index
from sydes.ingest.inventory import build_repo_inventory
from sydes.trace.handler_symbols.index import build_handler_symbol_index

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sydes"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small repository with a pruned directory and a nested source file."""
    root = tmp_path / "svc"
    (root / "app").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "app" / "routes.py").write_text(
        'from framework import App\n\napp = App()\n\n\n@app.get("/health")\ndef health():\n    return {}\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text('{"name": "svc"}', encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    return root


@pytest.fixture()
def without_path_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Python 3.11, where `Path.walk` does not exist."""
    monkeypatch.delattr(Path, "walk", raising=False)


def test_declared_python_floor_predates_path_walk() -> None:
    """The guard only matters while Sydes supports a Python without `Path.walk`."""
    manifest = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    requires = manifest["project"]["requires-python"]

    assert requires == ">=3.11", (
        "Sydes declares support for a Python older than 3.12, so `Path.walk` "
        f"must not be used anywhere (requires-python={requires!r})."
    )


def test_no_source_file_calls_path_walk() -> None:
    """No module may call `.walk()` on a path object."""
    offenders = [
        f"{path.relative_to(_SOURCE_ROOT)}:{index}"
        for path in _SOURCE_ROOT.rglob("*.py")
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\b(?:root|path|dirpath|base|directory)\w*\.walk\s*\(", line)
    ]

    assert offenders == [], f"`Path.walk()` is Python 3.12+; found at {offenders}"


def test_repo_map_walks_without_path_walk(repo: Path, without_path_walk: None) -> None:
    """Repo mapping works on 3.11 and still prunes ignored directories."""
    payload = build_repo_map(RepoRef(name="svc", root=str(repo)))

    paths = {item["path"] for item in payload["files"]}
    assert "package.json" in paths
    assert not any("node_modules" in item for item in paths)
    assert payload["summary"]["total_files_seen"] >= 2


def test_inventory_walks_without_path_walk(repo: Path, without_path_walk: None) -> None:
    """File inventory works on 3.11 and returns repo-relative paths."""
    inventory = build_repo_inventory("svc", repo, include_sizes=False)

    paths = {item.path for item in inventory.files}
    assert "app/routes.py" in paths
    assert not any(item.startswith("/") for item in paths)
    assert not any("node_modules" in item for item in paths)
    assert inventory.file_count == len(inventory.files)


def test_route_index_walks_without_path_walk(repo: Path, without_path_walk: None) -> None:
    """Route indexing works on 3.11 and keeps relative file paths."""
    index = build_route_index(RepoRef(name="svc", root=str(repo)))

    paths = {item["path"] for item in index["files"]}
    assert "app/routes.py" in paths
    assert not any("node_modules" in item for item in paths)


def test_handler_symbol_index_walks_without_path_walk(
    repo: Path, without_path_walk: None
) -> None:
    """Handler symbol indexing works on 3.11."""
    index = build_handler_symbol_index(RepoRef(name="svc", root=str(repo)))

    paths = {item["path"] for item in index["files"]}
    assert "app/routes.py" in paths
    assert not any("node_modules" in item for item in paths)


def test_discovery_cache_snapshot_walks_without_path_walk(
    repo: Path, without_path_walk: None
) -> None:
    """The discovery cache snapshot works on 3.11 and prunes ignored directories."""
    snapshot = collect_repo_file_snapshot([RepoRef(name="svc", root=str(repo))])

    relative_paths = {entry["relative_path"] for entry in snapshot.values()}
    assert "app/routes.py" in relative_paths
    assert not any("node_modules" in item for item in relative_paths)


def test_pruning_matches_between_walkers(repo: Path, without_path_walk: None) -> None:
    """Ignored-directory pruning is unchanged by the `os.walk` substitution."""
    inventory = build_repo_inventory("svc", repo, include_sizes=False)
    snapshot = collect_repo_file_snapshot([RepoRef(name="svc", root=str(repo))])

    relative_paths = {entry["relative_path"] for entry in snapshot.values()}
    assert "app/routes.py" in {item.path for item in inventory.files}
    assert "app/routes.py" in relative_paths
