"""Stage A: candidate-path/test discovery.

Proposes a HYPOTHESIS only — a plausible chain of symbol names connecting
an entrypoint to the changed behavior, and candidate tests — with no
evidence-citation responsibility at all. This is deliberately separated
from proving any one relationship (`sydes.recovery.evidence`, Stage B):
asking one agent to range broadly over the whole repository AND perfectly
cite evidence for every hop in the same pass is exactly what produced weak,
nearby-but-not-quite evidence in earlier versions of this prototype.
Splitting the jobs lets discovery guess freely — nothing it proposes is
trusted until Stage B independently proves each atomic relationship and
Stage C (`sydes.recovery.verify`) independently judges that proof.

No language- or framework-specific text appears anywhere in this module.
"""

from __future__ import annotations

from sydes.llm.client import LLMClient
from sydes.recovery.context import RecoveryContext
from sydes.recovery.react import TOOL_PROMPT_BLOCK, run_react_loop
from sydes.recovery.schema import CandidatePath, CandidateTestClaim, parse_discovery_result
from sydes.recovery.tools import RepoTools

_SYSTEM_PROMPT = f"""You are proposing a HYPOTHESIS about how a changed piece of code is reached, after a structural analyzer stopped early. You are NOT responsible for proving it — a separate step will independently verify every relationship you propose. Your job is only to search broadly and propose the most plausible candidate.

The structural analyzer (a deterministic code-graph pass, referred to below as "the first pass") found a real gap: either no complete path from a reachable, externally meaningful entrypoint to the changed behavior, or no test mapped to it despite a relevant test existing in this diff. Its own findings so far are a HYPOTHESIS you may verify, correct, or complete — not ground truth, and not a boundary on what you may look at.

Your objective:
- Propose the SHORTEST plausible chain of symbol names connecting a reachable, externally meaningful entrypoint (an HTTP route, an RPC/GraphQL operation, a scheduled/queued job, a CLI command, or any other externally triggerable entrypoint) to the changed behavior. Do not pad it with interfaces, abstract wrappers, aliases, helper layers, or extra nodes beyond what plausibly connects the two — a later step will only ever shorten your proposal further, never lengthen it, so propose the shortest chain you find plausible.
- Search the repository broadly if needed. Do not stop merely because a direct static caller is absent — runtime indirection, registration, decorators, dependency injection, dispatch, callbacks, configuration, events, queues, factories, middleware, reflection, or other repository-local wiring may connect the path. These are examples of the KINDS of things that might connect a path, not a checklist to apply mechanically — read what is actually in this repository.
- Also propose candidate tests that plausibly DIRECTLY exercise the changed behavior (construct or call the changed code, or exercise the entrypoint that reaches it) — not merely a test file that happens to be part of this diff, imports the same module, or shares a similar name. A later step independently checks each candidate.
- You do not need to cite line-level evidence here — a separate step proves each hop. But identity matters even at this stage: whenever you know or can quickly tell which file a symbol is declared in, name it — an entity with no file is more likely to get confused with a same-named symbol declared somewhere else in the repository during the proving step. A repository can legitimately contain more than one symbol with the same short name in different files/modules; naming the file whenever you can is how a later step avoids picking the wrong one.
- The node whose symbol you name as `target_node` MUST be the entity whose file matches one of the `changed_files` you were given below — that is the actual changed behavior from this diff, not a same-named or similar-looking symbol elsewhere. If you cannot confirm this, say so honestly rather than guessing.
- You are given a `declarative_entrypoints` list below: methods a deterministic scan found decorated/annotated in a way that shape-matches an externally-triggerable HTTP entrypoint, near the changed files. This is a HINT, not a verified fact — a separate step still has to prove the connection from any of these to the changed behavior — but the entrypoint for THIS change is very often one of these, even when it is not itself a changed file (a route's controller is routinely a sibling of the handler it dispatches to). Check this list before deciding you found no plausible entrypoint. Two entries in this list can share the same bare method name when the source overloads it (more than one method with that name in the class, each handling a different route) — each entry is already a DISTINCT entity, disambiguated by its own (file, line) and its own `path`/`http_method`; a repeated name here is not by itself a reason for caution or to abstain — use the accompanying path/method to judge which one, if any, plausibly matches this change.
- If, after actively looking, you find no plausible entrypoint-to-changed-behavior chain at all, propose no `candidate_path` (omit it or use `null`) rather than inventing a weak one — a separate step cannot prove a hypothesis that was never real to begin with.
- `nodes[0]` must be something an external caller genuinely triggers from OUTSIDE this process — a network request arriving at a route, a message arriving on a queue from elsewhere, a scheduler firing, a CLI invocation, or similar. An in-process object your own code constructs and passes around internally (a plain data/command/query/event object, a DTO, a local callback) is NOT by itself a valid `nodes[0]`, no matter how directly it connects to the changed behavior — it is a hop INSIDE the path, never the entrypoint. Picking an internal object as `nodes[0]` to shorten the chain is exactly the kind of shortcut this schema exists to prevent; if you cannot identify what actually triggers the chain from outside the process, that is itself a sign you have not found a real path yet — keep looking, or propose none.

{TOOL_PROMPT_BLOCK}

When you are done investigating (or you have used your available turns), respond with your FINAL structured answer instead, as a single JSON object and nothing else, matching exactly this shape:
{{"final": {{
  "candidate_path": {{
    "entrypoint": "e.g. GET /users or a queue/job name",
    "target_node": "the exact symbol (must also appear in nodes below) that IS the changed behavior",
    "nodes": [
      {{"symbol": "entrypoint symbol/route", "file": "file if known, else omit or empty string", "qualified_name": "optional"}},
      {{"symbol": "...", "file": "..."}},
      {{"symbol": "the changed behavior symbol", "file": "the actual changed file from changed_files"}}
    ]
  }},
  "candidate_tests": [
    {{"file": "...", "test": "test name/identifier", "covers": "short statement of what behavior it exercises", "target": {{"symbol": "the changed symbol this test covers", "file": "its actual file"}}}}
  ]
}}}}

`candidate_path` may be `null` if you found no plausible chain. `nodes` needs at least 2 entries (an entrypoint and something distinct from it); `target_node` must be one of `nodes`' symbols and must not be `nodes[0]`'s symbol.

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""


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
        "changed_files (the ONLY files that actually changed in this diff -- target_node's file must be one of these):",
        *([f"  - {f}" for f in context.changed_files] if context.changed_files else ["  (none recorded)"]),
        "",
        f"current_result_summary: {context.current_result_summary}",
        "",
        "first_pass_findings (your starting hypothesis to verify/correct/complete):",
        context.cbm_fragments,
        "",
        "known_entrypoints_already_seen_by_the_first_pass:",
        *([f"  - {e}" for e in context.known_entrypoints] if context.known_entrypoints else ["  (none)"]),
        "",
        "declarative_entrypoints (decorator-shape matches near the changed files -- hints, not proof; see above):",
        *([f"  - {e}" for e in context.declarative_entrypoints] if context.declarative_entrypoints else ["  (none found)"]),
        "",
        "test_candidates (files this diff changed that look test-related):",
        *([f"  - {t}" for t in context.test_candidates] if context.test_candidates else ["  (none flagged)"]),
        "",
        "Investigate now. Call one tool per turn, or give your final candidate answer when ready.",
    ]
    return "\n".join(lines)


def discover_candidate(
    context: RecoveryContext, *, tools: RepoTools, client: LLMClient,
    max_turns: int, max_response_chars: int, stats,
) -> tuple[CandidatePath | None, list[CandidateTestClaim]]:
    return run_react_loop(
        client=client, tools=tools, system_prompt=_SYSTEM_PROMPT,
        initial_prompt=_build_initial_prompt(context), max_turns=max_turns,
        max_response_chars=max_response_chars, stats=stats, parse_final=parse_discovery_result,
    )
