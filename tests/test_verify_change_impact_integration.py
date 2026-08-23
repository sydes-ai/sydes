"""ImpactInterpreter wired into verify-change's existing pipeline.

These tests exercise the seam added in `analyzer.py`: when the selected
code-intelligence backend is `cbm`, entrypoint *selection* comes from
`ImpactInterpreter` (reconciled against Sydes' own composed routes) rather
than the native file-overlap heuristic, and the pre-existing reachability
gate is fed the interpreter's own findings so a route reached only through a
decorator/usage reference is not silently dropped.

A real tiny git repo supplies genuine symbols and route facts (via the native
backend); a thin wrapper then relabels those facts as coming from `cbm` and
adds the one fact native parsing cannot produce on its own — a decorator
reference — so the test isolates the wiring, not backend parsing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.code_intelligence.cbm import CBM_BACKEND
from sydes.code_intelligence.factory import get_code_intelligence
from sydes.impact.models import COMPLETENESS_TRUNCATED, ImpactResult
from sydes.verify.models import ANALYSIS_PARTIAL, VERDICT_VERIFIED, ChangeVerificationResult

runner = CliRunner()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_FRAMEWORK_STUB = '''class APIRouter:
    def get(self, path, dependencies=None):
        def _wrap(fn):
            return fn
        return _wrap


def Depends(dep):
    return dep
'''

_SERVICE_V1 = "def helper():\n    return 1\n"
_SERVICE_V2 = "def helper():\n    return 2\n"

_VIEWS = '''from framework import APIRouter, Depends
from service import helper

router = APIRouter()


@router.get("/x", dependencies=[Depends(helper)])
def handle():
    return {"ok": True}
'''

_MAIN = '''from framework import APIRouter
from views import router
'''


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo where a decorator names a dependency it never calls."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\n')
    _write(root, "framework.py", _FRAMEWORK_STUB)
    _write(root, "service.py", _SERVICE_V1)
    _write(root, "views.py", _VIEWS)
    _write(root, "main.py", _MAIN)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _run(root: Path, tmp_path: Path) -> ChangeVerificationResult:
    out = tmp_path / "result.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never",
            "--repo", f"app={root}", "--json", str(out),
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    import json
    return ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))


class _CBMLabelledWrapper:
    """Relabels real native facts as `cbm`, adding only a decorator reference.

    `helper`'s call graph is left empty on purpose: the only structural path
    from the changed symbol to `handle` is the decorator argument naming it,
    which is exactly what CALL_REACHABILITY cannot see and DECORATOR_REFERENCE
    exists for.
    """

    def __init__(self, real):
        self._real = real

    def build_or_update(self, repos, *, workspace_id=None, root=None):
        facts = self._real.build_or_update(repos, workspace_id=workspace_id, root=root)
        repo_name = repos[0].name
        return replace(
            facts,
            backend=CBM_BACKEND,
            provides_call_graph=True,
            call_edges=[],
            usage_edges=[],
            entrypoints=[{
                "repo": repo_name,
                "qualified_name": f"{repo_name}.views.handle",
                "symbol": "handle",
                "file": "views.py",
                "line": 8,
                "route_method": "GET",
                "route_path": "/x",
                "decorators": '@router.get("/x", dependencies=[Depends(helper)])',
                "signature": "()",
                "source": CBM_BACKEND,
            }],
        )


@pytest.fixture()
def fake_cbm_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    real = get_code_intelligence("native")
    wrapper = _CBMLabelledWrapper(real)
    monkeypatch.setattr(
        "sydes.verify.analyzer.get_code_intelligence", lambda backend=None: wrapper
    )


def test_a_route_reached_only_by_decorator_reference_is_not_dropped(
    repo: Path, tmp_path: Path, fake_cbm_backend: None,
) -> None:
    """The reachability gate predates ImpactInterpreter and only trusts a
    called/changed handler; it must still surface `handle` because the
    interpreter proved a decorator-reference path to it."""
    _write(repo, "service.py", _SERVICE_V2)

    result = _run(repo, tmp_path)

    assert any(
        "impact_interpreter:" in line for line in result.diagnostics
    ), "ImpactInterpreter must be invoked for the cbm backend"
    paths = {(flow.method, flow.path) for flow in result.affected_flows}
    assert ("GET", "/x") in paths


def test_truncated_impact_keeps_the_verdict_conservative(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated impact traversal must show up as incomplete analysis, and
    must never let a route it could not fully resolve read as verified."""
    _write(repo, "service.py", _SERVICE_V2)

    truncated = ImpactResult(affected=[], unresolved=[], completeness=COMPLETENESS_TRUNCATED)
    monkeypatch.setattr(
        "sydes.verify.analyzer.ImpactInterpreter.interpret",
        lambda self, *a, **k: truncated,
    )

    result = _run(repo, tmp_path)

    assert result.analysis_status == ANALYSIS_PARTIAL
    assert any("truncated" in note.lower() for note in result.analysis_notes)
    assert result.summary.verdict != VERDICT_VERIFIED
    assert result.affected_flows == []
