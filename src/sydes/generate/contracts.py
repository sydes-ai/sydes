"""API contract artifact generation from discovered routes."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from sydes.core.models import (
    ApiContractArtifact,
    ApiContractEvidence,
    ApiRequestContract,
    ApiResponseContract,
    ApiRouteContract,
    ApiSchema,
    ApiSchemaProperty,
    EndpointCandidate,
    RoutesResult,
)

_PATH_PARAM_PATTERNS = [
    re.compile(r"<(?:(?P<conv>[^:>]+):)?(?P<name>[A-Za-z_]\w*)>"),
    re.compile(r"\{([A-Za-z_]\w*)\}"),
    re.compile(r":([A-Za-z_]\w*)"),
]

_JSON_SOURCE_ASSIGN_PATTERN = re.compile(
    r"^\s*(?P<name>[A-Za-z_]\w*)\s*=\s*request\.(?:get_json\(\s*\)|json\b)",
    re.MULTILINE,
)
_FIELD_GET_PATTERN = r"{var}\s*\.\s*get\(\s*['\"](?P<field>[A-Za-z_][\w-]*)['\"]"
_FIELD_INDEX_PATTERN = r"{var}\s*\[\s*['\"](?P<field>[A-Za-z_][\w-]*)['\"]\s*\]"
_REQUIRED_NOT_GET_PATTERN = r"if\s+not\s+{var}\s*\.\s*get\(\s*['\"]{field}['\"]"
_REQUIRED_NOT_IN_PATTERN = r"['\"]{field}['\"]\s+not\s+in\s+{var}\b"
_REQUEST_ARGS_GET_PATTERN = re.compile(r"request\.args\.get\(\s*['\"](?P<field>[A-Za-z_][\w-]*)['\"]")
_REQUEST_ARGS_INDEX_PATTERN = re.compile(r"request\.args\s*\[\s*['\"](?P<field>[A-Za-z_][\w-]*)['\"]\s*\]")
_REQUEST_HEADERS_GET_PATTERN = re.compile(r"request\.headers\.get\(\s*['\"](?P<field>[^'\"]+)['\"]")
_REQUEST_HEADERS_INDEX_PATTERN = re.compile(r"request\.headers\s*\[\s*['\"](?P<field>[^'\"]+)['\"]\s*\]")
_DIRECT_JSON_GET_PATTERN = re.compile(
    r"request\.(?:get_json\(\s*\)|json\b)\s*\.\s*get\(\s*['\"](?P<field>[A-Za-z_][\w-]*)['\"]"
)
_DIRECT_JSON_INDEX_PATTERN = re.compile(
    r"request\.(?:get_json\(\s*\)|json\b)\s*\[\s*['\"](?P<field>[A-Za-z_][\w-]*)['\"]\s*\]"
)
_AUTH_HINT_PATTERN = re.compile(
    r"authorization|bearer|require_auth|get_current_user|login_required|jwt",
    re.IGNORECASE,
)


@dataclass
class _SourceExtraction:
    request: ApiRequestContract
    responses: dict[str, ApiResponseContract]
    evidence: list[ApiContractEvidence]
    notes: list[str]


def _infer_param_type(name: str, converter: str | None = None) -> str:
    if converter:
        lowered = converter.lower()
        if lowered in {"int", "integer"}:
            return "integer"
        if lowered in {"float", "number", "double", "decimal"}:
            return "number"
    lowered_name = name.lower()
    if lowered_name.endswith("_id") or lowered_name == "id":
        return "string"
    return "string"


def infer_path_params(path: str | None) -> dict[str, ApiSchemaProperty]:
    """Infer path parameter names from common path syntaxes."""
    if not isinstance(path, str) or not path:
        return {}

    params: dict[str, ApiSchemaProperty] = {}
    for pattern in _PATH_PARAM_PATTERNS:
        for match in pattern.finditer(path):
            if "name" in match.groupdict():
                name = match.group("name")
                converter = match.groupdict().get("conv")
            else:
                name = match.group(1)
                converter = None
            if name in params:
                continue
            params[name] = ApiSchemaProperty(
                type=_infer_param_type(name, converter),
                required=True,
                description=f"Path parameter `{name}`.",
            )
    return params


def _default_request_body(method: str | None) -> ApiSchema | None:
    normalized = (method or "").upper()
    if normalized in {"POST", "PUT", "PATCH"}:
        return ApiSchema(
            type="object",
            required=[],
            properties={},
            additional_properties=True,
            description="Unknown request body shape (basic skeleton).",
        )
    return None


def _unknown_response_schema() -> ApiSchema:
    return ApiSchema(
        type="object",
        required=[],
        properties={},
        additional_properties=True,
        description="Unknown response body shape (basic skeleton).",
    )


def _default_statuses_for_method(method: str | None) -> list[str]:
    normalized = (method or "").upper()
    status_map = {
        "GET": ["200"],
        "POST": ["201"],
        "PUT": ["200"],
        "PATCH": ["200"],
        "DELETE": ["204"],
    }
    return status_map.get(normalized, ["200"])


def _default_responses(method: str | None, file: str | None, handler: str | None) -> dict[str, ApiResponseContract]:
    responses: dict[str, ApiResponseContract] = {}
    for status in _default_statuses_for_method(method):
        responses[status] = ApiResponseContract(
            status=status,
            description=f"Default {status} response skeleton.",
            body=_unknown_response_schema(),
            confidence="low",
            evidence=[
                ApiContractEvidence(
                    kind="route_handler_reference",
                    file=file,
                    symbol=handler,
                    source="routes_discovery",
                    confidence="low",
                    notes=["Default response placeholder generated by basic contract builder."],
                )
            ],
        )
    return responses


def _guess_property_type(name: str) -> tuple[str | None, str | None, Any | None]:
    lowered = name.lower()
    if "email" in lowered:
        return "string", "email", "test@example.com"
    if lowered in {"limit", "page", "count", "quantity", "total"}:
        return "integer", None, 1
    if any(token in lowered for token in {"price", "amount", "total", "cost"}):
        return "number", None, 9.99
    if lowered.endswith("_id") or lowered == "id":
        return "string", None, "id_123"
    return "string", None, "example"


def _merge_required_flags(schema: ApiSchema, required_fields: set[str]) -> None:
    if not required_fields:
        return
    current = set(schema.required)
    for field in required_fields:
        current.add(field)
        if field in schema.properties:
            schema.properties[field].required = True
    schema.required = sorted(current)


def _infer_status_from_abort_or_exception(line: str) -> str | None:
    abort_match = re.search(r"abort\(\s*(\d{3})", line)
    if abort_match:
        return abort_match.group(1)
    exception_match = re.search(r"HTTPException\s*\(\s*status_code\s*=\s*(\d{3})", line)
    if exception_match:
        return exception_match.group(1)
    return None


def _infer_schema_from_ast_value(node: ast.AST) -> ApiSchema | None:
    if isinstance(node, ast.Dict):
        properties: dict[str, ApiSchemaProperty] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            key = key_node.value
            properties[key] = _infer_property_from_ast_value(value_node)
        return ApiSchema(type="object", required=[], properties=properties, additional_properties=True)
    return None


def _infer_property_from_ast_value(node: ast.AST) -> ApiSchemaProperty:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return ApiSchemaProperty(type="boolean", example=value)
        if isinstance(value, int):
            return ApiSchemaProperty(type="integer", example=value)
        if isinstance(value, float):
            return ApiSchemaProperty(type="number", example=value)
        if isinstance(value, str):
            return ApiSchemaProperty(type="string", example=value)
        if value is None:
            return ApiSchemaProperty(type="string", nullable=True)
    if isinstance(node, ast.List):
        return ApiSchemaProperty(type="array")
    if isinstance(node, ast.Dict):
        return ApiSchemaProperty(type="object")
    return ApiSchemaProperty(type="string")


def _extract_status_from_return_node(node: ast.Return) -> str | None:
    if node.value is None:
        return None
    if isinstance(node.value, ast.Tuple) and len(node.value.elts) >= 2:
        status_node = node.value.elts[1]
        if isinstance(status_node, ast.Constant) and isinstance(status_node.value, int):
            return str(status_node.value)
    return None


def _extract_response_body_node(return_value: ast.AST | None) -> ast.AST | None:
    if return_value is None:
        return None
    if isinstance(return_value, ast.Tuple) and return_value.elts:
        candidate = return_value.elts[0]
    else:
        candidate = return_value

    if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name) and candidate.func.id == "jsonify":
        if candidate.args:
            return candidate.args[0]
    if isinstance(candidate, ast.Dict):
        return candidate
    return None


def _resolve_source_file(route: EndpointCandidate, repo_roots: dict[str, str] | None) -> Path | None:
    raw_file = route.file
    if not raw_file:
        return None
    file_path = Path(raw_file)
    if file_path.is_absolute() and file_path.exists():
        return file_path
    if repo_roots and route.repo in repo_roots:
        candidate = Path(repo_roots[route.repo]) / raw_file
        if candidate.exists():
            return candidate
    return None


def _select_handler_segment(source: str, handler: str | None) -> tuple[str, int]:
    if not handler:
        return source, 1
    handler_name = handler.split(".")[-1]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 1

    class _Finder(ast.NodeVisitor):
        def __init__(self) -> None:
            self.match: ast.AST | None = None

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            if node.name == handler_name and self.match is None:
                self.match = node
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            if node.name == handler_name and self.match is None:
                self.match = node
            self.generic_visit(node)

    finder = _Finder()
    finder.visit(tree)
    node = finder.match
    if node is None or not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
        return source, 1

    lines = source.splitlines()
    start = max(1, int(getattr(node, "lineno", 1)))
    end = max(start, int(getattr(node, "end_lineno", start)))
    snippet = "\n".join(lines[start - 1 : end])
    return snippet, start


def _extract_request_contract_from_source(source: str, base_line: int, file: str | None, handler: str | None) -> tuple[ApiRequestContract, list[ApiContractEvidence], list[str]]:
    request = ApiRequestContract(path_params={}, query_params={}, headers={}, body=None, examples=[])
    evidence: list[ApiContractEvidence] = []
    notes: list[str] = []

    body_vars = {match.group("name") for match in _JSON_SOURCE_ASSIGN_PATTERN.finditer(source)}
    body_vars.update({"request.json", "request.get_json()"})

    body_fields: dict[str, ApiSchemaProperty] = {}
    required_fields: set[str] = set()

    for direct_pattern in (_DIRECT_JSON_GET_PATTERN, _DIRECT_JSON_INDEX_PATTERN):
        for match in direct_pattern.finditer(source):
            field = match.group("field")
            inferred_type, inferred_format, example = _guess_property_type(field)
            body_fields[field] = ApiSchemaProperty(
                type=inferred_type,
                format=inferred_format,
                required=False,
                example=example,
                description=f"Inferred from request body usage for `{field}`.",
            )
            if direct_pattern is _DIRECT_JSON_INDEX_PATTERN:
                required_fields.add(field)

    for var in body_vars:
        escaped_var = re.escape(var)
        get_pattern = re.compile(_FIELD_GET_PATTERN.format(var=escaped_var))
        index_pattern = re.compile(_FIELD_INDEX_PATTERN.format(var=escaped_var))
        for match in get_pattern.finditer(source):
            field = match.group("field")
            inferred_type, inferred_format, example = _guess_property_type(field)
            body_fields[field] = ApiSchemaProperty(
                type=inferred_type,
                format=inferred_format,
                required=False,
                example=example,
                description=f"Inferred from `{var}.get(...)` usage.",
            )
            if re.search(_REQUIRED_NOT_GET_PATTERN.format(var=escaped_var, field=re.escape(field)), source):
                required_fields.add(field)
            if re.search(_REQUIRED_NOT_IN_PATTERN.format(var=escaped_var, field=re.escape(field)), source):
                required_fields.add(field)
        for match in index_pattern.finditer(source):
            field = match.group("field")
            inferred_type, inferred_format, example = _guess_property_type(field)
            body_fields[field] = ApiSchemaProperty(
                type=inferred_type,
                format=inferred_format,
                required=True,
                example=example,
                description=f"Inferred from `{var}[...]` usage.",
            )
            required_fields.add(field)

    if body_fields:
        request.body = ApiSchema(
            type="object",
            required=[],
            properties=body_fields,
            additional_properties=True,
            description="Inferred request body schema from handler source.",
        )
        _merge_required_flags(request.body, required_fields)
        evidence.append(
            ApiContractEvidence(
                kind="flask_request_body_usage",
                file=file,
                symbol=handler,
                line=base_line,
                source="handler_source",
                confidence="medium",
                notes=["Request body fields inferred from request.get_json()/request.json usage."],
            )
        )
    
    query_fields: dict[str, ApiSchemaProperty] = {}
    query_required: set[str] = set()
    for match in _REQUEST_ARGS_GET_PATTERN.finditer(source):
        name = match.group("field")
        p_type, p_format, example = _guess_property_type(name)
        if name.lower() in {"limit", "page", "count"}:
            p_type = "integer"
        query_fields[name] = ApiSchemaProperty(type=p_type, format=p_format, required=False, example=example)
    for match in _REQUEST_ARGS_INDEX_PATTERN.finditer(source):
        name = match.group("field")
        p_type, p_format, example = _guess_property_type(name)
        if name.lower() in {"limit", "page", "count"}:
            p_type = "integer"
        query_fields[name] = ApiSchemaProperty(type=p_type, format=p_format, required=True, example=example)
        query_required.add(name)

    for name in query_required:
        if name in query_fields:
            query_fields[name].required = True
    if query_fields:
        request.query_params = query_fields
        evidence.append(
            ApiContractEvidence(
                kind="flask_query_param_usage",
                file=file,
                symbol=handler,
                line=base_line,
                source="handler_source",
                confidence="medium",
                notes=["Query params inferred from request.args usage."],
            )
        )

    header_fields: dict[str, ApiSchemaProperty] = {}
    for match in _REQUEST_HEADERS_GET_PATTERN.finditer(source):
        header_name = match.group("field")
        example = "Bearer {{authToken}}" if header_name.lower() == "authorization" else "value"
        header_fields[header_name] = ApiSchemaProperty(type="string", required=False, example=example)
    for match in _REQUEST_HEADERS_INDEX_PATTERN.finditer(source):
        header_name = match.group("field")
        example = "Bearer {{authToken}}" if header_name.lower() == "authorization" else "value"
        header_fields[header_name] = ApiSchemaProperty(type="string", required=True, example=example)

    if "Authorization" not in header_fields and _AUTH_HINT_PATTERN.search(source):
        header_fields["Authorization"] = ApiSchemaProperty(
            type="string",
            required=False,
            example="Bearer {{authToken}}",
            description="Auth header inferred from auth-related handler hints.",
        )
        evidence.append(
            ApiContractEvidence(
                kind="heuristic",
                file=file,
                symbol=handler,
                line=base_line,
                source="handler_source",
                confidence="low",
                notes=["Authorization inferred from auth-like tokens in source."],
            )
        )

    if header_fields:
        if "Authorization" in header_fields:
            auth_direct = re.search(r"request\.headers(?:\.get\(|\[)\s*['\"]Authorization['\"]", source)
            if auth_direct:
                header_fields["Authorization"].required = True
        request.headers = header_fields
        evidence.append(
            ApiContractEvidence(
                kind="flask_header_usage",
                file=file,
                symbol=handler,
                line=base_line,
                source="handler_source",
                confidence="medium",
                notes=["Headers inferred from request.headers usage."],
            )
        )

    if request.body is None and not request.query_params and not request.headers:
        notes.append("No concrete request contract fields inferred from handler source; using scaffold defaults.")

    return request, evidence, notes


def _extract_responses_from_source(source: str, base_line: int, method: str | None, file: str | None, handler: str | None) -> tuple[dict[str, ApiResponseContract], list[ApiContractEvidence], list[str]]:
    responses: dict[str, ApiResponseContract] = {}
    evidence: list[ApiContractEvidence] = []
    notes: list[str] = []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        notes.append("Could not parse handler source for response inference; using scaffold defaults.")
        return responses, evidence, notes

    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            status = _extract_status_from_return_node(node) or _default_statuses_for_method(method)[0]
            body_node = _extract_response_body_node(node.value)
            if body_node is None:
                continue
            schema = _infer_schema_from_ast_value(body_node)
            if schema is None:
                continue
            responses[status] = ApiResponseContract(
                status=status,
                description=f"Inferred {status} response from handler return.",
                body=schema,
                confidence="medium",
                evidence=[
                    ApiContractEvidence(
                        kind="flask_jsonify_return",
                        file=file,
                        symbol=handler,
                        line=base_line + int(getattr(node, "lineno", 1)) - 1,
                        source="handler_source",
                        confidence="medium",
                        notes=["Response schema inferred from jsonify/dict literal return."],
                    )
                ],
            )

    for line_offset, line in enumerate(source.splitlines(), start=0):
        status = _infer_status_from_abort_or_exception(line)
        if not status:
            continue
        if status in responses:
            continue
        responses[status] = ApiResponseContract(
            status=status,
            description=f"Error response inferred from status {status} branch.",
            body=ApiSchema(
                type="object",
                required=[],
                properties={"error": ApiSchemaProperty(type="string", required=False, example="error")},
                additional_properties=True,
            ),
            confidence="low",
            evidence=[
                ApiContractEvidence(
                    kind="heuristic",
                    file=file,
                    symbol=handler,
                    line=base_line + line_offset,
                    source="handler_source",
                    confidence="low",
                    notes=["Error status inferred from abort/HTTPException usage."],
                )
            ],
        )

    if not responses:
        notes.append("No concrete response schema inferred from handler source; using scaffold defaults.")
    else:
        evidence.append(
            ApiContractEvidence(
                kind="flask_jsonify_return",
                file=file,
                symbol=handler,
                line=base_line,
                source="handler_source",
                confidence="medium",
                notes=["One or more response contracts inferred from return literals."],
            )
        )

    return responses, evidence, notes


def _extract_contract_from_source(route: EndpointCandidate, source_path: Path, source_text: str) -> _SourceExtraction:
    handler_snippet, base_line = _select_handler_segment(source_text, route.handler)
    request, request_evidence, request_notes = _extract_request_contract_from_source(
        handler_snippet,
        base_line,
        file=route.file,
        handler=route.handler,
    )
    responses, response_evidence, response_notes = _extract_responses_from_source(
        handler_snippet,
        base_line,
        method=route.method,
        file=route.file,
        handler=route.handler,
    )

    return _SourceExtraction(
        request=request,
        responses=responses,
        evidence=[*request_evidence, *response_evidence],
        notes=[*request_notes, *response_notes],
    )


def build_api_contract_from_routes(
    routes_result: RoutesResult,
    repo_roots: dict[str, str] | None = None,
) -> ApiContractArtifact:
    """Build C2 API contract with scaffold fallback and lightweight source extraction."""
    route_contracts: list[ApiRouteContract] = []
    global_notes: list[str] = []

    for route in routes_result.routes:
        path_params = infer_path_params(route.path)
        request = ApiRequestContract(
            path_params=path_params,
            query_params={},
            headers={},
            body=_default_request_body(route.method),
            examples=[],
        )
        responses = _default_responses(route.method, route.file, route.handler)
        route_notes = ["API contract initialized from route discovery scaffold."]

        route_evidence = [
            ApiContractEvidence(
                kind="route_candidate",
                file=route.file,
                symbol=route.handler,
                source="routes_discovery",
                confidence="medium" if route.confidence is not None else "low",
                notes=["Contract route inferred from discovered endpoint."],
            )
        ]

        source_file = _resolve_source_file(route, repo_roots)
        if source_file is None:
            route_notes.append("Handler source unavailable; using scaffold-only contract fields.")
        else:
            try:
                source_text = source_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                route_notes.append("Handler source unreadable; using scaffold-only contract fields.")
            else:
                extracted = _extract_contract_from_source(route, source_file, source_text)
                if extracted.request.body is not None:
                    request.body = extracted.request.body
                if extracted.request.query_params:
                    request.query_params = extracted.request.query_params
                if extracted.request.headers:
                    request.headers = extracted.request.headers
                if extracted.responses:
                    responses = extracted.responses
                route_evidence.extend(extracted.evidence)
                route_notes.extend(extracted.notes)

        route_contracts.append(
            ApiRouteContract(
                method=route.method,
                path=route.path,
                repo=route.repo,
                service=route.service,
                handler=route.handler,
                file=route.file,
                request=request,
                responses=responses,
                evidence=route_evidence,
                confidence="medium" if route.confidence is not None else "low",
                notes=route_notes,
            )
        )

    global_notes.extend(
        [
            "api_contract.json uses scaffold defaults with lightweight source inference when possible.",
            "C2 extraction currently targets pragmatic Flask-style handler patterns.",
        ]
    )

    return ApiContractArtifact(
        routes=route_contracts,
        notes=global_notes,
        confidence=0.55 if route_contracts else 0.0,
    )


def build_basic_api_contract_from_routes(routes_result: RoutesResult) -> ApiContractArtifact:
    """Build a first-pass API contract skeleton from discovered routes (C1 compatibility)."""
    contract = build_api_contract_from_routes(routes_result, repo_roots=None)
    contract.notes.append("Deep contract extraction (request/response schema inference) is planned for C2.")
    return contract


def render_api_contract_json(contract: ApiContractArtifact) -> str:
    """Serialize API contract artifact as pretty JSON."""
    return contract.model_dump_json(indent=2)
