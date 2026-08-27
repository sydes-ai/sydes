"""Ranked, typed boundary discovery — Increment C.

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

Soundness, by construction:
- Every candidate this module ever considers was reached by literally
  walking `index.inbound()` — real `RELATION_CALLS`/`RELATION_USAGE` edges
  (or `RELATION_SOURCE_CONFIRMED`, from the guide's own confirmed source
  reads). A signature/type-only reference is never part of this adjacency at
  all, so it can never become the sole reason a boundary is reached — see
  `_MIN_ADMIT_EDGE_STRENGTH`.
- `pr_semantic_analysis` hints (via `semantic_texts`) only ever adjust
  *ranking* (`_semantic_relevance`) — they add points to a candidate that a
  real edge already produced. They never create a candidate, never add an
  edge, and a caller with no semantic hints at all gets fully deterministic
  behavior (`_semantic_relevance` returns 0 for everyone).
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
    BOUNDARY_SUBTYPE_EVENT_HANDLER,
    BOUNDARY_SUBTYPE_HTTP,
    BOUNDARY_SUBTYPE_INTERNAL_SERVICE,
    BOUNDARY_SUBTYPE_PUBLIC_LIBRARY,
    BOUNDARY_SUBTYPE_SCHEDULED_JOB,
    EDGE_STRENGTH_MEDIUM,
    EDGE_STRENGTH_STRONG,
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

if TYPE_CHECKING:
    from sydes.code_intelligence.base import StructuralFacts
    from sydes.impact.interpreter import _FactIndex

#: A small, generic (not framework-specific) decorator-text keyword set for
#: recognizing an async/background boundary — the exact shape of entrypoint
#: current Sydes has historically dropped for being non-HTTP. Matched as
#: whole identifier tokens against the same verbatim decorator text
#: `_FactIndex` already carries; nothing here is a business rule about any
#: one framework.
_SCHEDULED_JOB_KEYWORDS = frozenset({
    "task", "cron", "schedule", "scheduled", "periodic", "celery", "job",
    "worker", "beat", "shared_task",
})
_EVENT_HANDLER_KEYWORDS = frozenset({
    "signal", "receiver", "subscribe", "subscriber", "consumer", "listener",
    "on_event", "event_handler", "queue", "handler", "dispatch",
})
_ASYNC_KEYWORDS = _SCHEDULED_JOB_KEYWORDS | _EVENT_HANDLER_KEYWORDS

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


def _decorator_keyword_tokens(text: str) -> frozenset[str]:
    """Tokens for ASYNC-keyword matching only: real decorators are commonly
    compound (`signal_receiver`, `shared_task`, `on_event`), so this also
    splits each identifier on `_` — `_semantic_relevance` deliberately does
    NOT use this, since exact identifier overlap is the right signal there."""
    tokens: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(text or ""):
        word = match.group(0).lower()
        tokens.add(word)
        tokens.update(part for part in word.split("_") if part)
    return frozenset(tokens)


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


def _decorator_text_for(index: "_FactIndex", identity: SymbolIdentity) -> str:
    entry = index.entrypoint_for_identity(identity)
    if entry is None:
        return ""
    return str(entry.get("decorators") or "")


def _route_info_for(index: "_FactIndex", identity: SymbolIdentity) -> tuple[str | None, str | None]:
    entry = index.entrypoint_for_identity(identity)
    if entry is None:
        return None, None
    method = entry.get("route_method")
    path = entry.get("route_path")
    return (str(method) if method else None, str(path) if path else None)


def _is_exported(facts: "StructuralFacts", identity: SymbolIdentity) -> bool:
    if not identity.file or not identity.short_name:
        return False
    for entry in facts.symbols_for_file(identity.repo, identity.file):
        if str(entry.get("name") or "") == identity.short_name:
            return bool(entry.get("exported"))
    return False


def _classify(
    candidate: _Candidate, index: "_FactIndex", facts: "StructuralFacts",
) -> tuple[str, str | None] | None:
    """Whether this candidate node is itself a meaningful boundary — and if
    so, which kind/subtype. Returns `None` when it is merely an intermediate
    caller worth expanding through, not a stopping point.

    Every check here reads only facts already computed elsewhere
    (`index.entrypoint_for_identity`, `facts.symbols_for_file`) — nothing is
    inferred or guessed about what a decorator or export "really" does.
    """
    identity = candidate.identity
    method, path = _route_info_for(index, identity)
    if method or path:
        return BOUNDARY_API, BOUNDARY_SUBTYPE_HTTP

    decorators = _decorator_text_for(index, identity)
    if decorators:
        tokens = _decorator_keyword_tokens(decorators)
        if tokens & _SCHEDULED_JOB_KEYWORDS:
            return BOUNDARY_ASYNC, BOUNDARY_SUBTYPE_SCHEDULED_JOB
        if tokens & _EVENT_HANDLER_KEYWORDS:
            return BOUNDARY_ASYNC, BOUNDARY_SUBTYPE_EVENT_HANDLER

    if candidate.distance > 0 and _is_exported(facts, identity):
        crossed_file = bool(candidate.reached_from_file) and candidate.reached_from_file != identity.file
        subtype = (
            BOUNDARY_SUBTYPE_PUBLIC_LIBRARY if crossed_file
            else BOUNDARY_SUBTYPE_INTERNAL_SERVICE
        )
        return BOUNDARY_CALLABLE, subtype

    return None


def _boundary_likelihood(candidate: _Candidate, index: "_FactIndex", facts: "StructuralFacts") -> float:
    """A cheap prioritization hint used only for frontier ordering — the
    actual accept/reject decision is `_classify`, called again at pop time."""
    identity = candidate.identity
    method, path = _route_info_for(index, identity)
    if method or path:
        return _LIKELIHOOD_ROUTE
    if _decorator_keyword_tokens(_decorator_text_for(index, identity)) & _ASYNC_KEYWORDS:
        return _LIKELIHOOD_ASYNC_DECORATOR
    if candidate.distance > 0 and _is_exported(facts, identity):
        crossed_file = bool(candidate.reached_from_file) and candidate.reached_from_file != identity.file
        return _LIKELIHOOD_EXPORTED_CROSS_FILE if crossed_file else _LIKELIHOOD_EXPORTED_SAME_FILE
    return 0.0


def _score(candidate: _Candidate, index: "_FactIndex", facts: "StructuralFacts",
           semantic_tokens: frozenset[str]) -> float:
    edge_reliability = candidate.weakest_points
    semantic_relevance = _semantic_relevance(candidate.identity, semantic_tokens)
    boundary_likelihood = _boundary_likelihood(candidate, index, facts)
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

    frontier: list[_Candidate] = []
    visited: set[str] = set()
    emitted_keys: set[str] = set()
    boundaries: list[DiscoveredBoundary] = []
    decisions: list[dict[str, Any]] = []
    expansions = 0
    frontier_nodes = 0
    budget_exhausted = False

    def _log(decision: str, candidate: _Candidate, *, kind: str | None = None,
              subtype: str | None = None, reason: str = "") -> None:
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
        })

    for symbol in changed_symbols:
        identity = index.identity_of(symbol)
        if identity.key in visited:
            continue
        visited.add(identity.key)
        candidate = _Candidate(
            identity=identity, distance=0, path=(), weakest_points=_POINTS_CALL,
            changed_symbol=str(symbol.get("name") or ""),
        )
        candidate.score = _score(candidate, index, facts, semantic_tokens)
        frontier.append(candidate)
        frontier_nodes += 1

    while frontier and len(boundaries) < budget.max_boundaries and expansions < budget.max_expansions:
        frontier.sort(key=lambda item: (-item.score, item.distance, item.identity.key))
        current = frontier.pop(0)
        expansions += 1

        kind_subtype = _classify(current, index, facts)
        if kind_subtype is not None and current.weakest_points >= _MIN_ADMIT_EDGE_POINTS:
            kind, subtype = kind_subtype
            key = _boundary_id(kind, current.identity)
            if key not in emitted_keys:
                emitted_keys.add(key)
                path = ImpactPath(
                    current.path,
                    "boundary_discovery",
                ) if current.path else None
                boundaries.append(DiscoveredBoundary(
                    id=key,
                    kind=kind,
                    subtype=subtype,
                    repo=current.identity.repo,
                    file=current.identity.file,
                    symbol=current.identity.short_name,
                    qualified_name=current.identity.qualified_name,
                    label=current.identity.qualified_name or current.identity.short_name,
                    changed_symbols=[current.changed_symbol],
                    path=path,
                    distance=current.distance,
                    evidence_strength=_strength_label(current.weakest_points),
                    score=current.score,
                ))
                _log("emitted", current, kind=kind, subtype=subtype)
            else:
                for existing in boundaries:
                    if existing.id == key and current.changed_symbol not in existing.changed_symbols:
                        existing.changed_symbols.append(current.changed_symbol)
            continue  # a meaningful boundary terminates this branch

        if kind_subtype is not None:
            # Classified, but the weakest edge on the path did not clear the
            # admission bar — the soundness rule in practice. Never emitted.
            _log("rejected_weak_evidence", current, kind=kind_subtype[0],
                 subtype=kind_subtype[1], reason="weakest edge below admission threshold")
            continue

        if current.distance >= budget.max_hops:
            if index.inbound(current.identity):
                budget_exhausted = True
                _log("budget_exhausted", current, reason="max_hops reached with unexplored inbound edges")
            continue

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
            next_candidate.score = _score(next_candidate, index, facts, semantic_tokens)
            frontier.append(next_candidate)
            frontier_nodes += 1

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
