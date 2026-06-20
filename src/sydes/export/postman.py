"""Postman Collection v2.1 formatter for Sydes TestMatrix scenarios."""

from __future__ import annotations

import json
from typing import Any

from sydes.core.models import IntegrationTestSuggestion, TestMatrix

POSTMAN_SCHEMA_URL = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def normalize_postman_path(path: str | None) -> str:
    """Return normalized API path suitable for URL joining."""
    if not path:
        return "/"
    value = path.strip()
    if not value.startswith("/"):
        value = f"/{value}"
    while "//" in value:
        value = value.replace("//", "/")
    return value


def build_postman_url(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build Postman URL object with baseUrl variable and query values."""
    normalized_path = normalize_postman_path(path)
    path_segments = [segment for segment in normalized_path.split("/") if segment]
    raw = "{{baseUrl}}" + normalized_path

    query_items: list[dict[str, str]] = []
    if query:
        for key, value in query.items():
            query_items.append({"key": str(key), "value": str(value)})
        if query_items:
            raw += "?" + "&".join(f"{item['key']}={item['value']}" for item in query_items)

    payload: dict[str, Any] = {
        "raw": raw,
        "host": ["{{baseUrl}}"],
        "path": path_segments,
    }
    if query_items:
        payload["query"] = query_items
    return payload


def build_postman_headers(
    headers: dict[str, Any] | None,
    *,
    has_body: bool,
    needs_auth: bool,
) -> list[dict[str, str]]:
    """Build Postman header list from request/header hints."""
    out: list[dict[str, str]] = []
    merged = dict(headers or {})
    if needs_auth and "Authorization" not in merged:
        merged["Authorization"] = "Bearer {{authToken}}"
    if has_body and "Content-Type" not in merged:
        merged["Content-Type"] = "application/json"

    for key, value in merged.items():
        out.append({"key": str(key), "value": str(value)})
    return out


def json_body_to_raw(body: Any) -> str:
    """Render JSON body as pretty string for Postman raw mode."""
    return json.dumps(body if body is not None else {}, indent=2)


def scenario_expected_status(scenario: IntegrationTestSuggestion) -> str | None:
    """Extract expected status from v2 or fallback expectations."""
    if isinstance(scenario.expected, dict) and scenario.expected.get("status") is not None:
        return str(scenario.expected.get("status"))

    for expectation in scenario.expectations:
        if expectation.kind == "http_response":
            # existing expectation model has no status field; keep descriptive fallback
            return None
    return None


def scenario_description(scenario: IntegrationTestSuggestion) -> str:
    """Build concise request description for Postman item."""
    lines: list[str] = []
    if scenario.purpose:
        lines.append(f"Purpose: {scenario.purpose}")
    elif scenario.summary:
        lines.append(f"Purpose: {scenario.summary}")

    status = scenario_expected_status(scenario)
    if status:
        lines.append(f"Expected status: {status}")

    if isinstance(scenario.expected, dict):
        behavior = scenario.expected.get("behavior")
        schema_ref = scenario.expected.get("response_schema_ref")
        if behavior:
            lines.append(f"Expected behavior: {behavior}")
        if schema_ref:
            lines.append(f"Expected response schema: {schema_ref}")

    if scenario.side_effects:
        lines.append("Side effects: " + ", ".join(scenario.side_effects))
    if scenario.related_sinks:
        lines.append("Related sinks: " + ", ".join(scenario.related_sinks))
    if scenario.related_steps:
        lines.append("Related steps: " + ", ".join(scenario.related_steps))

    if scenario.contract_refs:
        lines.append("Contract refs: " + ", ".join(scenario.contract_refs))

    if scenario.notes_text:
        lines.append(f"Notes: {scenario.notes_text}")
    if scenario.notes:
        lines.append("Notes: " + "; ".join(scenario.notes))

    if scenario.requires_mocking:
        lines.append("Requires mocking or controlled test environment.")

    return "\n".join(lines).strip()


def _scenario_request_method(scenario: IntegrationTestSuggestion, route_method: str | None) -> str:
    if isinstance(scenario.request, dict) and scenario.request.get("method"):
        return str(scenario.request["method"]).upper()
    if scenario.method:
        return scenario.method.upper()
    if route_method:
        return route_method.upper()
    return "GET"


def _scenario_request_path(
    scenario: IntegrationTestSuggestion,
    route_path: str | None,
) -> str:
    if isinstance(scenario.request, dict) and scenario.request.get("path"):
        return normalize_postman_path(str(scenario.request["path"]))
    if scenario.route:
        return normalize_postman_path(scenario.route)
    return normalize_postman_path(route_path)


def _scenario_needs_auth(scenario: IntegrationTestSuggestion, headers: dict[str, Any] | None) -> bool:
    if headers and "Authorization" in headers:
        return True
    if (scenario.category or "").lower() in {"auth", "authorization"}:
        return True
    return any(ref == "request.headers.Authorization" for ref in scenario.contract_refs)


def _postman_item_for_scenario(
    scenario: IntegrationTestSuggestion,
    *,
    route_method: str | None,
    route_path: str | None,
) -> dict[str, Any]:
    request_payload = scenario.request if isinstance(scenario.request, dict) else {}
    method = _scenario_request_method(scenario, route_method)
    path = _scenario_request_path(scenario, route_path)
    headers = request_payload.get("headers") if isinstance(request_payload.get("headers"), dict) else {}
    query = request_payload.get("query") if isinstance(request_payload.get("query"), dict) else {}
    body = request_payload.get("body") if isinstance(request_payload, dict) else None
    raw_body = request_payload.get("raw_body") if isinstance(request_payload, dict) else None

    has_body = method in {"POST", "PUT", "PATCH", "DELETE"} and (body is not None or raw_body is not None)
    needs_auth = _scenario_needs_auth(scenario, headers)
    header_items = build_postman_headers(headers, has_body=has_body, needs_auth=needs_auth)

    request_obj: dict[str, Any] = {
        "method": method,
        "header": header_items,
        "url": build_postman_url(path, query=query),
        "description": scenario_description(scenario),
    }

    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        request_obj["body"] = {
            "mode": "raw",
            "raw": str(raw_body) if raw_body is not None else json_body_to_raw(body if body is not None else {}),
            "options": {"raw": {"language": "json"}},
        }

    name = scenario.name or f"{method} {path}"
    if scenario.requires_mocking:
        name = f"[Setup required] {name}"

    return {
        "name": name,
        "request": request_obj,
    }


def format_postman_collection(
    test_matrix: TestMatrix,
    route_method: str | None = None,
    route_path: str | None = None,
    collection_name: str | None = None,
) -> dict[str, Any]:
    """Convert Sydes TestMatrix into Postman Collection v2.1 payload."""
    endpoint_title = f"{(route_method or 'ANY').upper()} {normalize_postman_path(route_path)}"
    folder_name = endpoint_title if route_path else "Endpoint Scenarios"

    items: list[dict[str, Any]] = []
    for group in test_matrix.groups:
        for scenario in group.tests:
            items.append(
                _postman_item_for_scenario(
                    scenario,
                    route_method=route_method,
                    route_path=route_path,
                )
            )

    return {
        "info": {
            "name": collection_name or (f"Sydes: {endpoint_title}" if route_path else "Sydes Generated Collection"),
            "schema": POSTMAN_SCHEMA_URL,
            "description": "Generated by Sydes from API trace/test matrix.",
        },
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000"},
            {"key": "authToken", "value": "replace-me"},
        ],
        "item": [
            {
                "name": folder_name,
                "item": items,
            }
        ],
    }


def render_postman_collection_json(
    test_matrix: TestMatrix,
    route_method: str | None = None,
    route_path: str | None = None,
    collection_name: str | None = None,
) -> str:
    """Render Postman collection JSON as pretty-formatted text."""
    collection = format_postman_collection(
        test_matrix,
        route_method=route_method,
        route_path=route_path,
        collection_name=collection_name,
    )
    return json.dumps(collection, indent=2)
