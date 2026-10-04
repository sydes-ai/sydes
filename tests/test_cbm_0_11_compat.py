"""Codebase Memory 0.11 response shapes (recorded from the real 0.11.0 server).

0.11 changed `query_graph` in three ways that each silently emptied Sydes'
structural facts while the run still exited 0: the default tree format writes
shared prefixes as `@N+suffix` behind a `rows_refs:` table (Sydes now always
asks for JSON, which never abbreviates), a response shows at most 200 rows
unless asked for more, and `search_graph` cuts a page at an output budget and
continues from `next_offset`. Seen on a 5-language
evaluation: "56 query row(s) did not match the expected column arity",
`changed_symbols=0`, no impact analysis at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from test_cbm_client import FakeSession

from sydes.code_intelligence import cbm_client
from sydes.code_intelligence.cbm_client import CBMClient, parse_rows, resolve_executable

P = "proj"

def test_queries_ask_for_json_rows_and_lift_the_row_cap() -> None:
    session = FakeSession({"query_graph": {
        "columns": ["a", "b"], "rows": [["x", None]], "has_more": False, "truncated": False}})
    client = CBMClient(session)
    rows = client._rows(P, "MATCH (a) RETURN a.x, a.y", columns=2, order_by="a.x")
    arguments = session.calls[0][1]
    assert arguments["format"] == "json"
    assert arguments["max_rows"] > 200
    assert rows == [["x", ""]]  # JSON null is empty, never the string "None"


def test_a_cut_response_is_continued_from_next_offset() -> None:
    pages = {
        None: {"columns": ["a", "b"], "rows": [["r1", "f"], ["r2", "f"]],
               "has_more": True, "truncated": True, "truncation_reason": "output_budget",
               "next_offset": 2},
        2: {"columns": ["a", "b"], "rows": [["r3", "f"]], "has_more": False, "truncated": False},
    }
    session = FakeSession({"query_graph": lambda arguments: pages[arguments.get("offset")]})
    client = CBMClient(session)
    rows = client._rows(P, "MATCH (a) RETURN a.x, a.y", columns=2, order_by="a.x")
    assert [row[0] for row in rows] == ["r1", "r2", "r3"]
    assert client.truncated_responses == 0


def test_a_cut_response_that_cannot_be_continued_is_counted() -> None:
    session = FakeSession({"query_graph": {
        "columns": ["a", "b"], "rows": [["r1", "f"]], "has_more": True, "truncated": True}})
    client = CBMClient(session)
    client._rows(P, "MATCH (a) RETURN a.x, a.y", columns=2, order_by="a.x")
    assert client.truncated_responses == 1


def test_structured_rows_of_the_wrong_arity_are_still_refused() -> None:
    rows, malformed = parse_rows({"rows": [["a", "b"], ["only"]]}, columns=2)
    assert rows == [["a", "b"]] and malformed == 1


def _search_page(names: list[str], *, next_offset: int | None) -> dict:
    page = {
        "cols": ["name", "label", "lines", "in", "out", "decorators", "signature",
                 "route_method", "route_path"],
        "groups": [{"qn_prefix": "proj.app", "file": "app/views.py",
                    "rows": [[n, "Function", "1-2", 0, 0, "@route('/x')", None, None, None]
                             for n in names]}],
        "has_more": next_offset is not None, "truncated": next_offset is not None,
    }
    if next_offset is not None:
        page["next_offset"] = next_offset
        page["truncation_reason"] = "output_budget"
    return page


def test_decorated_symbols_resume_where_a_budget_cut_page_stopped() -> None:
    """0.11 cut the page after 2 of 500 rows; advancing by the page size
    would skip the other 498 without a trace."""
    def reply(arguments: dict) -> dict:
        if arguments.get("label") != "Function":
            return _search_page([], next_offset=None)
        if arguments.get("offset", 0) == 0:
            return _search_page(["a", "b"], next_offset=2)
        assert arguments["offset"] == 2
        return _search_page(["c"], next_offset=None)

    session = FakeSession({"search_graph": reply})
    names = [row["name"] for row in CBMClient(session).decorated_symbols(P)]
    assert names == ["a", "b", "c"]


def test_the_cbm_installed_with_sydes_wins_over_one_earlier_on_path(monkeypatch, tmp_path: Path) -> None:
    """A self-updating global binary first on PATH replaced the pinned one."""
    env_bin = tmp_path / "env" / "bin"
    global_bin = tmp_path / "global"
    for directory in (env_bin, global_bin):
        directory.mkdir(parents=True)
        binary = directory / "codebase-memory-mcp"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(env_bin / "python"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "env"))
    monkeypatch.setenv("PATH", f"{global_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv(cbm_client.CBM_EXECUTABLE_ENV_VAR, raising=False)

    assert resolve_executable() == str(env_bin / "codebase-memory-mcp")
    # an explicit choice still wins
    monkeypatch.setenv(cbm_client.CBM_EXECUTABLE_ENV_VAR, str(global_bin / "codebase-memory-mcp"))
    assert resolve_executable() == str(global_bin / "codebase-memory-mcp")


class _VersionedSession(FakeSession):
    def __init__(self, version: str) -> None:
        super().__init__({})
        self.server_info = {"name": "codebase-memory-mcp", "version": version}
        self._executable = "/somewhere/codebase-memory-mcp"


def test_a_server_other_than_the_pinned_version_is_reported(monkeypatch) -> None:
    """A self-updating global binary first on PATH once replaced the pinned one silently."""
    monkeypatch.setattr(cbm_client, "pinned_version", lambda: "0.11.0")
    assert CBMClient(_VersionedSession("0.11.0")).version_mismatch() is None
    mismatch = CBMClient(_VersionedSession("0.12.0")).version_mismatch()
    assert mismatch is not None
    assert "0.12.0" in mismatch and "pinned codebase-memory-mcp 0.11.0" in mismatch
    assert "/somewhere/codebase-memory-mcp" in mismatch


def test_without_the_package_an_external_server_is_not_judged(monkeypatch) -> None:
    monkeypatch.setattr(cbm_client, "pinned_version", lambda: None)
    assert CBMClient(_VersionedSession("0.12.0")).version_mismatch() is None
