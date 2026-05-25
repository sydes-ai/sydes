"""Build a stable layered trace contract while preserving legacy graph fields."""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any


CANONICAL_STEP_KINDS = {
    "endpoint",
    "middleware",
    "prehandler",
    "handler",
    "request_input",
    "request_params",
    "request_query",
    "validation_branch",
    "database_read",
    "database_write",
    "database_query",
    "storage_call",
    "external_call",
    "service_call",
    "response_branch",
    "response",
    "response_transform",
    "transform",
    "unknown_important",
}


def canonical_step_kind(raw_kind: str, signals: Iterable[str] | None = None, text: str | None = None) -> str:
    """Map legacy/heuristic step labels to canonical UI-friendly kinds."""
    kind = (raw_kind or "").strip().lower()
    signal_set = {s.lower() for s in (signals or [])}
    line = (text or "").lower()

    aliases = {
        "db_write": "database_write",
        "db_read": "database_read",
        "external_api": "external_call",
        "external_api_call": "external_call",
        "request_body_read": "request_input",
        "request_params_read": "request_params",
        "request_query_read": "request_query",
        "branch": "validation_branch",
        "return": "response",
    }
    if kind in aliases:
        kind = aliases[kind]

    if kind in CANONICAL_STEP_KINDS:
        return kind
    if "request_body_read" in signal_set:
        return "request_input"
    if "request_params_read" in signal_set:
        return "request_params"
    if "request_query_read" in signal_set:
        return "request_query"
    if "response_transform" in signal_set:
        return "response_transform"
    if "response_return" in signal_set and "branch" in signal_set:
        return "response_branch"
    if "response_return" in signal_set:
        return "response"
    if "sql_literal" in signal_set and "insert into" in line:
        return "database_write"
    if "sql_literal" in signal_set and "select " in line:
        return "database_read"
    if "possible_db_call" in signal_set:
        if re.search(r"\b(insert|update|delete|create|save|commit|add)\b", line):
            return "database_write"
        if re.search(r"\b(select|find|get|query)\b", line):
            return "database_query"
        return "database_query"
    if "possible_external_call" in signal_set:
        if any(token in line for token in ("upload", "s3", "storage")):
            return "storage_call"
        return "external_call"
    if "await_call" in signal_set:
        return "service_call"
    if "branch" in signal_set:
        return "validation_branch"
    if kind in {"assignment", "statement", "await_call"}:
        return "transform"
    return "unknown_important"


def _step_name(kind: str, detail: str | None) -> str:
    if kind == "request_input":
        return "request body input"
    if kind == "database_write":
        return "database write"
    if kind == "database_read":
        return "database read"
    if kind == "database_query":
        return "database query"
    if kind == "storage_call":
        return "storage call"
    if kind == "response":
        return "response"
    if kind == "response_branch":
        return "response branch"
    if kind == "service_call":
        return "service call"
    if kind == "handler":
        return "handler"
    if kind == "middleware":
        return "middleware"
    if kind == "prehandler":
        return "prehandler"
    if kind == "endpoint":
        return "endpoint"
    if kind == "request_params":
        return "request params"
    if kind == "request_query":
        return "request query"
    if kind == "response_transform":
        return "response transform"
    if kind == "validation_branch":
        return "validation branch"
    if kind == "transform":
        return "transform"
    return detail or "important step"


def _truncate(text: str | None, max_chars: int = 200) -> str | None:
    if not isinstance(text, str):
        return None
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _mk_step(
    *,
    step_id: str,
    kind: str,
    detail: str,
    repo: str | None,
    file: str | None,
    symbol: str | None,
    depth: int,
    layer: str,
    line_start: int | None = None,
    line_end: int | None = None,
    confidence: float | None = None,
    status: str = "grounded",
    evidence_label: str | None = None,
) -> dict[str, Any]:
    evidence = []
    if file:
        evidence.append(
            {
                "file": file,
                "symbol": symbol,
                "label": evidence_label or kind,
                "snippet": _truncate(detail),
            }
        )
    return {
        "id": step_id,
        "kind": kind,
        "name": _step_name(kind, detail),
        "detail": _truncate(detail),
        "repo": repo,
        "file": file,
        "symbol": symbol,
        "line_start": line_start,
        "line_end": line_end,
        "depth": depth,
        "layer": layer,
        "evidence": evidence,
        "confidence": confidence if confidence is not None else 0.85,
        "status": status,
        "metadata": {"source": "layered_trace_contract"},
    }


def build_layered_trace_contract(
    *,
    matched_endpoint: dict | None,
    primary_slice: dict | None,
    resolved_handlers: dict | None,
    layered_trace_expansion: dict | None,
    llm_summary: dict | None,
    budgets: dict | None,
    artifact_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Produce UI-ready layered trace contract while keeping deterministic grounding."""
    route = matched_endpoint or {}
    repo = route.get("repo")
    handler_symbol = None
    primary_handler_name = None
    prehandlers: list[str] = []
    if isinstance(resolved_handlers, dict):
        resolution = resolved_handlers.get("resolution", {})
        if isinstance(resolution, dict):
            primary = resolution.get("primary_handler")
            if isinstance(primary, dict):
                primary_handler_name = primary.get("normalized_handler") or primary.get("handler_hint")
                symbol = primary.get("symbol")
                if isinstance(symbol, dict):
                    handler_symbol = symbol
            for item in resolution.get("prehandlers", []):
                if isinstance(item, dict):
                    name = item.get("normalized_handler") or item.get("handler_hint")
                    if isinstance(name, str) and name.strip():
                        prehandlers.append(name.strip())

    steps: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    diagnostics: list[str] = []

    endpoint_step = _mk_step(
        step_id="step:endpoint:1",
        kind="endpoint",
        detail=f"{route.get('method') or 'ANY'} {route.get('path') or '?'}",
        repo=repo,
        file=route.get("file"),
        symbol=route.get("handler"),
        depth=0,
        layer="endpoint",
        confidence=route.get("confidence") if isinstance(route.get("confidence"), (int, float)) else 1.0,
    )
    steps.append(endpoint_step)
    layer0 = {"depth": 0, "kind": "endpoint", "name": endpoint_step["detail"], "file": route.get("file"), "steps": [endpoint_step]}
    layers.append(layer0)

    layer1_steps: list[dict[str, Any]] = []
    for idx, name in enumerate(prehandlers, start=1):
        kind = "middleware" if "validator" in name.lower() else "prehandler"
        layer1_steps.append(
            _mk_step(
                step_id=f"step:prehandler:{idx}",
                kind=kind,
                detail=name,
                repo=repo,
                file=route.get("file"),
                symbol=name,
                depth=1,
                layer="handler",
                confidence=0.9,
            )
        )
    if primary_handler_name:
        layer1_steps.append(
            _mk_step(
                step_id="step:handler:1",
                kind="handler",
                detail=primary_handler_name,
                repo=repo,
                file=handler_symbol.get("file") if isinstance(handler_symbol, dict) else route.get("file"),
                symbol=primary_handler_name,
                depth=1,
                layer="handler",
                line_start=handler_symbol.get("line") if isinstance(handler_symbol, dict) else None,
                confidence=0.95,
            )
        )
    if isinstance(primary_slice, dict):
        for stmt in primary_slice.get("statements", [])[:40]:
            if not isinstance(stmt, dict):
                continue
            kind = canonical_step_kind(
                str(stmt.get("kind_hint") or ""),
                stmt.get("signals", []),
                str(stmt.get("text") or ""),
            )
            step = _mk_step(
                step_id=f"step:handler:{stmt.get('index')}",
                kind=kind,
                detail=str(stmt.get("text") or ""),
                repo=repo,
                file=primary_slice.get("file"),
                symbol=primary_handler_name or route.get("handler"),
                depth=1,
                layer="handler",
                line_start=stmt.get("line_start"),
                line_end=stmt.get("line_end"),
                confidence=stmt.get("confidence") if isinstance(stmt.get("confidence"), (int, float)) else 0.85,
                evidence_label=str(stmt.get("kind_hint") or kind),
            )
            layer1_steps.append(step)
    if layer1_steps:
        layers.append(
            {
                "depth": 1,
                "kind": "handler",
                "name": primary_handler_name or "handler",
                "file": (handler_symbol.get("file") if isinstance(handler_symbol, dict) else primary_slice.get("file") if isinstance(primary_slice, dict) else route.get("file")),
                "steps": layer1_steps,
            }
        )
        steps.extend(layer1_steps)

    if isinstance(layered_trace_expansion, dict):
        for idx, layer in enumerate(layered_trace_expansion.get("layers", [])[1:], start=1):
            if not isinstance(layer, dict):
                continue
            handler = str(layer.get("handler") or f"followed_call_{idx}")
            file = layer.get("file")
            follow_steps: list[dict[str, Any]] = [
                _mk_step(
                    step_id=f"step:follow:{idx}:0",
                    kind="service_call",
                    detail=handler,
                    repo=repo,
                    file=file,
                    symbol=handler,
                    depth=2,
                    layer="followed_call",
                    confidence=0.85,
                )
            ]
            for stmt in layer.get("steps", [])[:6]:
                if not isinstance(stmt, dict):
                    continue
                kind = canonical_step_kind(
                    str(stmt.get("kind_hint") or ""),
                    stmt.get("signals", []),
                    str(stmt.get("text") or ""),
                )
                follow_steps.append(
                    _mk_step(
                        step_id=f"step:follow:{idx}:{stmt.get('index')}",
                        kind=kind,
                        detail=str(stmt.get("text") or ""),
                        repo=repo,
                        file=file,
                        symbol=handler,
                        depth=2,
                        layer="followed_call",
                        line_start=stmt.get("line_start"),
                        line_end=stmt.get("line_end"),
                        confidence=stmt.get("confidence") if isinstance(stmt.get("confidence"), (int, float)) else 0.8,
                        evidence_label=str(stmt.get("kind_hint") or kind),
                    )
                )
            layers.append(
                {
                    "depth": 2,
                    "kind": "followed_call",
                    "name": handler,
                    "file": file,
                    "called_from": layer.get("called_from"),
                    "steps": follow_steps,
                }
            )
            steps.extend(follow_steps)

        for item in layered_trace_expansion.get("skipped_calls", []):
            if isinstance(item, dict) and item.get("reason") in {"max_steps", "max_functions", "max_files"}:
                diagnostics.append(f"trace_budget_limit:{item.get('reason')}")

    sinks: list[dict[str, Any]] = []
    sink_keys: set[tuple[str, str, str]] = set()
    for step in steps:
        sk = step["kind"]
        name = step.get("detail") or step.get("name") or ""
        sink_kind = None
        operation = None
        if sk in {"database_write", "database_read", "database_query"}:
            sink_kind = "database"
            operation = "write" if sk == "database_write" else ("read" if sk == "database_read" else "query")
        elif sk == "storage_call":
            sink_kind = "storage"
            operation = "write"
        elif sk == "external_call":
            sink_kind = "external_api"
            operation = "call"
        if sink_kind is None:
            continue
        key = (sink_kind, operation or "", step.get("file") or "")
        if key in sink_keys:
            continue
        sink_keys.add(key)
        sinks.append(
            {
                "kind": sink_kind,
                "operation": operation,
                "name": _truncate(name, 120) or sink_kind,
                "repo": step.get("repo"),
                "file": step.get("file"),
                "symbol": step.get("symbol"),
                "evidence": step.get("evidence", []),
                "confidence": step.get("confidence", 0.8),
                "status": step.get("status", "grounded"),
            }
        )

    if isinstance(layered_trace_expansion, dict):
        summary = layered_trace_expansion.get("summary", {})
        if isinstance(summary, dict):
            if summary.get("steps_added", 0) >= (budgets or {}).get("max_steps", 10**9):
                diagnostics.append("trace_budget_max_steps_hit=true")
                diagnostics.append("trace_truncated=true")

    summary_text = None
    if isinstance(llm_summary, dict):
        result = llm_summary.get("result")
        if isinstance(result, dict):
            text = result.get("summary")
            if isinstance(text, str) and text.strip():
                summary_text = text.strip()
    if not summary_text:
        kinds = [step["kind"] for step in steps]
        parts = []
        if any(k in kinds for k in ("request_input", "request_params", "request_query")):
            parts.append("handles request input")
        if any(k in kinds for k in ("database_write", "database_read", "database_query")):
            parts.append("performs database operations")
        if any(k in kinds for k in ("storage_call", "external_call")):
            parts.append("performs external/storage calls")
        if any(k in kinds for k in ("response", "response_branch")):
            parts.append("returns response")
        summary_text = ", ".join(parts) if parts else "Builds a deterministic inferred flow from source evidence."

    return {
        "target": {"method": route.get("method"), "path": route.get("path")},
        "matched_endpoint": route,
        "summary": summary_text,
        "flow": {"steps": steps},
        "layers": layers,
        "sinks": sinks,
        "resolved_handlers": [resolved_handlers.get("resolution")] if isinstance(resolved_handlers, dict) and isinstance(resolved_handlers.get("resolution"), dict) else [],
        "budgets": budgets or {},
        "diagnostics": diagnostics,
        "artifacts": artifact_paths or {},
    }

