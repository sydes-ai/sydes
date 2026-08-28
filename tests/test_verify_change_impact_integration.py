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
from sydes.impact.models import (
    COMPLETENESS_TRUNCATED,
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    PROVENANCE_LLM_INFERRED_CORROBORATED,
    RELATION_LLM_INFERRED,
    STRATEGY_LLM_SEMANTIC_INFERENCE,
    AffectedEntrypoint,
    ImpactPath,
    ImpactResult,
    ImpactStep,
    UnresolvedImpact,
)
from sydes.llm.client import LLMResponse
from sydes.verify.models import (
    ANALYSIS_PARTIAL,
    VERDICT_VERIFIED,
    VERIFICATION_UNVERIFIED,
    ChangeVerificationResult,
)

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

    def build_or_update(self, repos, *, workspace_id=None, root=None, defer_edges=False, changed_files_by_repo=None):
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


class _CountingFakeLLMClient:
    """A fake `LLMClient` that always tells the guide to stop.

    Used to prove `--impact-guide auto` does not perturb an already-resolved
    deterministic case: if the guide were consulted it would still stop
    immediately and change nothing, but the assertion here is on `calls`
    itself — a fully deterministic path must not reach the guide at all.
    """

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):  # noqa: ANN001 - matches LLMClient protocol
        self.calls += 1
        return LLMResponse(text='{"action": "stop_unresolved"}')


def test_impact_guide_auto_does_not_fire_on_an_already_resolved_case(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 9: a case the deterministic pass already resolves (decorator
    reference to `handle`) must not spend a guide turn just because
    `--impact-guide auto` is on."""
    _write(repo, "service.py", _SERVICE_V2)
    fake_client = _CountingFakeLLMClient()
    monkeypatch.setattr(
        "sydes.verify.analyzer.create_default_llm_client", lambda **kwargs: fake_client,
    )

    out = tmp_path / "result.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never",
            "--repo", f"app={repo}", "--json", str(out), "--impact-guide", "auto",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    import json
    result = ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))

    paths = {(flow.method, flow.path) for flow in result.affected_flows}
    assert ("GET", "/x") in paths
    assert fake_client.calls == 0
    assert any(
        "guide_triggered=False" in line for line in result.diagnostics
    )


def test_impact_guide_provider_unavailable_stays_conservative(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--impact-guide auto` with no usable LLM provider must degrade to the
    deterministic result, reported, never raised as a hard failure and never
    silently promoted to a guessed VERIFIED."""
    from sydes.llm.client import LLMClientError

    def _boom(**kwargs):
        raise LLMClientError("no provider configured")

    monkeypatch.setattr("sydes.verify.analyzer.create_default_llm_client", _boom)
    _write(repo, "service.py", _SERVICE_V2)

    out = tmp_path / "result.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never",
            "--repo", f"app={repo}", "--json", str(out), "--impact-guide", "auto",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    import json
    result = ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert any("impact_guide unavailable" in note for note in result.diagnostics)
    assert result.summary.verdict != VERDICT_VERIFIED


def test_inferred_flow_produces_an_obligation_but_never_verified(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 5: an M4 `IMPACT_STATUS_INFERRED` flow must still survive into
    `affected_flows`/obligations (tagged with its provenance), and must never
    by itself make the verdict VERIFIED — the same conservative aggregation
    that already governs every deterministic obligation, unaffected by how
    the flow was found."""
    _write(repo, "service.py", _SERVICE_V2)

    inferred_entry = AffectedEntrypoint(
        repo="app", symbol="handle", qualified_name="app.views.handle", file="views.py",
        kind=ENTRYPOINT_HTTP, route_method="GET", route_path="/x",
        status=IMPACT_STATUS_INFERRED, llm_confidence=0.81,
        llm_reason="shares a query helper", llm_inference_type="shared_utility",
        llm_uncertainty="no direct graph edge", corroborated=True,
        changed_symbols=["helper"],
        paths=[ImpactPath(
            steps=(ImpactStep(
                symbol="handle", qualified_name="app.views.handle", file="views.py",
                relation=RELATION_LLM_INFERRED, evidence="shares a query helper",
                provenance=PROVENANCE_LLM_INFERRED_CORROBORATED,
            ),),
            strategy=STRATEGY_LLM_SEMANTIC_INFERENCE,
        )],
    )
    fake_result = ImpactResult(affected=[inferred_entry], unresolved=[])
    monkeypatch.setattr(
        "sydes.verify.analyzer.ImpactInterpreter.interpret",
        lambda self, *a, **k: fake_result,
    )

    result = _run(repo, tmp_path)

    matching = [f for f in result.affected_flows if (f.method, f.path) == ("GET", "/x")]
    assert matching, "the inferred flow must reach affected_flows, not disappear silently"
    flow = matching[0]
    assert flow.impact_status == IMPACT_STATUS_INFERRED
    assert flow.obligations, "an inferred flow must still be allowed to generate obligations"
    assert all(o.status == VERIFICATION_UNVERIFIED for o in flow.obligations)  # never pre-verified
    assert result.summary.verdict != VERDICT_VERIFIED


def test_deterministic_and_inferred_duplicate_becomes_one_proven_accepted_impact(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 4 (full pipeline): the same real route found both deterministically
    and by the guide must appear exactly once in `accepted_impacts`, as PROVEN."""
    _write(repo, "service.py", _SERVICE_V2)

    proven_entry = AffectedEntrypoint(
        repo="app", symbol="handle", qualified_name="app.views.handle", file="views.py",
        kind=ENTRYPOINT_HTTP, route_method="GET", route_path="/x",
        status=IMPACT_STATUS_PROVEN, changed_symbols=["helper"],
    )
    inferred_duplicate = AffectedEntrypoint(
        repo="app", symbol="handle", qualified_name="app.views.handle", file="views.py",
        kind=ENTRYPOINT_HTTP, route_method="GET", route_path="/x",
        status=IMPACT_STATUS_INFERRED, llm_confidence=0.5, changed_symbols=["helper"],
    )
    fake_result = ImpactResult(affected=[proven_entry, inferred_duplicate], unresolved=[])
    monkeypatch.setattr(
        "sydes.verify.analyzer.ImpactInterpreter.interpret", lambda self, *a, **k: fake_result,
    )

    result = _run(repo, tmp_path)

    matching = [i for i in result.accepted_impacts if i.route_method == "GET" and i.route_path == "/x"]
    assert len(matching) == 1
    assert matching[0].status == IMPACT_STATUS_PROVEN


def test_generic_non_http_accepted_behavior_survives_through_the_real_pipeline(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 6: a non-HTTP accepted impact (no route to construct a flow
    from) must still appear in `accepted_impacts` — never silently dropped
    just because it cannot become a full `AffectedFlow`."""
    _write(repo, "service.py", _SERVICE_V2)

    generic_entry = AffectedEntrypoint(
        repo="app", symbol="nightly_digest", qualified_name="app.jobs.nightly_digest",
        file="jobs.py", kind=ENTRYPOINT_DECORATED, status=IMPACT_STATUS_INFERRED,
        llm_confidence=0.4, llm_reason="reads a cache the changed helper populates",
        changed_symbols=["helper"],
    )
    fake_result = ImpactResult(affected=[generic_entry], unresolved=[])
    monkeypatch.setattr(
        "sydes.verify.analyzer.ImpactInterpreter.interpret", lambda self, *a, **k: fake_result,
    )

    result = _run(repo, tmp_path)

    matching = [i for i in result.accepted_impacts if i.label == "nightly_digest"]
    assert len(matching) == 1
    assert matching[0].verification_model_status == "unsupported_or_partial"
    assert matching[0].status == IMPACT_STATUS_INFERRED
    # Not silently dropped from affected_flows either — it simply isn't
    # there, because it genuinely has no HTTP shape; the report (tested at
    # the renderer level) is what makes it visible.
    assert not any(f.handler == "nightly_digest" for f in result.affected_flows)


def test_obligation_retains_impact_provenance_via_shared_id(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 7: `changed code -> affected behavior -> obligation -> evidence
    -> verdict` must be traceable — an obligation's `flow_id` must match the
    id of the `accepted_impacts` entry that produced it."""
    _write(repo, "service.py", _SERVICE_V2)

    inferred_entry = AffectedEntrypoint(
        repo="app", symbol="handle", qualified_name="app.views.handle", file="views.py",
        kind=ENTRYPOINT_HTTP, route_method="GET", route_path="/x",
        status=IMPACT_STATUS_INFERRED, llm_confidence=0.7, corroborated=True,
        changed_symbols=["helper"],
        paths=[ImpactPath(
            steps=(ImpactStep(
                symbol="handle", qualified_name="app.views.handle", file="views.py",
                relation=RELATION_LLM_INFERRED, evidence="shares a query helper",
                provenance=PROVENANCE_LLM_INFERRED_CORROBORATED,
            ),),
            strategy=STRATEGY_LLM_SEMANTIC_INFERENCE,
        )],
    )
    fake_result = ImpactResult(affected=[inferred_entry], unresolved=[])
    monkeypatch.setattr(
        "sydes.verify.analyzer.ImpactInterpreter.interpret", lambda self, *a, **k: fake_result,
    )

    result = _run(repo, tmp_path)

    impact = next(i for i in result.accepted_impacts if i.route_path == "/x")
    flow = next(f for f in result.affected_flows if f.path == "/x")
    assert impact.id == flow.id
    assert impact.verification_model_status == "modeled"
    obligation_flow_ids = {o.flow_id for o in flow.obligations}
    assert obligation_flow_ids == {flow.id} == {impact.id}


def test_provider_failure_preserves_deterministic_analysis_and_counts(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 9: a guide-provider failure must not erase what deterministic
    analysis already found — `accepted_impacts`/counts must still reflect
    the real, PROVEN route."""
    from sydes.llm.client import LLMClientError

    def _boom(**kwargs):
        raise LLMClientError("no provider configured")

    monkeypatch.setattr("sydes.verify.analyzer.create_default_llm_client", _boom)
    _write(repo, "service.py", _SERVICE_V2)

    out = tmp_path / "result.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never",
            "--repo", f"app={repo}", "--json", str(out), "--impact-guide", "auto",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    import json
    result = ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))

    assert any(i.route_path == "/x" and i.status == IMPACT_STATUS_PROVEN for i in result.accepted_impacts)
    assert result.summary.counts.impacts_proven >= 1
    assert result.summary.counts.impacts_inferred == 0
    assert any("AI impact inference unavailable" in note for note in result.analysis_notes)
    assert result.summary.verdict != VERDICT_VERIFIED


def test_unresolved_changed_symbol_reaches_the_canonical_result_and_stays_visible(
    repo: Path, tmp_path: Path, fake_cbm_backend: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task item 3, full pipeline: a changed symbol the deterministic
    interpreter never connects to any entrypoint (`ImpactResult.unresolved`,
    with `completeness` still COMPLETE — nothing was truncated, there is
    simply nothing to find) must set `unresolved_changed_symbols` on the
    canonical result, keep the verdict off VERIFIED, and say so in
    `analysis_notes` — never silently absorbed into "analysis complete"."""
    _write(repo, "service.py", _SERVICE_V2)

    proven_entry = AffectedEntrypoint(
        repo="app", symbol="handle", qualified_name="app.views.handle", file="views.py",
        kind=ENTRYPOINT_HTTP, route_method="GET", route_path="/x",
        status=IMPACT_STATUS_PROVEN, changed_symbols=["helper"],
    )
    fake_result = ImpactResult(
        affected=[proven_entry],
        unresolved=[UnresolvedImpact(repo="app", symbol="orphan_helper", reason="no_entrypoint_reached")],
    )
    monkeypatch.setattr(
        "sydes.verify.analyzer.ImpactInterpreter.interpret",
        lambda self, *a, **k: fake_result,
    )

    result = _run(repo, tmp_path)

    assert result.unresolved_changed_symbols == 1
    assert result.summary.counts.unresolved_changed_symbols == 1
    assert any(i.route_path == "/x" and i.status == IMPACT_STATUS_PROVEN for i in result.accepted_impacts)
    assert any("no established" in note and "impact path" in note for note in result.analysis_notes)
    assert result.summary.verdict != VERDICT_VERIFIED
    # The precise "would have been VERIFIED but for this" case — where this
    # reason is the only thing standing between the verdict and VERIFIED —
    # is pinned exactly in test_verify_change_summary_safety.py; here we
    # only need to confirm the count and notes actually reach the canonical
    # result end to end.
