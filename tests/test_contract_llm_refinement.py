"""Tests for LLM-guided API contract refinement."""

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
    EvidenceSourceWindow,
)
from sydes.generate.contract_llm_refinement import refine_api_contract_with_evidence_packet
from sydes.llm.client import LLMRequest, LLMResponse


LAST_REQUEST: LLMRequest | None = None


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, request: LLMRequest) -> LLMResponse:
        global LAST_REQUEST
        LAST_REQUEST = request
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
                end_line=45,
                code=(
                    "def add_item():\n"
                    "    data = request.get_json()\n"
                    "    name = data.get('name')\n"
                    "    price = data.get('price')\n"
                    "    if not name:\n"
                    "        return {'error': 'name is required'}, 400\n"
                    "    if price is None:\n"
                    "        return {'error': 'price is required'}, 400\n"
                    "    item = {'id': len(items) +  1, 'name': name, 'price': price}\n"
                    "    items.append(item)\n"
                    "    return item, 201\n"
                ),
            )
        ],
    )


def _current_contract() -> ApiRouteContract:
    return ApiRouteContract(
        method="POST",
        path="/items",
        repo="api",
        handler="add_item",
        file="app.py",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                properties={
                    "name": ApiSchemaProperty(type="string"),
                    "price": ApiSchemaProperty(type="number"),
                },
            )
        ),
        responses={
            "400": ApiResponseContract(
                status=400,
                description="Validation error",
                body=ApiSchema(
                    type="object",
                    properties={"error": ApiSchemaProperty(type="string")},
                ),
            )
        },
    )


def _valid_payload() -> dict:
    return {
        "method": "POST",
        "path": "/items",
        "request": {
            "body": {
                "type": "object",
                "required": ["name", "price"],
                "properties": {
                    "name": {"type": "string", "required": True},
                    "price": {"type": "number", "required": True},
                },
                "additional_properties": True,
            }
        },
        "responses": {
            "201": {
                "status": "201",
                "description": "Created item response.",
                "body": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "price": {"type": "number"},
                    },
                },
                "confidence": "medium",
            }
        },
        "confidence": "medium",
        "notes": ["Inferred from validation branches and return item."],
    }


def test_valid_llm_json_refines_required_fields_and_201_response() -> None:
    result = refine_api_contract_with_evidence_packet(
        evidence_packet=_packet(),
        current_contract=_current_contract(),
        llm_client=FakeClient(json.dumps(_valid_payload())),
    )

    assert result.ok is True
    assert result.refined_contract is not None
    assert result.refined_contract.request.body is not None
    assert result.refined_contract.request.body.required == ["name", "price"]
    assert result.refined_contract.request.body.properties["name"].required is True
    assert "201" in result.refined_contract.responses
    assert "400" in result.refined_contract.responses
    assert result.refined_contract.evidence[-1].kind == "llm_graph_contract_refinement"
    assert LAST_REQUEST is not None
    assert "Return only valid JSON" in LAST_REQUEST.prompt


def test_fenced_json_output_parses() -> None:
    result = refine_api_contract_with_evidence_packet(
        evidence_packet=_packet(),
        current_contract=_current_contract(),
        llm_client=FakeClient("```json\n" + json.dumps(_valid_payload()) + "\n```"),
    )

    assert result.ok is True
    assert result.refined_contract is not None
    assert "201" in result.refined_contract.responses


def test_mismatched_method_or_path_is_rejected() -> None:
    payload = {**_valid_payload(), "path": "/other"}

    result = refine_api_contract_with_evidence_packet(
        evidence_packet=_packet(),
        current_contract=_current_contract(),
        llm_client=FakeClient(json.dumps(payload)),
    )

    assert result.ok is False
    assert result.error == "endpoint_mismatch"
    assert result.refined_contract is None


def test_invalid_json_returns_warning_and_keeps_original_contract() -> None:
    result = refine_api_contract_with_evidence_packet(
        evidence_packet=_packet(),
        current_contract=_current_contract(),
        llm_client=FakeClient("{not-json"),
    )

    assert result.ok is False
    assert result.error == "invalid_json"
    assert result.refined_contract is None
    assert result.warnings


def test_merge_preserves_existing_response_and_adds_new_response() -> None:
    result = refine_api_contract_with_evidence_packet(
        evidence_packet=_packet(),
        current_contract=_current_contract(),
        llm_client=FakeClient(json.dumps(_valid_payload())),
    )

    assert result.refined_contract is not None
    assert sorted(result.refined_contract.responses) == ["201", "400"]
    assert result.refined_contract.repo == "api"
    assert result.refined_contract.handler == "add_item"
