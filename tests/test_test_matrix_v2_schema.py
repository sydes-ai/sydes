from __future__ import annotations

from sydes.core.models import (
    IntegrationTestSuggestion,
    TestMatrix as SydesTestMatrix,
    TestMatrixGroup as SydesTestMatrixGroup,
)
from sydes.generate.tests import (
    make_test_suggestion,
    normalize_contract_ref,
    normalize_test_matrix,
    scenario_id_from_name,
)


def test_old_style_integration_test_suggestion_still_constructs() -> None:
    suggestion = IntegrationTestSuggestion(name="get_items_happy_path", route="/items", method="GET")
    assert suggestion.name == "get_items_happy_path"
    assert suggestion.route == "/items"


def test_v2_fields_serialize_when_present() -> None:
    suggestion = IntegrationTestSuggestion(
        name="post_items_creates_item",
        route="/items",
        method="POST",
        category="positive",
        priority="high",
        request={"method": "POST", "path": "/items", "body": {"name": "Widget"}},
        expected={"status": 201, "response_schema_ref": "responses.201"},
        side_effects=["database insert"],
        related_steps=["insert item"],
        related_sinks=["database:items"],
        contract_refs=["request.body.name", "responses.201"],
        requires_mocking=False,
        notes_text="fixture note",
        evidence=[{"kind": "trace_step", "label": "insert item"}],
    )
    payload = suggestion.model_dump(exclude_none=True)
    assert payload["category"] == "positive"
    assert payload["priority"] == "high"
    assert payload["expected"]["status"] == 201
    assert payload["contract_refs"] == ["request.body.name", "responses.201"]


def test_default_list_fields_are_not_shared() -> None:
    first = IntegrationTestSuggestion(name="a", route="/a")
    second = IntegrationTestSuggestion(name="b", route="/b")
    first.side_effects.append("db write")
    first.related_steps.append("step-1")
    first.related_sinks.append("database")
    first.contract_refs.append("responses.200")
    first.evidence.append({"kind": "x"})

    assert second.side_effects == []
    assert second.related_steps == []
    assert second.related_sinks == []
    assert second.contract_refs == []
    assert second.evidence == []


def test_make_test_suggestion_applies_v2_defaults() -> None:
    suggestion = make_test_suggestion(
        name="post_items_creates_item",
        route="/items",
        method="POST",
        summary="creates item",
    )
    assert suggestion.category == "positive"
    assert suggestion.priority == "medium"
    assert suggestion.request is not None
    assert suggestion.request["method"] == "POST"
    assert suggestion.side_effects == []
    assert suggestion.related_steps == []
    assert suggestion.related_sinks == []
    assert suggestion.contract_refs == []


def test_normalize_test_matrix_backfills_category_from_group() -> None:
    matrix = SydesTestMatrix(
        groups=[
            SydesTestMatrixGroup(
                category="validation",
                tests=[IntegrationTestSuggestion(name="post_items_rejects_missing_name", route="/items", method="POST")],
            )
        ]
    )
    normalized = normalize_test_matrix(matrix)
    test = normalized.groups[0].tests[0]
    assert test.category == "validation"
    assert test.priority == "medium"


def test_scenario_id_from_name_is_stable() -> None:
    assert scenario_id_from_name("POST /items creates item") == "post_items_creates_item"
    assert scenario_id_from_name("POST /items creates item") == scenario_id_from_name("POST /items creates item")


def test_normalize_contract_ref_fixes_response_aliases_only() -> None:
    assert normalize_contract_ref("response.201.body") == "responses.201.body"
    assert normalize_contract_ref("response.400.body.error") == "responses.400.body.error"
    assert normalize_contract_ref("responses.201") == "responses.201"
    assert normalize_contract_ref("request.body.name") == "request.body.name"
