"""Cold-vs-incremental equivalence for the structural index.

One invariant is under test, in eight variations:

    For the same repository state, an incremental build must produce
    semantically equivalent structural facts to a clean cold build.

The evaluated external index passed a single-file reparse but failed this,
losing cross-file call edges and never recovering them on revert. Every
scenario below therefore compares a *fully cold* build of a state against an
*incrementally reached* build of the same state, and asserts the facts match —
not merely that counts agree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sydes.core.models import RepoRef
from sydes.discover.file_facts import (
    STATE_COLD,
    STATE_UPDATED,
    STATE_WARM,
    FileFactStore,
    build_structural_index,
)

_ROUTES = '''from fastapi import APIRouter

import crud
from database import get_db

router = APIRouter(prefix="/students")


@router.post("")
def create_student(payload, db=get_db):
    """Create a student."""
    return crud.create_student(db, payload)


@router.get("/{student_id}")
def get_student(student_id, db=get_db):
    return crud.get_student(db, student_id)
'''

_CRUD = '''def create_student(db, payload):
    student = {"name": payload["name"]}
    db.add(student)
    db.commit()
    db.refresh(student)
    return student


def get_student(db, student_id):
    return db.query(student_id).first()


def archive_student(db, student_id):
    db.delete(student_id)
    db.commit()
    return True
'''

_DATABASE = '''def get_db():
    return _Session()


class _Session:
    def add(self, row):
        return row

    def commit(self):
        return True
'''

_MAIN = '''from fastapi import FastAPI

from routers import students

app = FastAPI()
app.include_router(students.router)
'''


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small service whose route composes through a router prefix."""
    root = tmp_path / "svc"
    _write(root, "main.py", _MAIN)
    _write(root, "database.py", _DATABASE)
    _write(root, "crud.py", _CRUD)
    _write(root, "routers/__init__.py", "")
    _write(root, "routers/students.py", _ROUTES)
    return root


def _repos(root: Path) -> list[RepoRef]:
    return [RepoRef(name="svc", root=str(root))]


def _cold(root: Path, tmp_path: Path, tag: str) -> dict[str, Any]:
    """A build with no prior state whatsoever."""
    store = FileFactStore(tmp_path / f"cold-{tag}")
    index = build_structural_index(_repos(root), store=store)
    assert index.metrics.index_state == STATE_COLD
    return _facts(index)


def _incremental(root: Path, store: FileFactStore) -> tuple[dict[str, Any], Any]:
    index = build_structural_index(_repos(root), store=store)
    return _facts(index), index.metrics


def _facts(index) -> dict[str, Any]:
    """The comparable structural surface, normalized for order.

    Absolute roots are dropped: they are environment, not structure.
    """

    def _routes(payload: dict) -> list[str]:
        rows = []
        for repo in payload.get("repos", []):
            for row in repo.get("composed_routes", []):
                rows.append(f"{row['method']} {row['path']} {row['file']}")
        return sorted(rows)

    def _index_files(payload: dict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for repo in payload.get("repos", []):
            for item in repo.get("files", []):
                out[f"{repo['repo']}:{item['path']}"] = item
        return out

    def _symbols(payload: dict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for repo in payload.get("repos", []):
            for item in repo.get("files", []):
                out[f"{repo['repo']}:{item['path']}"] = {
                    "imports": sorted(
                        json.dumps(entry, sort_keys=True) for entry in item.get("imports", [])
                    ),
                    "exports": sorted(
                        json.dumps(entry, sort_keys=True) for entry in item.get("exports", [])
                    ),
                    "symbols": sorted(
                        json.dumps(entry, sort_keys=True) for entry in item.get("symbols", [])
                    ),
                }
        return out

    return {
        "composed_routes": _routes(index.route_graph_facts),
        "route_graph_summary": index.route_graph_facts.get("summary"),
        "route_index_files": _index_files(index.route_index_batch),
        "route_index_summary": index.route_index_batch.get("summary"),
        "symbol_files": _symbols(index.handler_symbol_batch),
        "symbol_summary": index.handler_symbol_batch.get("summary"),
    }


def _assert_equivalent(cold: dict[str, Any], incremental: dict[str, Any]) -> None:
    """Compare fact-by-fact so a failure names the divergent fact."""
    assert incremental["composed_routes"] == cold["composed_routes"]
    assert incremental["route_graph_summary"] == cold["route_graph_summary"]
    assert set(incremental["route_index_files"]) == set(cold["route_index_files"])
    for key, value in cold["route_index_files"].items():
        assert incremental["route_index_files"][key] == value, f"route index diverged for {key}"
    assert set(incremental["symbol_files"]) == set(cold["symbol_files"])
    for key, value in cold["symbol_files"].items():
        assert incremental["symbol_files"][key] == value, f"symbols diverged for {key}"
    assert incremental["symbol_summary"] == cold["symbol_summary"]
    assert incremental["route_index_summary"] == cold["route_index_summary"]


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def test_unchanged_repository_reuses_every_fact(repo: Path, tmp_path: Path) -> None:
    """A no-op rebuild is warm, reuses everything, and changes nothing."""
    store = FileFactStore(tmp_path / "store")
    first, _ = _incremental(repo, store)
    second, metrics = _incremental(repo, store)

    assert metrics.index_state == STATE_WARM
    assert metrics.files_reparsed == 0
    assert metrics.files_reused > 0
    _assert_equivalent(first, second)


# --------------------------------------------------------------------------
# Scenario 1-8
# --------------------------------------------------------------------------


def test_1_modified_handler_body(repo: Path, tmp_path: Path) -> None:
    """A route handler body changes."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    _write(
        repo,
        "routers/students.py",
        _ROUTES.replace(
            '    """Create a student."""',
            '    """Create a student."""\n    if not payload.get("name", "").strip():\n        raise ValueError("blank")',
        ),
    )
    incremental, metrics = _incremental(repo, store)

    assert metrics.index_state == STATE_UPDATED
    assert metrics.files_modified == 1
    _assert_equivalent(_cold(repo, tmp_path, "1"), incremental)


def test_2_added_module_with_routes(repo: Path, tmp_path: Path) -> None:
    """A new router module appears and must be composed."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    _write(
        repo,
        "routers/grades.py",
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/grades")\n\n\n'
        '@router.post("")\ndef create_grade(payload):\n    return payload\n',
    )
    _write(repo, "main.py", _MAIN.replace(
        "from routers import students", "from routers import grades, students"
    ).replace(
        "app.include_router(students.router)",
        "app.include_router(students.router)\napp.include_router(grades.router)",
    ))
    incremental, metrics = _incremental(repo, store)

    assert metrics.files_added == 1
    assert any("POST /grades" in row for row in incremental["composed_routes"])
    _assert_equivalent(_cold(repo, tmp_path, "2"), incremental)


def test_3_deleted_module(repo: Path, tmp_path: Path) -> None:
    """A removed module leaves no residue behind."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    (repo / "routers/students.py").unlink()
    _write(repo, "main.py", "from fastapi import FastAPI\n\napp = FastAPI()\n")
    incremental, metrics = _incremental(repo, store)

    assert metrics.files_deleted == 1
    assert not any("students" in row for row in incremental["composed_routes"])
    assert not any("students.py" in key for key in incremental["symbol_files"])
    _assert_equivalent(_cold(repo, tmp_path, "3"), incremental)


def test_4_import_change(repo: Path, tmp_path: Path) -> None:
    """A handler swaps the dependency it imports."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    _write(repo, "store_layer.py", "def get_db():\n    return None\n")
    _write(
        repo,
        "routers/students.py",
        _ROUTES.replace("from database import get_db", "from store_layer import get_db"),
    )
    incremental, _ = _incremental(repo, store)

    _assert_equivalent(_cold(repo, tmp_path, "4"), incremental)


def test_5_router_prefix_change(repo: Path, tmp_path: Path) -> None:
    """Changing a router prefix must recompose every route beneath it."""
    store = FileFactStore(tmp_path / "store")
    before, _ = _incremental(repo, store)
    assert any(row.startswith("POST /students") for row in before["composed_routes"])

    _write(
        repo,
        "routers/students.py",
        _ROUTES.replace('APIRouter(prefix="/students")', 'APIRouter(prefix="/api/students")'),
    )
    incremental, _ = _incremental(repo, store)

    assert any(row.startswith("POST /api/students") for row in incremental["composed_routes"])
    assert not any(row.startswith("POST /students ") for row in incremental["composed_routes"])
    _assert_equivalent(_cold(repo, tmp_path, "5"), incremental)


def test_6_cross_file_call_change(repo: Path, tmp_path: Path) -> None:
    """The handler calls a different downstream function."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    _write(
        repo,
        "routers/students.py",
        _ROUTES.replace("crud.create_student(db, payload)", "crud.archive_student(db, payload)"),
    )
    incremental, _ = _incremental(repo, store)

    _assert_equivalent(_cold(repo, tmp_path, "6"), incremental)


def test_7_change_then_revert_matches_cold_original(repo: Path, tmp_path: Path) -> None:
    """The test the external index failed.

    Cold(original) must equal original -> incremental change -> incremental
    revert. Reaching a state by a different route may not change the facts.
    """
    cold_original = _cold(repo, tmp_path, "7-baseline")

    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    _write(
        repo,
        "routers/students.py",
        _ROUTES.replace("crud.create_student(db, payload)", "crud.archive_student(db, payload)"),
    )
    _incremental(repo, store)

    _write(repo, "routers/students.py", _ROUTES)
    reverted, metrics = _incremental(repo, store)

    assert metrics.index_state == STATE_UPDATED
    _assert_equivalent(cold_original, reverted)


def test_7b_add_then_delete_round_trip(repo: Path, tmp_path: Path) -> None:
    """A path-set round trip must also land exactly where it started."""
    cold_original = _cold(repo, tmp_path, "7b-baseline")

    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    _write(repo, "routers/extra.py", 'from fastapi import APIRouter\n\nrouter = APIRouter()\n')
    _incremental(repo, store)

    (repo / "routers/extra.py").unlink()
    reverted, _ = _incremental(repo, store)

    _assert_equivalent(cold_original, reverted)


def test_8_deleted_dependency_leaves_no_stale_resolution(repo: Path, tmp_path: Path) -> None:
    """When an imported file disappears, its resolution must disappear too.

    `routers/students.py` is untouched, so a naive per-file cache would reuse
    its facts wholesale and keep claiming `database.py` resolves. This is the
    exact failure mode the external spike exhibited.
    """
    store = FileFactStore(tmp_path / "store")
    before, _ = _incremental(repo, store)
    resolved = [
        entry
        for entry in before["symbol_files"]["svc:routers/students.py"]["imports"]
        if '"resolved_file": "database.py"' in entry
    ]
    assert resolved, "the baseline must actually resolve the dependency"

    (repo / "database.py").unlink()
    incremental, metrics = _incremental(repo, store)

    assert metrics.files_deleted == 1
    stale = [
        entry
        for entry in incremental["symbol_files"]["svc:routers/students.py"]["imports"]
        if '"resolved_file": "database.py"' in entry
    ]
    assert stale == [], "a resolution outlived the file it pointed at"
    _assert_equivalent(_cold(repo, tmp_path, "8"), incremental)


# --------------------------------------------------------------------------
# Manifest behaviour
# --------------------------------------------------------------------------


def test_touched_but_identical_file_is_not_reparsed(repo: Path, tmp_path: Path) -> None:
    """mtime is a hint; SHA-256 decides. Rewriting identical bytes is a no-op."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    path = repo / "crud.py"
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    _, metrics = _incremental(repo, store)

    assert metrics.files_modified == 0
    assert metrics.index_state == STATE_WARM


def test_fact_version_change_forces_a_cold_rebuild(repo: Path, tmp_path: Path) -> None:
    """Stale-shaped facts are discarded rather than mixed with new ones."""
    store = FileFactStore(tmp_path / "store")
    _incremental(repo, store)

    manifest_path = store.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["fact_versions"]["route_index"] = "route_index/v0-old"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = FileFactStore(store.directory)
    index = build_structural_index(_repos(repo), store=reopened)

    assert index.metrics.index_state == STATE_COLD
    _assert_equivalent(_cold(repo, tmp_path, "version"), _facts(index))


# --------------------------------------------------------------------------
# Drop-in equivalence with the existing pipeline
# --------------------------------------------------------------------------


def test_structural_index_matches_the_existing_batch_builders(
    repo: Path, tmp_path: Path
) -> None:
    """The index is a substrate, not a second pipeline.

    Whatever the existing batch builders produce today, a cold structural index
    must produce identically — otherwise wiring it underneath the commands
    would change their artifacts.
    """
    from sydes.discover.route_graph import build_route_graph_facts_batch
    from sydes.discover.route_index import build_route_index_batch
    from sydes.trace.handler_symbols.index import build_handler_symbol_index_batch

    repos = _repos(repo)
    store = FileFactStore(tmp_path / "store")
    index = build_structural_index(repos, store=store)

    assert index.route_index_batch == build_route_index_batch(repos)
    assert index.handler_symbol_batch == build_handler_symbol_index_batch(repos)
    assert index.route_graph_facts == build_route_graph_facts_batch(repos)


def test_warm_index_still_matches_the_existing_batch_builders(
    repo: Path, tmp_path: Path
) -> None:
    """Reuse may not drift from the builders it stands in for."""
    from sydes.discover.route_graph import build_route_graph_facts_batch
    from sydes.discover.route_index import build_route_index_batch

    repos = _repos(repo)
    store = FileFactStore(tmp_path / "store")
    build_structural_index(repos, store=store)
    warm = build_structural_index(repos, store=store)

    assert warm.metrics.index_state == STATE_WARM
    assert warm.route_index_batch == build_route_index_batch(repos)
    assert warm.route_graph_facts == build_route_graph_facts_batch(repos)
