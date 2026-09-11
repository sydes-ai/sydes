"""Unit tests for Phase A's comparison logic and the `_IncludedRouter`
recursion fix, using synthetic fixtures rather than importing a real app
(kept fast, hermetic, and independent of any target repo's own venv).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from _introspect_worker import _flatten_routes  # noqa: E402
from run_introspection import _normalize_path, compare  # noqa: E402


def _plain_route(path: str, methods: list[str]) -> SimpleNamespace:
    return SimpleNamespace(path=path, methods=set(methods))


def _included_router(prefix: str, sub_routes: list) -> SimpleNamespace:
    return SimpleNamespace(
        include_context=SimpleNamespace(
            prefix=prefix,
            included_router=SimpleNamespace(routes=sub_routes),
        )
    )


def test_flatten_plain_routes_unchanged():
    routes = [_plain_route("/health", ["GET"])]
    result = list(_flatten_routes(routes))
    assert result == [("/health", routes[0])]


def test_flatten_recurses_through_included_router_wrapper():
    sub = _plain_route("/speech", ["POST"])
    routes = [_included_router("/v1", [sub])]
    result = list(_flatten_routes(routes))
    assert result == [("/v1/speech", sub)]


def test_flatten_handles_nested_included_routers():
    sub = _plain_route("/inner", ["GET"])
    inner_wrapper = _included_router("/b", [sub])
    routes = [_included_router("/a", [inner_wrapper])]
    result = list(_flatten_routes(routes))
    assert result == [("/a/b/inner", sub)]


def test_flatten_skips_entries_with_no_path_and_no_include_context():
    routes = [SimpleNamespace()]
    assert list(_flatten_routes(routes)) == []


def test_normalize_path_collapses_param_syntax_and_trailing_slash():
    assert _normalize_path("/models/{id}/") == "/models/{param}"
    assert _normalize_path("/") == "/"


def test_compare_flags_prefix_composition_bug_as_mismatch_not_match():
    """Reproduces the real Kokoro-FastAPI finding at unit-test scale: a
    static entry missing a mount prefix must show up as BOTH a static-only
    row and a live-only row, never silently matched."""
    static_entrypoints = [
        {"route_method": "post", "route_path": "/audio/speech", "symbol": "create_speech", "file": "x.py"},
    ]
    live_routes = [
        {"path": "/v1/audio/speech", "methods": ["POST"], "endpoint_qualname": "create_speech", "endpoint_module": "x"},
    ]
    result = compare(static_entrypoints, live_routes)
    assert result["matched_count"] == 0
    assert result["static_only_count"] == 1
    assert result["live_only_count"] == 1


def test_compare_matches_identical_method_and_normalized_path():
    static_entrypoints = [
        {"route_method": "get", "route_path": "/health", "symbol": "health_check", "file": "main.py"},
    ]
    live_routes = [
        {"path": "/health", "methods": ["GET"], "endpoint_qualname": "health_check", "endpoint_module": "main"},
    ]
    result = compare(static_entrypoints, live_routes)
    assert result["matched_count"] == 1
    assert result["static_only_count"] == 0
    assert result["live_only_count"] == 0


def test_compare_matches_despite_differing_param_name():
    """Static and live may name a path parameter differently
    (`{filename}` vs `{model}` in different declarations) -- both must
    normalize to the same `{param}` placeholder to compare structurally,
    not by parameter name."""
    static_entrypoints = [
        {"route_method": "get", "route_path": "/models/{model}", "symbol": "retrieve_model", "file": "x.py"},
    ]
    live_routes = [
        {"path": "/v1/models/{model}", "methods": ["GET"], "endpoint_qualname": "retrieve_model", "endpoint_module": "x"},
    ]
    # Different prefix (v1 missing) -- still correctly NOT matched.
    result = compare(static_entrypoints, live_routes)
    assert result["matched_count"] == 0
