"""M4: the guide as a semantic impact-inference layer, not just a graph navigator.

`ACTION_INFER_IMPACT` lets the guide propose `ImpactCandidate`s directly —
zero or more entrypoints/behaviors it believes this changed symbol plausibly
affects, each carrying its own confidence and rationale. The one rule under
test throughout: lack of deterministic corroboration must never mean the
candidate is discarded — it survives as `IMPACT_STATUS_INFERRED`, distinct
from (and always dominated by) a `IMPACT_STATUS_PROVEN` deterministic
result for the same entrypoint.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.guide import GuideError
from sydes.impact.interpreter import ImpactInterpreter
from sydes.impact.models import (
    ACTION_INFER_IMPACT,
    ACTION_STOP_UNRESOLVED,
    ENTRYPOINT_HTTP,
    GUIDE_AUTO,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    PROVENANCE_LLM_INFERRED_CORROBORATED,
    PROVENANCE_LLM_INFERRED_UNCORROBORATED,
    STRATEGY_LLM_SEMANTIC_INFERENCE,
    ImpactCandidate,
    InvestigationDecision,
)

REPO = "app"


class ScriptedGuide:
    """Returns one scripted `InvestigationDecision` per call, in order."""

    def __init__(self, decisions: list) -> None:
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


def test_deterministic_result_still_works_with_guide_disabled() -> None:
    """Test 1: `guide_policy=off` must be a complete no-op for M4 too — the
    deterministic path is unaffected by anything this task added."""
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler")],
    )
    guide = ScriptedGuide([])
    interpreter = ImpactInterpreter(guide=guide, guide_policy="off")
    result = interpreter.interpret(changed("helper"), f, repo=REPO)
    assert result.affected
    assert result.affected[0].status == IMPACT_STATUS_PROVEN
    assert guide.calls == 0
    assert result.llm_candidate_log == []


def test_uncorroborated_candidate_is_preserved_as_inferred() -> None:
    """Test 2: a candidate the guide proposes with no matching known
    entrypoint must not be silently dropped — the exact failure mode this
    task exists to fix."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="some background job that reads leaf's output",
                    confidence=0.4, reason="plausible shared dependency",
                    inference_type="semantic_indirect_dependency",
                    uncertainty="no route matches this description",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert len(result.affected) == 1
    inferred = result.affected[0]
    assert inferred.status == IMPACT_STATUS_INFERRED
    assert inferred.corroborated is False
    assert inferred.llm_confidence == 0.4
    assert inferred.llm_inference_type == "semantic_indirect_dependency"
    assert inferred.strategies == [STRATEGY_LLM_SEMANTIC_INFERENCE]
    provenances = {step.provenance for path in inferred.paths for step in path.steps}
    assert provenances == {PROVENANCE_LLM_INFERRED_UNCORROBORATED}
    # Structurally unresolved (no PROVEN path) even though a candidate exists —
    # the two concepts are tracked separately, on purpose.
    assert result.unresolved and result.unresolved[0].symbol == "leaf"
    assert result.metrics["llm_candidates"] == 1
    assert result.metrics["llm_candidates_accepted"] == 1
    assert result.metrics["llm_candidates_corroborated"] == 0
    assert result.metrics["llm_candidates_uncorroborated"] == 1


def test_corroborated_candidate_records_corroboration() -> None:
    """Test 3: a candidate whose route matches a real, already-known
    entrypoint must be marked corroborated, with the real route's own
    method/path/file — never the guide's own guess."""
    f = facts(
        call_edges=[call_edge("orphan_caller", "leaf")],
        entrypoints=[entrypoint("real_handler", file="app/views.py", method="GET", path="/cases")],
    )
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="GET /cases", entrypoint_symbol="real_handler",
                    confidence=0.9, reason="shares a query helper",
                    inference_type="shared_utility", uncertainty="graph has no direct edge",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert len(result.affected) == 1
    inferred = result.affected[0]
    assert inferred.status == IMPACT_STATUS_INFERRED
    assert inferred.corroborated is True
    assert inferred.kind == ENTRYPOINT_HTTP
    assert inferred.route_method == "GET"
    assert inferred.route_path == "/cases"
    assert inferred.symbol == "real_handler"
    provenances = {step.provenance for path in inferred.paths for step in path.steps}
    assert provenances == {PROVENANCE_LLM_INFERRED_CORROBORATED}
    assert result.metrics["llm_candidates_corroborated"] == 1


def test_deterministic_and_inferred_same_entrypoint_dedupes_with_proven_winning() -> None:
    """Test 4: when the same real entrypoint is found both deterministically
    and by the guide, the merged record must stay PROVEN — no duplicate
    INFERRED entry for the same route."""
    # `helper` resolves deterministically via `handler`; a second, unrelated
    # changed symbol triggers the guide, which (perhaps implausibly, but the
    # test only cares about merge behavior) proposes the *same* real route.
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="GET /x", entrypoint_symbol="handler",
                    confidence=0.5, reason="guessed the same route",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    changed_symbols = changed("helper") + [{"name": "orphan_caller2", "file": "app/svc.py", "repo": REPO}]
    # orphan_caller2 has no call edge at all, so it goes unresolved and
    # reaches the guide; the fixture above still only sets up one call edge,
    # which is enough — the guide loop only needs *a* symbol to trigger on.
    f2 = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler", file="app/views.py", method="GET", path="/x")],
    )
    result = interpreter.interpret(changed_symbols, f2, repo=REPO)

    matching = [e for e in result.affected if e.route_method == "GET" and e.route_path == "/x"]
    assert len(matching) == 1  # no duplicate
    assert matching[0].status == IMPACT_STATUS_PROVEN  # PROVEN wins
    assert matching[0].corroborated is False  # PROVEN record, not merged with LLM metadata
    assert guide.calls >= 1


def test_no_hardcoded_action_selection_does_not_block_infer_impact() -> None:
    """A guide that reaches directly for INFER_IMPACT on its very first turn
    (the primary M4 path) must work without any prerequisite graph-navigation
    turn — the interpreter must not require "context gathering" first."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(ImpactCandidate(entrypoint_label="some behavior", confidence=0.3),),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert guide.calls == 2  # infer_impact, then stop — no prerequisite navigation turn needed
    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED


def test_empty_candidate_list_is_a_legitimate_answer() -> None:
    """`ACTION_INFER_IMPACT` with zero candidates ("nothing plausible") must
    not error and must not fabricate anything."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_INFER_IMPACT, candidates=()),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert result.affected == []
    assert result.metrics["llm_candidates"] == 0


def test_guide_error_fails_closed_with_visible_detail() -> None:
    """Tests 6 and 7: a provider/parser failure must fail closed — no
    candidate fabricated, the symbol stays unresolved — and must leave a
    readable trace in diagnostics, not just an incremented counter."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([GuideError("OpenAI request failed for model 'gpt-5.5': boom")])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []  # fails closed: nothing fabricated
    assert result.unresolved and result.unresolved[0].symbol == "leaf"
    assert result.metrics["guide_errors"] == 1
    assert result.metrics["llm_candidates"] == 0
    assert any(
        "guide_error=" in detail and "gpt-5.5" in detail
        for detail in result.metrics["guide_error_details"]
    )


def test_metrics_contain_llm_candidate_information() -> None:
    """Test 10: aggregate metrics must expose the new M4 counters."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(entrypoint_label="POST /widgets", confidence=0.6, reason="a"),
                ImpactCandidate(entrypoint_label="unrelated background sweep", confidence=0.2, reason="b"),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    for key in (
        "llm_candidates", "llm_candidates_accepted", "llm_candidates_corroborated",
        "llm_candidates_uncorroborated", "llm_new_entrypoints",
    ):
        assert key in result.metrics
    assert result.metrics["llm_candidates"] == 2
    assert len(result.llm_candidate_log) == 2
    for record in result.llm_candidate_log:
        assert record["changed_symbol"] == "leaf"
        assert record["turn"] == 1
        assert "confidence" in record and "rationale" in record
        assert "corroborated" in record and "accepted" in record


def test_confidence_is_clamped_to_zero_one() -> None:
    """`ImpactCandidate` confidence must never leave [0, 1], even from a
    model that returns something outside that range."""
    assert ImpactCandidate(entrypoint_label="x", confidence=5.0).confidence == 1.0
    assert ImpactCandidate(entrypoint_label="x", confidence=-3.0).confidence == 0.0
    assert ImpactCandidate(entrypoint_label="x", confidence=0.42).confidence == 0.42


def test_non_http_inferred_entrypoint_is_not_silently_discarded() -> None:
    """Test 11: a candidate describing a non-HTTP behavior (a scheduled job,
    a generic behavior) must still land in `result.affected` — the impact
    layer's own generic entrypoint kinds already support this; nothing here
    is HTTP-only."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="the nightly digest scheduled job",
                    confidence=0.55, reason="reads leaf's cached output",
                    inference_type="semantic_indirect_dependency",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    assert len(result.affected) == 1
    entry = result.affected[0]
    assert entry.status == IMPACT_STATUS_INFERRED
    assert entry.kind != ENTRYPOINT_HTTP  # generic, not forced into an HTTP shape
    assert entry.route_method is None and entry.route_path is None
    assert entry.label == "the nightly digest scheduled job"
