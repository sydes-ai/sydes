"""Bounded context preparation helpers for downstream flow expansion."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from sydes.core.models import (
    EndpointCandidate,
    EvidenceRef,
    ExpansionContextFile,
    FlowExpansionResult,
    FlowExpansionContext,
    RepoRef,
    SinkCandidate,
    TraceStep,
)
from sydes.ingest.inventory import build_repo_inventory
from sydes.ingest.readers import read_text_file_for_flow_expansion
from sydes.llm.client import LLMClient, LLMClientError, LLMRequest, create_default_llm_client
from sydes.llm.client import load_llm_settings_from_env

DEFAULT_RELATED_FILE_LIMIT = 4
DEFAULT_INVENTORY_MAX_FILES = 8_000
RELATED_FILE_KEYWORDS = {
    "service",
    "services",
    "client",
    "clients",
    "db",
    "database",
    "model",
    "models",
    "repository",
    "repositories",
    "repo",
    "dao",
    "store",
    "query",
    "queries",
}
KNOWN_STEP_KINDS = {"endpoint", "handler", "service_call", "db_read", "db_write", "external_api_call", "queue_publish", "queue_consume", "file_write", "validation", "auth", "transform", "unknown"}
KNOWN_SINK_KINDS = {"database", "external_api", "queue", "file", "unknown"}


def _repo_root_map(repos: list[RepoRef]) -> dict[str, str]:
    """Map repo name to normalized root path."""
    return {repo.name: repo.root for repo in repos}


def _tokenize_symbol(value: str | None) -> set[str]:
    """Extract conservative symbol tokens from a handler or evidence symbol."""
    if not value:
        return set()
    tokens = {part.lower() for part in re.split(r"[^A-Za-z0-9_]+", value) if part}
    return {token for token in tokens if len(token) >= 3}


def _collect_symbol_tokens(endpoint: EndpointCandidate) -> set[str]:
    """Collect symbol tokens from endpoint handler and evidence symbols."""
    tokens = _tokenize_symbol(endpoint.handler)
    for ref in endpoint.evidence:
        tokens.update(_tokenize_symbol(ref.symbol))
    return tokens


def _score_related_file(
    path: str,
    *,
    anchor_parts: tuple[str, ...],
    anchor_dir: str,
    anchor_suffix: str,
    anchor_stem: str,
    symbol_tokens: set[str],
) -> tuple[float, list[str]]:
    """Score one file as a nearby candidate for selective expansion context."""
    score = 0.0
    reasons: list[str] = []
    candidate = Path(path)
    candidate_parts = tuple(part.lower() for part in candidate.parts)
    candidate_name = candidate.name.lower()
    candidate_stem = candidate.stem.lower()
    candidate_dir = candidate.parent.as_posix().lower()

    if candidate_dir == anchor_dir:
        score += 3.0
        reasons.append("same_directory")

    if candidate.suffix.lower() == anchor_suffix:
        score += 0.7
        reasons.append("same_extension")

    if candidate_parts and anchor_parts and candidate_parts[0] == anchor_parts[0]:
        score += 0.6
        reasons.append("same_top_level_dir")

    keyword_hits = [token for token in RELATED_FILE_KEYWORDS if token in candidate_parts or token in candidate_name]
    if keyword_hits:
        score += min(2.0, 0.9 + 0.3 * len(keyword_hits))
        reasons.append("related_filename_keyword")

    if anchor_stem and anchor_stem in candidate_name and candidate_stem != anchor_stem:
        score += 0.7
        reasons.append("name_matches_anchor")

    symbol_hits = [token for token in symbol_tokens if token in candidate_name or token in candidate_stem]
    if symbol_hits:
        score += min(2.4, 1.0 + 0.4 * len(symbol_hits))
        reasons.append("name_matches_symbol")

    return score, reasons


def _select_related_files(
    endpoint: EndpointCandidate,
    repo_root: str,
    *,
    max_related_files: int,
    inventory_max_files: int,
) -> list[tuple[str, list[str]]]:
    """Select a bounded set of files near the anchor endpoint file."""
    inventory = build_repo_inventory(
        repo_name=endpoint.repo,
        repo_root=repo_root,
        include_sizes=False,
        max_files=inventory_max_files,
    )
    anchor_path = endpoint.file
    anchor = Path(anchor_path)
    anchor_parts = tuple(part.lower() for part in anchor.parts)
    anchor_dir = anchor.parent.as_posix().lower()
    anchor_suffix = anchor.suffix.lower()
    anchor_stem = anchor.stem.lower()
    symbol_tokens = _collect_symbol_tokens(endpoint)

    scored: list[tuple[float, str, list[str]]] = []
    for item in inventory.files:
        file_path = item.path
        if file_path == anchor_path:
            continue
        score, reasons = _score_related_file(
            file_path,
            anchor_parts=anchor_parts,
            anchor_dir=anchor_dir,
            anchor_suffix=anchor_suffix,
            anchor_stem=anchor_stem,
            symbol_tokens=symbol_tokens,
        )
        if score <= 0:
            continue
        scored.append((score, file_path, sorted(set(reasons))))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(path, reasons) for _, path, reasons in scored[:max_related_files]]


def _build_context_file(repo: str, path: str, reasons: list[str], repo_root: str) -> ExpansionContextFile:
    """Create one expansion context file entry with bounded read metadata."""
    read_result = read_text_file_for_flow_expansion(repo=repo, repo_root=repo_root, relative_path=path)
    truncated = read_result.snippet.truncated if read_result.snippet is not None else None
    return ExpansionContextFile(
        repo=repo,
        file=path,
        selection_reasons=reasons,
        read=read_result,
        truncated=truncated,
    )


def prepare_flow_expansion_context(
    matched_endpoint: EndpointCandidate,
    repos: list[RepoRef],
    *,
    max_related_files: int = DEFAULT_RELATED_FILE_LIMIT,
    inventory_max_files: int = DEFAULT_INVENTORY_MAX_FILES,
) -> FlowExpansionContext:
    """Prepare bounded contextual files anchored on a matched endpoint file."""
    root_by_repo = _repo_root_map(repos)
    repo_root = root_by_repo.get(matched_endpoint.repo)
    if repo_root is None:
        return FlowExpansionContext(
            anchor_repo=matched_endpoint.repo,
            anchor_file=matched_endpoint.file,
            notes=[f"Repo root for '{matched_endpoint.repo}' was not provided."],
        )

    files: list[ExpansionContextFile] = []
    notes: list[str] = []

    files.append(
        _build_context_file(
            repo=matched_endpoint.repo,
            path=matched_endpoint.file,
            reasons=["anchor_endpoint_file"],
            repo_root=repo_root,
        )
    )

    related = _select_related_files(
        matched_endpoint,
        repo_root,
        max_related_files=max_related_files,
        inventory_max_files=inventory_max_files,
    )
    for related_path, reasons in related:
        files.append(
            _build_context_file(
                repo=matched_endpoint.repo,
                path=related_path,
                reasons=reasons,
                repo_root=repo_root,
            )
        )

    notes.append(f"Selected {len(files)} contextual files for flow expansion.")
    if related:
        notes.append(f"Included {len(related)} nearby files beyond the anchor endpoint file.")
    else:
        notes.append("No nearby related files were selected beyond the anchor endpoint file.")

    skipped = [entry for entry in files if entry.read is not None and entry.read.skipped]
    if skipped:
        notes.append(f"{len(skipped)} contextual file reads were skipped due to reader safety checks.")

    truncated_count = sum(1 for entry in files if entry.truncated)
    if truncated_count:
        notes.append(f"{truncated_count} contextual files were truncated by bounded read caps.")

    return FlowExpansionContext(
        anchor_repo=matched_endpoint.repo,
        anchor_file=matched_endpoint.file,
        files=files,
        notes=notes,
    )


def build_flow_expansion_prompt_from_context(
    matched_endpoint: EndpointCandidate,
    context: FlowExpansionContext,
) -> str:
    """Build the LLM prompt text for downstream flow expansion from prepared context."""
    from sydes.llm.prompts import build_flow_expansion_prompt

    return build_flow_expansion_prompt(
        matched_endpoint=matched_endpoint,
        context=context,
    )


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences around potentially JSON model output."""
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
    """Best-effort parse for JSON object/list from possibly messy LLM output."""
    content = _strip_markdown_fences(text).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    brace_start = content.find("{")
    brace_end = content.rfind("}")
    list_start = content.find("[")
    list_end = content.rfind("]")
    candidates: list[tuple[int, int]] = []
    if brace_start >= 0 and brace_end > brace_start:
        candidates.append((brace_start, brace_end + 1))
    if list_start >= 0 and list_end > list_start:
        candidates.append((list_start, list_end + 1))
    candidates.sort(key=lambda item: item[0])
    for start, end in candidates:
        try:
            return json.loads(content[start:end])
        except json.JSONDecodeError:
            continue
    return None


def _normalize_status(value: Any) -> str:
    """Normalize status to a soft inferred/unknown-style value."""
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "inferred"


def _normalize_confidence(value: Any) -> float | None:
    """Normalize confidence into a bounded float when possible."""
    if not isinstance(value, (int, float)):
        return None
    score = float(value)
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return score


def _normalize_symbol(value: Any) -> str | None:
    """Normalize symbol/handler values while keeping them soft."""
    if not isinstance(value, str):
        return None
    symbol = value.strip()
    if not symbol:
        return None
    return symbol.strip(" ,;:()[]{}") or None


def _normalize_file_path(value: Any) -> str | None:
    """Normalize file path values into relative posix-style paths."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    return Path(candidate.replace("\\", "/")).as_posix()


def _normalize_evidence(value: Any, fallback_file: str | None) -> list[EvidenceRef]:
    """Normalize loose evidence structures into EvidenceRef entries."""
    entries: list[EvidenceRef] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            file_value = _normalize_file_path(item.get("file")) or fallback_file
            if not file_value:
                continue
            symbol = _normalize_symbol(item.get("symbol"))
            label = item.get("label") if isinstance(item.get("label"), str) and item.get("label").strip() else None
            entries.append(EvidenceRef(file=file_value, symbol=symbol, label=label))
    if not entries and fallback_file:
        entries.append(EvidenceRef(file=fallback_file, label="inferred-from-context"))
    return entries


def _normalize_step_kind(value: Any) -> str:
    """Normalize step kind into a compact, predictable token."""
    if isinstance(value, str) and value.strip():
        raw = value.strip().lower().replace(" ", "_").replace("-", "_")
    else:
        raw = "unknown"
    if raw in KNOWN_STEP_KINDS:
        return raw
    if "db" in raw or "database" in raw:
        return "db_read" if "read" in raw else "db_write" if "write" in raw else "unknown"
    if "queue" in raw:
        return "queue_publish" if "publish" in raw else "queue_consume" if "consume" in raw else "unknown"
    if "external" in raw or "http" in raw or "api" in raw:
        return "external_api_call"
    if "file" in raw and ("write" in raw or "save" in raw):
        return "file_write"
    if "handler" in raw:
        return "handler"
    if "service" in raw:
        return "service_call"
    return "unknown"


def _normalize_sink_kind(value: Any) -> str:
    """Normalize sink kind labels into supported sink families."""
    if isinstance(value, str) and value.strip():
        raw = value.strip().lower().replace(" ", "_").replace("-", "_")
    else:
        raw = "unknown"
    if raw in KNOWN_SINK_KINDS:
        return raw
    if "db" in raw or "database" in raw or "sql" in raw:
        return "database"
    if "queue" in raw or "kafka" in raw or "rabbit" in raw or "pubsub" in raw:
        return "queue"
    if "file" in raw or "storage" in raw or "s3" in raw:
        return "file"
    if "api" in raw or "http" in raw or "webhook" in raw or "external" in raw:
        return "external_api"
    return "unknown"


def _coerce_items(payload: Any, key: str) -> list[Any]:
    """Extract list payload by key or treat top-level list as step list."""
    if isinstance(payload, dict):
        value = payload.get(key)
        return value if isinstance(value, list) else []
    if isinstance(payload, list) and key == "steps":
        return payload
    return []


def _normalize_steps(raw_steps: list[Any], fallback_repo: str) -> tuple[list[TraceStep], list[str]]:
    """Normalize loose model step items into soft TraceStep objects."""
    steps: list[TraceStep] = []
    notes: list[str] = []
    for idx, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            notes.append(f"Ignored flow step #{idx}: not an object.")
            continue
        name_raw = item.get("name")
        symbol = _normalize_symbol(item.get("symbol"))
        if isinstance(name_raw, str) and name_raw.strip():
            name = name_raw.strip()
        elif symbol:
            name = symbol
        else:
            notes.append(f"Ignored flow step #{idx}: missing name/symbol.")
            continue

        file_value = _normalize_file_path(item.get("file"))
        repo_value = item.get("repo") if isinstance(item.get("repo"), str) and item.get("repo").strip() else fallback_repo
        service = item.get("service") if isinstance(item.get("service"), str) and item.get("service").strip() else None
        evidence = _normalize_evidence(item.get("evidence"), fallback_file=file_value)

        steps.append(
            TraceStep(
                kind=_normalize_step_kind(item.get("kind")),
                name=name,
                repo=repo_value,
                service=service,
                file=file_value,
                symbol=symbol,
                evidence=evidence,
                confidence=_normalize_confidence(item.get("confidence")),
                status=_normalize_status(item.get("status")),
            )
        )
    return steps, notes


def _normalize_sinks(raw_sinks: list[Any], fallback_repo: str) -> tuple[list[SinkCandidate], list[str]]:
    """Normalize loose model sink items into soft SinkCandidate objects."""
    sinks: list[SinkCandidate] = []
    notes: list[str] = []
    for idx, item in enumerate(raw_sinks, start=1):
        if not isinstance(item, dict):
            notes.append(f"Ignored sink #{idx}: not an object.")
            continue
        name_raw = item.get("name")
        symbol = _normalize_symbol(item.get("symbol"))
        if isinstance(name_raw, str) and name_raw.strip():
            name = name_raw.strip()
        elif symbol:
            name = symbol
        else:
            notes.append(f"Ignored sink #{idx}: missing name/symbol.")
            continue

        file_value = _normalize_file_path(item.get("file"))
        repo_value = item.get("repo") if isinstance(item.get("repo"), str) and item.get("repo").strip() else fallback_repo
        service = item.get("service") if isinstance(item.get("service"), str) and item.get("service").strip() else None
        action = item.get("action") if isinstance(item.get("action"), str) and item.get("action").strip() else None
        evidence = _normalize_evidence(item.get("evidence"), fallback_file=file_value)

        sinks.append(
            SinkCandidate(
                kind=_normalize_sink_kind(item.get("kind")),
                name=name,
                repo=repo_value,
                service=service,
                file=file_value,
                symbol=symbol,
                action=action,
                evidence=evidence,
                confidence=_normalize_confidence(item.get("confidence")),
                status=_normalize_status(item.get("status")),
            )
        )
    return sinks, notes


def _parse_flow_expansion_payload(payload: Any, fallback_repo: str) -> tuple[FlowExpansionResult, list[str]]:
    """Parse and normalize model payload into FlowExpansionResult."""
    notes: list[str] = []
    raw_steps = _coerce_items(payload, "steps")
    raw_sinks = _coerce_items(payload, "sinks")

    steps, step_notes = _normalize_steps(raw_steps, fallback_repo)
    sinks, sink_notes = _normalize_sinks(raw_sinks, fallback_repo)
    notes.extend(step_notes)
    notes.extend(sink_notes)

    result_notes: list[str] = []
    result_confidence: float | None = None
    if isinstance(payload, dict):
        if isinstance(payload.get("notes"), list):
            result_notes = [str(item) for item in payload["notes"]]
        result_confidence = _normalize_confidence(payload.get("confidence"))
    notes.extend(result_notes)

    if result_confidence is None:
        confidences = [item.confidence for item in [*steps, *sinks] if item.confidence is not None]
        if confidences:
            result_confidence = sum(confidences) / len(confidences)

    return (
        FlowExpansionResult(
            steps=steps,
            sinks=sinks,
            notes=notes,
            confidence=result_confidence,
        ),
        notes,
    )


def run_flow_expansion(
    matched_endpoint: EndpointCandidate,
    repos: list[RepoRef],
    *,
    llm_client: LLMClient | None = None,
) -> FlowExpansionResult:
    """Run bounded context + LLM flow expansion with graceful fallback behavior."""
    related_default = 4
    related_raw = os.getenv("SYDES_FLOW_EXPANSION_FILES", str(related_default)).strip()
    try:
        max_related_files = int(related_raw)
    except ValueError:
        max_related_files = related_default
    max_related_files = max(0, min(max_related_files, 8))

    context = prepare_flow_expansion_context(
        matched_endpoint=matched_endpoint,
        repos=repos,
        max_related_files=max_related_files,
    )
    notes = list(context.notes)
    files_selected = len(context.files)
    files_examined = sum(1 for item in context.files if item.read is not None and not item.read.skipped)
    notes.append(f"Flow expansion context files selected: {files_selected} (examined={files_examined}).")

    timeout_seconds: float | None = None
    if llm_client is None:
        settings = load_llm_settings_from_env()
        timeout_seconds = settings.timeout_seconds
        try:
            llm_client = create_default_llm_client()
        except LLMClientError as exc:
            notes.append(f"Flow expansion unavailable: {exc}")
            return FlowExpansionResult(
                entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
                notes=notes,
            )
    else:
        timeout_seconds = getattr(llm_client, "timeout_seconds", None)

    prompt = build_flow_expansion_prompt_from_context(matched_endpoint, context)
    notes.append(f"Flow expansion prompt chars: {len(prompt)}.")
    if timeout_seconds is not None:
        notes.append(f"Flow expansion timeout: {timeout_seconds:.0f}s.")

    try:
        response = llm_client.generate(LLMRequest(prompt=prompt))
    except LLMClientError as exc:
        notes.append(f"Flow expansion unavailable: {exc}")
        return FlowExpansionResult(
            entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
            notes=notes,
        )

    payload = _extract_json_payload(response.text)
    if payload is None:
        notes.append("Flow expansion output was not valid JSON.")
        return FlowExpansionResult(
            entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
            notes=notes,
        )

    normalized, normalize_notes = _parse_flow_expansion_payload(payload, matched_endpoint.repo)
    merged_notes = notes + [note for note in normalize_notes if note not in notes]
    return FlowExpansionResult(
        entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
        steps=normalized.steps,
        sinks=normalized.sinks,
        notes=merged_notes,
        confidence=normalized.confidence,
    )
