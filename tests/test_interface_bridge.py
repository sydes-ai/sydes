"""Tests for bridging a call edge through a Java interface to its sole
implementation — the generic fix for JAVA-S-01 (spring-boot-demo), where
`UserController.save()` calls `IUserService.save()` (interface-typed field),
and CBM's call graph accordingly stops at the interface, never reaching
`UserServiceImpl.save()`, the actual changed symbol.
"""

from __future__ import annotations

from sydes.discover.interface_bridge import INTERFACE_BRIDGE_SOURCE, bridge_interface_call_edges


def _route_index(files: list[dict]) -> dict:
    return {"repos": [{"repo": "app", "files": files}]}


def _file(path: str, java_type: dict | None) -> dict:
    return {"path": path, "java_type": java_type}


def _edge(caller_file: str, caller_symbol: str, callee_file: str, callee_symbol: str) -> dict:
    return {
        "repo": "app",
        "caller_file": caller_file,
        "caller_symbol": caller_symbol,
        "caller_qualified_name": f"Some.Long.Cbm.Path.{caller_symbol}",
        "caller_line": 10,
        "callee_file": callee_file,
        "callee_symbol": callee_symbol,
        "callee_qualified_name": f"Some.Long.Cbm.Path.{callee_symbol}",
        "callee_line": 20,
        "source": "cbm",
    }


def test_a_call_to_an_interface_with_one_implementation_is_bridged() -> None:
    route_index = _route_index([
        _file("service/IUserService.java", {"kind": "interface", "name": "IUserService", "implements": []}),
        _file("service/impl/UserServiceImpl.java", {"kind": "class", "name": "UserServiceImpl", "implements": ["IUserService"]}),
        _file("controller/UserController.java", {"kind": "class", "name": "UserController", "implements": []}),
    ])
    edges = [_edge("controller/UserController.java", "save", "service/IUserService.java", "save")]

    bridged = bridge_interface_call_edges(route_index, edges)

    assert len(bridged) == 1
    edge = bridged[0]
    assert edge["caller_file"] == "controller/UserController.java"
    assert edge["caller_symbol"] == "save"
    assert edge["callee_file"] == "service/impl/UserServiceImpl.java"
    assert edge["callee_symbol"] == "save"
    assert edge["callee_qualified_name"] == "UserServiceImpl.save"
    assert edge["source"] == INTERFACE_BRIDGE_SOURCE
    # The original interface edge is untouched — this is additive, not a rewrite.
    assert edges == [_edge("controller/UserController.java", "save", "service/IUserService.java", "save")]


def test_an_interface_with_two_implementations_is_left_unresolved() -> None:
    """More than one implementation is a genuine 'don't guess' case — the
    same reasoning `handler_resolver.py` already applies to an ambiguous
    bare method name across two receivers."""
    route_index = _route_index([
        _file("service/IUserService.java", {"kind": "interface", "name": "IUserService", "implements": []}),
        _file("service/impl/UserServiceImplA.java", {"kind": "class", "name": "UserServiceImplA", "implements": ["IUserService"]}),
        _file("service/impl/UserServiceImplB.java", {"kind": "class", "name": "UserServiceImplB", "implements": ["IUserService"]}),
    ])
    edges = [_edge("controller/UserController.java", "save", "service/IUserService.java", "save")]

    bridged = bridge_interface_call_edges(route_index, edges)

    assert bridged == []


def test_a_call_not_touching_any_interface_is_untouched() -> None:
    route_index = _route_index([
        _file("controller/UserController.java", {"kind": "class", "name": "UserController", "implements": []}),
        _file("service/impl/UserServiceImpl.java", {"kind": "class", "name": "UserServiceImpl", "implements": []}),
    ])
    edges = [_edge("controller/UserController.java", "save", "service/impl/UserServiceImpl.java", "save")]

    bridged = bridge_interface_call_edges(route_index, edges)

    assert bridged == []


def test_no_interfaces_anywhere_short_circuits_cleanly() -> None:
    route_index = _route_index([_file("controller/UserController.java", None)])
    edges = [_edge("controller/UserController.java", "save", "somewhere/Else.java", "save")]

    assert bridge_interface_call_edges(route_index, edges) == []


def test_a_bridge_already_present_is_not_duplicated() -> None:
    """If CBM (or some other source) already reports the direct edge, the
    bridge must not add a second, redundant copy of it."""
    route_index = _route_index([
        _file("service/IUserService.java", {"kind": "interface", "name": "IUserService", "implements": []}),
        _file("service/impl/UserServiceImpl.java", {"kind": "class", "name": "UserServiceImpl", "implements": ["IUserService"]}),
    ])
    edges = [
        _edge("controller/UserController.java", "save", "service/IUserService.java", "save"),
        _edge("controller/UserController.java", "save", "service/impl/UserServiceImpl.java", "save"),
    ]

    bridged = bridge_interface_call_edges(route_index, edges)

    assert bridged == []


def test_empty_route_index_produces_no_bridges() -> None:
    assert bridge_interface_call_edges({}, [_edge("a.java", "x", "b.java", "y")]) == []
