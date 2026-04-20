"""Prompt builders for provider-neutral LLM-assisted discovery and expansion."""

from __future__ import annotations

import json

from sydes.core.models import CandidateFileRead, EndpointCandidate, ExpansionContextFile, FlowExpansionContext


def _serialize_candidate(candidate: CandidateFileRead) -> dict:
    """Serialize a candidate read into a compact, model-friendly object."""
    if candidate.skipped or candidate.snippet is None:
        return {
            "repo": candidate.repo,
            "file": candidate.relative_path,
            "skipped": True,
            "skip_reason": candidate.skip_reason,
        }

    return {
        "repo": candidate.repo,
        "file": candidate.relative_path,
        "truncated": candidate.snippet.truncated,
        "line_count": candidate.snippet.line_count,
        "char_count": candidate.snippet.char_count,
        "content": candidate.snippet.text,
    }


def build_endpoint_discovery_prompt(
    candidates: list[CandidateFileRead],
    *,
    target_hint: str | None = None,
    method_hint: str | None = None,
) -> str:
    """Build an endpoint discovery prompt grounded in bounded candidate files."""
    repo_names = sorted({candidate.repo for candidate in candidates})
    candidate_files = [candidate.relative_path for candidate in candidates]
    payload = {
        "repos": repo_names,
        "target_hint": target_hint,
        "method_hint": method_hint,
        "candidate_files": candidate_files,
        "candidates": [_serialize_candidate(candidate) for candidate in candidates],
    }
    return (
        "Task: extract likely HTTP API endpoints from provided files only.\n"
        "Rules:\n"
        "- Only report endpoints grounded in snippets.\n"
        "- Do not invent unsupported routes.\n"
        "- If HTTP method/path/handler is unclear, use null.\n"
        "- If path is only '/' with weak evidence, return nothing for that candidate.\n"
        "- Prefer extracting handler symbol/function name when clearly visible.\n"
        "- Keep repo/file grounding and evidence labels.\n"
        "- Prefer uncertainty over guessing when ambiguous.\n\n"
        "Return JSON only.\n"
        "Use either:\n"
        '{"endpoints":[{"method":null,"path":null,"handler":null,"file":"","repo":"","service":null,'
        '"evidence":[{"file":"","symbol":null,"label":null}],"confidence":null,"status":null}],"notes":[]}\n'
        "or a top-level list of endpoint objects.\n\n"
        "Input:\n"
        f"{json.dumps(payload, separators=(',', ':'))}\n"
    )


def _serialize_expansion_context_file(context_file: ExpansionContextFile) -> dict:
    """Serialize one contextual file entry for compact flow-expansion prompts."""
    if context_file.read is None:
        return {
            "repo": context_file.repo,
            "file": context_file.file,
            "selection_reasons": context_file.selection_reasons,
            "missing_read": True,
        }
    if context_file.read.skipped or context_file.read.snippet is None:
        return {
            "repo": context_file.repo,
            "file": context_file.file,
            "selection_reasons": context_file.selection_reasons,
            "skipped": True,
            "skip_reason": context_file.read.skip_reason,
        }
    snippet = context_file.read.snippet
    return {
        "repo": context_file.repo,
        "file": context_file.file,
        "selection_reasons": context_file.selection_reasons,
        "truncated": snippet.truncated,
        "line_count": snippet.line_count,
        "char_count": snippet.char_count,
        "content": snippet.text,
    }


def build_flow_expansion_prompt(
    matched_endpoint: EndpointCandidate,
    context: FlowExpansionContext,
) -> str:
    """Build a compact prompt to infer one likely downstream execution flow."""
    payload = {
        "endpoint": {
            "method": matched_endpoint.method,
            "path": matched_endpoint.path,
            "handler": matched_endpoint.handler,
            "repo": matched_endpoint.repo,
            "service": matched_endpoint.service,
            "file": matched_endpoint.file,
            "evidence": [
                {"file": item.file, "symbol": item.symbol, "label": item.label}
                for item in matched_endpoint.evidence
            ],
        },
        "context": {
            "anchor_repo": context.anchor_repo,
            "anchor_file": context.anchor_file,
            "notes": context.notes,
            "files": [_serialize_expansion_context_file(item) for item in context.files],
        },
    }
    return (
        "Task: expand one likely downstream API flow from the matched endpoint.\n"
        "Focus on the main happy-path only.\n"
        "Return concise JSON only.\n"
        "Rules:\n"
        "- Use only the provided files.\n"
        "- Do not invent calls, symbols, or files.\n"
        "- Return partial flow when evidence is limited.\n"
        "- Preserve uncertainty instead of guessing.\n"
        "- Prefer ordered execution steps from endpoint handler outward.\n"
        "- Detect likely sinks when grounded: database read/write, external API call, queue publish/consume, file write.\n"
        "- If sink type is likely but exact target is unclear, mark status as inferred and keep name/action soft.\n"
        "- Include evidence with file and symbol when available.\n\n"
        "Return shape:\n"
        '{"steps":[{"kind":"","name":"","repo":null,"service":null,"file":null,"symbol":null,'
        '"evidence":[{"file":"","symbol":null,"label":null}],"confidence":null,"status":null}],'
        '"sinks":[{"kind":"","name":"","repo":null,"service":null,"file":null,"symbol":null,'
        '"action":null,"evidence":[{"file":"","symbol":null,"label":null}],"confidence":null,"status":null}],'
        '"notes":[],"confidence":null}\n\n'
        "Input:\n"
        f"{json.dumps(payload, separators=(',', ':'))}\n"
    )
