"""Reconciling an ImpactInterpreter entrypoint with a Sydes route.

CBM and Sydes can disagree about a route's *path* while agreeing completely
about which *handler* serves it. CBM reports the decorator's own literal
argument — `POST /` for a handler mounted under a router prefix — while
Sydes' route graph composes the mount chain and knows the real `POST
/students`. Both describe the same handler; only one of them is the path a
developer should be shown.

The reconciliation this module does is narrow on purpose: match by handler
identity (file + qualified/short name) against Sydes' already-composed route
graph, and take the composed method/path when a match exists. No text
inference, no fuzzy path matching — an entrypoint that cannot be matched to a
composed route by handler identity is passed through with whatever route
metadata it already carried (CBM's literal path, or none), never guessed at.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sydes.impact.models import AffectedEntrypoint, ENTRYPOINT_HTTP


def _handler_key(file: str, symbol: str) -> str:
    return f"{file}::{symbol}"


def build_route_lookup(route_graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index Sydes' composed routes by (handler file, handler symbol).

    Built once per `route_graph` payload and handed to
    `reconcile_entrypoints` for however many entrypoints need it, so
    reconciling a whole `ImpactResult` costs one pass over the route graph
    rather than one per entrypoint.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for repo_entry in route_graph.get("repos", []) or []:
        for row in repo_entry.get("composed_routes", []) or []:
            handler = str(row.get("handler") or "")
            file = str(row.get("file") or "")
            if not handler or not file:
                continue
            # First composed route for a handler wins; a handler served by
            # more than one route is unusual enough that picking arbitrarily
            # between them would misrepresent the change either way.
            lookup.setdefault(_handler_key(file, handler), row)
    return lookup


def reconcile_entrypoint(
    entrypoint: AffectedEntrypoint, route_lookup: dict[str, dict[str, Any]]
) -> AffectedEntrypoint:
    """Prefer Sydes' composed route for one entrypoint, by handler identity.

    Matching tries the entrypoint's short symbol name first (composed routes
    record a short handler name, not a qualified one) scoped to its file. A
    miss leaves the entrypoint exactly as the interpreter produced it — CBM's
    own route metadata if it had any, otherwise none.
    """
    if not entrypoint.file or not entrypoint.symbol:
        return entrypoint
    composed = route_lookup.get(_handler_key(entrypoint.file, entrypoint.symbol))
    if composed is None:
        return entrypoint
    return replace(
        entrypoint,
        kind=ENTRYPOINT_HTTP,
        route_method=str(composed.get("method") or entrypoint.route_method or ""),
        route_path=str(composed.get("path") or entrypoint.route_path or ""),
    )


def reconcile_entrypoints(
    entrypoints: list[AffectedEntrypoint], route_graph: dict[str, Any]
) -> list[AffectedEntrypoint]:
    """Reconcile every entrypoint in one pass over the route graph."""
    lookup = build_route_lookup(route_graph)
    return [reconcile_entrypoint(item, lookup) for item in entrypoints]
