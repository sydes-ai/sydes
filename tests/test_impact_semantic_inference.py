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
                    based_on_changed_symbols=("leaf",),
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
            candidates=(
                ImpactCandidate(
                    entrypoint_label="some behavior", confidence=0.3, reason="plausible dependency",
                    based_on_changed_symbols=("leaf",),
                ),
            ),
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
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        GuideError("OpenAI request failed for model 'gpt-5.5': boom"),
    ])
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
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
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
                    based_on_changed_symbols=("leaf",),
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


# --- Task item 7: inference metadata hygiene --------------------------------

def test_uncorroborated_inferred_impact_keeps_the_changed_symbols_own_repo() -> None:
    """An uncorroborated candidate has no matched entrypoint to take a repo
    from — it must fall back to the changed symbol's own real repo, never
    an empty string that would later collapse an accepted-impact id into
    `impact::symbol` instead of `impact:app:symbol`."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="process_chat_response", confidence=0.4,
                    reason="shares a formatting helper",
                    based_on_changed_symbols=("leaf",),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert len(result.affected) == 1
    assert result.affected[0].repo == REPO
    assert result.affected[0].repo != ""


def test_candidate_with_no_causal_reason_is_not_accepted() -> None:
    """An accepted inference must say why — a candidate with an empty
    `reason` is rejected before merge, not silently accepted with a blank
    rationale a reviewer would see with nothing to evaluate."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(entrypoint_label="GET /cases", confidence=0.9, reason=""),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_missing_reason"] == 1
    assert result.metrics["llm_candidates_accepted"] == 0
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is False
    assert "missing_reason" in log_entry["rejection_reason"]


def test_confidence_outside_zero_one_is_clamped_not_trusted_verbatim() -> None:
    """`ImpactCandidate` already clamps confidence to [0, 1] on construction
    (a model returning `1.7` or `-3` is a formatting slip, not a real
    probability) — pinned here as a regression against the M4 semantic-
    inference contract, not newly introduced by this task."""
    candidate_high = ImpactCandidate(entrypoint_label="x", confidence=1.7, reason="y")
    candidate_low = ImpactCandidate(entrypoint_label="x", confidence=-3.0, reason="y")
    assert candidate_high.confidence == 1.0
    assert candidate_low.confidence == 0.0


# --- Grounding gate for a candidate that names a specific symbol -----------
#
# Regression coverage for the Gitea go-gitea/gitea#39062 manual review: Sydes
# emitted a discrete "notification handling for pull request reads" inferred
# impact whose only support was `SetIssueReadBy` — a real function the guide
# saw only because it appears, unchanged, in the surrounding source of a
# changed function. `SetIssueReadBy` was never touched by that diff and had
# no known structural path from anything that was.
#
# The fix only ever gates a candidate that names a specific
# `entrypoint_symbol` — a candidate naming none (a pure cross-symbol or
# whole-change hypothesis, M4's core value) is untouched by these tests and
# stays covered by `test_uncorroborated_candidate_is_preserved_as_inferred`
# and `test_whole_change_turn_can_yield_a_reviewer_grade_pr_wide_semantic_finding`
# above/in `test_impact_primary_semantic_loop.py`.

def test_candidate_naming_a_symbol_only_seen_in_unchanged_context_is_rejected() -> None:
    """The exact Gitea #39062 shape: a candidate names a real, otherwise
    unrelated symbol as its `entrypoint_symbol` and justifies itself by
    citing an invocation the guide saw in unchanged surrounding source —
    not evidence that *this* change affects it, and must be rejected."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="notification handling for related reads",
                    entrypoint_symbol="SetIssueReadBy",
                    confidence=0.6,
                    reason=(
                        "the changed function shows invocation of SetIssueReadBy, "
                        "which could affect notification read state"
                    ),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_ungrounded"] == 1
    assert result.metrics["llm_candidates_accepted"] == 0
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is False
    assert "ungrounded" in log_entry["rejection_reason"]


def test_candidate_naming_a_symbol_reached_by_a_changed_symbol_is_accepted() -> None:
    """Structural corroboration must still work: a real call edge from the
    changed symbol to the candidate's named symbol is deterministic
    evidence, even though the target symbol itself was never touched by
    this PR — exactly the "preserve legitimate impact discovery" case."""
    f = facts(call_edges=[
        call_edge("orphan_caller", "leaf"),
        call_edge("leaf", "downstream_notifier"),
    ])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="downstream notification dispatch",
                    entrypoint_symbol="downstream_notifier",
                    confidence=0.6,
                    reason="leaf calls downstream_notifier directly",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED
    assert result.metrics["llm_candidates_ungrounded"] == 0
    assert result.metrics["llm_candidates_accepted"] == 1


def test_candidate_naming_a_symbol_known_elsewhere_in_the_repo_but_unconnected_is_rejected() -> None:
    """A symbol Sydes' facts do know about — it has its own call edges
    elsewhere in the repo — is still rejected as grounding for *this*
    change when none of those edges originate from a changed symbol. Being
    known to the repo in general is not the same as being reached by the
    change under review."""
    f = facts(call_edges=[
        call_edge("orphan_caller", "leaf"),
        call_edge("some_unrelated_caller", "repo_context_symbol"),
    ])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="unrelated repo behavior",
                    entrypoint_symbol="repo_context_symbol",
                    confidence=0.5,
                    reason="repo_context_symbol is part of the same codebase",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_ungrounded"] == 1


def test_candidate_naming_another_changed_symbol_in_the_same_pr_is_accepted() -> None:
    """Direct grounding: a candidate's `entrypoint_symbol` naming a
    *different* symbol this same PR also changed is accepted with no route
    corroboration and no call/usage edge between the two at all — it is
    grounded directly in changed code, not in a structural inference."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # whole-change turn
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="companion update behavior",
                    entrypoint_symbol="sibling_changed_symbol",
                    confidence=0.7,
                    reason="sibling_changed_symbol is part of the same PR and shares this logic",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # leaf's turn ends here
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # sibling_changed_symbol's own turn
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    changed_symbols = changed("leaf") + [
        {"name": "sibling_changed_symbol", "file": "app/other.py", "repo": REPO},
    ]
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    accepted = [e for e in result.affected if e.symbol == "sibling_changed_symbol"]
    assert len(accepted) == 1
    assert accepted[0].status == IMPACT_STATUS_INFERRED
    assert accepted[0].llm_reason.startswith("sibling_changed_symbol is part of the same PR")


def test_test_only_changed_symbol_does_not_ground_a_production_candidate() -> None:
    """A changed symbol that is itself test code (per
    `is_production_boundary_candidate`, the same predicate `_record` already
    applies to deterministic impacts) must not be usable to ground an LLM
    candidate — neither by naming it directly nor via a call edge
    attributed to it. Test code is evidence a change happened; it is not a
    production boundary the change can be said to reach."""
    f = facts(call_edges=[
        call_edge("orphan_caller", "leaf"),
        call_edge("test_something", "helper_only_used_by_tests"),
    ])
    changed_symbols = changed("leaf") + [
        {"name": "test_something", "file": "tests/test_leaf.py", "repo": REPO},
    ]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # whole-change turn
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # leaf's turn
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="helper behavior used by tests",
                    entrypoint_symbol="helper_only_used_by_tests",
                    confidence=0.5,
                    reason="test_something calls helper_only_used_by_tests",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # test_something's turn ends here
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_ungrounded"] == 1


def test_grounding_gate_does_not_affect_a_coexisting_proven_deterministic_impact() -> None:
    """An ungrounded LLM candidate for one changed symbol must not disturb a
    real, deterministic PROVEN result for a different changed symbol in the
    same run — the two evidence tiers stay fully independent."""
    f = facts(
        call_edges=[
            call_edge("handler", "helper"),
            call_edge("orphan_caller", "leaf"),
        ],
        entrypoints=[entrypoint("handler")],
    )
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # whole-change turn
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="unrelated notification behavior",
                    entrypoint_symbol="SetIssueReadBy",
                    confidence=0.6,
                    reason="seen only in unchanged surrounding source",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    changed_symbols = changed("helper") + [{"name": "leaf", "file": "app/svc.py", "repo": REPO}]
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    proven = [e for e in result.affected if e.status == IMPACT_STATUS_PROVEN]
    assert len(proven) == 1
    assert proven[0].route_path == "/x"
    assert result.metrics["llm_candidates_ungrounded"] == 1
    assert not any(e.label == "unrelated notification behavior" for e in result.affected)


def test_grounding_check_reads_only_already_loaded_edges_no_new_query() -> None:
    """Architectural proof, not just behavior: grounding a candidate reads
    only `facts.call_edges`/`usage_edges`. Passing them in as plain,
    immutable tuples — no `.query()`, no lazy fetch, no method beyond
    ordinary iteration — must work identically; if grounding needed
    anything beyond what the deterministic pass already loaded, a plain
    tuple could not satisfy it."""
    f = facts(call_edges=tuple([call_edge("leaf", "downstream_notifier")]))
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="downstream notification dispatch",
                    entrypoint_symbol="downstream_notifier",
                    confidence=0.6,
                    reason="leaf calls downstream_notifier directly",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED


# --- The residual blank-`entrypoint_symbol` hole: `based_on_changed_symbols` -
#
# The entrypoint_symbol-based grounding gate above cannot see a candidate
# that never names a symbol at all. The real Gitea #39062 candidate was
# exactly this shape: `entrypoint_symbol=""` on the whole-change turn, a
# plausible `reason` citing `SetIssueReadBy` — a real function the guide saw
# only in unchanged surrounding source — with nothing structured to check it
# against. These tests recreate that exact shape and its legitimate
# counterpart.

def test_whole_change_candidate_with_blank_symbol_and_no_based_on_is_rejected() -> None:
    """Recreates the EXACT original failure: `entrypoint_symbol=""`, a
    plausible non-empty `reason` naming an unchanged downstream symbol, no
    structural corroboration, and no `based_on_changed_symbols` at all. This
    is the shape the entrypoint_symbol-based gate alone could never catch —
    it must now be rejected too."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="notification handling for related reads",
                    entrypoint_symbol="",
                    confidence=0.6,
                    reason=(
                        "the changed function shows invocation of SetIssueReadBy, "
                        "which could affect notification read state"
                    ),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_whole_change_unanchored"] == 1
    assert result.metrics["llm_candidates_accepted"] == 0
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is False
    assert "whole_change_unanchored" in log_entry["rejection_reason"]


def test_whole_change_candidate_with_based_on_production_changed_symbols_is_accepted() -> None:
    """The legitimate counterpart: no single symbol names the claim, but the
    guide lists which of this PR's own changed symbols it is synthesized
    from — the M4 whole-change capability this fix must not disable."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label=(
                        "Merge operations now survive caller cancellation across "
                        "several entrypoints"
                    ),
                    entrypoint_symbol="",
                    confidence=0.8,
                    reason="both changed symbols now run detached from the caller's request context",
                    based_on_changed_symbols=("leaf", "sibling_changed_symbol"),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # leaf's own turn
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # sibling_changed_symbol's own turn
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    changed_symbols = changed("leaf") + [
        {"name": "sibling_changed_symbol", "file": "app/other.py", "repo": REPO},
    ]
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED
    assert result.metrics["llm_candidates_whole_change_unanchored"] == 0
    assert result.metrics["llm_candidates_accepted"] == 1


def test_whole_change_candidate_with_one_valid_and_one_unknown_symbol_is_rejected() -> None:
    """`based_on_changed_symbols` is checked exactly, not partially: naming
    one real changed symbol alongside one that was never part of this PR
    must not let the whole claim through — that would let a false claim
    smuggle in behind a true one."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="notification handling for related reads",
                    entrypoint_symbol="",
                    confidence=0.6,
                    reason="leaf and an unrelated symbol both affect notification state",
                    based_on_changed_symbols=("leaf", "SetIssueReadBy"),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_whole_change_unanchored"] == 1


def test_whole_change_candidate_based_on_only_a_test_only_changed_symbol_is_rejected() -> None:
    """`based_on_changed_symbols` is checked against *production* changed
    symbols only (`is_production_boundary_candidate`, the same predicate
    `_record` already applies to deterministic impacts) — naming only a
    changed test file must not ground a production behavioral claim."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    changed_symbols = changed("leaf") + [
        {"name": "test_something", "file": "tests/test_leaf.py", "repo": REPO},
    ]
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="some downstream behavior",
                    entrypoint_symbol="",
                    confidence=0.5,
                    reason="test_something's change implies this behavior shifted",
                    based_on_changed_symbols=("test_something",),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # leaf's turn
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # test_something's turn
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_whole_change_unanchored"] == 1


def test_populated_entrypoint_symbol_on_whole_change_turn_still_uses_the_original_grounding_gate() -> None:
    """`based_on_changed_symbols` is only ever checked when `entrypoint_symbol`
    is blank — a whole-change candidate that *does* name a symbol remains
    governed exactly by the earlier entrypoint_symbol grounding gate,
    unmodified by this change."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="notification handling for related reads",
                    entrypoint_symbol="SetIssueReadBy",
                    confidence=0.6,
                    reason="the changed function shows invocation of SetIssueReadBy",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_ungrounded"] == 1
    assert result.metrics["llm_candidates_whole_change_unanchored"] == 0
