from __future__ import annotations

import json

from sydes.core.models import EndpointCandidate, RepoRef, RoutesResult
from sydes.generate.contracts import (
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

