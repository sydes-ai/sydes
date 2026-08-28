"""Bounded graph-slice retrieval on the REAL verify-change path.

`tests/test_graph_slice.py` pins `build_graph_slice` in isolation. These
tests pin the production integration: that `analyze_change` /
`CBMCodeIntelligence` actually stop performing an unconditional
repository-wide CALLS/USAGE sweep for an ordinary change, and instead fetch
a bounded neighborhood seeded from the symbols the change touched.

The client double is a spy: it records every method the backend calls, so a
test can assert on what was NOT called (`all_call_edges`/`all_usage_edges`)
rather than only on what came back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sydes.code_intelligence.base import CodeIntelligenceError, StructuralFacts
from sydes.code_intelligence.cbm import CBM_BACKEND, CBMCodeIntelligence
from sydes.code_intelligence.graph_slice import GraphSliceLimits
from sydes.core.models import RepoRef
from sydes.verify.analyzer import VerifyChangeOptions, analyze_change

REPO = "app"


class SpyCBMClient:
    """A CBM client double that records which acquisition path was taken.

    Implements exactly the surface `CBMCodeIntelligence` uses. Full-sweep and
    seed-scoped methods are tracked separately, which is the whole point:
    the assertions below are about which of the two the production path
    chose.
    """

    def __init__(
        self,
        *,
        call_rows: list[list[str]] | None = None,
        usage_rows: list[list[str]] | None = None,
        symbol_rows: dict[str, list[list[str]]] | None = None,
        seed_error: Exception | None = None,
    ) -> None:
        self._call_rows = call_rows or []
        self._usage_rows = usage_rows or []
        self._symbol_rows = symbol_rows or {}
        self._seed_error = seed_error
        # `CBMCodeIntelligence` reads `client.metrics` as the dict-shaped
        # property `CBMClient` exposes, not the raw recorder.
        self.metrics: dict = {"calls": 0, "session_start_ms": 0, "mean_call_ms": 0}
        self.server_version = "spy"
        self.malformed_rows = 0
        # Full repository-wide sweeps — what this work exists to avoid.
        self.all_call_edges_calls = 0
        self.all_usage_edges_calls = 0
        # Bounded, seed-scoped fetches.
        self.seed_call_requests: list[tuple[str, ...]] = []
        self.seed_usage_requests: list[tuple[str, ...]] = []

    # -- indexing / cheap repo-wide metadata ------------------------------

    def index_repository(self, repo_path, *, mode: str = "fast") -> dict:
        return {"project": "spy-project", "nodes": 10, "edges": 4}

    def all_symbols(self, project: str, label: str) -> list[list[str]]:
        if self._symbol_rows:
            return self._symbol_rows.get(label, [])
        # Derived from the graph itself when a test does not care about the
        # symbol table specifically: every edge endpoint is a real symbol
        # whose canonical identity is exactly the name the edge is keyed by.
        # `all_symbols` returns 7 columns; the last is CBM's own
        # `qualified_name`, which is what seed canonicalization resolves to.
        if label != "Function":
            return []
        rows: list[list[str]] = []
        seen: set[str] = set()
        for row in self._call_rows:
            for qualified, path in ((row[0], row[1]), (row[3], row[4])):
                if qualified not in seen:
                    seen.add(qualified)
                    rows.append([
                        qualified.rsplit(".", 1)[-1], path, "1", "2", "", "true", qualified,
                    ])
        for row in self._usage_rows:
            for qualified, path in ((row[0], row[1]), (row[2], row[3])):
                if qualified not in seen:
                    seen.add(qualified)
                    rows.append([
                        qualified.rsplit(".", 1)[-1], path, "1", "2", "", "true", qualified,
                    ])
        return rows

    def all_imports(self, project: str) -> list[list[str]]:
        return []

    def decorated_symbols(self, project: str, *, page_size: int = 500) -> list[dict]:
        return [{
            "qualified_name": "app.views.handle", "name": "handle",
            "file": "views.py", "lines": "1-4",
            "route_method": "GET", "route_path": "/x",
            "decorators": "@router.get('/x')", "signature": "()",
        }]

    # -- repository-wide edge sweeps (the expensive path) ------------------

    def all_call_edges(self, project: str) -> list[list[str]]:
        self.all_call_edges_calls += 1
        return self._call_rows

    def all_usage_edges(self, project: str) -> list[list[str]]:
        self.all_usage_edges_calls += 1
        return self._usage_rows

    # -- bounded, seed-scoped fetches (the cheap path) ---------------------

    def call_edges_for_seeds(self, project, seeds, *, limit=1000) -> list[list[str]]:
        if self._seed_error is not None:
            raise self._seed_error
        self.seed_call_requests.append(tuple(seeds))
        seed_set = set(seeds)
        return [r for r in self._call_rows if r[0] in seed_set or r[3] in seed_set][:limit]

    def usage_edges_for_seeds(self, project, seeds, *, limit=1000) -> list[list[str]]:
        if self._seed_error is not None:
            raise self._seed_error
        self.seed_usage_requests.append(tuple(seeds))
        seed_set = set(seeds)
        return [r for r in self._usage_rows if r[0] in seed_set or r[2] in seed_set][:limit]

    @property
    def graph_query_calls(self) -> int:
        """Every remote graph query, by either strategy."""
        return (
            self.all_call_edges_calls + self.all_usage_edges_calls
            + len(self.seed_call_requests) + len(self.seed_usage_requests)
        )


def call_row(caller: str, callee: str, *, caller_file="svc.py", callee_file="svc.py") -> list[str]:
    return [caller, caller_file, "1", callee, callee_file, "2"]


def usage_row(user: str, used: str, *, user_file="svc.py", used_file="svc.py") -> list[str]:
    return [user, user_file, used, used_file]


def _facts_via(client: SpyCBMClient, *, defer_edges: bool, tmp_path: Path) -> StructuralFacts:
    backend = CBMCodeIntelligence(client=client)
    return backend.build_or_update(
        [RepoRef(name=REPO, root=str(tmp_path))], defer_edges=defer_edges,
    )


# --------------------------------------------------------------------------
# Backend level: build_or_update no longer sweeps when edges are deferred
# --------------------------------------------------------------------------


def test_deferred_build_does_not_sweep_the_repository_edge_tables(tmp_path: Path) -> None:
    client = SpyCBMClient(call_rows=[call_row("app.h", "app.changed")])

    facts = _facts_via(client, defer_edges=True, tmp_path=tmp_path)

    assert client.all_call_edges_calls == 0
    assert client.all_usage_edges_calls == 0
    assert facts.call_edges == []
    # The backend still declares it CAN supply a call graph — the edges are
    # deferred, not absent.
    assert facts.provides_call_graph is True


def test_non_deferred_build_still_sweeps_for_backward_compatibility(tmp_path: Path) -> None:
    """The full-sweep path must remain intact as the fallback."""
    client = SpyCBMClient(call_rows=[call_row("app.h", "app.changed")])

    facts = _facts_via(client, defer_edges=False, tmp_path=tmp_path)

    assert client.all_call_edges_calls == 1
    assert client.all_usage_edges_calls == 1
    assert len(facts.call_edges) == 1


def test_attach_bounded_edges_uses_only_seed_scoped_queries(tmp_path: Path) -> None:
    client = SpyCBMClient(
        call_rows=[call_row("app.handler", "app.changed")],
        usage_rows=[usage_row("app.user", "app.changed")],
    )
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(facts, seed_symbols=["app.changed"])

    assert outcome.used_slice is True
    assert outcome.fell_back is False
    assert client.all_call_edges_calls == 0
    assert client.all_usage_edges_calls == 0
    assert client.seed_call_requests  # seed-scoped path was taken
    assert len(facts.call_edges) == 1
    assert facts.call_edges[0]["caller_qualified_name"] == "app.handler"
    assert len(facts.usage_edges) == 1


def test_multiple_changed_symbols_are_batched_into_one_seed_request(tmp_path: Path) -> None:
    client = SpyCBMClient(call_rows=[
        call_row("app.shared", "app.changed_a"),
        call_row("app.shared", "app.changed_b"),
    ])
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    backend.attach_bounded_edges(
        facts, seed_symbols=["app.changed_a", "app.changed_b"],
        limits=GraphSliceLimits(max_depth=1),
    )

    assert len(client.seed_call_requests) == 1
    assert set(client.seed_call_requests[0]) == {"app.changed_a", "app.changed_b"}


# --------------------------------------------------------------------------
# Fallback policy: "no result" is not "query failed"
# --------------------------------------------------------------------------


def test_empty_successful_slice_does_not_trigger_a_full_sweep(tmp_path: Path) -> None:
    """The critical fallback rule. A slice that legitimately finds nothing
    must be kept as a valid empty answer, never escalated into the
    repository-wide sweep this work exists to avoid."""
    client = SpyCBMClient(
        call_rows=[call_row("app.unrelated", "app.other")],
        # `app.changed` is a real, resolvable symbol that simply has no
        # edges — the case that must NOT escalate to a full sweep. (A seed
        # with no canonical identity at all is a different condition, and
        # has its own test below.)
        symbol_rows={"Function": [
            ["changed", "svc.py", "1", "2", "", "true", "app.changed"],
        ]},
    )
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(facts, seed_symbols=["app.changed"])

    assert outcome.canonical_seed_count == 1, "the seed must genuinely resolve"
    assert outcome.used_slice is True
    assert outcome.fell_back is False
    assert facts.call_edges == []
    assert client.all_call_edges_calls == 0
    assert client.all_usage_edges_calls == 0


def test_a_real_slice_query_failure_falls_back_to_the_full_sweep(tmp_path: Path) -> None:
    client = SpyCBMClient(
        call_rows=[call_row("app.handler", "app.changed")],
        seed_error=CodeIntelligenceError("CBM transport failed"),
    )
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(facts, seed_symbols=["app.changed"])

    assert outcome.used_slice is False
    assert outcome.fell_back is True
    assert "CBM transport failed" in (outcome.reason or "")
    # Analysis is preserved rather than lost.
    assert client.all_call_edges_calls == 1
    assert len(facts.call_edges) == 1


def test_no_seed_symbols_does_not_sweep_the_repository(tmp_path: Path) -> None:
    """Nothing consumes these edges without a seed symbol, so the most
    expensive possible query is also the least useful one."""
    client = SpyCBMClient(call_rows=[call_row("app.a", "app.b")])
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(facts, seed_symbols=[])

    assert outcome.used_slice is False
    assert outcome.fell_back is False
    assert client.graph_query_calls == 0
    assert facts.call_edges == []


# --------------------------------------------------------------------------
# Truncation semantics
# --------------------------------------------------------------------------


def test_a_truncated_slice_reports_truncation_with_its_reason(tmp_path: Path) -> None:
    client = SpyCBMClient(call_rows=[
        call_row("app.a", "app.changed"),
        call_row("app.b", "app.changed"),
        call_row("app.c", "app.changed"),
    ])
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(
        facts, seed_symbols=["app.changed"], limits=GraphSliceLimits(max_edges=1),
    )

    assert outcome.truncated is True
    assert outcome.truncation_reason == "max_edges reached"
    # Partial evidence is preserved, not discarded.
    assert len(facts.call_edges) >= 1


def test_the_graph_call_cap_is_enforced_on_the_production_path(tmp_path: Path) -> None:
    client = SpyCBMClient(call_rows=[
        call_row("app.h1", "app.changed"), call_row("app.h2", "app.h1"),
        call_row("app.h3", "app.h2"), call_row("app.h4", "app.h3"),
    ])
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(
        facts, seed_symbols=["app.changed"],
        limits=GraphSliceLimits(max_depth=10, max_graph_calls=3),
    )

    assert outcome.graph_calls <= 3
    assert client.graph_query_calls <= 3
    assert outcome.truncated is True


def test_a_fully_explored_slice_is_not_marked_truncated(tmp_path: Path) -> None:
    client = SpyCBMClient(call_rows=[call_row("app.handler", "app.changed")])
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)

    outcome = backend.attach_bounded_edges(
        facts, seed_symbols=["app.changed"],
        limits=GraphSliceLimits(max_depth=4, max_nodes=100, max_edges=100, max_graph_calls=20),
    )

    assert outcome.truncated is False
    assert outcome.truncation_reason is None


# --------------------------------------------------------------------------
# Run-local cache lifetime
# --------------------------------------------------------------------------


def test_the_graph_cache_is_shared_across_slice_activity_in_one_run(tmp_path: Path) -> None:
    """One backend instance == one run, so a repeated identical hop query
    within that run is served from memory rather than re-issued."""
    client = SpyCBMClient(call_rows=[call_row("app.handler", "app.changed")])
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update([RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True)
    limits = GraphSliceLimits(max_depth=1)

    backend.attach_bounded_edges(facts, seed_symbols=["app.changed"], limits=limits)
    backend.attach_bounded_edges(facts, seed_symbols=["app.changed"], limits=limits)

    assert len(client.seed_call_requests) == 1
    assert len(client.seed_usage_requests) == 1


def test_a_new_run_does_not_reuse_the_previous_runs_cache(tmp_path: Path) -> None:
    client = SpyCBMClient(call_rows=[call_row("app.handler", "app.changed")])
    limits = GraphSliceLimits(max_depth=1)

    for _ in range(2):
        backend = CBMCodeIntelligence(client=client)  # a fresh "run"
        facts = backend.build_or_update(
            [RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True,
        )
        backend.attach_bounded_edges(facts, seed_symbols=["app.changed"], limits=limits)

    assert len(client.seed_call_requests) == 2


# --------------------------------------------------------------------------
# Before/after call-count proof, on the backend acquisition path
# --------------------------------------------------------------------------


def test_before_after_graph_query_counts_on_the_production_acquisition_path(
    tmp_path: Path,
) -> None:
    """The number the report cites. Same synthetic repository, same backend,
    same public entry point — only the acquisition strategy differs."""
    from sydes.code_intelligence.cbm_client import _BULK_QUERY_PAGE_SIZE

    relevant = [call_row("app.handler", "app.changed"), call_row("app.top", "app.handler")]
    # Enough unrelated edges to force real multi-page pagination on the
    # repository-wide sweep, which is exactly how cost scales with repo size.
    unrelated = [call_row(f"app.u{i}", f"app.u{i + 1}") for i in range(_BULK_QUERY_PAGE_SIZE * 3)]
    usage = [usage_row("app.user", "app.changed")]

    # OLD: the real `CBMClient` over a paging session, so the page count is
    # the genuine one `_rows` produces rather than a double's shortcut.
    from sydes.code_intelligence.cbm_client import CBMClient

    all_calls = relevant + unrelated

    class _PagingSession:
        def __init__(self) -> None:
            self.calls = 0

        def call_tool(self, tool, arguments, *, timeout=None):
            self.calls += 1
            query = arguments["query"]
            source = all_calls if ":CALLS]" in query else usage
            offset = int(query.rsplit("SKIP ", 1)[1].split(" ")[0])
            return {"rows": source[offset:offset + _BULK_QUERY_PAGE_SIZE]}

    session = _PagingSession()
    old_client = CBMClient(session)
    old_client.all_call_edges("proj")
    old_call_pages = session.calls
    old_client.all_usage_edges("proj")
    old_total = session.calls
    old_usage_pages = old_total - old_call_pages

    new_client = SpyCBMClient(call_rows=all_calls, usage_rows=usage)
    new_backend = CBMCodeIntelligence(client=new_client)
    new_facts = new_backend.build_or_update(
        [RepoRef(name=REPO, root=str(tmp_path))], defer_edges=True,
    )
    new_backend.attach_bounded_edges(new_facts, seed_symbols=["app.changed"])
    new_total = new_client.graph_query_calls

    # Recorded here so the report can quote real numbers rather than an
    # estimate: pagination on the old path scales with repository edge
    # count, the new path is capped regardless of it.
    assert old_call_pages >= 4 and old_usage_pages >= 1
    assert old_total >= 5
    assert new_total <= GraphSliceLimits().max_graph_calls
    assert new_total < old_total
    # The relevant structure survived the reduction.
    assert any(e["caller_qualified_name"] == "app.handler" for e in new_facts.call_edges)


# --------------------------------------------------------------------------
# Analyzer level: the real verify-change path
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "svc.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    (root / "svc.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
    return root


class _SpyBackend:
    """Wraps a real `CBMCodeIntelligence` over a spy client, so the analyzer
    exercises the genuine backend integration rather than a stub of it."""

    name = CBM_BACKEND

    def __init__(self, client: SpyCBMClient) -> None:
        self.client = client
        self._real = CBMCodeIntelligence(client=client)
        self.defer_edges_seen: list[bool] = []

    def build_or_update(self, repos, *, workspace_id=None, root=None, defer_edges=False):
        self.defer_edges_seen.append(defer_edges)
        return self._real.build_or_update(
            repos, workspace_id=workspace_id, root=root, defer_edges=defer_edges,
        )

    def attach_bounded_edges(self, facts, **kwargs):
        return self._real.attach_bounded_edges(facts, **kwargs)


def _analyze(repo: Path, backend: _SpyBackend, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "sydes.verify.analyzer.get_code_intelligence", lambda backend_name=None: backend,
    )
    return analyze_change(
        repos=[RepoRef(name=REPO, root=str(repo))],
        options=VerifyChangeOptions(
            base="main", llm_policy="never", run_tests=False, impact_guide="off",
        ),
    )


def test_the_real_analysis_path_never_sweeps_the_repository_edge_tables(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline production invariant: a normal change with resolvable
    symbols must not reach `all_call_edges`/`all_usage_edges` at all."""
    client = SpyCBMClient(
        call_rows=[call_row("app.views.handle", "app.svc.changed", caller_file="views.py")],
        symbol_rows={"Function": [["changed", "svc.py", "1", "2", "", "true", "app.svc.changed"]]},
    )
    backend = _SpyBackend(client)

    result = _analyze(git_repo, backend, monkeypatch)

    assert backend.defer_edges_seen == [True], "analyzer must defer edge materialization"
    assert client.all_call_edges_calls == 0
    assert client.all_usage_edges_calls == 0
    assert client.seed_call_requests, "the bounded seed-scoped path must be used"
    assert client.graph_query_calls <= GraphSliceLimits().max_graph_calls
    assert any("graph_slice:" in line for line in result.diagnostics)


def test_the_changed_symbol_is_actually_among_the_slice_seeds(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyCBMClient(
        call_rows=[call_row("app.views.handle", "app.svc.changed", caller_file="views.py")],
        symbol_rows={"Function": [["changed", "svc.py", "1", "2", "", "true", "app.svc.changed"]]},
    )
    backend = _SpyBackend(client)

    _analyze(git_repo, backend, monkeypatch)

    seeded = {name for request in client.seed_call_requests for name in request}
    # The changed symbol reaches CBM under its CANONICAL identity, not the
    # short display name the diff produced — that substitution is the fix.
    assert "app.svc.changed" in seeded
    assert "changed" not in seeded


def test_a_truncated_slice_adds_one_bounded_exploration_note(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyCBMClient(
        call_rows=[
            call_row("app.a", "app.svc.changed"), call_row("app.b", "app.svc.changed"),
            call_row("app.c", "app.svc.changed"),
        ],
        symbol_rows={"Function": [["changed", "svc.py", "1", "2", "", "true", "app.svc.changed"]]},
    )
    backend = _SpyBackend(client)
    monkeypatch.setattr(
        "sydes.code_intelligence.cbm.GraphSliceLimits",
        lambda *a, **k: GraphSliceLimits(max_edges=1),
    )

    result = _analyze(git_repo, backend, monkeypatch)

    bounded = [n for n in result.analysis_notes if n.startswith("Structural exploration was bounded")]
    assert len(bounded) == 1, "exactly one bounded-exploration note, never duplicated"
    assert "max_edges reached" in bounded[0]


def test_regression_short_display_name_reaches_the_canonical_graph_node(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact observed production failure, end to end.

    Real runs reported `node_count: 0, edge_count: 0, truncated: false` on
    repositories whose changed symbols plainly had callers. The diff yields
    a short display name (`changed`), while CBM's graph node and its edges
    are keyed by a fully module-qualified name — so the pre-fix exact `IN`
    match on the display name returned nothing at all.
    """
    canonical = "code.example.io/services/svc.changed"
    caller = "code.example.io/routers/repo.HandleChanged"
    user = "code.example.io/services/other.Uses"
    client = SpyCBMClient(
        call_rows=[call_row(caller, canonical, caller_file="routers/repo.go")],
        usage_rows=[usage_row(user, canonical, user_file="services/other.go")],
        symbol_rows={"Function": [
            ["changed", "svc.py", "1", "2", "", "true", canonical],
            ["HandleChanged", "routers/repo.go", "1", "2", "", "true", caller],
            ["Uses", "services/other.go", "1", "2", "", "true", user],
        ]},
    )
    backend = _SpyBackend(client)

    result = _analyze(git_repo, backend, monkeypatch)

    # The pre-fix behavior: the short display name never appears in a query.
    seeded = {name for request in client.seed_call_requests for name in request}
    assert "changed" not in seeded, "the un-canonicalized display name must not be queried"
    assert canonical in seeded

    # Recall is restored: real CALLS and USAGE edges come back, and the node
    # accounting reflects the merged slice rather than the zeros observed.
    assert len(result.change.symbols) == 1
    assert any("graph_slice:" in line for line in result.diagnostics)
    slice_line = next(line for line in result.diagnostics if line.startswith("graph_slice:"))
    assert "call_edges=1" in slice_line
    assert "usage_edges=1" in slice_line
    assert "nodes=0" not in slice_line
    assert "canonical_seeds=0" not in slice_line
    assert "unresolved_seeds=0" in slice_line

    # And the acquisition path is still bounded — no repository-wide sweep.
    assert client.all_call_edges_calls == 0
    assert client.all_usage_edges_calls == 0
    assert client.graph_query_calls <= GraphSliceLimits().max_graph_calls


def test_an_unresolved_changed_symbol_is_reported_and_never_sweeps(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identity Sydes could not map is a hole in what was looked at — it
    must be said out loud, and must NOT escalate to a full-repo sweep."""
    client = SpyCBMClient(
        call_rows=[call_row("mod.a", "mod.b")],
        symbol_rows={"Function": [
            # `changed` is indexed for attribution but carries no canonical
            # identity, so it cannot be resolved to a graph node.
            ["changed", "svc.py", "1", "2", "", "true", ""],
        ]},
    )
    backend = _SpyBackend(client)

    result = _analyze(git_repo, backend, monkeypatch)

    notes = [
        n for n in result.analysis_notes
        if n.startswith("No structural graph identity could be resolved")
    ]
    assert len(notes) == 1, "reported exactly once, never duplicated"
    assert "`changed`" in notes[0]
    # Crucially: no repository-wide fallback just to avoid the uncertainty.
    assert client.all_call_edges_calls == 0
    assert client.all_usage_edges_calls == 0


def test_a_complete_slice_adds_no_bounded_exploration_note(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyCBMClient(
        call_rows=[call_row("app.views.handle", "app.svc.changed", caller_file="views.py")],
        symbol_rows={"Function": [["changed", "svc.py", "1", "2", "", "true", "app.svc.changed"]]},
    )
    backend = _SpyBackend(client)

    result = _analyze(git_repo, backend, monkeypatch)

    assert not any(
        note.startswith("Structural exploration was bounded")
        for note in result.analysis_notes
    )
