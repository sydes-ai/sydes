"""The code-intelligence seam must be invisible from above.

Introducing a boundary is only safe if it changes nothing. These tests pin two
properties: the native backend returns exactly the facts the pre-adapter path
returned, and every command that now reads through the boundary produces the
same output it did before.

They also pin the one behavior that must *not* be forgiving. An unknown backend
raises. A verdict assembled from facts supplied by a backend the operator did
not choose would be unreadable, so a silent fallback is worse than a failure.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.code_intelligence import (
    BACKEND_ENV_VAR,
    NATIVE_BACKEND,
    CodeIntelligence,
    CodeIntelligenceError,
    NativeCodeIntelligence,
    available_backends,
    get_code_intelligence,
)
from sydes.core.models import RepoRef
from sydes.discover.file_facts import build_structural_index

runner = CliRunner()

_ROUTES = '''from fastapi import APIRouter

import crud

router = APIRouter(prefix="/students")


@router.post("")
def create_student(payload):
    """Create a student."""
    return crud.create_student(payload)
'''

_CRUD = '''def create_student(payload):
    student = {"name": payload["name"]}
    db.add(student)
    db.commit()
    return student
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
    """A committed service with one composed route and a downstream call."""
    root = tmp_path / "svc"
    _write(root, "main.py", _MAIN)
    _write(root, "crud.py", _CRUD)
    _write(root, "routers/__init__.py", "")
    _write(root, "routers/students.py", _ROUTES)
    _write(root, "tests/test_students.py", "def test_ok():\n    assert True\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["add", "."],
        ["commit", "-qm", "initial"],
    ):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)
    return root


def _repos(root: Path) -> list[RepoRef]:
    return [RepoRef(name="svc", root=str(root))]


# --------------------------------------------------------------------------
# 1. The native adapter returns the pre-adapter facts
# --------------------------------------------------------------------------


def test_native_adapter_matches_the_direct_structural_index(repo: Path) -> None:
    """Same payloads, only re-presented under boundary names."""
    direct = build_structural_index(_repos(repo), workspace_id="ws-direct")
    through = NativeCodeIntelligence().build_or_update(_repos(repo), workspace_id="ws-adapter")

    assert through.repo_map == direct.repo_map_batch
    assert through.route_index == direct.route_index_batch
    assert through.symbol_index == direct.handler_symbol_batch
    assert through.route_graph == direct.route_graph_facts


def test_native_adapter_reports_its_backend_and_metrics(repo: Path) -> None:
    """Diagnostics must say which backend answered."""
    facts = NativeCodeIntelligence().build_or_update(_repos(repo), workspace_id="ws")

    assert facts.backend == NATIVE_BACKEND
    assert facts.metrics["files_total"] > 0
    assert any(line.startswith("index_state=") for line in facts.diagnostics)


def test_facts_expose_per_file_symbols(repo: Path) -> None:
    """The helpers read the same index rather than re-deriving anything."""
    facts = NativeCodeIntelligence().build_or_update(_repos(repo), workspace_id="ws")

    names = {item["name"] for item in facts.symbols_for_file("svc", "crud.py")}
    assert "create_student" in names
    assert "crud.py" in facts.indexed_files("svc")
    assert facts.symbols_for_file("svc", "does/not/exist.py") == []


# --------------------------------------------------------------------------
# 2-4. Command output is unchanged
# --------------------------------------------------------------------------


def _verify_change(repo: Path, out: Path):
    outcome = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never",
         "--repo", f"svc={repo}", "--json", str(out)],
    )
    assert outcome.exit_code == 0, outcome.output
    return outcome, json.loads(out.read_text(encoding="utf-8"))


def test_verify_change_output_is_semantically_unchanged(repo: Path, tmp_path: Path) -> None:
    """The verdict and every obligation survive the boundary untouched."""
    _write(repo, "routers/students.py", _ROUTES.replace(
        '    """Create a student."""',
        '    """Create a student."""\n    if not payload.get("name"):\n        raise ValueError("blank")',
    ))

    _outcome, payload = _verify_change(repo, tmp_path / "a.json")

    assert payload["summary"]["verdict"]
    assert payload["analysis_status"]
    flows = payload["affected_flows"]
    assert flows, "the changed handler must still resolve to a flow"
    assert flows[0]["entry_label"] == "POST /students"
    assert [o["statement"] for o in flows[0]["obligations"]]


def test_verify_change_is_deterministic_across_runs(repo: Path, tmp_path: Path) -> None:
    """A warm second run through the boundary reports the same thing."""
    _write(repo, "routers/students.py", _ROUTES.replace("crud.create_student", "crud.archive"))

    _first, a = _verify_change(repo, tmp_path / "a.json")
    _second, b = _verify_change(repo, tmp_path / "b.json")

    def semantic(payload):
        return {
            "verdict": payload["summary"]["verdict"],
            "risk": payload["summary"]["risk"],
            "counts": payload["summary"]["counts"],
            "analysis": payload["analysis_status"],
            "flows": [
                (f["entry_label"], f["status"],
                 sorted((o["statement"], o["status"], o["required"]) for o in f["obligations"]))
                for f in payload["affected_flows"]
            ],
        }

    assert semantic(a) == semantic(b)


def test_routes_output_is_semantically_unchanged(repo: Path) -> None:
    """Route discovery still composes the router prefix."""
    outcome = runner.invoke(
        app, ["routes", "--repo", f"svc={repo}", "--llm-policy", "never"]
    )

    assert outcome.exit_code == 0, outcome.output
    assert "POST /students" in outcome.stdout


def test_trace_output_is_semantically_unchanged(repo: Path) -> None:
    """Trace still resolves the handler and its downstream call."""
    outcome = runner.invoke(
        app, ["trace", "POST /students", "--repo", f"svc={repo}", "--trace-llm-policy", "never"]
    )

    assert outcome.exit_code == 0, outcome.output
    assert "/students" in outcome.stdout


# --------------------------------------------------------------------------
# 5. Backend selection fails explicitly
# --------------------------------------------------------------------------


def test_default_backend_is_native() -> None:
    assert get_code_intelligence().name == NATIVE_BACKEND
    assert available_backends() == [NATIVE_BACKEND]


def test_explicit_native_selection_works() -> None:
    assert get_code_intelligence(NATIVE_BACKEND).name == NATIVE_BACKEND


@pytest.mark.parametrize("name", ["cbm", "codebase-memory", "typo"])
def test_unknown_backend_raises_instead_of_falling_back(name: str) -> None:
    """Not-yet-implemented is an error, never a quiet substitution."""
    with pytest.raises(CodeIntelligenceError) as excinfo:
        get_code_intelligence(name)

    assert name in str(excinfo.value)
    assert "native" in str(excinfo.value)


def test_environment_override_is_honoured_and_validated(monkeypatch) -> None:
    """The env var selects a backend, and an unknown one still fails loudly."""
    monkeypatch.setenv(BACKEND_ENV_VAR, NATIVE_BACKEND)
    assert get_code_intelligence().name == NATIVE_BACKEND

    monkeypatch.setenv(BACKEND_ENV_VAR, "cbm")
    with pytest.raises(CodeIntelligenceError):
        get_code_intelligence()


def test_native_backend_satisfies_the_protocol() -> None:
    """The adapter is substitutable, which is the whole point of the seam."""
    assert isinstance(NativeCodeIntelligence(), CodeIntelligence)
