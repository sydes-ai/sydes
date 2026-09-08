"""Bridge a call through a Java interface to its sole implementation.

A field typed as an interface (`private final IUserService userService`, the
standard Spring constructor-injection idiom) makes every call through it
resolve, in a purely static call graph, to the interface's own method
declaration — never to the concrete class actually run at runtime. CBM's call
edges have exactly this shape: `caller -> interface method`, never
`caller -> implementation method`, so a changed method that is only ever
reached through its interface has no path from any entrypoint at all.

This adds a synthetic edge alongside the interface edge — never replacing it
— from the same caller directly to the implementing class's same-named
method, but only when the interface has exactly one known implementation in
the repos being analyzed. More than one implementation is a genuine "don't
guess" case: attributing the call to one of several candidates would be
inventing evidence, the same reason an ambiguous bare method name is left
unresolved elsewhere (see `handler_resolver.py`).
"""

from __future__ import annotations

from typing import Any

#: Marks a synthetic edge as bridged rather than backend-reported, so a
#: reader (or a future dedup pass) can always tell the two apart — the same
#: convention `route_entrypoints.ROUTE_INDEX_SOURCE` already uses.
INTERFACE_BRIDGE_SOURCE = "interface_bridge"


def _java_typed_files(route_index: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for repo_payload in route_index.get("repos") or []:
        if not isinstance(repo_payload, dict):
            continue
        for file_item in repo_payload.get("files") or []:
            if isinstance(file_item, dict) and file_item.get("java_type"):
                files.append(file_item)
    return files


def _sole_implementation_index(route_index: dict[str, Any]) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """`(interface bare name -> its own file, interface bare name -> [(impl
    file, impl class name), ...])`, built from every file's `java_type`
    (added to `route_index.py`'s per-file scan alongside route composition —
    unrelated to it, just computed in the same pass)."""
    interface_files: dict[str, str] = {}
    implementations: dict[str, list[tuple[str, str]]] = {}
    for file_item in _java_typed_files(route_index):
        java_type = file_item["java_type"]
        file_path = str(file_item.get("path") or "")
        name = str(java_type.get("name") or "")
        if not name:
            continue
        if java_type.get("kind") == "interface":
            interface_files[name] = file_path
        elif java_type.get("kind") == "class":
            for interface_name in java_type.get("implements") or []:
                implementations.setdefault(interface_name, []).append((file_path, name))
    return interface_files, implementations


def _canonical_qualified_names(symbol_index: dict[str, Any] | None) -> dict[tuple[str, str], str]:
    """`(file, method_name) -> cbm_qualified_name` for every class method
    CBM gave one, so a bridged edge's callee can carry the same canonical
    form the changed symbol it needs to reach now prefers (see
    `SymbolIdentity.canonical_qualified_name`) — not just Sydes' own short
    `Class.method` display form, which a changed symbol's identity no
    longer keys on once a canonical form is available."""
    lookup: dict[tuple[str, str], str] = {}
    for repo_payload in (symbol_index or {}).get("repos") or []:
        if not isinstance(repo_payload, dict):
            continue
        for file_item in repo_payload.get("files") or []:
            if not isinstance(file_item, dict):
                continue
            file_path = str(file_item.get("path") or "")
            for symbol in file_item.get("symbols") or []:
                if not isinstance(symbol, dict) or symbol.get("kind") != "class_method":
                    continue
                canonical = symbol.get("cbm_qualified_name")
                if isinstance(canonical, str) and canonical:
                    lookup[(file_path, str(symbol.get("name") or ""))] = canonical
    return lookup


def bridge_interface_call_edges(
    route_index: dict[str, Any],
    call_edges: list[dict[str, Any]],
    symbol_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Synthetic edges to append to `call_edges` — never a replacement for
    any of them. Returns only the new edges; the caller decides how to merge
    (mirrors `route_entrypoints.entrypoints_from_route_graph`'s split between
    computing and merging).

    The synthetic edge's `callee_qualified_name` is CBM's own canonical
    qualified name for the implementation method when `symbol_index` gives
    one — the same form a changed symbol's identity now prefers (see
    `SymbolIdentity.canonical_qualified_name`) — falling back to Sydes' own
    short `Class.method` display form otherwise, exactly as before
    `canonical_qualified_name` existed. `symbol_index` is optional so any
    caller that predates it keeps working unchanged.
    """
    interface_files, implementations = _sole_implementation_index(route_index)
    if not interface_files:
        return []

    canonical_by_file_method = _canonical_qualified_names(symbol_index)
    interface_name_by_file = {file_path: name for name, file_path in interface_files.items()}
    seen = {
        (edge.get("caller_file"), edge.get("caller_symbol"), edge.get("callee_file"), edge.get("callee_symbol"))
        for edge in call_edges
    }

    bridged: list[dict[str, Any]] = []
    for edge in call_edges:
        callee_file = str(edge.get("callee_file") or "")
        interface_name = interface_name_by_file.get(callee_file)
        if not interface_name:
            continue
        impls = implementations.get(interface_name) or []
        if len(impls) != 1:
            continue
        impl_file, impl_class = impls[0]
        callee_symbol = str(edge.get("callee_symbol") or "")
        if not callee_symbol:
            continue
        key = (edge.get("caller_file"), edge.get("caller_symbol"), impl_file, callee_symbol)
        if key in seen:
            continue
        seen.add(key)
        qualified_name = canonical_by_file_method.get(
            (impl_file, callee_symbol), f"{impl_class}.{callee_symbol}",
        )
        bridged.append({
            **edge,
            "callee_file": impl_file,
            "callee_qualified_name": qualified_name,
            "callee_line": None,
            "source": INTERFACE_BRIDGE_SOURCE,
        })
    return bridged
