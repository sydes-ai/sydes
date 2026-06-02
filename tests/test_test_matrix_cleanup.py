from __future__ import annotations

from sydes.core.models import (
    ApiRequestContract,
    ApiResponseContract,
    ApiRouteContract,
    ApiSchema,
    ApiSchemaProperty,
    IntegrationTestSuggestion,
    TestMatrix,
    TestMatrixGroup,
)
from sydes.generate.tests import clean_test_matrix, make_test_suggestion


def _names(matrix: TestMatrix) -> list[str]:
    return [test.name for group in matrix.groups for test in group.tests]


def _tests(matrix: TestMatrix) -> list[IntegrationTestSuggestion]:
    return [test for group in matrix.groups for test in group.tests]


def _contract() -> ApiRouteContract:
    return ApiRouteContract(
        method="POST",
        path="/items",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=["name", "price"],
                properties={
                    "name": ApiSchemaProperty(type="string", example="Notebook"),
                    "price": ApiSchemaProperty(type="number", example=12.5),
                    "email": ApiSchemaProperty(type="string", format="email", example="buyer@example.com"),
                },
            )
        ),
        responses={
            "201": ApiResponseContract(
                status="201",
                body=ApiSchema(
                    type="object",
                    properties={"id": ApiSchemaProperty(type="integer"), "name": ApiSchemaProperty(type="string")},
                ),
            ),
            "400": ApiResponseContract(status="400", body=ApiSchema(type="object", properties={"error": ApiSchemaProperty(type="string")})),
        },
    )


def _scenario(name: str, category: str, *, refs: list[str] | None = None, status: int | None = None) -> IntegrationTestSuggestion:
    return make_test_suggestion(
        name=name,
        route="/items",
        method="POST",
        summary=name.replace("_", " "),
        category=category,
        request={"method": "POST", "path": "/items", "body": {"name": "Notebook"}},
        expected={"status": status, "behavior": "ok"},
        contract_refs=refs or [],
    )


def test_happy_path_and_positive_groups_merge_into_positive() -> None:
    matrix = TestMatrix(
        groups=[
            TestMatrixGroup(category="happy_path", tests=[_scenario("post_items_returns_success", "happy_path")]),
            TestMatrixGroup(category="positive", tests=[_scenario("post_items_contract_happy_path", "positive", refs=["responses.201"], status=201)]),
        ]
    )

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert [group.category for group in cleaned.groups].count("positive") == 1
    assert "happy_path" not in [group.category for group in cleaned.groups]


def test_duplicate_weak_generic_happy_path_removed_when_contract_happy_exists() -> None:
    matrix = TestMatrix(
        groups=[
            TestMatrixGroup(
                category="positive",
                tests=[
                    _scenario("post_items_creates_resource", "positive", status=200),
                    _scenario("post_items_contract_happy_path", "positive", refs=["responses.201"], status=201),
                ],
            )
        ]
    )

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert "post_items_contract_happy_path" in _names(cleaned)
    assert "post_items_creates_resource" not in _names(cleaned)


def test_generic_missing_required_field_removed_when_field_specific_exists() -> None:
    matrix = TestMatrix(
        groups=[
            TestMatrixGroup(
                category="validation",
                tests=[
                    _scenario("post_items_rejects_missing_required_field", "validation", status=400),
                    _scenario("post_items_missing_name", "validation", refs=["request.body.name"], status=400),
                ],
            )
        ]
    )

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert "post_items_missing_name" in _names(cleaned)
    assert "post_items_rejects_missing_required_field" not in _names(cleaned)


def test_generic_invalid_payload_removed_when_field_specific_exists() -> None:
    matrix = TestMatrix(
        groups=[
            TestMatrixGroup(
                category="validation",
                tests=[
                    _scenario("post_items_rejects_invalid_payload", "validation", status=400),
                    _scenario("post_items_invalid_type_price", "validation", refs=["request.body.price"], status=400),
                ],
            )
        ]
    )

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert "post_items_invalid_type_price" in _names(cleaned)
    assert "post_items_rejects_invalid_payload" not in _names(cleaned)


def test_positive_status_fixed_from_200_to_201_for_post_contract() -> None:
    matrix = TestMatrix(groups=[TestMatrixGroup(category="positive", tests=[_scenario("post_items_contract_happy_path", "positive", refs=["responses.200"], status=200)])])

    cleaned = clean_test_matrix(matrix, api_contract=_contract())
    happy = next(test for test in _tests(cleaned) if test.name == "post_items_contract_happy_path")

    assert happy.expected is not None
    assert happy.expected["status"] == 201
    assert happy.expected["response_schema_ref"] == "responses.201"
    assert "responses.201" in happy.contract_refs


def test_required_field_scenarios_added_from_contract() -> None:
    matrix = TestMatrix(groups=[])

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert "post_items_missing_name" in _names(cleaned)
    assert "post_items_missing_price" in _names(cleaned)


def test_malformed_json_scenario_added_for_object_body() -> None:
    matrix = TestMatrix(groups=[])

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert "post_items_malformed_json" in _names(cleaned)


def test_response_schema_scenario_added_from_success_response() -> None:
    matrix = TestMatrix(groups=[])

    cleaned = clean_test_matrix(matrix, api_contract=_contract())
    scenario = next(test for test in _tests(cleaned) if test.name == "post_items_response_schema_validation")

    assert scenario.category == "response_schema"
    assert scenario.expected is not None
    assert scenario.expected["response_schema_ref"] == "responses.201"


def test_final_scenario_cap_preserves_core_categories() -> None:
    many = [
        _scenario(f"post_items_edge_case_{idx}", "edge_case", status=400)
        for idx in range(20)
    ]
    many.extend(
        [
            _scenario("post_items_contract_happy_path", "positive", refs=["responses.201"], status=201),
            _scenario("post_items_missing_name", "validation", refs=["request.body.name"], status=400),
            _scenario("post_items_response_schema_validation", "response_schema", refs=["responses.201"], status=201),
            make_test_suggestion(
                name="post_items_database_failure_handled",
                route="/items",
                method="POST",
                category="database",
                request={"method": "POST", "path": "/items"},
                expected={"status": 500},
                related_sinks=["database:items"],
                side_effects=["rollback or no partial write"],
            ),
        ]
    )
    matrix = TestMatrix(groups=[TestMatrixGroup(category="edge_case", tests=many)])

    cleaned = clean_test_matrix(matrix, api_contract=_contract())

    assert len(_tests(cleaned)) <= 12
    categories = {test.category for test in _tests(cleaned)}
    assert {"positive", "validation", "response_schema", "database"}.issubset(categories)


def test_cleanup_tolerates_old_style_suggestions() -> None:
    old_style = IntegrationTestSuggestion(name="get_status_happy_path", route="/status", method="GET")
    matrix = TestMatrix(groups=[TestMatrixGroup(category="happy_path", tests=[old_style])])

    cleaned = clean_test_matrix(matrix)

    assert _names(cleaned) == ["get_status_happy_path"]
    assert cleaned.groups[0].category == "positive"
