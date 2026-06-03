from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.core.models import (
    ApiContractArtifact,
    ApiRequestContract,
    ApiResponseContract,
    ApiRouteContract,
    ApiSchema,
    ApiSchemaProperty,
    ConfidenceSummary,
    EndpointCandidate,
    GraphEdge,
    GraphNode,
    RepoRef,
    RoutesResult,
    TargetSpec,
    TestMatrix as SydesTestMatrix,
    TestMatrixGroup as SydesTestMatrixGroup,
    TraceResult,
    TraceSummary,
)
from sydes.export.postman import POSTMAN_SCHEMA_URL, format_postman_collection
from sydes.generate.artifact_consistency import validate_artifact_consistency
from sydes.generate.contract_llm_refinement import ContractRefinementResult
from sydes.generate.test_llm_generation import (
    TestMatrixGenerationResult as _TestMatrixGenerationResult,
)
from sydes.generate.tests import clean_test_matrix, make_test_suggestion
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse, LLMValidationResult

runner = CliRunner()


@dataclass
class _FakeDiscoveryClient:
    payload: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "extract likely HTTP API route declarations" in request.prompt
        return LLMResponse(text=self.payload)


class _UnavailableFlowClient:
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("mock unavailable")


def _write_consistency_flask_fixture(repo_root: Path) -> None:
    (repo_root / "app").mkdir(parents=True, exist_ok=True)
    (repo_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo_root / "app" / "routes.py").write_text(
        "\n".join(
            [
                "from flask import Blueprint, request",
                "",
                "bp = Blueprint('items', __name__)",
                "items = []",
                "",
                "@bp.route('/items', methods=['POST'])",
                "def add_item():",
                "    data = request.get_json()",
                "    name = data.get('name')",
                "    price = data.get('price')",
                "",
                "    if not name:",
                "        return {'error': 'name is required'}, 400",
                "",
                "    if price is None:",
                "        return {'error': 'price is required'}, 400",
                "",
                "    item = {'id': len(items) + 1, 'name': name, 'price': price}",
                "    items.append(item)",
                "",
                "    return item, 201",
            ]
        ),
        encoding="utf-8",
    )


def _preflight_ok(monkeypatch) -> None:
    ok = LLMValidationResult(
        ok=True,
        provider="openai",
        model="gpt-4.1-mini",
        base_url="https://api.openai.com/v1",
    )
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


def _refined_contract() -> ApiRouteContract:
    return ApiRouteContract(
        method="POST",
        path="/items",
        repo="api",
        handler="add_item",
        file="app/routes.py",
        request=ApiRequestContract(
            body=ApiSchema(
                type="object",
                required=["name", "price"],
                properties={
                    "name": ApiSchemaProperty(type="string", required=True, example="Widget"),
                    "price": ApiSchemaProperty(type="number", required=True, example=9.99),
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
                body=ApiSchema(
                    type="object",
                    properties={"error": ApiSchemaProperty(type="string")},
                ),
            ),
        },
        confidence="medium",
        notes=["Refined from graph-grounded evidence packet."],
    )


def _llm_test_matrix() -> SydesTestMatrix:
    return SydesTestMatrix(
        groups=[
            SydesTestMatrixGroup(
                category="positive",
                tests=[
                    make_test_suggestion(
                        name="post_items_contract_happy_path",
                        route="/items",
                        method="POST",
                        summary="Valid item payload creates an item.",
                        category="positive",
                        priority="high",
                        purpose="Create item with contract-valid input.",
                        request={
                            "method": "POST",
                            "path": "/items",
                            "body": {"name": "Widget", "price": 9.99},
                        },
                        expected={
                            "status": 200,
                            "behavior": "returns created item with id, name, and price",
                            "response_schema_ref": "responses.200",
                        },
                        contract_refs=["responses.200", "request.body.name", "request.body.price"],
                        related_steps=["read JSON request body", "items.append(item)"],
                        related_sinks=["items.append(item)"],
                    ),
                    make_test_suggestion(
                        name="post_items_creates_resource",
                        route="/items",
                        method="POST",
                        summary="Generic happy path.",
                        category="positive",
                        expected={"status": None},
                    ),
                    make_test_suggestion(
                        name="post_items_contract_success",
                        route="/items",
                        method="POST",
                        summary="Contract-aware positive baseline.",
                        category="positive",
                        request={"method": "POST", "path": "/items", "body": {"name": "Widget", "price": 9.99}},
                        expected={"status": 201, "response_schema_ref": "responses.201"},
                        contract_refs=["responses.201"],
                    ),
                ],
            ),
            SydesTestMatrixGroup(
                category="validation",
                tests=[
                    make_test_suggestion(
                        name="post_items_missing_name",
                        route="/items",
                        method="POST",
                        summary="Missing name fails.",
                        category="validation",
                        request={"method": "POST", "path": "/items", "body": {"price": 9.99}},
                        expected={"status": 400, "behavior": "validation error"},
                        contract_refs=["request.body.name"],
                    ),
                    make_test_suggestion(
                        name="post_items_missing_price",
                        route="/items",
                        method="POST",
                        summary="Missing price fails.",
                        category="validation",
                        request={"method": "POST", "path": "/items", "body": {"name": "Widget"}},
                        expected={"status": 400, "behavior": "validation error"},
                        contract_refs=["request.body.price"],
                    ),
                    make_test_suggestion(
                        name="post_items_invalid_type_price",
                        route="/items",
                        method="POST",
                        summary="Price must be numeric.",
                        category="validation",
                        request={"method": "POST", "path": "/items", "body": {"name": "Widget", "price": "oops"}},
                        expected={"status": 400, "behavior": "validation error"},
                        contract_refs=["request.body.price"],
                    ),
                    make_test_suggestion(
                        name="post_items_rejects_invalid_payload",
                        route="/items",
                        method="POST",
                        summary="Generic invalid payload.",
                        category="validation",
                        expected={"status": None},
                    ),
                    make_test_suggestion(
                        name="post_items_malformed_json",
                        route="/items",
                        method="POST",
                        summary="Malformed JSON fails.",
                        category="validation",
                        request={"method": "POST", "path": "/items", "raw_body": "{bad-json"},
                        expected={"status": 400, "behavior": "validation error"},
                        contract_refs=["request.body"],
                    ),
                ],
            ),
            SydesTestMatrixGroup(
                category="response_schema",
                tests=[
                    make_test_suggestion(
                        name="post_items_response_schema_validation",
                        route="/items",
                        method="POST",
                        summary="Created response matches schema.",
                        category="response_schema",
                        expected={"status": 201, "response_schema_ref": "responses.201"},
                        contract_refs=["responses.201"],
                    )
                ],
            ),
            SydesTestMatrixGroup(
                category="side_effect",
                tests=[
                    make_test_suggestion(
                        name="post_items_store_append_side_effect",
                        route="/items",
                        method="POST",
                        summary="Store append happens on success.",
                        category="side_effect",
                        expected={"status": 201, "behavior": "item appended"},
                        related_sinks=["items.append(item)"],
                        side_effects=["item is appended to in-memory store"],
                    )
                ],
            ),
        ]
    )


def test_validate_artifact_consistency_detects_mismatches() -> None:
    contract = ApiContractArtifact(routes=[_refined_contract()])
    matrix = SydesTestMatrix(
        groups=[
            SydesTestMatrixGroup(
                category="positive",
                tests=[
                    make_test_suggestion(
                        name="post_items_contract_happy_path",
                        route="/items",
                        method="POST",
                        category="positive",
                        request={"method": "POST", "path": "/items"},
                        expected={"status": 200, "response_schema_ref": "responses.200"},
                        contract_refs=["responses.200"],
                    ),
                    make_test_suggestion(
                        name="post_items_contract_success",
                        route="/items",
                        method="POST",
                        category="positive",
                        request={"method": "POST", "path": "/items", "body": {"name": "Widget", "price": 9.99}},
                        expected={"status": 201, "response_schema_ref": "responses.201"},
                        contract_refs=["responses.201"],
                    ),
                    make_test_suggestion(
                        name="post_items_creates_resource",
                        route="/items",
                        method="POST",
                        category="positive",
                        expected={"status": None},
                    ),
                ],
            ),
            SydesTestMatrixGroup(
                category="positive",
                tests=[],
            ),
            SydesTestMatrixGroup(
                category="validation",
                tests=[
                    make_test_suggestion(
                        name="post_items_rejects_invalid_payload",
                        route="/items",
                        method="POST",
                        category="validation",
                        expected={"status": None},
                    ),
                ],
            ),
        ]
    )

    warnings = validate_artifact_consistency(contract, matrix)

    assert any("responses.200" in item for item in warnings)
    assert any("prefers 201" in item for item in warnings)
    assert any("duplicate test-matrix categories" in item for item in warnings)
    assert any("generic happy-path" in item for item in warnings)


def test_clean_test_matrix_rewrites_positive_status_and_refs() -> None:
    matrix = SydesTestMatrix(
        groups=[
            SydesTestMatrixGroup(
                category="positive",
                tests=[
                    make_test_suggestion(
                        name="post_items_contract_happy_path",
                        route="/items",
                        method="POST",
                        category="positive",
                        request={"method": "POST", "path": "/items", "body": {"name": "Widget", "price": 9.99}},
                        expected={"status": 200, "response_schema_ref": "responses.200"},
                        contract_refs=["responses.200", "request.body.name"],
                    )
                ],
            )
        ]
    )

    cleaned = clean_test_matrix(matrix, api_contract=_refined_contract())
    happy = cleaned.groups[0].tests[0]

    assert happy.expected is not None
    assert happy.expected["status"] == 201
    assert happy.expected["response_schema_ref"] == "responses.201"
    assert "responses.201" in happy.contract_refs
    assert "responses.200" not in happy.contract_refs


def test_clean_test_matrix_removes_weak_generic_scenarios() -> None:
    cleaned = clean_test_matrix(_llm_test_matrix(), api_contract=_refined_contract())
    names = {
        test.name
        for group in cleaned.groups
        for test in group.tests
    }
    categories = [group.category for group in cleaned.groups]

    assert categories.count("positive") == 1
    assert "post_items_creates_resource" not in names
    assert "post_items_rejects_invalid_payload" not in names
    assert "post_items_invalid_type_price" in names
    assert "post_items_missing_name" in names
    assert "post_items_missing_price" in names


def test_trace_output_artifacts_are_consistent_with_refined_contract_and_postman(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "api"
    _write_consistency_flask_fixture(repo_root)
    output_dir = tmp_path / "sydes-g4"
    output_dir.mkdir()
    _preflight_ok(monkeypatch)

    monkeypatch.setattr(
        "sydes.discover.endpoints.create_default_llm_client",
        lambda **_kwargs: _FakeDiscoveryClient(
            payload='{"endpoints":[{"method":"POST","path":"/items","handler":"add_item","file":"app/routes.py","repo":"api"}]}'
        ),
    )
    monkeypatch.setattr(
        "sydes.trace.expand.create_default_llm_client",
        lambda **_kwargs: _UnavailableFlowClient(),
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: tmp_path / f"{kwargs['artifact_name']}.json",
    )

    refined_contract = _refined_contract()
    monkeypatch.setattr(
        trace_module,
        "refine_api_contract_with_evidence_packet",
        lambda **_kwargs: ContractRefinementResult(
            ok=True,
            refined_contract=refined_contract,
            raw_output="{}",
            parsed_output=refined_contract.model_dump(mode="json"),
        ),
    )
    llm_matrix = _llm_test_matrix()
    monkeypatch.setattr(
        trace_module,
        "generate_test_matrix_with_evidence_packet",
        lambda **_kwargs: _TestMatrixGenerationResult(
            ok=True,
            test_matrix=llm_matrix,
            raw_output="{}",
            parsed_output=llm_matrix.model_dump(mode="json"),
            warnings=[],
        ),
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/items",
            "--method",
            "POST",
            "--repo",
            f"api={repo_root}",
            "--model",
            "openai:gpt-4.1-mini",
            "--trace-llm-policy",
            "always",
            "--allow-partial",
            "--format",
            "json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    for filename in (
        "trace_result.json",
        "api_contract.json",
        "test_matrix.json",
        "evidence_packet.json",
        "llm_contract_refinement.json",
        "llm_test_generation.json",
    ):
        assert (output_dir / filename).exists(), filename

    contract_payload = json.loads((output_dir / "api_contract.json").read_text(encoding="utf-8"))
    matrix_payload = json.loads((output_dir / "test_matrix.json").read_text(encoding="utf-8"))
    packet_payload = json.loads((output_dir / "evidence_packet.json").read_text(encoding="utf-8"))
    llm_contract_payload = json.loads((output_dir / "llm_contract_refinement.json").read_text(encoding="utf-8"))
    llm_test_payload = json.loads((output_dir / "llm_test_generation.json").read_text(encoding="utf-8"))

    assert packet_payload["endpoint"]["path"] == "/items"
    assert llm_contract_payload["ok"] is True
    assert llm_test_payload["ok"] is True

    contract = ApiContractArtifact.model_validate(contract_payload)
    matrix = SydesTestMatrix.model_validate(matrix_payload)
    warnings = validate_artifact_consistency(contract, matrix)
    assert warnings == []

    route = contract.routes[0]
    assert route.request.body is not None
    assert route.request.body.required == ["name", "price"]
    assert sorted(route.responses) == ["201", "400"]
    assert route.responses["201"].body is not None
    assert sorted(route.responses["201"].body.properties) == ["id", "name", "price"]
    assert route.responses["400"].body is not None
    assert "error" in route.responses["400"].body.properties

    names = {
        test.name
        for group in matrix.groups
        for test in group.tests
    }
    assert "post_items_contract_happy_path" in names
    assert "post_items_missing_name" in names
    assert "post_items_missing_price" in names
    assert "post_items_malformed_json" in names
    assert "post_items_response_schema_validation" in names
    assert "post_items_store_append_side_effect" in names
    assert "post_items_creates_resource" not in names
    assert "post_items_rejects_invalid_payload" not in names

    happy = next(
        test
        for group in matrix.groups
        for test in group.tests
        if test.name == "post_items_contract_happy_path"
    )
    assert happy.expected is not None
    assert happy.expected["status"] == 201
    assert happy.expected["response_schema_ref"] == "responses.201"
    assert "responses.200" not in happy.contract_refs

    collection = format_postman_collection(matrix, route_method="POST", route_path="/items")
    assert collection["info"]["schema"] == POSTMAN_SCHEMA_URL
    request_items = collection["item"][0]["item"]
    postman_happy = next(item for item in request_items if item["name"] == "post_items_contract_happy_path")
    assert postman_happy["request"]["method"] == "POST"
    body = json.loads(postman_happy["request"]["body"]["raw"])
    assert body["name"] == "Widget"
    assert body["price"] == 9.99
    assert any(
        item["name"] == "post_items_missing_name"
        for item in request_items
    )
