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

Topology fallback (see `_slice_local_roots`, used from `propose_graph_path`
when no known entrypoint reaches the target at all): a non-HTTP execution
boundary (a process's own `main`, a background worker, a queue consumer) has
no route/flow feeding it into `entrypoint_entities` today, so the search
would otherwise never even start. Rather than name what an "entrypoint"
means per language, this reverses the question: seed a bounded reachability
slice from the TARGET itself (already bidirectional by construction -- see
`graph_slice.build_graph_slice`'s seed-scoped fetch), and treat whatever has
no incoming edge within that same slice as a candidate upstream root. A
candidate discovered this way is never reported as equivalent to a real,
already-known entrypoint -- see `RecoveredPath.root_boundary_status` in
`sydes.recovery.schema`.
"""

from __future__ import annotations

from collections import deque
import re
from dataclasses import dataclass

from sydes.recovery.graph_tools import CBMGraphTools
from sydes.recovery.schema import (
    EntityRef, ROOT_CANDIDATE_BOUNDARY, ROOT_VERIFIED_BOUNDARY, RecoveredEdge, RecoveredEvidence, RecoveredPath,
)
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
    # A list per bare name, not a single winner: two reached symbols
    # legitimately sharing a bare name (e.g. two different classes each
    # with their own "execute" method) is common, and picking only one
    # arbitrarily -- especially from a `set`, whose iteration order Python
    # does not guarantee is stable across processes -- was a measured, real
    # source of the same repo/diff finding a path on one run and not the
    # next. Every reached candidate for a matched bare name is tried.
    reached_bare: dict[str, list[str]] = {}
    for qn in sorted(reached_qualified_names):
        bare = _short_name(qn)
        if _TYPE_SHAPED_RE.match(bare):
            reached_bare.setdefault(bare, []).append(qn)
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
        for bare in sorted(matched_bare):
            for referenced_qn in reached_bare[bare]:
                bridges.append((
                    _Node(qualified_name=referenced_qn, file=""),
                    _Node(qualified_name=decorated_qn, file=decorated_file, line=start_line),
                ))
    return bridges


#: Bounds how many already-reached, type-shaped nodes get expanded into
#: their own member methods (see `_expand_type_shaped_nodes`) -- a
#: generous but real cap, not "no limit", since each expansion is its own
#: CBM lookup.
#:
#: Root-caused, not guessed: this was 20, and the candidate list is sorted
#: on the full qualified name (needed for determinism -- see that sort's
#: own comment) which is prefixed by file path, not by anything about
#: usefulness. A live run had 55 type-shaped candidates in a single
#: reachability round; the one this specific case needed (a value object
#: named `Address`) sorted outside the first 20 purely because of where
#: its file happens to fall alphabetically among the others -- silently
#: excluding it from expansion regardless of how many turns/bridge rounds
#: ran, no error, no truncation flag, nothing to indicate why the search
#: failed. 300 comfortably covers the type-shaped subset of a reachability
#: slice bounded at 2000 total nodes (`GraphSliceLimits.max_nodes`) without
#: being unbounded.
_MAX_CLASS_EXPANSIONS = 300


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
    # Sorted, not iterated straight off the set: Python randomizes string
    # hashing per-process by default, so `reached_qns`' iteration order
    # (and therefore WHICH candidates survive the `_MAX_CLASS_EXPANSIONS`
    # cap) would otherwise differ between runs on the exact same repo
    # state -- this was a measured, real source of run-to-run flakiness in
    # whether a path was found at all, not a hypothetical one.
    candidates = sorted(
        qn for qn in reached_qns
        if qn not in already_expanded and _TYPE_SHAPED_RE.match(_short_name(qn))
    )[:_MAX_CLASS_EXPANSIONS]
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


#: How many lines of context to show on each side of whatever line an
#: evidence citation actually locates -- a single line (the caller's own
#: declaration, a class's own start line) routinely shows neither endpoint
#: by itself: the actual call site, or a decorator sitting just above a
#: class/method declaration, is a handful of lines away, not on it.
_CITATION_WINDOW_LINES = 6
_CITATION_MAX_CHARS = 800
#: How far past `hint_line` to search for `needle` with a BOUNDED read,
#: before falling back to a whole-file read. `RepoTools.read_file` caps
#: total output at a fixed character budget regardless of range -- a
#: normal, unremarkable ~800-line file already exceeds that budget well
#: before reaching line 200, so a whole-file read silently never reaches a
#: real call site that's merely a few dozen lines past a hint (measured
#: directly: a real repo's citation for a genuine CALLS edge -- caller
#: declared at line 300, actual call at line 330 -- came back showing
#: unrelated code from around line 160, because the whole-file read
#: truncated long before either line). A bounded read centered on the hint
#: stays far under that budget for any hint this close, regardless of how
#: long the file is.
_SEARCH_WINDOW_LINES = 60

_LINE_PREFIX_RE = re.compile(r"^(\d+): ?")


def _parse_numbered_lines(content: str) -> list[tuple[int, str]]:
    """`(real_line_number, "N: text")` pairs, parsed from the "N: " prefix
    `RepoTools.read_file` itself puts on every line -- read from the
    prefix, not inferred from list position, since a BOUNDED read (any
    `start_line` other than 1) means position and real line number are not
    the same thing. Silently skips anything that isn't a numbered line
    (e.g. `read_file`'s own `"ERROR: ..."` string on a missing file)."""
    if not content:
        return []
    out: list[tuple[int, str]] = []
    for raw in content.split("\n"):
        match = _LINE_PREFIX_RE.match(raw)
        if match:
            out.append((int(match.group(1)), raw))
    return out


def _search_needle(numbered: list[tuple[int, str]], needle: str) -> tuple[list[int], int | None]:
    """Real line numbers where `needle` appears as a whole identifier,
    outside an import-shaped line -- plus the first import-shaped match's
    line number, kept separately as a last-resort fallback. An
    import/include statement is the single most common reason a textual
    mention of an identifier is not actually evidence of anything -- every
    language surfaced so far (import ... from, from ... import,
    import ...;) shares the same recognizable shape."""
    pattern = re.compile(rf"\b{re.escape(needle)}\b")
    candidates: list[int] = []
    import_match: int | None = None
    for line_no, raw in numbered:
        if not pattern.search(raw):
            continue
        content_after_prefix = raw.split(":", 1)[-1]
        if re.match(r"^\s*(import\b|from\s+\S+\s+import\b)", content_after_prefix):
            if import_match is None:
                import_match = line_no
            continue
        candidates.append(line_no)
    return candidates, import_match


def _locate_and_cite(tools: RepoTools, file: str, needle: str, *, hint_line: int | None) -> RecoveredEvidence:
    """Searches for `needle` (a whole identifier) near `hint_line` first,
    via a BOUNDED read (see `_SEARCH_WINDOW_LINES`), falling back to a
    whole-file read only when that bounded search finds nothing at all (or
    there is no `hint_line` to center on) -- never the reverse, since a
    whole-file read is what silently truncates before reaching a real
    reference in anything but a short file. Cites a real window of
    surrounding lines around whichever line is actually found, never just
    the one line a graph edge happens to record a number for, which is
    routinely a declaration/signature line that never itself mentions the
    other endpoint. `sydes.recovery.verify`'s Layer 1/2 independently
    judges whether the result is actually sufficient.
    """
    numbered: list[tuple[int, str]] = []
    if hint_line is not None:
        lo = max(1, hint_line - _SEARCH_WINDOW_LINES)
        hi = hint_line + _SEARCH_WINDOW_LINES
        numbered = _parse_numbered_lines(tools.read_file(file, start_line=lo, end_line=hi))

    match_line: int | None = None
    if needle:
        candidates, import_match = _search_needle(numbered, needle) if numbered else ([], None)
        if not candidates and import_match is None:
            # No hint_line at all, or the bounded window around it didn't
            # contain `needle` anywhere -- fall back to a whole-file
            # search, exactly the prior behavior. A real reference this
            # far from its hint is rare; this only costs an extra read
            # when the bounded attempt above didn't already succeed.
            numbered = _parse_numbered_lines(tools.read_file(file))
            candidates, import_match = _search_needle(numbered, needle)
        if candidates:
            # Nearest to the caller's own recorded line, not simply the
            # FIRST textual occurrence in the file: a symbol's own
            # declaration very often appears earlier in the file than a
            # sibling method's call to it, and citing the declaration
            # instead of the call site doesn't show the interaction this
            # edge actually claims -- measured directly (a real repo cited
            # a callee's own definition instead of its call site, and the
            # citation was correctly judged insufficient downstream).
            match_line = (
                min(candidates, key=lambda ln: abs(ln - hint_line)) if hint_line is not None else candidates[0]
            )
        else:
            match_line = import_match
    if match_line is None:
        match_line = hint_line if hint_line is not None else (numbered[0][0] if numbered else 1)

    # The final citation is always a FRESH, small, bounded read centered on
    # `match_line` -- regardless of which of the reads above found it --
    # so this can never inherit a stale, truncated buffer from either
    # earlier attempt.
    disp_lo = max(1, match_line - _CITATION_WINDOW_LINES)
    disp_hi = match_line + _CITATION_WINDOW_LINES
    disp_numbered = _parse_numbered_lines(tools.read_file(file, start_line=disp_lo, end_line=disp_hi))
    if not disp_numbered:
        return RecoveredEvidence(file=file, line_start=None, line_end=None, fact="")
    fact = "\n".join(text for _, text in disp_numbered)[:_CITATION_MAX_CHARS]
    return RecoveredEvidence(file=file, line_start=disp_numbered[0][0], line_end=disp_numbered[-1][0], fact=fact)


def _edge_evidence(
    from_qn: str, to_qn: str, edge: dict, *, tools: RepoTools, file_by_qn: dict[str, str],
) -> tuple[str, list[RecoveredEvidence]]:
    """A generic relationship description plus one real, re-readable
    evidence citation for one hop -- either a structural CALLS/USAGE edge
    (search the caller's own file for the callee's name, cite the window
    around it) or a decorator bridge/class-defines-method fact (search for
    the referenced identifier or method name the same way)."""
    if edge.get("_defines_method"):
        file = file_by_qn.get(to_qn, "")
        evidence = _locate_and_cite(tools, file, _short_name(to_qn), hint_line=None)
        return (
            f"{_short_name(from_qn)} (declared in this file) defines the method {_short_name(to_qn)}.",
            [evidence],
        )
    if edge.get("_override"):
        # A virtual/interface call reaching `from_qn` can continue into
        # `to_qn`, the concrete method CBM already resolved as overriding
        # it (see `CBMClient.override_edges_for_seeds`) -- not a textual
        # call site (dynamic dispatch has none to cite), so the evidence
        # is `to_qn`'s own declaration, the only real, re-readable content
        # this relationship has.
        file = str(edge.get("callee_file") or file_by_qn.get(to_qn, ""))
        line = edge.get("callee_line")
        line_int = int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None
        evidence = _locate_and_cite(tools, file, _short_name(to_qn), hint_line=line_int)
        return (
            f"{_short_name(to_qn)} is a concrete override/implementation of the virtual or interface method {_short_name(from_qn)}.",
            [evidence],
        )
    if edge.get("_bridge"):
        file = file_by_qn.get(to_qn, "")
        evidence = _locate_and_cite(tools, file, _short_name(from_qn), hint_line=edge.get("line"))
        return (
            f"{_short_name(to_qn)} carries a decorator/annotation whose source references {_short_name(from_qn)}.",
            [evidence],
        )
    kind = "CALLS" if "caller_qualified_name" in edge else "USAGE"
    file = str(edge.get("caller_file") or edge.get("user_file") or file_by_qn.get(from_qn, ""))
    line = edge.get("caller_line")
    line_int = int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None
    evidence = _locate_and_cite(tools, file, _short_name(to_qn), hint_line=line_int)
    return (
        f"Static analysis found a {kind} reference from {_short_name(from_qn)} to {_short_name(to_qn)}.",
        [evidence],
    )


def _slice_local_roots(
    graph: CBMGraphTools, adjacency: dict[str, list[tuple[str, dict]]], reached_qns: set[str], target_qn: str,
) -> tuple[list[str], dict[str, dict[str, bool]]]:
    """Every node in an already-fetched, bounded slice that has NO incoming
    `CALLS`/`USAGE`/`OVERRIDE` edge from any other node IN THAT SAME SLICE --
    a purely topological, language-agnostic property, not a name or a
    framework convention. Used only as the topology fallback's candidate
    pool (see `propose_graph_path`): when no already-known entrypoint
    reaches the target, these are the upstream-most points this specific
    bounded neighborhood actually contains, i.e. exactly where a
    reverse-from-target walk runs out.

    Deliberately NOT a claim that any of these IS a real system boundary --
    a slice is bounded (`GraphSliceLimits`), so "no incoming edge in this
    slice" can mean either "genuinely nothing calls this" (a real root) or
    "the real caller simply wasn't fetched" (truncation). Both cases are
    handled identically downstream: every candidate here is only ever a
    BFS source to try, still subject to the exact same
    `sydes.recovery.verify` Layer 0/1/2 judging as any other proposed edge
    -- being wrong here costs a little wasted search, never a false
    conclusion.

    Test nodes are excluded via CBM's own already-computed `is_test`
    property (`CBMGraphTools.symbol_flags`) -- a test calling the changed
    symbol directly (measured, real shape: a unit test is very often the
    ONLY direct caller of a newly-changed function) would otherwise look
    exactly like a legitimate root by this same in-degree-0 test alone.

    Returns `(surviving_candidates, flags)` -- `flags` is the SAME
    `symbol_flags` lookup already spent filtering test nodes, returned so
    the caller can also read `is_entry_point` off it for whichever
    candidate is ultimately selected, without a second CBM call.
    """
    destinations = {dst for edges in adjacency.values() for dst, _ in edges}
    candidates = sorted((reached_qns - destinations) - {target_qn})
    if not candidates:
        return [], {}
    flags = graph.symbol_flags(candidates)
    return [qn for qn in candidates if not flags.get(qn, {}).get("is_test", False)], flags


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

    # No known entrypoint at all is no longer an immediate `None` -- the
    # topology fallback below (seeded from `target_qn` itself, never from
    # `entry_qns`) still gets a chance once the normal entrypoint-seeded
    # search below is skipped for lack of any `entry_qns` to seed it with.
    adjacency: dict[str, list[tuple[str, dict]]] = {}
    reached_qns: set[str] = set(entry_qns)
    hops: list[tuple[str, str, dict]] | None = None
    expanded_classes: set[str] = set()

    if entry_qns:
        slice_ = graph.reachability_slice(entry_qns, max_depth=max_depth)
        if slice_ is None:
            return None
        slice_node_qns = {str(node.get("qualified_name") or "") for node in slice_.nodes.values()} - {""}
        for node in slice_.nodes.values():
            qn = str(node.get("qualified_name") or "")
            if qn:
                file_by_qn.setdefault(qn, str(node.get("file") or ""))

        adjacency = _build_adjacency(slice_.edges)
        reached_qns = set(entry_qns) | slice_node_qns
        hops = _bfs_path(adjacency, entry_qns, target_qn)

        if hops is None and _expand_type_shaped_nodes(graph, adjacency, reached_qns, file_by_qn, already_expanded=expanded_classes):
            hops = _bfs_path(adjacency, entry_qns, target_qn)

    bridge_rounds = 0
    while entry_qns and hops is None and bridge_rounds < _MAX_DECORATOR_BRIDGES:
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

    # Topology fallback: no known entrypoint (there may have been none at
    # all, or none of them reached `target_qn` above) -- try discovering an
    # upstream root from the target's OWN reachability slice instead of
    # giving up. Seeded from `target_qn`, never from `entry_qns`: a slice
    # already seeded from a known entrypoint reflects THAT entrypoint's
    # neighborhood, not necessarily the target's full upstream reach within
    # budget, and the two are not interchangeable for this purpose.
    root_boundary_status = ROOT_VERIFIED_BOUNDARY
    used_topology_fallback = False
    if hops is None:
        topo_slice = graph.reachability_slice([target_qn], max_depth=max_depth)
        if topo_slice is not None:
            topo_node_qns = {str(n.get("qualified_name") or "") for n in topo_slice.nodes.values()} - {""}
            for n in topo_slice.nodes.values():
                qn = str(n.get("qualified_name") or "")
                if qn:
                    file_by_qn.setdefault(qn, str(n.get("file") or ""))
            topo_adjacency = _build_adjacency(topo_slice.edges)
            topo_reached = topo_node_qns | {target_qn}
            candidate_roots, root_flags = _slice_local_roots(graph, topo_adjacency, topo_reached, target_qn)
            # Independently-corroborated candidates (CBM's own `is_entry_point`)
            # are tried FIRST, on their own -- not merged into one combined
            # multi-source BFS with every other candidate. A slice-local root
            # with no incoming edge is often something structurally trivial
            # (an interface/virtual method's OWN declaration, one hop from
            # the target via the very `OVERRIDE` edge that connects them) that
            # is topologically "closer" than a genuine, deeper root like
            # `main` -- shortest-path BFS would silently prefer the spurious
            # one-hop shortcut over the real chain if both were sourced
            # together. Trying the verified group alone first is what keeps a
            # real, corroborated root from losing to a shorter but
            # meaningless one; the full (uncorroborated) group is only tried
            # if no verified candidate reaches the target at all.
            verified_candidates = [qn for qn in candidate_roots if root_flags.get(qn, {}).get("is_entry_point")]
            topo_hops = _bfs_path(topo_adjacency, verified_candidates, target_qn) if verified_candidates else None
            if topo_hops is not None:
                hops = topo_hops
                adjacency = topo_adjacency
                reached_qns = topo_reached
                used_topology_fallback = True
                root_boundary_status = ROOT_VERIFIED_BOUNDARY
            elif candidate_roots:
                # No verified candidate reached the target at all (the
                # branch above already tried and failed) -- whatever this
                # combined attempt finds, if anything, necessarily starts
                # from an uncorroborated candidate. `is_entry_point` (CBM's
                # own, already-computed signal) is used above ONLY to
                # corroborate a root already found by topology, never to
                # decide which nodes get tried as roots in the first place
                # -- measured directly across three languages, this flag
                # alone is NOT a reliable cross-language "is this a process
                # entrypoint" signal (correct for a Go `main`, absent for
                # Java's, populated with unrelated leaf functions for one
                # TS repo).
                topo_hops = _bfs_path(topo_adjacency, candidate_roots, target_qn)
                if topo_hops is not None:
                    hops = topo_hops
                    adjacency = topo_adjacency
                    reached_qns = topo_reached
                    used_topology_fallback = True
                    root_boundary_status = ROOT_CANDIDATE_BOUNDARY

    if hops is None:
        return None

    # `hops[0][0]` is whichever root the search actually started from --
    # `_bfs_path` may reach `target_qn` fastest from any of its sources, not
    # necessarily the first one. An empty `hops` means `target_qn` itself
    # was among the sources (the zero-hop case).
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

    if used_topology_fallback:
        # Not one of `entrypoints` at all -- discovered from graph shape,
        # not passed in. See `root_boundary_status` above for how much
        # confidence that discovery earns.
        entrypoint_symbol = _short_name(start_qn)
    else:
        entrypoint_label = next(
            (
                ep for ep in entrypoints
                if (ep.qualified_name or graph.resolve_qualified_name(ep.symbol, ep.file)) == start_qn
            ),
            entrypoints[0],
        )
        entrypoint_symbol = entrypoint_label.symbol
    # `target_node` must exactly match the LAST node's `.symbol` (per
    # `sydes.recovery.verify._derive_path_outcome`'s strict lookup) --
    # that's whatever this function actually computed it to be, which may
    # differ in class-qualification convention from the caller's own
    # `target.symbol`, not necessarily that original string.
    return RecoveredPath(
        entrypoint=entrypoint_symbol, target_node=nodes[-1].symbol, nodes=nodes, edges=edges,
        root_boundary_status=root_boundary_status,
    )
