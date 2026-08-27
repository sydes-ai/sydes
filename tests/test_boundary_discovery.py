"""Increment C / C.1: ranked, typed boundary discovery.

`ImpactInterpreter.interpret()` runs `discover_boundaries` over the same
facts/index it already built, producing `ImpactResult.boundaries` — a small
set of typed (api/callable/async) software boundaries reachable from the
changed symbols, found by walking real structural edges only.

C.1 corrects a specific defect real-PR evaluation exposed: boundary
ELIGIBILITY (is this node a meaningful architectural cut?) was too weak and
too easily satisfied by ranking-adjacent signals (reachable, exported,
decorated) — test functions and `main` were emitted as `callable` boundaries,
while genuine seed-level API/route-registration boundaries were missed.
These tests pin the corrected eligibility predicates, the seed-before-
expansion architecture, and that ranking can never substitute for
eligibility.
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
# A. Seed API boundary — no caller required first
# --------------------------------------------------------------------------

def test_a_changed_http_handler_is_emitted_directly_as_api() -> None:
    """The changed symbol IS the route handler — no caller, no expansion,
    eligibility is checked at the seed itself before anything else."""
    f = facts(entrypoints=[entrypoint("checkout_handler", method="POST", path="/checkout")])
    result = interpret(changed("checkout_handler"), f)

    assert len(result.boundaries) == 1
    boundary = result.boundaries[0]
    assert boundary.kind == BOUNDARY_API
    assert boundary.subtype == "http"
    assert boundary.distance == 0
    assert boundary.status == IMPACT_STATUS_PROVEN


def test_changed_service_reaching_an_http_handler_is_also_an_api_boundary() -> None:
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
    assert boundary.path is not None and boundary.path.length >= 1


# --------------------------------------------------------------------------
# B. Seed route-registration boundary
# --------------------------------------------------------------------------

def test_b_changed_route_registration_symbol_is_emitted_as_api_without_a_caller() -> None:
    """A route-REGISTRATION symbol, not a per-route handler — structurally
    represented the same way a decorated/route-tagged entrypoint is
    (CBM/native attach `route_method`/`route_path` or decorator text to
    whatever it recognized as registering a route). It must be classified
    directly from the seed, never requiring a global route-file search or
    another caller first."""
    f = facts(
        entrypoints=[
            entrypoint("RegisterPublicRoutes", decorators="@router.register", method=None, path=None),
        ],
    )
    # A route-registration symbol may carry route metadata directly, which
    # is the strongest, most direct form of this evidence.
    f.entrypoints[0]["route_method"] = "ANY"
    f.entrypoints[0]["route_path"] = "/*"
    result = interpret(changed("RegisterPublicRoutes"), f)

    assert len(result.boundaries) == 1
    assert result.boundaries[0].kind == BOUNDARY_API
    assert result.boundaries[0].distance == 0


# --------------------------------------------------------------------------
# C. Test caller exclusion / D. traversal does not stop at a test
# --------------------------------------------------------------------------

def test_c_test_caller_is_never_emitted_as_a_production_boundary() -> None:
    """changed production function -> test caller. The test must not be
    reported as callable/api/async, by file-role classification."""
    f = facts(
        call_edges=[call_edge("test_get_deleted_community", "get_deleted_community")],
        files=[symbol_file("app/tests/community_tests.py", [sym("test_get_deleted_community", exported=True)])],
    )
    result = interpret(changed("get_deleted_community", file="app/svc.py"), f)

    assert result.boundaries == []
    assert all(item.symbol != "test_get_deleted_community" for item in result.boundaries)


def test_c_inline_test_function_excluded_by_name_convention_alone() -> None:
    """The Rust shape: a test lives inside an ordinary source file (no
    separate tests/ directory), recognizable only by the same `test_*`
    naming convention Sydes already uses for test *files*."""
    f = facts(
        call_edges=[call_edge("test_outbox_deleted_user", "delete_user")],
        files=[symbol_file("app/svc.py", [sym("test_outbox_deleted_user", exported=True)])],
    )
    result = interpret(changed("delete_user", file="app/svc.py"), f)

    assert result.boundaries == []


def test_d_traversal_continues_through_a_test_caller_to_a_real_boundary() -> None:
    """A test node must not prematurely terminate discovery: production
    code the test itself calls, in turn reaching a real API boundary, must
    still be found."""
    f = facts(
        call_edges=[
            call_edge("test_get_deleted_community", "get_deleted_community"),
            call_edge("http_handler", "test_get_deleted_community",
                      callee_file="app/tests/community_tests.py"),
        ],
        files=[symbol_file("app/tests/community_tests.py", [sym("test_get_deleted_community", exported=True)])],
        entrypoints=[entrypoint("http_handler", method="GET", path="/community")],
    )
    result = interpret(changed("get_deleted_community", file="app/svc.py"), f)

    assert len(result.boundaries) == 1
    assert result.boundaries[0].kind == BOUNDARY_API
    assert result.boundaries[0].symbol == "http_handler"


# --------------------------------------------------------------------------
# E. Generic exported cross-file caller — not automatically public_library
# --------------------------------------------------------------------------

def test_e_generic_exported_same_module_caller_is_not_a_boundary() -> None:
    """Exported + a different file is common, ordinary code splitting
    within the same module/directory — not evidence of a public API
    surface. Must remain unresolved rather than guessed as `callable`."""
    f = facts(
        call_edges=[call_edge("neighbor_caller", "helper", caller_file="app/neighbor.py")],
        files=[symbol_file("app/neighbor.py", [sym("neighbor_caller", exported=True)])],
    )
    result = interpret(changed("helper", file="app/svc.py"), f)

    assert result.boundaries == []
    assert result.unresolved and result.unresolved[0].symbol == "helper"


# --------------------------------------------------------------------------
# F. Real public callable synthetic case
# --------------------------------------------------------------------------

def test_f_exported_symbol_across_a_module_boundary_is_callable() -> None:
    """Stronger evidence: the exported symbol lives in a genuinely different
    module/package directory (`app/public/`) than its caller
    (`app/internal/`) — a real cross-module boundary, not just a
    neighboring file."""
    f = facts(
        call_edges=[
            call_edge("service_method", "helper",
                      caller_file="app/internal/svc.py", callee_file="app/internal/helper.py"),
            call_edge("public_export", "service_method",
                      caller_file="app/public/api.py", callee_file="app/internal/svc.py"),
        ],
        files=[symbol_file("app/public/api.py", [sym("public_export", exported=True)])],
    )
    result = interpret(changed("helper", file="app/internal/helper.py"), f)

    assert len(result.boundaries) == 1
    boundary = result.boundaries[0]
    assert boundary.kind == BOUNDARY_CALLABLE
    assert boundary.subtype == "public_library"
    assert boundary.symbol == "public_export"


# --------------------------------------------------------------------------
# G. `main` must not become public_library
# --------------------------------------------------------------------------

def test_g_main_is_never_classified_as_a_callable_boundary() -> None:
    f = facts(
        call_edges=[call_edge("main", "helper", caller_file="app/other_dir/bin.py")],
        files=[symbol_file("app/other_dir/bin.py", [sym("main", exported=True)])],
    )
    result = interpret(changed("helper", file="app/svc.py"), f)

    assert result.boundaries == []
    assert all(item.symbol != "main" for item in result.boundaries)


# --------------------------------------------------------------------------
# H. Async — strong structural evidence required
# --------------------------------------------------------------------------

def test_h_changed_helper_reaching_a_scheduled_job_is_an_async_boundary() -> None:
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


def test_h_event_handler_decorator_is_recognized_as_a_distinct_async_subtype() -> None:
    f = facts(
        call_edges=[call_edge("on_order_created", "helper")],
        entrypoints=[entrypoint("on_order_created", decorators="@signal_receiver(OrderCreated)")],
    )
    result = interpret(changed("helper"), f)

    assert result.boundaries[0].kind == BOUNDARY_ASYNC
    assert result.boundaries[0].subtype == "event_handler"


# --------------------------------------------------------------------------
# I. Generic decorator alone must NOT become async
# --------------------------------------------------------------------------

def test_i_generic_unrecognized_decorator_does_not_become_async() -> None:
    """A decorator exists, but its keywords match nothing in the small
    scheduled-job/event-handler vocabulary — ambiguous, so it must not be
    upgraded to a proven async boundary. It also is not exported, so it is
    not classified any other way either: traversal ends without inventing
    a boundary, which is the correct, honest outcome."""
    f = facts(
        call_edges=[call_edge("middleware_wrapper", "helper")],
        entrypoints=[entrypoint("middleware_wrapper", decorators="@app.middleware('http')")],
    )
    result = interpret(changed("helper"), f)

    assert result.boundaries == []


# --------------------------------------------------------------------------
# J. Existing soundness — semantic hints and signature references
# --------------------------------------------------------------------------

def test_j_semantic_hint_never_fabricates_an_edge_to_an_unrelated_symbol() -> None:
    f = facts(
        call_edges=[call_edge("cleanup_expired_orders", "helper")],
        entrypoints=[
            entrypoint("cleanup_expired_orders", decorators="@celery.task"),
            entrypoint("totally_unrelated_admin_tool", decorators="@celery.task"),
        ],
    )
    result = interpret(changed("helper"), f, semantic_texts=["totally_unrelated_admin_tool"])

    labels = {item.symbol for item in result.boundaries}
    assert "totally_unrelated_admin_tool" not in labels
    assert "cleanup_expired_orders" in labels


def test_j_semantic_hint_with_no_structural_edges_at_all_produces_no_boundary() -> None:
    f = facts(entrypoints=[entrypoint("cleanup_expired_orders", decorators="@celery.task")])
    result = interpret(
        changed("helper"), f,
        semantic_texts=["order expiration", "cleanup_expired_orders", "sales channel"],
    )

    assert result.boundaries == []


def test_j_signature_only_reference_never_becomes_a_boundary_or_proven_impact() -> None:
    """A changed *type* named only in a handler's signature — the weakest
    evidence the interpreter produces. It must not appear in `boundaries`
    (this traversal never walks signature references at all) and the
    existing entrypoint record it does produce must be INFERRED, not
    PROVEN — the prior soundness fix `_record(status=...)` still holds."""
    f = facts(entrypoints=[entrypoint("update_item", method="PUT", path="/items/{id}")])
    f.entrypoints[0]["signature"] = "(item_in: ItemUpdate, db: Session)"
    result = interpret(changed("ItemUpdate"), f)

    assert result.boundaries == []
    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED


def test_semantically_relevant_candidate_is_emitted_before_an_unrelated_one() -> None:
    """Ranking still works among real candidates — the semantically
    matching one wins the single budget slot, but both edges are real."""
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


# --------------------------------------------------------------------------
# K. Existing HTTP regression
# --------------------------------------------------------------------------

def test_k_existing_http_entrypoint_behavior_is_unaffected_by_boundary_discovery() -> None:
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


def test_k_boundary_discovery_works_with_no_semantic_hints_at_all() -> None:
    f = facts(
        call_edges=[call_edge("http_handler", "helper")],
        entrypoints=[entrypoint("http_handler", method="POST", path="/y")],
    )
    result = interpret(changed("helper"), f, semantic_texts=None)

    assert len(result.boundaries) == 1
    assert result.boundaries[0].kind == BOUNDARY_API


# --------------------------------------------------------------------------
# Bounded traversal (shared-utility explosion) — unchanged budget behavior
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
# Non-HTTP serialization / reporting
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
