"""M4.1: AI moves from fallback recovery into the primary semantic loop.

Root cause this fixes: under `--impact-guide auto`, a changed symbol with a
*resolved* identity, no dead ends, and no truncated traversal was treated as
"nothing to investigate" and skipped entirely — appropriate when the guide
was a graph-navigation controller with nothing to navigate toward, wrong now
that its primary action is semantic inference, which needs the changed
symbol's own behavior, not a structural dead end. A decorator-only change
(no new call/usage edge at all) is exactly this shape, and exactly the case
semantic inference exists for.

Also covers: whole-change context now reaches every guide turn (other
changed symbols in the same PR, impacts already accepted this run), and the
self-reference guard now checks only the candidate's *label* — not
`entrypoint_symbol`, which the prompt itself invites the guide to set to the
changed symbol's own name when that is the nearest known anchor.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.guide import build_guide_prompt
from sydes.impact.interpreter import ImpactInterpreter
from sydes.impact.models import (
    ACTION_INFER_IMPACT,
    ACTION_STOP_UNRESOLVED,
    GUIDE_AUTO,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    ImpactCandidate,
    ImpactQuestion,
    InvestigationDecision,
)

REPO = "app"


class ScriptedGuide:
    """Returns one scripted `InvestigationDecision` per call, in order, and
    records every `ImpactQuestion` it was actually asked."""

    def __init__(self, decisions: list) -> None:
        self._decisions = list(decisions)
        self.calls = 0
        self.questions: list[ImpactQuestion] = []

    def investigate(self, question):
        self.calls += 1
        self.questions.append(question)
        if not self._decisions:
            raise AssertionError("guide asked for more turns than scripted")
        next_item = self._decisions.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


def call_edge(caller: str, callee: str) -> dict:
    return {
        "repo": REPO, "caller_file": "app/svc.py", "caller_symbol": caller,
        "caller_qualified_name": f"app.{caller}", "caller_line": 1,
        "callee_file": "app/svc.py", "callee_symbol": callee,
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


def resolved_changed(name: str, *, file: str = "app/svc.py", line: int = 12) -> dict:
    """A changed symbol with a resolved identity (qualified line known) but
    genuinely no structural lead: no call edge, no usage edge, nothing to
    traverse — e.g. a decorator added to a view with no new dependency."""
    return {"name": name, "file": file, "repo": REPO, "line": line}


def _infer(entrypoint_label: str, *, entrypoint_symbol: str = "", confidence: float = 0.6,
           reason: str = "plausible downstream effect") -> InvestigationDecision:
    return InvestigationDecision(
        action=ACTION_INFER_IMPACT,
        candidates=(
            ImpactCandidate(
                entrypoint_label=entrypoint_label, entrypoint_symbol=entrypoint_symbol,
                confidence=confidence, reason=reason,
            ),
        ),
    )


# --- 1/2. Semantic pass invoked regardless of whether structural impacts
# already exist -------------------------------------------------------------

def test_auto_guide_now_fires_for_a_resolved_symbol_with_no_structural_lead() -> None:
    """The exact root cause: a resolved identity, empty dead ends, no
    truncation used to mean 'nothing to investigate' under AUTO. It no
    longer does — semantic inference needs the symbol's own behavior, not a
    graph lead."""
    f = facts()  # no call/usage edges at all: genuinely no structural lead
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the per-symbol turn
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    interpreter.interpret([resolved_changed("apply_never_cache")], f, repo=REPO)

    assert guide.calls >= 2, "AUTO must no longer skip a resolved, lead-less symbol"


def test_semantic_pass_fires_when_structural_impacts_already_exist_elsewhere() -> None:
    """Structural evidence existing for OTHER changed symbols in the same
    PR must not suppress a semantic turn for one that has none."""
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler")],
    )
    changed_symbols = [
        {"name": "helper", "file": "app/svc.py", "repo": REPO},  # resolves deterministically
        resolved_changed("apply_never_cache", file="app/other.py"),  # no lead at all
    ]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the per-symbol turn
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    assert any(item.status == IMPACT_STATUS_PROVEN for item in result.affected)
    assert guide.calls >= 2


# --- 3/4/9. Structural evidence remains available; PROVEN vs INFERRED stay
# distinct; deterministic behavior is unaffected ----------------------------

def test_structural_endpoints_remain_available_as_evidence_not_erased() -> None:
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler")],
    )
    guide = ScriptedGuide([])
    interpreter = ImpactInterpreter(guide=guide, guide_policy="off")
    result = interpreter.interpret([{"name": "helper", "file": "app/svc.py", "repo": REPO}], f, repo=REPO)

    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_PROVEN
    assert guide.calls == 0  # policy=off is still a complete no-op


def test_proven_and_inferred_can_coexist_for_the_same_change() -> None:
    """A large deterministic result set must not crowd out a distinct
    semantic finding for a different, structurally-lead-less symbol."""
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler"), entrypoint("other_handler", path="/y")],
    )
    changed_symbols = [
        {"name": "helper", "file": "app/svc.py", "repo": REPO},
        resolved_changed("apply_never_cache", file="app/other.py"),
    ]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        _infer("GET /y", entrypoint_symbol="other_handler"),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    statuses = {item.status for item in result.affected}
    assert IMPACT_STATUS_PROVEN in statuses
    assert IMPACT_STATUS_INFERRED in statuses


# --- 5. Whole-change context reaches the guide ------------------------------

def test_whole_change_context_is_supplied_to_every_guide_turn() -> None:
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler")],
    )
    changed_symbols = [
        {"name": "helper", "file": "app/svc.py", "repo": REPO},
        resolved_changed("apply_never_cache", file="app/other.py"),
    ]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the per-symbol turn
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    interpreter.interpret(changed_symbols, f, repo=REPO)

    assert guide.calls >= 2
    assert guide.questions[0].is_whole_change is True
    question = guide.questions[1]
    assert question.changed_symbol == "apply_never_cache"
    # The OTHER changed symbol in this same PR is visible ...
    assert "helper" in question.other_changed_symbols
    # ... and so is the deterministic impact already accepted for it.
    assert any("handler" in item or "/x" in item for item in question.accepted_impacts_so_far)

    prompt = build_guide_prompt(question)
    assert "helper" in prompt
    assert "other_symbols_changed_by_this_same_pr" in prompt
    assert "impacts_already_accepted_this_run" in prompt


# --- 6/7. Self-reference: domain-concept elaboration accepted, trivial
# restatement still rejected -------------------------------------------------

def test_candidate_naming_the_changed_symbol_as_entrypoint_symbol_is_not_rejected() -> None:
    """The exact case Task 12 describes: changed symbol `set_expires`, a
    candidate whose *label* is a genuine downstream behavior description but
    whose `entrypoint_symbol` is the changed symbol itself (its own nearest
    known anchor) must survive."""
    f = facts()
    guide = ScriptedGuide([
        _infer(
            "order expiry calculation now varies by sales channel",
            entrypoint_symbol="set_expires",
            reason="the changed method's channel parameter feeds the expiry calculation",
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret([resolved_changed("set_expires")], f, repo=REPO)

    assert result.metrics["llm_candidates_self_referential"] == 0
    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED


def test_trivial_self_restatement_is_still_rejected() -> None:
    f = facts()
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # the whole-change turn, harmless
        _infer("set_expires", entrypoint_symbol="set_expires"),  # label is just the symbol name
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret([resolved_changed("set_expires")], f, repo=REPO)

    assert result.affected == []
    assert result.metrics["llm_candidates_self_referential"] == 1


# --- 10. Bounded AI-call behavior --------------------------------------------

def test_guide_calls_remain_bounded_even_with_several_lead_less_symbols() -> None:
    """Removing the AUTO skip must not turn this into an unbounded agent —
    the existing per-symbol/total budgets are still the only bound needed."""
    f = facts()
    changed_symbols = [resolved_changed(f"view_{i}", file=f"app/view_{i}.py") for i in range(6)]
    guide = ScriptedGuide([InvestigationDecision(action=ACTION_STOP_UNRESOLVED) for _ in range(7)])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    interpreter.interpret(changed_symbols, f, repo=REPO)

    assert guide.calls <= 8  # the existing default GuideBudget.max_turns_total


# --- M4.2: whole-change pass precedes per-symbol turns, and informs their
# order without ever silently dropping a candidate ---------------------------

def test_whole_change_pass_runs_before_any_per_symbol_turn() -> None:
    """The very first guide turn must be the whole-change synthesis, not a
    per-symbol question — this is the primary-loop reordering itself."""
    f = facts()
    changed_symbols = [resolved_changed("view_a"), resolved_changed("view_b", file="app/view_b.py")]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    interpreter.interpret(changed_symbols, f, repo=REPO)

    assert guide.questions[0].is_whole_change is True
    assert guide.questions[1].is_whole_change is False
    assert guide.questions[2].is_whole_change is False


def test_follow_up_symbols_reprioritize_investigation_order_not_iteration_order() -> None:
    """Target selection must be informed by the whole-change interpretation,
    not simple changed-symbol iteration order: when the whole-change turn
    names `view_b` as the priority follow-up, it must be investigated before
    `view_a` even though `view_a` appears first among the changed symbols."""
    f = facts()
    changed_symbols = [resolved_changed("view_a"), resolved_changed("view_b", file="app/view_b.py")]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_INFER_IMPACT, candidates=(), follow_up_symbols=("view_b",)),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # per-symbol turn for view_b
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # per-symbol turn for view_a
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    interpreter.interpret(changed_symbols, f, repo=REPO)

    assert guide.questions[1].changed_symbol == "view_b"
    assert guide.questions[2].changed_symbol == "view_a"


def test_unlisted_symbols_keep_their_original_order_after_reprioritization() -> None:
    """A follow-up hint naming only some symbols must not drop or silently
    reorder the rest beyond the named ones moving to the front."""
    f = facts()
    changed_symbols = [
        resolved_changed("view_a"), resolved_changed("view_b", file="app/view_b.py"),
        resolved_changed("view_c", file="app/view_c.py"),
    ]
    guide = ScriptedGuide([
        InvestigationDecision(action=ACTION_INFER_IMPACT, candidates=(), follow_up_symbols=("view_c",)),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # view_c, prioritized
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # view_a, original order preserved
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),  # view_b, original order preserved
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    interpreter.interpret(changed_symbols, f, repo=REPO)

    assert [q.changed_symbol for q in guide.questions[1:]] == ["view_c", "view_a", "view_b"]


def test_whole_change_turn_can_yield_a_reviewer_grade_pr_wide_semantic_finding() -> None:
    """The whole-change turn's own candidates must be accepted the same way
    a per-symbol candidate is — surfacing a PR-wide behavior description
    (not a bare symbol/config name) as an INFERRED impact."""
    f = facts()
    changed_symbols = [
        resolved_changed("device_render_config_view"),
        resolved_changed("device_config_tab_view", file="app/other.py"),
    ]
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="Several data/configuration HTTP views are now explicitly non-cacheable",
                    confidence=0.7,
                    reason="both changed views now apply a never-cache decorator to their responses",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed_symbols, f, repo=REPO)

    assert len(result.affected) == 1
    inferred = result.affected[0]
    assert inferred.status == IMPACT_STATUS_INFERRED
    assert inferred.label == "Several data/configuration HTTP views are now explicitly non-cacheable"
