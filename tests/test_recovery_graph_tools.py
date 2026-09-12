"""`sydes.recovery.graph_tools` -- optional CBM-backed callers/callees/
search, degrading to an ERROR observation rather than raising whenever CBM
is unavailable. No live CBM process is started in these tests; a fake
client factory stands in for `CBMClient.spawn`."""

from __future__ import annotations

from pathlib import Path

from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.recovery.graph_tools import CBMGraphTools


class _FakeClient:
    def __init__(self, project: str = "demo-project") -> None:
        self.project = project
        self.closed = False
        self.trace_callers_calls: list[tuple[str, str, int]] = []
        self.trace_callees_calls: list[tuple[str, str, int]] = []
        self.search_calls: list[tuple[str, str | None]] = []

    def index_repository(self, repo_path, mode="fast"):
        return {"project": self.project}

    def trace_callers(self, project, function_name, *, depth=3, limit=200):
        self.trace_callers_calls.append((project, function_name, depth))
        return {"callers_total": 1, "callers": [["a.b.c", 1]]}

    def trace_callees(self, project, function_name, *, depth=3, limit=200):
        self.trace_callees_calls.append((project, function_name, depth))
        return {"callees_total": 0, "callees": []}

    def search_symbols(self, project, *, name_pattern=None, query=None, label=None, file_pattern=None, fields=None, limit=100):
        self.search_calls.append((name_pattern, file_pattern))
        return {"total": 1, "count": 1, "groups": []}

    def resolve_qualified_name(self, project, bare_name, file_path):
        return f"pkg.{bare_name}" if bare_name == "matches" else None

    def call_edges_for_seeds(self, project, seed_qualified_names, *, limit=1000):
        return [["pkg.a", "a.ts", "1", "pkg.b", "b.ts", "2"]] if "pkg.a" in seed_qualified_names else []

    def usage_edges_for_seeds(self, project, seed_qualified_names, *, limit=1000):
        return []

    def decorated_symbols(self, project, *, page_size=500):
        return [{"qualified_name": "pkg.Decorated", "file": "d.ts", "decorators": "@Foo(Bar)"}]

    def close(self):
        self.closed = True


class _AlwaysFailsFactory:
    def __call__(self):
        raise CodeIntelligenceError("no CBM runtime available")


def test_trace_callers_returns_json_observation(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    out = tools.trace_callers("FooHandler.execute")
    assert '"callers_total":1' in out
    assert client.trace_callers_calls == [("demo-project", "FooHandler.execute", 3)]


def test_trace_callees_returns_json_observation(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    out = tools.trace_callees("FooHandler.execute", depth=5)
    assert '"callees_total":0' in out
    assert client.trace_callees_calls == [("demo-project", "FooHandler.execute", 5)]


def test_search_symbol_returns_json_observation(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    out = tools.search_symbol(".*getUser.*", file_pattern="controller")
    assert '"total":1' in out
    assert client.search_calls == [(".*getUser.*", "controller")]


def test_unavailable_cbm_degrades_to_error_string_never_raises(tmp_path: Path):
    tools = CBMGraphTools(tmp_path, client_factory=_AlwaysFailsFactory())
    assert tools.trace_callers("x").startswith("ERROR:")
    assert tools.trace_callees("x").startswith("ERROR:")
    assert tools.search_symbol("x").startswith("ERROR:")


def test_unavailable_reason_is_remembered_not_retried_every_call(tmp_path: Path):
    factory = _AlwaysFailsFactory()
    calls = {"count": 0}
    real_call = factory.__call__

    def counting_call():
        calls["count"] += 1
        return real_call()

    tools = CBMGraphTools(tmp_path, client_factory=counting_call)
    tools.trace_callers("x")
    tools.trace_callers("y")
    tools.trace_callees("z")
    assert calls["count"] == 1


def test_close_closes_the_underlying_client_once_connected(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    tools.trace_callers("x")
    tools.close()
    assert client.closed is True


def test_close_before_any_use_is_a_noop(tmp_path: Path):
    tools = CBMGraphTools(tmp_path, client_factory=lambda: _FakeClient())
    tools.close()  # must not raise


def test_resolve_qualified_name_delegates_to_client(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    assert tools.resolve_qualified_name("matches", "any.ts") == "pkg.matches"
    assert tools.resolve_qualified_name("nomatch", "any.ts") is None


def test_resolve_qualified_name_unavailable_returns_none_not_raise(tmp_path: Path):
    tools = CBMGraphTools(tmp_path, client_factory=_AlwaysFailsFactory())
    assert tools.resolve_qualified_name("x", "y.ts") is None


def test_decorated_symbols_delegates_to_client(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    rows = tools.decorated_symbols()
    assert rows == [{"qualified_name": "pkg.Decorated", "file": "d.ts", "decorators": "@Foo(Bar)"}]


def test_decorated_symbols_unavailable_returns_empty_list_not_raise(tmp_path: Path):
    tools = CBMGraphTools(tmp_path, client_factory=_AlwaysFailsFactory())
    assert tools.decorated_symbols() == []


def test_reachability_slice_finds_seeded_neighborhood(tmp_path: Path):
    client = _FakeClient()
    tools = CBMGraphTools(tmp_path, client_factory=lambda: client)
    slice_ = tools.reachability_slice(["pkg.a"], max_depth=2)
    assert slice_ is not None
    assert {n.get("qualified_name") for n in slice_.nodes.values()} == {"pkg.a", "pkg.b"}


def test_reachability_slice_unavailable_returns_none_not_raise(tmp_path: Path):
    tools = CBMGraphTools(tmp_path, client_factory=_AlwaysFailsFactory())
    assert tools.reachability_slice(["pkg.a"]) is None
