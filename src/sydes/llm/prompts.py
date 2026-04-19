"""Prompt builders for provider-neutral LLM-assisted endpoint discovery."""

from __future__ import annotations

import json

from sydes.core.models import CandidateFileRead


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
        "You are extracting likely HTTP API endpoint candidates from backend-oriented source files.\n"
        "Only use evidence present in the provided candidate snippets.\n\n"
        "What counts as an endpoint:\n"
        "- Clear route/handler registration for HTTP APIs (e.g. router/app/server route bindings).\n"
        "- Endpoint-like declarations with clear method/path linkage in code.\n"
        "- Ignore business logic functions that are not API entrypoints.\n\n"
        "Rules:\n"
        "1. Report only likely HTTP API endpoints grounded in provided files.\n"
        "2. Do not invent routes, handlers, methods, or paths that are not supported.\n"
        "3. If method/path/handler is unclear, set it to null instead of guessing.\n"
        "4. If multiple handlers seem possible, prefer uncertainty/status notes over invention.\n"
        "5. Prefer candidates with clearer route-registration evidence.\n"
        "6. Preserve grounding with repo, file, and evidence labels.\n\n"
        "Response format:\n"
        "- Return JSON only (no markdown fences, no prose).\n"
        "- You may return either:\n"
        "  a) top-level object with `endpoints` and optional `notes`, or\n"
        "  b) top-level list of endpoint objects.\n"
        "- Keep structure tolerant of missing fields.\n\n"
        "Endpoint object shape (soft fields allowed):\n"
        "[\n"
        "    {\n"
        '      "method": string|null,\n'
        '      "path": string|null,\n'
        '      "handler": string|null,\n'
        '      "file": string,\n'
        '      "repo": string,\n'
        '      "service": string|null,\n'
        '      "evidence": [{"file": string, "symbol": string|null, "label": string|null}],\n'
        '      "confidence": number|null,\n'
        '      "status": string|null\n'
        "    }\n"
        "]\n\n"
        "Candidate input payload:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )
