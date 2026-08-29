"""An inferred impact's grounding anchor and its reviewer-facing behavior
label are two different things, and both must survive to the report.

Root cause this pins: `_record_inferred` used to build an `AffectedEntrypoint`
from the candidate's `entrypoint_symbol` alone and drop
`ImpactCandidate.entrypoint_label` on the floor — there was no field to keep
it in. Reports then showed bare implementation identifiers
(`createAllocationsForOrderLines`) where the model had already written a
perfectly good behavior description ("Inventory allocation and stock
modification during order processing"). Confirmed in three real runs
(Vendure, NetBox, Gitea) before this fix.

The invariant under test throughout: the anchor symbol still anchors
(identity, dedup key, evidence) and is never weakened; the behavior label is
carried alongside it and is what a reviewer is shown.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.interpreter import ImpactInterpreter
from sydes.impact.models import (
    ACTION_INFER_IMPACT,
    ACTION_STOP_UNRESOLVED,
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    GUIDE_AUTO,
    AffectedEntrypoint,
    ImpactCandidate,
    ImpactResult,
    InvestigationDecision,
)
from sydes.verify.analyzer import _build_accepted_impacts

REPO = "app"


class ScriptedGuide:
    def __init__(self, decisions: list) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    def investigate(self, question):
        self.calls += 1
        if not self._decisions:
            raise AssertionError("guide asked for more turns than scripted")
        return self._decisions.pop(0)


def call_edge(caller: str, callee: str) -> dict:
    return {
        "repo": REPO, "caller_file": "app/svc.py", "caller_symbol": caller,
        "caller_qualified_name": f"app.{caller}", "caller_line": 1,
        "callee_file": "app/svc.py", "callee_symbol": callee,
        "callee_qualified_name": f"app.{callee}", "callee_line": 2,
    }


def facts(**kwargs) -> StructuralFacts:
    return StructuralFacts(
        call_edges=kwargs.get("call_edges", []),
        usage_edges=kwargs.get("usage_edges", []),
        entrypoints=kwargs.get("entrypoints", []),
        symbol_index=kwargs.get("symbol_index", {"repos": []}),
        provides_call_graph=True, backend="cbm",
    )


def changed(name: str, *, file: str = "app/svc.py") -> list[dict]:
    return [{"name": name, "file": file, "repo": REPO}]


# --- 1/2. The exact Vendure shape: label preserved, anchor still anchors ----

def test_inferred_candidate_preserves_behavior_label_and_keeps_symbol_anchor() -> None:
    """The precise real-world shape that motivated this fix."""
    f = facts(call_edges=[call_edge("orphan_caller", "createAllocationsForOrderLines")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="Inventory allocation during order processing",
                    entrypoint_symbol="createAllocationsForOrderLines",
                    confidence=0.85,
                    reason="allocation and stock-level writes changed together",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(
        changed("createAllocationsForOrderLines"), f, repo=REPO,
    )

    assert len(result.affected) == 1
    entry = result.affected[0]
    # The grounding anchor is untouched — still the symbol, still identity.
    assert entry.symbol == "createAllocationsForOrderLines"
    # ... and the behavior description survives alongside it.
    assert entry.behavior_label == "Inventory allocation during order processing"

    accepted = _build_accepted_impacts(result, [])
    assert len(accepted) == 1
    impact = accepted[0]
    # Reviewer-facing display is the behavior, not the identifier.
    assert impact.label == "Inventory allocation during order processing"
    assert impact.behavior_label == "Inventory allocation during order processing"
    # The anchor remains recoverable from the canonical id.
    assert impact.id.endswith("createAllocationsForOrderLines")
    # Causal reason and grounding status are entirely unaffected.
    assert impact.llm_reason == "allocation and stock-level writes changed together"
    assert impact.status == IMPACT_STATUS_INFERRED


def test_grounding_counters_are_unchanged_by_label_preservation() -> None:
    """Test 10: this is a representation change only — acceptance and every
    grounding counter must read exactly as they did before it."""
    f = facts(call_edges=[call_edge("orphan_caller", "leaf")])
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="a described downstream behavior",
                    entrypoint_symbol="leaf", confidence=0.6, reason="because",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert result.metrics["llm_candidates"] == 1
    assert result.metrics["llm_candidates_accepted"] == 1
    assert result.metrics["llm_candidates_ungrounded"] == 0
    assert result.metrics["llm_candidates_whole_change_unanchored"] == 0
    assert result.metrics["llm_candidates_self_referential"] == 0


# --- 3/4. Merge semantics -------------------------------------------------

def test_merge_preserves_an_existing_label_against_a_later_empty_one() -> None:
    """First non-empty wins — the same conservative merge `llm_reason`
    already uses. A later candidate on the same key must not blank it."""
    found: dict[str, AffectedEntrypoint] = {}
    corroboration = {
        "corroborated": False, "detail": "", "route_method": None, "route_path": None,
        "symbol": "handler", "qualified_name": "", "file": "", "repo": REPO,
        "kind": ENTRYPOINT_DECORATED, "ambiguous": False,
    }
    ImpactInterpreter._record_inferred(
        found,
        ImpactCandidate(
            entrypoint_label="the useful behavior description",
            entrypoint_symbol="handler", confidence=0.7, reason="first",
        ),
        corroboration, "changed_a", REPO,
    )
    ImpactInterpreter._record_inferred(
        found,
        ImpactCandidate(
            entrypoint_label="", entrypoint_symbol="handler",
            confidence=0.5, reason="second",
        ),
        corroboration, "changed_b", REPO,
    )

    assert len(found) == 1
    assert found["handler"].behavior_label == "the useful behavior description"


def test_merge_lets_a_later_label_fill_an_initially_empty_one() -> None:
    """The complementary direction: an entry that started with no label may
    still gain one from a later candidate on the same key."""
    found: dict[str, AffectedEntrypoint] = {}
    corroboration = {
        "corroborated": False, "detail": "", "route_method": None, "route_path": None,
        "symbol": "handler", "qualified_name": "", "file": "", "repo": REPO,
        "kind": ENTRYPOINT_DECORATED, "ambiguous": False,
    }
    ImpactInterpreter._record_inferred(
        found,
        ImpactCandidate(entrypoint_label="", entrypoint_symbol="handler",
                        confidence=0.5, reason="first"),
        corroboration, "changed_a", REPO,
    )
    assert found["handler"].behavior_label == ""

    ImpactInterpreter._record_inferred(
        found,
        ImpactCandidate(entrypoint_label="a real behavior description",
                        entrypoint_symbol="handler", confidence=0.7, reason="second"),
        corroboration, "changed_b", REPO,
    )
    assert found["handler"].behavior_label == "a real behavior description"


# --- 5. Fallback when no behavior label exists ----------------------------

def test_inferred_impact_without_a_behavior_label_falls_back_to_the_symbol() -> None:
    """Exactly today's behavior is retained when there is no label to show —
    the fix adds a preference, it does not remove the fallback chain."""
    entry = AffectedEntrypoint(
        repo=REPO, symbol="nightly_digest", qualified_name="app.jobs.nightly_digest",
        file="jobs.py", kind=ENTRYPOINT_DECORATED, status=IMPACT_STATUS_INFERRED,
        llm_confidence=0.4, llm_reason="reads a cache the changed helper populates",
        changed_symbols=["helper"],
    )
    accepted = _build_accepted_impacts(ImpactResult(affected=[entry]), [])
    assert len(accepted) == 1
    assert accepted[0].label == "nightly_digest"
    assert accepted[0].behavior_label == ""


# --- 6/7. Deterministic impacts are untouched -----------------------------

def test_proven_route_impact_label_is_unchanged() -> None:
    """`GET /owners` must remain exactly `GET /owners` — a real route is
    still the most precise thing a reviewer can be shown, and a PROVEN
    impact never carries a model-authored behavior label at all."""
    entry = AffectedEntrypoint(
        repo=REPO, symbol="show_owner", qualified_name="app.owners.show_owner",
        file="owners.py", kind=ENTRYPOINT_HTTP, status=IMPACT_STATUS_PROVEN,
        route_method="GET", route_path="/owners", changed_symbols=["helper"],
    )
    accepted = _build_accepted_impacts(ImpactResult(affected=[entry]), [])
    assert accepted[0].label == "GET /owners"
    assert accepted[0].behavior_label == ""
    assert accepted[0].status == IMPACT_STATUS_PROVEN


def test_proven_non_route_impact_label_is_unchanged() -> None:
    """A deterministic non-HTTP impact keeps falling back to its symbol."""
    entry = AffectedEntrypoint(
        repo=REPO, symbol="scheduled_sweep", qualified_name="app.jobs.scheduled_sweep",
        file="jobs.py", kind=ENTRYPOINT_DECORATED, status=IMPACT_STATUS_PROVEN,
        changed_symbols=["helper"],
    )
    accepted = _build_accepted_impacts(ImpactResult(affected=[entry]), [])
    assert accepted[0].label == "scheduled_sweep"
    assert accepted[0].behavior_label == ""


def test_a_behavior_label_never_overrides_a_real_route() -> None:
    """Ordering guard: route wins over behavior_label, so an inferred
    candidate corroborated onto a real route still shows the route."""
    entry = AffectedEntrypoint(
        repo=REPO, symbol="list_cases", qualified_name="app.views.list_cases",
        file="views.py", kind=ENTRYPOINT_HTTP, status=IMPACT_STATUS_INFERRED,
        route_method="GET", route_path="/cases",
        behavior_label="case listing results may now differ",
        llm_confidence=0.7, llm_reason="shares a filter helper",
    )
    accepted = _build_accepted_impacts(ImpactResult(affected=[entry]), [])
    assert accepted[0].label == "GET /cases"
    # ... but the semantic label is still preserved for any consumer that wants it.
    assert accepted[0].behavior_label == "case listing results may now differ"


# --- 8. Serialization ------------------------------------------------------

def test_behavior_label_is_serialized_on_both_models() -> None:
    entry = AffectedEntrypoint(
        repo=REPO, symbol="anchor", qualified_name="", file="", status=IMPACT_STATUS_INFERRED,
        behavior_label="a described behavior", llm_reason="why",
    )
    assert entry.to_dict()["behavior_label"] == "a described behavior"

    accepted = _build_accepted_impacts(ImpactResult(affected=[entry]), [])
    payload = accepted[0].model_dump()
    assert payload["behavior_label"] == "a described behavior"
    assert payload["label"] == "a described behavior"


# --- Existing-artifact shapes: NetBox and Gitea would be preserved too -----
#
# Replays the exact candidate shapes captured in those runs'
# `trace/impact_decisions.jsonl` (no new LLM call, no eval-code change) to
# confirm this is one general mechanism, not a Vendure-specific fix.

def test_real_captured_candidate_shapes_preserve_their_labels() -> None:
    captured = [
        # (behavior label the model actually wrote, anchor symbol it chose)
        ("browser cache behavior for DataFile content views", "DataFileView"),      # NetBox
        ("web-based manual merge operation for pull requests", "MergePullRequest"),  # Gitea
        ("Order workflows involving stock checks and allocations", "forAllocation"),  # Vendure
    ]
    for behavior_label, anchor_symbol in captured:
        found: dict[str, AffectedEntrypoint] = {}
        ImpactInterpreter._record_inferred(
            found,
            ImpactCandidate(
                entrypoint_label=behavior_label, entrypoint_symbol=anchor_symbol,
                confidence=0.8, reason="captured from a real run",
            ),
            {
                "corroborated": False, "detail": "", "route_method": None,
                "route_path": None, "symbol": anchor_symbol, "qualified_name": "",
                "file": "", "repo": REPO, "kind": ENTRYPOINT_DECORATED, "ambiguous": False,
            },
            "(whole change)", REPO,
        )
        entry = found[anchor_symbol]
        assert entry.symbol == anchor_symbol          # anchor intact
        assert entry.behavior_label == behavior_label  # label preserved

        accepted = _build_accepted_impacts(ImpactResult(affected=[entry]), [])
        assert accepted[0].label == behavior_label
