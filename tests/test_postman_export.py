from __future__ import annotations

import json
from pathlib import Path

from sydes.core.models import TestMatrix as SydesTestMatrix
from sydes.export.postman import (
    POSTMAN_SCHEMA_URL,
    format_postman_collection,
    render_postman_collection_json,
)


def _load_v2_matrix() -> TestMatrix:
    fixture_path = Path("fixtures/artifacts/test_matrix_v2.json")
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return SydesTestMatrix.model_validate(payload)


def test_postman_collection_has_v21_schema_and_variables() -> None:
    matrix = _load_v2_matrix()
    collection = format_postman_collection(matrix, route_method="POST", route_path="/items")

    assert collection["info"]["schema"] == POSTMAN_SCHEMA_URL
    variables = {item["key"]: item["value"] for item in collection["variable"]}
    assert variables["baseUrl"] == "http://localhost:8000"
    assert variables["authToken"] == "replace-me"


def test_post_scenario_with_body_exports_raw_json_body() -> None:
    matrix = _load_v2_matrix()
    collection = format_postman_collection(matrix, route_method="POST", route_path="/items")

    request_items = collection["item"][0]["item"]
    target = next(item for item in request_items if item["name"] == "post_items_creates_item")
    request = target["request"]
    assert request["method"] == "POST"
    assert request["body"]["mode"] == "raw"
    parsed = json.loads(request["body"]["raw"])
    assert parsed["name"] == "Widget"
    assert parsed["price"] == 9.99


def test_get_scenario_does_not_include_body() -> None:
    matrix = SydesTestMatrix.model_validate(
        {
            "groups": [
                {
                    "category": "positive",
                    "tests": [
                        {
                            "name": "get_items_happy",
                            "route": "/items",
                            "method": "GET",
                            "request": {"method": "GET", "path": "/items", "query": {"page": 2}},
                        }
                    ],
                }
            ]
        }
    )

    collection = format_postman_collection(matrix, route_method="GET", route_path="/items")
    request = collection["item"][0]["item"][0]["request"]
    assert request["method"] == "GET"
    assert "body" not in request


def test_auth_scenario_includes_authorization_header() -> None:
    matrix = _load_v2_matrix()
    collection = format_postman_collection(matrix, route_method="POST", route_path="/items")
    request_items = collection["item"][0]["item"]
    target = next(item for item in request_items if item["name"] == "post_items_requires_authorization")
    headers = {item["key"]: item["value"] for item in target["request"]["header"]}
    assert headers["Authorization"] == "Bearer {{authToken}}"


def test_query_params_are_exported() -> None:
    matrix = SydesTestMatrix.model_validate(
        {
            "groups": [
                {
                    "category": "edge_case",
                    "tests": [
                        {
                            "name": "get_items_with_query",
                            "route": "/items",
                            "method": "GET",
                            "request": {"method": "GET", "path": "/items", "query": {"limit": 10, "page": 1}},
                        }
                    ],
                }
            ]
        }
    )

    collection = format_postman_collection(matrix, route_method="GET", route_path="/items")
    url = collection["item"][0]["item"][0]["request"]["url"]
    assert url["raw"].startswith("{{baseUrl}}/items?")
    keys = {item["key"] for item in url["query"]}
    assert keys == {"limit", "page"}


def test_setup_required_scenario_marked_in_name_and_description() -> None:
    matrix = _load_v2_matrix()
    collection = format_postman_collection(matrix, route_method="POST", route_path="/items")
    request_items = collection["item"][0]["item"]
    target = next(item for item in request_items if "post_items_database_insert_failure" in item["name"])
    assert target["name"].startswith("[Setup required]")
    assert "Requires mocking or controlled test environment." in target["request"]["description"]


def test_expected_status_appears_in_description() -> None:
    matrix = _load_v2_matrix()
    collection = format_postman_collection(matrix, route_method="POST", route_path="/items")
    request_items = collection["item"][0]["item"]
    target = next(item for item in request_items if item["name"] == "post_items_creates_item")
    assert "Expected status: 201" in target["request"]["description"]


def test_render_postman_collection_json_is_pretty_json() -> None:
    matrix = _load_v2_matrix()
    rendered = render_postman_collection_json(matrix, route_method="POST", route_path="/items")
    assert rendered.startswith("{\n")
    payload = json.loads(rendered)
    assert payload["info"]["schema"] == POSTMAN_SCHEMA_URL


def test_postman_export_uses_materialized_contract_body() -> None:
    matrix = SydesTestMatrix.model_validate(
        {
            "groups": [
                {
                    "category": "positive",
                    "tests": [
                        {
                            "name": "put_clients_contract_happy_path",
                            "route": "/api/v1/clients/{id}",
                            "method": "PUT",
                            "request": {
                                "method": "PUT",
                                "path": "/api/v1/clients/{id}",
                                "body": {"name": "example"},
                            },
                        }
                    ],
                }
            ]
        }
    )

    collection = format_postman_collection(matrix, route_method="PUT", route_path="/api/v1/clients/{id}")
    request = collection["item"][0]["item"][0]["request"]
    assert json.loads(request["body"]["raw"]) == {"name": "example"}


def test_postman_export_preserves_malformed_raw_body() -> None:
    matrix = SydesTestMatrix.model_validate(
        {
            "groups": [
                {
                    "category": "validation",
                    "tests": [
                        {
                            "name": "put_clients_malformed_json",
                            "route": "/api/v1/clients/{id}",
                            "method": "PUT",
                            "request": {
                                "method": "PUT",
                                "path": "/api/v1/clients/{id}",
                                "raw_body": "{malformed-json",
                            },
                        }
                    ],
                }
            ]
        }
    )

    collection = format_postman_collection(matrix, route_method="PUT", route_path="/api/v1/clients/{id}")
    request = collection["item"][0]["item"][0]["request"]
    assert request["body"]["raw"] == "{malformed-json"
