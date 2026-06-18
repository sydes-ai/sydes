"""Build a UI-facing merged contract view from contract and trace artifacts."""

from __future__ import annotations

import re
from typing import Any

from sydes.core.models import ApiContractArtifact
from sydes.generate.tests import match_route_contract

_REQ_BODY_FIELD_PATTERN = re.compile(r"\breq\.body\.([A-Za-z_]\w*)")
_REQ_QUERY_FIELD_PATTERN = re.compile(r"\breq\.query\.([A-Za-z_]\w*)")
_REQ_PARAMS_FIELD_PATTERN = re.compile(r"\breq\.params\.([A-Za-z_]\w*)")
_REQ_HEADERS_FIELD_PATTERN = re.compile(r"\breq\.headers(?:\.get\(|\[)\s*[\"']([^\"']+)[\"']")
_REQ_USER_CONTEXT_PATTERN = re.compile(r"\breq\.user(?:\?\.|\.)([A-Za-z_]\w*)")
_RES_STATUS_PATTERN = re.compile(r"\bres\.status\(\s*(\d{3})\s*\)\s*\.(?:send|json)\s*\(")
_SERVER_RESPONSE_PATTERN = re.compile(r"\bnew\s+(ServerResponse)\s*\(")
_DB_WRITE_TABLE_PATTERN = re.compile(r"\b(insert\s+into|update|delete\s+from)\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_DB_READ_TABLE_PATTERN = re.compile(r"\bselect\b.*?\bfrom\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_SQL_RETURNING_PATTERN = re.compile(r"\breturning\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*)", re.IGNORECASE)


def build_contract_view(
    *,
    api_contract: dict | None = None,
    trace_result: dict | None = None,
    layered_trace_contract: dict | None = None,
    layered_trace_expansion: dict | None = None,
    handler_body_slices: dict | None = None,
    flow_expansion: dict | None = None,
    trace_graph: dict | None = None,
    llm_contract_refinement: dict | None = None,
) -> dict:
    """Build merged contract view using deterministic artifacts first."""

    used_artifacts: list[str] = []
    developer: dict[str, Any] = {
        "source_artifacts": [],
        "merge_notes": [],
        "excluded_candidates": [],
        "normalization_notes": [],
    }
    quality = {
        "overall_confidence": "low",
        "grounded_facts": 0,
        "inferred_facts": 0,
        "scaffold_facts": 0,
        "used_artifacts": used_artifacts,
        "llm_used": bool(llm_contract_refinement),
        "llm_accepted": False,
        "llm_rejected": False,
    }

    route = _merge_route(
        api_contract=api_contract,
        trace_result=trace_result,
        layered_trace_contract=layered_trace_contract,
    )
    if route.get("method") or route.get("path"):
        quality["grounded_facts"] += 1

    route_contract = _select_route_contract(api_contract, route["method"], route["path"])
    if route_contract is not None:
        used_artifacts.append("api_contract")
        developer["source_artifacts"].append("api_contract")
    if layered_trace_contract:
        used_artifacts.append("layered_trace_contract")
        developer["source_artifacts"].append("layered_trace_contract")
    if handler_body_slices:
        used_artifacts.append("handler_body_slices")
        developer["source_artifacts"].append("handler_body_slices")
    if trace_result:
        used_artifacts.append("trace_result")
        developer["source_artifacts"].append("trace_result")
    if trace_graph:
        used_artifacts.append("trace_graph")
        developer["source_artifacts"].append("trace_graph")
    if flow_expansion:
        used_artifacts.append("flow_expansion")
        developer["source_artifacts"].append("flow_expansion")
    if layered_trace_expansion:
        used_artifacts.append("layered_trace_expansion")
        developer["source_artifacts"].append("layered_trace_expansion")

    llm_contract = _extract_valid_llm_contract(llm_contract_refinement, route["method"], route["path"])
    if llm_contract_refinement:
        developer["source_artifacts"].append("llm_contract_refinement")
        if llm_contract is not None:
            quality["llm_accepted"] = True
            used_artifacts.append("llm_contract_refinement")
        elif llm_contract_refinement.get("error") == "endpoint_mismatch":
            quality["llm_rejected"] = True
            developer["excluded_candidates"].append(
                {
                    "artifact": "llm_contract_refinement",
                    "reason": "endpoint_mismatch",
                }
            )

    texts, evidence_rollup = _collect_texts_and_evidence(
        route=route,
        layered_trace_contract=layered_trace_contract,
        handler_body_slices=handler_body_slices,
        trace_result=trace_result,
    )

    request = {
        "path_params": [],
        "query_params": [],
        "headers": [],
        "body_fields": [],
        "body_shape": None,
    }
    context: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    side_effects: list[dict[str, Any]] = []
    unknowns: list[str] = []

    request["path_params"] = _collect_param_fields(route_contract, "path_params")
    request["query_params"] = _collect_param_fields(route_contract, "query_params")
    request["headers"] = _collect_param_fields(route_contract, "headers")
    request["body_shape"] = ((route_contract or {}).get("request") or {}).get("body")

    if route_contract:
        request["body_fields"].extend(
            _schema_properties_to_fields(
                (((route_contract.get("request") or {}).get("body") or {}).get("properties") or {}),
                origin="api_contract",
                grounding="inferred",
                description_fallback="Inferred from API contract body schema.",
            )
        )

    request["body_fields"] = _merge_fields(
        request["body_fields"],
        [
            _make_field(
                name=field,
                source_expr=f"req.body.{field}",
                description="Inferred from request body usage.",
                confidence="medium",
                origin="layered_trace_contract",
                grounding="grounded",
            )
            for field in sorted({match.group(1) for text in texts for match in _REQ_BODY_FIELD_PATTERN.finditer(text)})
        ],
        developer=developer,
    )
    request["query_params"] = _merge_fields(
        request["query_params"],
        [
            _make_field(
                name=field,
                source_expr=f"req.query.{field}",
                description="Inferred from query param usage.",
                confidence="medium",
                origin="layered_trace_contract",
                grounding="grounded",
            )
            for field in sorted({match.group(1) for text in texts for match in _REQ_QUERY_FIELD_PATTERN.finditer(text)})
        ],
        developer=developer,
    )
    request["path_params"] = _merge_fields(
        request["path_params"],
        [
            _make_field(
                name=field,
                source_expr=f"req.params.{field}",
                description="Inferred from path param usage.",
                confidence="medium",
                origin="layered_trace_contract",
                grounding="grounded",
            )
            for field in sorted({match.group(1) for text in texts for match in _REQ_PARAMS_FIELD_PATTERN.finditer(text)})
        ],
        developer=developer,
    )
    request["headers"] = _merge_fields(
        request["headers"],
        [
            _make_field(
                name=field,
                source_expr=f"req.headers['{field}']",
                description="Inferred from request header usage.",
                confidence="medium",
                origin="layered_trace_contract",
                grounding="grounded",
            )
            for field in sorted({match.group(1) for text in texts for match in _REQ_HEADERS_FIELD_PATTERN.finditer(text)})
        ],
        developer=developer,
    )

    for field in sorted({match.group(1) for text in texts for match in _REQ_USER_CONTEXT_PATTERN.finditer(text)}):
        context.append(
            {
                "kind": "auth_user",
                "label": f"Authenticated user {field}",
                "source_expr": f"req.user?.{field}",
                "required": "unknown",
                "confidence": "medium",
                "origin": "layered_trace_contract",
                "grounding": "grounded",
                "evidence": [],
            }
        )

    response_map: dict[str, dict[str, Any]] = {}
    for status, response in (((route_contract or {}).get("responses") or {}).items()):
        response_map[str(status)] = {
            "status": str(status),
            "description": response.get("description"),
            "wrapper": _detect_wrapper(_response_texts(texts)),
            "content_type": None,
            "fields": _schema_properties_to_fields(
                ((response.get("body") or {}).get("properties") or {}),
                origin="api_contract",
                grounding="inferred",
                description_fallback="Inferred from response schema.",
            ),
            "body_shape": response.get("body"),
            "confidence": response.get("confidence") or "low",
            "origin": "api_contract",
            "grounding": _grounding_from_response(response),
            "evidence": _response_evidence(response),
            "unknowns": [],
        }

    explicit_statuses = sorted({match.group(1) for text in texts for match in _RES_STATUS_PATTERN.finditer(text)})
    if explicit_statuses:
        developer["merge_notes"].append("Deterministic explicit response status overrides scaffold defaults.")
    for status in explicit_statuses:
        response_item = response_map.get(status)
        if response_item is None:
            response_item = {
                "status": status,
                "description": None,
                "wrapper": None,
                "content_type": None,
                "fields": [],
                "body_shape": None,
                "confidence": "high",
                "origin": "layered_trace_contract",
                "grounding": "grounded",
                "evidence": [],
                "unknowns": [],
            }
        response_item["origin"] = _merge_origin(response_item.get("origin"), "layered_trace_contract")
        response_item["grounding"] = "grounded"
        response_item["confidence"] = "high"
        response_map[status] = response_item

    if explicit_statuses:
        response_map = {status: response_map[status] for status in explicit_statuses if status in response_map}

    wrapper = _detect_wrapper(_response_texts(texts))
    returning_fields = _extract_returning_fields(texts)
    llm_route_responses = ((llm_contract or {}).get("responses") or {}) if llm_contract else {}
    for status, response_item in response_map.items():
        if wrapper and not response_item.get("wrapper"):
            response_item["wrapper"] = wrapper
        if wrapper and not response_item.get("description"):
            response_item["description"] = f"{wrapper} wrapper containing returned handler data."
        if returning_fields:
            response_item["fields"] = _merge_fields(
                response_item["fields"],
                [
                    _make_field(
                        name=field,
                        source_expr="SQL RETURNING clause",
                        description="Inferred from SQL RETURNING clause.",
                        confidence="medium",
                        origin="layered_trace_contract",
                        grounding="inferred",
                    )
                    for field in returning_fields
                ],
                developer=developer,
            )
        llm_response = llm_route_responses.get(status)
        if isinstance(llm_response, dict):
            response_item["fields"] = _merge_fields(
                response_item["fields"],
                _schema_properties_to_fields(
                    (((llm_response.get("body") or {}).get("properties")) or {}),
                    origin="llm_contract_refinement",
                    grounding="inferred",
                    description_fallback="Suggested by validated LLM refinement.",
                ),
                developer=developer,
            )
        if response_item.get("content_type") is None:
            response_item["unknowns"].append("response content type not inferred")
        if response_item.get("wrapper") and not response_item["fields"]:
            response_item["unknowns"].append("response wrapper detected but exact wrapper schema unresolved")

    if llm_contract and not response_map:
        for status, response in (((llm_contract.get("responses") or {})).items()):
            response_map[str(status)] = {
                "status": str(status),
                "description": response.get("description"),
                "wrapper": None,
                "content_type": None,
                "fields": _schema_properties_to_fields(
                    (((response.get("body") or {}).get("properties")) or {}),
                    origin="llm_contract_refinement",
                    grounding="inferred",
                    description_fallback="Suggested by validated LLM refinement.",
                ),
                "body_shape": response.get("body"),
                "confidence": response.get("confidence") or "low",
                "origin": "llm_contract_refinement",
                "grounding": "inferred",
                "evidence": [],
                "unknowns": ["response content type not inferred"],
            }

    responses = [response_map[key] for key in sorted(response_map.keys())]

    side_effects.extend(_collect_side_effects(layered_trace_contract, trace_result))

    if route_contract and not request["body_fields"] and (((route_contract.get("request") or {}).get("body") or {}).get("description") or "").startswith("Unknown request body shape"):
        unknowns.append("request body shape unresolved beyond scaffold defaults")
        quality["scaffold_facts"] += 1
    if any(field.get("required") == "unknown" for field in request["body_fields"]):
        unknowns.append("request requiredness not proven")

    grounded = 0
    inferred = 0
    scaffold = quality["scaffold_facts"]
    for field in [*request["path_params"], *request["query_params"], *request["headers"], *request["body_fields"], *context]:
        if field.get("grounding") == "grounded":
            grounded += 1
        elif field.get("grounding") == "inferred":
            inferred += 1
        else:
            scaffold += 1
    for item in [*responses, *side_effects]:
        if item.get("grounding") == "grounded":
            grounded += 1
        elif item.get("grounding") == "inferred":
            inferred += 1
        else:
            scaffold += 1
    quality["grounded_facts"] = grounded
    quality["inferred_facts"] = inferred
    quality["scaffold_facts"] = scaffold
    quality["overall_confidence"] = _overall_confidence(grounded=grounded, inferred=inferred, scaffold=scaffold)

    evidence_rollup = _dedupe_evidence(evidence_rollup)
    developer["source_artifacts"] = _dedupe_strings(developer["source_artifacts"])
    used_artifacts[:] = _dedupe_strings(used_artifacts)
    developer["merge_notes"] = _dedupe_strings(developer["merge_notes"])
    developer["normalization_notes"] = _dedupe_strings(developer["normalization_notes"])
    unknowns = _dedupe_strings(unknowns)

    return {
        "version": "v1",
        "route": route,
        "request": request,
        "context": context,
        "responses": responses,
        "side_effects": side_effects,
        "evidence": evidence_rollup,
        "unknowns": unknowns,
        "quality": quality,
        "developer": developer,
    }


def _merge_route(*, api_contract: dict | None, trace_result: dict | None, layered_trace_contract: dict | None) -> dict[str, Any]:
    matched = ((trace_result or {}).get("matched_endpoint") or {}) if isinstance(trace_result, dict) else {}
    layered_matched = ((layered_trace_contract or {}).get("matched_endpoint") or {}) if isinstance(layered_trace_contract, dict) else {}
    route_contract = _select_route_contract(
        api_contract,
        ((matched.get("method") or layered_matched.get("method")) or ((trace_result or {}).get("target") or {}).get("method")),
        ((matched.get("path") or layered_matched.get("path")) or ((trace_result or {}).get("target") or {}).get("path")),
    )
    contract_route = route_contract or {}
    summary = None
    if isinstance(layered_trace_contract, dict):
        summary = layered_trace_contract.get("summary")
    if not summary and isinstance(trace_result, dict):
        summary = ((trace_result.get("summary") or {}).get("text"))
    return {
        "method": matched.get("method") or layered_matched.get("method") or contract_route.get("method") or ((trace_result or {}).get("target") or {}).get("method"),
        "path": matched.get("path") or layered_matched.get("path") or contract_route.get("path") or ((trace_result or {}).get("target") or {}).get("path"),
        "repo": matched.get("repo") or layered_matched.get("repo") or contract_route.get("repo"),
        "service": matched.get("service") or layered_matched.get("service") or contract_route.get("service"),
        "handler": matched.get("handler") or layered_matched.get("handler") or contract_route.get("handler"),
        "route_file": matched.get("file") or layered_matched.get("file") or contract_route.get("file"),
        "handler_file": _handler_file_from_trace(trace_result, layered_trace_contract),
        "summary": summary,
    }


def _handler_file_from_trace(trace_result: dict | None, layered_trace_contract: dict | None) -> str | None:
    resolved = ((trace_result or {}).get("resolved_handlers") or []) if isinstance(trace_result, dict) else []
    if resolved and isinstance(resolved[0], dict):
        primary = resolved[0].get("primary_handler")
        if isinstance(primary, dict):
            symbol = primary.get("symbol")
            if isinstance(symbol, dict) and isinstance(symbol.get("file"), str):
                return symbol["file"]
    layers = ((layered_trace_contract or {}).get("layers") or []) if isinstance(layered_trace_contract, dict) else []
    for layer in layers:
        if isinstance(layer, dict) and layer.get("kind") == "handler" and isinstance(layer.get("file"), str):
            return layer["file"]
    return None


def _select_route_contract(api_contract: dict | None, method: str | None, path: str | None) -> dict[str, Any] | None:
    if not isinstance(api_contract, dict):
        return None
    try:
        contract_obj = ApiContractArtifact.model_validate(api_contract)
        route = match_route_contract(contract_obj, method=method, path=path or "")
    except Exception:
        route = None
    if route is not None:
        if hasattr(route, "model_dump"):
            return route.model_dump(mode="json")
        if isinstance(route, dict):
            return route
    routes = api_contract.get("routes")
    if isinstance(routes, list) and len(routes) == 1 and isinstance(routes[0], dict):
        return routes[0]
    return None


def _collect_param_fields(route_contract: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    if not route_contract:
        return []
    request = route_contract.get("request") or {}
    values = request.get(key) or {}
    return _schema_properties_to_fields(
        values,
        origin="api_contract",
        grounding="inferred",
        description_fallback=f"Inferred from request {key.replace('_', ' ')}.",
    )


def _schema_properties_to_fields(
    properties: dict[str, Any],
    *,
    origin: str,
    grounding: str,
    description_fallback: str,
) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for name, prop in sorted(properties.items()):
        if not isinstance(prop, dict):
            continue
        fields.append(
            {
                "name": name,
                "type": prop.get("type") or "unknown",
                "required": _normalize_required(prop.get("required")),
                "source_expr": None,
                "description": prop.get("description") or description_fallback,
                "confidence": "medium" if origin == "api_contract" else "low",
                "origin": origin,
                "grounding": grounding,
                "evidence": [],
            }
        )
    return fields


def _make_field(
    *,
    name: str,
    source_expr: str,
    description: str,
    confidence: str,
    origin: str,
    grounding: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": _guess_field_type(name),
        "required": "unknown",
        "source_expr": source_expr,
        "description": description,
        "confidence": confidence,
        "origin": origin,
        "grounding": grounding,
        "evidence": [],
    }


def _merge_fields(
    base: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    *,
    developer: dict[str, Any],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {str(item.get("name")): dict(item) for item in base if isinstance(item, dict) and item.get("name")}
    for item in extra:
        name = str(item.get("name") or "")
        if not name:
            continue
        current = merged.get(name)
        if current is None:
            merged[name] = dict(item)
            continue
        if current.get("origin") != item.get("origin"):
            developer["merge_notes"].append(f"Merged duplicate field `{name}` across artifacts.")
        if current.get("grounding") != "grounded" and item.get("grounding") == "grounded":
            merged[name] = dict(item)
            continue
        if (current.get("type") in {None, "unknown"}) and item.get("type") not in {None, "unknown"}:
            current["type"] = item.get("type")
        if current.get("source_expr") is None and item.get("source_expr") is not None:
            current["source_expr"] = item.get("source_expr")
        if current.get("description") is None and item.get("description") is not None:
            current["description"] = item.get("description")
        if current.get("origin") != item.get("origin"):
            current["origin"] = _merge_origin(current.get("origin"), item.get("origin"))
    return [merged[name] for name in sorted(merged)]


def _extract_valid_llm_contract(llm_contract_refinement: dict | None, method: str | None, path: str | None) -> dict[str, Any] | None:
    if not isinstance(llm_contract_refinement, dict):
        return None
    if not llm_contract_refinement.get("ok"):
        return None
    parsed = llm_contract_refinement.get("parsed_output")
    if not isinstance(parsed, dict):
        return None
    if ((parsed.get("method") or "").upper() != (method or "").upper()) or (parsed.get("path") != path):
        return None
    return parsed


def _collect_texts_and_evidence(
    *,
    route: dict[str, Any],
    layered_trace_contract: dict | None,
    handler_body_slices: dict | None,
    trace_result: dict | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    texts: list[str] = []
    evidence: list[dict[str, Any]] = []
    route_file = route.get("route_file")
    handler_file = route.get("handler_file")
    handler = route.get("handler")
    if isinstance(handler_body_slices, dict):
        for slice_item in handler_body_slices.get("slices", []):
            if not isinstance(slice_item, dict):
                continue
            file = slice_item.get("file") or handler_file
            for stmt in slice_item.get("statements", []):
                if not isinstance(stmt, dict):
                    continue
                text = stmt.get("text")
                if isinstance(text, str):
                    texts.append(text)
                    evidence.append(
                        {
                            "kind": stmt.get("kind_hint") or "handler_statement",
                            "label": "Handler body statement",
                            "file": file,
                            "line": stmt.get("line_start"),
                            "symbol": handler,
                            "snippet": text,
                            "source_artifact": "handler_body_slices",
                            "confidence": "high" if stmt.get("signals") else "medium",
                        }
                    )
    if isinstance(layered_trace_contract, dict):
        flow = layered_trace_contract.get("flow")
        if isinstance(flow, dict):
            for step in flow.get("steps", []):
                if not isinstance(step, dict):
                    continue
                detail = step.get("detail")
                if isinstance(detail, str):
                    texts.append(detail)
                for item in step.get("evidence", []):
                    if isinstance(item, dict) and isinstance(item.get("snippet"), str):
                        texts.append(item["snippet"])
                        evidence.append(
                            {
                                "kind": step.get("kind") or item.get("label") or "step",
                                "label": _evidence_label_from_kind(step.get("kind")),
                                "file": item.get("file") or handler_file or route_file,
                                "line": step.get("line_start"),
                                "symbol": item.get("symbol") or handler,
                                "snippet": item.get("snippet"),
                                "source_artifact": "layered_trace_contract",
                                "confidence": "high" if step.get("kind") in {"request_input", "database_write", "response"} else "medium",
                            }
                        )
                if isinstance(detail, str):
                    evidence.append(
                        {
                            "kind": step.get("kind") or "step",
                            "label": _evidence_label_from_kind(step.get("kind")),
                            "file": step.get("file") or handler_file or route_file,
                            "line": step.get("line_start"),
                            "symbol": step.get("symbol") or handler,
                            "snippet": detail,
                            "source_artifact": "layered_trace_contract",
                            "confidence": "high" if step.get("kind") in {"request_input", "database_write", "response"} else "medium",
                        }
                    )
        for sink in layered_trace_contract.get("sinks", []):
            if not isinstance(sink, dict):
                continue
            if isinstance(sink.get("name"), str):
                texts.append(sink["name"])
            for item in sink.get("evidence", []):
                if isinstance(item, dict) and isinstance(item.get("snippet"), str):
                    texts.append(item["snippet"])
                    evidence.append(
                        {
                            "kind": sink.get("kind") or "sink",
                            "label": "Side effect evidence",
                            "file": item.get("file") or handler_file or route_file,
                            "line": None,
                            "symbol": item.get("symbol") or handler,
                            "snippet": item.get("snippet"),
                            "source_artifact": "layered_trace_contract",
                            "confidence": "high",
                        }
                    )
    if isinstance(trace_result, dict):
        for sink in trace_result.get("sinks", []):
            if not isinstance(sink, dict):
                continue
            if isinstance(sink.get("name"), str):
                texts.append(sink["name"])
    return texts, evidence


def _response_texts(texts: list[str]) -> list[str]:
    return [text for text in texts if "res.status" in text or "res.send" in text or "res.json" in text or "ServerResponse" in text]


def _detect_wrapper(texts: list[str]) -> str | None:
    for text in texts:
        match = _SERVER_RESPONSE_PATTERN.search(text)
        if match:
            return match.group(1)
    return None


def _extract_returning_fields(texts: list[str]) -> list[str]:
    fields: list[str] = []
    for text in texts:
        for match in _SQL_RETURNING_PATTERN.finditer(text):
            for raw in match.group(1).split(","):
                name = raw.strip().strip("`\"'")
                if name and re.fullmatch(r"[A-Za-z_]\w*", name):
                    fields.append(name)
    return sorted(set(fields))


def _collect_side_effects(layered_trace_contract: dict | None, trace_result: dict | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    sinks: list[dict[str, Any]] = []
    if isinstance(layered_trace_contract, dict):
        sinks.extend(item for item in layered_trace_contract.get("sinks", []) if isinstance(item, dict))
    if isinstance(trace_result, dict):
        sinks.extend(item for item in trace_result.get("sinks", []) if isinstance(item, dict))
    for sink in sinks:
        text = " ".join(
            str(value)
            for value in [sink.get("kind"), sink.get("operation"), sink.get("name")]
            if isinstance(value, str)
        )
        op, target, kind = _classify_side_effect(text)
        label = f"{kind.replace('_', ' ')} {target}".strip() if target else (sink.get("name") or "Side effect")
        key = (kind, op or "", target or "")
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "kind": kind,
                "target": target,
                "operation": op,
                "label": label[0].upper() + label[1:] if label else "Side effect",
                "phase": "handler",
                "confidence": "high" if kind in {"database_write", "database_read"} else "medium",
                "origin": "layered_trace_contract" if isinstance(layered_trace_contract, dict) else "trace_result",
                "grounding": "grounded",
                "evidence": sink.get("evidence", []),
            }
        )
    return items


def _classify_side_effect(text: str) -> tuple[str | None, str | None, str]:
    match = _DB_WRITE_TABLE_PATTERN.search(text)
    if match:
        operation = match.group(1).split()[0].upper()
        return operation, match.group(2), "database_write"
    match = _DB_READ_TABLE_PATTERN.search(text)
    if match:
        return "SELECT", match.group(1), "database_read"
    if "database" in text.lower():
        return None, None, "database_write"
    return None, None, "side_effect"


def _response_evidence(response: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in response.get("evidence", []):
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "kind": item.get("kind") or "response",
                "label": "Response evidence",
                "file": item.get("file"),
                "line": item.get("line"),
                "symbol": item.get("symbol"),
                "snippet": "; ".join(item.get("notes", [])) if isinstance(item.get("notes"), list) else None,
                "source_artifact": item.get("source") or "api_contract",
                "confidence": item.get("confidence") or "medium",
            }
        )
    return out


def _grounding_from_response(response: dict[str, Any]) -> str:
    description = str(response.get("description") or "")
    if description.startswith("Default "):
        return "scaffold"
    return "inferred"


def _guess_field_type(name: str) -> str:
    lowered = name.lower()
    if "email" in lowered:
        return "string"
    if lowered in {"limit", "page", "count", "quantity", "total"}:
        return "integer"
    if any(token in lowered for token in {"price", "amount", "cost"}):
        return "number"
    return "unknown"


def _normalize_required(value: Any) -> bool | str:
    if value is True:
        return True
    if value is False:
        return False
    return "unknown"


def _overall_confidence(*, grounded: int, inferred: int, scaffold: int) -> str:
    if grounded >= max(2, inferred + scaffold):
        return "high"
    if grounded or inferred:
        return "medium"
    return "low"


def _evidence_label_from_kind(kind: Any) -> str:
    mapping = {
        "request_input": "Reads request body fields",
        "database_write": "Database write",
        "database_read": "Database read",
        "response": "Returns response",
        "response_branch": "Response branch",
    }
    if isinstance(kind, str) and kind in mapping:
        return mapping[kind]
    return "Trace evidence"


def _dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (
            item.get("kind"),
            item.get("file"),
            item.get("line"),
            item.get("symbol"),
            item.get("snippet"),
            item.get("source_artifact"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _merge_origin(left: Any, right: Any) -> str:
    parts = [str(item) for item in (left, right) if isinstance(item, str) and item]
    return "+".join(_dedupe_strings(parts))
