"""Cross-repo endpoint indexing and lookup helpers for inferred API linking.

This phase focuses on lightweight linking across repositories through inferred API
calls and discovered HTTP endpoints. It is not full distributed tracing, and does
not attempt queue/event linking unless evidence is explicitly available elsewhere.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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

HTTP_METHOD_CHAIN_RE = re.compile(
    r"(?P<client>[A-Za-z_][A-Za-z0-9_\.]*)\s*\.\s*(?P<method>get|post|put|patch|delete)\s*\(",
    re.IGNORECASE,
)
URI_CALL_RE = re.compile(r"""\.uri\s*\(\s*["'](?P<value>[^"']{1,240})["']\s*\)""", re.IGNORECASE)
CHAIN_START_RE = re.compile(r"\.\s*(?:get|post|put|delete|patch)\s*\(", re.IGNORECASE)
PYTHON_DECORATOR_ROUTE_RE = re.compile(
    r"^@\s*[A-Za-z_][A-Za-z0-9_\.]*\s*\.\s*(?:get|post|put|patch|delete|route)\s*\(",
    re.IGNORECASE,
)
SPRING_ROUTE_ANNOTATION_RE = re.compile(
    r"^@\s*(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\b",
    re.IGNORECASE,
)
EXPRESS_ROUTE_DECLARATION_RE = re.compile(
    r"^(?:app|router|blueprint|bp)\s*\.\s*(?:get|post|put|patch|delete|route)\s*\(",
    re.IGNORECASE,
)
CHAIN_CONTINUATION_RE = re.compile(
    r"^\s*(?:\.\s*[A-Za-z_][A-Za-z0-9_]*\s*\(|\)|\}\)\s*;?)",
    re.IGNORECASE,
)
QUOTED_LITERAL_RE = re.compile(r"""["'](?P<value>[^"'\n]{1,240})["']""")
ROUTE_LITERAL_RE = re.compile(r"""["'](?P<path>/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]{1,200})["']""")
OUTBOUND_CLIENT_HINT_RE = re.compile(
    r"\b(?:client|webclient|requests|httpx|axios)\b|fetch\s*\(|\.retrieve\s*\(|\.exchange\s*\(|\.uri\s*\(",
    re.IGNORECASE,
)
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
HTTP_METHOD_ALIASES = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
}
MAX_CHAIN_CONTINUATION_LINES = 6


@dataclass(frozen=True)
class CandidateExpression:
    """A bounded expression candidate used for cross-repo call extraction."""

    text: str
    line_start: int
    line_end: int
    extraction_type: str


def normalize_http_method(value: str | None) -> str | None:
    """Normalize HTTP method hints from loose call text into canonical verbs."""
    if value is None:
        return None
    token = value.strip().strip("\"'").lower()
    if not token:
        return None
    if token in HTTP_METHOD_ALIASES:
        return HTTP_METHOD_ALIASES[token]
    match = re.search(r"\b(get|post|put|delete|patch)\b", token)
    if match:
        return HTTP_METHOD_ALIASES[match.group(1)]
    return None


def normalize_api_path(value: str | None) -> str | None:
    """Normalize API path hints into slash-stable route paths for matching."""
    if value is None:
        return None
    path = value.strip().strip("\"'")
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        path = urlparse(path).path
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def _normalize_method(value: str | None) -> str | None:
    """Backward-compatible internal alias for method normalization."""
    return normalize_http_method(value)


def _normalize_path(value: str | None) -> str | None:
    """Backward-compatible internal alias for path normalization."""
    return normalize_api_path(value)


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


def _extract_method_and_client_from_line(line: str) -> tuple[str | None, str | None]:
    """Extract HTTP method + client expression from one chained-call line."""
    match = HTTP_METHOD_CHAIN_RE.search(line)
    if match is None:
        return None, None
    client_expr = match.group("client")
    raw_method = match.group("method")
    return _normalize_method(raw_method), client_expr


def _extract_uri_path_from_line(line: str) -> str | None:
    """Extract URI path from `.uri(\"/path\")` style chain segments."""
    match = URI_CALL_RE.search(line)
    if match is None:
        return None
    return _normalize_path(match.group("value"))


def _extract_any_path_from_line(line: str) -> tuple[str | None, str | None]:
    """Extract any path-like literal from a line (route literal or URL)."""
    for literal_match in QUOTED_LITERAL_RE.finditer(line):
        literal_value = literal_match.group("value")
        path, service_hint = _extract_path_and_service_hint(literal_value)
        if path is not None:
            return path, service_hint
    return None, None


def _compact_snippet(text: str, *, max_chars: int = 320) -> str:
    """Normalize raw expression text into a compact single-line snippet."""
    compact = " ".join(text.strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def is_route_declaration_line(line: str) -> bool:
    """Detect route declaration/annotation syntax (not outbound API calls)."""
    stripped = line.strip()
    if not stripped:
        return False
    if PYTHON_DECORATOR_ROUTE_RE.match(stripped):
        return True
    if SPRING_ROUTE_ANNOTATION_RE.match(stripped):
        return True
    if EXPRESS_ROUTE_DECLARATION_RE.match(stripped):
        return True
    return False


def _has_outbound_client_context(line: str) -> bool:
    """Heuristic outbound-call context check for candidate extraction."""
    return bool(OUTBOUND_CLIENT_HINT_RE.search(line))


def _is_chain_start(line: str) -> bool:
    """Return True when a line appears to begin an HTTP client call chain."""
    if is_route_declaration_line(line):
        return False
    if not CHAIN_START_RE.search(line):
        return False
    return _has_outbound_client_context(line)


def _is_chain_continuation(line: str) -> bool:
    """Return True when a line appears to continue a previously started chain."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("."):
        return True
    return bool(CHAIN_CONTINUATION_RE.match(line))


def iter_candidate_expressions(text: str) -> list[CandidateExpression]:
    """Yield bounded candidate expressions, merging short multiline call chains."""
    lines = text.splitlines()
    expressions: list[CandidateExpression] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        if _is_chain_start(stripped):
            combined_parts = [stripped]
            line_start = index
            line_end = index
            lookahead = index + 1
            appended = 0
            while lookahead < len(lines) and appended < MAX_CHAIN_CONTINUATION_LINES:
                continuation = lines[lookahead]
                if not _is_chain_continuation(continuation):
                    break
                combined_parts.append(continuation.strip())
                line_end = lookahead
                lookahead += 1
                appended += 1

            expressions.append(
                CandidateExpression(
                    text=" ".join(part for part in combined_parts if part),
                    line_start=line_start,
                    line_end=line_end,
                    extraction_type="multiline_chain" if line_end > line_start else "single_line",
                )
            )
            index = line_end + 1
            continue

        expressions.append(
            CandidateExpression(
                text=stripped,
                line_start=index,
                line_end=index,
                extraction_type="single_line",
            )
        )
        index += 1

    return expressions


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
        normalized_method = item.normalized_target_method or _normalize_method(item.target_method)
        normalized_path = item.normalized_target_path or _normalize_path(item.target_path)
        key: CandidateKey = (
            item.source_repo,
            item.source_file or "",
            normalized_method or "",
            normalized_path or "",
            _normalize_service(item.target_service_hint) or "",
        )
        existing = deduped.get(key)
        if existing is None:
            item.normalized_target_method = normalized_method
            item.normalized_target_path = normalized_path
            deduped[key] = item
            continue
        if existing.normalized_target_method is None:
            existing.normalized_target_method = normalized_method
        if existing.normalized_target_path is None:
            existing.normalized_target_path = normalized_path
        if existing.raw_call_text is None and item.raw_call_text:
            existing.raw_call_text = item.raw_call_text
        labels = {(e.file, e.symbol, e.label, e.snippet) for e in existing.evidence}
        for evidence in item.evidence:
            label_key = (evidence.file, evidence.symbol, evidence.label, evidence.snippet)
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
        line_offsets: list[int] = []
        cursor = 0
        for line in text.splitlines():
            line_offsets.append(cursor)
            cursor += len(line) + 1

        for expression in iter_candidate_expressions(text):
            expression_text = expression.text.strip()
            if not expression_text:
                continue
            if is_route_declaration_line(expression_text):
                continue
            if not _has_outbound_client_context(expression_text):
                continue

            char_index = line_offsets[expression.line_start] if expression.line_start < len(line_offsets) else 0
            symbol = source_symbol_hint or _infer_enclosing_symbol(text, char_index)
            method, client_expr = _extract_method_and_client_from_line(expression_text)
            uri_path = _extract_uri_path_from_line(expression_text)
            any_path, any_path_service = _extract_any_path_from_line(expression_text)
            service_hint = _service_hint_from_client_expr(client_expr) or any_path_service

            normalized_method = _normalize_method(method)
            normalized_path = uri_path or any_path
            if normalized_path is not None:
                normalized_path = _normalize_path(normalized_path)
            is_multiline_chain = expression.extraction_type == "multiline_chain"
            strong_label_prefix = "multiline_chain" if is_multiline_chain else "chain_extraction"
            partial_label_prefix = "multiline_chain_partial" if is_multiline_chain else "partial_extraction"

            # Strong chained-call extraction: method + explicit uri(...) path.
            if normalized_method is not None and uri_path is not None:
                snippet = _compact_snippet(expression_text)
                candidates.append(
                    CrossRepoCallCandidate(
                        source_repo=source_repo,
                        source_file=source_file,
                        source_symbol=symbol,
                        target_path=normalized_path,
                        target_method=normalized_method,
                        normalized_target_path=normalized_path,
                        normalized_target_method=normalized_method,
                        target_service_hint=service_hint,
                        raw_call_text=expression_text,
                        evidence=[
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label=f"{strong_label_prefix}:{normalized_method}:{normalized_path or '?'}",
                            ),
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label="webclient_call",
                                snippet=snippet,
                            ),
                        ],
                        confidence=max(0.8, _confidence_from_parts(normalized_method, normalized_path, service_hint)),
                        status="extracted_from_chain",
                    )
                )
                continue

            # Generic method+path extraction from non-chain call forms (e.g. requests.get('/x')).
            if normalized_method is not None and normalized_path is not None:
                snippet = _compact_snippet(expression_text)
                candidates.append(
                    CrossRepoCallCandidate(
                        source_repo=source_repo,
                        source_file=source_file,
                        source_symbol=symbol,
                        target_path=normalized_path,
                        target_method=normalized_method,
                        normalized_target_path=normalized_path,
                        normalized_target_method=normalized_method,
                        target_service_hint=service_hint,
                        raw_call_text=expression_text,
                        evidence=[
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label=f"{strong_label_prefix}:{normalized_method}:{normalized_path or '?'}",
                            ),
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label="http_client_call",
                                snippet=snippet,
                            ),
                        ],
                        confidence=_confidence_from_parts(normalized_method, normalized_path, service_hint),
                        status=STATUS_INFERRED,
                    )
                )
                continue

            # Method-only lines become partial candidates; do not mark as strong.
            if normalized_method is not None and normalized_path is None:
                snippet = _compact_snippet(expression_text)
                candidates.append(
                    CrossRepoCallCandidate(
                        source_repo=source_repo,
                        source_file=source_file,
                        source_symbol=symbol,
                        target_path=None,
                        target_method=normalized_method,
                        normalized_target_path=None,
                        normalized_target_method=normalized_method,
                        target_service_hint=service_hint,
                        raw_call_text=expression_text,
                        evidence=[
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label=f"{partial_label_prefix}:{normalized_method}:?",
                            ),
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label="http_client_call_partial",
                                snippet=snippet,
                            ),
                        ],
                        confidence=0.4,
                        status="partial",
                    )
                )
                continue

            # Path-only client context remains a partial candidate.
            if normalized_path is not None:
                snippet = _compact_snippet(expression_text)
                candidates.append(
                    CrossRepoCallCandidate(
                        source_repo=source_repo,
                        source_file=source_file,
                        source_symbol=symbol,
                        target_path=normalized_path,
                        target_method=None,
                        normalized_target_path=normalized_path,
                        normalized_target_method=None,
                        target_service_hint=service_hint,
                        raw_call_text=expression_text,
                        evidence=[
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label=f"{partial_label_prefix}:?:{normalized_path}",
                            ),
                            EvidenceRef(
                                file=source_file,
                                symbol=symbol,
                                label="http_client_path_partial",
                                snippet=snippet,
                            ),
                        ],
                        confidence=_confidence_from_parts(None, normalized_path, service_hint),
                        status="partial",
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
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    for evidence in [*call_evidence, *endpoint_evidence]:
        key = (evidence.file, evidence.symbol, evidence.label, evidence.snippet)
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
    normalized_method = call.normalized_target_method or _normalize_method(call.target_method)
    normalized_path = call.normalized_target_path or _normalize_path(call.target_path)
    normalized_service = _normalize_service(call.target_service_hint)
    notes: list[str] = [
        f"Cross-repo candidate normalized: method={normalized_method or '?'} path={normalized_path or '?'}."
    ]
    if normalized_path is None:
        notes.append("Call candidate has no normalized path; cannot perform endpoint lookup.")
        return [], None, notes

    by_method_path = index.get("by_method_path", {})
    by_path = index.get("by_path", {})

    if normalized_method is not None:
        exact = list(by_method_path.get((normalized_method, normalized_path), []))
        if exact:
            if normalized_service:
                narrowed = [item for item in exact if _normalize_service(item.service) == normalized_service]
                if narrowed:
                    notes.append("Applied service hint narrowing within exact method+path matches.")
                    exact = narrowed
            notes.append(f"Matched {len(exact)} endpoint candidate(s) by exact method+path.")
            return sorted(exact, key=_deterministic_endpoint_sort_key), "exact_method_path", notes

    path_only = list(by_path.get(normalized_path, []))
    if path_only:
        if normalized_service:
            narrowed = [item for item in path_only if _normalize_service(item.service) == normalized_service]
            if narrowed:
                notes.append("Applied service hint narrowing within path-only matches.")
                path_only = narrowed
        notes.append(f"Matched {len(path_only)} endpoint candidate(s) by normalized path fallback.")
        return sorted(path_only, key=_deterministic_endpoint_sort_key), "path_only", notes

    notes.append("No endpoint candidates matched by normalized method+path or path-only.")
    return [], None, notes


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
    normalized_method = call.normalized_target_method or _normalize_method(call.target_method)
    normalized_path = call.normalized_target_path or _normalize_path(call.target_path)
    raw_call_text = (call.raw_call_text or "").strip()
    if not matches:
        no_match_notes = list(notes)
        if raw_call_text:
            no_match_notes.append(f"Raw call text: {raw_call_text}")
        return [
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=None,
                link_type=None,
                normalized_target_method=normalized_method,
                normalized_target_path=normalized_path,
                evidence=list(call.evidence),
                confidence=_link_confidence(call, None),
                notes=no_match_notes,
            )
        ]

    if len(matches) == 1:
        endpoint = matches[0]
        matched_method = _normalize_method(endpoint.method) or "?"
        matched_path = _normalize_path(endpoint.path) or "?"
        matched_notes = [*notes, f"Matched target endpoint: {endpoint.repo}::{matched_method} {matched_path}."]
        if raw_call_text:
            matched_notes.append(f"Raw call text: {raw_call_text}")
        return [
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=_endpoint_lookup_id(endpoint),
                link_type=link_type,
                normalized_target_method=normalized_method,
                normalized_target_path=normalized_path,
                evidence=_merge_link_evidence(call.evidence, endpoint.evidence),
                confidence=_link_confidence(call, endpoint),
                notes=matched_notes,
            )
        ]

    stronger = _choose_clearly_stronger_match(matches)
    if stronger is not None:
        selected_notes = [*notes, "Multiple matches found; selected clearly stronger candidate by confidence."]
        selected_notes.append(
            f"Matched target endpoint: {stronger.repo}::{_normalize_method(stronger.method) or '?'} {_normalize_path(stronger.path) or '?'}."
        )
        if raw_call_text:
            selected_notes.append(f"Raw call text: {raw_call_text}")
        return [
            CrossRepoLinkResult(
                source_endpoint_id=source_id,
                matched_target_endpoint_id=_endpoint_lookup_id(stronger),
                link_type=link_type,
                normalized_target_method=normalized_method,
                normalized_target_path=normalized_path,
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
                normalized_target_method=normalized_method,
                normalized_target_path=normalized_path,
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
