"""Stage B: atomic evidence completion.

Proves or disproves ONE specific relationship at a time — one edge of a
candidate path, or one candidate test — never a whole path. This is the
other half of the discovery/evidence-completion split: Stage A
(`sydes.recovery.discovery`) is free to guess broadly because nothing it
proposes is trusted; this stage's only job is to actively search for
concrete, inspectable evidence for exactly one claim, and to say so
honestly (an empty evidence list) when it cannot.

`prove_relationship` and `prove_test_claim` never decide accept/established
themselves — they only gather evidence (or fail to). Stage C
(`sydes.recovery.verify`) independently judges whether the gathered
evidence actually proves the claim; an edge/test with no evidence at all is
automatically rejected there without needing an LLM verifier call.

No language- or framework-specific text appears anywhere in this module —
the relationship kinds named in the prompt below are illustrative examples
the model may recognize, never a checklist this code special-cases.
"""

from __future__ import annotations

from sydes.llm.client import LLMClient
from sydes.recovery.react import TOOL_PROMPT_BLOCK, run_react_loop
from sydes.recovery.schema import (
    CandidateTestClaim,
    RecoveredEdge,
    RecoveredTest,
    RecoveryError,
    parse_atomic_completion_result,
)
from sydes.recovery.tools import RepoTools

_EDGE_SYSTEM_PROMPT = f"""You are proving or disproving ONE specific relationship between two named points in a repository, as one small step of a larger recovery task. You are not responsible for the whole path — only this one relationship. Do not restate or describe the whole path; answer only about the two points you are given.

You will be told a FROM point and a TO point (each a symbol name, possibly qualified, with a file hint if known). Search the repository to determine whether the FROM point is actually connected to the TO point through real, inspectable source.

When a relationship remains unproven after your first look, do not give up immediately — search for both endpoint names/types and inspect call sites, registrations, construction sites, annotations/decorators, imports, and configuration that mention both. Runtime dispatch, dependency injection, decorators/annotations, configuration, events, queues, callbacks, and reflection are examples of the KINDS of wiring this might be, not a checklist to apply mechanically. Only after actively investigating should you conclude it cannot be shown.

Rules:
- Evidence must show the SPECIFIC relationship between these two exact points, not merely that both exist somewhere in the codebase, or that they are declared/registered in the same module with nothing tying them together.
- If the relationship goes through an intermediate object or message (a request, command, event, job, or similar) rather than a direct call, evidence must show it flowing from the FROM side to a form the TO side is specifically associated with — not just that the FROM side produces something and the TO side exists in the same area of the codebase.
- Never invent a file, symbol, or line you have not actually read via a tool call in this conversation.
- If you cannot find proof after actively investigating, say so honestly with an empty evidence list and a clear relationship description of what you found instead (or "" if nothing) — do not force a weak or tangential citation to look sufficient. An empty evidence list is a complete, valid, honest answer.
- A separate step will independently and adversarially re-check whatever evidence you submit — do not describe a relationship your cited lines do not actually show.

{TOOL_PROMPT_BLOCK}

When you are done investigating (or you have used your available turns), respond with your FINAL answer instead, as a single JSON object and nothing else:
{{"final": {{"relationship": "short description of the specific connection you found (empty string if none)", "evidence": [{{"file": "...", "line_start": 1, "line_end": 5, "fact": "short specific statement of what these lines show"}}]}}}}

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""

_TEST_SYSTEM_PROMPT = f"""You are proving or disproving ONE specific claim: that a named test directly exercises a specific changed behavior. You are not responsible for anything else in this task.

Search the test file (and anything it calls into) to determine whether the test's own code actually calls, constructs, or asserts on the changed behavior — not merely that it imports the same module, lives in the same directory, or shares a similar name.

Rules:
- Evidence must show the test's own code invoking or asserting on the changed behavior specifically, not just importing the file it lives in.
- Never invent a file, symbol, or line you have not actually read via a tool call in this conversation.
- If you cannot find proof after actively investigating, say so honestly with an empty evidence list — do not force a weak citation to look sufficient.
- A separate step will independently and adversarially re-check whatever evidence you submit.

{TOOL_PROMPT_BLOCK}

When you are done investigating (or you have used your available turns), respond with your FINAL answer instead, as a single JSON object and nothing else:
{{"final": {{"relationship": "short description of how the test exercises the behavior (empty string if it does not)", "evidence": [{{"file": "...", "line_start": 1, "line_end": 5, "fact": "..."}}]}}}}

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""


def prove_relationship(
    from_node: str, to_node: str, context: str, *, tools: RepoTools, client: LLMClient,
    max_turns: int, max_response_chars: int, stats,
) -> RecoveredEdge:
    """`prove_relationship(from_node, to_node, context, repo)` per the
    task's own generic signature — `repo` here is `tools`, already bound
    to the repository root; `context` is a short, free-text note about
    where this hop came from (e.g. the candidate path it's part of), never
    a framework hint."""
    prompt = (
        f"FROM: {from_node}\nTO: {to_node}\ncontext: {context}\n\n"
        "Investigate now. Call one tool per turn, or give your final answer when ready."
    )
    try:
        relationship, evidence = run_react_loop(
            client=client, tools=tools, system_prompt=_EDGE_SYSTEM_PROMPT, initial_prompt=prompt,
            max_turns=max_turns, max_response_chars=max_response_chars, stats=stats,
            parse_final=parse_atomic_completion_result,
        )
    except RecoveryError:
        # A failed atomic proof attempt (provider hiccup, malformed turn,
        # ran out of its small turn budget) is "no evidence found" for
        # THIS one relationship, not a fatal error for the whole recovery
        # run — Stage C rejects an empty-evidence edge/test automatically.
        relationship, evidence = "", []
    return RecoveredEdge(**{"from": from_node, "to": to_node}, relationship=relationship or "no relationship found", evidence=evidence)


def prove_test_claim(
    candidate: CandidateTestClaim, context: str, *, tools: RepoTools, client: LLMClient,
    max_turns: int, max_response_chars: int, stats,
) -> RecoveredTest:
    prompt = (
        f"test file: {candidate.file}\ntest: {candidate.test}\nclaimed to cover: {candidate.covers}\n"
        f"context: {context}\n\nInvestigate now. Call one tool per turn, or give your final answer when ready."
    )
    try:
        relationship, evidence = run_react_loop(
            client=client, tools=tools, system_prompt=_TEST_SYSTEM_PROMPT, initial_prompt=prompt,
            max_turns=max_turns, max_response_chars=max_response_chars, stats=stats,
            parse_final=parse_atomic_completion_result,
        )
    except RecoveryError:
        # A failed atomic proof attempt (provider hiccup, malformed turn,
        # ran out of its small turn budget) is "no evidence found" for
        # THIS one relationship, not a fatal error for the whole recovery
        # run — Stage C rejects an empty-evidence edge/test automatically.
        relationship, evidence = "", []
    covers = candidate.covers if not relationship else relationship
    return RecoveredTest(file=candidate.file, test=candidate.test, covers=covers, evidence=evidence)
