"""The recovery agent's tool-use loop, and the `recover()` entry point.

CBM is a helper here, not the authority: the agent starts from the first
pass's own hypothesis (the CBM/structural fragments in `RecoveryContext`)
but is explicitly told it may inspect any file in the repository, not only
ones CBM already surfaced, and that it may correct — not merely extend —
the first pass's hypothesis if source evidence contradicts it.

Reuses `sydes.llm.client.LLMClient` exactly as `sydes.impact.guide` does:
no second provider system, no structured-output mode assumed. The contract
is a manual ReAct loop over plain text completions: each turn the model
returns either one tool call or a final structured answer, both as a single
JSON object; this module parses that itself rather than relying on any
provider's native function-calling API, so the same loop works unchanged
across Ollama/OpenAI/Anthropic.

No language- or framework-specific text appears anywhere in this module.
The prompt below names concepts ("registration", "decorators", "dispatch",
"dependency injection", "events", "queues", "middleware", "reflection") as
open EXAMPLES of what repository-local wiring might look like — not rules
this module checks for or special-cases in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any

from sydes.llm.client import LLMClient, LLMClientError, LLMRequest
from sydes.recovery.context import RecoveryContext
from sydes.recovery.schema import (
    RecoveryError,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    parse_recovery_result,
)
from sydes.recovery.tools import RepoTools, ToolCallRecord
from sydes.recovery.verify import verify_recovery_result

_TOOL_NAMES = ("read_file", "search_text", "list_directory", "final")

_SYSTEM_PROMPT = """You are recovering missing impact evidence after a structural analyzer stopped early.

The structural analyzer (a deterministic code-graph pass, referred to below as "the first pass") found a real gap: either no complete path from a reachable, externally meaningful entrypoint to the changed behavior, or no test mapped to it despite a relevant test existing in this diff. Its own findings so far are a HYPOTHESIS for you to verify, correct, or complete — not ground truth, and not a boundary on what you may look at.

Your objective:
- Verify the first pass's current hypothesis, and correct it if source evidence shows something different. If the first pass implies a path like "A -> B -> ?" but the actual source shows "A -> C -> D", report the corrected path, not a patched-up version of the wrong one.
- Establish source-backed paths from the changed behavior to reachable, externally meaningful entrypoints (an HTTP route, an RPC/GraphQL operation, a scheduled/queued job, a CLI command, or any other externally triggerable entrypoint).
- Search the repository broadly if needed. Do not stop merely because direct static callers are absent from what the first pass already found — runtime indirection, registration, decorators, dependency injection, dispatch, callbacks, configuration, events, queues, factories, middleware, reflection, or other repository-local wiring may connect the path. These are examples of the KINDS of things that might connect a path, not a checklist to apply mechanically — read what is actually in this repository.
- Also identify tests that DIRECTLY exercise the changed behavior (construct or call the changed code, or exercise the entrypoint that reaches it) — not merely a test file that happens to be part of this diff, imports the same module, or shares a similar name.

Shortest-sufficient-path rule (important):
- Report the SHORTEST chain of nodes that connects the entrypoint to the changed behavior. Do not pad the path with interfaces, abstract wrappers, aliases, helper layers, or extra registration nodes beyond what is actually needed to prove the connection.
- Every path has exactly one `target_node`: the node that IS the changed behavior. If the changed code lives in the first node past the entrypoint, your path should usually be just two nodes long — do not keep appending further hops merely because you found them nearby or because they technically exist.
- If reaching the changed behavior requires going through an intermediate object or message (a request, command, event, job, or similar) rather than a direct call, your path must include that hop explicitly, and your evidence for THAT hop must show two things together: (a) the source actually constructs/produces that specific object or message, and (b) the target is specifically associated with or registered for that exact same object/message type — not merely that the source makes something and the target exists somewhere in the same area of the codebase. Evidence of one side alone is not sufficient for that edge.
- A path is not optional padding to skip: `target_node` must NEVER be the same as the entrypoint node (`nodes[0]`). Declaring the changed symbol as its own entrypoint, with no real edge to anything external, is not a recovered path — it proves nothing about reachability and will be rejected outright. If you cannot find a genuine externally-reachable entrypoint that connects to the changed behavior, do not invent a trivial one-node "path" to avoid that hard problem — report it honestly via `unresolved` instead, naming exactly what evidence (a route, a registration, a dispatch site) you looked for and could not find.

Evidence rules (important):
- Every edge in your path is judged independently by a separate verifier afterward. Each edge needs its OWN sufficient evidence — do not rely on one piece of evidence to justify multiple edges, and do not describe a relationship your cited evidence does not actually show.
- Do NOT mark an edge or path "established" on "seems likely", "probably", or "framework/runtime convention suggests" reasoning alone. Two symbols merely existing, or being declared/registered in the same module/file with no dispatch or call evidence connecting them, is NOT sufficient evidence for an edge between them.
- Do not return ambiguity too early — actively try to eliminate it first (read the files a plausible bridge would require, follow an import, check a registration point) before giving up on a lead.
- If you genuinely cannot establish a relationship after active investigation, return it as unresolved and state EXACTLY what evidence is missing (e.g. "no file registers or dispatches this symbol under any name I could find" — not "insufficient context").
- Never invent a file, symbol, or line you have not actually read via a tool call in this conversation.
- For a recovered test: only claim it if your cited evidence shows the test's own code actually calling or asserting on the changed behavior, not just importing the file it lives in.

You have these tools. Call exactly one per turn, as a single JSON object and nothing else:
{"tool": "read_file", "args": {"path": "relative/path.ext", "start_line": optional_int, "end_line": optional_int}}
{"tool": "search_text", "args": {"pattern": "regex pattern", "path_glob": "optional glob, e.g. src/**/*.ts"}}
{"tool": "list_directory", "args": {"path": "relative/dir/or/. for repo root"}}

When you are done investigating (or you have used your available turns), respond with your FINAL structured answer instead, as a single JSON object and nothing else, matching exactly this shape:
{"final": {
  "recovered_paths": [
    {
      "entrypoint": "e.g. GET /users or a queue/job name",
      "target_node": "the exact symbol (must also appear in nodes below) that IS the changed behavior",
      "nodes": [{"symbol": "...", "file": "..."}, {"symbol": "...", "file": "..."}],
      "edges": [{"from": "<a nodes[].symbol>", "to": "<the next nodes[].symbol>", "relationship": "e.g. constructs and dispatches this message", "evidence": [{"file": "...", "line_start": 1, "line_end": 5, "fact": "short specific statement of what these lines show"}]}]
    }
  ],
  "recovered_tests": [
    {"file": "...", "test": "test name/identifier", "covers": "short statement of what behavior it exercises", "evidence": [{"file": "...", "line_start": 1, "line_end": 5, "fact": "..."}]}
  ],
  "corrected_first_pass_claims": [
    {"claim": "what this corrects", "original": "the first pass's claim", "corrected": "what the evidence actually shows", "evidence": [...]}
  ],
  "unresolved": [
    {"question": "what could not be established", "missing_evidence": "exactly what evidence is missing"}
  ]
}}

`nodes` must have exactly one more entry than `edges`, forming a single chain: edges[0] goes from nodes[0] to nodes[1], edges[1] from nodes[1] to nodes[2], and so on. Do not add a status field to paths or edges yourself — a separate verifier assigns those from your evidence.

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""


@dataclass(frozen=True)
class RecoveryBudget:
    """A hard ceiling on the recovery run — "start simple", not a routing
    policy. `max_turns` bounds tool-call turns before a final answer is
    forced; `max_verify_retries` bounds the verify/retry cycle in
    `sydes.recovery.verify` (see its module docstring for why this is
    capped at exactly one retry)."""

    max_turns: int = 6
    max_verify_retries: int = 1
    max_response_chars: int = 4000


@dataclass
class RecoveryRunStats:
    """Cost/latency/tool-usage accounting for one `recover()` call — the
    evaluation harness's primary output alongside the `RecoveryResult`
    itself."""

    turns: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    llm_calls: int = 0
    verify_retries: int = 0
    files_read: list[str] = field(default_factory=list)


@dataclass
class RecoveryOutcome:
    """What `recover()` returns: the (possibly verify-downgraded) result,
    plus the stats needed to judge whether this prototype is worth
    integrating."""

    result: RecoveryResult
    stats: RecoveryRunStats
    trigger_reason: str


def _extract_turn(text: str, *, max_chars: int) -> dict[str, Any]:
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


def _build_initial_prompt(context: RecoveryContext) -> str:
    lines = [
        f"why_the_first_pass_stopped: {context.reason_first_pass_stopped}",
        f"gap_kinds: {', '.join(context.gap_kinds)}",
        "",
        "diff (changed files this PR touches):",
        context.diff_summary,
        "",
        "changed_symbols:",
        *([f"  - {s}" for s in context.changed_symbols] if context.changed_symbols else ["  (none listed)"]),
        "",
        f"current_result_summary: {context.current_result_summary}",
        "",
        "first_pass_findings (your starting hypothesis to verify/correct/complete):",
        context.cbm_fragments,
        "",
        "known_entrypoints_already_seen_by_the_first_pass:",
        *([f"  - {e}" for e in context.known_entrypoints] if context.known_entrypoints else ["  (none)"]),
        "",
        "test_candidates (files this diff changed that look test-related):",
        *([f"  - {t}" for t in context.test_candidates] if context.test_candidates else ["  (none flagged)"]),
        "",
        "Investigate now. Call one tool per turn, or give your final answer when ready.",
    ]
    return "\n".join(lines)


def _run_tool(tools: RepoTools, tool_name: str, args: dict[str, Any]) -> str:
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


def _run_loop(
    client: LLMClient, tools: RepoTools, context: RecoveryContext, budget: RecoveryBudget,
    stats: RecoveryRunStats, *, retry_note: str | None = None,
) -> RecoveryResult:
    prompt = _build_initial_prompt(context)
    if retry_note:
        prompt = f"{prompt}\n\nA previous attempt was rejected on verification: {retry_note}\nTry again, investigating further before answering.".strip()

    for turn in range(budget.max_turns + 1):
        stats.turns += 1
        forced_final = turn == budget.max_turns
        turn_prompt = prompt if not forced_final else f"{prompt}\n\nYou are out of turns. Give your FINAL answer now."
        started = time.perf_counter()
        try:
            response = client.generate(LLMRequest(prompt=turn_prompt, system=_SYSTEM_PROMPT, temperature=None))
        except LLMClientError as exc:
            raise RecoveryError(f"recovery provider failed: {exc}") from exc
        stats.latency_ms += (time.perf_counter() - started) * 1000.0
        stats.llm_calls += 1
        if response.usage:
            stats.prompt_tokens += response.usage.get("prompt_tokens", 0)
            stats.completion_tokens += response.usage.get("completion_tokens", 0)

        payload = _extract_turn(response.text, max_chars=budget.max_response_chars)
        if "final" in payload:
            return parse_recovery_result(json.dumps(payload["final"]))

        tool_name = payload.get("tool")
        args = payload.get("args", {})
        if tool_name not in _TOOL_NAMES or not isinstance(args, dict):
            raise RecoveryError(f"agent turn named an unsupported tool: {tool_name!r}")

        observation = _run_tool(tools, tool_name, args)
        if tool_name == "read_file" and isinstance(args.get("path"), str):
            stats.files_read.append(args["path"])
        prompt = (
            f"{prompt}\n\nTOOL CALL: {tool_name}({args})\nOBSERVATION:\n{observation}\n\n"
            "Continue investigating, or give your final answer."
        )

    raise RecoveryError("recovery loop exited without a final answer")


def recover(
    result_context: RecoveryContext,
    *,
    repo_root: Path,
    client: LLMClient,
    trigger_reason: str,
    budget: RecoveryBudget | None = None,
) -> RecoveryOutcome:
    """Run one recovery attempt: initial investigation, then exactly one
    verification pass, then (only if verification rejects an established
    path) exactly one targeted retry followed by one more verification pass.
    Never more than that — see `RecoveryBudget`/`sydes.recovery.verify`.

    Never raises on a clean failure path: an `LLMClientError` or malformed
    model output during the FIRST attempt still raises `RecoveryError` (the
    caller — the CLI's opt-in `--ai-recovery` hook — is responsible for
    catching it and leaving the first-pass result untouched), but once an
    initial `RecoveryResult` is in hand, a failure during retry/verify never
    discards it — it is returned as-is rather than lost.
    """
    active_budget = budget or RecoveryBudget()
    tools = RepoTools(repo_root)
    stats = RecoveryRunStats()

    draft = _run_loop(client, tools, result_context, active_budget, stats)
    verified = verify_recovery_result(draft, tools=tools, client=client, stats=stats)

    unresolved_reasons = [
        edge.rejection_reason
        for path in verified.recovered_paths
        for edge in path.unresolved_suffix
        if edge.rejection_reason
    ]
    if (
        verified.status != STATUS_ESTABLISHED
        and unresolved_reasons
        and stats.verify_retries < active_budget.max_verify_retries
    ):
        rejection_notes = "; ".join(unresolved_reasons)
        stats.verify_retries += 1
        try:
            retry_draft = _run_loop(
                client, tools, result_context, active_budget, stats, retry_note=rejection_notes,
            )
            retry_verified = verify_recovery_result(retry_draft, tools=tools, client=client, stats=stats)
            if _status_rank(retry_verified.status) > _status_rank(verified.status):
                verified = retry_verified
        except RecoveryError:
            pass  # keep the original (better-or-equal) verified result rather than losing it

    stats.tool_calls = list(tools.calls)
    return RecoveryOutcome(result=verified, stats=stats, trigger_reason=trigger_reason)


def _status_rank(status: str) -> int:
    return {STATUS_ESTABLISHED: 2, STATUS_PARTIAL: 1, STATUS_UNRESOLVED: 0}.get(status, 0)
