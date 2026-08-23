"""The bounded guide loop wired into `ImpactInterpreter.interpret`.

These pin the M3 contract at the interpreter level, with a scripted fake
guide standing in for a live model: the loop must only ever run on symbols
the deterministic pass already left unresolved, must never itself declare an
entrypoint affected, and any resolution it does produce must trace back to a
concrete piece of evidence — either a graph edge the bounded walk simply
never reached, or a source reference the executor actually read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.guide import GuideError
from sydes.impact.interpreter import GuideBudget, ImpactInterpreter
from sydes.impact.models import (
    ACTION_INSPECT_ENCLOSING_FUNCTION,
    ACTION_INSPECT_NEARBY_ENTRYPOINTS,
    ACTION_STOP_UNRESOLVED,
    ACTION_TRACE_CALLERS,
    GUIDE_ALWAYS,
    GUIDE_AUTO,
    GUIDE_OFF,
    PROVENANCE_DETERMINISTIC,
    PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED,
    STRATEGY_GUIDED_INVESTIGATION,
    InvestigationDecision,
)

REPO = "app"


class ScriptedGuide:
    """Returns one scripted `InvestigationDecision` per call, in order.

    Raises if asked for more decisions than scripted, so a test that expects
    the loop to stop after N turns fails loudly if it does not.
    """

    def __init__(self, decisions: list[InvestigationDecision | Exception]) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    def investigate(self, question):
        self.calls += 1
        if not self._decisions:
            raise AssertionError("guide asked for more turns than scripted")
        next_item = self._decisions.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


def call_edge(caller: str, callee: str, *, caller_file: str = "app/svc.py", callee_file: str = "app/svc.py") -> dict:
    return {
        "repo": REPO,
        "caller_file": caller_file, "caller_symbol": caller,
        "caller_qualified_name": f"app.{caller}", "caller_line": 1,
        "callee_file": callee_file, "callee_symbol": callee,
        "callee_qualified_name": f"app.{callee}", "callee_line": 2,
    }


def entrypoint(symbol: str, *, file: str = "app/views.py", method="GET", path="/x") -> dict:
    return {
        "repo": REPO, "qualified_name": f"app.{symbol}", "symbol": symbol,
        "file": file, "line": 10, "route_method": method, "route_path": path,
        "decorators": "", "signature": "",
    }


def facts(**kwargs) -> StructuralFacts:
    return StructuralFacts(
        call_edges=kwargs.get("call_edges", []),
        usage_edges=kwargs.get("usage_edges", []),
        entrypoints=kwargs.get("entrypoints", []),
        symbol_index=kwargs.get("symbol_index", {"repos": []}),
        provides_call_graph=True,
        backend="cbm",
    )


def changed(name: str, *, file: str = "app/svc.py") -> list[dict]:
    return [{"name": name, "file": file, "repo": REPO}]


def test_a_deterministically_resolved_symbol_never_calls_the_guide() -> None:
    """A symbol that already reaches an entrypoint must not spend a turn."""
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler")],
    )
    guide = ScriptedGuide([])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("helper"), f, repo=REPO)
    assert result.affected  # resolved deterministically
    assert guide.calls == 0
    assert result.metrics["guide_triggered"] is False


def test_a_dead_end_triggers_the_guide_in_auto_mode() -> None:
    """A symbol with a partial path but no entrypoint must reach the guide."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([InvestigationDecision(action=ACTION_STOP_UNRESOLVED)])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert not result.affected
    assert guide.calls == 1
    assert result.metrics["guide_triggered"] is True
    assert result.unresolved[0].guide_investigated is True


def test_guide_is_never_consulted_when_policy_is_off_even_with_a_guide_present() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_OFF)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert guide.calls == 0
    assert result.metrics["guide_triggered"] is False


def test_stop_unresolved_ends_the_loop_immediately() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([InvestigationDecision(action=ACTION_STOP_UNRESOLVED)])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_ALWAYS, guide_budget=GuideBudget(max_turns_per_symbol=3, max_turns_total=8))
    interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert guide.calls == 1  # never used its remaining 2 per-symbol turns


def test_guide_error_fails_closed_and_leaves_the_symbol_unresolved() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([GuideError("provider timeout")])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert not result.affected
    assert result.metrics["guide_errors"] == 1


def test_repeating_the_same_decision_is_detected_as_no_progress() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    same = InvestigationDecision(action=ACTION_TRACE_CALLERS, target="leaf")
    guide = ScriptedGuide([same, same, same])  # a third would prove the loop kept going
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, guide_budget=GuideBudget(max_turns_per_symbol=3, max_turns_total=8))
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    # Turn 1 finds the (already-known) edge and re-evaluates without
    # resolving; turn 2 asks again, gets the identical decision, and the loop
    # stops right there rather than asking a third time.
    assert guide.calls == 2
    assert result.metrics["guide_no_progress"] == 1


def test_per_symbol_budget_is_enforced() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    decisions = [
        InvestigationDecision(action=ACTION_TRACE_CALLERS, target="leaf"),
        InvestigationDecision(action=ACTION_INSPECT_NEARBY_ENTRYPOINTS, target="app/svc.py"),
        InvestigationDecision(action=ACTION_INSPECT_NEARBY_ENTRYPOINTS, target="app/other.py"),
    ]
    guide = ScriptedGuide(decisions)
    interpreter = ImpactInterpreter(
        guide=guide, guide_policy=GUIDE_AUTO, guide_budget=GuideBudget(max_turns_per_symbol=2, max_turns_total=8),
    )
    interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert guide.calls == 2  # third decision never consumed


def test_total_budget_is_enforced_across_symbols() -> None:
    f = facts(call_edges=[
        call_edge("caller_one", "leaf_one"),
        call_edge("caller_two", "leaf_two"),
    ])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(
        guide=guide, guide_policy=GUIDE_AUTO, guide_budget=GuideBudget(max_turns_per_symbol=3, max_turns_total=1),
    )
    result = interpreter.interpret(
        [{"name": "leaf_one", "file": "app/svc.py", "repo": REPO},
         {"name": "leaf_two", "file": "app/svc.py", "repo": REPO}],
        f, repo=REPO,
    )
    assert guide.calls == 1
    assert result.metrics["guide_budget_exhausted"] is True


@pytest.fixture()
def chat_repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "chat.py").write_text(
        "def chat_completion():\n"
        "    payload = build_payload()\n"
        "    process_chat_response(payload)\n"
        "    return payload\n",
        encoding="utf-8",
    )
    return tmp_path


def _chat_facts() -> StructuralFacts:
    # Reachability walks *backward* from the changed symbol toward its
    # callers, so the graph fact needed here is "process_chat_response calls
    # streaming_chat_response_handler" — the reverse of the missing
    # "chat_completion calls process_chat_response" edge this test exists to
    # recover.
    return facts(
        call_edges=[call_edge(
            "process_chat_response", "streaming_chat_response_handler",
            caller_file="app/chat.py", callee_file="app/svc.py",
        )],
        entrypoints=[entrypoint("chat_completion", file="app/chat.py", method="POST", path="/api/chat/completions")],
        symbol_index={"repos": [{"repo": REPO, "files": [{"path": "app/chat.py", "symbols": [
            {"name": "chat_completion", "file": "app/chat.py", "start_line": 1, "end_line": 4, "language": "python"},
        ]}]}]},
    )


def test_source_confirmed_edge_recovers_the_missing_entrypoint(chat_repo: Path) -> None:
    """The open-webui #28872 shape: CBM has no edge from chat_completion to
    process_chat_response, but the guide finds it structurally nearby and the
    executor confirms the reference by reading the real source."""
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_INSPECT_NEARBY_ENTRYPOINTS, target="app/chat.py"),
        InvestigationDecision(action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="chat_completion"),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, repo_root=chat_repo)
    result = interpreter.interpret(changed("streaming_chat_response_handler"), _chat_facts(), repo=REPO)

    # One confirmation for discovering chat_completion as a nearby candidate,
    # one for the source reference the executor actually found in its body.
    assert result.metrics["evidence_confirmed"] == 2
    assert len(result.affected) == 1
    recovered = result.affected[0]
    assert recovered.route_method == "POST"
    assert recovered.route_path == "/api/chat/completions"
    assert recovered.strategies == [STRATEGY_GUIDED_INVESTIGATION]

    path = recovered.paths[0]
    provenances = {step.provenance for step in path.steps}
    assert PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED in provenances
    # The first hop (the real call edge to process_chat_response) is still a
    # deterministic fact; only the guided hop is tagged as guided.
    assert PROVENANCE_DETERMINISTIC in provenances


@pytest.fixture()
def chat_repo_no_reference(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "chat.py").write_text(
        "def chat_completion():\n"
        "    payload = build_payload()\n"
        "    return payload\n",
        encoding="utf-8",
    )
    return tmp_path


def test_unconfirmed_hypothesis_is_not_promoted(chat_repo_no_reference: Path) -> None:
    """If source inspection finds no reference, the entrypoint must not be
    fabricated even though it genuinely exists elsewhere in the facts."""
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_INSPECT_NEARBY_ENTRYPOINTS, target="app/chat.py"),
        InvestigationDecision(action=ACTION_INSPECT_ENCLOSING_FUNCTION, target="chat_completion"),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, repo_root=chat_repo_no_reference)
    result = interpreter.interpret(changed("streaming_chat_response_handler"), _chat_facts(), repo=REPO)
    assert not result.affected
    # The nearby-entrypoint discovery still counts as evidence (chat_completion
    # is a real candidate); only the source claim about it fails to confirm.
    assert result.metrics["evidence_confirmed"] == 1
    assert result.unresolved[0].guide_investigated is True
