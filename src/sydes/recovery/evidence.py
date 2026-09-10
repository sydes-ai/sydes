"""Stage B: atomic evidence completion, entity resolution, and recursive
missing-link decomposition.

Proves or disproves ONE specific relationship at a time — one edge of a
candidate path, or one candidate test — never a whole path. This is the
other half of the discovery/evidence-completion split: Stage A
(`sydes.recovery.discovery`) is free to guess broadly (including proposing
a bare symbol with no known file) because nothing it proposes is trusted;
this stage's only job is to actively search for concrete, inspectable
evidence for exactly one claim, RESOLVE which exact file each side of the
claim actually lives in, and say so honestly (an empty evidence list, or
an unresolved file) when it cannot.

`prove_relationship` and `prove_test_claim` never decide accept/established
themselves — they only gather evidence and resolve identity (or fail to).
Stage C (`sydes.recovery.verify`) independently judges whether the gathered
evidence actually proves the claim AND whether the resolved identities are
consistent (its identity layer) — an edge whose evidence never touches
either entity's own resolved file is rejected there regardless of how
plausible the relationship text reads, which is what makes a same-named
symbol in the wrong module structurally unable to substitute for the real
one.

`decompose_relationship` is the recursive missing-link step: when a direct
proof attempt finds nothing, one separate call asks the agent to name a
small, source-backed chain of intermediate entities, which the caller
(`sydes.recovery.agent`) then proves hop by hop with the same
`prove_relationship` — never a second decomposition on top of the first.

No language- or framework-specific text appears anywhere in this module —
the relationship kinds named in the prompts below are illustrative examples
the model may recognize, never a checklist this code special-cases.
"""

from __future__ import annotations

from sydes.llm.client import LLMClient
from sydes.recovery.react import TOOL_PROMPT_BLOCK, run_react_loop
from sydes.recovery.schema import (
    CandidateTestClaim,
    EntityRef,
    RecoveredEdge,
    RecoveredTest,
    RecoveryError,
    parse_atomic_completion_result,
    parse_decomposition_result,
)
from sydes.recovery.tools import RepoTools


def _describe_entity(entity: EntityRef) -> str:
    parts = [f"symbol: {entity.symbol}"]
    parts.append(f"file: {entity.file or '(unknown -- resolve this)'}")
    if entity.qualified_name:
        parts.append(f"qualified_name: {entity.qualified_name}")
    return ", ".join(parts)


_EDGE_SYSTEM_PROMPT = f"""You are proving or disproving ONE specific relationship between two named repository entities, as one small step of a larger recovery task. You are not responsible for the whole path — only this one relationship. Do not restate or describe the whole path; answer only about the two entities you are given.

You will be told a FROM entity and a TO entity, each with a symbol name and, if already known, a file. Search the repository to determine (a) EXACTLY which file each entity is actually declared in if not already given or if it looks uncertain, and (b) whether the FROM entity is actually connected to the TO entity through real, inspectable source.

Identity matters as much as the relationship itself: if more than one file in the repository declares something with this symbol name, you must determine which ONE is actually relevant here (the one your evidence chain actually connects to) and resolve `to_file`/`from_file` to that exact file — never assume the first same-named match you find is the right one. A same-named symbol in an unrelated file/module is a DIFFERENT entity and must not be used as if it were the one you were asked about.

When a relationship remains unproven after your first look, do not give up immediately — search for both entities' names/types and inspect call sites, registrations, construction sites, annotations/decorators, imports, and configuration that mention both. Runtime dispatch, dependency injection, decorators/annotations, configuration, events, queues, callbacks, and reflection are examples of the KINDS of wiring this might be, not a checklist to apply mechanically. Only after actively investigating should you conclude it cannot be shown.

Rules:
- Evidence must show the SPECIFIC relationship between these two exact entities, not merely that something with a matching name exists somewhere in the codebase, or that they are declared/registered in the same module with nothing tying them together.
- If the relationship goes through an intermediate object or message (a request, command, event, job, or similar) rather than a direct call, evidence must show it flowing from the FROM side to a form the TO side is specifically associated with — not just that the FROM side produces something and the TO side exists in the same area of the codebase.
- Never invent a file, symbol, or line you have not actually read via a tool call in this conversation.
- If you cannot find proof after actively investigating, say so honestly with an empty evidence list (do not force a weak or tangential citation to look sufficient), but still report `to_file`/`from_file` as best you determined them if you found the right declaration, even without a proven relationship.
- A separate step will independently and adversarially re-check whatever evidence you submit, including whether it is actually located at the files you resolved.

{TOOL_PROMPT_BLOCK}

When you are done investigating (or you have used your available turns), respond with your FINAL answer instead, as a single JSON object and nothing else:
{{"final": {{
  "from_file": "the exact repo-relative file where the FROM entity is declared",
  "to_file": "the exact repo-relative file where the TO entity is declared",
  "from_qualified_name": "optional, if you found one",
  "to_qualified_name": "optional, if you found one",
  "relationship": "short description of the specific connection you found (empty string if none)",
  "evidence": [{{"file": "...", "line_start": 1, "line_end": 5, "fact": "short specific statement of what these lines show"}}]
}}}}

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""

_TEST_SYSTEM_PROMPT = f"""You are proving or disproving ONE specific claim: that a named test directly exercises a specific changed behavior (a specific entity, identified by symbol and file). You are not responsible for anything else in this task.

Search the test file (and anything it calls into) to determine whether the test's own code actually calls, constructs, or asserts on THAT SPECIFIC entity — not a same-named symbol declared elsewhere, and not merely that it imports the same module, lives in the same directory, or shares a similar name.

If no target entity was named for you (it will say so explicitly), your first job is to determine, from the test's own code, which specific production entity (symbol and file) it is actually exercising — do not treat the test itself, or anything declared in the test file, as the entity under test. A test's target is always something the test CALLS INTO, never the test method/class itself.

Rules:
- Evidence must show the test's own code invoking or asserting on the changed behavior specifically, not just importing the file it lives in.
- The entity under test must be declared OUTSIDE the test file itself — a test can never be its own target.
- If you are not certain the entity the test touches is the SAME one you were asked about (same file), say so via an empty evidence list rather than assuming a name match is enough.
- Never invent a file, symbol, or line you have not actually read via a tool call in this conversation.
- If you cannot find proof after actively investigating, say so honestly with an empty evidence list — do not force a weak citation to look sufficient.
- A separate step will independently and adversarially re-check whatever evidence you submit.

{TOOL_PROMPT_BLOCK}

When you are done investigating (or you have used your available turns), respond with your FINAL answer instead, as a single JSON object and nothing else:
{{"final": {{
  "to_file": "the exact repo-relative file where the changed entity actually is (confirm or correct what you were given) -- never the test's own file",
  "to_symbol": "the exact symbol of the entity under test, only if none was given to you or you determined it should be corrected -- otherwise leave empty",
  "relationship": "short description of how the test exercises the behavior (empty string if it does not)",
  "evidence": [{{"file": "...", "line_start": 1, "line_end": 5, "fact": "..."}}]
}}}}

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""

_DECOMPOSE_SYSTEM_PROMPT = """A direct relationship between two repository entities could not be proven. Your job now is to find one or more concrete INTERMEDIATE entities that plausibly connect them, each backed by something you can actually point to in source (a name, a type, a file) even if you have not yet proven the connections between them one by one — a later step will prove each hop independently.

Search for intermediate calls, objects, registrations, configuration, callbacks, annotations/decorators, bindings, or other source-backed entities that might sit between the FROM and TO entities you are given. These are examples of the KINDS of things that might bridge a gap, not a checklist to apply mechanically.

Return the SMALLEST chain of intermediate entities you can support — prefer fewer over more, and never invent an entity you have not actually seen in source via a tool call. If you cannot find any plausible, source-backed intermediate, return an empty list rather than guessing.

""" + TOOL_PROMPT_BLOCK + """

When you are done investigating (or you have used your available turns), respond with your FINAL answer instead, as a single JSON object and nothing else:
{"final": {"intermediates": [{"symbol": "...", "file": "...", "qualified_name": "optional"}, ...]}}

An empty `intermediates` array is a complete, honest, valid answer. Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else — no prose outside the JSON."""


def prove_relationship(
    from_entity: EntityRef, to_entity: EntityRef, context: str, *, tools: RepoTools, client: LLMClient,
    max_turns: int, max_response_chars: int, stats,
) -> RecoveredEdge:
    """`prove_relationship(from_node, to_node, context, repo)` per the
    task's own generic signature — `repo` here is `tools`, already bound
    to the repository root; `context` is a short, free-text note about
    where this hop came from (e.g. the candidate path it's part of), never
    a framework hint. Resolves/confirms `from_entity`/`to_entity`'s files
    from what Stage B actually found; a file it could not confirm stays
    empty, which `sydes.recovery.verify`'s identity layer treats as
    automatically unresolved — ambiguity is never silently carried
    forward as if it were resolved.
    """
    prompt = (
        f"FROM entity: {_describe_entity(from_entity)}\n"
        f"TO entity: {_describe_entity(to_entity)}\n"
        f"context: {context}\n\n"
        "Investigate now. Call one tool per turn, or give your final answer when ready."
    )
    try:
        completion = run_react_loop(
            client=client, tools=tools, system_prompt=_EDGE_SYSTEM_PROMPT, initial_prompt=prompt,
            max_turns=max_turns, max_response_chars=max_response_chars, stats=stats,
            parse_final=parse_atomic_completion_result,
        )
    except RecoveryError:
        # A failed atomic proof attempt (provider hiccup, malformed turn,
        # ran out of its small turn budget) is "no evidence found" for
        # THIS one relationship, not a fatal error for the whole recovery
        # run — Stage C rejects an empty-evidence/unresolved-identity edge
        # automatically.
        resolved_from = from_entity
        resolved_to = to_entity
        return RecoveredEdge(**{"from": resolved_from, "to": resolved_to}, relationship="no relationship found", evidence=[])

    resolved_from = from_entity.model_copy(update={
        "file": completion.from_file or from_entity.file,
        "qualified_name": completion.from_qualified_name or from_entity.qualified_name,
    })
    resolved_to = to_entity.model_copy(update={
        "file": completion.to_file or to_entity.file,
        "qualified_name": completion.to_qualified_name or to_entity.qualified_name,
    })
    return RecoveredEdge(
        **{"from": resolved_from, "to": resolved_to},
        relationship=completion.relationship or "no relationship found",
        evidence=completion.evidence,
    )


def prove_test_claim(
    candidate: CandidateTestClaim, target: EntityRef | None, context: str, *, tools: RepoTools, client: LLMClient,
    max_turns: int, max_response_chars: int, stats,
) -> RecoveredTest:
    """`target` is discovery's guess at which entity this test covers --
    Stage A is free to propose none at all (it may not know), in which case
    this asks the agent to first determine the entity from the test's own
    code rather than silently treating the test itself as its own target
    (a real failure mode: naming a fallback target after the test would
    make the test trivially "resolve" to itself)."""
    target_line = (
        f"target entity: {_describe_entity(target)}"
        if target is not None
        else "target entity: none proposed -- determine from the test's own code which production entity (symbol and file, never the test file itself) it actually exercises"
    )
    prompt = (
        f"test file: {candidate.file}\ntest: {candidate.test}\nclaimed to cover: {candidate.covers}\n"
        f"{target_line}\n"
        f"context: {context}\n\nInvestigate now. Call one tool per turn, or give your final answer when ready."
    )
    try:
        completion = run_react_loop(
            client=client, tools=tools, system_prompt=_TEST_SYSTEM_PROMPT, initial_prompt=prompt,
            max_turns=max_turns, max_response_chars=max_response_chars, stats=stats,
            parse_final=parse_atomic_completion_result,
        )
    except RecoveryError:
        fallback_target = target if target is not None else EntityRef(symbol="", file="")
        return RecoveredTest(file=candidate.file, test=candidate.test, covers=candidate.covers, target=fallback_target, evidence=[])

    base_symbol = target.symbol if target is not None else ""
    base_file = target.file if target is not None else ""
    base_qualified_name = target.qualified_name if target is not None else None
    resolved_target = EntityRef(
        symbol=completion.to_symbol or base_symbol,
        file=completion.to_file or base_file,
        qualified_name=completion.to_qualified_name or base_qualified_name,
    )
    covers = candidate.covers if not completion.relationship else completion.relationship
    return RecoveredTest(
        file=candidate.file, test=candidate.test, covers=covers, target=resolved_target, evidence=completion.evidence,
    )


def decompose_relationship(
    from_entity: EntityRef, to_entity: EntityRef, context: str, *, tools: RepoTools, client: LLMClient,
    max_turns: int, max_response_chars: int, stats,
) -> list[EntityRef]:
    """Ask for a small, source-backed chain of intermediate entities
    between `from_entity` and `to_entity` after a direct proof attempt
    found nothing. Returns `[]` (never raises) when nothing plausible is
    found, over budget, on a malformed response, or on provider failure —
    a failed decomposition attempt is exactly as unremarkable as a failed
    direct proof attempt: the caller (`sydes.recovery.agent`) simply keeps
    the original (unresolved) direct edge.
    """
    prompt = (
        f"FROM entity: {_describe_entity(from_entity)}\n"
        f"TO entity: {_describe_entity(to_entity)}\n"
        f"context: {context}\n\n"
        "Investigate now. Call one tool per turn, or give your final answer when ready."
    )
    try:
        return run_react_loop(
            client=client, tools=tools, system_prompt=_DECOMPOSE_SYSTEM_PROMPT, initial_prompt=prompt,
            max_turns=max_turns, max_response_chars=max_response_chars, stats=stats,
            parse_final=parse_decomposition_result,
        )
    except RecoveryError:
        return []
