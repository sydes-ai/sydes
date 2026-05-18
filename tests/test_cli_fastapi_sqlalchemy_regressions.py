"""CLI regressions for FastAPI + SQLAlchemy fixture behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.routes as routes_module
import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse, LLMValidationResult

runner = CliRunner()


@dataclass
class _FakeDiscoveryClient:
    """Fake endpoint-discovery client returning deterministic route payload."""

    payload: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "extract likely HTTP API endpoints" in request.prompt
        return LLMResponse(text=self.payload)


class _UnavailableFlowClient:
    """Fake flow-expansion client that always fails to force deterministic fallback."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("mock unavailable")


def _write_fastapi_sqlalchemy_fixture(repo_root: Path) -> None:
    """Create a minimal FastAPI + SQLAlchemy-like fixture file layout."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "main.py").write_text(
        "\n".join(
            [
                "from fastapi import FastAPI, Depends",
                "from sqlalchemy.orm import Session",
                "",
                "app = FastAPI()",
                "",
                "def get_db():",
                "    yield None",
                "",
                "class User:",
                "    pass",
                "",
                "class UserCreate:",
                "    def model_dump(self):",
                "        return {}",
                "",
                "@app.get('/users/')",
                "def get_all_users(db: Session = Depends(get_db)):",
                "    return db.query(User).all()",
                "",
                "@app.post('/users/')",
                "def create_user(user_in: UserCreate, db: Session = Depends(get_db)):",
                "    db_user = User()",
                "    db.add(db_user)",
                "    db.commit()",
                "    db.refresh(db_user)",
                "    return db_user",
            ]
        ),
        encoding="utf-8",
    )


def _mock_cli_preflight_ok(monkeypatch) -> None:
    """Patch routes/trace preflight to deterministic success for positive regressions."""
    ok = LLMValidationResult(
        ok=True,
        provider="ollama",
        model="llama3.1:latest",
        base_url="http://localhost:11434",
    )
    monkeypatch.setattr("sydes.cli.routes.validate_llm_available", lambda model_spec=None: ok)
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


def test_routes_json_regression_discovers_fastapi_users_routes(tmp_path: Path, monkeypatch) -> None:
    """Routes JSON should include GET/POST /users with handler/file/repo fields populated."""
    repo_root = tmp_path / "SimpleFastPyAPI"
    _write_fastapi_sqlalchemy_fixture(repo_root)
    _mock_cli_preflight_ok(monkeypatch)

    client = _FakeDiscoveryClient(
        payload=(
            '{"endpoints":['
            '{"method":"GET","path":"/users","handler":"get_all_users","file":"main.py","repo":"SimpleFastPyAPI"},'
            '{"method":"POST","path":"/users","handler":"create_user","file":"main.py","repo":"SimpleFastPyAPI"}'
            ']}'
        )
    )
    monkeypatch.setattr("sydes.discover.endpoints.create_default_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr(routes_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(routes_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        routes_module,
        "save_run_artifact",
        lambda **kwargs: Path("/tmp/routes_discovery.json"),
    )

    result = runner.invoke(
        app,
        ["routes", "--repo", f"SimpleFastPyAPI={repo_root}", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    route_map = {(item.get("method"), item.get("path")): item for item in payload["routes"]}
    assert ("GET", "/users") in route_map
    assert ("POST", "/users") in route_map
    assert route_map[("GET", "/users")]["handler"] == "get_all_users"
    assert route_map[("GET", "/users")]["file"] == "main.py"
    assert route_map[("GET", "/users")]["repo"] == "SimpleFastPyAPI"
    assert route_map[("POST", "/users")]["handler"] == "create_user"


def test_trace_get_users_regression_includes_deterministic_query_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    """Trace GET /users should include endpoint + deterministic db.query(User).all() evidence."""
    repo_root = tmp_path / "SimpleFastPyAPI"
    _write_fastapi_sqlalchemy_fixture(repo_root)
    _mock_cli_preflight_ok(monkeypatch)

    client = _FakeDiscoveryClient(
        payload=(
            '{"endpoints":['
            '{"method":"GET","path":"/users","handler":"get_all_users","file":"main.py","repo":"SimpleFastPyAPI"}'
            ']}'
        )
    )
    monkeypatch.setattr("sydes.discover.endpoints.create_default_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr("sydes.trace.expand.create_default_llm_client", lambda **_kwargs: _UnavailableFlowClient())
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "GET",
            "--repo",
            f"SimpleFastPyAPI={repo_root}",
            "--allow-partial",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    endpoint_nodes = [n for n in payload["nodes"] if n["type"] == "api_endpoint"]
    assert any(n.get("path") == "/users" and n.get("method") == "GET" for n in endpoint_nodes)
    flow_steps = payload["flows"][0]["steps"]
    assert flow_steps and flow_steps[0]["kind"] == "endpoint"
    internal_steps = [n for n in payload["nodes"] if n["type"] == "internal_step"]
    assert any(n.get("metadata", {}).get("expression") == "db.query(User).all()" for n in internal_steps)
    db_sinks = [n for n in payload["nodes"] if n["type"] == "database"]
    assert any(
        n.get("metadata", {}).get("action") == "read"
        and n.get("metadata", {}).get("target_entity") == "User"
        for n in db_sinks
    )


def test_trace_post_users_regression_includes_write_sequence_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    """Trace POST /users should preserve db.add/db.commit/db.refresh deterministic evidence."""
    repo_root = tmp_path / "SimpleFastPyAPI"
    _write_fastapi_sqlalchemy_fixture(repo_root)
    _mock_cli_preflight_ok(monkeypatch)

    client = _FakeDiscoveryClient(
        payload=(
            '{"endpoints":['
            '{"method":"POST","path":"/users","handler":"create_user","file":"main.py","repo":"SimpleFastPyAPI"}'
            ']}'
        )
    )
    monkeypatch.setattr("sydes.discover.endpoints.create_default_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr("sydes.trace.expand.create_default_llm_client", lambda **_kwargs: _UnavailableFlowClient())
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "POST",
            "--repo",
            f"SimpleFastPyAPI={repo_root}",
            "--allow-partial",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    expressions = {
        node.get("metadata", {}).get("expression")
        for node in payload["nodes"]
        if node.get("type") == "internal_step"
    }
    assert "db.add(db_user)" in expressions
    assert "db.commit()" in expressions
    assert "db.refresh(db_user)" in expressions
    db_sinks = [n for n in payload["nodes"] if n["type"] == "database"]
    assert any(n.get("metadata", {}).get("action") == "write" for n in db_sinks)


def test_routes_strict_failure_missing_ollama_model_exits_nonzero_without_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """Missing Ollama model should fail preflight and avoid success-looking artifact writes."""
    repo_root = tmp_path / "SimpleFastPyAPI"
    _write_fastapi_sqlalchemy_fixture(repo_root)
    called = {"saved": False}

    monkeypatch.setenv("SYDES_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SYDES_LLM_MODEL", "missing-model")
    monkeypatch.setenv("SYDES_LLM_BASE_URL", "http://localhost:11434")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"models":[{"name":"llama3.1:latest"}]}'

    monkeypatch.setattr("sydes.llm.client.request.urlopen", lambda *_args, **_kwargs: _FakeResponse())

    def _should_not_save(*_args, **_kwargs):
        called["saved"] = True
        raise AssertionError("save_run_artifact should not be called on preflight failure")

    monkeypatch.setattr(routes_module, "save_run_artifact", _should_not_save)

    result = runner.invoke(
        app,
        ["routes", "--repo", f"SimpleFastPyAPI={repo_root}", "--model", "ollama:missing-model"],
    )

    assert result.exit_code != 0
    assert "LLM validation failed: LLM model not available: missing-model." in result.stdout
    assert "Saved discovery artifact" not in result.stdout
    assert called["saved"] is False


def test_routes_strict_failure_missing_openai_key_exits_nonzero_without_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """Missing OpenAI key should fail preflight clearly and not write success artifacts."""
    repo_root = tmp_path / "SimpleFastPyAPI"
    _write_fastapi_sqlalchemy_fixture(repo_root)
    called = {"saved": False}

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def _should_not_save(*_args, **_kwargs):
        called["saved"] = True
        raise AssertionError("save_run_artifact should not be called on preflight failure")

    monkeypatch.setattr(routes_module, "save_run_artifact", _should_not_save)

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"SimpleFastPyAPI={repo_root}",
            "--model",
            "openai:gpt-4.1-mini",
        ],
    )

    assert result.exit_code != 0
    assert "LLM validation failed: OpenAI API key is not configured." in result.stdout
    assert "Saved discovery artifact" not in result.stdout
    assert called["saved"] is False
