"""Tests for LLM-guided downstream flow expansion behavior."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.core.models import (
    EndpointCandidate,
    EvidenceRef,
    FlowExpansionResult,
    RepoRef,
    RoutesResult,
)
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.trace.expand import run_flow_expansion

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_llm_preflight_success(monkeypatch):
    """Trace CLI tests in this module should not depend on live LLM preflight checks."""
    from sydes.llm.client import LLMValidationResult

    ok = LLMValidationResult(ok=True, provider="ollama", model="llama3.1:latest", base_url="http://localhost:11434")
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


@dataclass
class _FakeFlowClient:
    """Simple fake client for flow expansion prompt tests."""

    payload: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "infer one short happy-path flow" in request.prompt
        return LLMResponse(text=self.payload)


def test_run_flow_expansion_drops_unsupported_abstract_steps_and_normalizes_sinks(tmp_path: Path) -> None:
    """Flow expansion should reject ungrounded abstract steps while normalizing sinks."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text(
        "router.post('/checkout', create_checkout)\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "checkout_service.py").write_text(
        "def create_checkout():\n    db.save()\n",
        encoding="utf-8",
    )
    endpoint = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="create_checkout",
        file="src/routes.py",
        repo="api",
        evidence=[EvidenceRef(file="src/routes.py", symbol="create_checkout", label="route")],
    )
    repos = [RepoRef(name="api", root=str(repo_root))]
    client = _FakeFlowClient(
        payload=(
            "```json\n"
            '{"steps":[{"kind":" handler ","name":" create_checkout ","file":"src/routes.py","repo":"api","confidence":0.82},'
            '{"kind":"external http","name":"call payment_client","file":"src/checkout_service.py"},'
            '{"kind":"service_call","name":"   ","symbol":"   "}],'
            '"sinks":[{"kind":"sql-db","name":"orders","action":"write","file":"src/checkout_service.py"}],'
            '"notes":["partial flow"]}\n'
            "```"
        )
    )

    result = run_flow_expansion(endpoint, repos, llm_client=client)

    assert len(result.steps) == 1
    assert result.steps[0].kind == "handler"
    assert result.steps[0].status == "inferred"
    assert len(result.sinks) == 1
    assert result.sinks[0].kind == "database"
    assert result.sinks[0].status == "inferred"
    assert any("Dropped suspicious abstract step" in note for note in result.notes)
    assert any("missing meaningful content" in note for note in result.notes)
    assert result.confidence is not None


def test_run_flow_expansion_recovers_sinks_from_steps_when_explicit_is_weak(
    tmp_path: Path,
) -> None:
    """Weak explicit sinks should be supplemented by derived sinks from retained steps."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text(
        "router.post('/users', create_user)\n",
        encoding="utf-8",
    )
    endpoint = EndpointCandidate(
        method="POST",
        path="/users",
        handler="create_user",
        file="src/routes.py",
        repo="api",
    )
    repos = [RepoRef(name="api", root=str(repo_root))]
    client = _FakeFlowClient(
        payload=(
            '{"steps":[{"kind":"internal_step","name":"db.add","file":"src/routes.py","repo":"api"},'
            '{"kind":"internal_step","name":"db.commit","file":"src/routes.py","repo":"api"}],'
            '"sinks":[{"kind":"database"}]}'
        )
    )

    result = run_flow_expansion(endpoint, repos, llm_client=client)

    assert len(result.steps) >= 2
    assert result.sinks
    assert any(sink.kind == "database" and sink.action == "write" for sink in result.sinks)
    assert any("Derived" in note for note in result.notes)


def test_run_flow_expansion_graceful_fallback_on_client_failure(tmp_path: Path) -> None:
    """Flow expansion should return valid empty result with notes on client error."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text("router.get('/status', status)\n", encoding="utf-8")
    endpoint = EndpointCandidate(method="GET", path="/status", file="src/routes.py", repo="api")
    repos = [RepoRef(name="api", root=str(repo_root))]

    class _ErrorClient:
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise LLMClientError("mock unavailable")

    result = run_flow_expansion(endpoint, repos, llm_client=_ErrorClient())

    assert result.steps == []
    assert result.sinks == []
    assert any("Flow expansion unavailable" in note for note in result.notes)


def test_run_flow_expansion_raises_on_malformed_json_in_strict_mode(tmp_path: Path) -> None:
    """Malformed model output should fail in strict mode instead of returning partial output."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text("router.get('/status', status)\n", encoding="utf-8")
    endpoint = EndpointCandidate(method="GET", path="/status", file="src/routes.py", repo="api")
    repos = [RepoRef(name="api", root=str(repo_root))]
    client = _FakeFlowClient(payload="not-json")

    with pytest.raises(LLMClientError, match="model output parse failure"):
        run_flow_expansion(endpoint, repos, llm_client=client, strict_llm=True)


def test_trace_command_saves_flow_expansion_artifact(tmp_path: Path, monkeypatch) -> None:
    """Trace command should save flow expansion artifact alongside trace artifact."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    saved_names: list[str] = []

    def _fake_discovery(
        repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False
    ) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/checkout",
                    handler="checkout_primary",
                    file="src/routes.py",
                    repo="api",
                    service="orders",
                    confidence=0.9,
                )
            ],
        )

    def _fake_save_run_artifact(**kwargs):
        saved_names.append(kwargs["artifact_name"])
        return Path(f"/tmp/{kwargs['artifact_name']}.json")

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
            entry_endpoint_id="endpoint:api",
            notes=["mock expansion"],
        ),
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(trace_module, "save_run_artifact", _fake_save_run_artifact)

    result = runner.invoke(
        app,
        ["trace", "/checkout", "--method", "POST", "--repo", f"api={repo_root}"],
    )

    assert result.exit_code == 0
    assert "trace_result" in saved_names
    assert "flow_expansion" in saved_names
