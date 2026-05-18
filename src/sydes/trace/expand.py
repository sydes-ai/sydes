"""Bounded context preparation helpers for downstream flow expansion."""

from __future__ import annotations

import ast
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
from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    classify_llm_error,
    create_default_llm_client,
)
from sydes.llm.client import load_llm_settings_from_env
from sydes.trace.sinks import (
    derive_sink_candidates_from_steps,
    merge_and_dedupe_sinks,
    normalize_sink_candidate,
)

DEFAULT_RELATED_FILE_LIMIT = 1
DEFAULT_INVENTORY_MAX_FILES = 8_000
STRONG_RELATED_SCORE_THRESHOLD = 4.2
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
KNOWN_STEP_KINDS = {
    "endpoint",
    "handler",
    "dependency",
    "input_model",
    "service_call",
    "db_read",
    "db_write",
    "external_api_call",
    "queue_publish",
    "queue_consume",
    "file_write",
    "validation",
    "auth",
    "transform",
    "unknown",
}
SUSPICIOUS_ABSTRACT_STEPS = {
    "call payment client",
    "call service",
    "call external api",
    "invoke client",
    "process request",
    "handle request",
}
GENERIC_ABSTRACT_TOKENS = {"call", "invoke", "process", "handle", "request", "service", "client", "external", "api"}
PYTHON_DB_READ_RE = re.compile(
    r"\bdb\.query\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)(?:\.[A-Za-z_][A-Za-z0-9_]*\([^)]*\))*\.(all|first)\(\)",
    re.IGNORECASE,
)
PYTHON_DB_WRITE_RE = re.compile(
    r"\bdb\.(add|commit|refresh|delete)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
PYTHON_EXTERNAL_CALL_RE = re.compile(
    r"\b(requests|httpx)\.(get|post|put|delete|patch)\s*\(",
    re.IGNORECASE,
)


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
    strong: list[tuple[str, list[str]]] = []
    for score, path, reasons in scored:
        if score < STRONG_RELATED_SCORE_THRESHOLD:
            continue
        if "name_matches_symbol" not in reasons and "related_filename_keyword" not in reasons:
            continue
        strong.append((path, reasons))
        if len(strong) >= max_related_files:
            break
    return strong


def _anchor_appears_sufficient(anchor: ExpansionContextFile) -> bool:
    """Heuristic check for whether anchor content alone is likely enough."""
    if anchor.read is None or anchor.read.skipped or anchor.read.snippet is None:
        return False
    if anchor.read.snippet.truncated:
        return False
    text = anchor.read.snippet.text.lower()
    signal_groups = [
        ("router.", "@app.", "route(", "post(", "get(", "put(", "delete("),
        ("db.", ".commit(", ".add(", ".save(", "insert", "update", "delete"),
        ("client.", "requests.", "httpx.", "fetch(", "queue", "publish", "enqueue"),
    ]
    matched_groups = sum(1 for group in signal_groups if any(signal in text for signal in group))
    return matched_groups >= 2


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


def _unparse_expression(node: ast.AST, source: str) -> str:
    """Render a compact source-like expression for deterministic evidence extraction."""
    segment = ast.get_source_segment(source, node)
    if isinstance(segment, str) and segment.strip():
        return " ".join(segment.strip().split())
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _extract_depends_symbol(default_node: ast.AST) -> str | None:
    """Extract dependency symbol from Depends(...) argument default."""
    if not isinstance(default_node, ast.Call):
        return None
    if isinstance(default_node.func, ast.Name):
        func_name = default_node.func.id
    elif isinstance(default_node.func, ast.Attribute):
        func_name = default_node.func.attr
    else:
        return None
    if func_name != "Depends":
        return None
    if not default_node.args:
        return "Depends"
    first = default_node.args[0]
    if isinstance(first, ast.Name):
        return first.id
    if isinstance(first, ast.Attribute):
        return first.attr
    return _unparse_expression(first, "")


def _iter_function_defaults(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Collect positional and keyword defaults from a function signature."""
    defaults = list(function_node.args.defaults)
    defaults.extend(item for item in function_node.args.kw_defaults if item is not None)
    return defaults


def _safe_annotation_text(arg: ast.arg, source: str) -> str | None:
    """Return annotation text for one function arg when available."""
    if arg.annotation is None:
        return None
    annotation = _unparse_expression(arg.annotation, source)
    return annotation or None


def _deterministic_step_key(step: TraceStep) -> tuple[str, str, str, str, str]:
    """Build a stable key for deterministic/LLM step merge deduplication."""
    return (
        (step.repo or "").strip().lower(),
        (step.file or "").strip().lower(),
        (step.symbol or "").strip().lower(),
        (step.kind or "").strip().lower(),
        " ".join((step.name or "").strip().lower().split()),
    )


def _merge_steps(
    baseline_steps: list[TraceStep],
    llm_steps: list[TraceStep],
) -> list[TraceStep]:
    """Merge baseline + LLM steps while preserving baseline ordering/content."""
    merged: list[TraceStep] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for step in baseline_steps + llm_steps:
        key = _deterministic_step_key(step)
        if key in seen:
            continue
        seen.add(key)
        merged.append(step)
    return merged


def _extract_python_handler_baseline(
    matched_endpoint: EndpointCandidate,
    context: FlowExpansionContext,
) -> tuple[list[TraceStep], list[SinkCandidate], list[str]]:
    """Extract deterministic flow evidence from Python handler code in the anchor file."""
    notes: list[str] = []
    anchor = next(
        (
            item
            for item in context.files
            if item.file == context.anchor_file
        ),
        None,
    )
    if anchor is None or anchor.read is None or anchor.read.skipped or anchor.read.snippet is None:
        return [], [], notes

    source = anchor.read.snippet.text
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], [], notes

    handler_name = (matched_endpoint.handler or "").strip()
    if not handler_name:
        return [], [], notes

    handler_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == handler_name:
            handler_node = node
            break
    if handler_node is None:
        return [], [], notes

    repo = matched_endpoint.repo
    file_path = matched_endpoint.file
    steps: list[TraceStep] = []
    sinks: list[SinkCandidate] = []

    def add_step(kind: str, name: str, label: str) -> None:
        steps.append(
            TraceStep(
                kind=kind,
                name=name,
                repo=repo,
                file=file_path,
                symbol=handler_name,
                status="grounded",
                confidence=0.95,
                evidence=[EvidenceRef(file=file_path, symbol=handler_name, label=label)],
            )
        )

    # Dependency extraction from Depends(...) defaults
    for default in _iter_function_defaults(handler_node):
        depends_symbol = _extract_depends_symbol(default)
        if depends_symbol:
            add_step(
                "dependency",
                f"Depends({depends_symbol})",
                f"deterministic:dependency:{depends_symbol}",
            )

    # Input model extraction for likely write handlers
    method = (matched_endpoint.method or "").upper()
    if method in {"POST", "PUT", "PATCH"}:
        for arg in handler_node.args.args:
            if arg.arg == "self":
                continue
            annotation = _safe_annotation_text(arg, source)
            if not annotation:
                continue
            annotation_lower = annotation.lower()
            if "session" in annotation_lower:
                continue
            add_step(
                "input_model",
                f"input model: {annotation}",
                f"deterministic:input_model:{annotation}",
            )
            break

    found_db_read = False
    found_db_write = False
    found_external = False
    found_return = False

    # Object creation hints from assignment calls like: db_user = User(...)
    for node in handler_node.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id and call.func.id[:1].isupper():
                add_step(
                    "transform",
                    f"create {call.func.id} object",
                    f"deterministic:object_creation:{call.func.id}",
                )

    for node in ast.walk(handler_node):
        if isinstance(node, ast.Return):
            found_return = True
            continue
        if not isinstance(node, ast.Call):
            continue

        expression = _unparse_expression(node, source)
        if not expression:
            continue
        expression = " ".join(expression.split())

        read_match = PYTHON_DB_READ_RE.search(expression)
        if read_match:
            entity = read_match.group(1)
            add_step("db_read", expression, f"deterministic:db_read:{expression}")
            sinks.append(
                SinkCandidate(
                    kind="database",
                    name=entity or "database",
                    repo=repo,
                    file=file_path,
                    symbol=handler_name,
                    action="read",
                    status="grounded",
                    confidence=0.95,
                    evidence=[
                        EvidenceRef(
                            file=file_path,
                            symbol=handler_name,
                            label=f"deterministic:db_read:{expression}",
                        )
                    ],
                )
            )
            found_db_read = True

        write_match = PYTHON_DB_WRITE_RE.search(expression)
        if write_match:
            action = write_match.group(1).lower()
            add_step("db_write", expression, f"deterministic:db_write:{expression}")
            sinks.append(
                SinkCandidate(
                    kind="database",
                    name="database",
                    repo=repo,
                    file=file_path,
                    symbol=handler_name,
                    action="write",
                    status="grounded",
                    confidence=0.95,
                    evidence=[
                        EvidenceRef(
                            file=file_path,
                            symbol=handler_name,
                            label=f"deterministic:db_write:{expression}",
                        )
                    ],
                )
            )
            found_db_write = True
            if action == "refresh":
                add_step("transform", "return refreshed entity", "deterministic:return_refreshed_entity")

        external_match = PYTHON_EXTERNAL_CALL_RE.search(expression)
        if external_match:
            method_label = external_match.group(2).upper()
            add_step("external_api_call", expression, f"deterministic:external_call:{method_label}")
            sinks.append(
                SinkCandidate(
                    kind="external_api",
                    name=expression,
                    repo=repo,
                    file=file_path,
                    symbol=handler_name,
                    action="read",
                    status="grounded",
                    confidence=0.9,
                    evidence=[
                        EvidenceRef(
                            file=file_path,
                            symbol=handler_name,
                            label=f"deterministic:external_call:{expression}",
                        )
                    ],
                )
            )
            found_external = True

    if found_return:
        add_step("transform", "return response", "deterministic:return_response")

    if steps:
        notes.append(
            f"Deterministic anchor extraction added {len(steps)} step(s)"
            f" and {len(sinks)} sink candidate(s)."
        )
    else:
        if any(isinstance(node, ast.Return) for node in ast.walk(handler_node)):
            notes.append("Deterministic anchor extraction found handler return path but no concrete sink operations.")

    if found_db_read:
        notes.append("Deterministic evidence captured database read operations from handler body.")
    if found_db_write:
        notes.append("Deterministic evidence captured database write operations from handler body.")
    if found_external:
        notes.append("Deterministic evidence captured outbound HTTP calls from handler body.")

    return steps, sinks, notes


def prepare_flow_expansion_context(
    matched_endpoint: EndpointCandidate,
    repos: list[RepoRef],
    *,
    max_related_files: int = DEFAULT_RELATED_FILE_LIMIT,
    inventory_max_files: int = DEFAULT_INVENTORY_MAX_FILES,
) -> FlowExpansionContext:
    """Prepare bounded contextual files anchored on a matched endpoint file."""
    max_related_files = max(0, min(max_related_files, 2))
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

    anchor_file = _build_context_file(
        repo=matched_endpoint.repo,
        path=matched_endpoint.file,
        reasons=["anchor_endpoint_file"],
        repo_root=repo_root,
    )
    files.append(anchor_file)
    notes.append(f"Using anchor file: {matched_endpoint.file}.")
    if _anchor_appears_sufficient(anchor_file):
        notes.append("Anchor file appears sufficient; no extra contextual files added.")
        related: list[tuple[str, list[str]]] = []
    else:
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
        notes.append(f"Included {len(related)} extra contextual files beyond the anchor endpoint file.")
        for related_path, reasons in related:
            notes.append(f"Included {related_path} because: {', '.join(reasons)}.")
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


def _normalize_step_name(value: Any) -> str | None:
    """Normalize a step name while preserving concise operation-like phrases."""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    if not normalized:
        return None
    normalized = normalized.strip(" ,;:")
    return normalized or None


def _normalize_literal_text(value: str) -> str:
    """Normalize free-form text into token-friendly lowercase content."""
    lowered = value.lower().replace("_", " ")
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return " ".join(lowered.split())


def _looks_like_concrete_code_operation(step_name: str) -> bool:
    """Return true for concrete operation-like steps that should be retained."""
    normalized = _normalize_literal_text(step_name)
    if not normalized:
        return False
    raw = step_name.strip().lower()
    if "." in raw:
        return True
    if "(" in raw and ")" in raw:
        return True
    concrete_prefixes = (
        "create ",
        "return ",
        "query ",
        "insert ",
        "update ",
        "delete ",
        "commit ",
        "refresh ",
        "validate ",
    )
    return normalized.startswith(concrete_prefixes)


def _is_suspicious_abstract_step(step_name: str) -> bool:
    """Return true for narrow, known generic abstraction phrases."""
    return _normalize_literal_text(step_name) in SUSPICIOUS_ABSTRACT_STEPS


def _is_supported_literal_step(
    step_name: str,
    *,
    context_raw_lower: str,
    context_literal_blob: str,
) -> bool:
    """Return true when a step phrase is supported by literal context text."""
    if not context_raw_lower and not context_literal_blob:
        return False
    normalized = _normalize_literal_text(step_name)
    if not normalized:
        return False
    if normalized in context_literal_blob:
        return True
    underscored = normalized.replace(" ", "_")
    if underscored and underscored in context_raw_lower:
        return True
    content_tokens = [
        token for token in normalized.split() if len(token) >= 4 and token not in GENERIC_ABSTRACT_TOKENS
    ]
    if not content_tokens:
        return False
    return all(token in context_literal_blob for token in content_tokens)


def _derive_step_name(raw: dict[str, Any], symbol: str | None) -> str | None:
    """Derive a useful step name from available soft fields."""
    candidate_fields = ("name", "operation", "description", "label", "action")
    for field in candidate_fields:
        name = _normalize_step_name(raw.get(field))
        if name:
            return name
    return symbol


def _coerce_items(payload: Any, key: str) -> list[Any]:
    """Extract list payload by key or treat top-level list as step list."""
    if isinstance(payload, dict):
        value = payload.get(key)
        return value if isinstance(value, list) else []
    if isinstance(payload, list) and key == "steps":
        return payload
    return []


def _normalize_steps(
    raw_steps: list[Any],
    fallback_repo: str,
    *,
    context_raw_lower: str = "",
    context_literal_blob: str = "",
) -> tuple[list[TraceStep], list[str]]:
    """Normalize loose model step items into soft TraceStep objects."""
    steps: list[TraceStep] = []
    notes: list[str] = []
    for idx, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            notes.append(f"Ignored flow step #{idx}: not an object.")
            continue
        symbol = _normalize_symbol(item.get("symbol"))
        name = _derive_step_name(item, symbol)
        if name is None:
            notes.append(f"Ignored flow step #{idx}: missing meaningful content.")
            continue
        if (
            _is_suspicious_abstract_step(name)
            and not _looks_like_concrete_code_operation(name)
            and not _is_supported_literal_step(
                name,
                context_raw_lower=context_raw_lower,
                context_literal_blob=context_literal_blob,
            )
        ):
            notes.append(f"Dropped suspicious abstract step #{idx}: {name}.")
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
            normalize_sink_candidate(
                SinkCandidate(
                    kind=item.get("kind") if isinstance(item.get("kind"), str) and item.get("kind").strip() else "unknown",
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
        )
    return sinks, notes


def _parse_flow_expansion_payload(
    payload: Any,
    fallback_repo: str,
    *,
    context_raw_lower: str = "",
    context_literal_blob: str = "",
) -> tuple[FlowExpansionResult, list[str]]:
    """Parse and normalize model payload into FlowExpansionResult."""
    notes: list[str] = []
    raw_steps = _coerce_items(payload, "steps")
    raw_sinks = _coerce_items(payload, "sinks")

    steps, step_notes = _normalize_steps(
        raw_steps,
        fallback_repo,
        context_raw_lower=context_raw_lower,
        context_literal_blob=context_literal_blob,
    )
    explicit_sinks, sink_notes = _normalize_sinks(raw_sinks, fallback_repo)
    derived_sinks = derive_sink_candidates_from_steps(steps)
    sinks = merge_and_dedupe_sinks(explicit_sinks, derived_sinks)
    notes.extend(step_notes)
    notes.extend(sink_notes)
    if derived_sinks:
        notes.append(f"Derived {len(derived_sinks)} sink candidate(s) from retained flow steps.")
    if len(sinks) != len(explicit_sinks):
        notes.append(
            f"Sink merge result: explicit={len(explicit_sinks)}, "
            f"derived={len(derived_sinks)}, final={len(sinks)}."
        )

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
    model_spec: str | None = None,
    strict_llm: bool = False,
) -> FlowExpansionResult:
    """Run bounded context + LLM flow expansion with graceful fallback behavior."""
    related_default = 1
    related_raw = os.getenv("SYDES_FLOW_EXPANSION_FILES", str(related_default)).strip()
    try:
        max_related_files = int(related_raw)
    except ValueError:
        max_related_files = related_default
    max_related_files = max(0, min(max_related_files, 2))

    context = prepare_flow_expansion_context(
        matched_endpoint=matched_endpoint,
        repos=repos,
        max_related_files=max_related_files,
    )
    notes = list(context.notes)
    files_selected = len(context.files)
    files_examined = sum(1 for item in context.files if item.read is not None and not item.read.skipped)
    notes.append(f"Flow expansion context files selected: {files_selected} (examined={files_examined}).")
    context_raw_lower_parts: list[str] = []
    context_literal_parts: list[str] = []
    for file_item in context.files:
        if file_item.read is None or file_item.read.skipped or file_item.read.snippet is None:
            continue
        snippet_text = file_item.read.snippet.text
        context_raw_lower_parts.append(snippet_text.lower())
        context_literal_parts.append(_normalize_literal_text(snippet_text))
    context_raw_lower = "\n".join(context_raw_lower_parts)
    context_literal_blob = " ".join(context_literal_parts)
    baseline_steps, baseline_sinks, baseline_notes = _extract_python_handler_baseline(
        matched_endpoint,
        context,
    )
    notes.extend(baseline_notes)

    timeout_seconds: float | None = None
    if llm_client is None:
        settings = load_llm_settings_from_env()
        timeout_seconds = settings.timeout_seconds
        try:
            llm_client = create_default_llm_client(model_spec=model_spec)
        except LLMClientError as exc:
            if strict_llm:
                raise LLMClientError(classify_llm_error(str(exc))) from exc
            notes.append(f"Flow expansion unavailable: {exc}")
            return FlowExpansionResult(
                entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
                steps=baseline_steps,
                sinks=merge_and_dedupe_sinks(baseline_sinks, derive_sink_candidates_from_steps(baseline_steps)),
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
        if strict_llm:
            raise LLMClientError(classify_llm_error(str(exc))) from exc
        notes.append(f"Flow expansion unavailable: {exc}")
        return FlowExpansionResult(
            entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
            steps=baseline_steps,
            sinks=merge_and_dedupe_sinks(baseline_sinks, derive_sink_candidates_from_steps(baseline_steps)),
            notes=notes,
        )

    payload = _extract_json_payload(response.text)
    if payload is None:
        if strict_llm:
            raise LLMClientError(
                classify_llm_error("Flow expansion output was not valid JSON.")
            )
        notes.append("Flow expansion output was not valid JSON.")
        return FlowExpansionResult(
            entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
            steps=baseline_steps,
            sinks=merge_and_dedupe_sinks(baseline_sinks, derive_sink_candidates_from_steps(baseline_steps)),
            notes=notes,
        )

    normalized, normalize_notes = _parse_flow_expansion_payload(
        payload,
        matched_endpoint.repo,
        context_raw_lower=context_raw_lower,
        context_literal_blob=context_literal_blob,
    )
    merged_steps = _merge_steps(baseline_steps, normalized.steps)
    explicit_sinks = merge_and_dedupe_sinks(baseline_sinks, normalized.sinks)
    merged_sinks = merge_and_dedupe_sinks(explicit_sinks, derive_sink_candidates_from_steps(merged_steps))
    if baseline_steps:
        normalize_notes.append(
            f"Merged deterministic baseline with LLM flow output (baseline_steps={len(baseline_steps)})."
        )
    merged_notes = notes + [note for note in normalize_notes if note not in notes]
    merged_confidence = normalized.confidence
    if merged_confidence is None:
        confidences = [item.confidence for item in [*merged_steps, *merged_sinks] if item.confidence is not None]
        if confidences:
            merged_confidence = sum(confidences) / len(confidences)
    return FlowExpansionResult(
        entry_endpoint_id=f"{matched_endpoint.repo}:{matched_endpoint.file}:{matched_endpoint.path or '?'}",
        steps=merged_steps,
        sinks=merged_sinks,
        notes=merged_notes,
        confidence=merged_confidence,
    )
