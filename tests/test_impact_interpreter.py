"""What the impact interpreter concludes, and what it refuses to conclude.

The interpreter turns changed symbols into reachable entrypoints. These tests
pin both halves of that: the paths it should find, and the boundaries it must
not cross — no ranking of ordinary callers as APIs, no guessing an entrypoint's
kind, no unbounded walk, and an explicit "unresolved" when nothing connects.

Facts are built by hand rather than by querying a live backend, so every case
states exactly which structural relationship is under test.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_TRUNCATED,
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    RELATION_CALLS,
    RELATION_DECORATOR_REFERENCE,
    RELATION_DIRECT,
    RELATION_USAGE,
    STRATEGY_CALL_REACHABILITY,
    STRATEGY_DECORATOR_REFERENCE,
    STRATEGY_DIRECT_ENTRYPOINT,
    STRATEGY_SIGNATURE_REFERENCE,
    STRATEGY_USAGE_REACHABILITY,
    ImpactInterpreter,
    TraversalBudget,
)

REPO = "app"


def call_edge(caller: str, callee: str, *, caller_file: str = "app/svc.py") -> dict:
    return {
        "repo": REPO,
        "caller_file": caller_file, "caller_symbol": caller,
        "caller_qualified_name": f"app.{caller}", "caller_line": 1,
        "callee_file": "app/svc.py", "callee_symbol": callee,
        "callee_qualified_name": f"app.{callee}", "callee_line": 2,
    }


def usage_edge(user: str, used: str, *, user_file: str = "app/perm.py") -> dict:
    return {
        "repo": REPO,
        "user_file": user_file, "user_symbol": user,
        "user_qualified_name": f"app.{user}",
        "used_file": "app/perm.py", "used_symbol": used,
        "used_qualified_name": f"app.{used}",
    }


def entrypoint(
    symbol: str, *, method: str | None = "GET", path: str | None = "/x",
    decorators: str = "", signature: str = "", file: str = "app/views.py",
) -> dict:
    # Qualified name deliberately matches call_edge()/usage_edge()'s own
    # convention (f"app.{name}") so a symbol acting as both a call-graph
    # participant and a registered entrypoint resolves to one identity.
    return {
        "repo": REPO, "qualified_name": f"app.{symbol}", "symbol": symbol,
        "file": file, "line": 10, "route_method": method, "route_path": path,
        "decorators": decorators, "signature": signature,
    }


def facts(**kwargs) -> StructuralFacts:
    return StructuralFacts(
        call_edges=kwargs.get("call_edges", []),
        usage_edges=kwargs.get("usage_edges", []),
        entrypoints=kwargs.get("entrypoints", []),
        provides_call_graph=True,
        backend="cbm",
    )


def changed(*names: str, file: str = "app/svc.py") -> list[dict]:
    # Real change attribution always reports the file a symbol actually lives
    # in (it comes from the diff itself), so tests pin it explicitly whenever
    # a fixture places the symbol somewhere other than the default module.
    return [{"name": name, "file": file, "repo": REPO} for name in names]


# --------------------------------------------------------------------------
# DIRECT_ENTRYPOINT
# --------------------------------------------------------------------------


def test_changed_handler_maps_to_its_own_entrypoint() -> None:
    """The baseline: the changed symbol carries route metadata itself."""
    result = ImpactInterpreter().interpret(
        [{"name": "list_items", "file": "app/views.py", "repo": REPO}],
        facts(entrypoints=[entrypoint("list_items", method="GET", path="/items")]),
    )

    assert len(result.affected) == 1
    affected = result.affected[0]
    assert affected.label == "GET /items"
    assert affected.kind == ENTRYPOINT_HTTP
    assert affected.strategies == [STRATEGY_DIRECT_ENTRYPOINT]
    assert affected.paths[0].relations == (RELATION_DIRECT,)


# --------------------------------------------------------------------------
# CALL_REACHABILITY
# --------------------------------------------------------------------------


def test_one_hop_call_reaches_the_single_entrypoint() -> None:
    result = ImpactInterpreter().interpret(
        changed("delete_record"),
        facts(
            call_edges=[call_edge("delete_item", "delete_record")],
            entrypoints=[entrypoint("delete_item", method="DELETE", path="/items/{id}")],
        ),
    )

    assert [item.label for item in result.affected] == ["DELETE /items/{id}"]
    assert result.affected[0].strategies == [STRATEGY_CALL_REACHABILITY]


def test_multi_hop_call_preserves_the_whole_path() -> None:
    """The intermediate hop must survive: it is how a reviewer checks the claim."""
    result = ImpactInterpreter().interpret(
        changed("low_level"),
        facts(
            call_edges=[call_edge("mid_layer", "low_level"),
                        call_edge("create_item", "mid_layer")],
            entrypoints=[entrypoint("create_item", method="POST", path="/items")],
        ),
    )

    path = result.affected[0].paths[0]
    assert [step.symbol for step in path.steps] == ["mid_layer", "create_item"]
    assert path.relations == (RELATION_CALLS, RELATION_CALLS)


def test_a_shared_symbol_reports_every_reachable_entrypoint() -> None:
    """Forcing one winner would hide real blast radius."""
    result = ImpactInterpreter().interpret(
        changed("shared_update"),
        facts(
            call_edges=[call_edge("update_item", "shared_update"),
                        call_edge("escalate_item", "shared_update")],
            entrypoints=[
                entrypoint("update_item", method="PUT", path="/items/{id}"),
                entrypoint("escalate_item", method="PUT", path="/items/{id}/escalate"),
            ],
        ),
    )

    assert {item.label for item in result.affected} == {
        "PUT /items/{id}", "PUT /items/{id}/escalate"
    }


def test_ordinary_callers_are_not_reported_as_affected_apis() -> None:
    """A background helper that calls the change is not an API surface."""
    result = ImpactInterpreter().interpret(
        changed("shared_update"),
        facts(
            call_edges=[call_edge("background_job", "shared_update"),
                        call_edge("update_item", "shared_update")],
            entrypoints=[entrypoint("update_item", method="PUT", path="/items/{id}")],
        ),
    )

    labels = [item.label for item in result.affected]
    assert labels == ["PUT /items/{id}"]
    assert "background_job" not in labels


# --------------------------------------------------------------------------
# USAGE_REACHABILITY and DECORATOR_REFERENCE
# --------------------------------------------------------------------------


def test_decorator_reference_connects_a_dependency_to_its_handler() -> None:
    """A symbol named in a decorator argument produces no call edge at all."""
    result = ImpactInterpreter().interpret(
        changed("AdminOnlyPermission"),
        facts(entrypoints=[
            entrypoint(
                "delete_item", method="DELETE", path="/items/{id}",
                decorators='@router.delete(\n "/items/{id}",\n'
                           ' dependencies=[Depends(Guard([AdminOnlyPermission]))],\n)',
            ),
            entrypoint("list_items", method="GET", path="/items",
                       decorators='@router.get("/items")'),
        ]),
    )

    assert [item.label for item in result.affected] == ["DELETE /items/{id}"]
    path = result.affected[0].paths[0]
    assert path.strategy == STRATEGY_DECORATOR_REFERENCE
    assert path.relations == (RELATION_DECORATOR_REFERENCE,)
    assert "AdminOnlyPermission" in path.steps[0].evidence


def test_usage_then_decorator_recovers_a_composed_dependency() -> None:
    """The composed case: the change is used by a symbol a decorator names.

    The new symbol never appears in any decorator itself, so decorator
    matching alone cannot see it; only usage-then-decorator reaches the route.
    """
    result = ImpactInterpreter().interpret(
        changed("ReporterCheck", file="app/perm.py"),
        facts(
            usage_edges=[usage_edge("EditPermission", "ReporterCheck")],
            # EditPermission is a dependency class, not an entrypoint of its
            # own; only the handler that cites it in a decorator is one.
            entrypoints=[
                entrypoint(
                    "update_item", method="PUT", path="/items/{id}",
                    decorators="@router.put(dependencies=[Depends(Guard([EditPermission]))])",
                ),
            ],
        ),
    )

    labels = {item.label for item in result.affected}
    assert "PUT /items/{id}" in labels, "the composed dependency should reach the route"


def test_usage_reachability_is_labelled_distinctly_from_calls() -> None:
    """A usage hop is weaker evidence than a call and must be visible as such."""
    result = ImpactInterpreter().interpret(
        changed("Helper", file="app/perm.py"),
        facts(
            usage_edges=[usage_edge("handler", "Helper")],
            entrypoints=[entrypoint("handler", method="GET", path="/h")],
        ),
    )

    path = result.affected[0].paths[0]
    assert path.strategy == STRATEGY_USAGE_REACHABILITY
    assert path.relations == (RELATION_USAGE,)


# --------------------------------------------------------------------------
# SIGNATURE_REFERENCE
# --------------------------------------------------------------------------


def test_signature_reference_links_a_changed_type_to_a_handler() -> None:
    result = ImpactInterpreter().interpret(
        changed("ItemUpdate"),
        facts(entrypoints=[
            entrypoint("update_item", method="PUT", path="/items/{id}",
                       signature="(item_in: ItemUpdate, db: Session)"),
            entrypoint("list_items", method="GET", path="/items", signature="(db: Session)"),
        ]),
    )

    assert [item.label for item in result.affected] == ["PUT /items/{id}"]
    assert result.affected[0].paths[0].strategy == STRATEGY_SIGNATURE_REFERENCE


def test_common_type_names_do_not_match_everything() -> None:
    """A reference that matches every handler distinguishes none."""
    result = ImpactInterpreter().interpret(
        changed("str"),
        facts(entrypoints=[
            entrypoint("a", signature="(x: str)"), entrypoint("b", signature="(y: str)")
        ]),
    )

    assert result.affected == []
    assert result.unresolved[0].reason == "no_entrypoint_reached"


# --------------------------------------------------------------------------
# Bounds, cycles, determinism
# --------------------------------------------------------------------------


def test_a_call_cycle_terminates() -> None:
    result = ImpactInterpreter().interpret(
        changed("a"),
        facts(
            call_edges=[call_edge("b", "a"), call_edge("a", "b"),
                        call_edge("handler", "b")],
            entrypoints=[entrypoint("handler", method="GET", path="/h")],
        ),
    )

    assert [item.label for item in result.affected] == ["GET /h"]


def test_exceeding_the_depth_bound_is_reported_as_truncated() -> None:
    """Stopping early must never look like 'nothing was reachable'."""
    edges = [call_edge(f"n{i}", f"n{i + 1}") for i in range(6)]
    edges.append(call_edge("handler", "n0"))
    result = ImpactInterpreter(TraversalBudget(max_depth=2)).interpret(
        [{"name": "n6", "file": "app/svc.py", "repo": REPO}],
        facts(call_edges=edges, entrypoints=[entrypoint("handler")]),
    )

    assert result.completeness == COMPLETENESS_TRUNCATED
    assert any("bound" in note for note in result.notes)


def test_a_complete_traversal_is_not_marked_truncated() -> None:
    result = ImpactInterpreter().interpret(
        changed("x"),
        facts(call_edges=[call_edge("handler", "x")],
              entrypoints=[entrypoint("handler")]),
    )

    assert result.completeness == COMPLETENESS_COMPLETE


def test_ordering_is_deterministic_regardless_of_input_order() -> None:
    """Two runs over the same facts must be comparable."""
    entrypoints = [
        entrypoint("zebra", method="GET", path="/z"),
        entrypoint("alpha", method="GET", path="/a"),
    ]
    edges = [call_edge("zebra", "core"), call_edge("alpha", "core")]

    first = ImpactInterpreter().interpret(
        changed("core"), facts(call_edges=edges, entrypoints=entrypoints))
    second = ImpactInterpreter().interpret(
        changed("core"), facts(call_edges=list(reversed(edges)),
                               entrypoints=list(reversed(entrypoints))))

    assert [item.label for item in first.affected] == [item.label for item in second.affected]


# --------------------------------------------------------------------------
# Unresolved
# --------------------------------------------------------------------------


def test_an_unreachable_change_is_unresolved_not_silently_empty() -> None:
    result = ImpactInterpreter().interpret(
        changed("orphan"),
        facts(entrypoints=[entrypoint("handler")]),
    )

    assert result.affected == []
    assert [item.symbol for item in result.unresolved] == ["orphan"]
    assert result.unresolved[0].reason == "no_entrypoint_reached"


def test_absent_entrypoint_facts_are_called_out() -> None:
    """No entrypoints supplied is a different problem from none reachable."""
    result = ImpactInterpreter().interpret(changed("anything"), facts())

    assert any("no entrypoint symbols" in note for note in result.notes)


def test_a_decorated_symbol_without_route_metadata_is_not_called_http() -> None:
    """Unknown kind is a real answer; guessing HTTP would be a false claim."""
    result = ImpactInterpreter().interpret(
        changed("run_job"),
        facts(entrypoints=[
            {"repo": REPO, "qualified_name": "app.jobs.run_job", "symbol": "run_job",
             "file": "app/jobs.py", "line": 3, "route_method": None, "route_path": None,
             "decorators": "@scheduler.every(60)", "signature": "()"},
        ]),
    )

    assert result.affected[0].kind == ENTRYPOINT_DECORATED
    assert result.affected[0].kind != ENTRYPOINT_HTTP


def test_multiple_reasons_for_one_entrypoint_are_all_kept() -> None:
    """Two different relationships reaching one handler are two reasons to look."""
    result = ImpactInterpreter().interpret(
        changed("Thing"),
        facts(
            call_edges=[call_edge("handler", "Thing")],
            entrypoints=[entrypoint("handler", method="GET", path="/h",
                                    signature="(item: Thing)")],
        ),
    )

    strategies = result.affected[0].strategies
    assert STRATEGY_CALL_REACHABILITY in strategies
    assert STRATEGY_SIGNATURE_REFERENCE in strategies
