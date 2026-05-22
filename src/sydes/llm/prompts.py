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
            "role": candidate.role,
            "skipped": True,
            "skip_reason": candidate.skip_reason,
        }

    return {
        "repo": candidate.repo,
        "file": candidate.relative_path,
        "role": candidate.role,
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
            "missing_read": True,
        }
    if context_file.read.skipped or context_file.read.snippet is None:
        return {
            "repo": context_file.repo,
            "file": context_file.file,
            "skipped": True,
            "skip_reason": context_file.read.skip_reason,
        }
    snippet = context_file.read.snippet
    return {
        "repo": context_file.repo,
        "file": context_file.file,
        "truncated": snippet.truncated,
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
            "file": matched_endpoint.file,
        },
        "files": [_serialize_expansion_context_file(item) for item in context.files],
    }
    return (
        "Task: infer one short happy-path flow for the matched API endpoint.\n"
        "Rules:\n"
        "- Use only provided files; do not invent code paths.\n"
        "- Prefer short high-confidence flow over broad speculation.\n"
        "- Keep useful intermediate steps even without exact symbol.\n"
        "- Prefer literal operations visible in code: db.add, db.commit, db.refresh, create User object, return user.\n"
        "- Do not introduce clients/services unless explicitly referenced in provided files.\n"
        "- If unsure, omit the step rather than inventing a generic abstraction.\n"
        "- If uncertain, keep partial steps/sinks and set status='inferred'.\n"
        "- Sinks: database, external_api, queue, file_sink.\n"
        "- Output JSON only.\n\n"
        "JSON shape:\n"
        '{"steps":[{"kind":"internal_step","name":"","symbol":null,"file":null,"repo":null,"service":null,"evidence":[],"confidence":null,"status":"inferred"}],'
        '"sinks":[{"kind":"database","name":"","action":null,"symbol":null,"file":null,"repo":null,"service":null,"evidence":[],"confidence":null,"status":"inferred"}],'
        '"notes":[],"confidence":null}\n\n'
        "Input:\n"
        f"{json.dumps(payload, separators=(',', ':'))}\n"
    )
