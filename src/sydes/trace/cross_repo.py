"""Cross-repo endpoint indexing and lookup helpers for inferred API linking.

This phase focuses on lightweight linking across repositories through inferred API
calls and discovered HTTP endpoints. It is not full distributed tracing, and does
not attempt queue/event linking unless evidence is explicitly available elsewhere.
"""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse
import re
from typing import TypeAlias

from sydes.core.models import (
    CandidateFileRead,
    CrossRepoCallCandidate,
    CrossRepoLinkResult,
    EndpointCandidate,
    EvidenceRef,
    ExpansionContextFile,
    FlowExpansionContext,
    STATUS_INFERRED,
)

MethodPathKey: TypeAlias = tuple[str, str]
ServicePathKey: TypeAlias = tuple[str, str]
ServiceMethodPathKey: TypeAlias = tuple[str, str, str]
CandidateKey: TypeAlias = tuple[str, str, str, str, str]

HTTP_CLIENT_CALL_RE = re.compile(
    r"(?P<client>[A-Za-z_][A-Za-z0-9_\.]*)\.(?P<method>get|post|put|patch|delete)\s*\((?P<args>[^)\n]{0,320})\)",
    re.IGNORECASE,
)
QUOTED_LITERAL_RE = re.compile(r"""["'](?P<value>[^"'\n]{1,240})["']""")
ROUTE_LITERAL_RE = re.compile(r"""["'](?P<path>/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]{1,200})["']""")
CLIENT_HINT_RE = re.compile(r"\b(client|service|requests|httpx|axios|fetch)\b", re.IGNORECASE)
SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*function\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(
        r"^\s*(?:const|let|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(?.*\)?\s*=>"
    ),
)
GENERIC_SERVICE_TOKENS = {
    "api",
    "apis",
    "internal",
    "service",
    "services",
    "svc",
    "client",
    "gateway",
    "localhost",
    "127",
}


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


def build_endpoint_lookup_id(endpoint: EndpointCandidate) -> str:
    """Public wrapper for deterministic endpoint lookup id generation."""
    return _endpoint_lookup_id(endpoint)


def _confidence_from_parts(method: str | None, path: str | None, service_hint: str | None) -> float:
    """Compute a small deterministic confidence score for call candidates."""
    score = 0.35
    if method:
        score += 0.2
    if path:
        score += 0.3
    if service_hint:
        score += 0.1
    if score > 0.9:
        score = 0.9
    return score


def _extract_path_and_service_hint(value: str | None) -> tuple[str | None, str | None]:
    """Extract path and service hint from URL/path-like literal values."""
    if value is None:
        return None, None
    literal = value.strip()
    if not literal:
        return None, None
    if literal.startswith("/"):
        return _normalize_path(literal), None
    if literal.startswith("http://") or literal.startswith("https://"):
        parsed = urlparse(literal)
        path = _normalize_path(parsed.path)
        host = parsed.hostname or ""
        service_hint = None
        if host:
            pieces = [piece for piece in host.split(".") if piece]
            for piece in pieces:
                token = piece.lower()
                if token in GENERIC_SERVICE_TOKENS:
                    continue
                if token.isdigit():
                    continue
                service_hint = token
                break
        return path, service_hint
    return None, None


def _service_hint_from_client_expr(client_expr: str | None) -> str | None:
    """Infer a possible service hint from client variable/callee names."""
    if not client_expr:
        return None
    token = client_expr.split(".")[-1].lower()
    token = re.sub(r"_(client|service|api)$", "", token)
    token = re.sub(r"(client|service|api)$", "", token)
    token = token.strip("_- ")
    if not token:
        return None
    if token in {"requests", "httpx", "axios", "fetch", "client"}:
        return None
    return token


def _infer_enclosing_symbol(text: str, char_index: int) -> str | None:
    """Infer a nearby function symbol name without AST parsing."""
    prefix = text[:char_index]
    lines = prefix.splitlines()
    for line in reversed(lines):
        for pattern in SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                return match.group("name")
    return None


def _iter_readable_context_files(context: FlowExpansionContext) -> list[tuple[ExpansionContextFile, CandidateFileRead]]:
    """Return context files that contain readable snippets."""
    result: list[tuple[ExpansionContextFile, CandidateFileRead]] = []
    for item in context.files:
        read = item.read
        if read is None or read.skipped or read.snippet is None:
            continue
        result.append((item, read))
    return result


def _dedupe_cross_repo_candidates(
    candidates: list[CrossRepoCallCandidate],
) -> list[CrossRepoCallCandidate]:
    """Deduplicate candidates deterministically while preserving useful evidence."""
    deduped: dict[CandidateKey, CrossRepoCallCandidate] = {}
    for item in candidates:
        key: CandidateKey = (
            item.source_repo,
            item.source_file or "",
            _normalize_method(item.target_method) or "",
            _normalize_path(item.target_path) or "",
            _normalize_service(item.target_service_hint) or "",
        )
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = item
            continue
        if existing.raw_call_text is None and item.raw_call_text:
            existing.raw_call_text = item.raw_call_text
        labels = {(e.file, e.symbol, e.label) for e in existing.evidence}
        for evidence in item.evidence:
            label_key = (evidence.file, evidence.symbol, evidence.label)
            if label_key in labels:
                continue
            existing.evidence.append(evidence)
            labels.add(label_key)
        if (existing.confidence or 0.0) < (item.confidence or 0.0):
            existing.confidence = item.confidence
    return list(deduped.values())


def detect_cross_repo_call_candidates(
    context: FlowExpansionContext,
    *,
    source_symbol_hint: str | None = None,
) -> list[CrossRepoCallCandidate]:
    """Detect likely internal API call candidates from bounded flow expansion context."""
    candidates: list[CrossRepoCallCandidate] = []
    for context_file, read in _iter_readable_context_files(context):
        snippet = read.snippet
        assert snippet is not None  # narrowed by _iter_readable_context_files
        text = snippet.text
        source_repo = snippet.repo or context.anchor_repo
        source_file = snippet.relative_path
        http_call_line_indexes: set[int] = set()

        for match in HTTP_CLIENT_CALL_RE.finditer(text):
            line_index = text.count("\n", 0, match.start())
            http_call_line_indexes.add(line_index)
            method = _normalize_method(match.group("method"))
            client_expr = match.group("client")
            args = match.group("args") or ""
            literal = None
            for literal_match in QUOTED_LITERAL_RE.finditer(args):
                literal = literal_match.group("value")
                path, url_service = _extract_path_and_service_hint(literal)
                if path is not None:
                    break
            else:
                path = None
                url_service = None

            service_hint = url_service or _service_hint_from_client_expr(client_expr)
            symbol = source_symbol_hint or _infer_enclosing_symbol(text, match.start())
            raw_call_text = match.group(0).strip()
            candidates.append(
                CrossRepoCallCandidate(
                    source_repo=source_repo,
                    source_file=source_file,
                    source_symbol=symbol,
                    target_path=path,
                    target_method=method,
                    target_service_hint=service_hint,
                    raw_call_text=raw_call_text,
                    evidence=[
                        EvidenceRef(
                            file=source_file,
                            symbol=symbol,
                            label="http_client_call",
                        )
                    ],
                    confidence=_confidence_from_parts(method, path, service_hint),
                    status=STATUS_INFERRED,
                )
            )

        for line_index, line in enumerate(text.splitlines()):
            if line_index in http_call_line_indexes:
                continue
            if not CLIENT_HINT_RE.search(line):
                continue
            path_match = ROUTE_LITERAL_RE.search(line)
            if path_match is None:
                continue
            path = _normalize_path(path_match.group("path"))
            if path is None:
                continue
            symbol = source_symbol_hint or _infer_enclosing_symbol(text, text.find(line))
            client_hint = _service_hint_from_client_expr(line.split(".", 1)[0])
            candidates.append(
                CrossRepoCallCandidate(
                    source_repo=source_repo,
                    source_file=source_file,
                    source_symbol=symbol,
                    target_path=path,
                    target_method=None,
                    target_service_hint=client_hint,
                    raw_call_text=line.strip(),
                    evidence=[
                        EvidenceRef(
                            file=source_file,
                            symbol=symbol,
                            label="route_literal_in_client_context",
                        )
                    ],
                    confidence=_confidence_from_parts(None, path, client_hint),
                    status=STATUS_INFERRED,
                )
            )

    return _dedupe_cross_repo_candidates(candidates)


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


def _source_lookup_id(call: CrossRepoCallCandidate) -> str:
    """Build a deterministic source identifier for cross-repo link records."""
    return (
        f"{call.source_repo}:"
        f"{call.source_file or '?'}:"
        f"{call.source_symbol or '?'}"
    )


def build_call_source_lookup_id(call: CrossRepoCallCandidate) -> str:
    """Public wrapper for deterministic cross-repo call source id generation."""
    return _source_lookup_id(call)


def _merge_link_evidence(
    call_evidence: list[EvidenceRef],
    endpoint_evidence: list[EvidenceRef],
) -> list[EvidenceRef]:
    """Merge call + endpoint evidence while preserving deterministic order."""
    merged: list[EvidenceRef] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for evidence in [*call_evidence, *endpoint_evidence]:
        key = (evidence.file, evidence.symbol, evidence.label)
        if key in seen:
            continue
        seen.add(key)
        merged.append(evidence)
    return merged


def _link_confidence(call: CrossRepoCallCandidate, endpoint: EndpointCandidate | None) -> float | None:
    """Compute deterministic link confidence from call + endpoint confidence."""
    call_conf = call.confidence
    endpoint_conf = endpoint.confidence if endpoint is not None else None
    if call_conf is None and endpoint_conf is None:
        return None
    if endpoint is None:
        return min(0.8, max(0.0, (call_conf or 0.0) * 0.8))
    return min(0.95, max(0.0, ((call_conf or 0.45) + (endpoint_conf or 0.45)) / 2))


def _deterministic_endpoint_sort_key(endpoint: EndpointCandidate) -> tuple[str, str, str, str]:
    """Sort endpoint matches deterministically before linking decisions."""
    return (
        endpoint.repo,
        endpoint.file,
        _normalize_method(endpoint.method) or "",
        _normalize_path(endpoint.path) or "",
    )


def _lookup_with_priority(
    call: CrossRepoCallCandidate,
    index: dict[str, dict[object, list[EndpointCandidate]]],
) -> tuple[list[EndpointCandidate], str | None, list[str]]:
    """Look up endpoint matches using conservative phase priority rules."""
    notes: list[str] = []
    normalized_path = _normalize_path(call.target_path)
    normalized_method = _normalize_method(call.target_method)
    normalized_service = _normalize_service(call.target_service_hint)
    if normalized_path is None:
        return [], None, ["Call candidate has no target_path; cannot perform endpoint lookup."]

    by_method_path = index.get("by_method_path", {})
    by_path = index.get("by_path", {})
    by_service_path = index.get("by_service_path", {})
    by_service_method_path = index.get("by_service_method_path", {})

    if normalized_method is not None:
        exact = list(by_method_path.get((normalized_method, normalized_path), []))
        if exact:
            if normalized_service:
                narrowed = [item for item in exact if _normalize_service(item.service) == normalized_service]
                if narrowed:
                    notes.append("Applied service hint narrowing within exact method+path matches.")
                    exact = narrowed
            return sorted(exact, key=_deterministic_endpoint_sort_key), "exact_method_path", notes

    path_only = list(by_path.get(normalized_path, []))
    if path_only:
        if normalized_service:
            narrowed = [item for item in path_only if _normalize_service(item.service) == normalized_service]
            if narrowed:
                notes.append("Applied service hint narrowing within path-only matches.")
                path_only = narrowed
        return sorted(path_only, key=_deterministic_endpoint_sort_key), "path_only", notes

    if normalized_service:
        if normalized_method is not None:
            service_method = list(
                by_service_method_path.get((normalized_service, normalized_method, normalized_path), [])
            )
            if service_method:
                notes.append("Matched using service hint + method + path.")
                return sorted(service_method, key=_deterministic_endpoint_sort_key), "service_path", notes
        service_path = list(by_service_path.get((normalized_service, normalized_path), []))
        if service_path:
            notes.append("Matched using service hint + path.")
            return sorted(service_path, key=_deterministic_endpoint_sort_key), "service_path", notes

    return [], None, ["No endpoint candidates matched by method/path, path-only, or service+path."]


def _choose_clearly_stronger_match(matches: list[EndpointCandidate]) -> EndpointCandidate | None:
    """Choose one endpoint only when confidence clearly dominates alternatives."""
    if len(matches) <= 1:
        return matches[0] if matches else None
    ranked = sorted(
        matches,
        key=lambda item: ((item.confidence if item.confidence is not None else -1.0), _deterministic_endpoint_sort_key(item)),
        reverse=True,
    )
    top = ranked[0]
    second = ranked[1]
    if top.confidence is None or second.confidence is None:
        return None
    if top.confidence - second.confidence >= 0.2:
        return top
    return None


def link_cross_repo_call_candidate(
    call: CrossRepoCallCandidate,
    index: dict[str, dict[object, list[EndpointCandidate]]],
) -> list[CrossRepoLinkResult]:
    """Link one call candidate to one or more endpoint targets across repos."""
    matches, link_type, notes = _lookup_with_priority(call, index)
    source_id = _source_lookup_id(call)
    if not matches:
        return [
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=None,
                link_type=None,
                evidence=list(call.evidence),
                confidence=_link_confidence(call, None),
                notes=notes,
            )
        ]

    if len(matches) == 1:
        endpoint = matches[0]
        return [
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=_endpoint_lookup_id(endpoint),
                link_type=link_type,
                evidence=_merge_link_evidence(call.evidence, endpoint.evidence),
                confidence=_link_confidence(call, endpoint),
                notes=notes,
            )
        ]

    stronger = _choose_clearly_stronger_match(matches)
    if stronger is not None:
        selected_notes = [*notes, "Multiple matches found; selected clearly stronger candidate by confidence."]
        return [
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=_endpoint_lookup_id(stronger),
                link_type=link_type,
                evidence=_merge_link_evidence(call.evidence, stronger.evidence),
                confidence=_link_confidence(call, stronger),
                notes=selected_notes,
            )
        ]

    ambiguity_notes = [
        *notes,
        f"Ambiguous endpoint link: {len(matches)} candidates matched; no clearly stronger candidate.",
    ]
    results: list[CrossRepoLinkResult] = []
    for endpoint in matches:
        results.append(
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=_endpoint_lookup_id(endpoint),
                link_type=link_type,
                evidence=_merge_link_evidence(call.evidence, endpoint.evidence),
                confidence=_link_confidence(call, endpoint),
                notes=ambiguity_notes,
            )
        )
    return results


def link_cross_repo_call_candidates(
    calls: list[CrossRepoCallCandidate],
    endpoints: list[EndpointCandidate],
) -> list[CrossRepoLinkResult]:
    """Link multiple call candidates against discovered endpoints across repos."""
    index = index_discovered_endpoints(endpoints)
    results: list[CrossRepoLinkResult] = []
    for call in calls:
        results.extend(link_cross_repo_call_candidate(call, index))
    return results


def resolve_cross_repo_call_targets(
    call: CrossRepoCallCandidate,
    index: dict[str, dict[object, list[EndpointCandidate]]],
) -> tuple[list[EndpointCandidate], list[str]]:
    """Resolve endpoint candidates for compatibility with earlier helper usage."""
    matches, _link_type, notes = _lookup_with_priority(call, index)
    return matches, notes
