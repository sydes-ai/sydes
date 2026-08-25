"""Fix for the #6155 Ruff-config failure mode: the guide proposed
`tool.ruff`, `tool.ruff.lint`, `tool.ruff.lint.mccabe` as INFERRED impacts of
changes to those exact keys — restating the changed configuration as its own
"impact" rather than naming a downstream behavior.

Two independent fixes are under test here:

1. A lightweight, deterministic self-reference check at the M4 boundary
   (`_is_self_referential` in `interpreter.py`) that rejects only the
   obvious case: the candidate *is* the changed symbol under a
   name/punctuation/case normalization. It is not a semantic classifier and
   is not meant to catch generic restatement in different words — that is
   the guide contract's job (the prompt), not a heuristic's.
2. The guide's own contract now explicitly distinguishes "no meaningful
   downstream impact" (a legitimate INFER_IMPACT answer with an empty
   candidate list) from a provider/parse failure — the former must never be
   treated as an error.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.guide import GuideError
from sydes.impact.interpreter import ImpactInterpreter
from sydes.impact.models import (
    ACTION_INFER_IMPACT,
    ACTION_STOP_UNRESOLVED,
    GUIDE_AUTO,
    IMPACT_STATUS_INFERRED,
    ImpactCandidate,
    InvestigationDecision,
)
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.models import AcceptedImpact, ChangeSet, ChangeVerificationResult

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


def changed(name: str, *, file: str = "pyproject.toml") -> list[dict]:
    return [{"name": name, "file": file, "repo": REPO}]


# --- Case A: obvious self-reference is rejected -----------------------------

def test_case_a_exact_self_reference_is_rejected() -> None:
    """changed symbol `tool.ruff`, candidate `tool.ruff` -> not accepted."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(entrypoint_label="tool.ruff", confidence=0.6, reason="ruff config changed"),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("tool.ruff"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates"] == 1
    assert result.metrics["llm_candidates_accepted"] == 0
    assert result.metrics["llm_candidates_self_referential"] == 1
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is False
    assert "self_referential" in log_entry["rejection_reason"]


def test_case_a_nested_self_reference_is_rejected_even_with_punctuation_variance() -> None:
    """changed symbol `tool.ruff.lint`, candidate `tool.ruff.lint` (and a
    punctuation-varied restatement) -> both rejected."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(entrypoint_label="tool.ruff.lint", confidence=0.6),
                ImpactCandidate(entrypoint_label="Tool Ruff Lint", confidence=0.5),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("tool.ruff.lint"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_self_referential"] == 2
    assert result.metrics["llm_candidates_accepted"] == 0


def test_case_a_a_genuinely_different_route_is_not_treated_as_self_referential() -> None:
    """`restricted_case_filter` -> `GET /cases` must not be caught by the
    self-reference guard — it names a different, downstream behavior."""
    f = facts(call_edges=[call_edge("orphan_caller", "restricted_case_filter")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="GET /cases", confidence=0.72,
                    reason="shares the case-query filtering path", inference_type="shared_utility",
                    uncertainty="no direct graph edge",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("restricted_case_filter", file="app/svc.py"), f, repo=REPO)

    assert result.metrics["llm_candidates_self_referential"] == 0
    assert result.metrics["llm_candidates_accepted"] == 1


# --- Case B/D: no-meaningful-impact and generic config self-description -----

def test_case_b_generic_config_self_description_is_not_forced_into_a_candidate() -> None:
    """The guide contract, not a deterministic classifier, is responsible for
    recognizing that "McCabe lint config changes lint behavior" is a
    restatement, not a downstream impact. This test proves the *legitimate*
    way to express that: an INFER_IMPACT turn with zero candidates, which
    must be accepted cleanly, not treated as a failure."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT, candidates=(),
            rationale="no meaningful downstream impact inferred",
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("tool.ruff.lint.mccabe"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["guide_errors"] == 0
    assert result.metrics["llm_candidates"] == 0
    assert result.metrics["llm_no_candidate_turns"] == 1


def test_case_d_explicit_no_impact_conclusion_is_valid_not_an_error() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_INFER_IMPACT, candidates=()),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf", file="app/svc.py"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["guide_errors"] == 0
    assert result.metrics["llm_no_candidate_turns"] == 1
    assert result.llm_candidate_log == []  # nothing to log: no candidates were proposed


# --- Case C: meaningful downstream inference survives without corroboration -

def test_case_c_meaningful_uncorroborated_inference_survives_and_is_visible_in_report() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "restricted_case_filter")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="GET /cases", confidence=0.72,
                    reason="participates in the same case-query filtering path",
                    inference_type="shared_utility", uncertainty="no direct graph edge",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("restricted_case_filter", file="app/svc.py"), f, repo=REPO)

    assert len(result.affected) == 1
    inferred = result.affected[0]
    assert inferred.status == IMPACT_STATUS_INFERRED
    assert inferred.corroborated is False  # no known route named "GET /cases" in this fixture

    impact = AcceptedImpact(
        id="impact:app:restricted_case_filter", label="GET /cases", repo="app",
        status="inferred", route_method="GET", route_path="/cases",
        llm_confidence=inferred.llm_confidence, llm_reason=inferred.llm_reason,
        corroborated=False, verification_model_status="unsupported_or_partial",
    )
    change = ChangeSet(base="main", head="abc123", files=[], symbols=[])
    report = render_verify_change_terminal(
        ChangeVerificationResult(change=change, accepted_impacts=[impact])
    )
    assert "GET /cases" in report
    assert "Inferred impact" in report
    assert "INFERRED · 0.72" in report


# --- Case E: provider failure remains distinct from "no impact" -------------

def test_case_e_provider_failure_is_not_confused_with_a_successful_no_impact_conclusion() -> None:
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        GuideError("OpenAI request failed for model 'gpt-5.5': boom"),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf", file="app/svc.py"), f, repo=REPO)

    assert result.affected == []
    assert result.metrics["guide_errors"] == 1
    assert result.metrics["llm_no_candidate_turns"] == 0  # distinct counters: error, not a clean "no impact"
    assert any("guide_error=" in detail for detail in result.metrics["guide_error_details"])
