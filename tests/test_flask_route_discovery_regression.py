"""Regression fixtures for Flask route discovery false positives.

Phase 31A intentionally captures current behavior without changing production
discovery logic yet.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.core.models import CandidateFileRead, ReadFileSnippet, RepoRef
from sydes.discover.endpoints import discover_endpoints, run_llm_endpoint_discovery
from sydes.llm.client import LLMRequest, LLMResponse


class _FakeDiscoveryClient:
    """Minimal fake client returning a pre-baked JSON payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "HTTP API route declarations" in request.prompt
        return LLMResponse(text=json.dumps(self._payload))


def _write_flask_sample_fixture(repo_root: Path) -> None:
    """Create a Flask sample app with real declarations and test-client calls."""
    (repo_root / "app").mkdir(parents=True, exist_ok=True)
    (repo_root / "tests").mkdir(parents=True, exist_ok=True)

    (repo_root / "app" / "__init__.py").write_text(
        "from flask import Flask\n"
        "from app.routes import bp\n\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    app.register_blueprint(bp)\n"
        "    return app\n",
        encoding="utf-8",
    )
    (repo_root / "app" / "routes.py").write_text(
        "from flask import Blueprint, jsonify, request\n\n"
        "bp = Blueprint('items', __name__)\n\n"
        "@bp.route('/')\n"
        "def hello():\n"
        "    return 'hello'\n\n"
        "@bp.route('/items', methods=['GET'])\n"
        "def get_items():\n"
        "    return jsonify([])\n\n"
        "@bp.route('/items/<int:item_id>', methods=['GET'])\n"
        "def get_item(item_id):\n"
        "    return jsonify({'id': item_id})\n\n"
        "@bp.route('/items', methods=['POST'])\n"
        "def add_item():\n"
        "    data = request.get_json()\n"
        "    return jsonify(data), 201\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "test_app.py").write_text(
        "def test_get_item(client):\n"
        "    response = client.get('/items/0')\n"
        "    assert response.status_code == 200\n\n"
        "def test_get_item_1(client):\n"
        "    response = client.get('/items/1')\n"
        "    assert response.status_code == 200\n\n"
        "def test_add_item(client):\n"
        "    response = client.post('/items', json={'name': 'apple'})\n"
        "    assert response.status_code == 201\n",
        encoding="utf-8",
    )
    (repo_root / "run.py").write_text(
        "from app import create_app\n\n"
        "app = create_app()\n",
        encoding="utf-8",
    )


def _build_candidate_reads() -> list[CandidateFileRead]:
    """Build candidate reads used by endpoint normalization in discovery."""
    return [
        CandidateFileRead(
            repo="flask-sample-app",
            relative_path="app/routes.py",
            snippet=ReadFileSnippet(
                repo="flask-sample-app",
                relative_path="app/routes.py",
                text="@bp.route('/items', methods=['GET'])",
                line_count=1,
                char_count=34,
            ),
        ),
        CandidateFileRead(
            repo="flask-sample-app",
            relative_path="tests/test_app.py",
            snippet=ReadFileSnippet(
                repo="flask-sample-app",
                relative_path="tests/test_app.py",
                text="response = client.get('/items/0')",
                line_count=1,
                char_count=33,
            ),
        ),
    ]


def _mocked_flask_discovery_payload() -> dict:
    """Mock LLM payload that includes both valid declarations and false positives."""
    return {
        "endpoints": [
            {"method": "GET", "path": "/", "repo": "flask-sample-app", "handler": "hello", "file": "app/routes.py", "confidence": 1},
            {"method": "GET", "path": "/items", "repo": "flask-sample-app", "handler": "get_items", "file": "app/routes.py", "confidence": 1},
            {"method": "GET", "path": "/items/{item_id}", "repo": "flask-sample-app", "handler": "get_item", "file": "app/routes.py", "confidence": 1},
            {"method": "POST", "path": "/items", "repo": "flask-sample-app", "handler": "add_item", "file": "app/routes.py", "confidence": 1},
            {"method": "GET", "path": "/items/0", "repo": "flask-sample-app", "handler": "get_item", "file": "tests/test_app.py", "confidence": 1},
            {"method": "GET", "path": "/items/1", "repo": "flask-sample-app", "handler": "get_item_1", "file": "tests/test_app.py", "confidence": 1},
            {"method": "POST", "path": "/items", "repo": "flask-sample-app", "handler": "test_add_item", "file": "tests/test_app.py", "confidence": 1},
        ]
    }


def test_flask_valid_route_declarations_are_present(tmp_path: Path) -> None:
    """Stocktake: valid Flask route declarations should be discoverable."""
    _write_flask_sample_fixture(tmp_path / "flask_sample_app")
    result = run_llm_endpoint_discovery(
        _build_candidate_reads(),
        llm_client=_FakeDiscoveryClient(_mocked_flask_discovery_payload()),
    )

    route_keys = {(item.method, item.path, item.file) for item in result.endpoints}
    assert ("GET", "/", "app/routes.py") in route_keys
    assert ("GET", "/items", "app/routes.py") in route_keys
    assert ("GET", "/items/{item_id}", "app/routes.py") in route_keys
    assert ("POST", "/items", "app/routes.py") in route_keys


def test_flask_test_client_calls_are_not_route_declarations_regression(tmp_path: Path) -> None:
    """Regression: test-client call files must not be accepted as route declarations."""
    _write_flask_sample_fixture(tmp_path / "flask_sample_app")
    result = run_llm_endpoint_discovery(
        _build_candidate_reads(),
        llm_client=_FakeDiscoveryClient(_mocked_flask_discovery_payload()),
    )

    # Desired behavior (captured regression): no test files as declaring route files.
    assert all(item.file != "tests/test_app.py" for item in result.endpoints)
    paths = {item.path for item in result.endpoints}
    assert "/items/0" not in paths
    assert "/items/1" not in paths


def test_flask_discovery_pipeline_prefers_source_declarations_and_drops_test_calls(tmp_path: Path) -> None:
    """End-to-end discovery should include source declarations and exclude test invocations."""
    repo_root = tmp_path / "flask_sample_app"
    _write_flask_sample_fixture(repo_root)
    # Intentionally noisy LLM output: deterministic extraction should still ground valid declarations.
    llm_payload = {"endpoints": [{"method": "GET", "path": "/items/0", "file": "tests/test_app.py", "repo": "flask_sample_app"}]}
    result = discover_endpoints(
        [RepoRef(name="flask_sample_app", root=str(repo_root))],
        llm_client=_FakeDiscoveryClient(llm_payload),
        read_top_n=20,
        rank_top_k=100,
    )

    route_keys = {(item.method, item.path, item.file) for item in result.routes}
    assert ("GET", "/", "app/routes.py") in route_keys
    assert ("GET", "/items", "app/routes.py") in route_keys
    assert ("GET", "/items/{item_id}", "app/routes.py") in route_keys
    assert ("POST", "/items", "app/routes.py") in route_keys
    assert all(item.file != "tests/test_app.py" for item in result.routes)
    assert all(item.path not in {"/items/0", "/items/1"} for item in result.routes)
