"""Safe executor for routing-pattern plans using deterministic discovery facts only."""

from __future__ import annotations

import re
from typing import Any

from sydes.core.models import EndpointCandidate, EvidenceRef

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ALL"}
_ALLOWED_DECL_KINDS = {"method_call"}
_ALLOWED_MOUNT_KINDS = {"router_use"}
_ALLOWED_CONTAINER_KINDS = {"express_router_instance", "router_instance"}


def _normalize_path(path: str | None) -> str | None:
    if path is None:
        return None
    p = path.strip()
    if not p:
        return None
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/+", "/", p)
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    p = re.sub(r":([A-Za-z_]\w*)", r"{\1}", p)
    return p


def _split_args(expr: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in expr:
        if quote is not None:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                args.append(part)
            buf = []
            continue
        buf.append(ch)
    part = "".join(buf).strip()
    if part:
        args.append(part)
    return args


def extract_handler_from_call_snippet(snippet: str) -> str | None:
    """Extract handler hint from method-call snippet safely and generically."""
    text = " ".join((snippet or "").strip().split())
    if not text or "(" not in text or ")" not in text:
        return None
    start = text.find("(")
    end = text.rfind(")")
    if end <= start:
        return None
    args = _split_args(text[start + 1 : end])
    if len(args) < 2:
        return None
    candidate = args[-1].strip().rstrip(";")
    if not candidate:
        return None

    def _unwrap(expr: str) -> str | None:
        expr = expr.strip().rstrip(";")
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", expr):
            return expr
        if re.fullmatch(r"[A-Za-z_]\w*", expr):
            return expr
        call_match = re.fullmatch(r"[A-Za-z_]\w*\((.*)\)", expr)
        if call_match:
            inner = call_match.group(1).strip()
            if not inner:
                return None
            inner_args = _split_args(inner)
            if not inner_args:
                return None
            return _unwrap(inner_args[0])
        return None

    return _unwrap(candidate)


def _plan_supported(plan: dict[str, Any]) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    containers = plan.get("route_container_patterns")
    declarations = plan.get("route_declaration_patterns")
    mounts = plan.get("mount_patterns")

    if not isinstance(containers, list) or not isinstance(declarations, list) or not isinstance(mounts, list):
        return False, ["plan_missing_pattern_lists"]

    for item in containers:
        if isinstance(item, dict) and item.get("kind") not in _ALLOWED_CONTAINER_KINDS:
            warnings.append(f"ignored_container_kind:{item.get('kind')}")
    for item in declarations:
        if isinstance(item, dict) and item.get("kind") not in _ALLOWED_DECL_KINDS:
            warnings.append(f"ignored_declaration_kind:{item.get('kind')}")
    for item in mounts:
        if isinstance(item, dict) and item.get("kind") not in _ALLOWED_MOUNT_KINDS:
            warnings.append(f"ignored_mount_kind:{item.get('kind')}")

    has_supported_decl = any(isinstance(item, dict) and item.get("kind") in _ALLOWED_DECL_KINDS for item in declarations)
    has_supported_mount = any(isinstance(item, dict) and item.get("kind") in _ALLOWED_MOUNT_KINDS for item in mounts)
    if not has_supported_decl:
        return False, warnings
    return has_supported_decl or has_supported_mount, warnings


def execute_routing_pattern_plan(
    *,
    repo_name: str,
    plan: dict[str, Any],
    route_graph_repo: dict | None,
) -> dict:
    """Apply a validated plan safely against deterministic graph facts."""
    route_graph_repo = route_graph_repo or {}
    supported, warnings = _plan_supported(plan)
    if not supported:
        return {
            "plan_applied": False,
            "routes": [],
            "routes_added": 0,
            "mount_edges_used": 0,
            "unresolved_mounts": 0,
            "warnings": [*warnings, "no_supported_plan_kinds"],
        }

    composed = route_graph_repo.get("composed_routes", [])
    summary = route_graph_repo.get("summary", {}) if isinstance(route_graph_repo, dict) else {}

    routes: list[EndpointCandidate] = []
    for item in composed:
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        path = _normalize_path(item.get("path") if isinstance(item.get("path"), str) else None)
        file_path = item.get("file") if isinstance(item.get("file"), str) else None
        if not isinstance(method, str) or method.upper() not in _ALLOWED_METHODS:
            continue
        if not path or not file_path:
            continue

        evidence_payload = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        evidence: list[EvidenceRef] = []
        handler_hint = item.get("handler") if isinstance(item.get("handler"), str) else None
        for ev in evidence_payload:
            if not isinstance(ev, dict):
                continue
            snippet = ev.get("snippet") if isinstance(ev.get("snippet"), str) else None
            label = ev.get("label") if isinstance(ev.get("label"), str) else None
            symbol = ev.get("symbol") if isinstance(ev.get("symbol"), str) else None
            file_value = ev.get("file") if isinstance(ev.get("file"), str) else file_path
            if snippet and handler_hint is None and label == "route_declaration":
                parsed = extract_handler_from_call_snippet(snippet)
                if parsed:
                    handler_hint = parsed
                    symbol = symbol or parsed
            evidence.append(
                EvidenceRef(
                    file=file_value,
                    symbol=symbol,
                    label=label,
                    snippet=snippet,
                )
            )

        evidence.append(
            EvidenceRef(
                file=file_path,
                symbol=handler_hint,
                label="routing_pattern_plan",
                snippet=f"derived using routing_pattern_plan: {plan.get('routing_convention')}",
            )
        )

        routes.append(
            EndpointCandidate(
                method=method.upper(),
                path=path,
                handler=handler_hint,
                file=file_path,
                repo=repo_name,
                evidence=evidence,
                confidence=1.0,
                status="deterministic_plan",
            )
        )

    dedup: dict[tuple[str, str, str], EndpointCandidate] = {}
    for route in routes:
        key = (route.repo, route.method or "", route.path or "")
        if key not in dedup or (dedup[key].handler is None and route.handler is not None):
            dedup[key] = route

    return {
        "plan_applied": True,
        "routes": list(dedup.values()),
        "routes_added": len(dedup),
        "mount_edges_used": int(summary.get("mount_edges", 0)) if isinstance(summary, dict) else 0,
        "unresolved_mounts": int(summary.get("unresolved_mounts", 0)) if isinstance(summary, dict) else 0,
        "warnings": warnings,
    }
