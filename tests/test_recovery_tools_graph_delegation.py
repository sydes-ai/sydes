"""`sydes.recovery.tools.RepoTools`'s optional CBM graph delegation, and
`sydes.recovery.react.run_tool`'s dispatch to the three new graph tools."""

from __future__ import annotations

from pathlib import Path

from sydes.recovery.react import run_tool
from sydes.recovery.tools import RepoTools, ToolCallRecord


class _StubGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def trace_callers(self, symbol: str, *, depth: int = 3) -> str:
        self.calls.append(("trace_callers", symbol, str(depth)))
        return '{"callers_total":1}'

    def trace_callees(self, symbol: str, *, depth: int = 3) -> str:
        self.calls.append(("trace_callees", symbol, str(depth)))
        return '{"callees_total":0}'

    def search_symbol(self, name_pattern: str, *, file_pattern: str | None = None) -> str:
        self.calls.append(("search_symbol", name_pattern, file_pattern or ""))
        return '{"total":0}'


def test_repo_tools_without_graph_returns_error_and_records_the_call(tmp_path: Path):
    tools = RepoTools(tmp_path)
    out = tools.trace_callers("Foo.bar")
    assert out.startswith("ERROR:")
    assert tools.trace_callees("Foo.bar").startswith("ERROR:")
    assert tools.search_symbol("Foo.*").startswith("ERROR:")


def test_repo_tools_with_graph_delegates_and_records_ok_call(tmp_path: Path):
    graph = _StubGraph()
    tools = RepoTools(tmp_path, graph=graph)
    out = tools.trace_callers("Foo.bar", depth=2)
    assert out == '{"callers_total":1}'
    assert graph.calls == [("trace_callers", "Foo.bar", "2")]
    record = tools.calls[-1]
    assert isinstance(record, ToolCallRecord)
    assert record.tool == "trace_callers"
    assert record.ok is True


def test_repo_tools_records_failed_call_as_not_ok(tmp_path: Path):
    class _FailingGraph:
        def trace_callers(self, symbol, *, depth=3):
            return "ERROR: CBM graph unavailable (boom)"

    tools = RepoTools(tmp_path, graph=_FailingGraph())
    tools.trace_callers("Foo.bar")
    assert tools.calls[-1].ok is False


def test_run_tool_dispatches_trace_callers(tmp_path: Path):
    graph = _StubGraph()
    tools = RepoTools(tmp_path, graph=graph)
    out = run_tool(tools, "trace_callers", {"symbol": "X.y", "depth": 4})
    assert out == '{"callers_total":1}'
    assert graph.calls == [("trace_callers", "X.y", "4")]


def test_run_tool_dispatches_trace_callees(tmp_path: Path):
    graph = _StubGraph()
    tools = RepoTools(tmp_path, graph=graph)
    out = run_tool(tools, "trace_callees", {"symbol": "X.y"})
    assert out == '{"callees_total":0}'
    assert graph.calls == [("trace_callees", "X.y", "3")]  # default depth


def test_run_tool_dispatches_search_symbol(tmp_path: Path):
    graph = _StubGraph()
    tools = RepoTools(tmp_path, graph=graph)
    out = run_tool(tools, "search_symbol", {"name_pattern": "Foo.*"})
    assert out == '{"total":0}'
    assert graph.calls == [("search_symbol", "Foo.*", "")]


def test_run_tool_requires_symbol_for_trace_callers(tmp_path: Path):
    tools = RepoTools(tmp_path, graph=_StubGraph())
    assert run_tool(tools, "trace_callers", {}).startswith("ERROR:")


def test_run_tool_requires_name_pattern_for_search_symbol(tmp_path: Path):
    tools = RepoTools(tmp_path, graph=_StubGraph())
    assert run_tool(tools, "search_symbol", {}).startswith("ERROR:")
