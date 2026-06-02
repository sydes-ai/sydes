"""LLM-guided API contract refinement from graph-grounded evidence packets."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from pydantic import ValidationError

from sydes.core.models import (
    ApiContractEvidence,
    ApiRouteContract,
    EvidencePacket,
)
from sydes.llm.client import LLMClient, LLMClientError, LLMRequest, create_default_llm_client

MAX_PROMPT_CHARS = 14_000


@dataclass
class ContractRefinementResult:
    """Outcome of an LLM contract refinement attempt."""

    ok: bool
    refined_contract: ApiRouteContract | None = None
    raw_output: str | None = None
    parsed_output: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def refine_api_contract_with_evidence_packet(
    *,
    evidence_packet: EvidencePacket,
    current_contract: ApiRouteContract | None = None,
    llm_client: LLMClient | None = None,
    model_spec: str | None = None,
    timeout_s: int | None = None,
) -> ContractRefinementResult:
    """Refine a selected route contract using bounded evidence packet context."""

    warnings: list[str] = []
    if llm_client is None:
        try:
            llm_client = create_default_llm_client(
                model_spec=model_spec,
                timeout_seconds_override=timeout_s,
            )
        except LLMClientError as exc:
            return ContractRefinementResult(ok=False, warnings=[str(exc)], error=str(exc))

    prompt = build_contract_refinement_prompt(evidence_packet, current_contract)
    try:
        response = llm_client.generate(LLMRequest(prompt=prompt, temperature=0))
    except LLMClientError as exc:
        return ContractRefinementResult(ok=False, warnings=[str(exc)], error=str(exc))

    raw_text = response.text
    parsed = extract_json_object(raw_text)
    if parsed is None:
        return ContractRefinementResult(
            ok=False,
            raw_output=_truncate(raw_text, 4000),
            warnings=["LLM contract refinement output was not valid JSON."],
            error="invalid_json",
        )
    if not isinstance(parsed, dict):
        return ContractRefinementResult(
            ok=False,
            raw_output=_truncate(raw_text, 4000),
            parsed_output={"value": parsed},
            warnings=["LLM contract refinement output was not a JSON object."],
            error="invalid_schema",
        )

    normalized = normalize_refined_contract_payload(parsed, evidence_packet, current_contract)
    if normalized is None:
        return ContractRefinementResult(
            ok=False,
            raw_output=_truncate(raw_text, 4000),
            parsed_output=parsed,
            warnings=["LLM contract refinement did not match selected endpoint."],
            error="endpoint_mismatch",
        )
    warnings.extend(normalized.notes)
    normalized.notes = _dedupe_strings(
        [
            *(current_contract.notes if current_contract is not None else []),
            *normalized.notes,
            "Refined from graph-grounded evidence packet.",
        ]
    )
    normalized.evidence.append(
        ApiContractEvidence(
            kind="llm_graph_contract_refinement",
            file=normalized.file,
            symbol=normalized.handler,
            source="evidence_packet",
            confidence=normalized.confidence or "medium",
            notes=["LLM refinement validated against selected route evidence packet."],
        )
    )

    refined = merge_route_contract(current_contract, normalized)
    return ContractRefinementResult(
        ok=True,
        refined_contract=refined,
        raw_output=_truncate(raw_text, 4000),
        parsed_output=parsed,
        warnings=warnings,
    )


def build_contract_refinement_prompt(
    evidence_packet: EvidencePacket,
    current_contract: ApiRouteContract | None,
) -> str:
    """Build the strict JSON contract refinement prompt."""

    payload = {
        "evidence_packet": evidence_packet.model_dump(mode="json", exclude_none=True),
        "current_contract": current_contract.model_dump(mode="json", exclude_none=True)
        if current_contract is not None
        else evidence_packet.current_contract,
    }
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    prompt = (
        "You refine one selected API route contract from bounded evidence.\n"
        "Use ONLY the provided evidence packet and current_contract.\n"
        "Do not invent fields, statuses, files, handlers, side effects, or schemas.\n"
        "Refine only the selected route's API contract.\n"
        "Infer request body fields, requiredness, response statuses, and response body schemas when evidence supports them.\n"
        "Cite evidence through source snippets, node ids, or source window lines in evidence.notes where possible.\n"
        "Mark uncertain fields low confidence. If evidence is insufficient, preserve unknown or low-confidence fields.\n"
        "Do not include markdown. Do not include commentary. Return only valid JSON.\n"
        "Use snake_case field names matching Sydes contract models.\n"
        "Return JSON object with fields: method, path, request, responses, evidence, confidence, notes.\n"
        "Input:\n"
        f"{payload_json}"
    )
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    trimmed_payload = dict(payload)
    packet = dict(trimmed_payload["evidence_packet"])
    packet["source_windows"] = packet.get("source_windows", [])[:2]
    packet["trace_nodes"] = packet.get("trace_nodes", [])[:30]
    packet["trace_edges"] = packet.get("trace_edges", [])[:40]
    trimmed_payload["evidence_packet"] = packet
    payload_json = json.dumps(trimmed_payload, ensure_ascii=True, separators=(",", ":"))
    prompt = prompt.split("Input:\n", 1)[0] + "Input:\n" + payload_json
    return prompt[:MAX_PROMPT_CHARS]


def extract_json_object(text: str) -> Any | None:
    """Parse raw, fenced, or first balanced object JSON from model output."""

    stripped = _strip_markdown_fences(text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start : index + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def normalize_refined_contract_payload(
    payload: dict[str, Any],
    evidence_packet: EvidencePacket,
    current_contract: ApiRouteContract | None,
) -> ApiRouteContract | None:
    """Validate endpoint match and convert raw LLM payload to ApiRouteContract."""

    method = str(payload.get("method") or evidence_packet.endpoint.method or "").upper()
    path = str(payload.get("path") or evidence_packet.endpoint.path or "")
    if method != str(evidence_packet.endpoint.method or "").upper():
        return None
    if _normalize_path(path) != _normalize_path(evidence_packet.endpoint.path):
        return None

    route_metadata = current_contract or ApiRouteContract(
        method=evidence_packet.endpoint.method,
        path=evidence_packet.endpoint.path,
        repo=evidence_packet.endpoint.repo,
        handler=evidence_packet.endpoint.handler,
        file=evidence_packet.endpoint.file,
    )
    candidate = {
        **payload,
        "method": route_metadata.method or method,
        "path": route_metadata.path or path,
        "repo": route_metadata.repo,
        "service": route_metadata.service,
        "handler": route_metadata.handler,
        "file": route_metadata.file,
        "confidence": payload.get("confidence") or "medium",
    }
    try:
        return ApiRouteContract.model_validate(candidate)
    except ValidationError:
        return None


def merge_route_contract(
    current: ApiRouteContract | None,
    refinement: ApiRouteContract,
) -> ApiRouteContract:
    """Merge LLM refinement into the deterministic route contract baseline."""

    if current is None:
        return refinement
    merged = current.model_copy(deep=True)
    merged.method = current.method or refinement.method
    merged.path = current.path or refinement.path
    merged.repo = current.repo or refinement.repo
    merged.service = current.service or refinement.service
    merged.handler = current.handler or refinement.handler
    merged.file = current.file or refinement.file
    merged.confidence = refinement.confidence or current.confidence

    merged.request.path_params.update(refinement.request.path_params)
    merged.request.query_params.update(refinement.request.query_params)
    merged.request.headers.update(refinement.request.headers)
    if refinement.request.body is not None:
        if merged.request.body is None:
            merged.request.body = refinement.request.body
        else:
            existing_body = merged.request.body
            existing_body.required = _dedupe_strings(
                [*existing_body.required, *refinement.request.body.required]
            )
            existing_body.properties.update(refinement.request.body.properties)
            existing_body.description = refinement.request.body.description or existing_body.description
            existing_body.example = refinement.request.body.example or existing_body.example
            existing_body.additional_properties = (
                refinement.request.body.additional_properties
                if refinement.request.body.additional_properties is not None
                else existing_body.additional_properties
            )
    merged.request.examples.extend(refinement.request.examples)
    merged.responses.update(refinement.responses)
    merged.evidence.extend(refinement.evidence)
    merged.notes = _dedupe_strings([*merged.notes, *refinement.notes])
    return merged


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    if stripped.startswith("```json"):
        stripped = stripped[len("```json") :].strip()
    if stripped.startswith("```"):
        stripped = stripped[len("```") :].strip()
    if stripped.endswith("```"):
        stripped = stripped[: -len("```")].strip()
    return stripped


def _normalize_path(path: str) -> str:
    normalized = path.strip()
    if not normalized:
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/") or "/"


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 3)] + "..."
