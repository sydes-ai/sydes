"""Phase A worker: runs INSIDE the target repo's own interpreter/venv, in a
subprocess, to import the app module and read back its OWN already-resolved
route table -- no static pattern-matching of any framework's routing syntax.

SECURITY / SAFETY DISCIPLINE (non-negotiable for this experiment):
  - This process writes ONLY route metadata (path, http methods, endpoint
    module + qualname, declared line if available) to `--out`.
  - It NEVER serializes the app's `settings`/config object, `os.environ`,
    or any attribute not explicitly listed in `_ROUTE_FIELDS` below.
  - It never touches app.state, app.dependency_overrides, or middleware
    internals -- only the route table, which is what this experiment needs.
  - Importing the module executes ONLY module-level statements (route
    registration is one of those); a FastAPI `lifespan` context manager body
    does NOT run just from import, so no model loading / network calls are
    triggered by this step for the fixture this was built against. This is
    still run under a hard wall-clock timeout by the parent (run_introspection.py)
    as a defense against any repo whose import has unexpected side effects.

Not wired into any production path. Standalone script, run with the target
repo's own Python (its own venv / dependencies), never Sydes' own venv.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys


def _route_methods(route: object) -> list[str]:
    methods = getattr(route, "methods", None)
    if not methods:
        return []
    return sorted(str(m) for m in methods)


def _endpoint_ref(route: object) -> dict[str, str | None]:
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return {"module": None, "qualname": None}
    func = getattr(endpoint, "__wrapped__", endpoint)
    return {
        "module": getattr(func, "__module__", None),
        "qualname": getattr(func, "__qualname__", None) or getattr(func, "__name__", None),
    }


def _flatten_routes(routes: list, prefix: str = ""):
    """Yield (full_path, route) pairs, recursing through FastAPI's opaque
    `_IncludedRouter` wrapper (present in newer FastAPI: `include_router`
    no longer flattens sub-routes into plain `APIRoute` objects in
    `app.routes` -- it stores a wrapper carrying `include_context.prefix`
    and `include_context.included_router.routes` instead). Older FastAPI
    versions that DO flatten directly fall through the plain-route branch
    unchanged, so this stays correct either way -- a real, version-specific
    fragility discovered empirically while building this experiment, not
    assumed in advance."""
    for route in routes:
        include_context = getattr(route, "include_context", None)
        if include_context is not None:
            sub_prefix = getattr(include_context, "prefix", None) or ""
            sub_router = getattr(include_context, "included_router", None)
            sub_routes = getattr(sub_router, "routes", None) or []
            yield from _flatten_routes(sub_routes, prefix + sub_prefix)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        yield prefix + path, route


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True, help="dotted module path exposing `app`")
    parser.add_argument("--attr", default="app", help="attribute name of the FastAPI/Starlette app object")
    parser.add_argument("--out", required=True, help="output JSON file path")
    args = parser.parse_args()

    mod = importlib.import_module(args.module)
    app = getattr(mod, args.attr)

    entries: list[dict] = []
    for full_path, route in _flatten_routes(getattr(app, "routes", [])):
        endpoint_ref = _endpoint_ref(route)
        entries.append({
            "path": full_path,
            "methods": _route_methods(route),
            "name": getattr(route, "name", None),
            "endpoint_module": endpoint_ref["module"],
            "endpoint_qualname": endpoint_ref["qualname"],
        })

    # Deliberately narrow: only the fields constructed above ever reach the
    # output file. No settings object, no environ, no arbitrary route
    # attribute is ever passed through unfiltered.
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"routes": entries}, fh, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
