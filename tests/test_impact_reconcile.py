"""Reconciling ImpactInterpreter entrypoints against Sydes' composed routes.

CBM reports a route's literal decorator path; Sydes' own route graph knows the
composed path after mount prefixes are applied. Reconciliation must prefer the
composed route whenever handler identity (file + symbol) matches, and must
never guess a path when no such match exists.
"""

from __future__ import annotations

from sydes.impact.models import (
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    ENTRYPOINT_UNKNOWN,
    AffectedEntrypoint,
)
from sydes.impact.reconcile import (
    build_route_lookup,
    reconcile_entrypoint,
    reconcile_entrypoints,
)


def entrypoint(
    symbol: str, *, file: str = "app/views.py", kind: str = ENTRYPOINT_UNKNOWN,
    route_method: str | None = None, route_path: str | None = None,
) -> AffectedEntrypoint:
    return AffectedEntrypoint(
        repo="app", symbol=symbol, qualified_name=f"app.{symbol}", file=file,
        kind=kind, route_method=route_method, route_path=route_path,
    )


def route_graph(*rows: dict) -> dict:
    return {"repos": [{"repo": "app", "composed_routes": list(rows)}]}


def composed_route(method: str, path: str, handler: str, file: str = "app/views.py") -> dict:
    return {"method": method, "path": path, "handler": handler, "file": file}


def test_composed_route_is_preferred_over_the_decorators_literal_path() -> None:
    ep = entrypoint(
        "create_student", kind=ENTRYPOINT_HTTP, route_method="POST", route_path="/",
    )
    graph = route_graph(composed_route("POST", "/students", "create_student"))
    reconciled = reconcile_entrypoint(ep, build_route_lookup(graph))
    assert reconciled.route_method == "POST"
    assert reconciled.route_path == "/students"
    assert reconciled.kind == ENTRYPOINT_HTTP


def test_no_composed_match_passes_the_entrypoint_through_unchanged() -> None:
    ep = entrypoint(
        "handle_webhook", kind=ENTRYPOINT_DECORATED, route_method=None, route_path=None,
    )
    graph = route_graph(composed_route("POST", "/students", "create_student"))
    reconciled = reconcile_entrypoint(ep, build_route_lookup(graph))
    assert reconciled is ep
    assert reconciled.kind == ENTRYPOINT_DECORATED
    assert reconciled.route_path is None


def test_matching_is_scoped_to_the_handlers_own_file() -> None:
    # Two different files can each define a function with the same short
    # name; only the same-file composed route may resolve an entrypoint.
    ep = entrypoint("update", file="app/other.py", kind=ENTRYPOINT_UNKNOWN)
    graph = route_graph(composed_route("PUT", "/x", "update", file="app/views.py"))
    reconciled = reconcile_entrypoint(ep, build_route_lookup(graph))
    assert reconciled is ep
    assert reconciled.route_path is None


def test_no_text_based_path_inference_when_handler_is_unresolved() -> None:
    # An entrypoint with no file/symbol at all cannot be matched by identity,
    # and reconciliation must not fall back to guessing from the path string.
    ep = AffectedEntrypoint(
        repo="app", symbol="", qualified_name="", file="",
        kind=ENTRYPOINT_UNKNOWN, route_method=None, route_path="/students",
    )
    graph = route_graph(composed_route("POST", "/students", "create_student"))
    reconciled = reconcile_entrypoint(ep, build_route_lookup(graph))
    assert reconciled is ep


def test_reconcile_entrypoints_processes_a_whole_list_in_one_pass() -> None:
    items = [
        entrypoint("create_student", kind=ENTRYPOINT_HTTP, route_method="POST", route_path="/"),
        entrypoint("delete_student", kind=ENTRYPOINT_HTTP, route_method="DELETE", route_path="/"),
        entrypoint("unmatched_handler", kind=ENTRYPOINT_DECORATED),
    ]
    graph = route_graph(
        composed_route("POST", "/students", "create_student"),
        composed_route("DELETE", "/students/{id}", "delete_student"),
    )
    reconciled = reconcile_entrypoints(items, graph)
    by_symbol = {item.symbol: item for item in reconciled}
    assert by_symbol["create_student"].route_path == "/students"
    assert by_symbol["delete_student"].route_path == "/students/{id}"
    assert by_symbol["unmatched_handler"].route_path is None
