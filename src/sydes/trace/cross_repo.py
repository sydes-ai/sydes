"""Cross-repo endpoint indexing and lookup helpers for inferred API linking.

This phase focuses on lightweight linking across repositories through inferred API
calls and discovered HTTP endpoints. It is not full distributed tracing, and does
not attempt queue/event linking unless evidence is explicitly available elsewhere.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TypeAlias

from sydes.core.models import CrossRepoCallCandidate, EndpointCandidate

MethodPathKey: TypeAlias = tuple[str, str]
ServicePathKey: TypeAlias = tuple[str, str]
ServiceMethodPathKey: TypeAlias = tuple[str, str, str]


def _normalize_method(value: str | None) -> str | None:
    """Normalize HTTP method to uppercase when present."""
    if value is None:
        return None
    method = value.strip().upper()
    return method or None


def _normalize_path(value: str | None) -> str | None:
    """Normalize route path into a slash-prefixed, compact form."""
    if value is None:
        return None
    path = value.strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    path = "/" + "/".join(part for part in path.split("/") if part)
    return path or "/"


def _normalize_service(value: str | None) -> str | None:
    """Normalize service hint values for stable lookup keys."""
    if value is None:
        return None
    service = value.strip().lower()
    return service or None


def _endpoint_lookup_id(endpoint: EndpointCandidate) -> str:
    """Build a deterministic endpoint identifier for cross-repo link bookkeeping."""
    method = _normalize_method(endpoint.method) or "ANY"
    path = _normalize_path(endpoint.path) or "?"
    return f"{endpoint.repo}:{endpoint.file}:{method}:{path}"


def index_discovered_endpoints(
    endpoints: list[EndpointCandidate],
) -> dict[str, dict[object, list[EndpointCandidate]]]:
    """Index discovered endpoints for deterministic cross-repo path/service lookups."""
    by_path: defaultdict[str, list[EndpointCandidate]] = defaultdict(list)
    by_method_path: defaultdict[MethodPathKey, list[EndpointCandidate]] = defaultdict(list)
    by_service_path: defaultdict[ServicePathKey, list[EndpointCandidate]] = defaultdict(list)
    by_service_method_path: defaultdict[ServiceMethodPathKey, list[EndpointCandidate]] = defaultdict(list)
    by_endpoint_id: dict[str, list[EndpointCandidate]] = {}

    for endpoint in endpoints:
        normalized_path = _normalize_path(endpoint.path)
        if normalized_path is None:
            continue
        normalized_method = _normalize_method(endpoint.method)
        normalized_service = _normalize_service(endpoint.service)

        by_path[normalized_path].append(endpoint)
        if normalized_method is not None:
            by_method_path[(normalized_method, normalized_path)].append(endpoint)
        if normalized_service is not None:
            by_service_path[(normalized_service, normalized_path)].append(endpoint)
            if normalized_method is not None:
                by_service_method_path[(normalized_service, normalized_method, normalized_path)].append(endpoint)
        by_endpoint_id[_endpoint_lookup_id(endpoint)] = [endpoint]

    return {
        "by_path": dict(by_path),
        "by_method_path": dict(by_method_path),
        "by_service_path": dict(by_service_path),
        "by_service_method_path": dict(by_service_method_path),
        "by_endpoint_id": by_endpoint_id,
    }


def lookup_candidate_endpoints_by_path(
    index: dict[str, dict[object, list[EndpointCandidate]]],
    *,
    path: str | None,
    method: str | None = None,
) -> list[EndpointCandidate]:
    """Look up endpoints by path, optionally preferring method+path exact matches."""
    normalized_path = _normalize_path(path)
    if normalized_path is None:
        return []
    normalized_method = _normalize_method(method)

    by_method_path = index.get("by_method_path", {})
    by_path = index.get("by_path", {})

    if normalized_method is not None:
        method_hits = by_method_path.get((normalized_method, normalized_path))
        if method_hits:
            return list(method_hits)
    return list(by_path.get(normalized_path, []))


def lookup_candidate_endpoints_by_service_path(
    index: dict[str, dict[object, list[EndpointCandidate]]],
    *,
    service_hint: str | None,
    path: str | None,
    method: str | None = None,
) -> list[EndpointCandidate]:
    """Look up endpoints using service/path hints with method-aware preference."""
    normalized_service = _normalize_service(service_hint)
    normalized_path = _normalize_path(path)
    if normalized_service is None or normalized_path is None:
        return []
    normalized_method = _normalize_method(method)

    by_service_method_path = index.get("by_service_method_path", {})
    by_service_path = index.get("by_service_path", {})

    if normalized_method is not None:
        service_method_hits = by_service_method_path.get((normalized_service, normalized_method, normalized_path))
        if service_method_hits:
            return list(service_method_hits)
    return list(by_service_path.get((normalized_service, normalized_path), []))


def resolve_cross_repo_call_targets(
    call: CrossRepoCallCandidate,
    index: dict[str, dict[object, list[EndpointCandidate]]],
) -> tuple[list[EndpointCandidate], list[str]]:
    """Resolve a cross-repo call candidate to endpoint candidates using soft lookup hints."""
    notes: list[str] = []
    matches: list[EndpointCandidate] = []

    if call.target_path is None:
        notes.append("Call candidate has no target_path; cannot perform endpoint lookup.")
        return [], notes

    if call.target_service_hint:
        matches = lookup_candidate_endpoints_by_service_path(
            index,
            service_hint=call.target_service_hint,
            path=call.target_path,
            method=call.target_method,
        )
        if matches:
            notes.append("Resolved call using service/path endpoint lookup.")
            return matches, notes
        notes.append("No service/path match; falling back to path lookup.")

    matches = lookup_candidate_endpoints_by_path(
        index,
        path=call.target_path,
        method=call.target_method,
    )
    if matches:
        notes.append("Resolved call using path-based endpoint lookup.")
    else:
        notes.append("No endpoint candidates matched target_path/target_method.")
    return matches, notes
