"""Integration test suggestion generation from traced flows.

This module produces structured suggestion objects only.
It does not emit runnable framework-specific test files yet.
"""

import re

from sydes.core.models import (
    Flow,
    GraphNode,
    IntegrationTestSuggestion,
    TEST_MATRIX_CATEGORY_EDGE_CASES,
    TEST_MATRIX_CATEGORY_HAPPY_PATH,
    TEST_MATRIX_CATEGORY_SIDE_EFFECTS,
    TEST_MATRIX_CATEGORY_STATE_CONSISTENCY,
    TEST_MATRIX_CATEGORY_VALIDATION,
    TestMatrix,
    TestMatrixGroup,
    TestExpectation,
    TestInputHint,
    TraceResult,
)


def _route_token(path: str) -> str:
    """Build a compact route token usable in deterministic test names."""
    normalized = path.strip().strip("/")
    if not normalized:
        return "root"
    chunks = [chunk for chunk in normalized.replace("-", "_").split("/") if chunk]
    cleaned = []
    for chunk in chunks:
        token = "".join(char.lower() if char.isalnum() else "_" for char in chunk).strip("_")
        cleaned.append(token or "segment")
    return "_".join(cleaned)


def _entity_label_from_route(path: str) -> str:
    """Infer a lightweight singular-ish entity label from route path."""
    normalized = path.strip().strip("/")
    if not normalized:
        return "record"
    parts = [part for part in normalized.split("/") if part and not part.startswith("{") and not part.startswith(":")]
    if not parts:
        return "record"
    entity = parts[-1].replace("-", "_")
    if entity.endswith("ies") and len(entity) > 3:
        entity = f"{entity[:-3]}y"
    elif entity.endswith("s") and not entity.endswith("ss") and len(entity) > 3:
        entity = entity[:-1]
    entity = entity.replace("_", " ").strip()
    return entity or "record"


def _resource_words_from_route(path: str) -> str:
    """Infer lightweight resource words from route for human-readable naming/summaries."""
    normalized = path.strip().strip("/")
    if not normalized:
        return "data"
    parts = [
        part
        for part in normalized.split("/")
        if part and not part.startswith("{") and not part.startswith(":") and not (part.startswith("<") and part.endswith(">"))
    ]
    if not parts:
        return "data"
    return parts[-1].replace("-", " ").replace("_", " ")


def _flow_step_names(trace_result: TraceResult) -> list[str]:
    """Return display names for nodes participating in the selected flow."""
    node_by_id: dict[str, GraphNode] = {node.id: node for node in trace_result.nodes}
    nodes = _selected_flow_nodes(trace_result, node_by_id=node_by_id)
    names: list[str] = []
    for node in nodes:
        if node.type in {"api_endpoint", "database", "external_api", "queue", "file_sink", "sink"}:
            continue
        if node.name:
            names.append(node.name.lower().strip())
    return names


def _selected_flow_nodes(
    trace_result: TraceResult,
    *,
    node_by_id: dict[str, GraphNode] | None = None,
) -> list[GraphNode]:
    """Return graph nodes that participate in the selected flow ordering."""
    if not trace_result.flows:
        return []
    flow: Flow | None = None
    if trace_result.summary.key_flow_id:
        flow = next((item for item in trace_result.flows if item.id == trace_result.summary.key_flow_id), None)
    if flow is None:
        flow = trace_result.flows[0]
    by_id = node_by_id or {node.id: node for node in trace_result.nodes}
    selected: list[GraphNode] = []
    for step in flow.steps:
        node = by_id.get(step.node_id)
        if node is not None:
            selected.append(node)
    return selected


def _contains_return_step(step_names: list[str]) -> bool:
    """Detect return-like flow operations from inferred step names."""
    return any(name.startswith("return ") or " return " in name for name in step_names)


def _unique_suggestions(items: list[IntegrationTestSuggestion]) -> list[IntegrationTestSuggestion]:
    """Keep deterministic ordering while removing duplicate suggestion names."""
    seen: set[str] = set()
    result: list[IntegrationTestSuggestion] = []
    for item in items:
        key = item.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _has_path_param(path: str) -> bool:
    """Detect path-parameter syntax in route templates."""
    return bool(re.search(r"\{[^}]+\}|:[A-Za-z_][A-Za-z0-9_]*|<[^>]+>", path))


def _normalized_resource_root(path: str) -> str:
    """Return resource-root path token for lightweight route comparisons."""
    cleaned = path.strip()
    if not cleaned:
        return "/"
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        return "/"
    parts = [part for part in cleaned.split("/") if part]
    static_parts = [part for part in parts if not part.startswith("{") and not part.startswith(":")]
    if not static_parts:
        return "/"
    return "/" + "/".join(static_parts)


def _has_database_write_sink(trace_result: TraceResult) -> bool:
    """Return True when trace includes a database write sink node."""
    return any(
        node.type == "database" and (node.metadata or {}).get("action") == "write"
        for node in trace_result.nodes
    )


def _has_database_read_sink(trace_result: TraceResult) -> bool:
    """Return True when trace includes a database read sink node."""
    return any(
        node.type == "database" and (node.metadata or {}).get("action") == "read"
        for node in trace_result.nodes
    )


def _has_external_api_sink(trace_result: TraceResult) -> bool:
    """Return True when trace includes an external API sink node."""
    return any(node.type == "external_api" for node in trace_result.nodes)


def _has_queue_sink(trace_result: TraceResult) -> bool:
    """Return True when trace includes queue sink nodes."""
    return any(node.type == "queue" for node in trace_result.nodes)


def _has_cross_repo_link(trace_result: TraceResult) -> bool:
    """Return True when CALLS_API edges connect across repositories."""
    node_by_id: dict[str, GraphNode] = {node.id: node for node in trace_result.nodes}
    for edge in trace_result.edges:
        if edge.type != "CALLS_API":
            continue
        source = node_by_id.get(edge.source)
        target = node_by_id.get(edge.target)
        if source is None or target is None:
            continue
        if source.repo and target.repo and source.repo != target.repo:
            return True
    return False


def _has_auth_signal(step_names: list[str]) -> bool:
    """Detect auth-boundary signals from step names."""
    return any(token in name for name in step_names for token in ("auth", "authorize", "permission", "jwt", "token"))


def _has_validation_signal(step_names: list[str]) -> bool:
    """Detect validation signals from inferred step names."""
    return any(token in name for name in step_names for token in ("validate", "validation", "schema", "required field"))


def _flow_metadata_text(trace_result: TraceResult) -> str:
    """Flatten selected flow metadata/name/evidence text for lightweight rule matching."""
    by_id = {node.id: node for node in trace_result.nodes}
    nodes = _selected_flow_nodes(trace_result, node_by_id=by_id)
    chunks: list[str] = []
    for node in nodes:
        if node.name:
            chunks.append(node.name.lower())
        if isinstance(node.metadata, dict):
            for value in node.metadata.values():
                if isinstance(value, str):
                    chunks.append(value.lower())
        for ref in node.evidence:
            if ref.label:
                chunks.append(ref.label.lower())
            if ref.symbol:
                chunks.append(ref.symbol.lower())
    return " ".join(chunks)


def _extract_input_model_hint(trace_result: TraceResult) -> str | None:
    """Extract deterministic input model hint from selected flow step metadata/names."""
    by_id = {node.id: node for node in trace_result.nodes}
    for node in _selected_flow_nodes(trace_result, node_by_id=by_id):
        if node.type != "internal_step":
            continue
        step_kind = (node.metadata or {}).get("step_kind") if isinstance(node.metadata, dict) else None
        if step_kind != "input_model":
            continue
        marker = "input model:"
        lowered = node.name.lower()
        if marker in lowered:
            _, _, tail = node.name.partition(":")
            hint = tail.strip()
            if hint:
                return hint
        if node.name.strip():
            return node.name.strip()
    return None


def _has_lookup_signal(trace_result: TraceResult) -> bool:
    """Detect entity-lookup style read signals (e.g., .first(), get_by_id)."""
    blob = _flow_metadata_text(trace_result)
    lookup_tokens = (
        ".first()",
        "get by id",
        "get_by_id",
        "find_by_id",
        "query(",
    )
    return any(token in blob for token in lookup_tokens)


def _has_db_add_signal(trace_result: TraceResult) -> bool:
    """Detect DB add/create-path signals from deterministic/LLM flow evidence."""
    blob = _flow_metadata_text(trace_result)
    return any(token in blob for token in ("db.add(", "insert", "create ", "repository.save"))


def _has_db_commit_signal(trace_result: TraceResult) -> bool:
    """Detect commit/write-finalization signals from flow evidence."""
    blob = _flow_metadata_text(trace_result)
    return any(token in blob for token in ("db.commit", "commit(", "save(", "flush("))


def _has_unique_field_hint(trace_result: TraceResult) -> bool:
    """Detect simple uniqueness hints for duplicate-input matrix suggestions."""
    blob = _flow_metadata_text(trace_result)
    return any(token in blob for token in ("email", "unique", "already exists", "duplicate"))


def _has_explicit_get_route_hint(trace_result: TraceResult) -> bool:
    """Return True when trace graph already includes GET endpoint hints."""
    target_root = _normalized_resource_root(trace_result.target.path)
    for node in trace_result.nodes:
        if node.type != "api_endpoint":
            continue
        if (node.method or "").upper() != "GET":
            continue
        node_root = _normalized_resource_root(node.path or "")
        if node_root == target_root or node_root.startswith(target_root) or target_root.startswith(node_root):
            return True
    return False


def _build_matrix_suggestion(
    *,
    name: str,
    route: str,
    method: str,
    summary: str,
    expectations: list[TestExpectation],
    flow_id: str | None,
    confidence: float | None,
    notes: list[str] | None = None,
) -> IntegrationTestSuggestion:
    """Create deterministic matrix suggestion with minimal repeated boilerplate."""
    suggestion_notes = list(notes or [])
    if not any("deterministic" in note.lower() for note in suggestion_notes):
        suggestion_notes.append("deterministic baseline derived from traced flow evidence")
    return IntegrationTestSuggestion(
        name=name,
        route=route,
        method=method,
        summary=summary,
        inputs=[
            TestInputHint(kind="request_path", value_hint=route, required=True),
            TestInputHint(kind="http_method", value_hint=method, required=True),
        ],
        expectations=expectations,
        derived_from_flow_id=flow_id,
        confidence=confidence,
        notes=suggestion_notes,
    )


def _build_fallback_matrix(
    *,
    route: str,
    method: str,
    route_token: str,
    flow_id: str | None,
    confidence: float | None,
) -> list[TestMatrixGroup]:
    """Build minimal grouped baseline when no category rules produced tests."""
    method_token = method.lower()
    happy = _build_matrix_suggestion(
        name=f"{method_token}_{route_token}_baseline_happy_path",
        route=route,
        method=method,
        summary=f"verifies {method} {route} succeeds for a valid request",
        expectations=[TestExpectation(kind="http_response", description="request succeeds with expected response")],
        flow_id=flow_id,
        confidence=confidence,
    )
    edge = _build_matrix_suggestion(
        name=f"{method_token}_{route_token}_baseline_edge_case",
        route=route,
        method=method,
        summary=f"verifies {method} {route} handles an edge-case input safely",
        expectations=[TestExpectation(kind="edge_case", description="edge-case input is handled safely")],
        flow_id=flow_id,
        confidence=confidence,
    )
    return [
        TestMatrixGroup(category=TEST_MATRIX_CATEGORY_HAPPY_PATH, tests=[happy]),
        TestMatrixGroup(category=TEST_MATRIX_CATEGORY_EDGE_CASES, tests=[edge]),
    ]


def generate_test_matrix(trace_result: TraceResult, *, max_suggestions: int = 7) -> TestMatrix:
    """Generate a deterministic category-grouped API test matrix from trace output."""
    route = trace_result.target.path
    method = (trace_result.target.method or "ANY").upper()
    method_token = method.lower()
    route_token = _route_token(route)
    entity_label = _entity_label_from_route(route)
    flow_id = trace_result.summary.key_flow_id
    confidence = trace_result.summary.confidence
    has_db_write = _has_database_write_sink(trace_result)
    has_db_read = _has_database_read_sink(trace_result)
    has_external_api = _has_external_api_sink(trace_result)
    has_queue = _has_queue_sink(trace_result)
    has_cross_repo = _has_cross_repo_link(trace_result)
    has_explicit_get = _has_explicit_get_route_hint(trace_result)
    inferred_get = not has_explicit_get and has_db_write and method in {"POST", "PUT", "PATCH"}
    has_id_param = _has_path_param(route)
    step_names = _flow_step_names(trace_result)
    has_auth = _has_auth_signal(step_names)
    has_validation = _has_validation_signal(step_names)
    has_lookup = _has_lookup_signal(trace_result)
    has_db_add = _has_db_add_signal(trace_result)
    has_db_commit = _has_db_commit_signal(trace_result)
    input_model_hint = _extract_input_model_hint(trace_result)
    has_unique_hint = _has_unique_field_hint(trace_result)

    by_category: dict[str, list[IntegrationTestSuggestion]] = {
        TEST_MATRIX_CATEGORY_HAPPY_PATH: [],
        TEST_MATRIX_CATEGORY_VALIDATION: [],
        TEST_MATRIX_CATEGORY_SIDE_EFFECTS: [],
        TEST_MATRIX_CATEGORY_STATE_CONSISTENCY: [],
        TEST_MATRIX_CATEGORY_EDGE_CASES: [],
        "failure_modes": [],
        "data_shape": [],
        "persistence": [],
    }

    if method == "POST":
        by_category[TEST_MATRIX_CATEGORY_HAPPY_PATH].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_creates_resource",
                route=route,
                method=method,
                summary=f"verifies {method} {route} creates a new {entity_label} and returns success",
                expectations=[
                    TestExpectation(kind="http_response", description="request succeeds with expected response"),
                    TestExpectation(kind="behavior", description=f"a new {entity_label} resource is created"),
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category["data_shape"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_returns_created_entity_shape",
                route=route,
                method=method,
                summary=f"verifies {method} {route} returns created {entity_label} data with expected fields",
                expectations=[
                    TestExpectation(
                        kind="data_shape",
                        description=f"created {entity_label} response includes id and expected fields",
                    )
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_rejects_missing_required_field",
                route=route,
                method=method,
                summary=f"verifies {method} {route} rejects missing required fields",
                expectations=[TestExpectation(kind="validation", description="missing required field is rejected")],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_rejects_invalid_payload",
                route=route,
                method=method,
                summary=f"verifies {method} {route} rejects invalid payloads",
                expectations=[TestExpectation(kind="validation", description="invalid payload is rejected")],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        if has_unique_hint:
            duplicate_field = "email" if "email" in _flow_metadata_text(trace_result) else "unique field"
            by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_rejects_duplicate_{duplicate_field.replace(' ', '_')}",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} rejects duplicate {duplicate_field} values",
                    expectations=[
                        TestExpectation(
                            kind="validation",
                            description=f"duplicate {duplicate_field} value is rejected",
                        )
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )
        if has_db_write:
            by_category["persistence"].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_writes_to_database",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} causes a database write",
                    expectations=[
                        TestExpectation(kind="side_effect", description="database write occurs", target="database")
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )
            if has_db_commit:
                by_category["failure_modes"].append(
                    _build_matrix_suggestion(
                        name=f"{method_token}_{route_token}_database_commit_failure_handled",
                        route=route,
                        method=method,
                        summary=f"verifies {method} {route} handles database commit failures safely",
                        expectations=[
                            TestExpectation(
                                kind="failure_mode",
                                description="database commit failure is handled safely",
                                target="database",
                            )
                        ],
                        flow_id=flow_id,
                        confidence=confidence,
                    )
                )
        if has_explicit_get or inferred_get:
            notes = []
            if inferred_get:
                notes.append("follow-up fetch inferred from write-heavy flow evidence")
            by_category[TEST_MATRIX_CATEGORY_STATE_CONSISTENCY].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_create_then_fetch_consistent",
                    route=route,
                    method=method,
                    summary=f"verifies created {entity_label} is retrievable in a follow-up fetch",
                    expectations=[
                        TestExpectation(
                            kind="state_consistency",
                            description="create followed by fetch returns consistent state",
                        )
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                    notes=notes,
                )
            )

    elif method == "GET":
        happy_name = f"{method_token}_{route_token}_returns_entity_or_list"
        happy_summary = f"verifies {method} {route} returns the expected entity/list response"
        if has_external_api:
            resource_words = _resource_words_from_route(route)
            happy_name = f"{method_token}_{route_token}_proxies_downstream_response"
            happy_summary = f"verifies {method} {route} returns {resource_words} from a downstream service call"
            if has_cross_repo:
                happy_summary = (
                    f"verifies {method} {route} returns {resource_words} from the linked downstream service"
                )
        by_category[TEST_MATRIX_CATEGORY_HAPPY_PATH].append(
            _build_matrix_suggestion(
                name=happy_name,
                route=route,
                method=method,
                summary=happy_summary,
                expectations=[TestExpectation(kind="http_response", description="response returns entity/list data")],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        if has_id_param:
            by_category[TEST_MATRIX_CATEGORY_EDGE_CASES].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_returns_not_found_for_missing_resource",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} handles not-found resources",
                    expectations=[TestExpectation(kind="edge_case", description="not found case is handled correctly")],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )
        else:
            by_category[TEST_MATRIX_CATEGORY_EDGE_CASES].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_handles_empty_result_set",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} safely handles empty result sets",
                    expectations=[
                        TestExpectation(kind="edge_case", description="empty list/result is handled safely")
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )
            by_category["data_shape"].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_returns_expected_response_shape",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} response schema/shape remains stable",
                    expectations=[
                        TestExpectation(kind="data_shape", description="response shape remains valid for collection output")
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )
        if has_id_param:
            by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_rejects_invalid_path_param",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} rejects invalid path parameter values",
                    expectations=[
                        TestExpectation(kind="validation", description="invalid id/path parameter is rejected")
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )
        if has_db_read:
            by_category["failure_modes"].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_database_read_failure_handled",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} handles database read failures safely",
                    expectations=[
                        TestExpectation(
                            kind="failure_mode",
                            description="database read failure is handled safely",
                            target="database",
                        )
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                )
            )

    elif method in {"PUT", "PATCH"}:
        by_category[TEST_MATRIX_CATEGORY_HAPPY_PATH].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_updates_resource",
                route=route,
                method=method,
                summary=f"verifies {method} {route} updates the target {entity_label}",
                expectations=[TestExpectation(kind="http_response", description="update request succeeds")],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_rejects_invalid_payload",
                route=route,
                method=method,
                summary=f"verifies {method} {route} rejects invalid payloads",
                expectations=[TestExpectation(kind="validation", description="invalid payload is rejected")],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category[TEST_MATRIX_CATEGORY_STATE_CONSISTENCY].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_update_then_fetch_consistent",
                route=route,
                method=method,
                summary=f"verifies updated {entity_label} is returned in follow-up fetch",
                expectations=[
                    TestExpectation(
                        kind="state_consistency",
                        description="update followed by fetch returns the latest persisted state",
                    )
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    elif method == "DELETE":
        by_category[TEST_MATRIX_CATEGORY_HAPPY_PATH].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_deletes_resource",
                route=route,
                method=method,
                summary=f"verifies {method} {route} deletes the target {entity_label}",
                expectations=[TestExpectation(kind="http_response", description="delete request succeeds")],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category[TEST_MATRIX_CATEGORY_STATE_CONSISTENCY].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_deleted_resource_not_returned",
                route=route,
                method=method,
                summary=f"verifies deleted {entity_label} is no longer returned by follow-up fetch",
                expectations=[
                    TestExpectation(
                        kind="state_consistency",
                        description="deleted resource is not returned after deletion",
                    )
                ],
                flow_id=flow_id,
                confidence=confidence,
                )
            )

    if has_external_api:
        by_category["failure_modes"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_downstream_unavailable",
                route=route,
                method=method,
                summary=f"verifies {method} {route} handles downstream service unavailability",
                expectations=[
                    TestExpectation(kind="failure_mode", description="downstream connection failure is handled safely", target="external_api")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category["failure_modes"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_downstream_timeout",
                route=route,
                method=method,
                summary=f"verifies {method} {route} handles downstream timeout safely",
                expectations=[
                    TestExpectation(kind="failure_mode", description="downstream timeout is handled safely", target="external_api")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category["data_shape"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_downstream_empty_payload_handled",
                route=route,
                method=method,
                summary=f"verifies {method} {route} safely handles empty downstream payloads",
                expectations=[
                    TestExpectation(kind="data_shape", description="empty downstream entity/list is handled safely", target="external_api")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category["data_shape"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_downstream_malformed_payload_handled",
                route=route,
                method=method,
                summary=f"verifies {method} {route} safely handles malformed downstream payloads",
                expectations=[
                    TestExpectation(kind="data_shape", description="malformed downstream payload is handled safely", target="external_api")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    if has_cross_repo:
        by_category["data_shape"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_cross_service_contract_compatible",
                route=route,
                method=method,
                summary=f"verifies {method} {route} response remains compatible with linked downstream service contract",
                expectations=[
                    TestExpectation(kind="contract", description="cross-service response contract remains compatible", target="cross_repo")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    if has_db_write:
        by_category["failure_modes"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_database_write_failure_handled",
                route=route,
                method=method,
                summary=f"verifies {method} {route} handles database write failures safely",
                expectations=[
                    TestExpectation(kind="failure_mode", description="database write failure is handled safely", target="database")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category["persistence"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_write_path_is_idempotent",
                route=route,
                method=method,
                summary=f"verifies repeated {method} {route} calls preserve safe state semantics",
                expectations=[
                    TestExpectation(kind="state_consistency", description="repeated write calls do not corrupt state", target="database")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        if has_db_add:
            by_category["persistence"].append(
                _build_matrix_suggestion(
                    name=f"{method_token}_{route_token}_write_sequence_persists_entity",
                    route=route,
                    method=method,
                    summary=f"verifies {method} {route} persists {entity_label} through add/commit workflow",
                    expectations=[
                        TestExpectation(
                            kind="persistence",
                            description="entity is persisted after add/commit sequence",
                            target="database",
                        )
                    ],
                    flow_id=flow_id,
                    confidence=confidence,
                    notes=[f"input model hint: {input_model_hint}"] if input_model_hint else [],
                )
            )

    if has_db_read:
        by_category[TEST_MATRIX_CATEGORY_EDGE_CASES].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_empty_result_handled",
                route=route,
                method=method,
                summary=f"verifies {method} {route} safely handles empty read results",
                expectations=[
                    TestExpectation(kind="edge_case", description="empty database read result is handled safely", target="database")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    if has_queue:
        by_category["failure_modes"].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_queue_publish_failure_handled",
                route=route,
                method=method,
                summary=f"verifies {method} {route} handles queue publish failures safely",
                expectations=[
                    TestExpectation(kind="failure_mode", description="queue publish failure is handled or retried safely", target="queue")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    if has_auth:
        by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_rejects_unauthenticated_requests",
                route=route,
                method=method,
                summary=f"verifies {method} {route} rejects unauthenticated requests",
                expectations=[
                    TestExpectation(kind="auth", description="unauthenticated request is rejected")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )
        by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_rejects_unauthorized_requests",
                route=route,
                method=method,
                summary=f"verifies {method} {route} rejects unauthorized requests",
                expectations=[
                    TestExpectation(kind="auth", description="unauthorized request is rejected")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    if has_validation and method not in {"POST", "PUT", "PATCH"}:
        by_category[TEST_MATRIX_CATEGORY_VALIDATION].append(
            _build_matrix_suggestion(
                name=f"{method_token}_{route_token}_rejects_invalid_or_missing_fields",
                route=route,
                method=method,
                summary=f"verifies {method} {route} rejects invalid or missing required fields",
                expectations=[
                    TestExpectation(kind="validation", description="invalid or missing required fields are rejected")
                ],
                flow_id=flow_id,
                confidence=confidence,
            )
        )

    if method == "GET" and not has_id_param:
        data_shape_names = {
            item.name
            for item in by_category["data_shape"]
        }
        empty_list_name = f"{method_token}_{route_token}_handles_empty_result_set"
        downstream_empty_name = f"{method_token}_{route_token}_downstream_empty_payload_handled"
        if downstream_empty_name in data_shape_names:
            by_category[TEST_MATRIX_CATEGORY_EDGE_CASES] = [
                item
                for item in by_category[TEST_MATRIX_CATEGORY_EDGE_CASES]
                if item.name != empty_list_name
            ]

    matrix_groups: list[TestMatrixGroup] = []
    used_names: set[str] = set()
    total = 0
    category_order = [
        TEST_MATRIX_CATEGORY_HAPPY_PATH,
        "data_shape",
        "failure_modes",
        TEST_MATRIX_CATEGORY_VALIDATION,
        "persistence",
        TEST_MATRIX_CATEGORY_SIDE_EFFECTS,
        TEST_MATRIX_CATEGORY_STATE_CONSISTENCY,
        TEST_MATRIX_CATEGORY_EDGE_CASES,
    ]
    for category in category_order:
        selected: list[IntegrationTestSuggestion] = []
        for suggestion in by_category.get(category, []):
            key = suggestion.name.strip().lower()
            if key in used_names:
                continue
            if total >= max_suggestions:
                break
            selected.append(suggestion)
            used_names.add(key)
            total += 1
        if selected:
            matrix_groups.append(
                TestMatrixGroup(
                    category=category,
                    tests=selected,
                )
            )
        if total >= max_suggestions:
            break

    notes: list[str] = []
    if inferred_get:
        notes.append("consistency group includes inferred fetch checks from write + route shape hints")
    if not matrix_groups:
        matrix_groups = _build_fallback_matrix(
            route=route,
            method=method,
            route_token=route_token,
            flow_id=flow_id,
            confidence=confidence,
        )
        notes.append("applied fallback grouped baseline test matrix")
    return TestMatrix(groups=matrix_groups, notes=notes)


def generate_test_suggestions(trace_result: TraceResult) -> list[IntegrationTestSuggestion]:
    """Generate deterministic integration-test suggestions from trace output."""
    route = trace_result.target.path
    method = (trace_result.target.method or "ANY").upper()
    flow_id = trace_result.summary.key_flow_id
    route_token = _route_token(route)
    method_token = method.lower()
    entity_label = _entity_label_from_route(route)

    sink_nodes = [node for node in trace_result.nodes if node.type in {"database", "external_api", "queue", "file_sink"}]
    sink_types = {node.type for node in sink_nodes}
    has_db_write = any(node.type == "database" and (node.metadata or {}).get("action") == "write" for node in sink_nodes)
    has_db_read = any(node.type == "database" and (node.metadata or {}).get("action") == "read" for node in sink_nodes)
    has_queue = "queue" in sink_types
    has_external_api = "external_api" in sink_types
    step_names = _flow_step_names(trace_result)
    has_return_step = _contains_return_step(step_names)
    sink_only_evidence = bool(sink_nodes) and not step_names

    core_expectations: list[TestExpectation] = [
        TestExpectation(
            kind="http_response",
            description="request succeeds with expected response",
            target=f"{method} {route}",
        )
    ]

    if method == "POST" and has_db_write:
        primary_name = f"{method_token}_{route_token}_creates_record"
        primary_summary = f"verifies {method} {route} persists a new {entity_label} record"
        core_expectations.append(
            TestExpectation(kind="side_effect", description=f"persists a new {entity_label} record", target="database")
        )
    elif method == "GET" and has_db_read:
        primary_name = f"{method_token}_{route_token}_returns_retrieved_data"
        primary_summary = f"verifies {method} {route} returns retrieved {entity_label} data"
        core_expectations.append(
            TestExpectation(kind="behavior", description="response includes retrieved entity or list", target="database")
        )
    elif has_db_write:
        primary_name = f"{method_token}_{route_token}_writes_to_database"
        primary_summary = "validate primary route behavior from inferred flow and sink evidence"
        core_expectations.append(TestExpectation(kind="side_effect", description="database write occurs", target="database"))
    elif has_db_read:
        primary_name = f"{method_token}_{route_token}_reads_from_database"
        primary_summary = "validate primary route behavior from inferred flow and sink evidence"
        core_expectations.append(TestExpectation(kind="behavior", description="response reflects retrieved data", target="database"))
    else:
        primary_name = f"{method_token}_{route_token}_returns_success"
        primary_summary = "validate primary route behavior from inferred flow and sink evidence"

    if has_return_step:
        core_expectations.append(
            TestExpectation(kind="behavior", description="response payload reflects returned domain data", target="response")
        )
    if has_queue:
        core_expectations.append(
            TestExpectation(kind="side_effect", description="event/message emission occurs", target="queue")
        )
    if has_external_api:
        core_expectations.append(
            TestExpectation(kind="side_effect", description="outbound dependency interaction occurs", target="external_api")
        )

    basic = IntegrationTestSuggestion(
        name=primary_name,
        route=route,
        method=method,
        summary=primary_summary,
        inputs=[
            TestInputHint(kind="request_path", value_hint=route, required=True),
            TestInputHint(kind="http_method", value_hint=method, required=True),
        ],
        expectations=core_expectations,
        derived_from_flow_id=flow_id,
        confidence=trace_result.summary.confidence,
        notes=["expectations inferred from sink evidence only"] if sink_only_evidence else [],
    )

    suggestions: list[IntegrationTestSuggestion] = [basic]
    if has_return_step:
        if method == "POST":
            payload_name = f"{method_token}_{route_token}_returns_created_entity"
            payload_summary = f"verifies the response returns created {entity_label} data"
            payload_expectation = "response returns created entity data"
        else:
            payload_name = f"{method_token}_{route_token}_returns_response_payload"
            payload_summary = "validate response body shape from return-like flow steps"
            payload_expectation = "response payload includes expected created or fetched data"
        suggestions.append(
            IntegrationTestSuggestion(
                name=payload_name,
                route=route,
                method=method,
                summary=payload_summary,
                expectations=[
                    TestExpectation(kind="behavior", description=payload_expectation)
                ],
                derived_from_flow_id=flow_id,
                confidence=trace_result.summary.confidence,
            )
        )
    if (has_db_write or has_queue or has_external_api) and len(suggestions) < 3:
        suffix = "writes_to_database" if has_db_write else "verifies_side_effects"
        suggestions.append(
            IntegrationTestSuggestion(
                name=f"{method_token}_{route_token}_{suffix}",
                route=route,
                method=method,
                summary="validate major side effects inferred from sink semantics",
                expectations=[
                    TestExpectation(kind="side_effect", description="database write occurs", target="database")
                    if has_db_write
                    else TestExpectation(
                        kind="side_effect", description="observable side effects occur in dependencies", target="integration"
                    )
                ],
                derived_from_flow_id=flow_id,
                confidence=trace_result.summary.confidence,
                notes=["derived from V1 sink taxonomy; framework assertions are intentionally generic"]
                if sink_only_evidence
                else [],
            )
        )
        if has_queue:
            suggestions[-1].expectations.append(
                TestExpectation(kind="side_effect", description="event/message emission occurs", target="queue")
            )
        if has_external_api:
            suggestions[-1].expectations.append(
                TestExpectation(kind="side_effect", description="outbound dependency interaction occurs", target="external_api")
            )

    return _unique_suggestions(suggestions)[:3]
