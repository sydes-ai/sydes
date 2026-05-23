"""CLI regressions for Flask route + trace quality after Phase 31 hardening."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse, LLMValidationResult

runner = CliRunner()


@dataclass
class _FakeDiscoveryClient:
    """Fake endpoint-discovery client returning deterministic route payload."""

    payload: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "extract likely HTTP API route declarations" in request.prompt
        return LLMResponse(text=self.payload)


class _UnavailableFlowClient:
    """Fake flow-expansion client that fails so deterministic baseline is exercised."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("mock unavailable")


def _write_flask_fixture(repo_root: Path) -> None:
    """Create a small Flask app fixture with source routes and test client usage."""
    (repo_root / "app").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)
    (repo_root / "app" / "__init__.py").write_text(
        "\n".join(
            [
                "from flask import Flask",
                "from .routes import bp",
                "",
                "def create_app():",
                "    app = Flask(__name__)",
                "    app.register_blueprint(bp)",
                "    return app",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "app" / "routes.py").write_text(
        "\n".join(
            [
                "from flask import Blueprint, jsonify, request, abort",
                "",
                "bp = Blueprint('items', __name__)",
                "items = []",
                "",
                "@bp.route('/', methods=['GET'])",
                "def hello():",
                "    return jsonify({'ok': True})",
                "",
                "@bp.route('/items', methods=['GET'])",
                "def get_items():",
                "    return jsonify(items)",
                "",
                "@bp.route('/items/<int:item_id>', methods=['GET'])",
                "def get_item(item_id):",
                "    if item_id >= len(items):",
                "        abort(404)",
                "    return jsonify(items[item_id])",
                "",
                "@bp.route('/items', methods=['POST'])",
                "def add_item():",
                "    data = request.get_json()",
                "    item = {'name': data['name'], 'price': data.get('price')}",
                "    items.append(item)",
                "    return jsonify(item), 201",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "tests" / "test_app.py").write_text(
        "\n".join(
            [
                "def test_get_item(client):",
                "    response = client.get('/items/0')",
                "    assert response.status_code in (200, 404)",
            ]
        ),
        encoding="utf-8",
    )


def _mock_cli_preflight_ok(monkeypatch) -> None:
    ok = LLMValidationResult(
        ok=True,
        provider="ollama",
        model="llama3.1:latest",
        base_url="http://localhost:11434",
    )
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


def test_flask_trace_post_items_has_grounded_steps_and_request_body_matrix(tmp_path: Path, monkeypatch) -> None:
    """Flask POST trace should keep grounded flow evidence and include request_body test inputs."""
    repo_root = tmp_path / "flask-sample-app"
    _write_flask_fixture(repo_root)
    _mock_cli_preflight_ok(monkeypatch)
    monkeypatch.setattr(
        "sydes.discover.endpoints.create_default_llm_client",
        lambda **_kwargs: _FakeDiscoveryClient(
            payload='{"endpoints":[{"method":"POST","path":"/items","handler":"add_item","file":"app/routes.py","repo":"flask-sample-app"}]}'
        ),
    )
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
            "/items",
            "--method",
            "POST",
            "--repo",
            f"flask-sample-app={repo_root}",
            "--allow-partial",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    step_nodes = [node for node in payload["nodes"] if node.get("type") == "internal_step"]
    expressions = {node.get("metadata", {}).get("expression") for node in step_nodes}
    step_names = {node.get("name") for node in step_nodes}
    assert "read JSON request body" in step_names
    assert "items.append(item)" in expressions
    assert "return JSON response" in step_names
    matrix_groups = payload.get("test_matrix", {}).get("groups", [])
    request_body_hints = [
        input_hint.get("value_hint")
        for group in matrix_groups
        for test_case in group.get("tests", [])
        for input_hint in test_case.get("inputs", [])
        if input_hint.get("kind") == "request_body"
    ]
    assert request_body_hints
    assert any(isinstance(hint, dict) and "name" in hint for hint in request_body_hints)
    names = {
        test_case["name"]
        for group in matrix_groups
        for test_case in group.get("tests", [])
    }
    assert "post_items_rejects_invalid_payload" in names


def test_flask_trace_get_item_includes_lookup_and_error_path(tmp_path: Path, monkeypatch) -> None:
    """Flask GET item trace should include lookup/read + obvious not-found behavior cues."""
    repo_root = tmp_path / "flask-sample-app"
    _write_flask_fixture(repo_root)
    _mock_cli_preflight_ok(monkeypatch)
    monkeypatch.setattr(
        "sydes.discover.endpoints.create_default_llm_client",
        lambda **_kwargs: _FakeDiscoveryClient(
            payload='{"endpoints":[{"method":"GET","path":"/items/{item_id}","handler":"get_item","file":"app/routes.py","repo":"flask-sample-app"}]}'
        ),
    )
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
            "/items/{item_id}",
            "--method",
            "GET",
            "--repo",
            f"flask-sample-app={repo_root}",
            "--allow-partial",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    step_names = [
        node.get("name", "")
        for node in payload["nodes"]
        if node.get("type") == "internal_step"
    ]
    assert any(name.startswith("if ") for name in step_names)
    assert "abort request" in step_names
    assert "read items[item_id]" in step_names
    assert all("tests/test_app.py" not in (node.get("file") or "") for node in payload["nodes"])
