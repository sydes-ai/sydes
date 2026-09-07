"""Bridge Sydes' own deterministic route composition into structural entrypoints.

`StructuralFacts.entrypoints` has historically been populated only from a
backend's decorator/annotation model (CBM's `decorated_symbols`). That model
describes Python (`@app.route(...)`), TypeScript (`@Get()`), and Java
(`@GetMapping`) route registration well, because those frameworks attach the
route declaration directly to the handler as a decorator or annotation. It
describes nothing about frameworks that register a route by passing the
handler as a plain function-reference argument into a call elsewhere — Go/Gin
(`router.POST("/transfers", server.createTransfer)`) is the case that exposed
this, but the same gap applies to any handler-by-reference registration style.

Sydes' own route-index/route-graph composition (`discover/route_index.py`,
`discover/route_graph.py`) already discovers these routes correctly and
deterministically — it is framework-agnostic by construction, not a Gin
detector — but that data has only ever fed route-discovery output (`sydes
routes`/`sydes trace`) and coverage diagnostics, never `StructuralFacts`.

This module converts that already-computed, already-deterministic route data
into the same entrypoint-dict shape CBM's own entrypoints use, and merges the
two lists without losing or duplicating either side. It never reads an LLM
route candidate — only `status="deterministic_composed"` entries from
`route_graph`'s composition, which never involves the LLM discovery path at
all (see `discover/endpoints.py`'s separate `run_llm_endpoint_discovery`).
"""

from __future__ import annotations

from typing import Any

#: Marks an entrypoint dict as bridged from Sydes' own deterministic route
#: composition rather than reported by the code-intelligence backend, so a
#: reader (or a future dedup pass) can always tell the two apart.
ROUTE_INDEX_SOURCE = "route_index"


def bare_handler_symbol(handler: str) -> str:
    """The callable name a handler reference actually names.

    Generic across languages: a handler is either a bare identifier
    (`createTransfer`) or a receiver/method reference (`server.createTransfer`,
    `Server.createTransfer`, `self.create_transfer`). In every case the
    callable's own name is the last dotted segment — this makes no assumption
    about what a receiver is (a Go method value, a Python `self`, a JS object),
    only that the shape is `<...>.<name>`. The receiver itself is deliberately
    discarded rather than guessed at: matching happens by (file, bare name)
    downstream, which is exactly what already resolves this class of mismatch
    for identities CBM reports differently in different facts (see
    `impact.interpreter._FactIndex._learn`).
    """
    name = handler.strip()
    if not name:
        return ""
    return name.rsplit(".", 1)[-1]


def entrypoints_from_route_graph(
    route_graph: dict[str, Any], repo_names: list[str],
) -> list[dict[str, Any]]:
    """Convert deterministically-composed routes into entrypoint-dict shape.

    Only reads `_repo_endpoint_candidates`, the per-repo list of
    `EndpointCandidate` objects `route_graph.py` composes from
    `route_index.py`'s declarations and mounts. Every candidate there carries
    `status="deterministic_composed"` — there is no path from the LLM route
    discovery pass into this structure, so nothing here can promote an
    LLM-only hypothesis to a structural fact.

    A candidate with no handler reference is skipped: a route path/method
    with no known handler symbol would produce an entrypoint Sydes could
    never actually match against a changed symbol, which is not useful and
    would only add index noise.
    """
    candidates_by_repo = route_graph.get("_repo_endpoint_candidates") or {}
    entrypoints: list[dict[str, Any]] = []
    for repo in repo_names:
        for candidate in candidates_by_repo.get(repo, []) or []:
            handler = getattr(candidate, "handler", None)
            file = getattr(candidate, "file", None)
            if not handler or not file:
                continue
            symbol = bare_handler_symbol(str(handler))
            if not symbol:
                continue
            entrypoints.append({
                "repo": repo,
                "qualified_name": "",
                "symbol": symbol,
                "file": str(file),
                "line": None,
                "route_method": getattr(candidate, "method", None),
                "route_path": getattr(candidate, "path", None),
                "decorators": "",
                "signature": "",
                "source": ROUTE_INDEX_SOURCE,
            })
    return entrypoints


def _entry_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    """Identity used for dedup: the same (repo, file, symbol) an existing
    entrypoint already covers makes a second entry redundant, regardless of
    which side reported it first."""
    return (
        str(entry.get("repo") or ""),
        str(entry.get("file") or ""),
        str(entry.get("symbol") or ""),
    )


def merge_entrypoints(
    backend_entrypoints: list[dict[str, Any]],
    route_entrypoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge backend-reported and route-derived entrypoints.

    Every backend entrypoint is kept exactly as reported — this never
    replaces or edits a CBM (or any other backend's) entry. A route-derived
    entry is added only when no existing entry already covers the same
    (repo, file, symbol); a decorator-based framework where both CBM and the
    route-index already agree keeps exactly one entry, not two.
    """
    merged = list(backend_entrypoints)
    seen = {_entry_key(entry) for entry in backend_entrypoints}
    for entry in route_entrypoints:
        key = _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged
