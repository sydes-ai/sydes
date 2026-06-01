from __future__ import annotations

import json

from sydes.core.models import EndpointCandidate, RepoRef, RoutesResult
from sydes.generate.contracts import (
    build_api_contract_from_routes,
    build_basic_api_contract_from_routes,
    infer_path_params,
    render_api_contract_json,
)


def test_infer_path_params_brace_style() -> None:
    params = infer_path_params("/items/{item_id}")
    assert "item_id" in params
    assert params["item_id"].required is True


def test_infer_path_params_colon_style() -> None:
    params = infer_path_params("/items/:item_id")
    assert "item_id" in params


def test_infer_path_params_flask_style() -> None:
    params = infer_path_params("/items/<int:item_id>")
    assert "item_id" in params
    assert params["item_id"].type == "integer"


def test_basic_contract_creates_route_per_endpoint() -> None:
    routes = RoutesResult(
        repos=[RepoRef(name="api", root="/tmp/api")],
        routes=[
            EndpointCandidate(method="GET", path="/items", file="app/routes.py", repo="api"),
            EndpointCandidate(method="GET", path="/items/{item_id}", file="app/routes.py", repo="api"),
            EndpointCandidate(method="POST", path="/items", file="app/routes.py", repo="api"),
        ],
    )
    contract = build_basic_api_contract_from_routes(routes)
    assert len(contract.routes) == 3
    post_contract = next(item for item in contract.routes if item.method == "POST")
    assert post_contract.request.body is not None
    assert post_contract.request.body.type == "object"


def test_contract_serializes_json() -> None:
    routes = RoutesResult(
        routes=[EndpointCandidate(method="GET", path="/items/{item_id}", file="app/routes.py", repo="api")]
    )
    contract = build_basic_api_contract_from_routes(routes)
    rendered = render_api_contract_json(contract)
    payload = json.loads(rendered)
    assert payload["version"] == "v1"
    assert payload["routes"][0]["request"]["path_params"]["item_id"]["type"] == "string"


def test_build_contract_extracts_flask_request_fields_and_required(tmp_path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    handler_file = repo_root / "app.py"
    handler_file.write_text(
        """
from flask import request, jsonify

def create_item():
    data = request.get_json()
    name = data.get("name")
    email = data.get("email")
    sku = data["sku"]
    if not data.get("email"):
        return jsonify({"error": "email required"}), 400
    return jsonify({"id": 123, "name": name}), 201
""".strip(),
        encoding="utf-8",
    )

    routes = RoutesResult(
        repos=[RepoRef(name="api", root=str(repo_root))],
        routes=[EndpointCandidate(method="POST", path="/items", file="app.py", repo="api", handler="create_item")],
    )

    contract = build_api_contract_from_routes(routes, repo_roots={"api": str(repo_root)})
    route_contract = contract.routes[0]
    assert route_contract.request.body is not None
    body = route_contract.request.body
    assert "name" in body.properties
    assert "email" in body.properties
    assert "sku" in body.properties
    assert "email" in body.required
    assert "sku" in body.required
    assert body.properties["email"].format == "email"


def test_build_contract_extracts_query_and_headers(tmp_path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    handler_file = repo_root / "routes.py"
    handler_file.write_text(
        """
from flask import request, jsonify

def list_items():
    q = request.args.get("q")
    limit = request.args.get("limit", 10)
    page = request.args["page"]
    token = request.headers.get("Authorization")
    req_id = request.headers["X-Request-ID"]
    return jsonify({"q": q, "limit": limit, "page": page})
""".strip(),
        encoding="utf-8",
    )

    routes = RoutesResult(
        repos=[RepoRef(name="api", root=str(repo_root))],
        routes=[EndpointCandidate(method="GET", path="/items", file="routes.py", repo="api", handler="list_items")],
    )

    contract = build_api_contract_from_routes(routes, repo_roots={"api": str(repo_root)})
    route_contract = contract.routes[0]
    assert route_contract.request.query_params["q"].required is False
    assert route_contract.request.query_params["limit"].type == "integer"
    assert route_contract.request.query_params["page"].required is True
    assert route_contract.request.headers["Authorization"].required is True
    assert route_contract.request.headers["Authorization"].example == "Bearer {{authToken}}"
    assert route_contract.request.headers["X-Request-ID"].required is True


def test_build_contract_extracts_jsonify_and_error_response(tmp_path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    handler_file = repo_root / "items.py"
    handler_file.write_text(
        """
from flask import jsonify

def get_item():
    if True:
        return jsonify({"error": "Item not found"}), 404
    return jsonify({"id": 1, "name": "chair"}), 200
""".strip(),
        encoding="utf-8",
    )

    routes = RoutesResult(
        repos=[RepoRef(name="api", root=str(repo_root))],
        routes=[EndpointCandidate(method="GET", path="/items/{item_id}", file="items.py", repo="api", handler="get_item")],
    )

    contract = build_api_contract_from_routes(routes, repo_roots={"api": str(repo_root)})
    route_contract = contract.routes[0]
    assert "404" in route_contract.responses
    assert "200" in route_contract.responses
    assert "error" in route_contract.responses["404"].body.properties
    assert "id" in route_contract.responses["200"].body.properties


def test_build_contract_falls_back_when_file_missing() -> None:
    routes = RoutesResult(
        repos=[RepoRef(name="api", root="/tmp/does-not-exist")],
        routes=[EndpointCandidate(method="POST", path="/items", file="missing.py", repo="api", handler="create")],
    )

    contract = build_api_contract_from_routes(routes, repo_roots={"api": "/tmp/does-not-exist"})
    route_contract = contract.routes[0]
    assert route_contract.request.body is not None
    assert route_contract.responses
    assert any("source" in note.lower() or "scaffold" in note.lower() for note in route_contract.notes)
