"""Code review sees the code the changed lines call or construct, chosen structurally.

Shape (neutral names): a changed handler hands its generator to a response wrapper class on
a line next to the change. The wrapper's implementation enters the review context because
of that call edge and its distance to the changed lines -- not because of any name. A callee
far from the changed lines, test code and changed symbols themselves are not added.
"""

from __future__ import annotations

from pathlib import Path

from sydes.code_intelligence.cbm_facts import Relation
from sydes.verify.analyzer import is_program_source
from sydes.verify.llm_findings import _validate_findings, build_code_review_context
from sydes.verify.models import ChangedFile, ChangedSymbol, ChangeSet, Hunk
from sydes.verify.review_context import select_related_code

P = "proj"
HANDLER, WRAPPER, FAR, TEST, ROUTE = (f"{P}.routes.handle", f"{P}.wrap.Wrapper", f"{P}.util.far",
                                      f"{P}.tests.test_x", f"{P}.main.route")


class _Facts:
    def __init__(self, relations: list[Relation]) -> None:
        self.rel = relations

    def relations(self, seeds: list[str]) -> dict[str, list[Relation]]:
        return {s: [r for r in self.rel if s in (r.source, r.target)] for s in seeds}


def _calls(source: str, sfile: str, target: str, tfile: str, line: int) -> Relation:
    return Relation(source, "Function", sfile, "CALLS", target, "Class", tfile, {"line": line})


def _setup(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "wrap.py").write_text("class Wrapper:\n    def __init__(self, it):\n        self.it = it\n")
    (tmp_path / "app" / "util.py").write_text("def far():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text("def route():\n    return handle()\n")
    index = {"repos": [{"repo": "app", "files": [
        {"path": "app/routes.py", "symbols": [{"name": "handle", "kind": "function", "start_line": 1, "end_line": 50,
                                               "cbm_qualified_name": HANDLER}]},
        {"path": "app/wrap.py", "symbols": [{"name": "Wrapper", "kind": "class", "start_line": 1, "end_line": 3,
                                             "cbm_qualified_name": WRAPPER}]},
        {"path": "app/util.py", "symbols": [{"name": "far", "kind": "function", "start_line": 1, "end_line": 2,
                                             "cbm_qualified_name": FAR}]},
        {"path": "app/main.py", "symbols": [{"name": "route", "kind": "function", "start_line": 1, "end_line": 2,
                                             "cbm_qualified_name": ROUTE}]},
        {"path": "tests/test_x.py", "symbols": [{"name": "test_x", "kind": "function", "start_line": 1, "end_line": 2,
                                                 "cbm_qualified_name": TEST}]},
    ]}]}
    change = ChangeSet(base="main", files=[ChangedFile(repo="app", path="app/routes.py", hunks=[Hunk(start_line=10, end_line=12)])],
                       symbols=[ChangedSymbol(id="s", repo="app", file="app/routes.py", name="handle", qualified_name="handle",
                                              cbm_qualified_name=HANDLER, kind="function", start_line=1, end_line=50)])
    facts = _Facts([
        _calls(HANDLER, "app/routes.py", WRAPPER, "app/wrap.py", 14),   # 2 lines from the change
        _calls(HANDLER, "app/routes.py", FAR, "app/util.py", 40),       # far from the change
        _calls(TEST, "tests/test_x.py", HANDLER, "app/routes.py", 1),   # a test caller
        _calls(ROUTE, "app/main.py", HANDLER, "app/routes.py", 2),      # a real caller
    ])
    return change, facts, index


def test_code_called_next_to_the_change_and_its_callers_are_added_with_reasons(tmp_path: Path) -> None:
    change, facts, index = _setup(tmp_path)
    entries, notes = select_related_code(change=change, facts=facts, symbol_index=index, repo="app",
                                         repo_root=tmp_path, program_source=is_program_source)
    assert [e["symbol"] for e in entries] == ["Wrapper", "route"]
    wrapper = entries[0]
    assert "class Wrapper" in wrapper["source"]
    assert wrapper["why"] == "handle constructs Wrapper at app/routes.py:14 (2 line(s) from a changed line)"
    assert "calls the changed handle" in entries[1]["why"]
    assert any(n.startswith("code_review_context_added: app/wrap.py:Wrapper") for n in notes)
    assert "code_review_context_related=2" in notes


def test_without_a_code_graph_nothing_is_added_and_that_is_said(tmp_path: Path) -> None:
    change, _, index = _setup(tmp_path)
    entries, notes = select_related_code(change=change, facts=None, symbol_index=index, repo="app",
                                         repo_root=tmp_path, program_source=is_program_source)
    assert entries == [] and notes == ["code_review_context_related=none (no code graph for this repository)"]


def test_related_code_reaches_the_review_context_and_backs_snippets(tmp_path: Path) -> None:
    change, facts, index = _setup(tmp_path)
    entries, _ = select_related_code(change=change, facts=facts, symbol_index=index, repo="app",
                                     repo_root=tmp_path, program_source=is_program_source)
    context = build_code_review_context(change=change, diff_text="diff --git a/app/routes.py", related_code=entries)
    assert context["related_code"][0]["symbol"] == "Wrapper"
    raw = {"findings": [{"severity": "P1", "title": "t", "file": "app/routes.py", "line": 11,
                         "evidence_snippet": "self.it = it"}]}
    findings, _ = _validate_findings(raw, context)
    assert findings and findings[0].evidence[0].snippet == "self.it = it"
    # a finding must still sit on a changed line of a changed file
    off = {"findings": [{"severity": "P1", "title": "t", "file": "app/wrap.py", "line": 3}]}
    assert _validate_findings(off, context)[0] == []
