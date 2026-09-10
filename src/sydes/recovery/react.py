"""The generic ReAct tool-use loop, shared by discovery
(`sydes.recovery.discovery`) and atomic evidence completion
(`sydes.recovery.evidence`).

Both stages need the exact same mechanics — one JSON-in/JSON-out turn per
LLM call, either a tool call or a final structured answer, executed
against the same repo-scoped `RepoTools` — differing only in system
prompt, initial prompt, and how the final answer is parsed. Factoring that
out here means the loop itself is written and tested once, not duplicated
per stage.

No language- or framework-specific text appears anywhere in this module.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Callable, TypeVar

from sydes.llm.client import LLMClient, LLMClientError, LLMRequest
from sydes.recovery.schema import RecoveryError
from sydes.recovery.tools import RepoTools

T = TypeVar("T")

_TOOL_NAMES = ("read_file", "search_text", "list_directory", "final")

TOOL_PROMPT_BLOCK = """You have these tools. Call exactly one per turn, as a single JSON object and nothing else:
{"tool": "read_file", "args": {"path": "relative/path.ext", "start_line": optional_int, "end_line": optional_int}}
{"tool": "search_text", "args": {"pattern": "regex pattern", "path_glob": "optional glob, e.g. src/**/*.ts"}}
{"tool": "list_directory", "args": {"path": "relative/dir/or/. for repo root"}}"""


def extract_turn(text: str, *, max_chars: int) -> dict[str, Any]:
    stripped = text.strip()[:max_chars]
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise RecoveryError("agent turn was not a JSON object")
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RecoveryError(f"agent turn was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecoveryError("agent turn was not a JSON object")
    return payload


def run_tool(tools: RepoTools, tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "read_file":
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return "ERROR: 'path' is required"
        return tools.read_file(path, start_line=args.get("start_line"), end_line=args.get("end_line"))
    if tool_name == "search_text":
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return "ERROR: 'pattern' is required"
        return tools.search_text(pattern, path_glob=args.get("path_glob"))
    if tool_name == "list_directory":
        return tools.list_directory(args.get("path", "."))
    return f"ERROR: unknown tool {tool_name!r}"


def run_react_loop(
    *,
    client: LLMClient,
    tools: RepoTools,
    system_prompt: str,
    initial_prompt: str,
    max_turns: int,
    max_response_chars: int,
    stats: Any,
    parse_final: Callable[[str], T],
    out_of_turns_note: str = "You are out of turns. Give your FINAL answer now.",
) -> T:
    """Drive one tool-use loop to a parsed final answer.

    `stats` needs only `.turns`, `.llm_calls`, `.latency_ms`,
    `.prompt_tokens`, `.completion_tokens`, `.files_read` — any object with
    those (in particular `sydes.recovery.agent.RecoveryRunStats`) works;
    this module has no dependency on that class itself. Raises
    `RecoveryError` on provider failure, malformed turn JSON, an
    unsupported tool name, or running out of turns without a final answer
    — the caller decides what "failed" means for its stage.
    """
    prompt = initial_prompt
    for turn in range(max_turns + 1):
        stats.turns += 1
        forced_final = turn == max_turns
        turn_prompt = prompt if not forced_final else f"{prompt}\n\n{out_of_turns_note}"
        started = time.perf_counter()
        try:
            response = client.generate(LLMRequest(prompt=turn_prompt, system=system_prompt, temperature=None))
        except LLMClientError as exc:
            raise RecoveryError(f"recovery provider failed: {exc}") from exc
        stats.latency_ms += (time.perf_counter() - started) * 1000.0
        stats.llm_calls += 1
        if response.usage:
            stats.prompt_tokens += response.usage.get("prompt_tokens", 0)
            stats.completion_tokens += response.usage.get("completion_tokens", 0)

        payload = extract_turn(response.text, max_chars=max_response_chars)
        if "final" in payload:
            return parse_final(json.dumps(payload["final"]))

        tool_name = payload.get("tool")
        args = payload.get("args", {})
        if tool_name not in _TOOL_NAMES or not isinstance(args, dict):
            raise RecoveryError(f"agent turn named an unsupported tool: {tool_name!r}")

        observation = run_tool(tools, tool_name, args)
        if tool_name == "read_file" and isinstance(args.get("path"), str):
            stats.files_read.append(args["path"])
        prompt = (
            f"{prompt}\n\nTOOL CALL: {tool_name}({args})\nOBSERVATION:\n{observation}\n\n"
            "Continue investigating, or give your final answer."
        )

    raise RecoveryError("recovery loop exited without a final answer")
