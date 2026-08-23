"""The executor: the only thing in M3 that touches a graph or a file.

Every action is checked against a minimal fake graph index (satisfying the
same shape `_FactIndex` exposes) plus, for source-inspection actions, a real
tiny file on disk. The one rule under test throughout: a decision whose
target (or sought_symbol) was not already surfaced to the question is
rejected before anything is queried, and a source claim is only "found" when
the text is actually there — pointing at the exact line it was found on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.impact.investigate import (
    PROVENANCE_EXECUTOR_REJECTED,
    PROVENANCE_SOURCE_INSPECTION,
    InvestigationExecutor,
)
from sydes.impact.models import (
    ACTION_INSPECT_ENCLOSING_FUNCTION,
    ACTION_INSPECT_NEARBY_ENTRYPOINTS,
    ACTION_STOP_UNRESOLVED,
    ACTION_TRACE_CALLERS,
    ACTION_TRACE_USAGES,
    InvestigationDecision,
    RELATION_CALLS,
    SymbolIdentity,
)

REPO = "app"


def identity(name: str, file: str, *, qualified: str = "", line: int | None = None) -> SymbolIdentity:
    return SymbolIdentity.from_fields(repo=REPO, file=file, qualified_name=qualified, short_name=name, line=line)


class FakeIndex:
    """A minimal stand-in for `_FactIndex`, built by hand per test."""

    def __init__(self, *, inbound=None, entrypoints=None, decorator_refs=None, signature_refs=None) -> None:
        self._inbound = inbound or {}
        self.entrypoints = entrypoints or []
        self._decorator_refs = decorator_refs or {}
        self._signature_refs = signature_refs or {}

    def inbound(self, ident):
        return self._inbound.get(ident.key, [])

    def entrypoints_referencing(self, name):
        return self._decorator_refs.get(name, [])

    def entrypoints_with_signature_reference(self, name):
        return self._signature_refs.get(name, [])


class FakeFacts:
    """Stands in for `StructuralFacts.symbols_for_file`.

    Real CBM/native facts always carry a full symbol entry (with `end_line`
    and `language`) for anything actually defined in a file; `by_file` lets a
    test supply exactly that, matching what production data looks like.
    """

    def __init__(self, by_file: dict[str, list[dict]] | None = None) -> None:
        self._by_file = by_file or {}

    def symbols_for_file(self, repo, path):
        return self._by_file.get(path, [])


def test_target_not_in_known_is_rejected_without_querying() -> None:
    index = FakeIndex()
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action=ACTION_TRACE_CALLERS, target="nonexistent_symbol")
    evidence = executor.execute(decision, known={}, origins={})
    assert evidence.found is False
    assert evidence.provenance == PROVENANCE_EXECUTOR_REJECTED


def test_trace_callers_finds_an_existing_inbound_call_edge() -> None:
    target = identity("process_chat_response", "app/chat.py")
    caller = identity("chat_completion", "app/chat.py")
    index = FakeIndex(inbound={target.key: [(RELATION_CALLS, caller, {})]})
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action=ACTION_TRACE_CALLERS, target="process_chat_response")
    evidence = executor.execute(
        decision, known={"process_chat_response": target}, origins={},
    )
    assert evidence.found is True
    assert "chat_completion" in evidence.detail


def test_trace_callers_reports_nothing_when_the_graph_has_no_edge() -> None:
    target = identity("process_chat_response", "app/chat.py")
    index = FakeIndex(inbound={})
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action=ACTION_TRACE_CALLERS, target="process_chat_response")
    evidence = executor.execute(
        decision, known={"process_chat_response": target}, origins={},
    )
    assert evidence.found is False


def test_trace_usages_only_matches_usage_relation_not_calls() -> None:
    target = identity("Widget", "app/models.py")
    caller = identity("compose", "app/svc.py")
    index = FakeIndex(inbound={target.key: [(RELATION_CALLS, caller, {})]})
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action=ACTION_TRACE_USAGES, target="Widget")
    evidence = executor.execute(decision, known={"Widget": target}, origins={})
    assert evidence.found is False  # only a CALLS edge exists, not USAGE


def test_find_decorator_references_uses_the_index_lookup() -> None:
    target = identity("SomePermission", "app/perm.py")
    entry = {"symbol": "handle", "qualified_name": "app.handle", "file": "app/views.py"}
    index = FakeIndex(decorator_refs={"SomePermission": [entry]})
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action="find_decorator_references", target="SomePermission")
    evidence = executor.execute(decision, known={"SomePermission": target}, origins={})
    assert evidence.found is True
    assert "handle" in evidence.detail


def test_inspect_nearby_entrypoints_is_restricted_to_a_known_file() -> None:
    origin = identity("process_chat_response", "app/chat.py")
    index = FakeIndex(entrypoints=[
        {"repo": REPO, "symbol": "chat_completion", "file": "app/chat.py", "qualified_name": "app.chat_completion"},
    ])
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    known = {"process_chat_response": origin}
    decision = InvestigationDecision(action=ACTION_INSPECT_NEARBY_ENTRYPOINTS, target="app/chat.py")
    evidence = executor.execute(decision, known=known, origins={})
    assert evidence.found is True
    assert "chat_completion" in known  # the executor grows `known` in place


def test_inspect_nearby_entrypoints_rejects_an_unknown_file() -> None:
    origin = identity("process_chat_response", "app/chat.py")
    index = FakeIndex(entrypoints=[{"repo": REPO, "symbol": "x", "file": "app/other.py"}])
    executor = InvestigationExecutor(index=index, facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action=ACTION_INSPECT_NEARBY_ENTRYPOINTS, target="some/unrelated/file.py")
    evidence = executor.execute(decision, known={"process_chat_response": origin}, origins={})
    assert evidence.found is False
    assert evidence.provenance == PROVENANCE_EXECUTOR_REJECTED


def test_stop_unresolved_produces_no_finding() -> None:
    executor = InvestigationExecutor(index=FakeIndex(), facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(action=ACTION_STOP_UNRESOLVED)
    evidence = executor.execute(decision, known={}, origins={})
    assert evidence.found is False


def test_sought_symbol_not_in_origins_is_rejected_without_querying() -> None:
    """A source-confirming decision's `sought_symbol` must come from the
    question's `candidate_origins`, not merely from `known` — a name that is
    a legal `target` but was never offered as an origin must still fail."""
    target = identity("chat_completion", "app/chat.py", line=1)
    executor = InvestigationExecutor(index=FakeIndex(), facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(
        action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="chat_completion",
        sought_symbol="process_chat_response",
    )
    evidence = executor.execute(decision, known={"chat_completion": target}, origins={})
    assert evidence.found is False
    assert evidence.provenance == PROVENANCE_EXECUTOR_REJECTED


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "chat.py").write_text(
        "def chat_completion():\n"
        "    x = 1\n"
        "    process_chat_response(x)\n"
        "    return x\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "other.py").write_text(
        "def unrelated():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    return tmp_path


def test_source_inspection_confirms_a_real_reference(repo_root: Path) -> None:
    sought = identity("process_chat_response", "app/chat.py")
    target = identity("chat_completion", "app/chat.py", line=1)
    index = FakeIndex()
    facts = FakeFacts({"app/chat.py": [
        {"name": "chat_completion", "file": "app/chat.py", "start_line": 1,
         "end_line": 4, "language": "python"},
    ]})
    executor = InvestigationExecutor(index=index, facts=facts, repo_root=repo_root)
    decision = InvestigationDecision(
        action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="chat_completion",
        sought_symbol="process_chat_response",
    )
    evidence = executor.execute(
        decision,
        known={"chat_completion": target, "process_chat_response": sought},
        origins={"process_chat_response": sought},
    )
    assert evidence.found is True
    assert evidence.provenance == PROVENANCE_SOURCE_INSPECTION
    assert evidence.sought_symbol == "process_chat_response"
    assert "process_chat_response" in evidence.matched_text
    # The exact matched line, not merely a line within the sliced block.
    assert evidence.line == 3


def test_source_inspection_does_not_promote_an_unconfirmed_hypothesis(repo_root: Path) -> None:
    sought = identity("process_chat_response", "app/chat.py")
    target = identity("unrelated", "app/other.py", line=1)
    index = FakeIndex()
    facts = FakeFacts({"app/other.py": [
        {"name": "unrelated", "file": "app/other.py", "start_line": 1,
         "end_line": 2, "language": "python"},
    ]})
    executor = InvestigationExecutor(index=index, facts=facts, repo_root=repo_root)
    decision = InvestigationDecision(
        action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="unrelated",
        sought_symbol="process_chat_response",
    )
    evidence = executor.execute(
        decision,
        known={"unrelated": target, "process_chat_response": sought},
        origins={"process_chat_response": sought},
    )
    assert evidence.found is False
    assert evidence.provenance == PROVENANCE_SOURCE_INSPECTION


def test_source_inspection_without_a_repo_root_does_not_fabricate_a_finding() -> None:
    sought = identity("process_chat_response", "app/chat.py")
    target = identity("chat_completion", "app/chat.py", line=1)
    executor = InvestigationExecutor(index=FakeIndex(), facts=FakeFacts(), repo_root=None)
    decision = InvestigationDecision(
        action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="chat_completion",
        sought_symbol="process_chat_response",
    )
    evidence = executor.execute(
        decision,
        known={"chat_completion": target, "process_chat_response": sought},
        origins={"process_chat_response": sought},
    )
    assert evidence.found is False


def test_source_evidence_points_to_the_exact_matched_line_not_a_merged_block(tmp_path: Path) -> None:
    """A long function whose body the statement splitter merges into one
    block must still report the real line the reference sits on."""
    (tmp_path / "app").mkdir()
    padding = "\n".join(f"    filler_{i} = {i}" for i in range(40))
    (tmp_path / "app" / "big.py").write_text(
        "def chat_completion():\n"
        f"{padding}\n"
        "    return process_chat_response(filler_0)\n",
        encoding="utf-8",
    )
    sought = identity("process_chat_response", "app/big.py")
    target = identity("chat_completion", "app/big.py", line=1)
    facts = FakeFacts({"app/big.py": [
        {"name": "chat_completion", "file": "app/big.py", "start_line": 1,
         "end_line": 42, "language": "python"},
    ]})
    executor = InvestigationExecutor(index=FakeIndex(), facts=facts, repo_root=tmp_path)
    decision = InvestigationDecision(
        action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="chat_completion",
        sought_symbol="process_chat_response",
    )
    evidence = executor.execute(
        decision,
        known={"chat_completion": target, "process_chat_response": sought},
        origins={"process_chat_response": sought},
    )
    assert evidence.found is True
    assert evidence.line == 42
    assert "process_chat_response" in evidence.matched_text
