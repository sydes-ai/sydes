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
from sydes.impact.boundary_discovery import BoundaryBudget, is_production_boundary_candidate
from sydes.impact.boundary_evidence import build_boundary_evidence
from sydes.impact.models import (
    BOUNDARY_API,
    BOUNDARY_ASYNC,
    BOUNDARY_CALLABLE,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    SymbolIdentity,
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


def sym(name: str, *, exported: bool = False, start_line: int = 1, end_line: int = 2) -> dict:
    return {
        "name": name, "kind": "function", "exported": exported,
        "start_line": start_line, "end_line": end_line,
    }


def route_call(*, method: str, path: str, line: int, handler_hint: str = "") -> dict:
    """One entry of `route_index.files[].route_calls` — the shape Sydes'
    own deterministic route extractor already produces for a registration
    call site, including the line it sits on."""
    return {
        "receiver": "router", "method": method, "path": path,
        "handler_hint": handler_hint, "line": line, "snippet": "",
    }


def route_index_file(path: str, *, route_calls: list[dict] | None = None,
                     exports: list[dict] | None = None) -> dict:
    return {
        "path": path, "language": "python", "role": "source_route_candidate",
        "signals": [], "router_symbols": [], "containers": [],
        "route_calls": route_calls or [], "mount_calls": [],
        "imports": [], "exports": exports or [], "path_literals": [],
    }


def facts(**kwargs) -> StructuralFacts:
    symbol_index = kwargs.get("symbol_index")
    if symbol_index is None:
        symbol_index = {"repos": [{"repo": REPO, "files": kwargs.get("files", [])}]}
    route_index = kwargs.get("route_index")
    if route_index is None:
        route_index = {"repos": [{"repo": REPO, "files": kwargs.get("route_files", [])}]}
    return StructuralFacts(
        call_edges=kwargs.get("call_edges", []),
        usage_edges=kwargs.get("usage_edges", []),
        entrypoints=kwargs.get("entrypoints", []),
        symbol_index=symbol_index,
        route_index=route_index,
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

def test_b2_route_registration_call_site_inside_a_changed_method_is_api() -> None:
    """C.2's central recall fix. The changed method contains route-
    registration call sites (`route_index.route_calls`, with line numbers
    Sydes' own extractor already recorded). Attributing those lines to the
    enclosing symbol makes it an API boundary at distance 0 — no route
    metadata on the symbol itself, no global route-file search, no caller."""
    f = facts(
        files=[symbol_file("app/routes.py", [
            sym("RegisterPublicRoutes", start_line=10, end_line=30),
        ])],
        route_files=[route_index_file("app/routes.py", route_calls=[
            route_call(method="get", path="/self-service/login", line=12),
            route_call(method="post", path="/self-service/logout", line=18),
        ])],
    )
    result = interpret(changed("RegisterPublicRoutes", file="app/routes.py"), f)

    assert len(result.boundaries) == 1
    boundary = result.boundaries[0]
    assert boundary.kind == BOUNDARY_API
    assert boundary.subtype == "route_registration"
    assert boundary.distance == 0
    assert boundary.status == IMPACT_STATUS_PROVEN


def test_b3_route_call_outside_any_symbol_span_is_not_attributed_to_a_symbol() -> None:
    """A module-level route call (line 2) sits inside no symbol's body — it
    must not be misattributed to a nearby function (lines 10-30)."""
    f = facts(
        files=[symbol_file("app/routes.py", [sym("unrelated", start_line=10, end_line=30)])],
        route_files=[route_index_file("app/routes.py", route_calls=[
            route_call(method="get", path="/x", line=2),
        ])],
    )
    result = interpret(changed("unrelated", file="app/routes.py"), f)

    assert result.boundaries == []


def test_b4_handler_named_by_a_route_call_is_an_http_boundary() -> None:
    """The handler a route registration names is itself an API boundary,
    even when the backend attached no route metadata to that symbol."""
    f = facts(
        call_edges=[call_edge("login_handler", "helper",
                              caller_file="app/handlers.py", callee_file="app/svc.py")],
        files=[symbol_file("app/handlers.py", [sym("login_handler", start_line=5, end_line=9)])],
        route_files=[route_index_file("app/handlers.py", route_calls=[
            route_call(method="post", path="/login", line=1, handler_hint="login_handler"),
        ])],
    )
    result = interpret(changed("helper", file="app/svc.py"), f)

    assert len(result.boundaries) == 1
    assert result.boundaries[0].kind == BOUNDARY_API
    assert result.boundaries[0].subtype == "http"
    assert result.boundaries[0].symbol == "login_handler"


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


def test_c_go_test_file_basename_excludes_bare_camelcase_test_symbol() -> None:
    """The real-PR failure shape this fix targets: a Go test file recognized
    purely by its own `_test.go` basename convention (no `tests/` directory
    involved at all), with a bare CamelCase `TestX` symbol that the older
    `test_`/`_test` English-word convention would have missed entirely."""
    f = facts(
        call_edges=[call_edge("TestLogout", "logoutHandler",
                              caller_file="selfservice/flow/logout/handler_test.go",
                              callee_file="selfservice/flow/logout/handler.go")],
        files=[symbol_file("selfservice/flow/logout/handler_test.go",
                            [sym("TestLogout", exported=True)])],
    )
    result = interpret(
        changed("logoutHandler", file="selfservice/flow/logout/handler.go"), f,
    )

    assert all(item.symbol != "TestLogout" for item in result.boundaries)


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

def test_f_explicit_export_statement_makes_a_reached_symbol_callable() -> None:
    """C.2: an explicit export *statement* (recorded in `route_index`) is
    the strong public-surface evidence a callable boundary requires — not
    the raw `exported` flag, which for Python is only a naming convention."""
    f = facts(
        call_edges=[
            call_edge("service_method", "helper",
                      caller_file="app/internal/svc.py", callee_file="app/internal/helper.py"),
            call_edge("public_export", "service_method",
                      caller_file="app/public/api.py", callee_file="app/internal/svc.py"),
        ],
        files=[symbol_file("app/public/api.py", [sym("public_export", exported=True)])],
        route_files=[
            route_index_file("app/public/api.py",
                             exports=[{"kind": "named", "symbol": "public_export"}]),
        ],
    )
    result = interpret(changed("helper", file="app/internal/helper.py"), f)

    assert len(result.boundaries) == 1
    boundary = result.boundaries[0]
    assert boundary.kind == BOUNDARY_CALLABLE
    assert boundary.subtype == "public_callable"
    assert boundary.symbol == "public_export"


def test_g2_raw_exported_flag_alone_does_not_make_a_callable_boundary() -> None:
    """The C.2 correction, stated directly: identical graph to the test
    above but with NO explicit export statement. The `exported=True` flag
    and a cross-directory hop are no longer sufficient on their own."""
    f = facts(
        call_edges=[
            call_edge("service_method", "helper",
                      caller_file="app/internal/svc.py", callee_file="app/internal/helper.py"),
            call_edge("plain_caller", "service_method",
                      caller_file="app/public/api.py", callee_file="app/internal/svc.py"),
        ],
        files=[symbol_file("app/public/api.py", [sym("plain_caller", exported=True)])],
    )
    result = interpret(changed("helper", file="app/internal/helper.py"), f)

    assert result.boundaries == []


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

def test_emitted_decision_carries_its_normalized_evidence_for_tracing() -> None:
    """Observability: the bounded decision log records WHY a node qualified
    — the normalized evidence — so `impact_decisions.jsonl` can explain a
    boundary without a raw source dump."""
    f = facts(
        call_edges=[call_edge("http_handler", "helper")],
        entrypoints=[entrypoint("http_handler", method="GET", path="/x")],
    )
    result = interpret(changed("helper"), f)

    emitted = [item for item in result.boundary_decisions if item["decision"] == "emitted"]
    assert len(emitted) == 1
    evidence = emitted[0]["normalized_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "api"
    assert evidence[0]["subtype"] == "http"
    assert evidence[0]["source"] == "route_metadata"
    assert evidence[0]["strength"] == "strong"


# --------------------------------------------------------------------------
# Normalization layer, tested directly
# --------------------------------------------------------------------------

def _identity(file: str, symbol: str) -> SymbolIdentity:
    return SymbolIdentity.from_fields(repo=REPO, file=file, short_name=symbol)


def test_normalization_route_metadata_becomes_strong_api_http_evidence() -> None:
    f = facts(entrypoints=[entrypoint("show_order", method="GET", path="/orders/{id}")])
    evidence = build_boundary_evidence(f, repo=REPO).strongest(_identity("app/svc.py", "show_order"))

    assert evidence is not None
    assert (evidence.kind, evidence.subtype) == ("api", "http")
    assert evidence.source == "route_metadata"
    assert evidence.strength == "strong"


def test_normalization_scheduler_decorator_becomes_strong_async_scheduled_job() -> None:
    f = facts(entrypoints=[entrypoint("nightly", decorators="@shared_task(cron='0 2 * * *')")])
    evidence = build_boundary_evidence(f, repo=REPO).strongest(_identity("app/svc.py", "nightly"))

    assert evidence is not None
    assert (evidence.kind, evidence.subtype) == ("async", "scheduled_job")
    assert evidence.strength == "strong"


def test_normalization_signal_decorator_becomes_strong_async_event_handler() -> None:
    f = facts(entrypoints=[entrypoint("on_created", decorators="@receiver(post_save)")])
    evidence = build_boundary_evidence(f, repo=REPO).strongest(_identity("app/svc.py", "on_created"))

    assert evidence is not None
    assert (evidence.kind, evidence.subtype) == ("async", "event_handler")


def test_normalization_generic_decorator_produces_no_evidence_at_all() -> None:
    """Not weak evidence — NO evidence. Generic decoration stays generic."""
    f = facts(entrypoints=[entrypoint("wrapped", decorators="@functools.wraps(inner)")])
    index = build_boundary_evidence(f, repo=REPO)

    assert index.for_identity(_identity("app/svc.py", "wrapped")) == []


def test_normalization_raw_exported_flag_produces_no_evidence() -> None:
    """The `exported` bool never becomes evidence — only an explicit export
    statement does. Pins the C.2 correction at the normalization layer."""
    f = facts(files=[symbol_file("app/pub.py", [sym("thing", exported=True)])])
    index = build_boundary_evidence(f, repo=REPO)

    assert index.for_identity(_identity("app/pub.py", "thing")) == []


def test_normalization_reads_no_semantic_input_and_needs_no_repo() -> None:
    """`build_boundary_evidence` takes only `StructuralFacts` — there is no
    parameter through which a semantic hint could ever arrive, which is why
    semantic analysis structurally cannot manufacture evidence."""
    import inspect

    parameters = set(inspect.signature(build_boundary_evidence).parameters)
    assert parameters == {"facts", "repo"}


# --------------------------------------------------------------------------
# Precision hardening: Go's own `_test.go` basename convention and its
# `TestFoo`/`BenchmarkFoo`/`FuzzFoo` naming convention, on the shared
# `is_production_boundary_candidate` predicate directly.
# --------------------------------------------------------------------------

def test_go_test_file_basename_alone_excludes_the_symbol_no_directory_needed() -> None:
    """`selfservice/flow/logout/handler_test.go` has no `tests/`-named
    directory anywhere in its path — only Go's own `_test.go` basename
    convention identifies it, and that must be enough on its own."""
    identity = _identity("selfservice/flow/logout/handler_test.go", "TestLogout")

    assert is_production_boundary_candidate(identity, facts()) is False


def test_go_benchmark_and_fuzz_functions_are_excluded_by_the_same_file_convention() -> None:
    for name in ("BenchmarkLogout", "FuzzLogout"):
        identity = _identity("selfservice/flow/logout/handler_test.go", name)
        assert is_production_boundary_candidate(identity, facts()) is False


def test_go_bare_test_function_name_is_excluded_when_file_context_establishes_go() -> None:
    """Fallback for when file evidence is incomplete (e.g. a grouped
    boundary resolved by symbol name alone) but the language context is
    still known to be Go — the name convention alone must still exclude it."""
    for name in ("TestLogout", "BenchmarkLogout", "FuzzLogout", "Test", "Benchmark", "Fuzz"):
        identity = _identity("selfservice/flow/logout/other.go", name)
        assert is_production_boundary_candidate(identity, facts()) is False


def test_go_neighboring_production_symbol_still_survives() -> None:
    """The paired positive case: a real handler/route-registration symbol
    in the neighboring, non-test Go file must not be caught by this fix."""
    identity = _identity("selfservice/flow/logout/handler.go", "RegisterPublicRoutes")

    assert is_production_boundary_candidate(identity, facts()) is True


def test_go_style_name_outside_go_file_context_is_not_excluded_by_this_rule() -> None:
    """Requirement: no broad `/^Test/` rule — an ordinary production symbol
    that merely starts with the English word "Test" survives when there is
    no Go file evidence to establish test-code context at all."""
    identity = _identity("app/handler.py", "TestConnectionPool")

    assert is_production_boundary_candidate(identity, facts()) is True


def test_go_production_file_with_similar_but_non_matching_name_survives() -> None:
    """Within Go file context, only the exact convention (prefix followed
    by a non-lowercase rune, or an exact bare prefix) excludes a symbol —
    `Testing`/`Fuzzy`-shaped names are ordinary identifiers to `go test`
    itself and must not be excluded."""
    for name in ("Testing", "Fuzzy", "BenchmarkingSuite"):
        identity = _identity("selfservice/flow/logout/handler.go", name)
        assert is_production_boundary_candidate(identity, facts()) is True


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
