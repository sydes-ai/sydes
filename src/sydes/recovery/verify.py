"""Edge-level adversarial verification of AI-recovered paths and tests.

Nothing here validates a whole path (or a whole test) as one blob. Every
edge is judged independently — "all cited files/lines exist" is only the
cheap Layer 1 gate; Layer 2 asks, for each edge on its own, whether the
cited evidence actually PROVES that specific relationship, not merely that
both endpoints exist or happen to be co-located.

Layer 1 — deterministic, no LLM call:
  - the evidence's file exists and the line range is real
  - the cited text is not empty
  - the cited text actually mentions something from the edge's own
    endpoints or the evidence's own `fact` — a purely lexical relevance
    check (shared identifiers), not semantic understanding, but enough to
    reject an evidence item that cites unrelated code outright

Layer 2 — one adversarial LLM call judging every edge independently:
  - reject if the evidence only shows both symbols exist
  - reject if both symbols are merely registered/co-located in the same
    module with no dispatch/call evidence tying them together
  - reject if a runtime/framework convention is assumed but not evidenced
  - reject if the cited lines are irrelevant to the specific claimed edge
  - accept only when the evidence is sufficient on its own

Path outcome (`_derive_path_outcome`) then implements the
shortest-sufficient-path rule: `target_node` is the anchor. Edges beyond
it are dropped outright (pure padding, not even reported). Edges needed to
reach it are walked in order; the first rejected edge truncates the
path — the surviving prefix becomes the reported path, and reaching (or
not reaching) `target_node` decides `established` vs `partial` vs
`unresolved`.

Test verification applies the identical evidence rigor: a recovered test is
accepted only when its own evidence shows it actually calls/exercises the
changed behavior, not merely imports the same module or shares a similar
name.

`sydes.recovery.agent.recover` allows at most one retry after a rejection
here, then one more verification pass — never an unbounded loop.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from sydes.llm.client import LLMClient, LLMClientError, LLMRequest
from sydes.recovery.schema import (
    PROVENANCE_AI_RECOVERY,
    PROVENANCE_AI_RECOVERY_EXHAUSTED,
    RecoveredEdge,
    RecoveredPath,
    RecoveredTest,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    recompute_overall_status,
)
from sydes.recovery.tools import RepoTools

if TYPE_CHECKING:
    from sydes.recovery.agent import RecoveryRunStats

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_EDGE_VERIFIER_SYSTEM_PROMPT = """You are an adversarial verifier for AI-recovered code-relationship edges. You did not propose these edges; your job is to try to break each one, not to agree with it.

Judge EACH edge independently, in isolation from the others. For each edge, you are told the FROM symbol, the TO symbol, the claimed relationship, and the cited evidence. Ask:

Does the cited repository evidence actually establish the claimed relationship from FROM to TO?

Reject an edge if:
- the evidence only shows both symbols exist
- both symbols are merely registered or declared in the same module/file, with no dispatch or call evidence actually tying them together
- a framework or runtime convention is assumed but not evidenced in the cited lines
- the relationship is plausible but not proven
- the cited lines are irrelevant to this specific edge
- runtime dispatch between FROM and TO is asserted without evidence that actually connects the source and target (e.g. evidence showing a message/request type is constructed by one side is not enough without evidence the other side is specifically associated with that same type)

Accept only when the evidence is clearly sufficient on its own to prove this specific edge.

Respond with a single JSON object and nothing else:
{"verdicts": [{"index": 0, "accept": true, "reason": "one sentence"}, {"index": 1, "accept": false, "reason": "one sentence naming exactly what is missing or merely assumed"}]}"""

_TEST_VERIFIER_SYSTEM_PROMPT = """You are an adversarial verifier for AI-recovered test mappings. Your job is to try to break each claim, not to agree with it.

For EACH recovered test, judge: does the cited evidence show this test actually CALLS or EXERCISES the changed behavior -- not merely that it imports the same module, shares a similar name, or lives in the same directory?

Reject if:
- the evidence only shows an import or a file-location coincidence
- the evidence shows the test file changed but not that this specific test invokes the changed code
- the claim is plausible from naming alone but the cited lines do not show an actual call/assertion touching the changed behavior

Accept only when the cited evidence shows a direct invocation of (or assertion about) the changed behavior.

Respond with a single JSON object and nothing else:
{"verdicts": [{"index": 0, "accept": true, "reason": "one sentence"}, {"index": 1, "accept": false, "reason": "one sentence"}]}"""


def _evidence_layer1(edge_or_test_evidence, endpoints: tuple[str, ...], tools: RepoTools) -> str | None:
    """Returns a rejection reason, or `None` if every evidence citation is
    real and lexically relevant. `endpoints` are the symbol names (or, for
    a test, just the test name/covers text) whose presence in the cited
    text is the relevance signal."""
    if not edge_or_test_evidence:
        return "no evidence cited at all"
    endpoint_tokens = set()
    for endpoint in endpoints:
        endpoint_tokens.update(t.lower() for t in _IDENTIFIER_RE.findall(endpoint))

    for item in edge_or_test_evidence:
        content = tools.read_file(item.file, start_line=item.line_start, end_line=item.line_end)
        if content.startswith("ERROR:"):
            return f"cited evidence file {item.file!r} could not be read: {content}"
        if not content.strip():
            return f"cited evidence file {item.file!r} was empty at the claimed location"
        cited_tokens = {t.lower() for t in _IDENTIFIER_RE.findall(content)}
        fact_tokens = {t.lower() for t in _IDENTIFIER_RE.findall(item.fact)}
        # Relevance: either an endpoint symbol or a token from the model's
        # own stated `fact` must actually appear in the cited source —
        # otherwise this citation could be evidence for anything.
        if endpoint_tokens and not (endpoint_tokens & cited_tokens):
            if not (fact_tokens & cited_tokens):
                return (
                    f"cited lines in {item.file!r} do not mention any of the claimed "
                    f"symbols or the stated fact — evidence appears unrelated"
                )
    return None


def _build_edge_verifier_prompt(edges: list[RecoveredEdge]) -> str:
    lines = ["Edges to verify (judge each independently):"]
    for index, edge in enumerate(edges):
        lines.append(f"\n[{index}] FROM: {edge.from_symbol}  TO: {edge.to_symbol}")
        lines.append(f"  claimed relationship: {edge.relationship}")
        for item in edge.evidence:
            lines.append(f"  evidence: {item.file}:{item.line_start}-{item.line_end} -- {item.fact}")
    return "\n".join(lines)


def _build_test_verifier_prompt(tests: list[RecoveredTest]) -> str:
    lines = ["Recovered tests to verify (judge each independently):"]
    for index, test in enumerate(tests):
        lines.append(f"\n[{index}] test: {test.test}  file: {test.file}")
        lines.append(f"  claimed to cover: {test.covers}")
        for item in test.evidence:
            lines.append(f"  evidence: {item.file}:{item.line_start}-{item.line_end} -- {item.fact}")
    return "\n".join(lines)


def _parse_verdicts(text: str, *, count: int) -> dict[int, tuple[bool, str]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    raw_verdicts = payload.get("verdicts")
    if not isinstance(raw_verdicts, list):
        return {}
    out: dict[int, tuple[bool, str]] = {}
    for item in raw_verdicts:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        accept = item.get("accept")
        reason = item.get("reason", "")
        if not isinstance(index, int) or index < 0 or index >= count or not isinstance(accept, bool):
            continue
        out[index] = (accept, reason if isinstance(reason, str) else "")
    return out


def _verify_edges(
    all_edges: list[RecoveredEdge], *, tools: RepoTools, client: LLMClient, stats: "RecoveryRunStats",
) -> list[RecoveredEdge]:
    """Verify every edge across every path in one batch: Layer 1 first
    (no LLM call, catches fabricated/irrelevant citations), then one LLM
    call judging only the Layer-1 survivors, each in isolation."""
    if not all_edges:
        return []

    layer1_rejections: dict[int, str] = {}
    survivors: list[tuple[int, RecoveredEdge]] = []
    for i, edge in enumerate(all_edges):
        reason = _evidence_layer1(edge.evidence, (edge.from_symbol, edge.to_symbol), tools)
        if reason is not None:
            layer1_rejections[i] = reason
        else:
            survivors.append((i, edge))

    llm_verdicts: dict[int, tuple[bool, str]] = {}
    if survivors:
        prompt = _build_edge_verifier_prompt([e for _, e in survivors])
        try:
            response = client.generate(
                LLMRequest(prompt=prompt, system=_EDGE_VERIFIER_SYSTEM_PROMPT, temperature=None)
            )
        except LLMClientError:
            llm_verdicts = {}  # fail closed: a verifier that could not run is not tacit acceptance
        else:
            stats.llm_calls += 1
            if response.usage:
                stats.prompt_tokens += response.usage.get("prompt_tokens", 0)
                stats.completion_tokens += response.usage.get("completion_tokens", 0)
            parsed = _parse_verdicts(response.text, count=len(survivors))
            for local_index, (original_index, _edge) in enumerate(survivors):
                llm_verdicts[original_index] = parsed.get(
                    local_index, (False, "verifier gave no explicit verdict for this edge")
                )

    verified: list[RecoveredEdge] = []
    for i, edge in enumerate(all_edges):
        if i in layer1_rejections:
            verified.append(
                edge.model_copy(update={
                    "status": STATUS_UNRESOLVED, "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                    "rejection_reason": layer1_rejections[i],
                })
            )
            continue
        accept, reason = llm_verdicts.get(i, (False, "no verifier verdict recorded"))
        if accept:
            verified.append(edge.model_copy(update={"status": STATUS_ESTABLISHED, "provenance": PROVENANCE_AI_RECOVERY}))
        else:
            verified.append(
                edge.model_copy(update={
                    "status": STATUS_UNRESOLVED, "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                    "rejection_reason": reason or "verifier rejected this edge",
                })
            )
    return verified


def _derive_path_outcome(path: RecoveredPath, verified_edges: list[RecoveredEdge]) -> RecoveredPath:
    """The shortest-sufficient-path rule: `target_node` is the anchor.

    Edges beyond the point where the chain reaches `target_node` are pure
    padding and are dropped from the reported path entirely, regardless of
    their own verdict — reaching the target is what the path exists to
    prove, and anything past it was never needed. Edges strictly needed to
    reach `target_node` are walked in order; the first rejected edge
    truncates the path there.
    """
    node_order = [path.nodes[0].symbol] + [e.to_symbol for e in verified_edges]
    try:
        target_index = node_order.index(path.target_node)
    except ValueError:
        target_index = len(node_order) - 1  # defensive; parsing already guarantees target_node is a node

    edges_to_target = verified_edges[:target_index]
    kept_edges: list[RecoveredEdge] = []
    for edge in edges_to_target:
        if edge.status != STATUS_ESTABLISHED:
            break
        kept_edges.append(edge)

    reached_target = len(kept_edges) == len(edges_to_target)
    kept_node_count = len(kept_edges) + 1
    kept_nodes = path.nodes[:kept_node_count]

    if reached_target:
        status = STATUS_ESTABLISHED
        unresolved_suffix: list[RecoveredEdge] = []
    elif kept_edges:
        status = STATUS_PARTIAL
        unresolved_suffix = edges_to_target[len(kept_edges):]
    else:
        status = STATUS_UNRESOLVED
        unresolved_suffix = edges_to_target

    return path.model_copy(update={
        "nodes": kept_nodes, "edges": kept_edges, "status": status, "unresolved_suffix": unresolved_suffix,
    })


def _verify_tests(
    tests: list[RecoveredTest], *, tools: RepoTools, client: LLMClient, stats: "RecoveryRunStats",
) -> list[RecoveredTest]:
    if not tests:
        return []

    layer1_rejections: dict[int, str] = {}
    survivors: list[tuple[int, RecoveredTest]] = []
    for i, test in enumerate(tests):
        reason = _evidence_layer1(test.evidence, (test.test, test.covers), tools)
        if reason is not None:
            layer1_rejections[i] = reason
        else:
            survivors.append((i, test))

    llm_verdicts: dict[int, tuple[bool, str]] = {}
    if survivors:
        prompt = _build_test_verifier_prompt([t for _, t in survivors])
        try:
            response = client.generate(
                LLMRequest(prompt=prompt, system=_TEST_VERIFIER_SYSTEM_PROMPT, temperature=None)
            )
        except LLMClientError:
            llm_verdicts = {}
        else:
            stats.llm_calls += 1
            if response.usage:
                stats.prompt_tokens += response.usage.get("prompt_tokens", 0)
                stats.completion_tokens += response.usage.get("completion_tokens", 0)
            parsed = _parse_verdicts(response.text, count=len(survivors))
            for local_index, (original_index, _test) in enumerate(survivors):
                llm_verdicts[original_index] = parsed.get(
                    local_index, (False, "verifier gave no explicit verdict for this test")
                )

    verified: list[RecoveredTest] = []
    for i, test in enumerate(tests):
        if i in layer1_rejections:
            verified.append(test.model_copy(update={"status": "rejected", "rejection_reason": layer1_rejections[i]}))
            continue
        accept, reason = llm_verdicts.get(i, (False, "no verifier verdict recorded"))
        if accept:
            verified.append(test.model_copy(update={"status": "accepted", "rejection_reason": None}))
        else:
            verified.append(test.model_copy(update={"status": "rejected", "rejection_reason": reason or "verifier rejected this test"}))
    return verified


def verify_recovery_result(
    draft: RecoveryResult, *, tools: RepoTools, client: LLMClient, stats: "RecoveryRunStats",
) -> RecoveryResult:
    all_edges = [edge for path in draft.recovered_paths for edge in path.edges]
    verified_edges = _verify_edges(all_edges, tools=tools, client=client, stats=stats)

    new_paths: list[RecoveredPath] = []
    cursor = 0
    for path in draft.recovered_paths:
        n = len(path.edges)
        path_edges = verified_edges[cursor : cursor + n]
        cursor += n
        new_paths.append(_derive_path_outcome(path, path_edges))

    verified_tests = _verify_tests(draft.recovered_tests, tools=tools, client=client, stats=stats)

    result = draft.model_copy(update={"recovered_paths": new_paths, "recovered_tests": verified_tests})
    return recompute_overall_status(result)
