"""Structured, strictly-parsed recovery output — canonical entity identity,
edge-level proof, and independent path/test recovery outcomes.

Three separate ideas live here, each addressing a real failure this
prototype has hit in practice:

1. `EntityRef` (not a bare symbol string) identifies every node an edge
   connects. A same-named symbol in a different file/module is a
   different `EntityRef` — this is what makes "the recovery agent found a
   different, similarly-named class in another module and used it as
   evidence" structurally rejectable by `sydes.recovery.verify`, not just
   discouraged by a prompt (see that module's identity layer).

2. A path is a chain of `EntityRef` nodes connected by edges, and EVERY
   edge carries its own status/evidence/provenance — "all cited
   files/lines exist" is not the bar for calling a path established; each
   edge must be independently supported by evidence that actually proves
   that specific relationship, judged one at a time, never a whole path
   as one blob.

3. Path recovery and test recovery are reported as two independent
   outcomes (`PathRecoveryResult`, `TestRecoveryResult`) inside one
   `RecoveryResult` — a failure or non-establishment in one must never
   discard or taint the other; they can reach different maturity levels
   at different times (test recovery has proven far more reliable than
   path recovery in practice).

`target_node` names which node in a path's chain is the actually-changed
behavior — not necessarily the last node the agent proposed. This is what
lets `sydes.recovery.verify` implement the shortest-sufficient-path rule:
once the chain reaches `target_node`, anything appended beyond it is
padding, dropped outright rather than merely marked unresolved.

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

TEST_STATUS_ACCEPTED = "accepted"
TEST_STATUS_REJECTED = "rejected"

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

#: Recursive missing-link decomposition: at most this many intermediate
#: entities may be inserted between one unproven (from, to) pair, and
#: (enforced by `sydes.recovery.agent`, not here) at most one decomposition
#: attempt per originally-failed relationship — no recursing into a
#: recursion.
MAX_BRIDGE_NODES = 3


class RecoveryError(RuntimeError):
    """The recovery agent could not produce a usable structured result.

    Covers provider failure, non-JSON output, and a missing/invalid field.
    The caller's only correct response is to record the failure and leave
    the first-pass result exactly as it was — never to substitute a
    guessed recovery.
    """


class EntityRef(BaseModel):
    """A canonical, disambiguated reference to one repository entity —
    never a bare name. `file` is the repo-relative path where this entity
    is actually declared; empty means "not yet resolved" (discovery may
    propose a bare symbol without knowing its file — see
    `sydes.recovery.discovery`), and an entity with no resolved `file` can
    never back an `established` edge (see `sydes.recovery.verify`'s
    identity layer) — ambiguity must be resolved by Stage B
    (`sydes.recovery.evidence`) or the edge stays unresolved, never
    guessed past.

    `qualified_name` is optional extra disambiguation (a package/namespace
    path) when the repository's own conventions make one available; a
    `file` match is the load-bearing check, `qualified_name` is
    corroborating, not required.
    """

    symbol: str
    file: str = ""
    qualified_name: str | None = None


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


class RecoveredEdge(BaseModel):
    """One hop in a recovered path, judged entirely on its own — never as
    part of a whole-path blob. `from_entity`/`to_entity` are canonical
    `EntityRef`s, not bare names: `sydes.recovery.verify`'s identity layer
    rejects an edge whose cited evidence is not actually located at (or
    does not otherwise tie together) these entities' own declared files,
    which is what makes a same-named-different-module substitution
    structurally impossible to establish, not merely discouraged.

    `status`/`provenance` reflect the OUTCOME of verification, not the
    agent's own claim: Stage B (`sydes.recovery.evidence`) proposes
    `relationship`, `evidence`, and (when it can resolve them) the
    entities' files; `sydes.recovery.verify` decides `status`/`provenance`
    independently and this model is then rebuilt with the verified values.
    """

    from_entity: EntityRef = Field(alias="from")
    to_entity: EntityRef = Field(alias="to")
    relationship: str
    evidence: list[RecoveredEvidence] = Field(default_factory=list)
    status: str = STATUS_UNRESOLVED
    provenance: str = PROVENANCE_AI_RECOVERY_EXHAUSTED
    rejection_reason: str | None = None
    #: True when this edge was inserted by recursive missing-link
    #: decomposition rather than proposed directly by discovery — purely
    #: informational (see `sydes.recovery.evidence.decompose_relationship`).
    from_decomposition: bool = False

    model_config = {"populate_by_name": True}


class RecoveredPath(BaseModel):
    """One recovered entrypoint-to-changed-behavior path, expressed as a
    chain of `EntityRef` nodes/edges rather than a single all-or-nothing
    claim. Retains the FULL internal proof chain, including any nodes
    inserted by recursive decomposition — a shorter, human-facing
    collapsed view (if ever built) is a presentation concern for a later
    consumer, never something this schema discards before verification.

    `target_node` is the symbol (must match one entry in `nodes`) that is
    the actually-changed behavior this path exists to reach — the anchor
    the shortest-sufficient-path rule truncates around: edges needed to
    reach it are kept and judged; anything appended past it is dropped
    outright (see `sydes.recovery.verify._derive_path_outcome`).

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
    nodes: list[EntityRef] = Field(default_factory=list)
    edges: list[RecoveredEdge] = Field(default_factory=list)
    status: str = STATUS_UNRESOLVED
    unresolved_suffix: list[RecoveredEdge] = Field(default_factory=list)


class RecoveredTest(BaseModel):
    """A test the agent found that directly exercises the changed behavior
    — not merely a test file that happens to have changed, imports the
    same module, or shares a similar name.

    `target` is the canonical `EntityRef` this test claims to cover — the
    same identity discipline path edges get: a test claiming to cover a
    same-named symbol in the wrong file/module is rejectable the same way
    (see `sydes.recovery.verify`'s identity layer, applied here too).
    `status`/`rejection_reason` are set by that verification pass, the same
    way an edge's are — the agent's own claim is not the final word.
    """

    file: str
    test: str
    covers: str
    target: EntityRef
    evidence: list[RecoveredEvidence] = Field(default_factory=list)
    status: str = TEST_STATUS_REJECTED
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
    of entities from an entrypoint to the changed behavior, with no
    evidence-citation responsibility at all. Nodes may have an empty
    `file` (discovery is not required to know exactly where a symbol
    lives; Stage B — `sydes.recovery.evidence` — resolves and confirms
    that while proving each hop). Never trusted on its own: every adjacent
    pair is independently proven by Stage B and judged by Stage C
    (`sydes.recovery.verify`) before any of this becomes a
    `RecoveredPath`."""

    entrypoint: str
    target_node: str
    nodes: list[EntityRef] = Field(default_factory=list)


class CandidateTestClaim(BaseModel):
    """Stage A's hypothesis that some test exercises the changed behavior
    — no evidence yet; Stage B proves or disproves it the same way it does
    a path edge. `target`, if the agent could name one, is its guess at
    which canonical entity the test covers — Stage B resolves/confirms
    this the same way it does an edge's entities."""

    file: str
    test: str
    covers: str
    target: EntityRef | None = None


class PathRecoveryResult(BaseModel):
    """Path recovery's own, independent outcome — see the module
    docstring's point 3. `status`: `established` iff at least one path is
    fully established; else `partial` iff at least one path has a real
    (even if incomplete) verified prefix; else `unresolved`. Never
    discarded or altered by anything that happens in `TestRecoveryResult`.
    """

    status: str = STATUS_UNRESOLVED
    paths: list[RecoveredPath] = Field(default_factory=list)
    corrected_first_pass_claims: list[CorrectedFirstPassClaim] = Field(default_factory=list)
    unresolved: list[UnresolvedQuestion] = Field(default_factory=list)


class TestRecoveryResult(BaseModel):
    """Test recovery's own, independent outcome. `status`: `established`
    iff at least one test is `accepted`; else `unresolved`. Never
    discarded or altered by anything that happens in `PathRecoveryResult`
    — a path-proving failure (even a hard one) must not take recovered
    tests down with it; see `sydes.recovery.agent.recover`.
    """

    #: Not a pytest test class -- its name just happens to start with
    #: "Test" because it means "the outcome of AI test recovery". Silences
    #: pytest's collection heuristic on this name.
    __test__ = False

    status: str = STATUS_UNRESOLVED
    tests: list[RecoveredTest] = Field(default_factory=list)


class RecoveryResult(BaseModel):
    """The complete output of one recovery run: two independently usable
    outcomes, never one all-or-nothing blob. A caller that only wants test
    recovery (currently the more reliable capability — see
    `sydes.recovery`'s module docstring) can use `test_recovery` alone
    without caring whether `path_recovery` ever reached anything.
    """

    path_recovery: PathRecoveryResult = Field(default_factory=PathRecoveryResult)
    test_recovery: TestRecoveryResult = Field(default_factory=TestRecoveryResult)


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


def _parse_entity_ref(raw: Any, *, context: str, require_symbol: bool = True) -> EntityRef:
    if not isinstance(raw, dict):
        raise RecoveryError(f"{context} must be an object")
    symbol = raw.get("symbol")
    if require_symbol and (not isinstance(symbol, str) or not symbol.strip()):
        raise RecoveryError(f"{context} missing non-empty 'symbol'")
    file = raw.get("file")
    file = file.strip() if isinstance(file, str) else ""
    qualified_name = raw.get("qualified_name")
    qualified_name = qualified_name.strip() if isinstance(qualified_name, str) and qualified_name.strip() else None
    return EntityRef(symbol=(symbol or "").strip(), file=file, qualified_name=qualified_name)


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
        raise RecoveryError("'candidate_path.nodes' must be an array of at least 2 entities")
    nodes = [_parse_entity_ref(item, context="candidate_path.nodes[]") for item in raw_nodes]
    symbols = [n.symbol for n in nodes]
    if any(not s for s in symbols):
        raise RecoveryError("'candidate_path.nodes' entries must all have a non-empty 'symbol'")
    target_node = target_node.strip()
    if target_node not in symbols:
        raise RecoveryError(f"'candidate_path.target_node' {target_node!r} is not among 'nodes'")
    if target_node == symbols[0]:
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
    raw_target = raw.get("target")
    target = None
    if raw_target is not None:
        try:
            target = _parse_entity_ref(raw_target, context=f"candidate_tests[{index}].target")
        except RecoveryError:
            target = None
    return CandidateTestClaim(file=file.strip(), test=test.strip(), covers=covers.strip(), target=target)


def parse_discovery_result(text: str) -> tuple[CandidatePath | None, list[CandidateTestClaim]]:
    """Parse Stage A's response: `{"candidate_path": {...} | null,
    "candidate_tests": [...]}`.

    Both a malformed individual candidate test AND a malformed
    `candidate_path` are dropped (treated as "no candidate proposed"),
    never fatal to the whole discovery response — partial recovery over
    all-or-nothing. This matters here specifically because discovery is
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


class AtomicCompletionResult(BaseModel):
    """Stage B's response for ONE atomic claim (one edge, or one candidate
    test/target). `from_file`/`to_file` (and the optional qualified-name
    counterparts) are Stage B's resolution/confirmation of identity for
    the two entities it was asked about — empty means "could not resolve",
    which leaves that entity's `file` empty and therefore automatically
    unable to pass `sydes.recovery.verify`'s identity layer. An empty
    `evidence` list is a complete, honest, valid answer — "I looked and
    could not find proof" — not a parse failure.
    """

    relationship: str = ""
    evidence: list[RecoveredEvidence] = Field(default_factory=list)
    from_file: str = ""
    to_file: str = ""
    from_qualified_name: str | None = None
    to_qualified_name: str | None = None


def parse_atomic_completion_result(text: str) -> AtomicCompletionResult:
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise RecoveryError("atomic evidence-completion output was not a single JSON object")
    relationship = payload.get("relationship", "")
    relationship = relationship.strip() if isinstance(relationship, str) else ""
    evidence = _parse_evidence_list(payload.get("evidence"), context="atomic_evidence_completion")

    def _str_or_empty(key: str) -> str:
        v = payload.get(key, "")
        return v.strip() if isinstance(v, str) else ""

    def _str_or_none(key: str) -> str | None:
        v = payload.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    return AtomicCompletionResult(
        relationship=relationship, evidence=evidence,
        from_file=_str_or_empty("from_file"), to_file=_str_or_empty("to_file"),
        from_qualified_name=_str_or_none("from_qualified_name"), to_qualified_name=_str_or_none("to_qualified_name"),
    )


def parse_decomposition_result(text: str) -> list[EntityRef]:
    """Parse the recursive missing-link response: `{"intermediates":
    [{"symbol": ..., "file": ..., "qualified_name": ...}, ...]}`. More than
    `MAX_BRIDGE_NODES` intermediates is treated as "could not find a
    supportable chain" (empty list) rather than truncated — the point of
    the cap is that a long, padded chain is not what recursion exists to
    produce; a response that ignores the cap gets no chain at all, not a
    silently shortened one.
    """
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise RecoveryError("decomposition output was not a single JSON object")
    raw_intermediates = payload.get("intermediates", [])
    if not isinstance(raw_intermediates, list):
        raise RecoveryError("'intermediates' must be a JSON array")
    if len(raw_intermediates) > MAX_BRIDGE_NODES:
        return []
    out: list[EntityRef] = []
    for item in raw_intermediates:
        try:
            out.append(_parse_entity_ref(item, context="intermediates[]"))
        except RecoveryError:
            return []  # one malformed intermediate invalidates the whole proposed bridge
    return out


def recompute_path_status(result: PathRecoveryResult) -> PathRecoveryResult:
    """Overall path-recovery `status` from the (post-verification) paths:
    `established` iff any path is fully established, else `partial` iff
    any path has a real verified prefix, else `unresolved`."""
    statuses = {p.status for p in result.paths}
    if STATUS_ESTABLISHED in statuses:
        overall = STATUS_ESTABLISHED
    elif STATUS_PARTIAL in statuses:
        overall = STATUS_PARTIAL
    else:
        overall = STATUS_UNRESOLVED
    return result.model_copy(update={"status": overall})


def recompute_test_status(result: TestRecoveryResult) -> TestRecoveryResult:
    """Overall test-recovery `status`: `established` iff any test is
    `accepted`, else `unresolved`."""
    overall = STATUS_ESTABLISHED if any(t.status == TEST_STATUS_ACCEPTED for t in result.tests) else STATUS_UNRESOLVED
    return result.model_copy(update={"status": overall})
