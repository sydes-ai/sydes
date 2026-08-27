"""Increment C: ranked, typed boundary discovery.

`ImpactInterpreter.interpret()` now also runs `discover_boundaries` over the
same facts/index it already built, producing `ImpactResult.boundaries` — a
small set of typed (api/callable/async) software boundaries reachable from
the changed symbols, found by walking real structural edges only. These
tests pin the taxonomy, the ranking, the stopping/budget rules, and the
hardest soundness requirement: a semantic hint or a weak signature/type-only
reference can never manufacture a boundary that no real edge supports.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact import ImpactInterpreter
from sydes.impact.boundary_discovery import BoundaryBudget
from sydes.impact.models import (
    BOUNDARY_API,
    BOUNDARY_ASYNC,
    BOUNDARY_CALLABLE,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
)
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.models import AffectedBoundary, ChangeSet, ChangeVerificationResult

REPO = "app"


def call_edge(caller: str, callee: str, *, caller_file: str = "app/svc.py",
              callee_file: str = "app/svc.py") -> dict:
    return {
        "repo": REPO,
        "caller_file": caller_file, "caller_symbol": caller,
        "caller_qualified_name": f"app.{caller}", "caller_line": 1,
        "callee_file": callee_file, "callee_symbol": callee,
        "callee_qualified_name": f"app.{callee}", "callee_line": 2,
    }


def usage_edge(user: str, used: str, *, user_file: str = "app/svc.py",
               used_file: str = "app/svc.py") -> dict:
    return {
        "repo": REPO,
        "user_file": user_file, "user_symbol": user, "user_qualified_name": f"app.{user}",
        "used_file": used_file, "used_symbol": used, "used_qualified_name": f"app.{used}",
    }


def entrypoint(symbol: str, *, method: str | None = None, path: str | None = None,
               decorators: str = "", file: str = "app/svc.py") -> dict:
    return {
        "repo": REPO, "qualified_name": f"app.{symbol}", "symbol": symbol,
        "file": file, "line": 10, "route_method": method, "route_path": path,
        "decorators": decorators, "signature": "",
    }


def symbol_file(path: str, symbols: list[dict]) -> dict:
    return {"path": path, "symbols": symbols}


def sym(name: str, *, exported: bool = False) -> dict:
    return {"name": name, "kind": "function", "exported": exported, "start_line": 1, "end_line": 2}


def facts(**kwargs) -> StructuralFacts:
    symbol_index = kwargs.get("symbol_index")
    if symbol_index is None:
        symbol_index = {"repos": [{"repo": REPO, "files": kwargs.get("files", [])}]}
    return StructuralFacts(
        call_edges=kwargs.get("call_edges", []),
        usage_edges=kwargs.get("usage_edges", []),
        entrypoints=kwargs.get("entrypoints", []),
        symbol_index=symbol_index,
        provides_call_graph=True,
        backend="cbm",
    )


def changed(*names: str, file: str = "app/svc.py") -> list[dict]:
    return [{"name": name, "file": file, "repo": REPO} for name in names]


def interpret(changed_symbols, structural_facts, **kwargs):
    return ImpactInterpreter().interpret(changed_symbols, structural_facts, repo=REPO, **kwargs)


# --------------------------------------------------------------------------
# 1. API boundary
# --------------------------------------------------------------------------

def test_changed_service_reaching_an_http_handler_is_an_api_boundary() -> None:
    # Reachability walks *backward* from the changed symbol toward its
    # callers, so the fact needed is "http_handler calls service_method".
    f = facts(
        call_edges=[call_edge("http_handler", "service_method")],
        entrypoints=[entrypoint("http_handler", method="GET", path="/x")],
    )
    result = interpret(changed("service_method"), f)

    assert len(result.boundaries) == 1
    boundary = result.boundaries[0]
    assert boundary.kind == BOUNDARY_API
    assert boundary.subtype == "http"
    assert boundary.status == IMPACT_STATUS_PROVEN
    assert boundary.path is not None and boundary.path.length >= 1


# --------------------------------------------------------------------------
# 2. Callable boundary — no HTTP route required
# --------------------------------------------------------------------------

def test_changed_helper_reaching_an_exported_callable_is_a_callable_boundary() -> None:
    # Backward walk: service_method calls the changed helper, and
    # public_export (an exported symbol in a different file) calls
    # service_method in turn.
    f = facts(
        call_edges=[
            call_edge("service_method", "helper", callee_file="app/helper.py"),
            call_edge("public_export", "service_method", caller_file="app/public.py"),
        ],
        files=[symbol_file("app/public.py", [sym("public_export", exported=True)])],
    )
    result = interpret(changed("helper", file="app/helper.py"), f)

    kinds = {item.kind for item in result.boundaries}
    assert BOUNDARY_CALLABLE in kinds
    callable_boundary = next(item for item in result.boundaries if item.kind == BOUNDARY_CALLABLE)
    assert callable_boundary.symbol == "public_export"
    assert callable_boundary.status == IMPACT_STATUS_PROVEN
    # No HTTP route existed anywhere in these facts, yet it is still visible.
    assert not any(item.kind == BOUNDARY_API for item in result.boundaries)
    # The exported symbol lives in a different file than its immediate
    # caller (service_method, in app/svc.py) — a real cross-package/module
    # boundary, not just an internal same-file helper.
    assert callable_boundary.subtype == "public_library"


def test_exported_symbol_reached_from_its_own_file_is_an_internal_service() -> None:
    f = facts(
        call_edges=[
            call_edge("service_method", "helper", caller_file="app/svc.py", callee_file="app/helper.py"),
            call_edge("internal_export", "service_method", caller_file="app/svc.py"),
        ],
        files=[symbol_file("app/svc.py", [sym("internal_export", exported=True)])],
    )
    result = interpret(changed("helper", file="app/helper.py"), f)

    callable_boundary = next(item for item in result.boundaries if item.kind == BOUNDARY_CALLABLE)
    assert callable_boundary.subtype == "internal_service"


# --------------------------------------------------------------------------
# 3. Async boundary — must remain visible, never dropped for being non-HTTP
# --------------------------------------------------------------------------

def test_changed_helper_reaching_a_scheduled_job_is_an_async_boundary() -> None:
    f = facts(
        call_edges=[call_edge("cleanup_expired_orders", "helper")],
        entrypoints=[entrypoint("cleanup_expired_orders", decorators="@celery.task(schedule=cron)")],
    )
    result = interpret(changed("helper"), f)

    assert len(result.boundaries) == 1
    boundary = result.boundaries[0]
    assert boundary.kind == BOUNDARY_ASYNC
    assert boundary.subtype == "scheduled_job"
    assert boundary.status == IMPACT_STATUS_PROVEN


def test_event_handler_decorator_is_recognized_as_a_distinct_async_subtype() -> None:
    f = facts(
        call_edges=[call_edge("on_order_created", "helper")],
        entrypoints=[entrypoint("on_order_created", decorators="@signal_receiver(OrderCreated)")],
    )
    result = interpret(changed("helper"), f)

    assert result.boundaries[0].kind == BOUNDARY_ASYNC
    assert result.boundaries[0].subtype == "event_handler"


# --------------------------------------------------------------------------
# 4. Ranking — semantic hints prioritize among real candidates, never invent
# --------------------------------------------------------------------------

def test_semantically_relevant_candidate_is_emitted_before_an_unrelated_one() -> None:
    """Two equally-real candidates; only one budget slot. The semantically
    matching one must win — but both edges are real either way."""
    f = facts(
        call_edges=[
            call_edge("cleanup_expired_orders", "helper"),
            call_edge("admin_serializer", "helper"),
        ],
        entrypoints=[
            entrypoint("cleanup_expired_orders", decorators="@celery.task"),
            entrypoint("admin_serializer", decorators="@celery.task"),
        ],
    )
    budget = BoundaryBudget(max_boundaries=1)
    result = interpret(
        changed("helper"), f,
        semantic_texts=["order expiration cleanup_expired_orders"],
        boundary_budget=budget,
    )

    assert len(result.boundaries) == 1
    assert result.boundaries[0].symbol == "cleanup_expired_orders"


def test_semantic_hint_never_fabricates_an_edge_to_an_unrelated_symbol() -> None:
    """The semantic hint names a symbol with NO real edge from the changed
    symbol at all — it must never appear as a boundary."""
    f = facts(
        call_edges=[call_edge("cleanup_expired_orders", "helper")],
        entrypoints=[
            entrypoint("cleanup_expired_orders", decorators="@celery.task"),
            entrypoint("totally_unrelated_admin_tool", decorators="@celery.task"),
        ],
    )
    result = interpret(
        changed("helper"), f, semantic_texts=["totally_unrelated_admin_tool"],
    )

    labels = {item.symbol for item in result.boundaries}
    assert "totally_unrelated_admin_tool" not in labels
    assert "cleanup_expired_orders" in labels


# --------------------------------------------------------------------------
# 5. No semantic analysis — deterministic behavior unaffected
# --------------------------------------------------------------------------

def test_boundary_discovery_works_with_no_semantic_hints_at_all() -> None:
    f = facts(
        call_edges=[call_edge("http_handler", "helper")],
        entrypoints=[entrypoint("http_handler", method="POST", path="/y")],
    )
    result = interpret(changed("helper"), f, semantic_texts=None)

    assert len(result.boundaries) == 1
    assert result.boundaries[0].kind == BOUNDARY_API


# --------------------------------------------------------------------------
# 6. Weak-edge safety
# --------------------------------------------------------------------------

def test_signature_only_reference_never_becomes_a_boundary_or_proven_impact() -> None:
    """A changed *type* named only in a handler's signature — the weakest
    evidence the interpreter produces. It must not appear in `boundaries`
    (this traversal never walks signature references at all) and the
    existing entrypoint record it does produce must be INFERRED, not
    PROVEN — the soundness fix `_record(status=...)` enforces."""
    f = facts(
        entrypoints=[
            entrypoint("update_item", method="PUT", path="/items/{id}"),
        ],
    )
    f.entrypoints[0]["signature"] = "(item_in: ItemUpdate, db: Session)"
    result = interpret(changed("ItemUpdate"), f)

    assert result.boundaries == []
    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED


# --------------------------------------------------------------------------
# 7. Shared utility explosion — bounded traversal
# --------------------------------------------------------------------------

def test_many_callers_of_a_shared_helper_emit_only_top_ranked_boundaries() -> None:
    callers = [f"caller_{i}" for i in range(60)]
    f = facts(
        call_edges=[call_edge(caller, "shared_helper") for caller in callers]
        + [call_edge(f"handler_{i}", caller) for i, caller in enumerate(callers)],
        entrypoints=[
            entrypoint(f"handler_{i}", method="GET", path=f"/p{i}") for i in range(len(callers))
        ],
    )
    budget = BoundaryBudget(max_boundaries=5, max_expansions=40, max_candidates_per_symbol=60)
    result = interpret(changed("shared_helper"), f, boundary_budget=budget)

    assert len(result.boundaries) <= 5
    assert result.metrics["boundary_expansions"] <= 40


# --------------------------------------------------------------------------
# 8. HTTP regression — existing behavior continues to work
# --------------------------------------------------------------------------

def test_existing_http_entrypoint_behavior_is_unaffected_by_boundary_discovery() -> None:
    f = facts(
        call_edges=[call_edge("handler", "helper")],
        entrypoints=[entrypoint("handler", method="GET", path="/x")],
    )
    result = interpret(changed("helper"), f)

    assert len(result.affected) == 1
    assert result.affected[0].kind == "http_route"
    assert result.affected[0].status == IMPACT_STATUS_PROVEN
    assert result.affected[0].route_method == "GET"
    assert result.affected[0].route_path == "/x"
    # The boundary pass ALSO sees the same real route — both views coexist.
    assert any(item.kind == BOUNDARY_API for item in result.boundaries)


# --------------------------------------------------------------------------
# 9. Non-HTTP serialization / reporting
# --------------------------------------------------------------------------

def test_callable_and_async_boundaries_survive_serialization_and_reporting() -> None:
    change = ChangeSet(base="main", head="abc123", files=[], symbols=[])
    result = ChangeVerificationResult(
        change=change,
        affected_boundaries=[
            AffectedBoundary(
                id="boundary:callable:app:public_export:app/public.py",
                kind="callable", subtype="public_library", repo="app",
                file="app/public.py", symbol="public_export", label="app.public_export",
                changed_symbols=["helper"], evidence=["calls:helper -> usage:public_export"],
                distance=2, evidence_strength="medium",
            ),
            AffectedBoundary(
                id="boundary:async:app:cleanup_expired_orders:app/svc.py",
                kind="async", subtype="scheduled_job", repo="app",
                file="app/svc.py", symbol="cleanup_expired_orders",
                label="app.cleanup_expired_orders", changed_symbols=["helper"],
                evidence=["calls:helper -> calls:cleanup_expired_orders"],
                distance=1, evidence_strength="strong",
            ),
        ],
    )

    dumped = result.model_dump()
    assert len(dumped["affected_boundaries"]) == 2
    assert dumped["affected_boundaries"][0]["kind"] == "callable"
    assert dumped["affected_boundaries"][1]["kind"] == "async"

    report = render_verify_change_terminal(result)
    assert "callable · app.public_export" in report
    assert "async · app.cleanup_expired_orders" in report

    verbose = render_verify_change_terminal(result, verbose=True)
    assert "BOUNDARIES" in verbose
    assert "public_library" in verbose
    assert "scheduled_job" in verbose


# --------------------------------------------------------------------------
# 10. Soundness — semantic hint alone, no structural edge, no boundary
# --------------------------------------------------------------------------

def test_semantic_hint_with_no_structural_edges_at_all_produces_no_boundary() -> None:
    f = facts(entrypoints=[entrypoint("cleanup_expired_orders", decorators="@celery.task")])
    result = interpret(
        changed("helper"), f,
        semantic_texts=["order expiration", "cleanup_expired_orders", "sales channel"],
    )

    assert result.boundaries == []
