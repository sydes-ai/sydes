"""Task item 6: corroboration must not treat an ambiguous bare symbol name
as structural corroboration.

`InvestigationExecutor._corroborate_one` previously took the *first* known
entrypoint whose bare `symbol` matched `ImpactCandidate.entrypoint_symbol`.
A short name like `update` or `handler` can legitimately name more than one
real entrypoint in a repository — picking the first made a hallucinated or
coincidentally-named candidate look exactly as corroborated as a real match.
The fix: an exact route method+path is tried first (the strongest identity
this function can actually see — `ImpactCandidate` never carries a
qualified name/repo/file); a bare symbol match is only accepted when it is
unique, and reported as ambiguous — never resolved by guessing — otherwise.

Corroboration remains a confidence/provenance signal only throughout: even
a clean unique match never upgrades `IMPACT_STATUS_INFERRED` to
`IMPACT_STATUS_PROVEN`.
"""

from __future__ import annotations

from sydes.impact.investigate import InvestigationExecutor
from sydes.impact.models import ImpactCandidate


class _FakeIndex:
    """Only `entrypoints` is read by `corroborate_candidates` — the rest of
    the `_GraphIndex` protocol is irrelevant to this test."""

    def __init__(self, entrypoints: list[dict]) -> None:
        self.entrypoints = entrypoints


def _executor(entrypoints: list[dict]) -> InvestigationExecutor:
    return InvestigationExecutor(index=_FakeIndex(entrypoints), facts=None, repo_root=None)


def _entrypoint(symbol: str, *, file: str, method: str | None = None, path: str | None = None) -> dict:
    return {
        "repo": "app", "qualified_name": f"app.{file.replace('/', '.').removesuffix('.py')}.{symbol}",
        "symbol": symbol, "file": file, "route_method": method, "route_path": path,
    }


def test_unique_bare_symbol_is_still_corroborated() -> None:
    """Regression: the common, unambiguous case must keep working exactly
    as before this fix."""
    entrypoints = [_entrypoint("update", file="app/orders.py", method="POST", path="/orders/{id}")]
    executor = _executor(entrypoints)
    candidate = ImpactCandidate(entrypoint_label="orders update", entrypoint_symbol="update", confidence=0.7)

    [result] = executor.corroborate_candidates((candidate,))

    assert result["corroborated"] is True
    assert result["route_method"] == "POST"
    assert result["route_path"] == "/orders/{id}"


def test_ambiguous_bare_symbol_is_not_corroborated_and_does_not_guess() -> None:
    """Two unrelated entrypoints share the bare symbol `update` — neither
    may be silently picked; the candidate must come back uncorroborated
    with an ambiguity explanation, not a false match on the first one found."""
    entrypoints = [
        _entrypoint("update", file="app/orders.py", method="POST", path="/orders/{id}"),
        _entrypoint("update", file="app/settings.py", method="PATCH", path="/settings"),
    ]
    executor = _executor(entrypoints)
    candidate = ImpactCandidate(
        entrypoint_label="something update-related", entrypoint_symbol="update", confidence=0.6,
    )

    [result] = executor.corroborate_candidates((candidate,))

    assert result["corroborated"] is False
    assert "ambiguous" in result["detail"]
    assert "2" in result["detail"]
    # Never silently attributed to either colliding entrypoint.
    assert result["qualified_name"] == ""
    assert result["file"] == ""


def test_hallucinated_symbol_gains_no_strength_from_an_unrelated_namesake() -> None:
    """A candidate whose `entrypoint_symbol` happens to collide with some
    unrelated real symbol elsewhere in the repo must not read as more
    trustworthy than an entrypoint_symbol that matches nothing at all —
    both are "not corroborated", not a graduated partial credit."""
    entrypoints = [
        _entrypoint("update", file="app/unrelated_module.py", method="DELETE", path="/unrelated"),
    ]
    executor = _executor(entrypoints)
    real_match = ImpactCandidate(entrypoint_label="probably unrelated", entrypoint_symbol="update", confidence=0.5)
    no_match = ImpactCandidate(entrypoint_label="probably unrelated", entrypoint_symbol="totally_invented", confidence=0.5)

    [with_namesake] = executor.corroborate_candidates((real_match,))
    [with_no_match] = executor.corroborate_candidates((no_match,))

    # A single, unique match on a bare name IS a legitimate (if weak)
    # corroboration signal — this asserts the *unrelated* case: once there
    # are two or more colliding entrypoints, ambiguity wins over guessing.
    assert with_no_match["corroborated"] is False
    assert with_no_match["detail"] == "no known entrypoint or route matches this candidate"


def test_route_match_is_preferred_over_an_ambiguous_bare_symbol() -> None:
    """The strongest available signal (an exact route) wins even when the
    same candidate's `entrypoint_symbol` happens to be ambiguous."""
    entrypoints = [
        _entrypoint("update", file="app/orders.py", method="POST", path="/orders/{id}"),
        _entrypoint("update", file="app/settings.py", method="PATCH", path="/settings"),
        _entrypoint("handle_cases", file="app/cases.py", method="GET", path="/cases"),
    ]
    executor = _executor(entrypoints)
    candidate = ImpactCandidate(
        entrypoint_label="GET /cases", entrypoint_symbol="update",  # symbol is ambiguous, route is not
        confidence=0.8,
    )

    [result] = executor.corroborate_candidates((candidate,))

    assert result["corroborated"] is True
    assert result["route_method"] == "GET"
    assert result["route_path"] == "/cases"
    assert result["symbol"] == "handle_cases"  # the route's real handler, never the ambiguous "update"


def test_ambiguous_corroboration_never_upgrades_inferred_to_proven_through_the_full_interpreter() -> None:
    """Full pipeline: an ambiguous bare-symbol candidate must still survive
    as INFERRED (M4's "never silently drop" guarantee) but strictly
    uncorroborated — ambiguity is a confidence/provenance signal only and
    can never manufacture PROVEN."""
    from sydes.code_intelligence.base import StructuralFacts
    from sydes.impact.interpreter import ImpactInterpreter
    from sydes.impact.models import (
        ACTION_INFER_IMPACT,
        ACTION_STOP_UNRESOLVED,
        GUIDE_AUTO,
        IMPACT_STATUS_INFERRED,
        InvestigationDecision,
    )

    def call_edge(caller: str, callee: str) -> dict:
        return {
            "repo": "app", "caller_file": "app/svc.py", "caller_symbol": caller,
            "caller_qualified_name": f"app.{caller}", "caller_line": 1,
            "callee_file": "app/svc.py", "callee_symbol": callee,
            "callee_qualified_name": f"app.{callee}", "callee_line": 2,
        }

    f = StructuralFacts(
        call_edges=[call_edge("orphan_caller", "leaf")],
        usage_edges=[],
        entrypoints=[
            _entrypoint("update", file="app/orders.py", method="POST", path="/orders/{id}"),
            _entrypoint("update", file="app/settings.py", method="PATCH", path="/settings"),
        ],
        symbol_index={"repos": []},
        provides_call_graph=True,
        backend="cbm",
    )

    class ScriptedGuide:
        def __init__(self, decisions: list) -> None:
            self._decisions = list(decisions)

        def investigate(self, question):
            return self._decisions.pop(0)

    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="something update-ish", entrypoint_symbol="update",
                    confidence=0.65, reason="plausible shared update path",
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO)
    result = interpreter.interpret([{"name": "leaf", "file": "app/svc.py", "repo": "app"}], f, repo="app")

    assert len(result.affected) == 1
    inferred = result.affected[0]
    assert inferred.status == IMPACT_STATUS_INFERRED
    assert inferred.corroborated is False  # ambiguous match must never read as corroborated
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is True  # still survives — never dropped
    assert log_entry["corroborated"] is False
    assert "ambiguous" in log_entry["corroboration_evidence"]
