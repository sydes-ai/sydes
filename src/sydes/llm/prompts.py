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
        "Task: extract likely HTTP API endpoints from provided files only.\n"
        "Rules:\n"
        "- Only report endpoints grounded in snippets.\n"
        "- Do not invent unsupported routes.\n"
        "- If method/path/handler is unclear, use null.\n"
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
