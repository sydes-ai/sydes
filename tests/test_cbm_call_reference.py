"""CBM's `CALL_REFERENCE` relationship -- a symbol passed by name/value rather
than invoked (a handler registered with a route builder, a callback passed to
another function, an axum `.route(path, get(handler))`, ...) -- must reach
Sydes' structural facts the same way a `USAGE` edge does, in both the
full-repository sweep and the bounded/seed-scoped path, without ever being
promoted to a proven direct call.

Confirmed against a real repo (`sydes-examples/realworld-axum-sqlx`): CBM's
own graph already links a router's registration function to a
cross-file-imported handler via `CALL_REFERENCE`, but neither
`all_usage_edges` nor `usage_edges_for_seeds` asked for that relationship
type at all -- the edge existed in CBM and was invisible to Sydes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sydes.code_intelligence.cbm import CBMCodeIntelligence
from sydes.code_intelligence.cbm_client import CBMClient, ClientMetrics
from sydes.core.models import RepoRef

REPO = "app"


class FakeSession:
    """A scripted CBM session: canned replies, recorded calls."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.metrics = ClientMetrics()

    def call_tool(self, tool: str, arguments: dict[str, Any],
                  *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        reply = self.replies.get(tool)
        if callable(reply):
            return reply(arguments)
        return reply if reply is not None else {"rows": []}

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# 1. CALL_REFERENCE is queried -- full sweep and seed-scoped both ask for it.
# --------------------------------------------------------------------------


def test_all_usage_edges_query_includes_call_reference() -> None:
    session = FakeSession()
    client = CBMClient(session)

    client.all_usage_edges("proj")

    query = session.calls[0][1]["query"]
    assert "USAGE" in query
    assert "CALL_REFERENCE" in query


def test_usage_edges_for_seeds_query_includes_call_reference() -> None:
    session = FakeSession()
    client = CBMClient(session)

    client.usage_edges_for_seeds("proj", ["app.x"], limit=25)

    query = session.calls[0][1]["query"]
    assert "USAGE" in query
    assert "CALL_REFERENCE" in query


# --------------------------------------------------------------------------
# 3. Seed-scoped and full-sweep modes agree on which relationship types they
#    ask for -- a `defer_edges=True` bounded build must not see a narrower or
#    wider edge set than a full sweep would for the same neighborhood.
# --------------------------------------------------------------------------


def test_full_sweep_and_seed_scoped_usage_queries_use_the_same_relationship_pattern() -> None:
    session = FakeSession()
    client = CBMClient(session)

    client.all_usage_edges("proj")
    client.usage_edges_for_seeds("proj", ["app.x"])

    full_query = session.calls[0][1]["query"]
    seeded_query = session.calls[1][1]["query"]
    assert "[:USAGE|CALL_REFERENCE]" in full_query
    assert "[:USAGE|CALL_REFERENCE]" in seeded_query


# --------------------------------------------------------------------------
# 4. Existing CALLS semantics are unchanged -- CALL_REFERENCE must never leak
#    into the direct-call queries.
# --------------------------------------------------------------------------


def test_call_queries_remain_calls_only() -> None:
    session = FakeSession()
    client = CBMClient(session)

    client.all_call_edges("proj")
    client.call_edges_for_seeds("proj", ["app.x"])

    for _, arguments in session.calls:
        query = arguments["query"]
        assert "CALL_REFERENCE" not in query
        assert "[:CALLS]" in query


# --------------------------------------------------------------------------
# 2 & 5. A CALL_REFERENCE-sourced row reaches the same generic `usage_edges`
#    representation downstream, and never appears in `call_edges` -- so it
#    can never be mistaken for a proven direct call by anything reading
#    `StructuralFacts`.
# --------------------------------------------------------------------------


class FakeClient:
    """A CBM client double whose `all_usage_edges` stands in for a query that
    now also matches `CALL_REFERENCE` rows -- from `CBMClient`'s Cypher RETURN
    clause alone there is no way to tell a `USAGE` row from a `CALL_REFERENCE`
    row apart (the relationship type itself is never selected), which is
    exactly the point: both must produce the identical, generic edge shape.
    """

    def __init__(self, usage_rows: list[list[str]], call_rows: list[list[str]] | None = None) -> None:
        self._usage_rows = usage_rows
        self._call_rows = call_rows or []
        self.metrics: dict = {"calls": 0, "session_start_ms": 0, "mean_call_ms": 0}
        self.server_version = "fake"
        self.malformed_rows = 0

    def index_repository(self, repo_path, *, mode: str = "fast") -> dict[str, Any]:
        return {"project": "proj", "nodes": 1, "excluded": {"dirs": []}}

    def all_symbols(self, project: str, label: str) -> list[list[str]]:
        return []

    def all_imports(self, project: str) -> list[list[str]]:
        return []

    def decorated_symbols(self, project: str, *, page_size: int = 500) -> list[dict]:
        return []

    def all_call_edges(self, project: str) -> list[list[str]]:
        return self._call_rows

    def all_usage_edges(self, project: str) -> list[list[str]]:
        return self._usage_rows


def _build(client: FakeClient, repo_root: Path) -> Any:
    backend = CBMCodeIntelligence(client=client)
    return backend.build_or_update(
        [RepoRef(name=REPO, root=str(repo_root))],
        defer_edges=False,
    )


def test_a_call_reference_shaped_row_lands_in_usage_edges_not_call_edges(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.rs").write_text("fn router() {}\n", encoding="utf-8")
    (tmp_path / "src" / "listing.rs").write_text("fn feed_articles() {}\n", encoding="utf-8")

    # Shape returned by `all_usage_edges` once it also matches CALL_REFERENCE
    # rows -- a router function referencing a handler defined elsewhere,
    # never invoked directly from this row's own perspective.
    usage_row = ["app.mod.router", "src/mod.rs", "app.listing.feed_articles", "src/listing.rs"]
    facts = _build(FakeClient(usage_rows=[usage_row], call_rows=[]), tmp_path)

    assert facts.call_edges == []
    assert len(facts.usage_edges) == 1
    edge = facts.usage_edges[0]
    assert edge["user_symbol"] == "router"
    assert edge["used_symbol"] == "feed_articles"
    assert edge["used_file"] == "src/listing.rs"
    assert edge["source"] == "cbm"


def test_call_edges_are_unaffected_by_usage_edges_containing_a_reference(tmp_path: Path) -> None:
    """A real CALLS edge alongside a CALL_REFERENCE-shaped USAGE row must
    keep landing in `call_edges` exactly as before -- the two lists stay
    independent."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")

    call_row = ["app.a", "src/a.py", "1", "app.b", "src/b.py", "2"]
    usage_row = ["app.a", "src/a.py", "app.c", "src/c.py"]
    facts = _build(FakeClient(usage_rows=[usage_row], call_rows=[call_row]), tmp_path)

    assert len(facts.call_edges) == 1
    assert facts.call_edges[0]["callee_symbol"] == "b"
    assert len(facts.usage_edges) == 1
    assert facts.usage_edges[0]["used_symbol"] == "c"
