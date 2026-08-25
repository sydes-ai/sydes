"""Lifecycle and error propagation for the persistent CBM session.

The transport is a subprocess speaking JSON-RPC over a pipe, so the failures
worth pinning are the ones that pipe can produce: a server that reports an
error, a server that dies mid-conversation, a call that never answers, and a
handshake that never completes. Each must surface as an explicit
`CodeIntelligenceError` — never as a silent fallback to the CLI or to the
native backend, because a verdict built on facts from an engine the operator
did not choose is unreadable.

A fake session stands in for the daemon so these run without one.
"""

from __future__ import annotations

from typing import Any

import pytest

from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.code_intelligence.cbm_client import (
    CBMClient,
    ClientMetrics,
    parse_rows,
)


class FakeSession:
    """A scripted CBM session: canned replies, recorded calls."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0
        self.metrics = ClientMetrics()

    def call_tool(self, tool: str, arguments: dict[str, Any],
                  *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        self.metrics.record(tool, 1.0)
        reply = self.replies.get(tool)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(arguments)
        return reply if reply is not None else {}

    def close(self) -> None:
        self.closed += 1


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_one_session_serves_many_queries() -> None:
    """The point of the refactor: many calls, one session."""
    session = FakeSession({"query_graph": {"_text": "  a b\n"}})
    client = CBMClient(session)

    client.all_call_edges("proj")
    client.all_imports("proj")
    client.all_usage_edges("proj")

    assert len(session.calls) == 3
    assert session.closed == 0, "querying must not tear the session down"


def test_close_releases_the_session() -> None:
    session = FakeSession()
    CBMClient(session).close()

    assert session.closed == 1


def test_context_manager_closes_on_exit() -> None:
    session = FakeSession()
    with CBMClient(session):
        pass

    assert session.closed == 1


def test_context_manager_closes_even_when_the_body_raises() -> None:
    """A failed analysis must not leak a subprocess."""
    session = FakeSession()
    with pytest.raises(ValueError):
        with CBMClient(session):
            raise ValueError("boom")

    assert session.closed == 1


def test_metrics_report_session_and_call_cost() -> None:
    session = FakeSession({"query_graph": {"_text": ""}})
    session.metrics.session_start_ms = 1500.0
    client = CBMClient(session)
    client.all_imports("proj")

    metrics = client.metrics
    assert metrics["session_start_ms"] == 1500.0
    assert metrics["calls"] == 1
    assert metrics["calls_by_tool"]["query_graph"] == 1


# --------------------------------------------------------------------------
# Error propagation — explicit, never silent
# --------------------------------------------------------------------------


def test_tool_error_propagates_rather_than_returning_empty() -> None:
    """An error must not look like 'this repository has no call edges'."""
    session = FakeSession({"query_graph": CodeIntelligenceError("graph unavailable")})
    client = CBMClient(session)

    with pytest.raises(CodeIntelligenceError, match="graph unavailable"):
        client.all_call_edges("proj")


def test_indexing_without_a_project_id_is_an_error() -> None:
    """A reply that omits the project id cannot be used, so it must raise."""
    session = FakeSession({"index_repository": {"nodes": 10}})
    client = CBMClient(session)

    with pytest.raises(CodeIntelligenceError, match="did not return a project id"):
        client.index_repository("/tmp/whatever")


def test_missing_executable_names_the_explicit_alternative(monkeypatch) -> None:
    from sydes.code_intelligence.cbm_client import CBM_EXECUTABLE_ENV_VAR, resolve_executable

    monkeypatch.setenv(CBM_EXECUTABLE_ENV_VAR, "/nonexistent/cbm-binary")
    with pytest.raises(CodeIntelligenceError) as excinfo:
        resolve_executable()

    message = str(excinfo.value)
    assert "not found" in message
    assert "native" in message, "the error should name the explicit fallback"


# --------------------------------------------------------------------------
# Bulk sweep pagination (the saleor/saleor#19675 CBM transport-limit fix):
# a whole-repository sweep with no LIMIT could exceed CBM's ~10 MB MCP
# transport cap on a large enough codebase. Paginate with SKIP/LIMIT
# instead of ever raising the transport limit.
# --------------------------------------------------------------------------


def _text_rows(rows: list[tuple[str, str]]) -> dict:
    body = "\n".join(f"  {a} {b}" for a, b in rows)
    return {"_text": body}


def test_bulk_sweep_stops_after_a_partial_page_with_a_single_call() -> None:
    from sydes.code_intelligence.cbm_client import _BULK_QUERY_PAGE_SIZE

    session = FakeSession({"query_graph": _text_rows([("a", "1"), ("b", "2")])})
    client = CBMClient(session)

    rows = client._rows("proj", "MATCH (a) RETURN a.x, a.y", columns=2, order_by="a.x")

    assert rows == [["a", "1"], ["b", "2"]]
    assert len(session.calls) == 1  # a page smaller than the page size means "done"
    query = session.calls[0][1]["query"]
    assert "ORDER BY a.x" in query
    assert f"LIMIT {_BULK_QUERY_PAGE_SIZE}" in query
    assert "SKIP 0" in query


def test_bulk_sweep_pages_through_a_full_first_page() -> None:
    """A codebase large enough to fill one page must trigger a second call
    at the next SKIP offset, rather than silently truncating results."""
    from sydes.code_intelligence.cbm_client import _BULK_QUERY_PAGE_SIZE

    first_page = [(f"sym{i}", "f.py") for i in range(_BULK_QUERY_PAGE_SIZE)]
    second_page = [("last", "f.py")]

    def reply(arguments: dict) -> dict:
        if "SKIP 0 " in arguments["query"]:
            return _text_rows(first_page)
        return _text_rows(second_page)

    session = FakeSession({"query_graph": reply})
    client = CBMClient(session)

    rows = client._rows("proj", "MATCH (a) RETURN a.x, a.y", columns=2, order_by="a.x")

    assert len(rows) == _BULK_QUERY_PAGE_SIZE + 1
    assert rows[-1] == ["last", "f.py"]
    assert len(session.calls) == 2  # exactly one extra page, not an unbounded loop


def test_bulk_sweep_page_size_is_conservatively_bounded_relative_to_the_transport_cap() -> None:
    """No generic guarantee about row byte size, but the chosen page size
    must be small enough that the intent — staying under CBM's ~10 MB
    response cap — is credible, not merely nominal."""
    from sydes.code_intelligence.cbm_client import _BULK_QUERY_PAGE_SIZE

    assert _BULK_QUERY_PAGE_SIZE <= 10_000


# --------------------------------------------------------------------------
# Row parsing
# --------------------------------------------------------------------------


def test_rows_of_the_wrong_arity_are_dropped_not_mis_split() -> None:
    """A mis-split row would silently put a file path in a symbol column."""
    payload = {"_text": "rows: 2\n  a b c\n  only two\n  d e f\n"}

    rows, malformed = parse_rows(payload, columns=3)

    assert rows == [["a", "b", "c"], ["d", "e", "f"]]
    assert malformed == 1


def test_structured_rows_are_preferred_when_present() -> None:
    payload = {"rows": [["x", "y"]]}

    rows, malformed = parse_rows(payload, columns=2)

    assert rows == [["x", "y"]]
    assert malformed == 0


def test_malformed_rows_are_counted_on_the_client() -> None:
    session = FakeSession({"query_graph": {"_text": "  a b\n  bad\n"}})
    client = CBMClient(session)

    client._rows("proj", "MATCH ...", columns=2, order_by="a")

    assert client.malformed_rows == 1


# --------------------------------------------------------------------------
# Higher-level tools are preferred over raw Cypher
# --------------------------------------------------------------------------


def test_caller_tracing_uses_cbms_own_traversal() -> None:
    """Sydes should not re-implement BFS that CBM already bounds and paginates."""
    session = FakeSession({"trace_path": {"callers_total": 0}})
    client = CBMClient(session)

    client.trace_callers("proj", "some_function", depth=2)

    tool, arguments = session.calls[0]
    assert tool == "trace_path"
    assert arguments["direction"] == "inbound"
    assert arguments["mode"] == "calls"
    assert arguments["depth"] == 2


def test_decorated_symbols_use_search_graph_not_a_tabular_query() -> None:
    """Decorator text contains newlines, which a row parser cannot carry."""
    payload = {
        "cols": ["name", "decorators", "route_method", "route_path"],
        "groups": [
            {
                "qn_prefix": "app.views",
                "file": "app/views.py",
                "rows": [["handler", ["@route(\n  \"/x\",\n)"], "GET", "/x"]],
            }
        ],
        "has_more": False,
    }
    session = FakeSession({"search_graph": payload})
    client = CBMClient(session)

    records = client.decorated_symbols("proj")

    assert session.calls[0][0] == "search_graph"
    assert records[0]["qualified_name"] == "app.views.handler"
    assert records[0]["file"] == "app/views.py"
    assert "\n" in records[0]["decorators"], "multi-line decorator text survives"


def test_symbols_without_decorators_or_routes_are_skipped() -> None:
    payload = {
        "cols": ["name", "decorators", "route_method", "route_path"],
        "groups": [
            {
                "qn_prefix": "app.util",
                "file": "app/util.py",
                "rows": [["plain", None, None, None], ["routed", None, "GET", "/y"]],
            }
        ],
        "has_more": False,
    }
    session = FakeSession({"search_graph": payload})

    records = CBMClient(session).decorated_symbols("proj")

    assert [record["name"] for record in records] == ["routed", "routed"]
