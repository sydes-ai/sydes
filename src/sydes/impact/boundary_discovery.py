"""Ranked, typed boundary discovery — Increment C, corrected in C.1.

Where `ImpactInterpreter`'s existing deterministic walk stops only at a
declared *entrypoint* (a symbol CBM annotated with route metadata or a
decorator), this module answers a broader question: starting from the
changed symbols, what is the nearest *meaningful architectural boundary* —
API, callable, or async — reachable through real structural edges? A plain
exported function with no decorator at all was previously invisible as a
stopping point; this is the new code that makes it one.

Not a parallel impact engine: this reuses `_FactIndex` (identity resolution,
inbound call/usage adjacency) and `StructuralFacts` exactly as
`ImpactInterpreter` already built them — no new CBM calls, no new symbol
extraction, no second graph. The only new thing is the ranked frontier walk
and the classification rules layered on top of the same facts.

Increment C.1 — frontier ranking vs. boundary eligibility are two distinct
questions, and real-PR evaluation showed C's first cut conflated them:

- `_score` ("which real structural node should be inspected next") answers
  frontier ranking. A high score is a reason to look at a node *sooner*,
  never a reason to treat it as a boundary.
- `_classify` ("is this node a meaningful architectural boundary where this
  branch should stop") answers eligibility, entirely separately. Reachable,
  exported, or highly ranked are each necessary-but-not-sufficient; none of
  them alone qualifies a node. Every changed symbol is run through
  `_classify` *before* any expansion at all (see the seed loop in
  `discover_boundaries`) — a route handler or route-registration symbol
  never has to acquire a caller first.

C.1's specific eligibility corrections, each answering a real-PR failure:
- A test node (by file role or by the same generic naming convention Sydes
  already uses for test *files*, applied to the symbol name) is never
  eligible for any boundary kind — see `_is_test_identity`. Tests remain
  graph nodes traversal can pass *through*; they just cannot terminate a
  branch as a production boundary.
- An executable entrypoint (`main`) is never classified `callable` — see
  `_is_executable_entrypoint`.
- `exported` alone is not sufficient for `callable`. C.1 first tried
  requiring it to also cross a directory; C.2 replaced that proxy outright,
  because the raw `exported` flag is a *naming convention* in at least one
  supported language. Only an explicit export statement now qualifies.
- A "generic decorated" node is never treated as `async`: only a specific,
  small scheduled-job/event-handler keyword vocabulary is recognized, and a
  decorator matching none of it yields no evidence rather than a guess.

Increment C.2 — boundary RECALL. C.1's precision held, but real-PR
evaluation showed route-registration methods, decorated handlers and
service surfaces producing no boundary at all. The cause was not this
traversal: it was that architectural facts Sydes already computes (notably
the whole of `facts.route_index` — every route-registration call site, with
line numbers, populated by BOTH backends) never reached this layer in a
usable shape. `_classify` no longer re-derives evidence per fact family;
it consults one normalized vocabulary built by
`sydes.impact.boundary_evidence`, which is where every "what counts as
evidence" rule now lives. Ranking (`_score`) is unchanged and still
entirely separate from eligibility.

Soundness, unchanged from C/C.1, by construction:
- Every candidate this module ever considers was reached by literally
  walking `index.inbound()` — real `RELATION_CALLS`/`RELATION_USAGE` edges
  (or `RELATION_SOURCE_CONFIRMED`, from the guide's own confirmed source
  reads). A signature/type-only reference is never part of this adjacency at
  all, so it can never become the sole reason a boundary is reached — see
  `_MIN_ADMIT_EDGE_POINTS`.
- `pr_semantic_analysis` hints (via `semantic_texts`) only ever adjust
  *ranking* (`_semantic_relevance`) — they add points to a candidate that a
  real edge already produced. They never create a candidate, never add an
  edge, never enter `_classify`, and a caller with no semantic hints at all
  gets fully deterministic behavior (`_semantic_relevance` returns 0 for
  everyone).
- Every emitted `DiscoveredBoundary.status` is `IMPACT_STATUS_PROVEN` — this
  pass never proposes an `INFERRED` boundary; that vocabulary stays the
  guide's alone (`ImpactCandidate`/`llm_candidate_log`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sydes.impact.models import (
    BOUNDARY_API,
    BOUNDARY_ASYNC,
    BOUNDARY_CALLABLE,
    EDGE_STRENGTH_MEDIUM,
    EDGE_STRENGTH_STRONG,
    EDGE_STRENGTH_WEAK,
    DiscoveredBoundary,
    ImpactPath,
    ImpactStep,
    PROVENANCE_DETERMINISTIC,
    PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED,
    RELATION_CALLS,
    RELATION_SOURCE_CONFIRMED,
    RELATION_USAGE,
    SymbolIdentity,
)
from sydes.impact.boundary_evidence import (
    BoundaryEvidence,
    BoundaryEvidenceIndex,
    build_boundary_evidence,
)
from sydes.ingest.file_roles import FILE_ROLE_TEST_USAGE_CANDIDATE, classify_candidate_file_role

if TYPE_CHECKING:
    from sydes.code_intelligence.base import StructuralFacts
    from sydes.impact.interpreter import _FactIndex

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Edge-reliability points — the "v1 scoring shape" the task asks for,
#: applied per hop; a candidate's overall edge strength is the *weakest*
#: relation on its accepted path (see `_edge_points`/`_strength_label`).
_POINTS_CALL = 5.0
_POINTS_SOURCE_CONFIRMED = 5.0
_POINTS_USAGE = 2.0
#: A candidate is admitted as a boundary only if the weakest edge on its
#: path is at least this strong — i.e. at least a real usage/reference edge.
#: `index.inbound()` never yields anything weaker than usage anyway (import
#: and signature/type references are separate lookups this traversal never
#: walks), so this is a belt-and-suspenders invariant, not the only guard.
_MIN_ADMIT_EDGE_POINTS = _POINTS_USAGE

#: boundary_likelihood bonus — a cheap, local signal that a node is *worth*
#: classifying at all, independent of semantic ranking.
_LIKELIHOOD_ROUTE = 3.0
_LIKELIHOOD_ASYNC_DECORATOR = 3.0
_LIKELIHOOD_EXPORTED_CROSS_FILE = 2.0
_LIKELIHOOD_EXPORTED_SAME_FILE = 1.0

_HOP_PENALTY = 1.0
_AMBIGUITY_PENALTY = 2.0
#: Cap on `_semantic_relevance` — a handful of overlapping tokens is a
#: strong enough signal; more overlap should not dominate every other term.
_MAX_SEMANTIC_RELEVANCE = 3.0


@dataclass(frozen=True)
class BoundaryBudget:
    """Bounded resources for one `discover_boundaries` call. Small and
    explicit, as the task asks — no policy system, just counters."""

    max_hops: int = 4
    max_expansions: int = 60
    max_boundaries: int = 8
    max_candidates_per_symbol: int = 12
    max_frontier_nodes: int = 200
    max_decisions_logged: int = 40


@dataclass
class _Candidate:
    identity: SymbolIdentity
    distance: int
    path: tuple[ImpactStep, ...]
    weakest_points: float
    changed_symbol: str
    #: The file of the node this candidate was reached *from* (its more
    #: immediate neighbor toward the changed symbol) — "" at distance 0.
    #: Used only to detect a "crossed a file boundary" callable boundary;
    #: kept as its own field rather than derived from `path[-1]`, whose
    #: last step already describes this candidate's own arrival, not its
    #: predecessor's.
    reached_from_file: str = ""
    score: float = 0.0


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(match.group(0).lower() for match in _IDENTIFIER_RE.finditer(text or ""))


def _strength_label(points: float) -> str:
    if points >= _POINTS_CALL:
        return EDGE_STRENGTH_STRONG
    return EDGE_STRENGTH_MEDIUM


def _edge_points(relation: str) -> float:
    if relation in (RELATION_CALLS, RELATION_SOURCE_CONFIRMED):
        return _POINTS_CALL if relation == RELATION_CALLS else _POINTS_SOURCE_CONFIRMED
    if relation == RELATION_USAGE:
        return _POINTS_USAGE
    return 0.0


def _semantic_relevance(identity: SymbolIdentity, semantic_tokens: frozenset[str]) -> float:
    """LLM semantic hints inform ranking ONLY: this is pure token overlap
    against tokens the caller already extracted from `pr_semantic_analysis`
    text fields — it never touches the graph and never runs without a real
    edge already having produced this candidate."""
    if not semantic_tokens:
        return 0.0
    candidate_tokens = (
        _tokenize(identity.short_name) | _tokenize(identity.qualified_name)
        | _tokenize(identity.file)
    )
    overlap = len(candidate_tokens & semantic_tokens)
    return min(_MAX_SEMANTIC_RELEVANCE, float(overlap))


def _is_exported(facts: "StructuralFacts", identity: SymbolIdentity) -> bool:
    """The raw `exported` flag. WEAK evidence only — for Python it is a
    naming convention (`not name.startswith("_")`), which is why C.1's
    export-based rule over-fired. Used solely as a ranking hint now; the
    only export signal that can *establish* a callable boundary is an
    explicit export statement, normalized in `boundary_evidence`."""
    if not identity.file or not identity.short_name:
        return False
    for entry in facts.symbols_for_file(identity.repo, identity.file):
        if str(entry.get("name") or "") == identity.short_name:
            return bool(entry.get("exported"))
    return False


#: Generic, language-neutral test-name markers — the exact same convention
#: `sydes.ingest.file_roles.classify_candidate_file_role` already uses for
#: *file names* (`test_*.py`/`*_test.py` etc.), applied here to the *symbol*
#: name so a test embedded in its own module (common in Rust, where a test
#: is `#[test] fn test_x()` inside the same source file, not a separate
#: file) is still recognized. Not a language-specific pattern library — one
#: already-endorsed naming convention, reused.
_TEST_NAME_PREFIXES = ("test_",)
_TEST_NAME_SUFFIXES = ("_test",)

#: Go's own `testing` package convention: `go test` recognizes a function
#: as a test/benchmark/fuzz target only when its name is exactly one of
#: these prefixes or the prefix followed by a non-lowercase rune (so
#: `TestFoo`/`BenchmarkFoo`/`FuzzFoo` qualify, but `Testing`/`Fuzzy` do
#: not — Go itself treats a lowercase letter there as "not a test name").
#: Gated on the identity's own file having a `.go` extension so this can
#: never fire against an unrelated language's ordinary "Test..."-named
#: production function (see `_is_test_identity`).
_GO_TEST_NAME_PREFIXES = ("Test", "Benchmark", "Fuzz")


def _is_go_test_function_name(name: str) -> bool:
    for prefix in _GO_TEST_NAME_PREFIXES:
        if name == prefix:
            return True
        if name.startswith(prefix) and len(name) > len(prefix) and not name[len(prefix)].islower():
            return True
    return False

#: Executable-entrypoint names that must never be classified `callable` —
#: see the module docstring. Kept to the one universal, unambiguous case
#: rather than a broader "is this a main-like function" heuristic.
_EXECUTABLE_ENTRYPOINT_NAMES = frozenset({"main"})


def _is_test_identity(identity: SymbolIdentity, facts: "StructuralFacts") -> bool:
    """Tests remain graph nodes and reachability/verification evidence —
    they must never terminate a branch as a production boundary. Prefers
    the existing file-role classifier; the symbol-name convention is only a
    fallback for languages (like Rust) where tests commonly live inside an
    ordinary source file rather than a separate test file."""
    if identity.file and classify_candidate_file_role(identity.file) == FILE_ROLE_TEST_USAGE_CANDIDATE:
        return True
    if identity.file and identity.file.lower().endswith(".go"):
        # Go test/benchmark/fuzz function names are only ever legal inside
        # `_test.go` files (already caught above); this only helps when the
        # file evidence is incomplete but its language is still known to be
        # Go, e.g. a grouped boundary resolved by name alone.
        if _is_go_test_function_name(identity.short_name):
            return True
    name = identity.short_name.lower()
    return name.startswith(_TEST_NAME_PREFIXES) or name.endswith(_TEST_NAME_SUFFIXES)


def _is_executable_entrypoint(identity: SymbolIdentity) -> bool:
    return identity.short_name in _EXECUTABLE_ENTRYPOINT_NAMES


def is_production_boundary_candidate(
    identity: SymbolIdentity, facts: "StructuralFacts",
) -> bool:
    """Whether this symbol may be offered as a *production* boundary
    candidate at all — the C.1 exclusions, exposed for reuse.

    Public so Increment D's evidence packet applies exactly these rules
    rather than reimplementing them: a test symbol or an executable
    entrypoint must be excluded from LLM boundary reasoning for the same
    reasons it is excluded from deterministic emission, and two copies of
    that rule would inevitably drift apart. Tests may still appear in a
    packet as *supporting* evidence; this gates candidacy only.
    """
    return not _is_test_identity(identity, facts) and not _is_executable_entrypoint(identity)


def _classify(
    candidate: _Candidate, index: "_FactIndex", facts: "StructuralFacts",
    evidence_index: "BoundaryEvidenceIndex | None" = None,
) -> tuple[str, str | None, BoundaryEvidence | None] | None:
    """Boundary ELIGIBILITY — a distinct question from frontier ranking
    (`_score`/`_boundary_likelihood`). Returns `(kind, subtype, evidence)`
    only when this node is itself a meaningful architectural cut where a
    branch should stop; `None` means "not a boundary yet, keep expanding
    (subject to budget)" — reachable, exported, or highly ranked are each
    necessary but never sufficient on their own.

    Increment C.2: the positive answer now comes from `BoundaryEvidence`
    normalized out of facts Sydes already had (see
    `sydes.impact.boundary_evidence`), rather than from this function
    re-deriving it per fact family. That is what lets a route-registration
    method or an explicitly-exported public callable qualify at all — the
    underlying facts existed before, but never reached here.

    The C.1 exclusions still run first and still veto everything: no amount
    of strong evidence makes a test function or `main` a production
    boundary.
    """
    identity = candidate.identity

    # Tests are reachability evidence, never a production boundary — checked
    # first so nothing below can promote one, however strong its evidence.
    if _is_test_identity(identity, facts):
        return None

    strongest = evidence_index.strongest(identity) if evidence_index is not None else None
    if strongest is not None:
        # `main` is an executable entrypoint, never a public library/callable
        # surface — but it may legitimately carry API/async evidence.
        if strongest.kind == BOUNDARY_CALLABLE and _is_executable_entrypoint(identity):
            return None
        if strongest.strength == EDGE_STRENGTH_WEAK:
            return None  # weak evidence never establishes a boundary
        return strongest.kind, strongest.subtype, strongest

    return None


def _boundary_likelihood(candidate: _Candidate, index: "_FactIndex", facts: "StructuralFacts",
                          evidence_index: "BoundaryEvidenceIndex | None" = None) -> float:
    """A cheap prioritization hint used only for FRONTIER RANKING — never
    the accept/reject decision, which is `_classify` alone, called again at
    pop time. A node scoring high here (e.g. a test that happens to carry
    route evidence) can still be rejected by `_classify` — ranking only
    decides what gets inspected sooner, not what qualifies."""
    identity = candidate.identity
    if _is_test_identity(identity, facts) or _is_executable_entrypoint(identity):
        return 0.0
    strongest = evidence_index.strongest(identity) if evidence_index is not None else None
    if strongest is not None and strongest.strength != EDGE_STRENGTH_WEAK:
        if strongest.kind == BOUNDARY_API:
            return _LIKELIHOOD_ROUTE
        if strongest.kind == BOUNDARY_ASYNC:
            return _LIKELIHOOD_ASYNC_DECORATOR
        return _LIKELIHOOD_EXPORTED_CROSS_FILE
    # A plain `exported` flag is too weak to establish anything (see
    # `boundary_evidence`), but it is still a fine reason to look sooner.
    if candidate.distance > 0 and _is_exported(facts, identity):
        return _LIKELIHOOD_EXPORTED_SAME_FILE
    return 0.0


def _score(candidate: _Candidate, index: "_FactIndex", facts: "StructuralFacts",
           semantic_tokens: frozenset[str],
           evidence_index: "BoundaryEvidenceIndex | None" = None) -> float:
    edge_reliability = candidate.weakest_points
    semantic_relevance = _semantic_relevance(candidate.identity, semantic_tokens)
    boundary_likelihood = _boundary_likelihood(candidate, index, facts, evidence_index)
    hop_penalty = _HOP_PENALTY * candidate.distance
    ambiguity_penalty = _AMBIGUITY_PENALTY if not candidate.identity.resolved else 0.0
    return (
        edge_reliability + semantic_relevance + boundary_likelihood
        - hop_penalty - ambiguity_penalty
    )


def _boundary_id(kind: str, identity: SymbolIdentity) -> str:
    return f"boundary:{kind}:{identity.repo}:{identity.qualified_name or identity.short_name}:{identity.file}"


def discover_boundaries(
    changed_symbols: list[dict[str, Any]],
    index: "_FactIndex",
    facts: "StructuralFacts",
    *,
    semantic_texts: list[str] | None = None,
    budget: BoundaryBudget | None = None,
) -> tuple[list[DiscoveredBoundary], list[dict[str, Any]], dict[str, Any]]:
    """Rank a typed frontier outward from `changed_symbols` and emit the
    small set of meaningful boundaries reached.

    Returns `(boundaries, decisions, metrics)`. `decisions` is a bounded,
    decision-relevant log (emitted + notably rejected candidates only, never
    one entry per graph node) suitable for `impact_decisions.jsonl`.
    """
    budget = budget or BoundaryBudget()
    semantic_tokens: frozenset[str] = frozenset()
    for text in semantic_texts or []:
        semantic_tokens = semantic_tokens | _tokenize(text)

    # Increment C.2: normalize the architectural facts Sydes already holds
    # into one small vocabulary this traversal can reason over. Reads only
    # `facts` — no CBM call, no LLM call, and (critically) no access to any
    # semantic hint, so no semantic input can ever become evidence.
    evidence_index = build_boundary_evidence(facts, repo=index.repo_of({}) or None)

    frontier: list[_Candidate] = []
    visited: set[str] = set()
    emitted_keys: set[str] = set()
    boundaries: list[DiscoveredBoundary] = []
    decisions: list[dict[str, Any]] = []
    expansions = 0
    frontier_nodes = 0
    budget_exhausted = False

    def _log(decision: str, candidate: _Candidate, *, kind: str | None = None,
              subtype: str | None = None, reason: str = "",
              evidence: BoundaryEvidence | None = None) -> None:
        if len(decisions) >= budget.max_decisions_logged:
            return
        decisions.append({
            "changed_symbol": candidate.changed_symbol,
            "candidate": candidate.identity.qualified_name or candidate.identity.short_name,
            "file": candidate.identity.file,
            "distance": candidate.distance,
            "score": round(candidate.score, 3),
            "decision": decision,
            "kind": kind,
            "subtype": subtype,
            "reason": reason,
            # C.2: the normalized evidence behind this decision — a compact
            # dict, never a raw source payload. Flows into
            # `impact_decisions.jsonl` through the existing tracer.
            "normalized_evidence": [evidence.to_dict()] if evidence is not None else [],
        })

    def _try_emit(candidate: "_Candidate") -> bool:
        """Boundary ELIGIBILITY check for one candidate — `_classify` alone
        decides; ranking never does. Returns `True` when this candidate is
        terminal (either emitted, or rejected on weak evidence) — the caller
        must not expand past it either way once eligibility says stop, and
        must expand it when this returns `False`."""
        classified = _classify(candidate, index, facts, evidence_index)
        if classified is None:
            return False

        kind, subtype, evidence = classified
        if candidate.weakest_points < _MIN_ADMIT_EDGE_POINTS:
            # Classified, but the weakest edge on the path did not clear the
            # admission bar — the soundness rule in practice. Never emitted.
            _log("rejected_weak_evidence", candidate, kind=kind, subtype=subtype,
                 reason="weakest edge below admission threshold", evidence=evidence)
            return True

        key = _boundary_id(kind, candidate.identity)
        if key not in emitted_keys:
            emitted_keys.add(key)
            path = ImpactPath(candidate.path, "boundary_discovery") if candidate.path else None
            boundaries.append(DiscoveredBoundary(
                id=key,
                kind=kind,
                subtype=subtype,
                repo=candidate.identity.repo,
                file=candidate.identity.file,
                symbol=candidate.identity.short_name,
                qualified_name=candidate.identity.qualified_name,
                label=candidate.identity.qualified_name or candidate.identity.short_name,
                changed_symbols=[candidate.changed_symbol],
                path=path,
                distance=candidate.distance,
                evidence_strength=_strength_label(candidate.weakest_points),
                score=candidate.score,
            ))
            _log("emitted", candidate, kind=kind, subtype=subtype, evidence=evidence)
        else:
            for existing in boundaries:
                if existing.id == key and candidate.changed_symbol not in existing.changed_symbols:
                    existing.changed_symbols.append(candidate.changed_symbol)
        return True  # a meaningful boundary terminates this branch

    def _expand(current: "_Candidate") -> None:
        """Push `current`'s real inbound (caller/usage) edges onto the
        frontier for later ranking — called only when `_try_emit` already
        said this node is not itself a boundary."""
        nonlocal frontier_nodes, budget_exhausted
        if current.distance >= budget.max_hops:
            if index.inbound(current.identity):
                budget_exhausted = True
                _log("budget_exhausted", current, reason="max_hops reached with unexplored inbound edges")
            return

        inbound = index.inbound(current.identity)[: budget.max_candidates_per_symbol]
        for relation, predecessor_identity, _payload in inbound:
            if predecessor_identity.key in visited:
                continue
            if frontier_nodes >= budget.max_frontier_nodes:
                budget_exhausted = True
                break
            visited.add(predecessor_identity.key)
            step = ImpactStep(
                symbol=predecessor_identity.short_name,
                qualified_name=predecessor_identity.qualified_name,
                file=predecessor_identity.file,
                relation=relation,
                evidence="",
                identity_resolved=predecessor_identity.resolved,
                provenance=(
                    PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED
                    if relation == RELATION_SOURCE_CONFIRMED else PROVENANCE_DETERMINISTIC
                ),
            )
            next_candidate = _Candidate(
                identity=predecessor_identity,
                distance=current.distance + 1,
                path=current.path + (step,),
                weakest_points=min(current.weakest_points, _edge_points(relation)),
                changed_symbol=current.changed_symbol,
                reached_from_file=current.identity.file,
            )
            next_candidate.score = _score(next_candidate, index, facts, semantic_tokens, evidence_index)
            frontier.append(next_candidate)
            frontier_nodes += 1

    # --- 1. Seed every changed symbol as a distance-0 candidate. Nothing
    # below expands a candidate's callers until that same candidate's own
    # eligibility has been checked first (see the loop below) — a route
    # handler or route-registration symbol never has to acquire a caller
    # first to become a boundary. -----------------------------------------
    for symbol in changed_symbols:
        identity = index.identity_of(symbol)
        if identity.key in visited:
            continue
        visited.add(identity.key)
        seed = _Candidate(
            identity=identity, distance=0, path=(), weakest_points=_POINTS_CALL,
            changed_symbol=str(symbol.get("name") or ""),
        )
        seed.score = _score(seed, index, facts, semantic_tokens, evidence_index)
        frontier.append(seed)
        frontier_nodes += 1

    # --- 2. Ranked frontier walk. Every pop, seed or later hop alike, is
    # first run through boundary ELIGIBILITY (`_try_emit` -> `_classify`)
    # and only expanded (`_expand`, walking its real inbound edges) when
    # eligibility says "not a boundary yet" — ranking (the sort below)
    # never substitutes for that check. ------------------------------------
    while frontier and len(boundaries) < budget.max_boundaries and expansions < budget.max_expansions:
        frontier.sort(key=lambda item: (-item.score, item.distance, item.identity.key))
        current = frontier.pop(0)
        expansions += 1

        if _try_emit(current):
            continue
        _expand(current)

    if frontier and (len(boundaries) >= budget.max_boundaries or expansions >= budget.max_expansions):
        budget_exhausted = True

    metrics = {
        "boundary_frontier_considered": frontier_nodes,
        "boundary_expansions": expansions,
        "boundary_emitted": len(boundaries),
        "boundary_rejected_weak_evidence": sum(
            1 for item in decisions if item["decision"] == "rejected_weak_evidence"
        ),
        "boundary_budget_exhausted": budget_exhausted,
    }
    boundaries.sort(key=lambda item: (item.kind, item.label))
    return boundaries, decisions, metrics
