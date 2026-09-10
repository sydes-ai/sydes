"""Structured, strictly-parsed recovery output — edge-level proof, not
whole-path claims.

A path is a chain of nodes connected by edges, and EVERY edge carries its
own status/evidence/provenance. This is deliberate: "all cited files/lines
exist" is not the bar for calling a path established — each edge must be
independently supported by evidence that actually proves that specific
relationship (see `sydes.recovery.verify`, which judges edges one at a
time, never a whole path as one blob).

`target_node` names which node in the chain is the actually-changed
behavior — not necessarily the last node the agent proposed. This is what
lets `sydes.recovery.verify` implement the shortest-sufficient-path rule:
once the chain reaches `target_node`, anything the agent appended beyond it
is padding, dropped outright rather than merely marked unresolved.

Mirrors the fail-closed philosophy of `sydes.impact.guide`: the agent's
`LLMClient` has no structured-output mode, so the contract is "ask for JSON
in the prompt, then parse and validate strictly." Anything that does not
parse or does not match the schema raises `RecoveryError` rather than
being coerced into a best-effort guess.

Nothing in this module writes into `ChangeVerificationResult` or the CBM
graph — see `sydes.recovery.merge` for the read-only, additive view built
from a `RecoveryResult`.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

STATUS_ESTABLISHED = "established"
STATUS_PARTIAL = "partial"
STATUS_UNRESOLVED = "unresolved"
_VALID_PATH_STATUSES = (STATUS_ESTABLISHED, STATUS_PARTIAL, STATUS_UNRESOLVED)
_VALID_EDGE_STATUSES = (STATUS_ESTABLISHED, STATUS_UNRESOLVED)
_VALID_TEST_STATUSES = ("accepted", "rejected")

#: An AI-recovered path/edge/test that repository evidence supports.
PROVENANCE_AI_RECOVERY = "ai_recovery"
#: A path/edge/test the agent could not establish even after the retry
#: budget — distinct from never having tried; see `sydes.recovery.agent`.
PROVENANCE_AI_RECOVERY_EXHAUSTED = "ai_recovery_exhausted"

#: Response text considered for JSON extraction, mirroring
#: `sydes.impact.guide._MAX_RESPONSE_CHARS` — a reasoning-heavy response
#: that wanders past this is treated as malformed rather than scanned
#: indefinitely for a stray brace.
_MAX_RESPONSE_CHARS = 16000


class RecoveryError(RuntimeError):
    """The recovery agent could not produce a usable structured result.

    Covers provider failure, non-JSON output, and a missing/invalid field.
    The caller's only correct response is to record the failure and leave
    the first-pass result exactly as it was — never to substitute a
    guessed recovery.
    """


class RecoveredEvidence(BaseModel):
    """One inspectable, re-readable fact backing a recovered claim.

    `fact` is a short, specific statement of what the cited lines show
    (e.g. "constructs the query object and dispatches it to the bus" or
    "decorator registers this handler for that message type") — never a
    restatement of the bridge it is supposed to support ("this connects A
    to B" is not evidence, it is the claim).
    """

    file: str
    line_start: int | None = None
    line_end: int | None = None
    fact: str


class RecoveredNode(BaseModel):
    """One point in a recovered path."""

    symbol: str
    file: str


class RecoveredEdge(BaseModel):
    """One hop in a recovered path, judged entirely on its own — never as
    part of a whole-path blob. `from_symbol`/`to_symbol` must each name a
    node already listed in the owning path's `nodes` (enforced at parse
    time, not just by convention).

    `status`/`provenance` reflect the OUTCOME of verification, not the
    agent's own claim: the agent proposes `relationship` and `evidence`;
    `sydes.recovery.verify` decides `status`/`provenance` independently
    (Layer 1 deterministic check, then Layer 2 adversarial LLM judgment per
    edge) and this model is then rebuilt with the verified values.
    """

    from_symbol: str = Field(alias="from")
    to_symbol: str = Field(alias="to")
    relationship: str
    evidence: list[RecoveredEvidence] = Field(default_factory=list)
    status: str = STATUS_UNRESOLVED
    provenance: str = PROVENANCE_AI_RECOVERY_EXHAUSTED
    rejection_reason: str | None = None

    model_config = {"populate_by_name": True}


class RecoveredPath(BaseModel):
    """One recovered entrypoint-to-changed-behavior path, expressed as a
    chain of nodes/edges rather than a single all-or-nothing claim.

    `target_node` is the symbol (must match one entry in `nodes`) that is
    the actually-changed behavior this path exists to reach — the anchor
    the shortest-sufficient-path rule truncates around, in either
    direction: edges needed to reach it are kept and judged; anything the
    agent appended past it is dropped outright (see
    `sydes.recovery.verify._derive_path_outcome`).

    `status` here is the FINAL, post-verification outcome:
    `established` (the full chain to `target_node` is edge-proven),
    `partial` (a real prefix was proven but a required edge before
    `target_node` was rejected), or `unresolved` (nothing survived).
    `unresolved_suffix` names exactly the edges that would have been needed
    to reach `target_node` but were not proven — never edges beyond
    `target_node`, which are simply not part of the reported path at all.
    """

    entrypoint: str
    target_node: str
    nodes: list[RecoveredNode] = Field(default_factory=list)
    edges: list[RecoveredEdge] = Field(default_factory=list)
    status: str = STATUS_UNRESOLVED
    unresolved_suffix: list[RecoveredEdge] = Field(default_factory=list)


class RecoveredTest(BaseModel):
    """A test the agent found that directly exercises the changed behavior
    — not merely a test file that happens to have changed, imports the
    same module, or shares a similar name. `status`/`rejection_reason` are
    set by `sydes.recovery.verify`'s test-evidence pass, the same way an
    edge's are — the agent's own claim is not the final word."""

    file: str
    test: str
    covers: str
    evidence: list[RecoveredEvidence] = Field(default_factory=list)
    status: str = "rejected"
    rejection_reason: str | None = None


class CorrectedFirstPassClaim(BaseModel):
    """The agent found source evidence contradicting, not just extending,
    the first pass's own hypothesis (e.g. CBM implied `A -> B -> ?` but
    source shows `A -> C -> D`)."""

    claim: str
    original: str
    corrected: str
    evidence: list[RecoveredEvidence] = Field(default_factory=list)


class UnresolvedQuestion(BaseModel):
    """One thing the agent could not establish, and exactly what evidence
    would be needed to establish it — never a vague "insufficient
    context"."""

    question: str
    missing_evidence: str


class CandidatePath(BaseModel):
    """Stage A's (discovery) output: a HYPOTHESIS only — a plausible chain
    of symbol names from an entrypoint to the changed behavior, with no
    evidence-citation responsibility at all. `nodes` are bare symbol names
    (discovery is not required to know which file a name lives in; Stage B
    — `sydes.recovery.evidence` — finds that out while proving each hop).
    Never trusted on its own: every adjacent pair is independently proven
    by Stage B and judged by Stage C (`sydes.recovery.verify`) before any
    of this becomes a `RecoveredPath`."""

    entrypoint: str
    target_node: str
    nodes: list[str] = Field(default_factory=list)


class CandidateTestClaim(BaseModel):
    """Stage A's hypothesis that some test exercises the changed behavior
    — no evidence yet; Stage B proves or disproves it the same way it does
    a path edge."""

    file: str
    test: str
    covers: str


def _parse_candidate_path(raw: Any) -> CandidatePath:
    if not isinstance(raw, dict):
        raise RecoveryError("'candidate_path' must be an object")
    entrypoint = raw.get("entrypoint")
    target_node = raw.get("target_node") or raw.get("target")
    raw_nodes = raw.get("nodes")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise RecoveryError("'candidate_path' missing non-empty 'entrypoint'")
    if not isinstance(target_node, str) or not target_node.strip():
        raise RecoveryError("'candidate_path' missing non-empty 'target_node'")
    if not isinstance(raw_nodes, list) or len(raw_nodes) < 2:
        raise RecoveryError("'candidate_path.nodes' must be an array of at least 2 symbol names")
    nodes = [n.strip() for n in raw_nodes if isinstance(n, str) and n.strip()]
    if len(nodes) != len(raw_nodes):
        raise RecoveryError("'candidate_path.nodes' entries must all be non-empty strings")
    target_node = target_node.strip()
    if target_node not in nodes:
        raise RecoveryError(f"'candidate_path.target_node' {target_node!r} is not among 'nodes'")
    if target_node == nodes[0]:
        raise RecoveryError("'candidate_path.target_node' cannot be the entrypoint node (nodes[0])")
    return CandidatePath(entrypoint=entrypoint.strip(), target_node=target_node, nodes=nodes)


def _parse_candidate_test(raw: Any, *, index: int) -> CandidateTestClaim:
    if not isinstance(raw, dict):
        raise RecoveryError(f"candidate_tests[{index}] must be an object")
    file = raw.get("file")
    test = raw.get("test")
    covers = raw.get("covers")
    if not all(isinstance(v, str) and v.strip() for v in (file, test, covers)):
        raise RecoveryError(f"candidate_tests[{index}] requires non-empty file/test/covers")
    return CandidateTestClaim(file=file.strip(), test=test.strip(), covers=covers.strip())


def parse_discovery_result(text: str) -> tuple[CandidatePath | None, list[CandidateTestClaim]]:
    """Parse Stage A's response: `{"candidate_path": {...} | null,
    "candidate_tests": [...]}`.

    Both a malformed individual candidate test AND a malformed
    `candidate_path` are dropped (treated as "no candidate proposed"),
    never fatal to the whole discovery response — partial recovery over
    all-or-nothing, the same tolerance `parse_recovery_result` applies to
    the final answer. This matters here specifically because discovery is
    the one stage a bad response cannot be repaired downstream: Stage B has
    nothing to prove without *some* candidate, so a malformed
    `candidate_path` degrading to "propose no path this attempt" (which the
    agent is separately told is a valid, honest answer) is strictly better
    than crashing the entire run before Stage B or the retry budget ever
    gets a chance to run — observed in practice when a discovery response
    proposed a degenerate single-node "path" instead of honestly using
    `null`, exactly the shortcut the prompt tells it not to take.
    """
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise RecoveryError("discovery output was not a single JSON object")

    raw_path = payload.get("candidate_path")
    path: CandidatePath | None = None
    if raw_path is not None:
        try:
            path = _parse_candidate_path(raw_path)
        except RecoveryError:
            path = None

    raw_tests = payload.get("candidate_tests", [])
    if not isinstance(raw_tests, list):
        raise RecoveryError("'candidate_tests' must be a JSON array")
    tests: list[CandidateTestClaim] = []
    for i, item in enumerate(raw_tests):
        try:
            tests.append(_parse_candidate_test(item, index=i))
        except RecoveryError:
            continue  # one malformed candidate test does not sink discovery

    return path, tests


class RecoveryResult(BaseModel):
    """The complete, strict output of one recovery run.

    Overall `status`: `established` iff at least one path is fully
    established; else `partial` iff at least one path has a real (even if
    incomplete) verified prefix; else `unresolved`.
    """

    status: str = STATUS_UNRESOLVED
    recovered_paths: list[RecoveredPath] = Field(default_factory=list)
    recovered_tests: list[RecoveredTest] = Field(default_factory=list)
    corrected_first_pass_claims: list[CorrectedFirstPassClaim] = Field(default_factory=list)
    unresolved: list[UnresolvedQuestion] = Field(default_factory=list)


def _extract_json_object(text: str) -> Any | None:
    """Best-effort parse for a single JSON object in model output — the
    same tolerant-of-fences, strict-about-shape extraction
    `sydes.impact.guide._extract_json_object` uses."""
    stripped = text.strip()[:_MAX_RESPONSE_CHARS]
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None


def _parse_evidence_list(raw: Any, *, context: str) -> list[RecoveredEvidence]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RecoveryError(f"{context}: 'evidence' must be a JSON array")
    out: list[RecoveredEvidence] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RecoveryError(f"{context}: evidence entry must be an object")
        file = item.get("file")
        fact = item.get("fact")
        if not isinstance(file, str) or not file.strip():
            raise RecoveryError(f"{context}: evidence entry missing non-empty 'file'")
        if not isinstance(fact, str) or not fact.strip():
            raise RecoveryError(f"{context}: evidence entry missing non-empty 'fact'")
        line_start = item.get("line_start")
        line_end = item.get("line_end")
        out.append(
            RecoveredEvidence(
                file=file.strip(),
                line_start=line_start if isinstance(line_start, int) else None,
                line_end=line_end if isinstance(line_end, int) else None,
                fact=fact.strip(),
            )
        )
    return out


def _parse_nodes(raw: Any, *, context: str) -> list[RecoveredNode]:
    if not isinstance(raw, list) or not raw:
        raise RecoveryError(f"{context}: 'nodes' must be a non-empty JSON array")
    out: list[RecoveredNode] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RecoveryError(f"{context}: node entry must be an object")
        symbol = item.get("symbol")
        file = item.get("file")
        if not isinstance(symbol, str) or not symbol.strip():
            raise RecoveryError(f"{context}: node entry missing non-empty 'symbol'")
        if not isinstance(file, str) or not file.strip():
            raise RecoveryError(f"{context}: node entry missing non-empty 'file'")
        out.append(RecoveredNode(symbol=symbol.strip(), file=file.strip()))
    return out


def _parse_edges(raw: Any, *, context: str, known_symbols: set[str]) -> list[RecoveredEdge]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RecoveryError(f"{context}: 'edges' must be a JSON array")
    out: list[RecoveredEdge] = []
    for i, item in enumerate(raw):
        edge_context = f"{context}.edges[{i}]"
        if not isinstance(item, dict):
            raise RecoveryError(f"{edge_context} must be an object")
        from_symbol = item.get("from")
        to_symbol = item.get("to")
        relationship = item.get("relationship")
        if not isinstance(from_symbol, str) or not from_symbol.strip():
            raise RecoveryError(f"{edge_context} missing non-empty 'from'")
        if not isinstance(to_symbol, str) or not to_symbol.strip():
            raise RecoveryError(f"{edge_context} missing non-empty 'to'")
        if not isinstance(relationship, str) or not relationship.strip():
            raise RecoveryError(f"{edge_context} missing non-empty 'relationship'")
        from_symbol, to_symbol = from_symbol.strip(), to_symbol.strip()
        if from_symbol not in known_symbols:
            raise RecoveryError(f"{edge_context}: 'from' {from_symbol!r} is not one of this path's nodes")
        if to_symbol not in known_symbols:
            raise RecoveryError(f"{edge_context}: 'to' {to_symbol!r} is not one of this path's nodes")
        evidence = _parse_evidence_list(item.get("evidence"), context=edge_context)
        out.append(RecoveredEdge(**{"from": from_symbol, "to": to_symbol}, relationship=relationship.strip(), evidence=evidence))
    return out


def _validate_edge_chain(edges: list[RecoveredEdge], nodes: list[RecoveredNode], *, context: str) -> None:
    """Edges must form one contiguous chain starting at `nodes[0]` — the
    prefix-truncation algorithm in `sydes.recovery.verify` depends on this;
    an out-of-order or branching edge list is rejected at parse time rather
    than silently mishandled later."""
    if not edges:
        if len(nodes) > 1:
            raise RecoveryError(f"{context}: more than one node but no edges connecting them")
        return
    if len(edges) != len(nodes) - 1:
        raise RecoveryError(
            f"{context}: expected {len(nodes) - 1} edge(s) for {len(nodes)} node(s), got {len(edges)}"
        )
    expected_from = nodes[0].symbol
    for i, edge in enumerate(edges):
        if edge.from_symbol != expected_from:
            raise RecoveryError(
                f"{context}: edges do not form a single chain from nodes[0] at position {i}"
            )
        if edge.to_symbol != nodes[i + 1].symbol:
            raise RecoveryError(
                f"{context}: edge[{i}]'s 'to' does not match nodes[{i + 1}] — edges must follow node order"
            )
        expected_from = edge.to_symbol


def _parse_path(raw: Any, *, index: int) -> RecoveredPath:
    if not isinstance(raw, dict):
        raise RecoveryError(f"recovered_paths[{index}] must be an object")
    context = f"recovered_paths[{index}]"
    entrypoint = raw.get("entrypoint")
    target_node = raw.get("target_node") or raw.get("target")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise RecoveryError(f"{context} missing non-empty 'entrypoint'")
    if not isinstance(target_node, str) or not target_node.strip():
        raise RecoveryError(f"{context} missing non-empty 'target_node'")

    nodes = _parse_nodes(raw.get("nodes"), context=context)
    if len(nodes) < 2:
        # A "path" that is only the changed symbol itself, declared as its
        # own entrypoint, proves nothing — it is a way to dodge the actual
        # dispatch/connection evidence this schema exists to demand. Report
        # a genuinely unreachable symbol via `unresolved` instead; a
        # recovered path must connect at least two distinct points.
        raise RecoveryError(
            f"{context}: a path needs at least 2 nodes (an entrypoint and the changed behavior it "
            "reaches) — a single node claiming the changed symbol as its own entrypoint is not a path"
        )
    known_symbols = {n.symbol for n in nodes}
    target_node = target_node.strip()
    if target_node not in known_symbols:
        raise RecoveryError(f"{context}: 'target_node' {target_node!r} is not one of this path's nodes")
    if target_node == nodes[0].symbol:
        raise RecoveryError(
            f"{context}: 'target_node' cannot be nodes[0] — the entrypoint and the changed "
            "behavior must be different nodes, connected by at least one edge"
        )

    edges = _parse_edges(raw.get("edges"), context=context, known_symbols=known_symbols)
    _validate_edge_chain(edges, nodes, context=context)

    return RecoveredPath(entrypoint=entrypoint.strip(), target_node=target_node, nodes=nodes, edges=edges)


def _parse_test(raw: Any, *, index: int) -> RecoveredTest:
    if not isinstance(raw, dict):
        raise RecoveryError(f"recovered_tests[{index}] must be an object")
    file = raw.get("file")
    test = raw.get("test")
    covers = raw.get("covers")
    if not all(isinstance(v, str) and v.strip() for v in (file, test, covers)):
        raise RecoveryError(f"recovered_tests[{index}] requires non-empty file/test/covers")
    evidence = _parse_evidence_list(raw.get("evidence"), context=f"recovered_tests[{index}]")
    return RecoveredTest(file=file.strip(), test=test.strip(), covers=covers.strip(), evidence=evidence)


def _parse_corrected_claim(raw: Any, *, index: int) -> CorrectedFirstPassClaim:
    if not isinstance(raw, dict):
        raise RecoveryError(f"corrected_first_pass_claims[{index}] must be an object")
    claim = raw.get("claim")
    original = raw.get("original")
    corrected = raw.get("corrected")
    if not all(isinstance(v, str) and v.strip() for v in (claim, original, corrected)):
        raise RecoveryError(
            f"corrected_first_pass_claims[{index}] requires non-empty claim/original/corrected"
        )
    evidence = _parse_evidence_list(raw.get("evidence"), context=f"corrected_first_pass_claims[{index}]")
    return CorrectedFirstPassClaim(
        claim=claim.strip(), original=original.strip(), corrected=corrected.strip(), evidence=evidence,
    )


def _parse_unresolved(raw: Any, *, index: int) -> UnresolvedQuestion:
    if not isinstance(raw, dict):
        raise RecoveryError(f"unresolved[{index}] must be an object")
    question = raw.get("question")
    missing_evidence = raw.get("missing_evidence")
    if not isinstance(question, str) or not question.strip():
        raise RecoveryError(f"unresolved[{index}] missing non-empty 'question'")
    if not isinstance(missing_evidence, str) or not missing_evidence.strip():
        raise RecoveryError(f"unresolved[{index}] missing non-empty 'missing_evidence'")
    return UnresolvedQuestion(question=question.strip(), missing_evidence=missing_evidence.strip())


def _parse_list(raw: Any, *, field_name: str, parse_one) -> tuple[list, list[UnresolvedQuestion]]:
    """Parse a top-level array field item by item. One malformed entry does
    NOT fail the whole response — partial recovery beats an all-or-nothing
    failure that would discard everything else the agent legitimately
    found (e.g. a genuinely-recovered test alongside one malformed path
    entry). The array itself must still be an array; a dropped item is
    recorded as an `UnresolvedQuestion` rather than silently vanishing."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RecoveryError(f"'{field_name}' must be a JSON array")
    parsed: list = []
    dropped: list[UnresolvedQuestion] = []
    for i, item in enumerate(raw):
        try:
            parsed.append(parse_one(item, index=i))
        except RecoveryError as exc:
            dropped.append(
                UnresolvedQuestion(
                    question=f"agent's {field_name}[{i}] was malformed and dropped",
                    missing_evidence=str(exc),
                )
            )
    return parsed, dropped


def parse_atomic_completion_result(text: str) -> tuple[str, list[RecoveredEvidence]]:
    """Parse Stage B's response for ONE atomic claim (one edge, or one
    candidate test): `{"relationship": "...", "evidence": [...]}`. An
    empty `evidence` list is a complete, honest, valid answer — "I looked
    and could not find proof" — not a parse failure; the caller (Stage C)
    treats empty evidence as an automatic reject, no LLM verifier call
    needed for it.
    """
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise RecoveryError("atomic evidence-completion output was not a single JSON object")
    relationship = payload.get("relationship", "")
    relationship = relationship.strip() if isinstance(relationship, str) else ""
    evidence = _parse_evidence_list(payload.get("evidence"), context="atomic_evidence_completion")
    return relationship, evidence


def parse_recovery_result(text: str) -> RecoveryResult:
    """Parse and strictly validate one final recovery response.

    Raises `RecoveryError` only when the response is not a JSON object at
    all, or a top-level field is not an array. Within each array, one
    malformed entry is dropped (and recorded in `unresolved`) rather than
    failing the whole response — see `_parse_list`. This function does NOT
    decide `status`/`provenance` for any edge, path, or test — that is
    entirely `sydes.recovery.verify`'s job; everything parsed here starts
    `unresolved`/`rejected` until verified.
    """
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise RecoveryError("recovery output was not a single JSON object")

    paths, dropped_paths = _parse_list(payload.get("recovered_paths"), field_name="recovered_paths", parse_one=_parse_path)
    tests, dropped_tests = _parse_list(payload.get("recovered_tests"), field_name="recovered_tests", parse_one=_parse_test)
    corrected, dropped_corrected = _parse_list(
        payload.get("corrected_first_pass_claims"), field_name="corrected_first_pass_claims", parse_one=_parse_corrected_claim,
    )

    raw_unresolved = payload.get("unresolved", [])
    if not isinstance(raw_unresolved, list):
        raise RecoveryError("'unresolved' must be a JSON array")
    unresolved = [_parse_unresolved(item, index=i) for i, item in enumerate(raw_unresolved)]

    return RecoveryResult(
        status=STATUS_UNRESOLVED, recovered_paths=paths, recovered_tests=tests,
        corrected_first_pass_claims=corrected,
        unresolved=unresolved + dropped_paths + dropped_tests + dropped_corrected,
    )


def recompute_overall_status(result: RecoveryResult) -> RecoveryResult:
    """Overall `status` from the (post-verification) paths: `established`
    iff any path is fully established, else `partial` iff any path has a
    real verified prefix, else `unresolved`."""
    statuses = {p.status for p in result.recovered_paths}
    if STATUS_ESTABLISHED in statuses:
        overall = STATUS_ESTABLISHED
    elif STATUS_PARTIAL in statuses:
        overall = STATUS_PARTIAL
    else:
        overall = STATUS_UNRESOLVED
    return result.model_copy(update={"status": overall})
