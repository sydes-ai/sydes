"""Bounded GraphSlice prototype (`sydes.code_intelligence.graph_slice`).

Motivated by `sydes-evals` runtime forensics: `index_repository` and
`search_graph` are cheap, but the paginated whole-repository `query_graph`
sweep behind `CBMClient.all_call_edges`/`all_usage_edges` dominates runtime
on large repositories. These tests pin the bounded-slice alternative: a
seed-scoped, hop-batched, hard-capped fetch that a local traversal over
`StructuralFacts.call_edges`/`usage_edges` — already 100% in-memory, see
`ImpactInterpreter`/`_FactIndex` — can consume completely unchanged.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.code_intelligence.graph_slice import (
    GraphQueryCache,
    GraphSlice,
    GraphSliceLimits,
    build_graph_slice,
    graph_slice_call_edges,
    graph_slice_usage_edges,
)
from sydes.impact import ImpactInterpreter
from sydes.impact.boundary_discovery import is_production_boundary_candidate
from sydes.impact.models import BOUNDARY_API, IMPACT_STATUS_PROVEN, SymbolIdentity

REPO = "app"


def _node_names(graph_slice: GraphSlice) -> set[str]:
    return {str(node.get("qualified_name")) for node in graph_slice.nodes.values()}


def _changed(*names: str, file: str = "app/svc.py") -> list[dict]:
    return [{"name": name, "file": file, "repo": REPO} for name in names]


class FakeGraphClient:
    """A CBM client double implementing only the two seed-scoped methods
    `build_graph_slice` uses, backed by an explicit adjacency list so tests
    can express "this graph, seeded from here" directly."""

    def __init__(self, *, call_rows: list[list[str]] | None = None,
                 usage_rows: list[list[str]] | None = None) -> None:
        # Each row matches CBMClient.call_edges_for_seeds'/usage_edges_for_seeds'
        # own return shape (see cbm_client.py).
        self._call_rows = call_rows or []
        self._usage_rows = usage_rows or []
        self.call_edges_for_seeds_calls: list[tuple[str, tuple[str, ...], int]] = []
        self.usage_edges_for_seeds_calls: list[tuple[str, tuple[str, ...], int]] = []

    def call_edges_for_seeds(self, project: str, seeds: list[str], *, limit: int = 1000) -> list[list[str]]:
        self.call_edges_for_seeds_calls.append((project, tuple(seeds), limit))
        seed_set = set(seeds)
        rows = [r for r in self._call_rows if r[0] in seed_set or r[3] in seed_set]
        return rows[:limit]

    def usage_edges_for_seeds(self, project: str, seeds: list[str], *, limit: int = 1000) -> list[list[str]]:
        self.usage_edges_for_seeds_calls.append((project, tuple(seeds), limit))
        seed_set = set(seeds)
        rows = [r for r in self._usage_rows if r[0] in seed_set or r[2] in seed_set]
        return rows[:limit]

    @property
    def total_calls(self) -> int:
        return len(self.call_edges_for_seeds_calls) + len(self.usage_edges_for_seeds_calls)


def call_row(caller: str, callee: str, *, caller_file: str = "app/svc.py",
             callee_file: str = "app/svc.py") -> list[str]:
    return [caller, caller_file, "1", callee, callee_file, "2"]


def usage_row(user: str, used: str, *, user_file: str = "app/svc.py",
              used_file: str = "app/svc.py") -> list[str]:
    return [user, user_file, used, used_file]


def _identity(file: str, symbol: str, qualified: str | None = None) -> SymbolIdentity:
    return SymbolIdentity.from_fields(repo=REPO, file=file, qualified_name=qualified, short_name=symbol)


# --------------------------------------------------------------------------
# 1. Multiple changed symbols merge into one GraphSlice
# --------------------------------------------------------------------------


def test_multiple_seeds_merge_into_one_graph_slice_sharing_a_neighbor() -> None:
    """Two changed symbols that share a caller must produce ONE slice with
    that caller appearing once, discovered via one batched query per hop —
    not two separate slices, and not two separate calls for the two seeds."""
    client = FakeGraphClient(call_rows=[
        call_row("app.shared_caller", "app.seed_a"),
        call_row("app.shared_caller", "app.seed_b"),
    ])

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed_a", "app.seed_b"],
        limits=GraphSliceLimits(max_depth=1),
    )

    assert isinstance(result, GraphSlice)
    # Both seeds' neighborhoods were fetched in the SAME hop-1 call.
    assert len(client.call_edges_for_seeds_calls) == 1
    assert set(client.call_edges_for_seeds_calls[0][1]) == {"app.seed_a", "app.seed_b"}
    assert "app.shared_caller" in _node_names(result)
    assert sum(1 for e in result.edges if e.get("caller_qualified_name") == "app.shared_caller"
               and e.get("callee_qualified_name") == "app.seed_a") == 1


# --------------------------------------------------------------------------
# 2. Duplicate nodes/edges are deduplicated
# --------------------------------------------------------------------------


def test_duplicate_edges_across_hops_are_deduplicated() -> None:
    """`app.b` is reachable from `app.seed` directly AND via `app.mid` at
    hop 2 — the edge app.b->app.mid must not be double-counted just because
    two different hops both re-discover it."""
    client = FakeGraphClient(call_rows=[
        call_row("app.mid", "app.seed"),
        call_row("app.top", "app.mid"),
        call_row("app.top", "app.mid"),  # identical row returned twice by CBM
    ])

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed"], limits=GraphSliceLimits(max_depth=3),
    )

    top_to_mid = [e for e in result.edges if e.get("caller_qualified_name") == "app.top"]
    assert len(top_to_mid) == 1


# --------------------------------------------------------------------------
# 3-6. Hard caps
# --------------------------------------------------------------------------


def test_depth_cap_stops_expansion_and_marks_truncated() -> None:
    """A depth-3 chain with max_depth=1 must only reach hop 1."""
    client = FakeGraphClient(call_rows=[
        call_row("app.hop1", "app.seed"),
        call_row("app.hop2", "app.hop1"),
        call_row("app.hop3", "app.hop2"),
    ])

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed"], limits=GraphSliceLimits(max_depth=1),
    )

    assert "app.hop1" in _node_names(result)
    assert "app.hop2" not in _node_names(result)
    assert "app.hop3" not in _node_names(result)
    assert result.truncated is True
    assert result.depth_reached == 1


def test_node_cap_stops_expansion_and_marks_truncated() -> None:
    client = FakeGraphClient(call_rows=[
        call_row("app.a", "app.seed"),
        call_row("app.b", "app.seed"),
        call_row("app.c", "app.seed"),
    ])

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed"],
        limits=GraphSliceLimits(max_nodes=2, max_depth=3),
    )

    assert result.truncated is True
    assert result.truncation_reason == "max_nodes reached"
    assert result.node_count() <= 2


def test_edge_cap_stops_expansion_and_marks_truncated() -> None:
    client = FakeGraphClient(call_rows=[
        call_row("app.a", "app.seed"),
        call_row("app.b", "app.seed"),
        call_row("app.c", "app.seed"),
    ])

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed"],
        limits=GraphSliceLimits(max_edges=1, max_depth=3),
    )

    assert result.truncated is True
    assert result.truncation_reason == "max_edges reached"
    assert result.edge_count() <= 1


def test_graph_call_cap_stops_before_the_usage_hop_completes() -> None:
    """max_graph_calls=1 must stop after the CALLS call for hop 1, never
    reaching the USAGE call for that same hop."""
    client = FakeGraphClient(
        call_rows=[call_row("app.a", "app.seed")],
        usage_rows=[usage_row("app.b", "app.seed")],
    )

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed"],
        limits=GraphSliceLimits(max_graph_calls=1, max_depth=3),
    )

    assert result.source_call_count == 1
    assert len(client.call_edges_for_seeds_calls) == 1
    assert len(client.usage_edges_for_seeds_calls) == 0
    assert result.truncated is True
    assert result.truncation_reason == "max_graph_calls reached"


# --------------------------------------------------------------------------
# 7. Truncation is explicit
# --------------------------------------------------------------------------


def test_a_fully_contained_graph_is_not_marked_truncated() -> None:
    """The negative case for #7: caps generous enough to hold the whole
    reachable graph must leave `truncated=False`."""
    client = FakeGraphClient(call_rows=[call_row("app.caller", "app.seed")])

    result = build_graph_slice(
        client, "proj", REPO, ["app.seed"],
        limits=GraphSliceLimits(max_depth=3, max_nodes=100, max_edges=100, max_graph_calls=20),
    )

    assert result.truncated is False
    assert result.truncation_reason is None


# --------------------------------------------------------------------------
# 8-9. Local frontier traversal reuses C/C.1 unchanged
# --------------------------------------------------------------------------


def test_local_traversal_finds_a_production_boundary_with_no_extra_cbm_calls() -> None:
    """A GraphSlice, converted to StructuralFacts, must let the EXISTING
    ImpactInterpreter find an API boundary via pure local traversal — no
    method on the client is touched again after the slice is built."""
    client = FakeGraphClient(call_rows=[
        call_row("app.http_handler", "app.service_method"),
    ])

    graph_slice = build_graph_slice(client, "proj", REPO, ["app.service_method"])
    calls_after_slice_build = client.total_calls

    facts = StructuralFacts(
        call_edges=graph_slice_call_edges(graph_slice),
        usage_edges=graph_slice_usage_edges(graph_slice),
        symbol_index={"repos": [{"repo": REPO, "files": []}]},
        entrypoints=[{
            "repo": REPO, "qualified_name": "app.http_handler", "symbol": "http_handler",
            "file": "app/svc.py", "line": 10, "route_method": "GET", "route_path": "/x",
            "decorators": "", "signature": "",
        }],
        provides_call_graph=True, backend="cbm",
    )
    result = ImpactInterpreter().interpret(_changed("service_method"), facts, repo=REPO)

    assert len(result.boundaries) == 1
    assert result.boundaries[0].kind == BOUNDARY_API
    assert result.boundaries[0].status == IMPACT_STATUS_PROVEN
    # The whole point: local traversal spends zero additional CBM calls.
    assert client.total_calls == calls_after_slice_build


def test_test_and_main_symbols_remain_excluded_from_slice_sourced_facts() -> None:
    """C.1's production-boundary predicate must behave identically whether
    the identity comes from a full sweep or a GraphSlice — reused verbatim,
    not reimplemented."""
    client = FakeGraphClient(call_rows=[
        call_row("test_something", "app.helper", caller_file="app/tests/thing_test.py"),
        call_row("main", "app.helper", caller_file="app/cmd/bin.py"),
    ])

    graph_slice = build_graph_slice(client, "proj", REPO, ["app.helper"])
    facts = StructuralFacts(
        call_edges=graph_slice_call_edges(graph_slice),
        symbol_index={"repos": [{"repo": REPO, "files": []}]},
        provides_call_graph=True, backend="cbm",
    )

    test_identity = _identity("app/tests/thing_test.py", "test_something")
    main_identity = _identity("app/cmd/bin.py", "main")

    assert is_production_boundary_candidate(test_identity, facts) is False
    assert is_production_boundary_candidate(main_identity, facts) is False


# --------------------------------------------------------------------------
# 10-11. Deterministic precedence / inferred-boundary isolation unaffected
# --------------------------------------------------------------------------


def test_deterministic_precedence_over_slice_sourced_facts_is_unchanged() -> None:
    """An ESTABLISHED boundary discovered from slice-sourced facts must
    still be a `proven` status — the GraphSlice changes only where edges
    come from, never what status deterministic discovery assigns."""
    client = FakeGraphClient(call_rows=[call_row("app.http_handler", "app.service_method")])
    graph_slice = build_graph_slice(client, "proj", REPO, ["app.service_method"])
    facts = StructuralFacts(
        call_edges=graph_slice_call_edges(graph_slice),
        symbol_index={"repos": [{"repo": REPO, "files": []}]},
        entrypoints=[{
            "repo": REPO, "qualified_name": "app.http_handler", "symbol": "http_handler",
            "file": "app/svc.py", "line": 10, "route_method": "GET", "route_path": "/x",
            "decorators": "", "signature": "",
        }],
        provides_call_graph=True, backend="cbm",
    )
    result = ImpactInterpreter().interpret(_changed("service_method"), facts, repo=REPO)

    assert all(b.status == IMPACT_STATUS_PROVEN for b in result.boundaries)


def test_slice_sourced_boundaries_never_carry_inferred_status() -> None:
    """Deterministic discovery over a GraphSlice must never itself produce
    an `inferred` boundary — that status is D/D.1's alone, untouched here."""
    client = FakeGraphClient(call_rows=[call_row("app.http_handler", "app.service_method")])
    graph_slice = build_graph_slice(client, "proj", REPO, ["app.service_method"])
    facts = StructuralFacts(
        call_edges=graph_slice_call_edges(graph_slice),
        symbol_index={"repos": [{"repo": REPO, "files": []}]},
        entrypoints=[{
            "repo": REPO, "qualified_name": "app.http_handler", "symbol": "http_handler",
            "file": "app/svc.py", "line": 10, "route_method": "GET", "route_path": "/x",
            "decorators": "", "signature": "",
        }],
        provides_call_graph=True, backend="cbm",
    )
    result = ImpactInterpreter().interpret(_changed("service_method"), facts, repo=REPO)

    assert all(b.status != "inferred" for b in result.boundaries)


# --------------------------------------------------------------------------
# 12-13. Run-local memoization
# --------------------------------------------------------------------------


def test_identical_run_local_graph_query_is_memoized() -> None:
    cache = GraphQueryCache()
    fetch_calls = {"n": 0}

    def fetch() -> list[list[str]]:
        fetch_calls["n"] += 1
        return [["a", "b"]]

    first = cache.get_or_fetch(project="proj", edge_kind="calls", seeds=["app.x"], limit=100, fetch=fetch)
    second = cache.get_or_fetch(project="proj", edge_kind="calls", seeds=["app.x"], limit=100, fetch=fetch)

    assert first == second
    assert fetch_calls["n"] == 1
    assert cache.hits == 1
    assert cache.misses == 1


def test_seed_order_does_not_defeat_the_cache() -> None:
    cache = GraphQueryCache()
    calls = {"n": 0}

    def fetch() -> list[list[str]]:
        calls["n"] += 1
        return []

    cache.get_or_fetch(project="proj", edge_kind="calls", seeds=["app.a", "app.b"], limit=100, fetch=fetch)
    cache.get_or_fetch(project="proj", edge_kind="calls", seeds=["app.b", "app.a"], limit=100, fetch=fetch)

    assert calls["n"] == 1


def test_cache_does_not_collide_across_repo_edge_kind_or_limit() -> None:
    """Different project (repo/commit proxy), edge kind, or limit must never
    share a cache entry, even with the same seed set."""
    cache = GraphQueryCache()
    calls = {"n": 0}

    def fetch() -> list[list[str]]:
        calls["n"] += 1
        return []

    cache.get_or_fetch(project="proj-a", edge_kind="calls", seeds=["app.x"], limit=100, fetch=fetch)
    cache.get_or_fetch(project="proj-b", edge_kind="calls", seeds=["app.x"], limit=100, fetch=fetch)
    cache.get_or_fetch(project="proj-a", edge_kind="usage", seeds=["app.x"], limit=100, fetch=fetch)
    cache.get_or_fetch(project="proj-a", edge_kind="calls", seeds=["app.x"], limit=200, fetch=fetch)

    assert calls["n"] == 4


def test_build_graph_slice_reuses_a_shared_cache_across_two_seed_sets() -> None:
    """Two `build_graph_slice` calls sharing a cache and an identical seed
    set must not repeat any CBM call for that build."""
    client = FakeGraphClient(call_rows=[call_row("app.shared", "app.seed")])
    cache = GraphQueryCache()
    limits = GraphSliceLimits(max_depth=1)  # one hop: exactly one CALLS + one USAGE call

    build_graph_slice(client, "proj", REPO, ["app.seed"], limits=limits, cache=cache)
    build_graph_slice(client, "proj", REPO, ["app.seed"], limits=limits, cache=cache)

    # The second build's CALLS/USAGE queries are identical to the first's —
    # served from cache, not re-issued to the client.
    assert len(client.call_edges_for_seeds_calls) == 1
    assert len(client.usage_edges_for_seeds_calls) == 1


# --------------------------------------------------------------------------
# 14. Zero/empty graph stays conservative
# --------------------------------------------------------------------------


def test_empty_graph_around_a_real_seed_is_not_truncated() -> None:
    client = FakeGraphClient()  # no edges anywhere

    result = build_graph_slice(client, "proj", REPO, ["app.seed"])

    assert result.node_count() == 0
    assert result.edge_count() == 0
    assert result.truncated is False
    assert graph_slice_call_edges(result) == []
    assert graph_slice_usage_edges(result) == []


def test_no_seeds_at_all_is_conservatively_truncated() -> None:
    client = FakeGraphClient(call_rows=[call_row("app.a", "app.b")])

    result = build_graph_slice(client, "proj", REPO, [])

    assert result.truncated is True
    assert result.truncation_reason == "no seed symbols supplied"
    assert client.total_calls == 0


# --------------------------------------------------------------------------
# Concrete before/after call-count comparison (synthetic representative graph)
# --------------------------------------------------------------------------


def test_before_after_call_count_on_a_synthetic_large_repository() -> None:
    """The concrete number the final report cites. Builds one synthetic
    "repository" with a small relevant chain (2 hops from one seed) buried
    inside a large volume of unrelated edges, and compares:

    - BEFORE: `CBMClient.all_call_edges`/`all_usage_edges` (today's
      unconditional whole-repository sweep), paginated at the real
      `_BULK_QUERY_PAGE_SIZE`;
    - AFTER: `build_graph_slice` seeded from the one changed symbol.
    """
    from sydes.code_intelligence.cbm_client import CBMClient, _BULK_QUERY_PAGE_SIZE

    relevant_calls = [
        call_row("app.mid", "app.changed_symbol"),
        call_row("app.top", "app.mid"),
    ]
    relevant_usage = [usage_row("app.util_user", "app.changed_symbol")]
    # Large enough to force multi-page pagination at the real page size, on
    # both edge kinds, without making the test slow.
    unrelated_calls = [call_row(f"app.u{i}", f"app.u{i + 1}") for i in range(_BULK_QUERY_PAGE_SIZE * 6)]
    unrelated_usage = [usage_row(f"app.uu{i}", f"app.uu{i + 1}") for i in range(_BULK_QUERY_PAGE_SIZE * 3)]
    all_calls = relevant_calls + unrelated_calls
    all_usage = relevant_usage + unrelated_usage

    class _FakeSweepSession:
        def __init__(self) -> None:
            self.calls = 0

        def call_tool(self, tool: str, arguments: dict, *, timeout=None) -> dict:
            self.calls += 1
            query = arguments["query"]
            source = all_calls if ":CALLS]" in query else all_usage
            offset = int(query.rsplit("SKIP ", 1)[1].split(" ")[0])
            page = source[offset:offset + _BULK_QUERY_PAGE_SIZE]
            return {"rows": page}

    sweep_session = _FakeSweepSession()
    sweep_client = CBMClient(sweep_session)
    sweep_client.all_call_edges("proj")
    sweep_client.all_usage_edges("proj")
    before_calls = sweep_session.calls

    slice_client = FakeGraphClient(call_rows=all_calls, usage_rows=all_usage)
    result = build_graph_slice(slice_client, "proj", REPO, ["app.changed_symbol"])
    after_calls = result.source_call_count

    assert before_calls >= 8  # multi-page on both edge kinds, matching real-world scale
    assert after_calls <= 4  # default max_depth=2 -> at most 2 hops x 2 edge kinds
    assert after_calls < before_calls
    assert "app.mid" in _node_names(result)
    assert "app.top" in _node_names(result)
    assert "app.util_user" in _node_names(result)
