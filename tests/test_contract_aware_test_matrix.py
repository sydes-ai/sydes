from __future__ import annotations

from sydes.core.models import (
    ApiRequestContract,
    ApiResponseContract,
    ApiRouteContract,
    ApiSchema,
    ApiSchemaProperty,
    GraphNode,
    TargetSpec,
    TraceResult,
    TraceSummary,
)
from sydes.generate.tests import generate_test_matrix
from sydes.generate.tests import match_route_contract
from sydes.core.models import ApiContractArtifact


def _names(matrix) -> list[str]:
    return [test.name for group in matrix.groups for test in group.tests]


def _find_test(matrix, name: str):
    for group in matrix.groups:
        for test in group.tests:
            if test.name == name:
                return test
    raise AssertionError(f"missing scenario: {name}")


def test_contract_required_field_generates_missing_field_validation() -> None:
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        nodes=[GraphNode(id="db", type="database", name="items", metadata={"action": "write"}, repo="api")],
        summary=TraceSummary(confidence=0.8),
    )
    contract = ApiRouteContract(
        method="POST",
        path="/items",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=["name"],
                properties={
                    "name": ApiSchemaProperty(type="string"),
                    "email": ApiSchemaProperty(type="string", format="email"),
                },
            )
        ),
        responses={"201": ApiResponseContract(status="201", body=ApiSchema(type="object", properties={}, required=[]))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    names = _names(matrix)
    assert "post_items_missing_required_name" in names
    missing = _find_test(matrix, "post_items_missing_required_name")
    assert missing.category == "validation"
    assert "request.body.name" in missing.contract_refs


def test_contract_happy_path_materializes_request_body_from_contract() -> None:
    trace = TraceResult(
        target=TargetSpec(path="/api/v1/clients/{id}", method="PUT"),
        summary=TraceSummary(confidence=0.8),
    )
    contract = ApiRouteContract(
        method="PUT",
        path="/api/v1/clients/{id}",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=[],
                properties={"name": ApiSchemaProperty(type="string", example="example")},
            )
        ),
        responses={"200": ApiResponseContract(status="200", body=ApiSchema(type="object", properties={}, required=[]))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    happy = _find_test(matrix, "put_api_v1_clients_id_contract_happy_path")
    assert happy.request is not None
    assert happy.request["method"] == "PUT"
    assert happy.request["path"] == "/api/v1/clients/{id}"
    assert happy.request["body"] == {"name": "example"}
    body_hint = next(item.value_hint for item in happy.inputs if item.kind == "request_body")
    assert body_hint == {"name": "example"}
    assert matrix.endpoint == {"method": "PUT", "path": "/api/v1/clients/{id}"}


def test_contract_email_generates_invalid_email_scenario() -> None:
    trace = TraceResult(target=TargetSpec(path="/items", method="POST"), summary=TraceSummary(confidence=0.8))
    contract = ApiRouteContract(
        method="POST",
        path="/items",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=[],
                properties={"email": ApiSchemaProperty(type="string", format="email")},
            )
        ),
        responses={"201": ApiResponseContract(status="201", body=ApiSchema(type="object", properties={}, required=[]))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    assert "post_items_invalid_email_email" in _names(matrix)


def test_auth_header_generates_missing_and_invalid_auth() -> None:
    trace = TraceResult(target=TargetSpec(path="/items", method="GET"), summary=TraceSummary(confidence=0.8))
    contract = ApiRouteContract(
        method="GET",
        path="/items",
        request=ApiRequestContract(
            headers={"Authorization": ApiSchemaProperty(type="string", required=True)}
        ),
        responses={"200": ApiResponseContract(status="200", body=ApiSchema(type="object", properties={}, required=[]))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    names = _names(matrix)
    assert "get_items_missing_authorization" in names
    assert "get_items_invalid_authorization" in names


def test_success_response_generates_response_schema_validation_case() -> None:
    trace = TraceResult(target=TargetSpec(path="/items", method="GET"), summary=TraceSummary(confidence=0.8))
    contract = ApiRouteContract(
        method="GET",
        path="/items",
        request=ApiRequestContract(),
        responses={
            "200": ApiResponseContract(
                status="200",
                body=ApiSchema(
                    type="object",
                    required=[],
                    properties={"id": ApiSchemaProperty(type="integer")},
                ),
            )
        },
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    test = _find_test(matrix, "get_items_response_schema_validation")
    assert test.expected is not None
    assert test.expected.get("response_schema_ref") == "responses.200"


def test_db_sink_generates_database_failure_scenario() -> None:
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        nodes=[GraphNode(id="db", type="database", name="items", metadata={"action": "write"}, repo="api")],
        summary=TraceSummary(confidence=0.8),
    )
    contract = ApiRouteContract(
        method="POST",
        path="/items",
        request=ApiRequestContract(),
        responses={"201": ApiResponseContract(status="201", body=ApiSchema(type="object", required=[], properties={}))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    test = _find_test(matrix, "post_items_database_failure_handled")
    assert test.requires_mocking is True
    assert test.category == "database"


def test_queue_sink_generates_publish_failure_scenario() -> None:
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        nodes=[GraphNode(id="q", type="queue", name="items-events", repo="api")],
        summary=TraceSummary(confidence=0.8),
    )
    contract = ApiRouteContract(
        method="POST",
        path="/items",
        request=ApiRequestContract(),
        responses={"201": ApiResponseContract(status="201", body=ApiSchema(type="object", required=[], properties={}))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    assert "post_items_queue_publish_failure_handled" in _names(matrix)


def test_contract_scenarios_are_deduplicated() -> None:
    trace = TraceResult(target=TargetSpec(path="/items", method="POST"), summary=TraceSummary(confidence=0.8))
    contract = ApiRouteContract(
        method="POST",
        path="/items",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=["name"],
                properties={"name": ApiSchemaProperty(type="string")},
            )
        ),
        responses={"201": ApiResponseContract(status="201", body=ApiSchema(type="object", required=[], properties={}))},
    )

    matrix = generate_test_matrix(trace, route_contract=contract)
    names = _names(matrix)
    assert names.count("post_items_missing_required_name") == 1


def test_no_contract_keeps_fallback_without_crash() -> None:
    trace = TraceResult(target=TargetSpec(path="/status", method="GET"), summary=TraceSummary(confidence=0.5))
    matrix = generate_test_matrix(trace, route_contract=None)
    assert matrix.groups


def test_match_route_contract_tolerates_param_style_differences() -> None:
    contract = ApiContractArtifact(
        routes=[
            ApiRouteContract(method="GET", path="/items/{item_id}", request=ApiRequestContract()),
        ]
    )
    assert match_route_contract(contract, method="GET", path="/items/:item_id") is not None
    assert match_route_contract(contract, method="GET", path="/items/<int:item_id>") is not None
