"""Tests for real discovery orchestration with mocked LLM boundary."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sydes.cli.routes as routes_module
import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.core.models import CandidateFileRead, ReadFileSnippet, RepoRef
from sydes.discover.endpoints import discover_endpoints, run_llm_endpoint_discovery
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_llm_preflight_success(monkeypatch):
    """Mocked discovery tests should bypass live preflight checks."""
    from sydes.llm.client import LLMValidationResult

    ok = LLMValidationResult(ok=True, provider="ollama", model="llama3.1:latest", base_url="http://localhost:11434")
    monkeypatch.setattr("sydes.cli.routes.validate_llm_available", lambda model_spec=None: ok)
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


@dataclass
class _FakeClient:
    """Simple fake client returning a pre-defined response payload."""

    payload: str
    call_count: int = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        assert "Task: extract likely HTTP API route declarations" in request.prompt
        return LLMResponse(text=self.payload)


def test_mocked_llm_prompt_to_endpoint_candidates() -> None:
    """Prompt + parse path should convert mocked JSON into endpoint candidates."""
    client = _FakeClient(
        payload=(
            '{"endpoints":[{"method":"POST","path":"/checkout","handler":"checkout",'
            '"file":"src/routes.py","repo":"api","confidence":0.7,"status":"inferred",'
            '"evidence":[{"file":"src/routes.py","label":"route registration"}]}]}'
        )
    )
    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.post('/checkout', checkout)",
                line_count=1,
                char_count=34,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert client.call_count == 1
    assert len(result.endpoints) == 1
    assert result.endpoints[0].path == "/checkout"
    assert result.endpoints[0].method == "POST"


def test_mocked_llm_messy_json_with_code_fences_parses() -> None:
    """Messy fenced JSON should still parse into endpoints."""
    client = _FakeClient(
        payload=(
            "```json\n"
            '[{"method":"GET","path":"/health","file":"app.py","repo":"api"}]\n'
            "```"
        )
    )
    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="app.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="app.py",
                text="@app.get('/health')",
                line_count=1,
                char_count=18,
            ),
        )
    ]
    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].path == "/health"


def test_mocked_llm_dedupe_and_filter_behavior() -> None:
    """Deduplication/filtering should keep strong grounded candidates only."""
    client = _FakeClient(
        payload=(
            '{"endpoints":['
            '{"method":"post","path":"/checkout","handler":"checkout","file":"src/routes.py","repo":"api","confidence":0.6},'
            '{"method":"POST","path":"/checkout","handler":"checkout","file":"src/routes.py","repo":"api","confidence":0.4},'
            '{"file":"src/weak.py","repo":"api","evidence":[{"file":"src/weak.py","label":"maybe"}]}'
            ']}'
        )
    )
    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.post('/checkout', checkout)",
                line_count=1,
                char_count=34,
            ),
        )
    ]
    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].path == "/checkout"
    assert any("Dropped endpoint" in note for note in result.notes)


def test_routes_command_shows_discovered_endpoints_with_mocked_llm(
    tmp_path: Path, monkeypatch
) -> None:
    """Routes CLI should show discovered routes when LLM boundary is mocked."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text(
        "router.post('/checkout', checkout)\n",
        encoding="utf-8",
    )

    client = _FakeClient(
        payload=(
            '{"endpoints":[{"method":"POST","path":"/checkout","handler":"checkout",'
            '"file":"src/routes.py","repo":"api","service":"orders","status":"inferred"}]}'
        )
    )
    monkeypatch.setattr(
        "sydes.discover.endpoints.create_default_llm_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(routes_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(routes_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        routes_module, "save_run_artifact", lambda **kwargs: Path("/tmp/routes_discovery.json")
    )

    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}"])

    assert result.exit_code == 0
    assert "Routes discovered: 1" in result.stdout
    assert "api / orders:" in result.stdout
    assert "POST /checkout" in result.stdout


def test_trace_command_resolves_target_with_mocked_llm(tmp_path: Path, monkeypatch) -> None:
    """Trace CLI should resolve target route from mocked real discovery output."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text(
        "router.post('/checkout', checkout)\n",
        encoding="utf-8",
    )

    client = _FakeClient(
        payload=(
            '{"endpoints":[{"method":"POST","path":"/checkout","handler":"checkout",'
            '"file":"src/routes.py","repo":"api","service":"orders","confidence":0.8}]}'
        )
    )
    monkeypatch.setattr(
        "sydes.discover.endpoints.create_default_llm_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module, "save_run_artifact", lambda **kwargs: Path("/tmp/trace_result.json")
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"api={repo_root}",
            "--allow-partial",
        ],
    )

    assert result.exit_code == 0
    assert "Matched endpoint:" in result.stdout
    assert "POST /checkout" in result.stdout
    assert "repo=api" in result.stdout


def test_discovery_fallback_when_mocked_client_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """Discovery should keep deterministic routes even when LLM client creation fails."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text("router.get('/status', status)\n")

    monkeypatch.setattr(
        "sydes.discover.endpoints.create_default_llm_client",
        lambda **_kwargs: (_ for _ in ()).throw(LLMClientError("mock unavailable")),
    )

    result = discover_endpoints([RepoRef(name="api", root=str(repo_root))])

    assert len(result.routes) == 1
    assert result.routes[0].method == "GET"
    assert result.routes[0].path == "/status"
    assert result.routes[0].file == "src/routes.py"
    assert any("LLM discovery unavailable" in note for note in result.notes)
