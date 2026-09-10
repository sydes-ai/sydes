"""Layer 0 (canonical identity), Layer 1 (deterministic evidence), and
Layer 2 (adversarial LLM) verification of AI-recovered paths and tests.

Nothing here validates a whole path (or a whole test) as one blob. Every
edge is judged independently — "all cited files/lines exist" is only the
cheap Layer 1 gate, and neither Layer 1 nor Layer 2 is even reached until
Layer 0 has confirmed the edge is actually ABOUT the entities it claims to
be about.

Layer 0 — canonical entity identity (no LLM call, structural, not a prompt
instruction):
  - both entities must have a resolved (non-empty) `file` — an entity Stage
    B could not pin down to one file is ambiguous and cannot back an
    established edge, no matter how plausible its relationship text reads
  - at least one evidence item must be located in one of the two entities'
    OWN declared files — evidence entirely outside both is not evidence
    ABOUT this edge at all, which is exactly what stops a same-named
    symbol in an unrelated module from substituting for the real one: the
    wrong-module candidate's file never matches what this edge claims to
    be about
  - the edge that reaches a path's `target_node` must additionally resolve
    to one of the diff's own `changed_files` — the changed-target identity
    must connect to the actual changed file, not a same-named lookalike
    anywhere else in the repository

Layer 1 — deterministic, no LLM call:
  - the evidence's file exists and the line range is real
  - the cited text is not empty
  - the cited text actually mentions something from the edge's own
    endpoints or the evidence's own `fact` (a lexical relevance check)

Layer 2 — one adversarial LLM call judging every Layer-0/1 survivor
independently:
  - reject if the evidence only shows both symbols exist
  - reject if both symbols are merely registered/co-located with no
    dispatch/call evidence tying them together
  - reject if a runtime/framework convention is assumed but not evidenced
  - reject if the cited lines are irrelevant to the specific claimed edge
  - accept only when the evidence is sufficient on its own

Path outcome (`_derive_path_outcome`) implements the shortest-sufficient-
path rule exactly as before: `target_node` is the anchor, edges beyond it
are dropped outright, and the first rejected edge before it truncates the
path into a verified prefix (`partial`) rather than failing it wholesale.

Path recovery and test recovery are verified, and their outcomes computed,
completely independently — `verify_paths` and `verify_tests` share no
state and one's outcome never taints the other (see
`sydes.recovery.schema.PathRecoveryResult`/`TestRecoveryResult`).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from sydes.llm.client import LLMClient, LLMClientError, LLMRequest
from sydes.recovery.schema import (
    PROVENANCE_AI_RECOVERY,
    PROVENANCE_AI_RECOVERY_EXHAUSTED,
    PathRecoveryResult,
    RecoveredEdge,
    RecoveredPath,
    RecoveredTest,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    TEST_STATUS_ACCEPTED,
    TEST_STATUS_REJECTED,
    TestRecoveryResult,
    recompute_path_status,
    recompute_test_status,
)
from sydes.recovery.tools import RepoTools

if TYPE_CHECKING:
    from sydes.recovery.agent import RecoveryRunStats

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_EDGE_VERIFIER_SYSTEM_PROMPT = """You are an adversarial verifier for AI-recovered code-relationship edges. You did not propose these edges; your job is to try to break each one, not to agree with it.

Judge EACH edge independently, in isolation from the others. For each edge, you are told the FROM entity, the TO entity, the claimed relationship, and the cited evidence. Ask:

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


# ---------------------------------------------------------------------------
# Layer 0 -- canonical entity identity. Deterministic, no LLM call. This is
# the structural fix for "a same-named symbol in another module was used as
# evidence" -- it is checked BEFORE Layer 1/2 ever run, so no amount of a
# confident-sounding relationship description or adversarial-verifier
# accept can override it.
# ---------------------------------------------------------------------------


def _identity_layer(edge: RecoveredEdge, *, changed_files: frozenset[str], target_symbol: str | None) -> str | None:
    """Returns a rejection reason, or `None` if the edge's entities are
    unambiguously identified and its evidence is actually located at (at
    least one of) them.

    `target_symbol`, when given, means this edge is the one reaching a
    path's `target_node`: its `to_entity` must ALSO resolve to one of the
    diff's own `changed_files` -- the changed-target identity must connect
    to the real changed file, not a same-named lookalike anywhere else.
    """
    if not edge.from_entity.file:
        return f"source entity {edge.from_entity.symbol!r} has no resolved file -- ambiguous identity, cannot establish"
    if not edge.to_entity.file:
        return f"target entity {edge.to_entity.symbol!r} has no resolved file -- ambiguous identity, cannot establish"

    cited_files = {item.file for item in edge.evidence}
    if edge.from_entity.file not in cited_files and edge.to_entity.file not in cited_files:
        return (
            f"no evidence is located in either entity's own declared file "
            f"({edge.from_entity.file!r} or {edge.to_entity.file!r}) -- possible wrong-file/wrong-module substitution"
        )

    if target_symbol is not None and edge.to_entity.symbol == target_symbol and changed_files:
        if edge.to_entity.file not in changed_files:
            return (
                f"target entity {edge.to_entity.symbol!r} resolved to {edge.to_entity.file!r}, which is not "
                f"one of the diff's actually-changed files ({sorted(changed_files)}) -- this looks like a "
                f"same-named symbol in the wrong module, not the real changed behavior"
            )
    return None


def _identity_layer_test(test: RecoveredTest, *, changed_files: frozenset[str]) -> str | None:
    if not test.target.file:
        return f"target entity {test.target.symbol!r} has no resolved file -- ambiguous identity, cannot accept"
    if changed_files and test.target.file not in changed_files:
        return (
            f"target entity {test.target.symbol!r} resolved to {test.target.file!r}, which is not one of the "
            f"diff's actually-changed files ({sorted(changed_files)}) -- this looks like a same-named symbol "
            f"in the wrong module, not the real changed behavior"
        )
    return None


def _evidence_layer1(evidence, endpoints: tuple[str, ...], tools: RepoTools) -> str | None:
    """Returns a rejection reason, or `None` if every evidence citation is
    real and lexically relevant. `endpoints` are the symbol names whose
    presence in the cited text is the relevance signal."""
    if not evidence:
        return "no evidence cited at all"
    endpoint_tokens = set()
    for endpoint in endpoints:
        endpoint_tokens.update(t.lower() for t in _IDENTIFIER_RE.findall(endpoint))

    for item in evidence:
        content = tools.read_file(item.file, start_line=item.line_start, end_line=item.line_end)
        if content.startswith("ERROR:"):
            return f"cited evidence file {item.file!r} could not be read: {content}"
        if not content.strip():
            return f"cited evidence file {item.file!r} was empty at the claimed location"
        cited_tokens = {t.lower() for t in _IDENTIFIER_RE.findall(content)}
        fact_tokens = {t.lower() for t in _IDENTIFIER_RE.findall(item.fact)}
        if endpoint_tokens and not (endpoint_tokens & cited_tokens):
            if not (fact_tokens & cited_tokens):
                return (
                    f"cited lines in {item.file!r} do not mention any of the claimed "
                    f"symbols or the stated fact -- evidence appears unrelated"
                )
    return None


def _build_edge_verifier_prompt(edges: list[RecoveredEdge]) -> str:
    lines = ["Edges to verify (judge each independently):"]
    for index, edge in enumerate(edges):
        lines.append(f"\n[{index}] FROM: {edge.from_entity.symbol} ({edge.from_entity.file})  TO: {edge.to_entity.symbol} ({edge.to_entity.file})")
        lines.append(f"  claimed relationship: {edge.relationship}")
        for item in edge.evidence:
            lines.append(f"  evidence: {item.file}:{item.line_start}-{item.line_end} -- {item.fact}")
    return "\n".join(lines)


def _build_test_verifier_prompt(tests: list[RecoveredTest]) -> str:
    lines = ["Recovered tests to verify (judge each independently):"]
    for index, test in enumerate(tests):
        lines.append(f"\n[{index}] test: {test.test}  file: {test.file}")
        lines.append(f"  claimed target: {test.target.symbol} ({test.target.file})")
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
    all_edges: list[RecoveredEdge], *, target_symbols: list[str | None], changed_files: frozenset[str],
    tools: RepoTools, client: LLMClient, stats: "RecoveryRunStats",
) -> list[RecoveredEdge]:
    """Verify every edge across every path in one batch. `target_symbols[i]`
    is the path's `target_node` symbol when `all_edges[i]` is the edge
    reaching it, else `None` -- see `_identity_layer`."""
    if not all_edges:
        return []

    layer0_rejections: dict[int, str] = {}
    layer1_rejections: dict[int, str] = {}
    survivors: list[tuple[int, RecoveredEdge]] = []
    for i, edge in enumerate(all_edges):
        reason0 = _identity_layer(edge, changed_files=changed_files, target_symbol=target_symbols[i])
        if reason0 is not None:
            layer0_rejections[i] = reason0
            continue
        reason1 = _evidence_layer1(edge.evidence, (edge.from_entity.symbol, edge.to_entity.symbol), tools)
        if reason1 is not None:
            layer1_rejections[i] = reason1
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
        if i in layer0_rejections:
            verified.append(edge.model_copy(update={
                "status": STATUS_UNRESOLVED, "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                "rejection_reason": layer0_rejections[i],
            }))
        elif i in layer1_rejections:
            verified.append(edge.model_copy(update={
                "status": STATUS_UNRESOLVED, "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                "rejection_reason": layer1_rejections[i],
            }))
        else:
            accept, reason = llm_verdicts.get(i, (False, "no verifier verdict recorded"))
            if accept:
                verified.append(edge.model_copy(update={"status": STATUS_ESTABLISHED, "provenance": PROVENANCE_AI_RECOVERY}))
            else:
                verified.append(edge.model_copy(update={
                    "status": STATUS_UNRESOLVED, "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                    "rejection_reason": reason or "verifier rejected this edge",
                }))
    return verified


def _derive_path_outcome(path: RecoveredPath, verified_edges: list[RecoveredEdge]) -> RecoveredPath:
    """The shortest-sufficient-path rule: `target_node` is the anchor.

    Edges beyond the point where the chain reaches `target_node` are pure
    padding and are dropped from the reported path entirely, regardless of
    their own verdict. Edges strictly needed to reach `target_node` are
    walked in order; the first rejected edge truncates the path there.
    """
    node_order = [path.nodes[0].symbol] + [e.to_entity.symbol for e in verified_edges]
    try:
        target_index = node_order.index(path.target_node)
    except ValueError:
        target_index = len(node_order) - 1  # defensive; construction already guarantees target_node is a node

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


def verify_paths(
    paths: list[RecoveredPath], *, changed_files: frozenset[str], tools: RepoTools, client: LLMClient,
    stats: "RecoveryRunStats",
) -> PathRecoveryResult:
    """Stage C for path recovery only -- entirely independent of test
    verification (see `verify_tests`). Never raises: a verifier-call
    failure is treated as a reject (fail closed), never a fatal error."""
    all_edges: list[RecoveredEdge] = []
    target_symbols: list[str | None] = []
    for path in paths:
        for edge in path.edges:
            all_edges.append(edge)
            target_symbols.append(path.target_node if edge.to_entity.symbol == path.target_node else None)

    verified_edges = _verify_edges(
        all_edges, target_symbols=target_symbols, changed_files=changed_files, tools=tools, client=client, stats=stats,
    )

    new_paths: list[RecoveredPath] = []
    cursor = 0
    for path in paths:
        n = len(path.edges)
        path_edges = verified_edges[cursor : cursor + n]
        cursor += n
        new_paths.append(_derive_path_outcome(path, path_edges))

    return recompute_path_status(PathRecoveryResult(paths=new_paths))


def verify_tests(
    tests: list[RecoveredTest], *, changed_files: frozenset[str], tools: RepoTools, client: LLMClient,
    stats: "RecoveryRunStats",
) -> TestRecoveryResult:
    """Stage C for test recovery only -- entirely independent of path
    verification (see `verify_paths`). A test whose target identity does
    not resolve to one of the diff's own changed files is rejected at
    Layer 0, the same guard path edges get."""
    if not tests:
        return TestRecoveryResult()

    layer0_rejections: dict[int, str] = {}
    layer1_rejections: dict[int, str] = {}
    survivors: list[tuple[int, RecoveredTest]] = []
    for i, test in enumerate(tests):
        reason0 = _identity_layer_test(test, changed_files=changed_files)
        if reason0 is not None:
            layer0_rejections[i] = reason0
            continue
        reason1 = _evidence_layer1(test.evidence, (test.test, test.covers, test.target.symbol), tools)
        if reason1 is not None:
            layer1_rejections[i] = reason1
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
        if i in layer0_rejections:
            verified.append(test.model_copy(update={"status": TEST_STATUS_REJECTED, "rejection_reason": layer0_rejections[i]}))
        elif i in layer1_rejections:
            verified.append(test.model_copy(update={"status": TEST_STATUS_REJECTED, "rejection_reason": layer1_rejections[i]}))
        else:
            accept, reason = llm_verdicts.get(i, (False, "no verifier verdict recorded"))
            if accept:
                verified.append(test.model_copy(update={"status": TEST_STATUS_ACCEPTED, "rejection_reason": None}))
            else:
                verified.append(test.model_copy(update={"status": TEST_STATUS_REJECTED, "rejection_reason": reason or "verifier rejected this test"}))

    return recompute_test_status(TestRecoveryResult(tests=verified))
