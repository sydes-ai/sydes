"""Tests for graph-grounded evidence packet construction."""

from __future__ import annotations

import json
from pathlib import Path

from sydes.core.models import (
    ApiContractArtifact,
    ApiRequestContract,
    ApiRouteContract,
    ApiSchema,
    ApiSchemaProperty,
    ApiResponseContract,
    EndpointCandidate,
    EvidenceRef,
    Flow,
    FlowStep,
    GraphEdge,
    GraphNode,
    IntegrationTestSuggestion,
    RepoRef,
    TargetSpec,
    TestMatrix as SydesTestMatrix,
    TestMatrixGroup as SydesTestMatrixGroup,
    TraceResult,
    TraceSummary,
)
from sydes.generate.evidence_packet import (
    build_evidence_packet_for_route,
    render_evidence_packet_json,
)


def _trace(repo_root: Path, file: str = "app.py") -> TraceResult:
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        repos=[RepoRef(name="api", root=str(repo_root))],
        matched_endpoint=EndpointCandidate(
            method="POST",
            path="/items",
            handler="add_item",
            file=file,
            repo="api",
        ),
        nodes=[
            GraphNode(
                id="endpoint",
                type="endpoint",
                name="POST /items",
                method="POST",
                path="/items",
                repo="api",
            ),
            GraphNode(
                id="body",
                type="request_input",
                name="read JSON request body",
                repo="api",
                file=file,
                symbol="add_item",
                metadata={"step_kind": "request_input"},
                evidence=[EvidenceRef(file=file, snippet="data = request.get_json()")],
                confidence=0.9,
            ),
            GraphNode(
                id="store",
                type="store_write",
                name="items.append(item)",
                repo="api",
                file=file,
                symbol="add_item",
                metadata={"step_kind": "store_write"},
                evidence=[EvidenceRef(file=file, snippet="items.append(item)")],
                confidence=0.8,
            ),
        ],
        edges=[
            GraphEdge(id="e1", source="endpoint", target="body", type="NEXT_STEP"),
            GraphEdge(id="e2", source="body", target="store", type="NEXT_STEP"),
        ],
        flows=[
            Flow(
                id="f1",
                name="POST /items",
                entry_node="endpoint",
                steps=[
                    FlowStep(node_id="endpoint", kind="endpoint"),
                    FlowStep(node_id="body", kind="request_input"),
                    FlowStep(node_id="store", kind="store_write"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.8, key_flow_id="f1"),
    )
    return trace


def test_builds_packet_with_endpoint_nodes_edges_and_snippets(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "from flask import request\n\n"
        "items = []\n\n"
        "def add_item():\n"
        "    data = request.get_json()\n"
        "    item = {'name': data.get('name')}\n"
        "    items.append(item)\n"
        "    return item, 201\n",
        encoding="utf-8",
    )

    packet = build_evidence_packet_for_route(_trace(tmp_path))

    assert packet.endpoint.method == "POST"
    assert packet.endpoint.path == "/items"
    assert [node.id for node in packet.trace_nodes] == ["endpoint", "body", "store"]
    assert packet.trace_nodes[1].snippet == "data = request.get_json()"
    assert len(packet.trace_edges) == 2
    assert packet.sinks[0].kind == "store_write"


def test_includes_python_handler_source_window(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def helper():\n"
        "    return None\n\n"
        "def add_item():\n"
        "    data = request.get_json()\n"
        "    if not data.get('name'):\n"
        "        return {'error': 'name is required'}, 400\n"
        "    return {'id': 1, 'name': data.get('name')}, 201\n\n"
        "def unrelated():\n"
        "    return 'nope'\n",
        encoding="utf-8",
    )

    packet = build_evidence_packet_for_route(_trace(tmp_path))

    assert len(packet.source_windows) == 1
    window = packet.source_windows[0]
    assert window.symbol == "add_item"
    assert "def add_item" in window.code
    assert "name is required" in window.code
    assert "def unrelated" not in window.code
    assert window.start_line == 4


def test_caps_source_window_length(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def add_item():\n"
        + "\n".join(f"    value_{index} = {index}" for index in range(100))
        + "\n    return {}, 201\n",
        encoding="utf-8",
    )

    packet = build_evidence_packet_for_route(_trace(tmp_path), max_source_chars=120)

    assert packet.source_windows
    assert len(packet.source_windows[0].code) <= 120
    assert packet.source_windows[0].truncated is True


def test_includes_only_selected_route_contract_and_test_summary(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def add_item():\n    return {}, 201\n", encoding="utf-8")
    contract = ApiContractArtifact(
        routes=[
            ApiRouteContract(method="GET", path="/items", repo="api"),
            ApiRouteContract(
                method="POST",
                path="/items",
                repo="api",
                request=ApiRequestContract(
                    body=ApiSchema(
                        type="object",
                        required=["name"],
                        properties={"name": ApiSchemaProperty(type="string")},
                    )
                ),
                responses={"201": ApiResponseContract(status=201)},
            ),
        ]
    )
    matrix = SydesTestMatrix(
        groups=[
            SydesTestMatrixGroup(
                category="validation",
                tests=[
                    IntegrationTestSuggestion(
                        name="missing name",
                        route="/items",
                        method="POST",
                        contract_refs=["request.body.name"],
                    )
                ],
            )
        ]
    )

    packet = build_evidence_packet_for_route(
        _trace(tmp_path),
        api_contract=contract,
        test_matrix=matrix,
    )

    assert packet.current_contract is not None
    assert packet.current_contract["method"] == "POST"
    assert packet.current_contract["request"]["body"]["required"] == ["name"]
    assert packet.current_test_matrix_summary is not None
    assert packet.current_test_matrix_summary["scenario_count"] == 1
    assert packet.current_test_matrix_summary["scenarios"][0]["contract_refs"] == [
        "request.body.name"
    ]


def test_missing_source_file_adds_note_without_crashing(tmp_path: Path) -> None:
    packet = build_evidence_packet_for_route(_trace(tmp_path, file="missing.py"))

    assert packet.source_windows == []
    assert any("Source unavailable" in note for note in packet.notes)


def test_evidence_packet_serializes_to_valid_json(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def add_item():\n    return {}, 201\n", encoding="utf-8")
    packet = build_evidence_packet_for_route(_trace(tmp_path))

    payload = json.loads(render_evidence_packet_json(packet))

    assert payload["version"] == "v1"
    assert payload["endpoint"]["path"] == "/items"
