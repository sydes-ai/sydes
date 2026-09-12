"""Deterministic path discovery over CBM's already-built graph -- an
alternative Stage A/B source for `sydes.recovery.agent`, tried before the
LLM-driven discover/prove loop because it is cheap, exact, and (when it
finds anything) far more reliable than a bounded free-text search.

Motivation (measured directly, not assumed): CBM already indexes CALLS/USAGE
edges and decorator/annotation source for every symbol in a repo, for every
language it supports -- this was true before this module existed, and
`sydes.code_intelligence.graph_slice`/`CBMClient.decorated_symbols` already
expose it. What was missing was asking the graph the actual question a
recovery hop needs answered: "is the changed symbol reachable from this
known entrypoint, and if the direct call graph doesn't reach it, does any
decorator/annotation ANYWHERE in the repo reference something already
reached?" A same-repo investigation found CBM's own graph already contains
the complete answer for one repository's async-worker entrypoint (a chain
of ordinary CALLS/USAGE edges, no decorator involved at all) and the exact
missing fact for another repository's reflection-dispatched handler (one
symbol's decorator source naming the type a second, otherwise-unconnected
symbol constructs) -- a fact the LLM agent's bounded search never found in
44 calls, that this module's equivalent query answers in one.

Nothing here judges whether a found path is trustworthy -- every edge this
module proposes is still handed to `sydes.recovery.verify`'s unmodified
Layer 0/1/2 pipeline, exactly like an LLM-proposed edge. This module's only
job is PROPOSAL: find a candidate chain with real, inspectable evidence
attached, cheaply, before falling back to the expensive search loop.

Zero framework-specific code: every query here is keyed on generic graph
facts (CALLS, USAGE, DECORATES, symbol names) that CBM already extracts the
same way for every language it parses -- nothing here names a framework,
a decorator, or an annotation by name.
"""

from __future__ import annotations

from collections import deque
import re
from dataclasses import dataclass

from sydes.recovery.graph_tools import CBMGraphTools
from sydes.recovery.schema import EntityRef, RecoveredEdge, RecoveredEvidence, RecoveredPath
from sydes.recovery.tools import RepoTools

#: How many additional decorator-bridge hops (see module docstring) may be
#: chained in one lookup -- mirrors the "at most one recursive decomposition
#: per hop" discipline already established in `sydes.recovery.agent`: a
#: bridge is tried once, not chased recursively without bound.
_MAX_DECORATOR_BRIDGES = 1
#: Default reachability search depth -- deeper than the main structural
#: pipeline's bounded slice (2 hops; see `graph_slice.py`) because this is a
#: one-shot lookup for a specific unresolved hop, not a per-run neighborhood
#: fetch spent on every changed symbol.
_DEFAULT_MAX_DEPTH = 6

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class _Node:
    qualified_name: str
    file: str
    line: int | None = None


def _short_name(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1]


def _parse_lines(raw: object) -> tuple[int | None, int | None]:
    """`"14-44"` -> `(14, 44)`; anything else -> `(None, None)`. CBM's own
    `lines` column format, not a Sydes convention -- tolerant of whatever
    doesn't match rather than raising."""
    text = str(raw or "")
    match = re.match(r"^(\d+)-(\d+)$", text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _build_adjacency(slice_edges: list[dict]) -> dict[str, list[tuple[str, dict]]]:
    """Directed adjacency (qualified_name -> [(qualified_name, edge_dict)])
    from a `GraphSlice`'s edge list -- both CALLS and USAGE rows, in the
    direction the caller/user actually reaches the callee/used symbol."""
    adjacency: dict[str, list[tuple[str, dict]]] = {}
    for edge in slice_edges:
        if "caller_qualified_name" in edge:
            src, dst = edge.get("caller_qualified_name"), edge.get("callee_qualified_name")
        else:
            src, dst = edge.get("user_qualified_name"), edge.get("used_qualified_name")
        if not src or not dst:
            continue
        adjacency.setdefault(str(src), []).append((str(dst), edge))
    return adjacency


def _bfs_path(
    adjacency: dict[str, list[tuple[str, dict]]], sources: list[str], target: str,
) -> list[tuple[str, str, dict]] | None:
    """Shortest path (as a list of `(from_qn, to_qn, edge_dict)` hops) from
    any of `sources` to `target`, or `None` if unreachable in `adjacency`."""
    if target in sources:
        return []
    visited: set[str] = set(sources)
    queue: deque[tuple[str, list[tuple[str, str, dict]]]] = deque((s, []) for s in sources)
    while queue:
        node, path = queue.popleft()
        for neighbor, edge in adjacency.get(node, []):
            if neighbor == target:
                return [*path, (node, neighbor, edge)]
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, [*path, (node, neighbor, edge)]))
    return None


#: A generic, language-wide naming convention (not a framework rule): a
#: type/class/message name is conventionally PascalCase, while a property,
#: field, or parameter name starts lowercase. Filtering matches on this
#: shape is what keeps a short, common, lowercase identifier used as some
#: unrelated decorator's string argument (a CLI flag's description, a
#: config key) from being treated as a real cross-reference -- a bare
#: identifier match alone is far too permissive to spend the one bridge
#: attempt on.
_TYPE_SHAPED_RE = re.compile(r"^[A-Z][A-Za-z0-9]{3,}$")


def _find_decorator_bridges(
    graph: CBMGraphTools, reached_qualified_names: set[str],
) -> list[tuple[_Node, _Node]]:
    """Every decorator/annotation bridge: a symbol, ANYWHERE in the repo,
    whose decorator source mentions the (type-shaped) bare name of a
    symbol already reached -- the generic shape every reflection/DI-style
    dispatch framework shares (a decorator argument names a type/message
    that some OTHER decorated symbol is registered to handle). Returns
    EVERY qualifying match, not just the first -- `propose_graph_path`
    tries all of them in its one bridging round rather than gambling the
    whole attempt on whichever happened to be found first; a genuinely
    coincidental match still has to pass `sydes.recovery.verify`'s Layer
    0/1/2 like any other edge, so proposing more than one costs at most a
    few extra (cheap, batched) verifier judgments, never a correctness risk.
    """
    reached_bare = {_short_name(qn): qn for qn in reached_qualified_names if _TYPE_SHAPED_RE.match(_short_name(qn))}
    if not reached_bare:
        return []
    bridges: list[tuple[_Node, _Node]] = []
    seen_decorated: set[str] = set()
    for row in graph.decorated_symbols():
        decorators = str(row.get("decorators") or "")
        if not decorators:
            continue
        tokens = {t for t in _IDENTIFIER_RE.findall(decorators) if _TYPE_SHAPED_RE.match(t)}
        matched_bare = tokens & reached_bare.keys()
        if not matched_bare:
            continue
        decorated_qn = str(row.get("qualified_name") or "")
        decorated_file = str(row.get("file") or "")
        if not decorated_qn or not decorated_file or decorated_qn in seen_decorated:
            continue
        seen_decorated.add(decorated_qn)
        start_line, _end_line = _parse_lines(row.get("lines"))
        for bare in matched_bare:
            bridges.append((
                _Node(qualified_name=reached_bare[bare], file=""),
                _Node(qualified_name=decorated_qn, file=decorated_file, line=start_line),
            ))
    return bridges


#: Bounds how many already-reached, type-shaped nodes get expanded into
#: their own member methods (see `_expand_type_shaped_nodes`) -- a
#: generous but real cap, not "no limit", since each expansion is its own
#: CBM lookup.
_MAX_CLASS_EXPANSIONS = 20


def _expand_type_shaped_nodes(
    graph: CBMGraphTools, adjacency: dict[str, list[tuple[str, dict]]],
    reached_qns: set[str], file_by_qn: dict[str, str], *, already_expanded: set[str],
) -> bool:
    """Constructing or otherwise referencing a class very often makes ITS
    OWN methods reachable next (a constructor validating its own state
    being the single most common shape) -- a plain CALLS/USAGE edge to a
    class only names the class, never the method inside it that runs as a
    result, exactly the same gap `methods_of` already closes for a
    decorator-bridged class (see `propose_graph_path`), just applied here
    to ANY already-reached, type-shaped node instead of only ones reached
    via a decorator bridge. Scoped to type-shaped (PascalCase) names, the
    same generic, language-wide convention `_find_decorator_bridges` uses
    to avoid treating every lowercase property/variable name as a class.
    Mutates `adjacency`/`file_by_qn`/`reached_qns` in place; returns
    whether anything new was actually added (nothing to retry a BFS over
    otherwise)."""
    candidates = [
        qn for qn in reached_qns
        if qn not in already_expanded and _TYPE_SHAPED_RE.match(_short_name(qn))
    ][:_MAX_CLASS_EXPANSIONS]
    added = False
    for qn in candidates:
        already_expanded.add(qn)
        methods = graph.methods_of(qn)
        for method_qn in methods:
            file_by_qn.setdefault(method_qn, file_by_qn.get(qn, ""))
            adjacency.setdefault(qn, []).append((method_qn, {"_defines_method": True}))
            reached_qns.add(method_qn)
            added = True
    return added


def _entity_ref(node: _Node, *, file_by_qn: dict[str, str]) -> EntityRef:
    file = node.file or file_by_qn.get(node.qualified_name, "")
    return EntityRef(symbol=_short_name(node.qualified_name), file=file, qualified_name=node.qualified_name)


def _edge_evidence(
    from_qn: str, to_qn: str, edge: dict, *, tools: RepoTools, file_by_qn: dict[str, str],
) -> tuple[str, list[RecoveredEvidence]]:
    """A generic relationship description plus one real, re-readable
    evidence citation for one hop -- either a structural CALLS/USAGE edge
    (cite the caller's own file at the edge's line) or a decorator bridge
    (cite the decorated symbol's own declaration, whose source is exactly
    what names the referenced symbol)."""
    if edge.get("_defines_method"):
        file = file_by_qn.get(to_qn, "")
        content = tools.read_file(file)
        return (
            f"{_short_name(from_qn)} (declared in this file) defines the method {_short_name(to_qn)}.",
            [RecoveredEvidence(file=file, line_start=None, line_end=None, fact=content[:500])],
        )
    if edge.get("_bridge"):
        file = file_by_qn.get(to_qn, "")
        line = edge.get("line")
        content = tools.read_file(file, start_line=line, end_line=line) if line else tools.read_file(file)
        return (
            f"{_short_name(to_qn)} carries a decorator/annotation whose source references {_short_name(from_qn)}.",
            [RecoveredEvidence(file=file, line_start=line, line_end=line, fact=content[:500])],
        )
    kind = "CALLS" if "caller_qualified_name" in edge else "USAGE"
    file = str(edge.get("caller_file") or edge.get("user_file") or file_by_qn.get(from_qn, ""))
    line = edge.get("caller_line")
    line_int = int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None
    content = tools.read_file(file, start_line=line_int, end_line=line_int) if line_int else tools.read_file(file)
    return (
        f"Static analysis found a {kind} reference from {_short_name(from_qn)} to {_short_name(to_qn)}.",
        [RecoveredEvidence(file=file, line_start=line_int, line_end=line_int, fact=content[:500])],
    )


def propose_graph_path(
    entrypoints: list[EntityRef], target: EntityRef, *, graph: CBMGraphTools, tools: RepoTools,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> RecoveredPath | None:
    """Deterministic candidate path from any of `entrypoints` to `target`,
    built entirely from CBM's own already-indexed graph -- `None` if the
    graph doesn't connect them even after one decorator-bridge attempt (the
    caller should fall back to the LLM-driven discover/prove loop in that
    case, exactly as if this function didn't exist).

    The returned `RecoveredPath` is a DRAFT: every edge's `status` is left
    at its schema default (`unresolved`) and must still go through
    `sydes.recovery.verify.verify_paths` before anything is trusted -- this
    function only proposes a chain with real evidence attached, it does not
    judge sufficiency.
    """
    # `qualified_name`, when the caller already resolved it (e.g. `ChangeSet`
    # already carries `ChangedSymbol.cbm_qualified_name` for the diff's own
    # changed symbols), is used as-is -- `resolve_qualified_name` is only a
    # fallback for entities nothing upstream has already resolved.
    target_qn = target.qualified_name or graph.resolve_qualified_name(target.symbol, target.file)
    if not target_qn:
        return None

    entry_qns: list[str] = []
    file_by_qn: dict[str, str] = {target_qn: target.file}
    for ep in entrypoints:
        qn = ep.qualified_name or graph.resolve_qualified_name(ep.symbol, ep.file)
        if qn:
            entry_qns.append(qn)
            file_by_qn[qn] = ep.file
    if not entry_qns:
        return None

    slice_ = graph.reachability_slice(entry_qns, max_depth=max_depth)
    if slice_ is None:
        return None
    slice_node_qns = {str(node.get("qualified_name") or "") for node in slice_.nodes.values()} - {""}
    for node in slice_.nodes.values():
        qn = str(node.get("qualified_name") or "")
        if qn:
            file_by_qn.setdefault(qn, str(node.get("file") or ""))

    adjacency = _build_adjacency(slice_.edges)
    reached_qns: set[str] = set(entry_qns) | slice_node_qns
    hops = _bfs_path(adjacency, entry_qns, target_qn)

    expanded_classes: set[str] = set()
    if hops is None and _expand_type_shaped_nodes(graph, adjacency, reached_qns, file_by_qn, already_expanded=expanded_classes):
        hops = _bfs_path(adjacency, entry_qns, target_qn)

    bridge_rounds = 0
    while hops is None and bridge_rounds < _MAX_DECORATOR_BRIDGES:
        bridges = _find_decorator_bridges(graph, reached_qns)
        if not bridges:
            break
        bridge_rounds += 1
        # One ROUND tries every qualifying bridge found this round, not
        # just the first -- see `_find_decorator_bridges`' own docstring
        # for why gambling the single round on one candidate is unsafe.
        for referenced_node, decorated_node in bridges:
            file_by_qn[decorated_node.qualified_name] = decorated_node.file
            # The bridge is just one more edge FROM the already-reached node
            # it references TO the decorated symbol -- inserted into the
            # SAME adjacency graph the original entrypoints search, never a
            # new BFS source of its own. Treating the decorated symbol as a
            # fresh "seed" would let the shortest-path search start there
            # directly, silently discarding the real chain back to the
            # entrypoint.
            adjacency.setdefault(referenced_node.qualified_name, []).append((
                decorated_node.qualified_name,
                {"_bridge": True, "line": decorated_node.line},
            ))
            # The decorated symbol is very often a CLASS: a class-level
            # decorator/annotation has no CALLS/USAGE edge of its own (a
            # class declaration doesn't "call" anything) -- its methods do.
            # `methods_of` is a harmless no-op extra lookup when the
            # decorated symbol already IS a method (returns nothing to add).
            member_methods = graph.methods_of(decorated_node.qualified_name)
            for method_qn in member_methods:
                file_by_qn.setdefault(method_qn, decorated_node.file)
                adjacency.setdefault(decorated_node.qualified_name, []).append((
                    method_qn, {"_defines_method": True},
                ))
            seed_qns = [decorated_node.qualified_name, *member_methods]
            extra_slice = graph.reachability_slice(seed_qns, max_depth=max_depth)
            if extra_slice is not None:
                for node in extra_slice.nodes.values():
                    qn = str(node.get("qualified_name") or "")
                    if qn:
                        file_by_qn.setdefault(qn, str(node.get("file") or ""))
                        reached_qns.add(qn)
                for src, dsts in _build_adjacency(extra_slice.edges).items():
                    adjacency.setdefault(src, []).extend(dsts)
            reached_qns.add(decorated_node.qualified_name)
            reached_qns.update(member_methods)
        hops = _bfs_path(adjacency, entry_qns, target_qn)
        if hops is None and _expand_type_shaped_nodes(
            graph, adjacency, reached_qns, file_by_qn, already_expanded=expanded_classes,
        ):
            hops = _bfs_path(adjacency, entry_qns, target_qn)

    if hops is None:
        return None

    # `hops[0][0]` is whichever entrypoint the search actually started
    # from -- `_bfs_path` may reach `target_qn` fastest from any of
    # `entry_qns`, not necessarily the first one. An empty `hops` means
    # `target_qn` itself was among `entry_qns` (the zero-hop case).
    start_qn = hops[0][0] if hops else target_qn
    nodes: list[EntityRef] = [_entity_ref(_Node(qualified_name=start_qn, file=""), file_by_qn=file_by_qn)]
    edges: list[RecoveredEdge] = []
    for from_qn, to_qn, edge in hops:
        relationship, evidence = _edge_evidence(from_qn, to_qn, edge, tools=tools, file_by_qn=file_by_qn)
        edges.append(RecoveredEdge(
            **{
                "from": _entity_ref(_Node(qualified_name=from_qn, file=""), file_by_qn=file_by_qn),
                "to": _entity_ref(_Node(qualified_name=to_qn, file=""), file_by_qn=file_by_qn),
            },
            relationship=relationship, evidence=evidence,
        ))
        nodes.append(_entity_ref(_Node(qualified_name=to_qn, file=""), file_by_qn=file_by_qn))

    entrypoint_label = next(
        (
            ep for ep in entrypoints
            if (ep.qualified_name or graph.resolve_qualified_name(ep.symbol, ep.file)) == start_qn
        ),
        entrypoints[0],
    )
    # `target_node` must exactly match the LAST node's `.symbol` (per
    # `sydes.recovery.verify._derive_path_outcome`'s strict lookup) --
    # that's whatever this function actually computed it to be, which may
    # differ in class-qualification convention from the caller's own
    # `target.symbol`, not necessarily that original string.
    return RecoveredPath(
        entrypoint=entrypoint_label.symbol, target_node=nodes[-1].symbol, nodes=nodes, edges=edges,
    )
