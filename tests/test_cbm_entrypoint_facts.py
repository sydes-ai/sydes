"""Existing CBM entrypoint facts (HANDLES -> Route) reach the report.

A route registered as a method value (`router.POST("/users/login", server.loginUser)`) seeds
structural enrichment with the handler's symbol, so CBM's HANDLES fact is consumed and the
method/path survives; an RPC entrypoint discovered without its service/method gets them from
the RPC Route CBM already holds; a changed function a route container applies while
registering its handlers becomes an unresolved candidate, never an edge. No framework rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sydes.code_intelligence.cbm_facts import TRACK_A_RELATIONS, CBMFacts
from sydes.core.models import CandidateFileRead, EndpointCandidate, ReadFileSnippet
from sydes.discover.deterministic_routes import extract_deterministic_routes
from sydes.discover.structural_enrichment import (
    CATEGORY_ROUTE_CONTAINER_REGISTRATION,
    PROVENANCE_CBM_HANDLES,
    EnrichmentInput,
    enrich,
)
from sydes.report.verify_terminal import _framework_boundary_blocks
from sydes.verify.analyzer import _attach_rpc_identities
from sydes.verify.models import ChangeSet, ChangeVerificationResult

P = "proj"
SETUP, LOGIN, LIST = f"{P}.api.setupRouter", f"{P}.api.Server.loginUser", f"{P}.api.Server.listAccounts"
AUTH, TEST_FN = f"{P}.api.authMiddleware", f"{P}.api.TestAuthMiddleware"


def _rel(source: str, label: str, file: str, kind: str, target: str, tlabel: str, tfile: str, **props: Any) -> list[str]:
    return [source, label, file, kind, target, tlabel, tfile, json.dumps(props)]


class FakeClient:
    def __init__(self, relations: list[list[str]], handled: list[list[str]] | None = None) -> None:
        self.rows, self.handled = relations, handled or []
        self.calls: list[str] = []

    def graph_capabilities(self, project: str) -> dict[str, Any]:
        self.calls.append("schema")
        return {"edge_types": {t: 1 for t in TRACK_A_RELATIONS}}

    def relations(self, project: str, seeds: list[str], types: list[str]) -> list[list[str]]:
        self.calls.append("relations")
        return [r for r in self.rows if r[3] in types and (r[0] in seeds or r[4] in seeds)]

    def handled_routes(self, project: str, names: list[str]) -> list[list[str]]:
        self.calls.append("handles")
        return [r for r in self.handled if r[1] in names]

    def direct_relations(self, *a: Any, **k: Any) -> list[list[str]]:
        self.calls.append("direct")
        return []

    def calls_paths(self, *a: Any, **k: Any) -> list[list[str]]:
        self.calls.append("calls_path")
        return []

    def search_code(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.calls.append("search")
        return {"rows": [], "files": [], "truncated": False}


def _sym(name: str, kind: str, start: int, parent: str | None = None) -> dict[str, Any]:
    return {"name": name, "kind": kind, "start_line": start, "end_line": start + 5, "parent": parent,
            "cbm_qualified_name": f"{P}.api.{parent + '.' if parent else ''}{name}"}


DECL = 'router.POST("/users/login", server.loginUser)'


def _go_case(tmp_path: Path, *, handles: bool = True, container_routes: bool = True) -> tuple[Any, FakeClient]:
    (tmp_path / "api").mkdir(exist_ok=True)
    (tmp_path / "api" / "server.go").write_text(f"func (server *Server) setupRouter() {{\n\t{DECL}\n}}\n")
    index = {"repos": [{"repo": "app", "files": [
        {"path": "api/server.go", "symbols": [_sym("setupRouter", "method", 1, "Server") | {"cbm_qualified_name": SETUP}]},
        {"path": "api/user.go", "symbols": [_sym("loginUser", "method", 88, "Server")]},
        {"path": "api/account.go", "symbols": [_sym("listAccounts", "method", 70, "Server")]},
        {"path": "api/middleware.go", "symbols": [_sym("authMiddleware", "function", 20)]},
        {"path": "api/middleware_test.go", "symbols": [_sym("TestAuthMiddleware", "function", 10)]},
    ]}]}
    route_index = {"repos": [{"repo": "app", "files": [{"path": "api/server.go", "mount_calls": [], "route_calls": [
        {"receiver": "router", "method": "post", "handler_hint": "server.loginUser", "path": "/users/login",
         "line": 2, "snippet": DECL}]}]}]}
    relations = [
        _rel(SETUP, "Method", "api/server.go", "CALLS", AUTH, "Function", "api/middleware.go", line=50),
        _rel(f"{P}.api.TestAuthMiddleware", "Function", "api/middleware_test.go", "CALLS", AUTH, "Function",
             "api/middleware.go", line=12),
    ]
    if handles:
        relations += [_rel(LOGIN, "Method", "api/user.go", "HANDLES", "__route__POST__/users/login", "Route", "")]
    if container_routes:
        relations += [
            _rel(SETUP, "Method", "api/server.go", "USAGE", LOGIN, "Method", "api/user.go"),
            _rel(SETUP, "Method", "api/server.go", "USAGE", LIST, "Method", "api/account.go"),
            _rel(LIST, "Method", "api/account.go", "HANDLES", "__route__GET__/accounts", "Route", ""),
        ]
    ctx = EnrichmentInput(
        repo="app", repo_root=tmp_path,
        unresolved=[{"name": "authMiddleware", "file": "api/middleware.go"},
                    {"name": "TestAuthMiddleware", "file": "api/middleware_test.go"}],
        reached_entrypoints=[], decorated=[], symbol_index=index, route_index=route_index,
        route_flows=[{"method": "POST", "path": "/users/login", "handler": "server.loginUser",
                      "handler_file": "api/user.go", "route_file": "api/server.go"}],
    )
    client = FakeClient(relations)
    return enrich(ctx, CBMFacts(client, P)), client


# ----------------------------------------------------------------------------- HTTP: HANDLES consumed


def test_a_method_value_handler_is_extracted_with_its_reference() -> None:
    text = "\n".join([DECL, "router.get('/a', auth, controller.list)", "router.post('/b', async (req, res) => {})"])
    read = CandidateFileRead(repo="app", relative_path="api/server.go", role="source_route_candidate",
                             snippet=ReadFileSnippet(repo="app", relative_path="api/server.go", text=text,
                                                     line_count=3, char_count=len(text)))
    endpoints, _ = extract_deterministic_routes([read])
    handlers = {(e.method, e.path): e.handler for e in endpoints}
    # the handler is the last argument (middleware precedes it), never the receiver `server`
    assert handlers[("POST", "/users/login")] == "server.loginUser"
    assert handlers[("GET", "/a")] == "controller.list"


def test_handles_is_consumed_and_the_method_and_path_survive(tmp_path: Path) -> None:
    result, client = _go_case(tmp_path)
    route = next(c for c in result.candidates if c.category == "route_registration")
    assert route.via == "POST /users/login" and route.status == "resolved"
    assert ("cbm_handles", "CBM: loginUser HANDLES POST /users/login") in {(f.provenance, f.fact) for f in route.facts}
    # the declaration is found in the router file, not only where the handler is defined
    declaration = next(f for f in route.facts if " is registered on " in f.fact)
    assert (declaration.file, declaration.line) == ("api/server.go", 2)
    assert "HANDLES" in result.families and client.calls.count("relations") >= 1


def test_without_handles_the_route_says_so_and_nothing_is_inferred(tmp_path: Path) -> None:
    result, _ = _go_case(tmp_path, handles=False, container_routes=False)
    route = next(c for c in result.candidates if c.category == "route_registration")
    assert any(f.fact == "CBM has no HANDLES relation for loginUser" for f in route.facts)
    assert all(f.provenance != "ai_inferred" for c in result.candidates for f in c.facts)


# ----------------------------------------------------------------------------- route-container registration


def test_a_changed_function_a_route_container_applies_is_a_candidate_never_an_edge(tmp_path: Path) -> None:
    result, client = _go_case(tmp_path)
    [container] = [c for c in result.candidates if c.category == CATEGORY_ROUTE_CONTAINER_REGISTRATION]
    assert container.status == "unresolved" and container.resolved_by is None
    assert container.source_symbol == "api.setupRouter" and container.via == "registers authMiddleware (line 50)"
    assert container.targets == ["GET /accounts", "POST /users/login"]
    assert {f.provenance for f in container.facts} == {"cbm_call", PROVENANCE_CBM_HANDLES}
    assert container.missing and "framework-mediated" in container.missing[0]
    # no lifecycle call is fabricated: the only CALLS fact is the one CBM holds (line 50)
    assert [f.fact for f in container.facts if " calls " in f.fact] == ["api.setupRouter calls authMiddleware [line 50]"]
    # the changed test function triggers nothing, and only the families that apply are asked
    assert "route_container_registration: 1 unresolved changed symbol(s)" in result.triggered
    assert not any(t.startswith(("message_dispatch", "callback_registration")) for t in result.triggered)
    assert not {"direct", "calls_path", "search"} & set(client.calls)
    [block] = _framework_boundary_blocks([container.to_dict()])
    assert "  → [framework application unresolved]" in block
    assert "  → candidate routes (api.setupRouter registers them): GET /accounts, POST /users/login" in block


def test_a_caller_that_registers_no_routes_yields_no_candidate(tmp_path: Path) -> None:
    result, _ = _go_case(tmp_path, handles=False, container_routes=False)
    assert not [c for c in result.candidates if c.category == CATEGORY_ROUTE_CONTAINER_REGISTRATION]


# ----------------------------------------------------------------------------- gRPC identity from HANDLES


class _Intelligence:
    def __init__(self, facts: CBMFacts) -> None:
        self._facts = facts

    def facts(self, repo: str) -> CBMFacts:
        return self._facts


def _grpc(handler: str) -> EndpointCandidate:
    return EndpointCandidate(method="GRPC", path=None, handler=handler, file="gapi/x.go", repo="app", kind="grpc")


HANDLED = [
    ["__grpc__pb.SimpleBank/CreateUser", "CreateUser", f"{P}.proto.SimpleBank.CreateUser", "proto/service.proto"],
    ["__grpc__pb.SimpleBank/LoginUser", "LoginUser", f"{P}.proto.SimpleBank.LoginUser", "proto/service.proto"],
    ["__grpc__pb.Admin/LoginUser", "LoginUser", f"{P}.proto.Admin.LoginUser", "proto/admin.proto"],
]


def test_an_rpc_entrypoint_gets_its_service_and_method_from_handles() -> None:
    client = FakeClient([], HANDLED)
    endpoints = [_grpc("Server.CreateUser"), _grpc("Server.LoginUser")]
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    _attach_rpc_identities(endpoints, _Intelligence(CBMFacts(client, P)), "app", result)
    create, login = endpoints
    assert f"{create.method} {create.path}" == "GRPC pb.SimpleBank/CreateUser"  # never "GRPC None"
    assert create.evidence[-1].label == "cbm_handles"
    assert "HANDLES __grpc__pb.SimpleBank/CreateUser" in (create.evidence[-1].snippet or "")
    # two services share the method name: left unresolved, never guessed
    assert login.path is None
    assert "rpc_identities_from_cbm_handles=1/2" in result.diagnostics
    assert client.calls == ["schema", "handles"]  # one batched request


def test_handled_routes_is_capability_gated_and_cached() -> None:
    client = FakeClient([], HANDLED)
    facts = CBMFacts(client, P)
    facts.handled_routes(["CreateUser"])
    facts.handled_routes(["CreateUser"])
    assert client.calls == ["schema", "handles"] and facts.cache_hits["handles"] == 1

    class NoHandles(FakeClient):
        def graph_capabilities(self, project: str) -> dict[str, Any]:
            return {"edge_types": {"CALLS": 1}}

    bare = NoHandles([], HANDLED)
    assert CBMFacts(bare, P).handled_routes(["CreateUser"]) == {"CreateUser": []}
    assert "handles" not in bare.calls


def test_http_endpoints_and_named_rpcs_are_left_alone() -> None:
    client = FakeClient([], HANDLED)
    endpoints = [EndpointCandidate(method="POST", path="/users/login", handler="server.loginUser", file="a.go", repo="app"),
                 EndpointCandidate(method="GRPC", path="pb.SimpleBank/CreateUser", handler="Server.CreateUser",
                                   file="b.go", repo="app", kind="grpc")]
    _attach_rpc_identities(endpoints, _Intelligence(CBMFacts(client, P)), "app",
                           ChangeVerificationResult(change=ChangeSet(base="main")))
    assert client.calls == []


def test_a_boundary_labelled_with_a_raw_qualified_name_renders_its_symbol_and_file() -> None:
    from sydes.report.verify_terminal import _boundary_identity
    from sydes.verify.models import AffectedBoundary

    raw = AffectedBoundary(id="b", kind="api", label="private-tmp-x-simplebank.api.Server.setupRouter",
                           symbol="setupRouter", file="api/server.go")
    assert _boundary_identity(raw) == "setupRouter (api/server.go)"
    named = AffectedBoundary(id="c", kind="api", label="Protected HTTP request authorization", symbol="x")
    assert _boundary_identity(named) == "Protected HTTP request authorization"
