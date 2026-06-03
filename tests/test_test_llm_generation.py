"""Tests for LLM-guided test matrix generation from evidence packets."""

from __future__ import annotations

import json

from sydes.core.models import (
    ApiRequestContract,
    ApiResponseContract,
    ApiRouteContract,
    ApiSchema,
    ApiSchemaProperty,
    EvidenceEndpoint,
    EvidencePacket,
    EvidenceSink,
    EvidenceSourceWindow,
    EvidenceTraceNode,
    TestMatrix as SydesTestMatrix,
    TestMatrixGroup as SydesTestMatrixGroup,
)
from sydes.generate.test_llm_generation import (
    build_test_matrix_generation_prompt,
    generate_test_matrix_with_evidence_packet,
)
from sydes.generate.tests import make_test_suggestion
from sydes.llm.client import LLMRequest, LLMResponse


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.last_request: LLMRequest | None = None

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.last_request = request
        return LLMResponse(text=self.text)


def _packet() -> EvidencePacket:
    return EvidencePacket(
        endpoint=EvidenceEndpoint(
            method="POST",
            path="/items",
            repo="api",
            handler="add_item",
            file="app.py",
        ),
        source_windows=[
            EvidenceSourceWindow(
                repo="api",
                file="app.py",
                symbol="add_item",
                start_line=1,
                end_line=20,
                code=(
                    "def add_item():\n"
                    "    data = request.get_json()\n"
                    "    name = data.get('name')\n"
                    "    price = data.get('price')\n"
                    "    item = {'id': len(items) + 1, 'name': name, 'price': price}\n"
                    "    items.append(item)\n"
                    "    return item, 201\n"
                ),
            )
        ],
        trace_nodes=[
            EvidenceTraceNode(
                id="step-1",
                type="internal_step",
                name="read JSON request body",
                kind="request_input",
                repo="api",
                file="app.py",
                symbol="add_item",
                snippet="data = request.get_json()",
                confidence=0.9,
            ),
            EvidenceTraceNode(
                id="step-2",
                type="internal_step",
                name="append item",
                kind="side_effect",
                repo="api",
                file="app.py",
                symbol="add_item",
                snippet="items.append(item)",
                confidence=0.9,
            ),
        ],
        sinks=[
            EvidenceSink(
                name="items.append(item)",
                kind="store_write",
                repo="api",
                file="app.py",
                symbol="add_item",
                snippet="items.append(item)",
                confidence=0.9,
            )
        ],
    )


def _contract() -> ApiRouteContract:
    return ApiRouteContract(
        method="POST",
        path="/items",
        repo="api",
        handler="add_item",
        file="app.py",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=["name", "price"],
                properties={
                    "name": ApiSchemaProperty(type="string", example="notebook"),
                    "price": ApiSchemaProperty(type="number", example=9.99),
                    "email": ApiSchemaProperty(type="string", format="email", example="buyer@example.com"),
                },
            )
        ),
        responses={
            "201": ApiResponseContract(
                status=201,
                body=ApiSchema(
                    type="object",
                    properties={
                        "id": ApiSchemaProperty(type="integer"),
                        "name": ApiSchemaProperty(type="string"),
                        "price": ApiSchemaProperty(type="number"),
                    },
                ),
            ),
            "400": ApiResponseContract(
                status=400,
                body=ApiSchema(type="object", properties={"error": ApiSchemaProperty(type="string")}),
            ),
        },
    )


def _current_matrix() -> SydesTestMatrix:
    return SydesTestMatrix(
        groups=[
            SydesTestMatrixGroup(
                category="validation",
                tests=[
                    make_test_suggestion(
                        name="post_items_rejects_invalid_payload",
                        route="/items",
                        method="POST",
                        summary="verifies POST /items rejects invalid payloads",
                        category="validation",
                        request={"method": "POST", "path": "/items"},
                        expected={"status": 400},
                    )
                ],
            )
        ]
    )


def _valid_payload() -> dict:
    return {
        "groups": [
            {
                "category": "positive",
                "tests": [
                    {
                        "name": "post_items_valid_item_creates_resource",
                        "route": "/items",
                        "method": "POST",
                        "summary": "Valid item payload creates an item.",
                        "category": "positive",
                        "priority": "high",
                        "purpose": "Verify POST /items accepts contract-valid input and returns created item response.",
                        "request": {
                            "method": "POST",
                            "path": "/items",
                            "headers": {},
                            "query": {},
                            "body": {"name": "example", "price": 9.99},
                        },
                        "expected": {
                            "status": 201,
                            "behavior": "returns created item with id, name, and price",
                            "response_schema_ref": "responses.201",
                        },
                        "side_effects": ["item is appended to in-memory store"],
                        "related_steps": ["read JSON request body", "append item"],
                        "related_sinks": ["items.append(item)"],
                        "contract_refs": ["request.body.name", "request.body.price", "responses.201"],
                        "requires_mocking": False,
                        "notes_text": "Grounded in request body fields and returned item object.",
                        "evidence": [
                            {
                                "kind": "trace_node",
                                "source": "evidence_packet",
                                "confidence": "medium",
                                "notes": ["data = request.get_json()", "items.append(item)", "return item, 201"],
                            }
                        ],
                    }
                ],
            },
            {
                "category": "validation",
                "tests": [
                    {
                        "name": "post_items_invalid_type_price",
                        "route": "/items",
                        "method": "POST",
                        "summary": "Rejects non-numeric price values.",
                        "category": "validation",
                        "priority": "high",
                        "purpose": "Price should be numeric.",
                        "request": {"method": "POST", "path": "/items", "body": {"price": "oops"}},
                        "expected": {"status": 400, "behavior": "validation error"},
                        "side_effects": ["No database write should occur"],
                        "related_steps": ["read JSON request body"],
                        "related_sinks": ["items.append(item)"],
                        "contract_refs": ["request.body.price"],
                        "requires_mocking": False,
                        "notes_text": "Grounded in contract field typing.",
                        "evidence": [{"kind": "contract", "source": "api_contract", "confidence": "medium"}],
                    }
                ],
            },
        ],
        "notes": ["Generated from graph-grounded evidence packet."],
        "coverage": 0.8,
        "confidence": 0.75,
    }


def test_prompt_includes_strict_json_instruction() -> None:
    prompt = build_test_matrix_generation_prompt(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=_current_matrix(),
    )

    assert "Return strict JSON only" in prompt
    assert "Do not duplicate existing scenarios" in prompt


def test_valid_llm_json_produces_merged_cleaned_matrix() -> None:
    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=_current_matrix(),
        llm_client=_FakeClient(json.dumps(_valid_payload())),
    )

    assert result.ok is True
    assert result.test_matrix is not None
    names = [test.name for group in result.test_matrix.groups for test in group.tests]
    assert "post_items_valid_item_creates_resource" in names
    assert "post_items_invalid_type_price" in names
    assert "post_items_rejects_invalid_payload" not in names
    positive = next(test for group in result.test_matrix.groups for test in group.tests if test.name == "post_items_valid_item_creates_resource")
    assert positive.expected is not None
    assert positive.expected["status"] == 201


def test_fenced_json_output_parses() -> None:
    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=_current_matrix(),
        llm_client=_FakeClient("```json\n" + json.dumps(_valid_payload()) + "\n```"),
    )

    assert result.ok is True
    assert result.test_matrix is not None


def test_invalid_json_returns_warning_without_matrix() -> None:
    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=_current_matrix(),
        llm_client=_FakeClient("{not-json"),
    )

    assert result.ok is False
    assert result.test_matrix is None
    assert result.error == "invalid_json"
    assert result.warnings


def test_unrelated_method_or_path_scenarios_are_rejected() -> None:
    payload = _valid_payload()
    payload["groups"][0]["tests"][0]["route"] = "/other"
    payload["groups"][0]["tests"][0]["request"]["path"] = "/other"

    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=_current_matrix(),
        llm_client=_FakeClient(json.dumps(payload)),
    )

    assert result.ok is True
    assert result.test_matrix is not None
    names = [test.name for group in result.test_matrix.groups for test in group.tests]
    assert "post_items_valid_item_creates_resource" not in names
    assert any("endpoint mismatch" in warning for warning in result.warnings)


def test_final_matrix_stays_clean_and_contract_enriched() -> None:
    payload = {
        "groups": [
            {
                "category": "positive",
                "tests": [
                    {
                        "name": "post_items_contract_happy_path",
                        "route": "/items",
                        "method": "POST",
                        "summary": "Happy path create.",
                        "category": "positive",
                        "purpose": "Create item.",
                        "request": {"method": "POST", "path": "/items", "body": {"name": "ok", "price": 1}},
                        "expected": {"status": 200},
                        "contract_refs": ["responses.201"],
                        "related_steps": ["read JSON request body"],
                        "related_sinks": ["items.append(item)"],
                        "evidence": [{"kind": "trace_node", "source": "evidence_packet"}],
                    }
                ],
            }
        ]
    }

    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=SydesTestMatrix(groups=[]),
        llm_client=_FakeClient(json.dumps(payload)),
    )

    assert result.ok is True
    assert result.test_matrix is not None
    names = [test.name for group in result.test_matrix.groups for test in group.tests]
    assert "post_items_missing_name" in names
    happy = next(test for group in result.test_matrix.groups for test in group.tests if test.name == "post_items_contract_happy_path")
    assert happy.expected is not None
    assert happy.expected["status"] == 201
    assert happy.expected["response_schema_ref"] == "responses.201"


def test_llm_generated_response_alias_refs_are_normalized_after_cleanup() -> None:
    payload = {
        "groups": [
            {
                "category": "positive",
                "tests": [
                    {
                        "name": "post_items_contract_happy_path",
                        "route": "/items",
                        "method": "POST",
                        "summary": "Happy path create.",
                        "category": "positive",
                        "purpose": "Create item.",
                        "request": {"method": "POST", "path": "/items", "body": {"name": "ok", "price": 1}},
                        "expected": {"status": 200, "response_schema_ref": "response.201.body"},
                        "contract_refs": ["response.201.body", "request.body.name"],
                        "related_steps": ["read JSON request body"],
                        "related_sinks": ["items.append(item)"],
                        "evidence": [{"kind": "trace_node", "source": "evidence_packet"}],
                    }
                ],
            }
        ]
    }

    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=_contract(),
        current_test_matrix=SydesTestMatrix(groups=[]),
        llm_client=_FakeClient(json.dumps(payload)),
    )

    assert result.ok is True
    assert result.test_matrix is not None
    happy = next(
        test
        for group in result.test_matrix.groups
        for test in group.tests
        if test.name == "post_items_contract_happy_path"
    )
    assert happy.expected is not None
    assert happy.expected["response_schema_ref"] == "responses.201"
    assert "responses.201" in happy.contract_refs
    assert "responses.201.body" not in happy.contract_refs


def test_no_contract_keeps_valid_grounded_positive_scenario() -> None:
    payload = {
        "groups": [
            {
                "category": "positive",
                "tests": [
                    {
                        "name": "post_items_valid_item_creates_resource",
                        "route": "/items",
                        "method": "POST",
                        "summary": "Valid item payload creates an item.",
                        "category": "positive",
                        "purpose": "Create item from bounded evidence.",
                        "request": {"method": "POST", "path": "/items", "body": {"name": "example", "price": 9.99}},
                        "expected": {"status": 201},
                        "related_steps": ["read JSON request body"],
                        "related_sinks": ["items.append(item)"],
                        "evidence": [{"kind": "trace_node", "source": "evidence_packet"}],
                    }
                ],
            }
        ]
    }

    result = generate_test_matrix_with_evidence_packet(
        evidence_packet=_packet(),
        api_contract=None,
        current_test_matrix=SydesTestMatrix(groups=[]),
        llm_client=_FakeClient(json.dumps(payload)),
    )

    assert result.ok is True
    assert result.test_matrix is not None
