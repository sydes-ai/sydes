"""Task 7, Part 6: workflow-level smoke tests for the M4 loop-closed product.

Not the real-PR benchmark (that lives in sydes-evals and is never run here) —
these are cheap, local, no-network exercises of the actual public workflow
end to end (`sydes verify-change` CLI -> JSON artifact -> human report),
covering the four shapes that matter most now that PROVEN and INFERRED
impacts both survive to the final review:

  A. deterministic-only:  change -> PROVEN impact -> obligation -> report.
  B. LLM inferred:        change -> unresolved area -> INFERRED candidate
                           -> candidate survives -> obligation/provenance
                           -> report.
  C. provider failure:    change -> deterministic result remains -> AI
                           inference failure reported -> conservative
                           verdict -> report explains degraded analysis.
  D. duplicate:           deterministic and LLM both name the same route
                           -> one final impact -> PROVEN wins.

A fake LLM client stands in for the real provider (no network); everything
else — diffing, `ImpactInterpreter`, corroboration, obligation generation,
the terminal renderer — runs for real, exactly as `verify-change` runs it
for a product user.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.code_intelligence.cbm import CBM_BACKEND
from sydes.code_intelligence.factory import get_code_intelligence
from sydes.impact.models import IMPACT_STATUS_INFERRED, IMPACT_STATUS_PROVEN
from sydes.llm.client import LLMClientError, LLMResponse
from sydes.verify.models import VERDICT_VERIFIED, ChangeVerificationResult

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

_SERVICE_V1 = '''def helper():
    return 1


def orphan_helper():
    return 1
'''

_SERVICE_V2 = '''def helper():
    return 2


def orphan_helper():
    return 2
'''

_VIEWS = '''from framework import APIRouter, Depends
from service import helper

router = APIRouter()


@router.get("/x", dependencies=[Depends(helper)])
def handle_x():
    return {"ok": True}


@router.get("/y")
def handle_y():
    return {"ok": True}
'''

_MAIN = '''from framework import APIRouter
from views import router
'''


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo with one deterministically-reachable route (`/x`, reached via
    `helper`'s decorator reference) and one known-but-unreached route (`/y`,
    named by nothing) plus a fully orphaned symbol (`orphan_helper`, called
    and referenced by nothing) — enough surface for all four workflows."""
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


class _TwoRouteCBMWrapper:
    """Relabels real native facts as `cbm`: `/x` carries a decorator
    reference to `helper` (deterministically reachable); `/y` carries no
    reference from anywhere (known to the graph, reached by nothing) —
    exactly the shape an LLM-inferred impact needs to land on a real,
    already-known entrypoint rather than an invented one."""

    def __init__(self, real):
        self._real = real

    def build_or_update(self, repos, *, workspace_id=None, root=None, defer_edges=False, changed_files_by_repo=None):
        facts = self._real.build_or_update(repos, workspace_id=workspace_id, root=root)
        repo_name = repos[0].name
        return replace(
            facts,
            backend=CBM_BACKEND,
            provides_call_graph=True,
            call_edges=[],
            usage_edges=[],
            entrypoints=[
                {
                    "repo": repo_name, "qualified_name": f"{repo_name}.views.handle_x",
                    "symbol": "handle_x", "file": "views.py", "line": 9,
                    "route_method": "GET", "route_path": "/x",
                    "decorators": '@router.get("/x", dependencies=[Depends(helper)])',
                    "signature": "()", "source": CBM_BACKEND,
                },
                {
                    "repo": repo_name, "qualified_name": f"{repo_name}.views.handle_y",
                    "symbol": "handle_y", "file": "views.py", "line": 14,
                    "route_method": "GET", "route_path": "/y",
                    "decorators": '@router.get("/y")',
                    "signature": "()", "source": CBM_BACKEND,
                },
            ],
        )


@pytest.fixture()
def fake_cbm_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    real = get_code_intelligence("native")
    wrapper = _TwoRouteCBMWrapper(real)
    monkeypatch.setattr(
        "sydes.verify.analyzer.get_code_intelligence", lambda backend=None: wrapper
    )


def _run_cli(repo_path: Path, tmp_path: Path, *extra_args: str) -> tuple[ChangeVerificationResult, str]:
    """Invoke the real CLI end to end; return the parsed artifact and the
    human-readable terminal report (`--verbose` stdout) together, so a test
    can assert on both without re-invoking anything."""
    out = tmp_path / "result.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--repo", f"app={repo_path}",
            "--json", str(out), "--verbose", *extra_args,
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    result = ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))
    return result, outcome.output


class _ScriptedLLMClient:
    """Returns one fixed response per call, in order — no network, no
    randomness, exactly the "fake/fixed LLM response" Part 6 asks for."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def generate(self, request):  # noqa: ANN001 - matches LLMClient protocol
        self.calls += 1
        if not self._responses:
            return LLMResponse(text='{"action": "stop_unresolved", "rationale": "done"}')
        return LLMResponse(text=self._responses.pop(0))


def _infer_impact_response(
    entrypoint: str, *, confidence: float, reason: str, uncertainty: str,
    entrypoint_symbol: str | None = None,
) -> str:
    candidate: dict[str, object] = {
        "entrypoint": entrypoint, "confidence": confidence,
        "reason": reason, "inference_type": "semantic_indirect_dependency",
        "uncertainty": uncertainty,
    }
    if entrypoint_symbol is not None:
        candidate["entrypoint_symbol"] = entrypoint_symbol
    return json.dumps({
        "action": "infer_impact",
        "candidates": [candidate],
        "rationale": "reporting my best inference",
    })


# --- Workflow A: deterministic-only ---------------------------------------

def test_workflow_a_deterministic_only_change_reaches_proven_impact_and_report(
    repo: Path, tmp_path: Path, fake_cbm_backend: None,
) -> None:
    _write(repo, "service.py", _SERVICE_V2)

    result, report = _run_cli(repo, tmp_path, "--llm-policy", "never", "--impact-guide", "off")

    proven = [i for i in result.accepted_impacts if i.status == IMPACT_STATUS_PROVEN]
    assert any(i.route_path == "/x" for i in proven)
    assert result.summary.counts.impacts_proven >= 1
    assert result.summary.counts.impacts_inferred == 0
    matching_flow = next(f for f in result.affected_flows if f.path == "/x")
    assert matching_flow.obligations, "a proven flow must generate at least one obligation"

    assert "AFFECTED BEHAVIOR" in report
    assert "GET /x" in report
    assert "Proven impacts:" in report


# --- Workflow B: LLM inferred ----------------------------------------------

def test_workflow_b_llm_inferred_candidate_survives_to_obligation_and_report(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`orphan_helper` is unreachable by any deterministic strategy — the
    guide is the only path from it to anything. It proposes `/y`, a real,
    already-known route nothing else in this diff touches, anchored to
    `orphan_helper` (a genuinely changed production symbol).

    This is the positive half of the grounding invariant: a *grounded* LLM
    inference — one whose anchor is a real changed production symbol, and
    whose route additionally corroborates against the known-entrypoint
    index — must survive all the way to an obligation and the report.
    Grounding rejects unsupported candidates (see the companion test
    below); it must never reject supported ones."""
    _write(repo, "service.py", _SERVICE_V2)
    fake_client = _ScriptedLLMClient([
        _infer_impact_response(
            "GET /y", confidence=0.72,
            entrypoint_symbol="orphan_helper",
            reason="orphan_helper participates in the same query-shaping path as handle_y",
            uncertainty="no direct call or usage edge connects them in the current graph",
        ),
    ])
    monkeypatch.setattr("sydes.verify.analyzer.create_default_llm_client", lambda **kwargs: fake_client)

    result, report = _run_cli(repo, tmp_path, "--llm-policy", "never", "--impact-guide", "always")

    assert fake_client.calls >= 1
    inferred = [i for i in result.accepted_impacts if i.status == IMPACT_STATUS_INFERRED]
    assert any(i.route_path == "/y" for i in inferred), "the inferred candidate must survive into accepted_impacts"
    inferred_y = next(i for i in inferred if i.route_path == "/y")
    assert inferred_y.llm_confidence == pytest.approx(0.72)
    assert result.summary.counts.impacts_inferred >= 1

    matching_flow = next((f for f in result.affected_flows if f.path == "/y"), None)
    assert matching_flow is not None, "a corroborated inferred route must still reach affected_flows"
    assert matching_flow.impact_status == IMPACT_STATUS_INFERRED
    assert matching_flow.obligations
    assert result.summary.verdict != VERDICT_VERIFIED

    assert "Inferred impacts:" in report
    assert "GET /y" in report
    assert "LLM confidence" in report


def test_workflow_b_unanchored_candidate_naming_no_real_route_is_rejected(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inference that names no route, no `entrypoint_symbol`, and no
    `based_on_changed_symbols` is free-floating: nothing deterministic ties
    its claim to this change. Grounding rejects it rather than reporting it.

    This test previously asserted the opposite ("must still appear — never
    silently dropped"), which was correct under the pre-grounding contract
    where the guarantee was "never drop a meaningful inference". Grounding
    hardening deliberately replaced that with "never report an unsupported
    one": an impact a reviewer cannot trace back to the change is noise at
    best and a false lead at worst. The expectation here is stale, not the
    behavior — so the assertions are inverted rather than the rule relaxed.

    The candidate is still fully visible in `llm_candidate_log` with its
    rejection reason, so nothing becomes invisible — only unreported.
    """
    _write(repo, "service.py", _SERVICE_V2)
    fake_client = _ScriptedLLMClient([
        _infer_impact_response(
            "background cache warm for orphan_helper's cached value", confidence=0.4,
            reason="reads a value orphan_helper populates in an in-process cache",
            uncertainty="no structural edge represents in-process caching",
        ),
    ])
    monkeypatch.setattr("sydes.verify.analyzer.create_default_llm_client", lambda **kwargs: fake_client)

    result, report = _run_cli(repo, tmp_path, "--llm-policy", "never", "--impact-guide", "always")

    assert fake_client.calls >= 1, "the guide must actually have been consulted"
    inferred = [i for i in result.accepted_impacts if i.status == IMPACT_STATUS_INFERRED]
    assert inferred == [], "an unanchored candidate must not become an accepted impact"
    assert "background cache warm" not in report
    assert result.summary.counts.impacts_inferred == 0
    # Deterministic analysis is untouched by the rejection: the change's own
    # proven impact is still reported exactly as in Workflow A.
    assert any(i.route_path == "/x" for i in result.accepted_impacts)


# --- Workflow C: provider failure -------------------------------------------

def test_workflow_c_provider_failure_keeps_deterministic_result_and_explains_degradation(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both symbols change: `helper` still resolves deterministically to
    `/x`; the guide is triggered for the orphaned `orphan_helper` but the
    provider itself is unavailable. `/x` must survive untouched and the
    degradation must be visible in the human report, not just diagnostics."""
    _write(repo, "service.py", _SERVICE_V2)

    def _boom(**kwargs):
        raise LLMClientError("no provider configured")

    monkeypatch.setattr("sydes.verify.analyzer.create_default_llm_client", _boom)

    result, report = _run_cli(repo, tmp_path, "--llm-policy", "never", "--impact-guide", "always")

    proven = [i for i in result.accepted_impacts if i.status == IMPACT_STATUS_PROVEN]
    assert any(i.route_path == "/x" for i in proven)
    assert result.summary.counts.impacts_proven >= 1
    assert result.summary.counts.impacts_inferred == 0
    assert any("AI impact inference unavailable" in note for note in result.analysis_notes)
    assert result.summary.verdict != VERDICT_VERIFIED

    assert "AI impact inference unavailable" in report
    assert "Deterministic impact analysis still ran" in report
    assert "GET /x" in report


# --- Workflow D: duplicate ---------------------------------------------------

def test_workflow_d_deterministic_and_llm_agreeing_on_the_same_route_stays_proven(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`helper` deterministically reaches `/x` via its decorator reference.
    The guide, investigating the separately-orphaned `orphan_helper`, also
    names `/x` as affected. The real merge path (not a hand-built
    `ImpactResult`) must collapse this into exactly one PROVEN impact."""
    _write(repo, "service.py", _SERVICE_V2)
    fake_client = _ScriptedLLMClient([
        _infer_impact_response(
            "GET /x", confidence=0.55,
            reason="orphan_helper is invoked from the same request context as handle_x",
            uncertainty="no direct edge, only a naming/context resemblance",
        ),
    ])
    monkeypatch.setattr("sydes.verify.analyzer.create_default_llm_client", lambda **kwargs: fake_client)

    result, report = _run_cli(repo, tmp_path, "--llm-policy", "never", "--impact-guide", "always")

    matching = [i for i in result.accepted_impacts if i.route_method == "GET" and i.route_path == "/x"]
    assert len(matching) == 1, "the same route named by both paths must collapse to one accepted impact"
    assert matching[0].status == IMPACT_STATUS_PROVEN

    matching_flows = [f for f in result.affected_flows if f.path == "/x"]
    assert len(matching_flows) == 1
    assert matching_flows[0].impact_status == IMPACT_STATUS_PROVEN

    assert report.count("GET /x") >= 1
    assert "Proven impacts: " in report
