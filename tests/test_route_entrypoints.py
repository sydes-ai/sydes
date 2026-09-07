"""Tests for bridging deterministic route composition into structural
entrypoints — the generic fix for frameworks (Go/Gin and anything shaped
like it) that register a route by passing the handler as a plain
function-reference argument rather than decorating it.
"""

from __future__ import annotations

from pathlib import Path

from sydes.core.models import EndpointCandidate, RepoRef
from sydes.discover.route_entrypoints import (
    ROUTE_INDEX_SOURCE,
    bare_handler_symbol,
    entrypoints_from_route_graph,
    merge_entrypoints,
)
from sydes.discover.route_graph import build_route_graph_facts_from_route_index_batch
from sydes.discover.route_index import build_route_index_batch


def _go_style_route_index_batch() -> dict:
    """A route-index batch shaped like `simplebank`'s `api/server.go`: two
    receivers (`router`, `authRoutes`), no resolvable container/mount for
    either — matching what the real Go extractor produces, since Go's `:=`
    declaration and method-chained `.Use(...)` don't match the container/
    mount regexes today. `composed_routes` still comes out correct because
    a flat route call needs no container at all."""
    return {
        "version": "v1",
        "repos": [
            {
                "repo": "app",
                "root": "/repo",
                "files": [
                    {
                        "path": "api/server.go",
                        "language": "go",
                        "role": "source_route_candidate",
                        "signals": ["path_literals", "route_call:post"],
                        "router_symbols": [],
                        "route_calls": [
                            {
                                "receiver": "router",
                                "method": "post",
                                "path": "/users",
                                "handler_hint": "server.createUser",
                                "line": 46,
                                "snippet": 'router.POST("/users", server.createUser)',
                            },
                            {
                                "receiver": "authRoutes",
                                "method": "post",
                                "path": "/transfers",
                                "handler_hint": "server.createTransfer",
                                "line": 55,
                                "snippet": 'authRoutes.POST("/transfers", server.createTransfer)',
                            },
                        ],
                        "mount_calls": [],
                        "imports": [],
                        "exports": [],
                        "path_literals": ["/users", "/transfers"],
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------
# bare_handler_symbol
# --------------------------------------------------------------------------


def test_bare_handler_symbol_strips_a_go_style_receiver() -> None:
    assert bare_handler_symbol("server.createTransfer") == "createTransfer"


def test_bare_handler_symbol_strips_a_capitalized_receiver() -> None:
    assert bare_handler_symbol("Server.createTransfer") == "createTransfer"


def test_bare_handler_symbol_handles_a_bare_identifier() -> None:
    assert bare_handler_symbol("createUser") == "createUser"


def test_bare_handler_symbol_takes_the_last_segment_of_a_deeper_chain() -> None:
    assert bare_handler_symbol("self.controller.create_transfer") == "create_transfer"


def test_bare_handler_symbol_of_empty_string_is_empty() -> None:
    assert bare_handler_symbol("") == ""
    assert bare_handler_symbol("   ") == ""


# --------------------------------------------------------------------------
# entrypoints_from_route_graph
# --------------------------------------------------------------------------


def test_go_style_routes_become_entrypoint_dicts_with_full_route_evidence() -> None:
    route_graph = build_route_graph_facts_from_route_index_batch(_go_style_route_index_batch())
    entrypoints = entrypoints_from_route_graph(route_graph, ["app"])

    by_symbol = {item["symbol"]: item for item in entrypoints}
    assert set(by_symbol) == {"createUser", "createTransfer"}

    transfer = by_symbol["createTransfer"]
    assert transfer["route_method"] == "POST"
    assert transfer["route_path"] == "/transfers"
    assert transfer["file"] == "api/server.go"
    assert transfer["repo"] == "app"
    assert transfer["source"] == ROUTE_INDEX_SOURCE
    # No qualified_name is invented — matching is left to (file, bare name).
    assert transfer["qualified_name"] == ""


def test_routing_controllers_decorator_route_becomes_an_entrypoint_end_to_end(tmp_path: Path) -> None:
    """The same generic gap Go/Gin exposed (a route registration style the
    decorator-only entrypoint model can't see) also applies to any TypeScript
    framework that declares routes purely via class + method decorators
    (NestJS, routing-controllers, tsoa) rather than a call on a receiver.
    This exercises the real pipeline end to end: raw source text ->
    `build_route_index_batch` -> `build_route_graph_facts_from_route_index_batch`
    -> `entrypoints_from_route_graph`, mirroring the Go-style test above but
    starting from actual TypeScript source instead of a hand-built route index.
    """
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "PetController.ts").write_text(
        "\n".join(
            [
                "@Authorized()",
                "@JsonController('/pets')",
                "export class PetController {",
                "    constructor(private petService: PetService) { }",
                "    @Post()",
                "    @ResponseSchema(PetResponse)",
                "    public create(@Body() body: CreatePetBody): Promise<Pet> {",
                "        return this.petService.create(body);",
                "    }",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    repos = [RepoRef(name="app", root=str(repo_root))]
    route_index = build_route_index_batch(repos)
    route_graph = build_route_graph_facts_from_route_index_batch(route_index)
    entrypoints = entrypoints_from_route_graph(route_graph, ["app"])

    by_symbol = {item["symbol"]: item for item in entrypoints}
    assert "create" in by_symbol
    create = by_symbol["create"]
    assert create["route_method"] == "POST"
    assert create["route_path"] == "/pets"
    assert create["file"] == "src/PetController.ts"
    assert create["source"] == ROUTE_INDEX_SOURCE


def test_a_repo_not_present_in_the_route_graph_yields_no_entrypoints() -> None:
    route_graph = build_route_graph_facts_from_route_index_batch(_go_style_route_index_batch())
    assert entrypoints_from_route_graph(route_graph, ["other_repo"]) == []


def test_a_candidate_with_no_handler_is_skipped_not_guessed() -> None:
    route_graph = {
        "_repo_endpoint_candidates": {
            "app": [
                EndpointCandidate(method="get", path="/x", handler=None, file="a.go", repo="app"),
                EndpointCandidate(method="get", path="/x", handler="", file="a.go", repo="app"),
            ],
        },
    }
    assert entrypoints_from_route_graph(route_graph, ["app"]) == []


def test_a_candidate_with_no_file_is_skipped() -> None:
    route_graph = {
        "_repo_endpoint_candidates": {
            "app": [EndpointCandidate(method="get", path="/x", handler="h", file="", repo="app")],
        },
    }
    assert entrypoints_from_route_graph(route_graph, ["app"]) == []


def test_missing_repo_candidates_key_does_not_raise() -> None:
    assert entrypoints_from_route_graph({}, ["app"]) == []


# --------------------------------------------------------------------------
# merge_entrypoints
# --------------------------------------------------------------------------


def _entry(*, repo="app", file="f.py", symbol="handler", source="cbm", **extra) -> dict:
    base = {
        "repo": repo, "qualified_name": "", "symbol": symbol, "file": file,
        "line": None, "route_method": None, "route_path": None,
        "decorators": "", "signature": "", "source": source,
    }
    base.update(extra)
    return base


def test_existing_backend_entrypoints_are_preserved_verbatim() -> None:
    backend = [_entry(symbol="index", source="cbm")]
    merged = merge_entrypoints(backend, [])
    assert merged == backend
    assert merged is not backend  # a copy, not the same list object


def test_a_new_route_derived_entrypoint_is_added() -> None:
    backend = [_entry(symbol="index", file="a.py")]
    route_derived = [_entry(symbol="createTransfer", file="api/transfer.go", source="route_index")]
    merged = merge_entrypoints(backend, route_derived)
    assert len(merged) == 2
    assert {e["symbol"] for e in merged} == {"index", "createTransfer"}


def test_a_duplicate_repo_file_symbol_is_not_added_twice() -> None:
    backend = [_entry(symbol="createUser", file="api/server.go", source="cbm")]
    # Same (repo, file, symbol) the route-index also found — must not double it.
    route_derived = [_entry(symbol="createUser", file="api/server.go", source="route_index")]
    merged = merge_entrypoints(backend, route_derived)
    assert len(merged) == 1
    assert merged[0]["source"] == "cbm"  # the backend's own entry wins, untouched


def test_decorator_based_frameworks_are_unaffected_when_route_index_finds_nothing_new() -> None:
    """A Python/Flask repo where CBM already reports the decorated handler:
    merging in an empty route-derived list must change nothing."""
    backend = [_entry(symbol="create_user", file="app/routes.py", source="cbm")]
    merged = merge_entrypoints(backend, [])
    assert merged == backend


def test_two_different_files_with_the_same_symbol_name_both_survive() -> None:
    backend = [_entry(symbol="create", file="a.go")]
    route_derived = [_entry(symbol="create", file="b.go", source="route_index")]
    merged = merge_entrypoints(backend, route_derived)
    assert len(merged) == 2
