"""LLM-guided test matrix generation from bounded evidence packets."""

from __future__ import annotations

__test__ = False

from dataclasses import dataclass, field
import json
from typing import Any

from sydes.core.models import (
    ApiRouteContract,
    EvidencePacket,
    IntegrationTestSuggestion,
    TestMatrix,
    TestMatrixGroup,
)
from sydes.generate.contract_llm_refinement import extract_json_object
from sydes.generate.tests import (
    clean_test_matrix,
    make_test_suggestion,
    normalize_test_matrix,
)
from sydes.llm.client import LLMClient, LLMClientError, LLMRequest, create_default_llm_client

MAX_PROMPT_CHARS = 14_000


@dataclass
class TestMatrixGenerationResult:
    """Outcome of an LLM-assisted test matrix generation attempt."""

    ok: bool
    test_matrix: TestMatrix | None = None
    raw_output: str | None = None
    parsed_output: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def generate_test_matrix_with_evidence_packet(
    *,
    evidence_packet: EvidencePacket,
    api_contract: ApiRouteContract | None,
    current_test_matrix: TestMatrix | None,
    llm_client: LLMClient | None = None,
    model_spec: str | None = None,
    timeout_s: int | None = None,
) -> TestMatrixGenerationResult:
    """Generate and merge LLM-backed test scenarios using bounded route evidence."""

    warnings: list[str] = []
    if llm_client is None:
        try:
            llm_client = create_default_llm_client(
                model_spec=model_spec,
                timeout_seconds_override=timeout_s,
            )
        except LLMClientError as exc:
            return TestMatrixGenerationResult(ok=False, warnings=[str(exc)], error=str(exc))

    prompt = build_test_matrix_generation_prompt(
        evidence_packet=evidence_packet,
        api_contract=api_contract,
        current_test_matrix=current_test_matrix,
    )
    try:
        response = llm_client.generate(LLMRequest(prompt=prompt, temperature=0))
    except LLMClientError as exc:
        return TestMatrixGenerationResult(ok=False, warnings=[str(exc)], error=str(exc))

    raw_text = response.text
    parsed = extract_json_object(raw_text)
    if parsed is None:
        return TestMatrixGenerationResult(
            ok=False,
            raw_output=_truncate(raw_text, 4000),
            warnings=["LLM test generation output was not valid JSON."],
            error="invalid_json",
        )
    if not isinstance(parsed, dict):
        return TestMatrixGenerationResult(
            ok=False,
            raw_output=_truncate(raw_text, 4000),
            parsed_output={"value": parsed},
            warnings=["LLM test generation output was not a JSON object."],
            error="invalid_schema",
        )

    llm_matrix = normalize_llm_test_matrix_payload(
        parsed,
        evidence_packet=evidence_packet,
        api_contract=api_contract,
        warnings=warnings,
    )
    if llm_matrix is None:
        return TestMatrixGenerationResult(
            ok=False,
            raw_output=_truncate(raw_text, 4000),
            parsed_output=parsed,
            warnings=warnings or ["LLM test generation produced no acceptable scenarios."],
            error="invalid_matrix",
        )

    merged = _merge_test_matrices(current_test_matrix, llm_matrix)
    cleaned = clean_test_matrix(
        normalize_test_matrix(merged),
        api_contract=api_contract,
    )
    return TestMatrixGenerationResult(
        ok=True,
        test_matrix=cleaned,
        raw_output=_truncate(raw_text, 4000),
        parsed_output=parsed,
        warnings=warnings,
    )


def build_test_matrix_generation_prompt(
    *,
    evidence_packet: EvidencePacket,
    api_contract: ApiRouteContract | None,
    current_test_matrix: TestMatrix | None,
) -> str:
    """Build the strict JSON-only prompt for LLM test scenario generation."""

    payload = {
        "endpoint": evidence_packet.endpoint.model_dump(mode="json", exclude_none=True),
        "source_windows": [
            {
                "repo": window.repo,
                "file": window.file,
                "symbol": window.symbol,
                "start_line": window.start_line,
                "end_line": window.end_line,
                "code": _truncate(window.code, 1600),
            }
            for window in evidence_packet.source_windows[:3]
        ],
        "trace_nodes": [
            {
                "id": node.id,
                "type": node.type,
                "name": node.name,
                "kind": node.kind,
                "repo": node.repo,
                "file": node.file,
                "symbol": node.symbol,
                "snippet": _truncate(node.snippet, 300),
                "confidence": node.confidence,
            }
            for node in evidence_packet.trace_nodes[:30]
        ],
        "sinks": [
            {
                "name": sink.name,
                "kind": sink.kind,
                "repo": sink.repo,
                "file": sink.file,
                "symbol": sink.symbol,
                "snippet": _truncate(sink.snippet, 300),
                "confidence": sink.confidence,
            }
            for sink in evidence_packet.sinks[:12]
        ],
        "api_contract": api_contract.model_dump(mode="json", exclude_none=True)
        if api_contract is not None
        else evidence_packet.current_contract,
        "current_test_matrix_summary": _summarize_test_matrix(current_test_matrix)
        or evidence_packet.current_test_matrix_summary,
        "limits": _scenario_limits_for_endpoint(evidence_packet, api_contract),
    }
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    prompt = (
        "You generate integration-test scenarios for exactly one selected API route from bounded evidence.\n"
        "Use ONLY the provided endpoint, source_windows, trace_nodes, sinks, api_contract, and current_test_matrix_summary.\n"
        "Do not rewrite the trace. Do not return prose. Return strict JSON only.\n"
        "Do not include markdown.\n"
        "Do not duplicate existing scenarios.\n"
        "Every scenario must be grounded in contract fields, trace nodes, source snippets, or sinks.\n"
        "If evidence is insufficient, skip the scenario instead of inventing it.\n"
        "Prefer specific scenarios over generic ones.\n"
        "Do not invent external systems not present in the evidence.\n"
        "For POST/PUT/PATCH routes with request body and success response, prefer: one positive scenario, "
        "missing required fields, invalid field type/format, malformed JSON, response schema validation, "
        "side-effect scenario if a store/db/event sink exists, and dependency failure only if evidence shows it.\n"
        "For GET routes, prefer: happy path, not found if id/path param exists, invalid path/query param if typed, "
        "and response schema validation.\n"
        "For health/simple routes, keep it to one or two scenarios.\n"
        "Return JSON with fields: groups, notes, coverage, confidence.\n"
        "Each group must contain category and tests.\n"
        "Each test must include: name, route, method, summary, category, priority, purpose, request, expected, "
        "side_effects, related_steps, related_sinks, contract_refs, requires_mocking, notes_text, evidence.\n"
        "expected.status must be a number or null.\n"
        "method and path must match the selected endpoint exactly.\n"
        "Input:\n"
        f"{payload_json}"
    )
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    payload["trace_nodes"] = payload["trace_nodes"][:20]
    payload["source_windows"] = payload["source_windows"][:2]
    payload["sinks"] = payload["sinks"][:8]
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    prompt = prompt.split("Input:\n", 1)[0] + "Input:\n" + payload_json
    return prompt[:MAX_PROMPT_CHARS]


def normalize_llm_test_matrix_payload(
    payload: dict[str, Any],
    *,
    evidence_packet: EvidencePacket,
    api_contract: ApiRouteContract | None,
    warnings: list[str],
) -> TestMatrix | None:
    """Validate raw LLM payload and coerce it into a safe TestMatrix."""

    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list):
        warnings.append("LLM test generation payload missing groups list.")
        return None

    endpoint_method = str(evidence_packet.endpoint.method or "").upper()
    endpoint_path = str(evidence_packet.endpoint.path or "")
    accepted_groups: list[TestMatrixGroup] = []
    accepted_count = 0

    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            warnings.append("Ignored non-object test group from LLM output.")
            continue
        category = str(raw_group.get("category") or "").strip() or "edge_case"
        raw_tests = raw_group.get("tests")
        if not isinstance(raw_tests, list):
            warnings.append(f"Ignored group `{category}` without tests list.")
            continue
        accepted_tests: list[IntegrationTestSuggestion] = []
        for raw_test in raw_tests:
            accepted = _normalize_llm_test_candidate(
                raw_test,
                endpoint_method=endpoint_method,
                endpoint_path=endpoint_path,
                api_contract=api_contract,
                warnings=warnings,
            )
            if accepted is None:
                continue
            accepted_tests.append(accepted)
            accepted_count += 1
        if accepted_tests:
            accepted_groups.append(
                TestMatrixGroup(
                    category=category,
                    title=_optional_str(raw_group.get("title")),
                    tests=accepted_tests,
                    notes=_coerce_str_list(raw_group.get("notes")),
                )
            )

    if not accepted_groups:
        warnings.append("LLM test generation produced no valid scenarios after validation.")
        return None

    matrix = TestMatrix(
        groups=accepted_groups,
        notes=_coerce_str_list(payload.get("notes")),
        coverage=_optional_float(payload.get("coverage")),
        confidence=_optional_float(payload.get("confidence")),
    )
    matrix.notes.append(f"Accepted {accepted_count} LLM-generated test scenarios from evidence packet.")
    return matrix


def _normalize_llm_test_candidate(
    raw_test: Any,
    *,
    endpoint_method: str,
    endpoint_path: str,
    api_contract: ApiRouteContract | None,
    warnings: list[str],
) -> IntegrationTestSuggestion | None:
    if not isinstance(raw_test, dict):
        warnings.append("Ignored non-object LLM scenario.")
        return None
    name = _optional_str(raw_test.get("name"))
    summary = _optional_str(raw_test.get("summary"))
    purpose = _optional_str(raw_test.get("purpose"))
    category = _optional_str(raw_test.get("category"))
    request = raw_test.get("request")
    if not isinstance(request, dict):
        request = {}
    method = str(raw_test.get("method") or request.get("method") or endpoint_method or "").upper()
    path = str(raw_test.get("route") or request.get("path") or endpoint_path or "")
    if not name or not category or not method or not path or not (summary or purpose):
        warnings.append("Ignored LLM scenario missing name/category/method/path/summary.")
        return None
    if method != endpoint_method or _normalize_path(path) != _normalize_path(endpoint_path):
        warnings.append(f"Ignored LLM scenario `{name}` due to endpoint mismatch.")
        return None

    expected = raw_test.get("expected")
    if not isinstance(expected, dict):
        expected = {}
    status = expected.get("status")
    if isinstance(status, str) and status.isdigit():
        expected["status"] = int(status)
    elif status is None or isinstance(status, int):
        pass
    else:
        warnings.append(f"Ignored LLM scenario `{name}` with non-numeric expected.status.")
        return None

    contract_refs = _coerce_str_list(raw_test.get("contract_refs"))
    related_steps = _coerce_str_list(raw_test.get("related_steps"))
    related_sinks = _coerce_str_list(raw_test.get("related_sinks"))
    evidence = _coerce_evidence_list(raw_test.get("evidence"))
    if not (contract_refs or related_steps or related_sinks or evidence):
        if not _allow_ungrounded_positive(category=category, method=method, api_contract=api_contract):
            warnings.append(f"Ignored LLM scenario `{name}` without grounding.")
            return None

    request_payload = dict(request)
    request_payload["method"] = method
    request_payload["path"] = endpoint_path

    return make_test_suggestion(
        name=name,
        route=endpoint_path,
        method=method,
        summary=summary or purpose,
        category=category,
        priority=_optional_str(raw_test.get("priority")) or "medium",
        purpose=purpose or summary,
        request=request_payload,
        expected=expected,
        side_effects=_coerce_str_list(raw_test.get("side_effects")),
        related_steps=related_steps,
        related_sinks=related_sinks,
        contract_refs=contract_refs,
        requires_mocking=_optional_bool(raw_test.get("requires_mocking")),
        notes_text=_optional_str(raw_test.get("notes_text")),
        evidence=evidence,
    )


def _merge_test_matrices(
    current_test_matrix: TestMatrix | None,
    llm_test_matrix: TestMatrix,
) -> TestMatrix:
    if current_test_matrix is None:
        return llm_test_matrix
    groups = [group.model_copy(deep=True) for group in current_test_matrix.groups]
    groups.extend(group.model_copy(deep=True) for group in llm_test_matrix.groups)
    notes = list(current_test_matrix.notes)
    notes.extend(item for item in llm_test_matrix.notes if item not in notes)
    if "Merged deterministic and LLM-generated test scenarios." not in notes:
        notes.append("Merged deterministic and LLM-generated test scenarios.")
    return TestMatrix(
        groups=groups,
        notes=notes,
        coverage=llm_test_matrix.coverage or current_test_matrix.coverage,
        confidence=llm_test_matrix.confidence or current_test_matrix.confidence,
    )


def _summarize_test_matrix(test_matrix: TestMatrix | None) -> dict[str, Any] | None:
    if test_matrix is None:
        return None
    scenarios: list[dict[str, Any]] = []
    for group in test_matrix.groups:
        for test in group.tests:
            scenarios.append(
                {
                    "name": test.name,
                    "category": test.category or group.category,
                    "priority": test.priority,
                    "expected_status": (test.expected or {}).get("status")
                    if isinstance(test.expected, dict)
                    else None,
                    "contract_refs": list(test.contract_refs),
                }
            )
            if len(scenarios) >= 12:
                break
        if len(scenarios) >= 12:
            break
    return {
        "group_count": len(test_matrix.groups),
        "scenario_count": sum(len(group.tests) for group in test_matrix.groups),
        "scenarios": scenarios,
        "coverage": test_matrix.coverage,
        "confidence": test_matrix.confidence,
    }


def _scenario_limits_for_endpoint(
    evidence_packet: EvidencePacket,
    api_contract: ApiRouteContract | None,
) -> dict[str, int]:
    path = (evidence_packet.endpoint.path or "").lower()
    method = str(evidence_packet.endpoint.method or api_contract.method if api_contract else evidence_packet.endpoint.method or "").upper()
    if any(token in path for token in ("/health", "/ready", "/live", "/ping", "/status")):
        return {"max_scenarios": 2}
    if method == "GET":
        return {"max_scenarios": 8}
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return {"max_scenarios": 10}
    return {"max_scenarios": 6}


def _allow_ungrounded_positive(
    *,
    category: str | None,
    method: str,
    api_contract: ApiRouteContract | None,
) -> bool:
    if (category or "").strip().lower() != "positive":
        return False
    if api_contract is None:
        return method == "GET"
    body = api_contract.request.body
    return method == "GET" and (body is None or not body.properties)


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _coerce_evidence_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(item)
    return out


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _normalize_path(path: str) -> str:
    value = path.strip()
    if not value.startswith("/"):
        value = "/" + value
    while "//" in value:
        value = value.replace("//", "/")
    return value.rstrip("/") or "/"


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."
