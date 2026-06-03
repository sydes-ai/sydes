"""Integration test suggestion generation from traced flows.

This module produces structured suggestion objects only.
It does not emit runnable framework-specific test files yet.
"""

import ast
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any

from sydes.core.models import (
    ApiContractArtifact,
    ApiRouteContract,
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

WRITE_METHODS = {"POST", "PUT", "PATCH"}
PY_LITERAL_FIELD_REQUIRED_RE = re.compile(r'data\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]')
PY_LITERAL_FIELD_OPTIONAL_RE = re.compile(r'data\.get\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
PY_TEST_JSON_PAYLOAD_RE = re.compile(
    r"client\.(post|put|patch)\s*\(\s*['\"](?P<path>/[^'\"]*)['\"][^)]*json\s*=\s*(?P<body>\{.*?\})",
    re.IGNORECASE | re.DOTALL,
)
REQUEST_BODY_SIGNAL_TOKENS = (
    "request.get_json(",
    "request.json",
    "request.form",
    "request.data",
    "request.post",
    "await request.json(",
    "req.body",
    "request.body",
    "ctx.request.body",
    "await req.json(",
    "@requestbody",
    "json.newdecoder(r.body).decode",
    "io.readall(r.body)",
)


def _normalize_contract_path(path: str | None) -> str:
    if not path:
        return ""
    value = path.strip()
    if not value.startswith("/"):
        value = "/" + value
    value = re.sub(r"/+", "/", value)
    value = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", value)
    value = re.sub(r":([A-Za-z_]\w*)", r"{\1}", value)
    if value != "/" and value.endswith("/"):
        value = value[:-1]
    return value


def match_route_contract(
    contract: ApiContractArtifact | None,
    *,
    method: str | None,
    path: str,
) -> ApiRouteContract | None:
    """Find best matching route contract for target method/path."""
    if contract is None:
        return None
    target_method = (method or "").upper()
    target_path = _normalize_contract_path(path)
    exact: list[ApiRouteContract] = []
    path_only: list[ApiRouteContract] = []
    for route in contract.routes:
        candidate_method = (route.method or "").upper()
        candidate_path = _normalize_contract_path(route.path)
        if candidate_path != target_path:
            continue
        path_only.append(route)
        if target_method and candidate_method == target_method:
            exact.append(route)
    if exact:
        return exact[0]
    if path_only:
        return path_only[0]
    return None


@dataclass(frozen=True)
class RequestBodySignal:
    """Generic route input signal describing HTTP request-body consumption."""

    consumed: bool
    shape: dict[str, Any] | None
    required: bool
    required_fields: set[str]
    notes: list[str]


def _py_value_type(value: object) -> str:
    """Map a python literal to a lightweight field type label."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unknown"


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


def _extract_fields_from_model_class(repo_root: str, model_name: str) -> tuple[dict[str, str], set[str], list[str]]:
    """Find a Pydantic-like class definition and infer field names/types."""
    root = Path(repo_root)
    if not root.exists():
        return {}, set(), []
    class_pattern = re.compile(
        rf"class\s+{re.escape(model_name)}\s*\([^)]*BaseModel[^)]*\)\s*:\s*(?P<body>(?:\n[ \t]+[^\n]+)+)",
        re.IGNORECASE,
    )
    field_pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=\n#]+)")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = class_pattern.search(text)
        if not match:
            continue
        fields: dict[str, str] = {}
        required: set[str] = set()
        for line in match.group("body").splitlines():
            field_match = field_pattern.search(line)
            if not field_match:
                continue
            field_name = field_match.group(1)
            annotation = field_match.group(2).strip().lower()
            if "str" in annotation:
                field_type = "string"
            elif "int" in annotation or "float" in annotation or "decimal" in annotation:
                field_type = "number"
            elif "bool" in annotation:
                field_type = "boolean"
            elif "list" in annotation:
                field_type = "array"
            elif "dict" in annotation:
                field_type = "object"
            else:
                field_type = "unknown"
            fields[field_name] = field_type
            required.add(field_name)
        if fields:
            rel = path.relative_to(root).as_posix()
            return fields, required, [f"inferred from model {model_name} in {rel}"]
    return {}, set(), []


def _extract_fields_from_flow_evidence(trace_result: TraceResult) -> tuple[dict[str, str], set[str], list[str]]:
    """Infer request-body field names from deterministic flow evidence snippets."""
    fields: dict[str, str] = {}
    required: set[str] = set()
    notes: list[str] = []
    by_id = {node.id: node for node in trace_result.nodes}
    for node in _selected_flow_nodes(trace_result, node_by_id=by_id):
        for ref in node.evidence:
            snippet = ref.snippet or ref.label or ""
            if not snippet:
                continue
            for field in PY_LITERAL_FIELD_REQUIRED_RE.findall(snippet):
                fields.setdefault(field, "string")
                required.add(field)
                notes.append(f"field '{field}' inferred from {ref.file}:{ref.symbol or 'handler'}")
            for field in PY_LITERAL_FIELD_OPTIONAL_RE.findall(snippet):
                fields.setdefault(field, "unknown")
                notes.append(f"optional field '{field}' inferred from {ref.file}:{ref.symbol or 'handler'}")
    return fields, required, notes


def _extract_fields_from_test_payload_examples(trace_result: TraceResult, route: str, method: str) -> tuple[dict[str, str], list[str]]:
    """Use test client payload examples as supporting request-body hints."""
    normalized_route = route.rstrip("/") or "/"
    fields: dict[str, str] = {}
    notes: list[str] = []
    for repo in trace_result.repos:
        root = Path(repo.root)
        if not root.exists():
            continue
        checked = 0
        for path in root.rglob("test*.py"):
            checked += 1
            if checked > 120:
                break
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in PY_TEST_JSON_PAYLOAD_RE.finditer(text):
                route_hint = (match.group("path") or "").rstrip("/") or "/"
                method_hint = (match.group(1) or "").upper()
                if method_hint != method.upper() or route_hint != normalized_route:
                    continue
                payload = match.group("body")
                try:
                    parsed = ast.literal_eval(payload)
                except Exception:
                    continue
                if not isinstance(parsed, dict):
                    continue
                rel = path.relative_to(root).as_posix()
                notes.append(f"request body example inferred from {rel}")
                for key, value in parsed.items():
                    if isinstance(key, str):
                        fields[key] = _py_value_type(value)
    return fields, notes


def _route_consumes_request_body(trace_result: TraceResult) -> tuple[bool, list[str]]:
    """Infer request-body consumption from generic flow/node/evidence signals."""
    by_id = {node.id: node for node in trace_result.nodes}
    notes: list[str] = []
    for node in _selected_flow_nodes(trace_result, node_by_id=by_id):
        metadata = node.metadata if isinstance(node.metadata, dict) else {}
        step_kind = str(metadata.get("step_kind") or "").strip().lower()
        if step_kind in {"input", "input_model"}:
            notes.append(f"request body signal from step_kind={step_kind}")
            return True, notes

        text_parts: list[str] = []
        if node.name:
            text_parts.append(node.name)
        for value in metadata.values():
            if isinstance(value, str):
                text_parts.append(value)
        for ref in node.evidence:
            if ref.label:
                text_parts.append(ref.label)
            if ref.snippet:
                text_parts.append(ref.snippet)
        blob = " ".join(text_parts).lower()
        for token in REQUEST_BODY_SIGNAL_TOKENS:
            if token in blob:
                notes.append(f"request body signal from token '{token}'")
                return True, notes
    return False, notes


def infer_request_body_signal(trace_result: TraceResult) -> RequestBodySignal:
    """Infer generic request-body input signal from trace evidence."""
    method = (trace_result.target.method or "ANY").upper()

    fields: dict[str, str] = {}
    required: set[str] = set()
    notes: list[str] = []
    consumed, consumed_notes = _route_consumes_request_body(trace_result)
    notes.extend(consumed_notes)

    input_model_hint = _extract_input_model_hint(trace_result)
    if input_model_hint:
        consumed = True
        model_name = input_model_hint.split("[", 1)[0].split(".", 1)[-1].strip()
        for repo in trace_result.repos:
            model_fields, model_required, model_notes = _extract_fields_from_model_class(repo.root, model_name)
            for name, value in model_fields.items():
                fields[name] = value
            required.update(model_required)
            notes.extend(model_notes)
            if model_fields:
                break

    flow_fields, flow_required, flow_notes = _extract_fields_from_flow_evidence(trace_result)
    if flow_fields:
        consumed = True
    for name, value in flow_fields.items():
        fields.setdefault(name, value)
    required.update(flow_required)
    notes.extend(flow_notes)

    test_fields, test_notes = _extract_fields_from_test_payload_examples(
        trace_result,
        route=trace_result.target.path,
        method=method,
    )
    if test_fields:
        consumed = True
    for name, value in test_fields.items():
        if name not in fields or fields[name] == "unknown":
            fields[name] = value
    notes.extend(test_notes)

    deduped_notes = list(dict.fromkeys(notes))
    if not consumed:
        return RequestBodySignal(
            consumed=False,
            shape=None,
            required=False,
            required_fields=set(),
            notes=deduped_notes,
        )

    if fields:
        shape: dict[str, Any] = dict(fields)
    else:
        shape = {"type": "object", "description": "valid JSON payload"}
        deduped_notes.append("request body consumed but exact fields were not inferred")

    # For consumed request bodies, keep required=True so tests include an explicit payload input.
    return RequestBodySignal(
        consumed=True,
        shape=shape,
        required=True,
        required_fields=required,
        notes=deduped_notes,
    )


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
    body_shape: dict[str, str] | None = None,
    body_required: bool | None = None,
    notes: list[str] | None = None,
) -> IntegrationTestSuggestion:
    """Create deterministic matrix suggestion with minimal repeated boilerplate."""
    suggestion_notes = list(notes or [])
    if not any("deterministic" in note.lower() for note in suggestion_notes):
        suggestion_notes.append("deterministic baseline derived from traced flow evidence")
    inputs = [
        TestInputHint(kind="request_path", value_hint=route, required=True),
        TestInputHint(kind="http_method", value_hint=method, required=True),
    ]
    if body_shape:
        inputs.append(
            TestInputHint(
                kind="request_body",
                value_hint=body_shape,
                required=body_required,
            )
        )
    return make_test_suggestion(
        name=name,
        route=route,
        method=method,
        summary=summary,
        inputs=inputs,
        expectations=expectations,
        derived_from_flow_id=flow_id,
        confidence=confidence,
        notes=suggestion_notes,
    )


def scenario_id_from_name(name: str) -> str:
    """Build a stable scenario id from a suggestion name."""
    token = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return token or "scenario"


def make_test_suggestion(
    *,
    name: str,
    route: str,
    method: str | None = None,
    summary: str | None = None,
    inputs: list[TestInputHint] | None = None,
    expectations: list[TestExpectation] | None = None,
    derived_from_flow_id: str | None = None,
    confidence: float | None = None,
    notes: list[str] | None = None,
    category: str | None = None,
    priority: str | None = None,
    purpose: str | None = None,
    request: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    side_effects: list[str] | None = None,
    related_steps: list[str] | None = None,
    related_sinks: list[str] | None = None,
    contract_refs: list[str] | None = None,
    requires_mocking: bool | None = None,
    notes_text: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> IntegrationTestSuggestion:
    """Create a v2-capable suggestion while preserving old required fields."""
    normalized_category = category or ("positive" if (method or "").upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "edge_case")
    normalized_priority = priority or "medium"
    request_payload = request or {
        "method": (method or "ANY").upper(),
        "path": route,
    }
    expected_payload = expected or {}
    expected_payload.setdefault("status", None)
    expected_payload.setdefault("behavior", summary)

    return IntegrationTestSuggestion(
        name=name,
        route=route,
        method=method,
        summary=summary,
        inputs=list(inputs or []),
        expectations=list(expectations or []),
        derived_from_flow_id=derived_from_flow_id,
        confidence=confidence,
        notes=list(notes or []),
        category=normalized_category,
        priority=normalized_priority,
        purpose=purpose,
        request=request_payload,
        expected=expected_payload,
        side_effects=list(side_effects or []),
        related_steps=list(related_steps or []),
        related_sinks=list(related_sinks or []),
        contract_refs=list(contract_refs or []),
        requires_mocking=requires_mocking,
        notes_text=notes_text,
        evidence=list(evidence or []),
    )


def normalize_test_matrix(matrix: TestMatrix) -> TestMatrix:
    """Ensure matrix suggestions include safe v2 defaults without changing grouping."""
    normalized_groups: list[TestMatrixGroup] = []
    for group in matrix.groups:
        normalized_tests = []
        for test in group.tests:
            effective_category = test.category or group.category
            if (
                test.category == "positive"
                and _canonical_test_category(group.category) != "positive"
                and not test.contract_refs
                and not test.related_sinks
                and not test.side_effects
            ):
                effective_category = group.category
            normalized_tests.append(
                make_test_suggestion(
                name=test.name,
                route=test.route,
                method=test.method,
                summary=test.summary,
                inputs=test.inputs,
                expectations=test.expectations,
                derived_from_flow_id=test.derived_from_flow_id,
                confidence=test.confidence,
                notes=test.notes,
                category=effective_category,
                priority=test.priority,
                purpose=test.purpose,
                request=test.request,
                expected=test.expected,
                side_effects=test.side_effects,
                related_steps=test.related_steps,
                related_sinks=test.related_sinks,
                contract_refs=test.contract_refs,
                requires_mocking=test.requires_mocking,
                notes_text=test.notes_text,
                evidence=test.evidence,
            )
            )
        normalized_groups.append(
            TestMatrixGroup(
                category=group.category,
                title=group.title,
                tests=normalized_tests,
                notes=group.notes,
            )
        )
    return TestMatrix(
        groups=normalized_groups,
        notes=matrix.notes,
        coverage=matrix.coverage,
        confidence=matrix.confidence,
    )

CATEGORY_ALIASES = {
    TEST_MATRIX_CATEGORY_HAPPY_PATH: "positive",
    "happy_path": "positive",
    "data_shape": "response_schema",
    "authn": "auth",
    "authorization": "authorization",
    "validation_error": "validation",
    TEST_MATRIX_CATEGORY_VALIDATION: "validation",
    TEST_MATRIX_CATEGORY_SIDE_EFFECTS: "side_effect",
    "side_effects": "side_effect",
    TEST_MATRIX_CATEGORY_STATE_CONSISTENCY: "business_rule",
    "state_consistency": "business_rule",
    TEST_MATRIX_CATEGORY_EDGE_CASES: "edge_case",
    "edge_cases": "edge_case",
    "failure_modes": "error_handling",
    "persistence": "database",
    "downstream_failure": "external_api",
    "cross_service_contract": "response_schema",
}

ACCEPTED_TEST_MATRIX_CATEGORIES = {
    "positive",
    "validation",
    "auth",
    "authorization",
    "business_rule",
    "database",
    "cache",
    "external_api",
    "queue_event",
    "error_handling",
    "security",
    "edge_case",
    "response_schema",
    "side_effect",
}

CATEGORY_ORDER = [
    "positive",
    "validation",
    "auth",
    "authorization",
    "business_rule",
    "database",
    "cache",
    "external_api",
    "queue_event",
    "error_handling",
    "security",
    "response_schema",
    "side_effect",
    "edge_case",
]

GENERIC_CATEGORY = "edge_case"


def _canonical_test_category(category: str | None) -> str:
    raw = (category or GENERIC_CATEGORY).strip().lower()
    canonical = CATEGORY_ALIASES.get(raw, raw)
    return canonical if canonical in ACCEPTED_TEST_MATRIX_CATEGORIES else GENERIC_CATEGORY


def _status_key(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _status_value(status: str | int | None) -> int | str | None:
    if status is None:
        return None
    text = str(status)
    return int(text) if text.isdigit() else status


def _available_response_statuses(route_contract: ApiRouteContract | None) -> set[str]:
    if route_contract is None:
        return set()
    return {str(key) for key in route_contract.responses.keys()}


def _preferred_success_status(method: str | None, route_contract: ApiRouteContract | None) -> str:
    statuses = _available_response_statuses(route_contract)
    method_upper = (method or route_contract.method if route_contract else method or "GET").upper()
    if method_upper == "POST":
        return "201" if "201" in statuses else "200"
    if method_upper == "GET":
        return "200"
    if method_upper in {"PUT", "PATCH"}:
        return "200" if "200" in statuses else "204"
    if method_upper == "DELETE":
        return "204" if "204" in statuses else "200"
    return next((status for status in statuses if status.startswith("2")), "200")


def _contract_route_from_input(
    api_contract: ApiRouteContract | ApiContractArtifact | None,
    *,
    method: str | None,
    path: str,
) -> ApiRouteContract | None:
    if api_contract is None:
        return None
    if isinstance(api_contract, ApiRouteContract):
        return api_contract
    if isinstance(api_contract, ApiContractArtifact):
        return match_route_contract(api_contract, method=method, path=path)
    return None


def _route_method_path_from_matrix(matrix: TestMatrix) -> tuple[str | None, str]:
    for group in matrix.groups:
        for test in group.tests:
            method = test.method or (test.request or {}).get("method")
            path = test.route or (test.request or {}).get("path") or "/"
            return (str(method).upper() if method else None, str(path))
    return None, "/"


def _request_body_example_without_field(route_contract: ApiRouteContract, missing_field: str) -> dict[str, Any]:
    body = route_contract.request.body
    if body is None:
        return {}
    payload: dict[str, Any] = {}
    for key, prop in body.properties.items():
        if key == missing_field:
            continue
        payload[key] = prop.example if prop.example is not None else _example_for_schema_type(prop.type)
    return payload


def _has_body_ref(test: IntegrationTestSuggestion, field: str, *tokens: str) -> bool:
    refs = {str(ref) for ref in test.contract_refs}
    if f"request.body.{field}" not in refs:
        return False
    blob = f"{test.name} {test.summary or ''} {test.purpose or ''}".lower()
    return any(token in blob for token in tokens)


def _has_malformed_json_scenario(tests: list[IntegrationTestSuggestion]) -> bool:
    return any("malformed" in f"{test.name} {test.summary or ''}".lower() and "json" in f"{test.name} {test.summary or ''}".lower() for test in tests)


def _has_response_schema_scenario(tests: list[IntegrationTestSuggestion], status: str) -> bool:
    ref = f"responses.{status}"
    return any(ref in {str(item) for item in test.contract_refs} and (test.expected or {}).get("response_schema_ref") == ref for test in tests)


def _scenario_quality_score(test: IntegrationTestSuggestion) -> int:
    score = 0
    request = test.request or {}
    expected = test.expected or {}
    category = _canonical_test_category(test.category)
    if request.get("method") and request.get("path"):
        score += 5
    if expected.get("status") is not None:
        score += 5
    else:
        score -= 5
    if test.contract_refs:
        score += 4
    if test.related_steps or test.related_sinks:
        score += 3
    if test.purpose:
        score += 3
    if test.side_effects:
        score += 2
    if test.evidence:
        score += 2
    if category not in {"edge_case"}:
        score += 2
    name_blob = f"{test.name} {test.summary or ''}".lower()
    if "invalid payload" in name_blob or "invalid_payload" in name_blob:
        score -= 4
    if "missing required field" in name_blob or "missing_required_field" in name_blob:
        score -= 4
    method = str(request.get("method") or test.method or "").upper()
    if category == "validation" and method in WRITE_METHODS and not (request.get("body") or request.get("raw_body")):
        score -= 3
    return score


def _scenario_intent_key(test: IntegrationTestSuggestion) -> tuple[str, str, str, str, tuple[str, ...], str]:
    category = _canonical_test_category(test.category)
    request = test.request or {}
    expected = test.expected or {}
    method = str(request.get("method") or test.method or "").upper()
    path = _normalize_contract_path(str(request.get("path") or test.route or ""))
    status = _status_key(expected.get("status"))
    refs = tuple(sorted(str(ref) for ref in test.contract_refs))
    name = scenario_id_from_name(test.name)
    name = re.sub(r"^(get|post|put|patch|delete|any)_", "", name)
    name = re.sub(r"_(contract_)?happy_path$", "_happy_path", name)
    return category, method, path, status, refs, name


def _generic_suppression_reason(test: IntegrationTestSuggestion, *, field_missing: bool, field_invalid: bool, response_schema: bool, contract_happy: bool) -> str | None:
    blob = f"{test.name} {test.summary or ''} {test.purpose or ''}".lower()
    category = _canonical_test_category(test.category)
    has_body_field_ref = any(str(ref).startswith("request.body.") for ref in test.contract_refs)
    if field_missing and not has_body_field_ref and ("missing_required_field" in blob or "missing required field" in blob):
        return "suppressed generic missing-required-field scenario"
    if field_invalid and not has_body_field_ref and ("invalid_payload" in blob or "invalid payload" in blob):
        return "suppressed generic invalid-payload scenario"
    if response_schema and category == "response_schema" and not test.contract_refs:
        return "suppressed generic response-shape scenario"
    if contract_happy and category == "positive" and not test.related_sinks and not test.side_effects:
        if any(token in blob for token in ("creates_resource", "returns_success", "happy_path", "updates_resource", "deletes_resource")):
            if "contract" in blob:
                return None
            return "suppressed generic happy-path scenario"
    return None


def _scenario_cap(method: str | None, path: str) -> int:
    lowered_path = (path or "").lower()
    if any(token in lowered_path for token in ("/health", "/ready", "/live", "/ping", "/status")):
        return 2
    method_upper = (method or "GET").upper()
    if method_upper == "GET":
        return 8
    if method_upper in {"POST", "PUT", "PATCH", "DELETE"}:
        return 12
    return 8


def _is_field_validation(test: IntegrationTestSuggestion) -> bool:
    return _canonical_test_category(test.category) == "validation" and any(str(ref).startswith("request.body.") for ref in test.contract_refs)


def _is_sink_scenario(test: IntegrationTestSuggestion) -> bool:
    return bool(test.related_sinks or test.side_effects) or _canonical_test_category(test.category) in {"database", "cache", "external_api", "queue_event", "side_effect"}


def _cap_scenarios(tests: list[IntegrationTestSuggestion], *, method: str | None, path: str) -> list[IntegrationTestSuggestion]:
    cap = _scenario_cap(method, path)
    if len(tests) <= cap:
        return tests
    indexed = list(enumerate(tests))
    required_indices: set[int] = set()
    for predicate in (
        lambda t: _canonical_test_category(t.category) == "positive",
        _is_field_validation,
        lambda t: _canonical_test_category(t.category) == "response_schema",
        _is_sink_scenario,
    ):
        match = next((idx for idx, test in indexed if predicate(test)), None)
        if match is not None:
            required_indices.add(match)
    ordered_by_quality = sorted(indexed, key=lambda item: (_scenario_quality_score(item[1]), -item[0]), reverse=True)
    selected = set(required_indices)
    for idx, _test in ordered_by_quality:
        if len(selected) >= cap:
            break
        selected.add(idx)
    return [test for idx, test in indexed if idx in selected]


def clean_test_matrix(
    matrix: TestMatrix,
    api_contract: ApiRouteContract | ApiContractArtifact | None = None,
    trace_result: TraceResult | None = None,
) -> TestMatrix:
    """Apply deterministic quality cleanup, dedupe, and contract-aware gap filling."""
    try:
        normalized = normalize_test_matrix(matrix)
        method, path = _route_method_path_from_matrix(normalized)
        if trace_result is not None:
            method = trace_result.target.method or method
            path = trace_result.target.path or path
        route_contract = _contract_route_from_input(api_contract, method=method, path=path)
        if route_contract is not None:
            method = (method or route_contract.method or "GET").upper()
            if not path or path == "/":
                path = route_contract.path or "/"
        sink_labels = _collect_sink_labels(trace_result) if trace_result is not None else []
        related_steps = _collect_related_step_labels(trace_result) if trace_result is not None else []

        tests: list[IntegrationTestSuggestion] = []
        for group in normalized.groups:
            canonical_group_category = _canonical_test_category(group.category)
            for test in group.tests:
                canonical = _canonical_test_category(test.category or canonical_group_category)
                request = dict(test.request or {})
                request.setdefault("method", (test.method or method or "ANY").upper())
                request.setdefault("path", test.route or path or "/")
                expected = dict(test.expected or {})
                expected.setdefault("status", None)
                test.category = canonical
                test.request = request
                test.expected = expected
                if test.method is None and request.get("method"):
                    test.method = str(request.get("method"))
                tests.append(test)

        if route_contract is not None:
            success_status = _preferred_success_status(method, route_contract)
            success_ref = f"responses.{success_status}"
            for test in tests:
                if _canonical_test_category(test.category) != "positive":
                    continue
                expected = dict(test.expected or {})
                expected["status"] = _status_value(success_status)
                test.contract_refs = [
                    ref for ref in test.contract_refs
                    if not str(ref).startswith("responses.")
                ]
                if success_status in route_contract.responses:
                    expected["response_schema_ref"] = success_ref
                    if success_ref not in test.contract_refs:
                        test.contract_refs.append(success_ref)
                test.expected = expected

            body = route_contract.request.body
            if body is not None and body.type == "object":
                body_shape = {
                    key: (prop.type or "unknown")
                    for key, prop in body.properties.items()
                } or {"type": "object"}
                body_required = bool(body.required)
                added = 0
                for field in body.required:
                    if added >= 3:
                        break
                    if any(_has_body_ref(test, field, "missing") for test in tests):
                        continue
                    expected_status = 400 if "400" in route_contract.responses else None
                    tests.append(
                        make_test_suggestion(
                            name=f"{(method or 'ANY').lower()}_{_route_token(path)}_missing_{field}",
                            route=path,
                            method=method,
                            summary=f"rejects request missing required body field `{field}`",
                            category="validation",
                            priority="high",
                            purpose="required field validation",
                            request={
                                "method": method,
                                "path": path,
                                "body": _request_body_example_without_field(route_contract, field),
                            },
                            expected={"status": expected_status, "behavior": "validation error"},
                            contract_refs=[f"request.body.{field}"],
                            side_effects=["No database write should occur"] if any(s.startswith("database:") for s in sink_labels) else [],
                            related_sinks=sink_labels,
                        )
                    )
                    added += 1
                if (method or "").upper() in WRITE_METHODS and not _has_malformed_json_scenario(tests):
                    tests.append(
                        make_test_suggestion(
                            name=f"{(method or 'ANY').lower()}_{_route_token(path)}_malformed_json",
                            route=path,
                            method=method,
                            summary="rejects malformed JSON request body",
                            category="validation",
                            priority="medium",
                            purpose="malformed JSON validation",
                            request={"method": method, "path": path, "raw_body": "{malformed-json"},
                            expected={"status": 400 if "400" in route_contract.responses else None, "behavior": "validation error"},
                            contract_refs=["request.body"],
                        )
                    )
                for test in tests:
                    request_method = str((test.request or {}).get("method") or test.method or method or "").upper()
                    if request_method not in WRITE_METHODS:
                        continue
                    if any(item.kind == "request_body" for item in test.inputs):
                        continue
                    if not ((test.request or {}).get("body") or (test.request or {}).get("raw_body") or _canonical_test_category(test.category) in {"positive", "validation"}):
                        continue
                    test.inputs.append(
                        TestInputHint(
                            kind="request_body",
                            value_hint=body_shape,
                            required=body_required,
                        )
                    )
            success_response = route_contract.responses.get(success_status)
            if success_response is not None and success_response.body is not None and success_response.body.properties:
                if not _has_response_schema_scenario(tests, success_status):
                    tests.append(
                        make_test_suggestion(
                            name=f"{(method or 'ANY').lower()}_{_route_token(path)}_response_schema_validation",
                            route=path,
                            method=method,
                            summary="validates response body shape matches contract",
                            category="response_schema",
                            priority="medium",
                            purpose="response schema validation",
                            expected={
                                "status": _status_value(success_status),
                                "response_schema_ref": f"responses.{success_status}",
                            },
                            contract_refs=[f"responses.{success_status}"],
                            related_steps=related_steps,
                        )
                    )

        field_missing_exists = any(
            _canonical_test_category(test.category) == "validation"
            and any(str(ref).startswith("request.body.") for ref in test.contract_refs)
            and "missing" in f"{test.name} {test.summary or ''}".lower()
            for test in tests
        )
        field_invalid_exists = any(
            _canonical_test_category(test.category) == "validation"
            and any(str(ref).startswith("request.body.") for ref in test.contract_refs)
            and any(token in f"{test.name} {test.summary or ''}".lower() for token in ("invalid", "type", "email", "enum"))
            for test in tests
        )
        response_schema_exists = any(
            any(str(ref).startswith("responses.") for ref in test.contract_refs)
            and (test.expected or {}).get("response_schema_ref")
            for test in tests
        )
        contract_happy_exists = any(
            _canonical_test_category(test.category) == "positive"
            and any(str(ref).startswith("responses.") for ref in test.contract_refs)
            and "contract" in f"{test.name} {test.summary or ''} {test.purpose or ''}".lower()
            for test in tests
        )

        filtered: list[IntegrationTestSuggestion] = []
        for test in tests:
            reason = _generic_suppression_reason(
                test,
                field_missing=field_missing_exists,
                field_invalid=field_invalid_exists,
                response_schema=response_schema_exists,
                contract_happy=contract_happy_exists,
            )
            if reason:
                continue
            filtered.append(test)

        best_by_key: dict[tuple[str, str, str, str, tuple[str, ...], str], tuple[int, int, IntegrationTestSuggestion]] = {}
        best_by_name: dict[str, tuple[int, int, IntegrationTestSuggestion]] = {}
        for idx, test in enumerate(filtered):
            score = _scenario_quality_score(test)
            intent_key = _scenario_intent_key(test)
            name_key = scenario_id_from_name(test.name)
            existing = best_by_key.get(intent_key)
            if existing is None or score > existing[1]:
                best_by_key[intent_key] = (idx, score, test)
            existing_name = best_by_name.get(name_key)
            if existing_name is None or score > existing_name[1]:
                best_by_name[name_key] = (idx, score, test)
        selected_by_id: dict[int, IntegrationTestSuggestion] = {}
        allowed_ids = {id(item[2]) for item in best_by_key.values()} & {id(item[2]) for item in best_by_name.values()}
        for idx, _score, test in sorted(best_by_key.values(), key=lambda item: item[0]):
            if id(test) in allowed_ids:
                selected_by_id[idx] = test
        deduped = [selected_by_id[idx] for idx in sorted(selected_by_id)]
        deduped = _cap_scenarios(deduped, method=method, path=path)

        grouped: dict[str, list[IntegrationTestSuggestion]] = {category: [] for category in CATEGORY_ORDER}
        for test in deduped:
            grouped.setdefault(_canonical_test_category(test.category), []).append(test)
        groups = [
            TestMatrixGroup(category=category, tests=items)
            for category in CATEGORY_ORDER
            for items in [grouped.get(category, [])]
            if items
        ]
        notes = list(normalized.notes)
        if route_contract is not None and "test matrix cleaned with deterministic contract-aware quality filters" not in notes:
            notes.append("test matrix cleaned with deterministic contract-aware quality filters")
        return TestMatrix(groups=groups, notes=notes, coverage=matrix.coverage, confidence=matrix.confidence)
    except Exception as exc:  # noqa: BLE001
        fallback = matrix.model_copy(deep=True)
        note = f"test matrix cleanup skipped: {exc}"
        if note not in fallback.notes:
            fallback.notes.append(note)
        return fallback


def _example_for_schema_type(schema_type: str | None) -> Any:
    lowered = (schema_type or "string").lower()
    if lowered == "integer":
        return 1
    if lowered == "number":
        return 1.25
    if lowered == "boolean":
        return True
    if lowered == "array":
        return []
    if lowered == "object":
        return {}
    return "example"


def _invalid_for_schema_type(schema_type: str | None) -> Any:
    lowered = (schema_type or "string").lower()
    if lowered in {"integer", "number"}:
        return "not-a-number"
    if lowered == "boolean":
        return "not-a-bool"
    if lowered == "array":
        return "not-an-array"
    if lowered == "object":
        return "not-an-object"
    return 12345


def _build_request_from_contract(route_contract: ApiRouteContract, method: str, path: str) -> dict[str, Any]:
    request_payload: dict[str, Any] = {
        "method": method,
        "path": path,
        "headers": {},
        "query": {},
    }
    for key, prop in route_contract.request.headers.items():
        if prop.example is not None:
            request_payload["headers"][key] = prop.example
        else:
            request_payload["headers"][key] = _example_for_schema_type(prop.type)
    for key, prop in route_contract.request.query_params.items():
        if prop.example is not None:
            request_payload["query"][key] = prop.example
        else:
            request_payload["query"][key] = _example_for_schema_type(prop.type)
    if route_contract.request.body is not None:
        body: dict[str, Any] = {}
        for key, prop in route_contract.request.body.properties.items():
            if prop.example is not None:
                body[key] = prop.example
            else:
                body[key] = _example_for_schema_type(prop.type)
        request_payload["body"] = body
    return request_payload


def _collect_sink_labels(trace_result: TraceResult) -> list[str]:
    labels: list[str] = []
    for node in trace_result.nodes:
        if node.type not in {"database", "external_api", "queue", "file_sink"}:
            continue
        labels.append(f"{node.type}:{node.name}")
    return labels


def _collect_related_step_labels(trace_result: TraceResult, limit: int = 4) -> list[str]:
    labels: list[str] = []
    for node in _selected_flow_nodes(trace_result):
        if node.type == "internal_step" and node.name:
            labels.append(node.name)
        if len(labels) >= limit:
            break
    return labels


def generate_contract_aware_test_matrix(
    *,
    trace_result: TraceResult,
    base_matrix: TestMatrix,
    route_contract: ApiRouteContract | None,
    max_validation_scenarios: int = 5,
    max_sink_scenarios: int = 4,
) -> TestMatrix:
    """Augment baseline matrix with contract-aware and sink-aware scenarios."""
    if route_contract is None:
        return normalize_test_matrix(base_matrix)

    method = (trace_result.target.method or route_contract.method or "ANY").upper()
    path = trace_result.target.path
    route_token = _route_token(path)
    method_token = method.lower()
    sink_labels = _collect_sink_labels(trace_result)
    related_steps = _collect_related_step_labels(trace_result)

    by_category: dict[str, list[IntegrationTestSuggestion]] = {
        group.category: list(group.tests) for group in base_matrix.groups
    }

    def add(category: str, suggestion: IntegrationTestSuggestion) -> None:
        by_category.setdefault(category, [])
        by_category[category].append(suggestion)

    # Happy path scenario using contract examples/schema.
    success_status = next(
        (status for status in route_contract.responses.keys() if str(status).startswith("2")),
        "200",
    )
    add(
        "positive",
        make_test_suggestion(
            name=f"{method_token}_{route_token}_contract_happy_path",
            route=path,
            method=method,
            summary=f"verifies {method} {path} succeeds with contract-valid request",
            category="positive",
            priority="high" if method in WRITE_METHODS or method == "DELETE" else "medium",
            purpose="contract happy path",
            request=_build_request_from_contract(route_contract, method, path),
            expected={
                "status": int(success_status) if str(success_status).isdigit() else success_status,
                "behavior": "request succeeds",
                "response_schema_ref": f"responses.{success_status}",
            },
            contract_refs=[f"responses.{success_status}"],
            related_steps=related_steps,
            related_sinks=sink_labels,
        ),
    )

    # Request-body validation scenarios.
    validation_count = 0
    body = route_contract.request.body
    if body is not None and body.properties:
        for field in body.required[:max_validation_scenarios]:
            add(
                "validation",
                make_test_suggestion(
                    name=f"{method_token}_{route_token}_missing_required_{field}",
                    route=path,
                    method=method,
                    summary=f"rejects missing required field `{field}`",
                    category="validation",
                    priority="high",
                    purpose="required field validation",
                    request={"method": method, "path": path, "body": {k: _example_for_schema_type(v.type) for k, v in body.properties.items() if k != field}},
                    expected={"status": 400, "behavior": "validation error"},
                    contract_refs=[f"request.body.{field}"],
                    side_effects=["No database write should occur"] if any(s.startswith("database:") for s in sink_labels) else [],
                    related_sinks=sink_labels,
                ),
            )
            validation_count += 1
            if validation_count >= max_validation_scenarios:
                break
        for field, prop in body.properties.items():
            if validation_count >= max_validation_scenarios:
                break
            add(
                "validation",
                make_test_suggestion(
                    name=f"{method_token}_{route_token}_invalid_type_{field}",
                    route=path,
                    method=method,
                    summary=f"rejects invalid type for `{field}`",
                    category="validation",
                    priority="medium",
                    purpose="schema type validation",
                    request={"method": method, "path": path, "body": {field: _invalid_for_schema_type(prop.type)}},
                    expected={"status": 400, "behavior": "type validation error"},
                    contract_refs=[f"request.body.{field}"],
                ),
            )
            validation_count += 1
            if prop.format == "email" and validation_count < max_validation_scenarios:
                add(
                    "validation",
                    make_test_suggestion(
                        name=f"{method_token}_{route_token}_invalid_email_{field}",
                        route=path,
                        method=method,
                        summary=f"rejects invalid email format for `{field}`",
                        category="validation",
                        priority="high",
                        purpose="format validation",
                        request={"method": method, "path": path, "body": {field: "not-an-email"}},
                        expected={"status": 400, "behavior": "format validation error"},
                        contract_refs=[f"request.body.{field}"],
                    ),
                )
                validation_count += 1

    # Path/query/header scenarios.
    for key, prop in route_contract.request.path_params.items():
        if prop.type in {"integer", "number"}:
            add(
                "validation",
                make_test_suggestion(
                    name=f"{method_token}_{route_token}_invalid_path_param_{key}",
                    route=path,
                    method=method,
                    summary=f"rejects invalid path parameter `{key}`",
                    category="validation",
                    priority="medium",
                    request={"method": method, "path": path, "path_params": {key: "not-a-number"}},
                    expected={"status": 400, "behavior": "path parameter validation error"},
                    contract_refs=[f"request.path_params.{key}"],
                ),
            )
        if method == "GET" and key.lower().endswith("id"):
            add(
                "edge_case",
                make_test_suggestion(
                    name=f"{method_token}_{route_token}_not_found_for_missing_{key}",
                    route=path,
                    method=method,
                    summary=f"returns not found for unknown `{key}`",
                    category="edge_case",
                    priority="medium",
                    expected={"status": 404, "behavior": "resource not found"},
                    contract_refs=[f"request.path_params.{key}"],
                ),
            )

    for key, prop in route_contract.request.query_params.items():
        if prop.required:
            add(
                "validation",
                make_test_suggestion(
                    name=f"{method_token}_{route_token}_missing_query_{key}",
                    route=path,
                    method=method,
                    summary=f"rejects missing required query param `{key}`",
                    category="validation",
                    priority="medium",
                    request={"method": method, "path": path, "query": {}},
                    expected={"status": 400, "behavior": "query validation error"},
                    contract_refs=[f"request.query.{key}"],
                ),
            )

    auth_header = route_contract.request.headers.get("Authorization")
    if auth_header is not None and (auth_header.required or True):
        add(
            "auth",
            make_test_suggestion(
                name=f"{method_token}_{route_token}_missing_authorization",
                route=path,
                method=method,
                summary="rejects missing Authorization header",
                category="auth",
                priority="high",
                request={"method": method, "path": path, "headers": {}},
                expected={"status": 401, "behavior": "unauthorized"},
                contract_refs=["request.headers.Authorization"],
            ),
        )
        add(
            "auth",
            make_test_suggestion(
                name=f"{method_token}_{route_token}_invalid_authorization",
                route=path,
                method=method,
                summary="rejects invalid or expired token",
                category="auth",
                priority="high",
                request={"method": method, "path": path, "headers": {"Authorization": "Bearer invalid-token"}},
                expected={"status": 401, "behavior": "unauthorized"},
                contract_refs=["request.headers.Authorization"],
            ),
        )

    # Response schema validation scenario.
    if success_status in route_contract.responses:
        response_schema = route_contract.responses[success_status].body
        sensitive = False
        if response_schema is not None:
            sensitive = any(
                token in key.lower()
                for key in response_schema.properties.keys()
                for token in ("password", "token", "secret", "api_key", "password_hash")
            )
        add(
            "security" if sensitive else "error_handling",
            make_test_suggestion(
                name=f"{method_token}_{route_token}_response_schema_validation",
                route=path,
                method=method,
                summary="validates response body shape matches contract",
                category="security" if sensitive else "error_handling",
                priority="medium",
                expected={
                    "status": int(success_status) if str(success_status).isdigit() else success_status,
                    "response_schema_ref": f"responses.{success_status}",
                },
                contract_refs=[f"responses.{success_status}"],
                notes_text=(
                    "Response must not expose sensitive internal fields."
                    if sensitive
                    else "Response should match documented schema."
                ),
            ),
        )

    # Sink-aware scenarios.
    sink_added = 0
    if any(s.startswith("database:") for s in sink_labels):
        add(
            "database",
            make_test_suggestion(
                name=f"{method_token}_{route_token}_database_failure_handled",
                route=path,
                method=method,
                summary="handles database failure without partial write",
                category="database",
                priority="high",
                requires_mocking=True,
                expected={"status": 500, "behavior": "database error handled"},
                related_sinks=[s for s in sink_labels if s.startswith("database:")],
                side_effects=["rollback or no partial write"],
            ),
        )
        sink_added += 1
    if sink_added < max_sink_scenarios and any(s.startswith("external_api:") for s in sink_labels):
        add(
            "external_api",
            make_test_suggestion(
                name=f"{method_token}_{route_token}_downstream_timeout_handled",
                route=path,
                method=method,
                summary="handles downstream timeout gracefully",
                category="external_api",
                priority="medium",
                requires_mocking=True,
                expected={"status": 502, "behavior": "downstream failure handled"},
                related_sinks=[s for s in sink_labels if s.startswith("external_api:")],
            ),
        )
        sink_added += 1
    if sink_added < max_sink_scenarios and any(s.startswith("queue:") for s in sink_labels):
        add(
            "queue_event",
            make_test_suggestion(
                name=f"{method_token}_{route_token}_queue_publish_failure_handled",
                route=path,
                method=method,
                summary="handles queue publish failure correctly",
                category="queue_event",
                priority="high",
                requires_mocking=True,
                expected={"status": 500, "behavior": "publish failure handled"},
                related_sinks=[s for s in sink_labels if s.startswith("queue:")],
            ),
        )

    # Deduplicate and cap by keeping first occurrence by name.
    ordered_categories = [
        "positive",
        TEST_MATRIX_CATEGORY_HAPPY_PATH,
        "validation",
        TEST_MATRIX_CATEGORY_VALIDATION,
        "auth",
        "database",
        "external_api",
        "queue_event",
        TEST_MATRIX_CATEGORY_SIDE_EFFECTS,
        TEST_MATRIX_CATEGORY_STATE_CONSISTENCY,
        TEST_MATRIX_CATEGORY_EDGE_CASES,
        "error_handling",
        "security",
        "edge_case",
        "data_shape",
        "failure_modes",
        "persistence",
    ]
    seen: set[str] = set()
    groups: list[TestMatrixGroup] = []
    for category in ordered_categories:
        tests = by_category.get(category, [])
        if not tests:
            continue
        unique: list[IntegrationTestSuggestion] = []
        for test in tests:
            key = f"{test.category or category}:{scenario_id_from_name(test.name)}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(test)
        if unique:
            groups.append(TestMatrixGroup(category=category, tests=unique))

    notes = list(base_matrix.notes)
    notes.append("contract-aware scenarios added from inferred API contract and trace sinks")
    return clean_test_matrix(
        TestMatrix(groups=groups, notes=notes, coverage=base_matrix.coverage, confidence=base_matrix.confidence),
        api_contract=route_contract,
        trace_result=trace_result,
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


def generate_test_matrix(
    trace_result: TraceResult,
    *,
    max_suggestions: int = 7,
    route_contract: ApiRouteContract | None = None,
) -> TestMatrix:
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
    body_signal = infer_request_body_signal(trace_result)
    body_shape = body_signal.shape if body_signal.consumed else None
    body_required = body_signal.required if body_signal.consumed else None
    body_notes = body_signal.notes

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

    if body_signal.consumed and body_shape:
        for group in matrix_groups:
            for suggestion in group.tests:
                if any(item.kind == "request_body" for item in suggestion.inputs):
                    continue
                suggestion.inputs.append(
                    TestInputHint(
                        kind="request_body",
                        value_hint=body_shape,
                        required=body_required,
                    )
                )
                for note in body_notes:
                    if note not in suggestion.notes:
                        suggestion.notes.append(note)
    base = normalize_test_matrix(TestMatrix(groups=matrix_groups, notes=notes))
    return generate_contract_aware_test_matrix(
        trace_result=trace_result,
        base_matrix=base,
        route_contract=route_contract,
    )


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
    body_signal = infer_request_body_signal(trace_result)
    body_shape = body_signal.shape if body_signal.consumed else None
    body_required = body_signal.required if body_signal.consumed else None
    body_notes = body_signal.notes

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

    if body_signal.consumed and body_shape:
        for suggestion in suggestions:
            if not any(item.kind == "request_path" for item in suggestion.inputs):
                suggestion.inputs.append(TestInputHint(kind="request_path", value_hint=route, required=True))
            if not any(item.kind == "http_method" for item in suggestion.inputs):
                suggestion.inputs.append(TestInputHint(kind="http_method", value_hint=method, required=True))
            if not any(item.kind == "request_body" for item in suggestion.inputs):
                suggestion.inputs.append(
                    TestInputHint(kind="request_body", value_hint=body_shape, required=body_required)
                )
            for note in body_notes:
                if note not in suggestion.notes:
                    suggestion.notes.append(note)
    return _unique_suggestions(suggestions)[:3]
