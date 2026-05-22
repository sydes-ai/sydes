"""Endpoint discovery pipeline over bounded candidate file reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from collections import Counter
import re

from sydes.core.models import (
    CandidateFileRead,
    ConfidenceSummary,
    EndpointCandidate,
    EndpointDiscoveryResult,
    EvidenceRef,
    RepoRef,
    RoutesResult,
)
from sydes.ingest.inventory import build_repo_inventory
from sydes.ingest.ranking import rank_candidate_files
from sydes.ingest.readers import read_ranked_candidate_files_for_discovery
from sydes.ingest.repos import validate_repo_roots
from sydes.ingest.sense import sense_repo
from sydes.ingest.file_roles import (
    FILE_ROLE_DOCS_CANDIDATE,
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    classify_candidate_file_role,
)
from sydes.llm.client import LLMClient, LLMRequest
from sydes.llm.client import (
    classify_llm_error,
    LLMClientError,
    create_default_llm_client,
    load_llm_settings_from_env,
)
from sydes.llm.prompts import build_endpoint_discovery_prompt
from sydes.discover.deterministic_routes import extract_deterministic_routes


def _strip_markdown_fences(text: str) -> str:
    """Remove common markdown code-fence wrappers around JSON payloads."""
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


def _extract_json_payload(text: str) -> Any | None:
    """Best-effort parse for JSON object or list responses from model output."""
    text = _strip_markdown_fences(text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    list_start = text.find("[")
    list_end = text.rfind("]")
    slices: list[tuple[int, int]] = []
    if brace_start >= 0 and brace_end > brace_start:
        slices.append((brace_start, brace_end + 1))
    if list_start >= 0 and list_end > list_start:
        slices.append((list_start, list_end + 1))
    if not slices:
        return None

    slices.sort(key=lambda item: item[0])
    for start, end in slices:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None


def _coerce_raw_endpoints(payload: Any) -> tuple[list[Any], list[str]]:
    """Coerce supported payload shapes into a raw endpoint list."""
    if isinstance(payload, list):
        return payload, []
    if isinstance(payload, dict):
        raw = payload.get("endpoints")
        if isinstance(raw, list):
            return raw, []
        if isinstance(payload.get("routes"), list):
            return payload["routes"], ["Using 'routes' field as endpoint list."]
        if isinstance(payload.get("candidates"), list):
            return payload["candidates"], ["Using 'candidates' field as endpoint list."]
        return [], ["Model output missing endpoint list; treated as empty."]
    return [], ["Model output was neither object nor list; treated as empty."]


def _normalize_evidence(raw: Any, fallback_file: str) -> list[EvidenceRef]:
    """Normalize evidence entries from loose model JSON output."""
    evidence: list[EvidenceRef] = []
    if not isinstance(raw, list):
        return [EvidenceRef(file=fallback_file, label="inferred-from-file")]

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        file_value = entry.get("file")
        if not isinstance(file_value, str) or not file_value.strip():
            file_value = fallback_file
        symbol = entry.get("symbol") if isinstance(entry.get("symbol"), str) else None
        label = entry.get("label") if isinstance(entry.get("label"), str) else None
        evidence.append(EvidenceRef(file=file_value, symbol=symbol, label=label))

    if not evidence:
        evidence.append(EvidenceRef(file=fallback_file, label="inferred-from-file"))
    return evidence


def _normalize_method(method: str | None) -> str | None:
    """Normalize HTTP method values when present."""
    if method is None:
        return None
    normalized = method.strip().upper()
    return normalized or None


def _normalize_path(path: str | None) -> str | None:
    """Normalize endpoint path values when present."""
    if path is None:
        return None
    normalized = path.strip()
    if not normalized:
        return None
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _normalize_handler(handler: str | None) -> str | None:
    """Normalize handler symbol/name values when present."""
    if handler is None:
        return None
    normalized = handler.strip()
    while normalized and normalized[-1] in ",;:()[]{}":
        normalized = normalized[:-1].strip()
    while normalized and normalized[0] in ",;:()[]{}":
        normalized = normalized[1:].strip()
    if normalized.lower() in {"none", "null", "unknown", "n/a"}:
        return None
    return normalized or None


def _normalize_file_path(path: str | None) -> str | None:
    """Normalize file path values into stable relative-posix form."""
    if path is None:
        return None
    normalized = path.strip()
    if not normalized:
        return None
    normalized = normalized.replace("\\", "/")
    return Path(normalized).as_posix()


def _has_strong_evidence(endpoint: EndpointCandidate) -> bool:
    """Return true for unusually strong evidence when path+handler are both missing."""
    if endpoint.confidence is not None and endpoint.confidence >= 0.85:
        return True
    labels = [
        (item.label or "").lower()
        for item in endpoint.evidence
    ]
    strong_markers = ("route", "router", "endpoint", "http")
    return any(any(marker in label for marker in strong_markers) for label in labels)


INVOCATION_PATTERNS = (
    r"\bclient\.(get|post|put|patch|delete)\s*\(",
    r"\btest_client\.(get|post|put|patch|delete)\s*\(",
    r"\brequests\.(get|post|put|patch|delete)\s*\(",
    r"\bhttpx\.(get|post|put|patch|delete)\s*\(",
    r"\brequest\(app\)\.(get|post|put|patch|delete)\s*\(",
    r"\bsupertest\(app\)\.(get|post|put|patch|delete)\s*\(",
    r"\bfetch\s*\(",
    r"\baxios\.(get|post|put|patch|delete)\s*\(",
)
DECLARATION_PATTERNS = (
    r"@app\.route\s*\(",
    r"@bp\.route\s*\(",
    r"@router\.(get|post|put|patch|delete)\s*\(",
    r"@app\.(get|post|put|patch|delete)\s*\(",
    r"\bapp\.(get|post|put|patch|delete)\s*\(",
    r"\brouter\.(get|post|put|patch|delete)\s*\(",
    r"@GetMapping\s*\(",
    r"@PostMapping\s*\(",
    r"@PutMapping\s*\(",
    r"@DeleteMapping\s*\(",
    r"@PatchMapping\s*\(",
    r"@RequestMapping\s*\(",
)


def _evidence_text(endpoint: EndpointCandidate) -> str:
    """Flatten endpoint evidence/handler into one text blob for heuristics."""
    parts: list[str] = []
    if endpoint.handler:
        parts.append(endpoint.handler)
    for item in endpoint.evidence:
        if item.label:
            parts.append(item.label)
        if item.snippet:
            parts.append(item.snippet)
    return " ".join(parts).lower()


def _has_route_invocation_evidence(endpoint: EndpointCandidate) -> bool:
    """Return True when evidence looks like HTTP invocation, not declaration."""
    blob = _evidence_text(endpoint)
    return any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in INVOCATION_PATTERNS)


def _has_route_declaration_evidence(endpoint: EndpointCandidate) -> bool:
    """Return True when evidence looks like route declaration syntax."""
    blob = _evidence_text(endpoint)
    return any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in DECLARATION_PATTERNS)


def _validate_route_source(endpoint: EndpointCandidate) -> str | None:
    """Validate likely declaration source; return rejection reason when invalid."""
    role = classify_candidate_file_role(endpoint.file)
    if role == FILE_ROLE_TEST_USAGE_CANDIDATE:
        return "route_declared_in_test_file"
    if role == FILE_ROLE_DOCS_CANDIDATE:
        return "route_declared_in_docs_file"
    if _has_route_invocation_evidence(endpoint) and not _has_route_declaration_evidence(endpoint):
        return "route_invocation_not_declaration"
    return None


def _apply_quality_filters(
    endpoints: list[EndpointCandidate],
) -> tuple[list[EndpointCandidate], list[str]]:
    """Filter out very weak/unusable endpoint candidates."""
    kept: list[EndpointCandidate] = []
    notes: list[str] = []
    for idx, endpoint in enumerate(endpoints, start=1):
        rejected_reason = _validate_route_source(endpoint)
        if rejected_reason is not None:
            method = endpoint.method or "?"
            path = endpoint.path or "?"
            notes.append(
                f"Rejected route {method} {path} from {endpoint.file}: {rejected_reason}"
            )
            continue
        if not endpoint.file:
            notes.append(f"Dropped endpoint #{idx}: missing file grounding.")
            continue
        if not endpoint.repo:
            notes.append(f"Dropped endpoint #{idx}: missing repo grounding.")
            continue
        if endpoint.path == "/" and endpoint.method is None and not _has_strong_evidence(endpoint):
            notes.append(
                f"Dropped endpoint #{idx} ({endpoint.repo}:{endpoint.file}): "
                "root path with missing method and weak evidence."
            )
            continue
        if endpoint.method is None and endpoint.handler is None and not _has_strong_evidence(endpoint):
            notes.append(
                f"Dropped endpoint #{idx} ({endpoint.repo}:{endpoint.file}): "
                "missing both method and handler with weak evidence."
            )
            continue
        if endpoint.path is None and endpoint.handler is None and not _has_strong_evidence(endpoint):
            notes.append(
                f"Dropped endpoint #{idx} ({endpoint.repo}:{endpoint.file}): "
                "missing both path and handler with weak evidence."
            )
            continue
        kept.append(endpoint)
    return kept, notes


def _build_candidate_index(
    candidates: list[CandidateFileRead],
) -> tuple[dict[str, CandidateFileRead], str | None]:
    """Index candidates by file path and infer a shared repo when possible."""
    by_file: dict[str, CandidateFileRead] = {}
    repos: set[str] = set()
    for candidate in candidates:
        by_file[candidate.relative_path] = candidate
        repos.add(candidate.repo)
    shared_repo = next(iter(repos)) if len(repos) == 1 else None
    return by_file, shared_repo


def _normalize_endpoints(
    raw_endpoints: Any,
    candidates: list[CandidateFileRead],
) -> tuple[list[EndpointCandidate], list[str]]:
    """Coerce loose endpoint output into soft endpoint candidate models."""
    notes: list[str] = []
    if not isinstance(raw_endpoints, list):
        return [], ["Model output missing 'endpoints' list; treated as empty."]

    by_file, shared_repo = _build_candidate_index(candidates)
    normalized: list[EndpointCandidate] = []

    for idx, raw in enumerate(raw_endpoints):
        if isinstance(raw, EndpointCandidate):
            normalized.append(
                EndpointCandidate(
                    method=_normalize_method(raw.method),
                    path=_normalize_path(raw.path),
                    handler=_normalize_handler(raw.handler),
                    file=_normalize_file_path(raw.file) or raw.file,
                    repo=raw.repo.strip(),
                    service=raw.service.strip() if isinstance(raw.service, str) and raw.service.strip() else None,
                    evidence=raw.evidence,
                    confidence=raw.confidence,
                    status=raw.status.strip() if isinstance(raw.status, str) and raw.status.strip() else None,
                )
            )
            continue
        if not isinstance(raw, dict):
            notes.append(f"Ignored endpoint #{idx + 1}: not an object.")
            continue

        file_value = raw.get("file") if isinstance(raw.get("file"), str) else None
        repo_value = raw.get("repo") if isinstance(raw.get("repo"), str) else None
        if file_value is None:
            evidence_file = None
            raw_evidence = raw.get("evidence")
            if isinstance(raw_evidence, list):
                for entry in raw_evidence:
                    if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                        evidence_file = entry["file"]
                        break
            file_value = evidence_file

        if file_value is None:
            notes.append(f"Ignored endpoint #{idx + 1}: missing file grounding.")
            continue

        if repo_value is None and file_value in by_file:
            repo_value = by_file[file_value].repo
        if repo_value is None:
            repo_value = shared_repo
        if repo_value is None:
            notes.append(f"Ignored endpoint #{idx + 1}: missing repo grounding.")
            continue

        method = raw.get("method") if isinstance(raw.get("method"), str) else None
        path = raw.get("path") if isinstance(raw.get("path"), str) else None
        handler = raw.get("handler") if isinstance(raw.get("handler"), str) else None
        service = raw.get("service") if isinstance(raw.get("service"), str) else None
        confidence = raw.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = None
        status = raw.get("status") if isinstance(raw.get("status"), str) else None
        file_value = _normalize_file_path(file_value)
        if file_value is None:
            notes.append(f"Ignored endpoint #{idx + 1}: missing file grounding.")
            continue
        repo_value = repo_value.strip()
        if not repo_value:
            notes.append(f"Ignored endpoint #{idx + 1}: missing repo grounding.")
            continue
        evidence = _normalize_evidence(raw.get("evidence"), fallback_file=file_value)

        normalized.append(
            EndpointCandidate(
                method=_normalize_method(method),
                path=_normalize_path(path),
                handler=_normalize_handler(handler),
                file=file_value,
                repo=repo_value,
                service=service.strip() if isinstance(service, str) and service.strip() else None,
                evidence=evidence,
                confidence=float(confidence) if confidence is not None else None,
                status=status.strip() if isinstance(status, str) and status.strip() else None,
            )
        )

    return normalized, notes


def _endpoint_dedupe_key(endpoint: EndpointCandidate) -> tuple[str, ...]:
    """Build a dedupe key that stays safe for partial endpoints."""
    method = endpoint.method or ""
    path = endpoint.path or ""
    handler = endpoint.handler or ""
    if method or path or handler:
        return (endpoint.repo, endpoint.file, method, path, handler)

    evidence_symbols = sorted(
        {item.symbol for item in endpoint.evidence if isinstance(item.symbol, str) and item.symbol}
    )
    if evidence_symbols:
        return (endpoint.repo, endpoint.file, "", "", "|".join(evidence_symbols))
    return (endpoint.repo, endpoint.file, "", "", "__unresolved__")


def _dedupe_endpoints(endpoints: list[EndpointCandidate]) -> list[EndpointCandidate]:
    """Merge obvious duplicates while preserving strongest confidence/evidence."""
    deduped: dict[tuple[str, ...], EndpointCandidate] = {}
    for endpoint in endpoints:
        key = _endpoint_dedupe_key(endpoint)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = endpoint
            continue

        merged_evidence = existing.evidence + endpoint.evidence
        best_confidence = existing.confidence
        if endpoint.confidence is not None and (
            best_confidence is None or endpoint.confidence > best_confidence
        ):
            best_confidence = endpoint.confidence
        deduped[key] = EndpointCandidate(
            method=existing.method or endpoint.method,
            path=existing.path or endpoint.path,
            handler=existing.handler or endpoint.handler,
            file=existing.file,
            repo=existing.repo,
            service=existing.service or endpoint.service,
            evidence=merged_evidence,
            confidence=best_confidence,
            status=existing.status or endpoint.status,
        )
    return list(deduped.values())


def run_llm_endpoint_discovery(
    candidates: list[CandidateFileRead],
    *,
    llm_client: LLMClient | None = None,
    model_spec: str | None = None,
    strict_llm: bool = False,
    target_hint: str | None = None,
    method_hint: str | None = None,
) -> EndpointDiscoveryResult:
    """Run LLM-guided endpoint extraction with fallback-safe behavior."""
    timeout_seconds: float | None = None
    files_sent_to_llm = len(candidates)
    truncated_files = sum(
        1
        for candidate in candidates
        if candidate.snippet is not None and candidate.snippet.truncated
    )
    if llm_client is None:
        settings = load_llm_settings_from_env()
        timeout_seconds = settings.timeout_seconds
        try:
            llm_client = create_default_llm_client(model_spec=model_spec)
        except LLMClientError as exc:
            if strict_llm:
                raise LLMClientError(classify_llm_error(str(exc))) from exc
            return EndpointDiscoveryResult(
                endpoints=[],
                notes=[f"LLM discovery unavailable: {exc}"],
                files_sent_to_llm=files_sent_to_llm,
                timeout_seconds=timeout_seconds,
                truncated_files=truncated_files,
            )
    else:
        timeout_seconds = getattr(llm_client, "timeout_seconds", None)

    prompt = build_endpoint_discovery_prompt(
        candidates,
        target_hint=target_hint,
        method_hint=method_hint,
    )
    prompt_chars = len(prompt)
    try:
        response = llm_client.generate(LLMRequest(prompt=prompt))
    except LLMClientError as exc:
        if strict_llm:
            raise LLMClientError(classify_llm_error(str(exc))) from exc
        return EndpointDiscoveryResult(
            endpoints=[],
            notes=[f"LLM discovery unavailable: {exc}"],
            files_sent_to_llm=files_sent_to_llm,
            prompt_chars=prompt_chars,
            timeout_seconds=timeout_seconds,
            truncated_files=truncated_files,
        )
    payload = _extract_json_payload(response.text)
    if payload is None:
        if strict_llm:
            raise LLMClientError(
                classify_llm_error("Model output was not valid JSON.")
            )
        return EndpointDiscoveryResult(
            endpoints=[],
            notes=["Model output was not valid JSON; returning empty endpoint set."],
            files_sent_to_llm=files_sent_to_llm,
            prompt_chars=prompt_chars,
            timeout_seconds=timeout_seconds,
            truncated_files=truncated_files,
        )

    raw_endpoints, shape_notes = _coerce_raw_endpoints(payload)
    endpoints, normalize_notes = _normalize_endpoints(raw_endpoints, candidates)
    filtered, filter_notes = _apply_quality_filters(endpoints)
    deduped = _dedupe_endpoints(filtered)
    notes: list[str] = []

    if isinstance(payload, dict):
        raw_notes = payload.get("notes")
        if isinstance(raw_notes, list):
            notes.extend(str(note) for note in raw_notes)
    notes.extend(shape_notes)
    notes.extend(normalize_notes)
    notes.extend(filter_notes)

    confidences = [item.confidence for item in deduped if item.confidence is not None]
    summary_confidence = None
    if confidences:
        summary_confidence = sum(confidences) / len(confidences)

    return EndpointDiscoveryResult(
        endpoints=deduped,
        notes=notes,
        confidence=summary_confidence,
        files_sent_to_llm=files_sent_to_llm,
        prompt_chars=prompt_chars,
        timeout_seconds=timeout_seconds,
        truncated_files=truncated_files,
    )


def discover_endpoints_from_candidates(
    candidates: list[CandidateFileRead],
    *,
    llm_client: LLMClient | None = None,
    model_spec: str | None = None,
    strict_llm: bool = False,
    target_hint: str | None = None,
    method_hint: str | None = None,
) -> list[EndpointCandidate]:
    """Discover endpoint candidates from bounded file reads."""
    result = run_llm_endpoint_discovery(
        candidates,
        llm_client=llm_client,
        model_spec=model_spec,
        strict_llm=strict_llm,
        target_hint=target_hint,
        method_hint=method_hint,
    )
    return result.endpoints


def discover_endpoints(
    repos: list[RepoRef],
    *,
    llm_client: LLMClient | None = None,
    model_spec: str | None = None,
    strict_llm: bool = False,
    inventory_max_files: int = 5000,
    rank_top_k: int = 80,
    read_top_n: int = 5,
) -> RoutesResult:
    """Run end-to-end shallow endpoint discovery across input repositories."""
    validated = validate_repo_roots(repos)
    endpoints: list[EndpointCandidate] = []
    notes: list[str] = []
    candidate_files = 0
    files_examined = 0
    files_sent_to_llm = 0
    total_prompt_chars = 0
    timeout_seconds: float | None = None
    truncated_files = 0

    files_to_llm_default = 5
    files_to_llm_raw = os.getenv("SYDES_DISCOVERY_FILES_TO_LLM", str(files_to_llm_default))
    try:
        files_to_llm = int(files_to_llm_raw)
    except ValueError:
        files_to_llm = files_to_llm_default
    if files_to_llm <= 0:
        files_to_llm = files_to_llm_default
    if files_to_llm > 10:
        files_to_llm = 10

    for repo in validated:
        inventory = build_repo_inventory(
            repo.name,
            repo.root,
            include_sizes=False,
            max_files=inventory_max_files,
        )
        sense = sense_repo(repo.name, repo.root, inventory)
        ranked = rank_candidate_files(inventory, sense, top_k=rank_top_k)
        reads = read_ranked_candidate_files_for_discovery(
            repo.name,
            repo.root,
            ranked,
            top_n=read_top_n,
        )
        llm_candidates = _select_route_discovery_llm_candidates(reads, files_to_llm)
        role_counts = Counter(item.role or "unknown" for item in llm_candidates)
        role_counts_text = ", ".join(
            f"{role}={count}" for role, count in sorted(role_counts.items())
        )

        candidate_files += len(ranked)
        files_examined += sum(1 for item in reads if not item.skipped and item.snippet is not None)
        files_sent_to_llm += len(llm_candidates)

        deterministic_routes, deterministic_frameworks = extract_deterministic_routes(reads)

        discovery = run_llm_endpoint_discovery(
            llm_candidates,
            llm_client=llm_client,
            model_spec=model_spec,
            strict_llm=strict_llm,
        )
        total_prompt_chars += discovery.prompt_chars
        truncated_files += discovery.truncated_files
        if timeout_seconds is None and discovery.timeout_seconds is not None:
            timeout_seconds = discovery.timeout_seconds

        notes.append(
            f"{repo.name}: candidate_files={len(ranked)}, "
            f"files_sent_to_llm={len(llm_candidates)}, "
            f"prompt_chars={discovery.prompt_chars}, "
            f"candidate_roles: {role_counts_text}"
        )
        notes.append(
            f"{repo.name}: deterministic_routes_found={len(deterministic_routes)}, "
            f"deterministic_frameworks={','.join(sorted(deterministic_frameworks)) if deterministic_frameworks else 'none'}"
        )
        selected_files_text = ", ".join(
            f"{item.relative_path}({item.role or 'unknown'})" for item in llm_candidates
        )
        if selected_files_text:
            notes.append(f"{repo.name}: selected_files: {selected_files_text}")
        elif any((item.role or "unknown") == FILE_ROLE_TEST_USAGE_CANDIDATE for item in reads):
            notes.append(
                f"{repo.name}: selected_files: none (test/docs-only candidates were not sent for route declaration discovery)"
            )
        endpoints.extend(deterministic_routes)
        endpoints.extend(discovery.endpoints)
        notes.append(f"{repo.name}: merged_llm_routes={len(discovery.endpoints)}")
        notes.extend([f"{repo.name}: {note}" for note in discovery.notes])

    deduped = _dedupe_endpoints(endpoints)
    confidences = [item.confidence for item in deduped if item.confidence is not None]
    confidence_summary = None
    if confidences:
        confidence_summary = ConfidenceSummary(
            average=sum(confidences) / len(confidences),
            minimum=min(confidences),
            maximum=max(confidences),
        )

    return RoutesResult(
        repos=validated,
        routes=deduped,
        candidate_files=candidate_files,
        files_examined=files_examined,
        files_sent_to_llm=files_sent_to_llm,
        prompt_chars=total_prompt_chars,
        timeout_seconds=timeout_seconds,
        truncated_files=truncated_files,
        notes=notes,
        confidence_summary=confidence_summary,
    )


def _select_route_discovery_llm_candidates(
    reads: list[CandidateFileRead],
    files_to_llm: int,
) -> list[CandidateFileRead]:
    """Select role-aware candidates for route declaration discovery prompts."""
    if files_to_llm <= 0:
        return []

    def _role(item: CandidateFileRead) -> str:
        return item.role or "unknown"

    source = [item for item in reads if _role(item) == FILE_ROLE_SOURCE_ROUTE_CANDIDATE]
    unknown = [item for item in reads if _role(item) == "unknown"]
    test_or_docs = [
        item
        for item in reads
        if _role(item) in {FILE_ROLE_TEST_USAGE_CANDIDATE, FILE_ROLE_DOCS_CANDIDATE}
    ]

    if source:
        return (source + unknown)[:files_to_llm]
    if unknown:
        return unknown[:files_to_llm]
    if test_or_docs:
        return []
    return reads[:files_to_llm]
