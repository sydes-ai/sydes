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
    payload = {
        "target_hint": target_hint,
        "method_hint": method_hint,
        "candidates": [_serialize_candidate(candidate) for candidate in candidates],
    }
    return (
        "You are extracting likely HTTP API endpoint candidates from source files.\n"
        "Only use evidence present in the provided file snippets.\n\n"
        "Rules:\n"
        "1. Identify likely HTTP endpoints with method and path when supported by evidence.\n"
        "2. Infer handler symbol/name only when directly supported by code context.\n"
        "3. If method/path/handler is unclear, leave it null rather than guessing.\n"
        "4. Preserve grounding with repo+file and evidence entries (file, symbol, label).\n"
        "5. Preserve uncertainty using status/confidence, avoid invented certainty.\n"
        "6. Return partial endpoint objects when only some fields are supported.\n\n"
        "Return JSON object with shape:\n"
        "{\n"
        '  "endpoints": [\n'
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
        "  ],\n"
        '  "notes": [string]\n'
        "}\n\n"
        "Candidate inputs:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )
