"""Bounded structural neighborhoods — the CBM `query_graph` prototype.

Runtime forensics on `sydes-evals`' broad-validation cases (see
`sydes-evals/results/v1-broad-validation-02/runtime-forensics.md`) found
that `index_repository` and `search_graph` are cheap, but the repeated,
paginated `query_graph` calls behind `all_call_edges`/`all_usage_edges`
(a whole-repository sweep, unconditionally issued by every structural
build) dominate runtime on large repositories — 27 to 51 CBM calls in one
case, the bulk of them `query_graph` pages taking tens to hundreds of
seconds each, enough to blow through a 900s budget.

`CBMCodeIntelligence.build_or_update` still performs that full sweep — this
module does not replace it, and nothing here is wired into the default
`verify-change` path yet. This is the *bounded-slice building block*: given
one or more changed-symbol seeds, fetch only the CALLS/USAGE neighborhood
actually reachable from them, hop-batched (every seed at a given hop shares
one `query_graph` call per edge kind, via `CBMClient.call_edges_for_seeds`/
`usage_edges_for_seeds`) rather than swept whole-repository or issued one
call per node, with hard caps so a large repository cannot force unbounded
pagination.

The output, `GraphSlice`, converts to the exact `call_edges`/`usage_edges`
list-of-dict shape `StructuralFacts` already carries (see
`graph_slice_call_edges`/`graph_slice_usage_edges`) — so
`ImpactInterpreter`/`boundary_discovery`'s existing local traversal
(`_FactIndex`, already 100% in-memory, already issuing zero CBM calls per
frontier node) can consume a bounded slice completely unchanged. Nothing in
this module concerns itself with boundary eligibility, evidence grounding,
or LLM interpretation — those rules stay exactly where C/C.1/C.2/D/D.1 put
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sydes.observability import trace as _trace

if TYPE_CHECKING:
    from sydes.code_intelligence.cbm_client import CBMClient

__all__ = [
    "GraphQueryCache",
    "GraphSlice",
    "GraphSliceLimits",
    "build_graph_slice",
    "graph_slice_call_edges",
    "graph_slice_usage_edges",
]

#: Conservative prototype defaults. `max_graph_calls` is the real backstop:
#: with the default depth/edge-kind shape below (one CALLS + one USAGE call
#: per hop) a normal run spends at most `2 * max_depth` calls, but the cap
#: is enforced independently so a change in that shape can never silently
#: remove the bound. Chosen to keep a large repository's slice fetch in the
#: single digits to low tens of calls, versus the 27-51 whole-repository
#: sweep calls forensics observed.
_DEFAULT_MAX_DEPTH = 2
_DEFAULT_MAX_NODES = 400
_DEFAULT_MAX_EDGES = 1200
_DEFAULT_MAX_GRAPH_CALLS = 8
_DEFAULT_PAGE_LIMIT = 500


@dataclass(frozen=True)
class GraphSliceLimits:
    """Hard caps for one bounded slice fetch. Every cap is enforced
    independently; hitting any one of them truncates the slice."""

    max_depth: int = _DEFAULT_MAX_DEPTH
    max_nodes: int = _DEFAULT_MAX_NODES
    max_edges: int = _DEFAULT_MAX_EDGES
    max_graph_calls: int = _DEFAULT_MAX_GRAPH_CALLS
    page_limit: int = _DEFAULT_PAGE_LIMIT


@dataclass
class GraphSlice:
    """A bounded structural neighborhood, transport/storage neutral.

    Not a customer-facing API: an internal handoff between a CBM fetch and
    Sydes' own local traversal. `nodes`/`edges` intentionally mirror
    `StructuralFacts.call_edges`/`usage_edges`' row shape rather than
    inventing a second graph representation — see `graph_slice_call_edges`/
    `graph_slice_usage_edges`.
    """

    seed_symbols: tuple[str, ...] = ()
    #: qualified_name -> {"file": str, "line": int | None} — every node this
    #: slice has observed as an edge endpoint, seed or not.
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Each: {"kind": "calls" | "usage", caller/callee or user/used fields,
    #: matching StructuralFacts' own edge shape for that kind}.
    edges: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    truncation_reason: str | None = None
    #: How many CBM `query_graph` calls this slice actually spent.
    source_call_count: int = 0
    depth_reached: int = 0

    def node_count(self) -> int:
        return len(self.nodes)

    def edge_count(self) -> int:
        return len(self.edges)


class GraphQueryCache:
    """Run-local memoization for identical bounded graph queries.

    Not a general CBM response cache — scoped to exactly the query shapes
    `build_graph_slice` issues. Keyed on every part of the request that
    affects the result: repo/project identity, edge kind, the exact seed
    set for that hop (order-independent), and the page limit. Deliberately
    excludes anything that would let it silently span two different repo
    states — this cache is meant to live exactly as long as one
    `build_graph_slice` caller keeps it (typically one process/run); it is
    never persisted, and a fresh `GraphQueryCache()` per run is always
    correct even though sharing one across an entire run's multiple slice
    builds is also safe and is the point of exposing it as a parameter.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[Any, ...], list[list[str]]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(
        *, project: str, edge_kind: str, seeds: list[str], limit: int,
    ) -> tuple[Any, ...]:
        return (project, edge_kind, tuple(sorted(set(seeds))), limit)

    def get_or_fetch(
        self, *, project: str, edge_kind: str, seeds: list[str], limit: int,
        fetch: "Any",
    ) -> list[list[str]]:
        key = self._key(project=project, edge_kind=edge_kind, seeds=seeds, limit=limit)
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        rows = fetch()
        self._store[key] = rows
        return rows


def _node_key(qualified_name: str | None, file: str | None) -> str:
    return f"{file or ''}::{qualified_name or ''}"


def _short_name(qualified_name: str | None) -> str:
    return str(qualified_name or "").rsplit(".", 1)[-1]


def build_graph_slice(
    client: "CBMClient",
    project: str,
    repo: str,
    seed_symbols: list[str],
    *,
    limits: GraphSliceLimits | None = None,
    cache: GraphQueryCache | None = None,
) -> GraphSlice:
    """Fetch a bounded CALLS/USAGE neighborhood around `seed_symbols`.

    Hop-batched breadth-first expansion: at each hop, every symbol newly
    discovered at the previous hop (the seeds themselves at hop 0) is sent
    in ONE `call_edges_for_seeds` call and ONE `usage_edges_for_seeds` call
    — not one call per symbol — via `CBMClient`'s existing seed-scoped
    query methods. Expansion stops, and `truncated` is set, the moment any
    cap is reached: `max_depth` hops completed, `max_nodes`/`max_edges`
    exceeded, or `max_graph_calls` spent. A `page_limit`-sized page that
    comes back full is itself a truncation signal (more edges exist for
    that hop than were fetched) even if no other cap fired yet.

    Deterministic: hop order and within-hop query text are both fixed, so
    two calls with the same seeds/limits over the same graph state produce
    the same slice (subject to CBM's own row ordering, which the seed-scoped
    queries request via `ORDER BY`, same as the whole-repo sweep methods).
    """
    limits = limits or GraphSliceLimits()
    cache = cache if cache is not None else GraphQueryCache()

    seeds = [s for s in dict.fromkeys(seed_symbols) if s]  # de-dup, preserve order
    slice_ = GraphSlice(seed_symbols=tuple(seeds))
    if not seeds:
        slice_.truncated = True
        slice_.truncation_reason = "no seed symbols supplied"
        _emit_trace(slice_, limits)
        return slice_

    seen_edges: set[tuple[str, str, str, str]] = set()
    frontier = list(seeds)
    visited: set[str] = set()

    for hop in range(1, limits.max_depth + 1):
        frontier = [s for s in frontier if s not in visited]
        if not frontier:
            break
        visited.update(frontier)
        next_frontier: list[str] = []

        for edge_kind, fetch_method in (
            ("calls", client.call_edges_for_seeds),
            ("usage", client.usage_edges_for_seeds),
        ):
            if slice_.source_call_count >= limits.max_graph_calls:
                slice_.truncated = True
                slice_.truncation_reason = "max_graph_calls reached"
                break

            def _do_fetch(method=fetch_method, seeds_=frontier) -> list[list[str]]:
                return method(project, seeds_, limit=limits.page_limit)

            rows = cache.get_or_fetch(
                project=project, edge_kind=edge_kind, seeds=frontier,
                limit=limits.page_limit, fetch=_do_fetch,
            )
            slice_.source_call_count += 1
            if len(rows) >= limits.page_limit:
                slice_.truncated = True
                slice_.truncation_reason = (
                    slice_.truncation_reason or f"{edge_kind} hop {hop} filled a full page"
                )

            for row in rows:
                if edge_kind == "calls":
                    caller_q, caller_file, caller_line, callee_q, callee_file, callee_line = (
                        (row + [None] * 6)[:6]
                    )
                    edge_key = ("calls", str(caller_q), str(callee_q), str(caller_file))
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    slice_.nodes.setdefault(
                        _node_key(caller_q, caller_file),
                        {"qualified_name": caller_q, "file": caller_file, "line": caller_line},
                    )
                    slice_.nodes.setdefault(
                        _node_key(callee_q, callee_file),
                        {"qualified_name": callee_q, "file": callee_file, "line": callee_line},
                    )
                    slice_.edges.append({
                        "repo": repo,
                        "caller_file": caller_file,
                        "caller_symbol": _short_name(caller_q),
                        "caller_qualified_name": caller_q,
                        "caller_line": caller_line,
                        "callee_file": callee_file,
                        "callee_symbol": _short_name(callee_q),
                        "callee_qualified_name": callee_q,
                        "callee_line": callee_line,
                        "source": "cbm_graph_slice",
                    })
                    for candidate in (caller_q, callee_q):
                        if candidate and candidate not in visited:
                            next_frontier.append(str(candidate))
                else:
                    user_q, user_file, used_q, used_file = (row + [None] * 4)[:4]
                    edge_key = ("usage", str(user_q), str(used_q), str(user_file))
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    slice_.nodes.setdefault(
                        _node_key(user_q, user_file),
                        {"qualified_name": user_q, "file": user_file, "line": None},
                    )
                    slice_.nodes.setdefault(
                        _node_key(used_q, used_file),
                        {"qualified_name": used_q, "file": used_file, "line": None},
                    )
                    slice_.edges.append({
                        "repo": repo,
                        "user_file": user_file,
                        "user_symbol": _short_name(user_q),
                        "user_qualified_name": user_q,
                        "used_file": used_file,
                        "used_symbol": _short_name(used_q),
                        "used_qualified_name": used_q,
                        "source": "cbm_graph_slice",
                    })
                    for candidate in (user_q, used_q):
                        if candidate and candidate not in visited:
                            next_frontier.append(str(candidate))

                if slice_.node_count() >= limits.max_nodes:
                    slice_.truncated = True
                    slice_.truncation_reason = slice_.truncation_reason or "max_nodes reached"
                    break
                if slice_.edge_count() >= limits.max_edges:
                    slice_.truncated = True
                    slice_.truncation_reason = slice_.truncation_reason or "max_edges reached"
                    break

            if slice_.truncated and slice_.truncation_reason in (
                "max_nodes reached", "max_edges reached",
            ):
                break

        slice_.depth_reached = hop
        if slice_.truncated and slice_.truncation_reason in (
            "max_nodes reached", "max_edges reached", "max_graph_calls reached",
        ):
            break
        frontier = list(dict.fromkeys(next_frontier))

    if not slice_.truncated and slice_.depth_reached >= limits.max_depth and frontier:
        # Hit the depth cap with more frontier still unexplored — real
        # truncation, not merely "the graph happened to end here."
        slice_.truncated = True
        slice_.truncation_reason = "max_depth reached with unexplored frontier"

    _emit_trace(slice_, limits)
    return slice_


def _emit_trace(slice_: GraphSlice, limits: GraphSliceLimits) -> None:
    _trace.record_graph_slice(
        seed_symbols=list(slice_.seed_symbols),
        graph_calls_used=slice_.source_call_count,
        node_count=slice_.node_count(),
        edge_count=slice_.edge_count(),
        truncated=slice_.truncated,
        truncation_reason=slice_.truncation_reason,
        depth_reached=slice_.depth_reached,
    )


def graph_slice_call_edges(slice_: GraphSlice) -> list[dict[str, Any]]:
    """This slice's edges in `StructuralFacts.call_edges` shape."""
    return [edge for edge in slice_.edges if "caller_qualified_name" in edge]


def graph_slice_usage_edges(slice_: GraphSlice) -> list[dict[str, Any]]:
    """This slice's edges in `StructuralFacts.usage_edges` shape."""
    return [edge for edge in slice_.edges if "user_qualified_name" in edge]
