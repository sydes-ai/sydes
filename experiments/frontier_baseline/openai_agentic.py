"""Minimal, bounded agentic tool-loop for OpenAI models: read_file, list_dir,
grep -- so the "frontier model alone" variant gets genuine code-search/read
access to the real repo, not a pre-selected flat bundle (which would bias
recall upward by doing the search step for the model). Capped at
MAX_TOOL_ROUNDS round-trips; the model is told to give a final answer when
it has enough evidence.

Research harness only. Not wired into any production path. Uses the
OPENAI_API_KEY already configured in this environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

MODEL = "gpt-5.1"
MAX_TOOL_ROUNDS = 20
MAX_FILE_BYTES = 40_000

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories at a repo-relative path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Repo-relative directory path, '.' for root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a repo-relative file's full text content.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Repo-relative file path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a literal or regex substring across the repo. Returns matching file:line:text rows, capped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path_glob": {"type": "string", "description": "Optional glob to restrict search, e.g. '**/*.go'."},
                },
                "required": ["pattern"],
            },
        },
    },
]

_IGNORED_DIRS = {".git", "node_modules", "target", "dist", "build", ".venv", "vendor"}


def _safe_path(repo_root: Path, rel: str) -> Path | None:
    candidate = (repo_root / rel).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return candidate


def _tool_list_dir(repo_root: Path, rel: str) -> str:
    target = _safe_path(repo_root, rel)
    if target is None or not target.is_dir():
        return f"error: not a directory: {rel}"
    entries = []
    for item in sorted(target.iterdir()):
        if item.name in _IGNORED_DIRS:
            continue
        entries.append(f"{'d' if item.is_dir() else 'f'} {item.name}")
    return "\n".join(entries) if entries else "(empty)"


def _tool_read_file(repo_root: Path, rel: str) -> str:
    target = _safe_path(repo_root, rel)
    if target is None or not target.is_file():
        return f"error: not a file: {rel}"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"error: {exc}"
    if len(text) > MAX_FILE_BYTES:
        text = text[:MAX_FILE_BYTES] + "\n...[truncated]"
    return text


def _tool_grep(repo_root: Path, pattern: str, path_glob: str | None) -> str:
    import re
    regex = re.compile(pattern)
    matches: list[str] = []
    glob_pattern = path_glob or "**/*"
    for path in repo_root.glob(glob_pattern):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = path.relative_to(repo_root).as_posix()
                matches.append(f"{rel}:{i}:{line.strip()[:200]}")
                if len(matches) >= 100:
                    return "\n".join(matches) + "\n...[capped at 100 matches]"
    return "\n".join(matches) if matches else "(no matches)"


def _dispatch(repo_root: Path, name: str, args: dict[str, Any]) -> str:
    if name == "list_dir":
        return _tool_list_dir(repo_root, args.get("path", "."))
    if name == "read_file":
        return _tool_read_file(repo_root, args.get("path", ""))
    if name == "grep":
        return _tool_grep(repo_root, args.get("pattern", ""), args.get("path_glob"))
    return f"error: unknown tool {name}"


def run_agentic(*, repo_root: Path, prompt: str) -> dict[str, Any]:
    """Runs the bounded tool loop; returns {"final_answer": str, "tool_calls": int, "transcript": [...]}."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tool_call_count = 0
    transcript: list[dict[str, Any]] = []

    for _round in range(MAX_TOOL_ROUNDS):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            return {"final_answer": msg.content or "", "tool_calls": tool_call_count, "transcript": transcript}

        for tc in msg.tool_calls:
            tool_call_count += 1
            args = json.loads(tc.function.arguments or "{}")
            result = _dispatch(repo_root, tc.function.name, args)
            transcript.append({"tool": tc.function.name, "args": args, "result_preview": result[:300]})
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": result,
            })

    return {"final_answer": "(hit MAX_TOOL_ROUNDS without a final answer)", "tool_calls": tool_call_count, "transcript": transcript}
