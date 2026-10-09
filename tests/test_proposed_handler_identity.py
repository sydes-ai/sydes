"""A model-proposed handler is a lookup hint; the repository decides its identity.

Route discovery's model may write the same entrypoint handler as `create_user`,
`UserService.create_user`, a signature, or a decorated/receiver form. Before the handler seeds
anything deterministic it is resolved against the symbol index, strongest evidence first; one
repository symbol -> its canonical form, none or several -> left unresolved, never a first hit.
Every surface form of one handler therefore traces to the same flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.core.models import EndpointCandidate, EvidenceRef, RepoRef
from sydes.trace.handler_resolver import (
    canonicalize_proposed_handler,
    resolve_handler_reference,
)
from sydes.verify.analyzer import (
    VerifyChangeOptions,
    _canonicalize_proposed_handlers,
    _trace_route,
)
from sydes.verify.models import ChangeSet, ChangeVerificationResult

SERVICE = """\
class UserService:
    def create_user(self, request):
        record = normalize(request)
        return save(record)


def normalize(request):
    return request


def save(record):
    return record
"""
QUERIES = """\
class Queries:
    def create_user(self, record):
        return record
"""


def _method(name: str, parent: str, start: int, end: int, file: str) -> dict:
    return {"name": name, "kind": "class_method", "parent": parent, "qualified_name": f"{parent}.{name}",
            "language": "python", "file": file, "line": start, "start_line": start, "end_line": end}


def _function(name: str, start: int, end: int, file: str) -> dict:
    return {"name": name, "kind": "function", "qualified_name": name, "language": "python", "file": file,
            "line": start, "start_line": start, "end_line": end}


INDEX = {"repo": "app", "files": [
    {"path": "svc/users.py", "symbols": [
        {"name": "UserService", "kind": "class", "qualified_name": "UserService", "file": "svc/users.py",
         "start_line": 1, "end_line": 4},
        _method("create_user", "UserService", 2, 4, "svc/users.py"),
        _function("normalize", 7, 8, "svc/users.py"),
        _function("save", 11, 12, "svc/users.py")]},
    {"path": "db/queries.py", "symbols": [_method("create_user", "Queries", 2, 3, "db/queries.py")]},
    {"path": "api/rpc.py", "symbols": []},
]}


def _endpoint(handler: str, file: str = "svc/users.py") -> EndpointCandidate:
    return EndpointCandidate(method="GRPC", path="Users/CreateUser", handler=handler, file=file, repo="app",
                             kind="grpc", confidence=0.9)


@pytest.mark.parametrize("proposed", [
    "create_user",                                    # exact symbol
    "UserService.create_user",                        # qualified symbol
    "UserService::create_user",                       # another qualifier separator
    "*UserService).create_user",                      # receiver-style debris
    "def create_user(self, request: CreateUserRequest) -> CreateUserResponse",  # signature text
])
def test_equivalent_forms_resolve_to_one_canonical_symbol(proposed: str) -> None:
    canonical, how = canonicalize_proposed_handler(_endpoint(proposed), INDEX)
    assert canonical == "create_user" and how.startswith("resolved:")
    primary = resolve_handler_reference(_endpoint(canonical), INDEX)["primary_handler"]["symbol"]
    assert (primary["file"], primary["qualified_name"]) == ("svc/users.py", "UserService.create_user")


def test_strongest_evidence_decides_first() -> None:
    # the qualified name in the endpoint's own file wins before any bare-name level is consulted
    assert canonicalize_proposed_handler(_endpoint("UserService.create_user(normalize)"), INDEX) == (
        "create_user", "resolved:qualified_in_file")


def test_a_name_no_symbol_carries_stays_unresolved() -> None:
    assert canonicalize_proposed_handler(_endpoint("delete_user"), INDEX) == (None, "no_match")


def test_several_matching_symbols_are_ambiguous_never_a_first_hit() -> None:
    # bare name, endpoint file has no symbols: two repository methods share it
    assert canonicalize_proposed_handler(_endpoint("create_user", "api/rpc.py"), INDEX) == (
        None, "ambiguous:name_in_repo")
    # two names that each match a different symbol in the endpoint's file
    assert canonicalize_proposed_handler(_endpoint("normalize or save"), INDEX) == (None, "ambiguous:name_in_file")


def test_a_qualified_name_found_only_elsewhere_resolves_to_its_unique_form() -> None:
    assert canonicalize_proposed_handler(_endpoint("Queries.create_user", "api/rpc.py"), INDEX) == (
        "Queries.create_user", "resolved:qualified_in_repo")
    # debris that forms no qualified reference is not reassembled into one: away from the
    # defining file, the bare name is shared by two methods, so it stays unresolved
    assert canonicalize_proposed_handler(_endpoint("(q *Queries).create_user", "api/rpc.py"), INDEX) == (
        None, "ambiguous:name_in_repo")


def test_only_proposed_endpoints_are_canonicalized_and_unresolved_ones_are_left_as_proposed() -> None:
    deterministic = EndpointCandidate(method="POST", path="/users", handler="server.create_user", file="svc/users.py",
                                      repo="app", status="deterministic")
    noisy, unknown = _endpoint("*UserService).create_user"), _endpoint("delete_user")
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    _canonicalize_proposed_handlers([deterministic, noisy, unknown], {"repos": [INDEX]}, result)
    assert deterministic.handler == "server.create_user"
    assert noisy.handler == "create_user"
    assert noisy.evidence[-1].label == "handler_resolved_from_symbol_index"
    assert "proposed `*UserService).create_user`" in (noisy.evidence[-1].snippet or "")
    assert unknown.handler == "delete_user"
    assert "proposed_handler_unresolved: `delete_user` in svc/users.py (no_match)" in result.diagnostics
    assert "proposed_handlers: no_match=1 resolved=1" in result.diagnostics


def test_every_surface_form_traces_the_same_flow(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "users.py").write_text(SERVICE)
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "queries.py").write_text(QUERIES)
    traces = []
    for proposed in ("create_user", "*UserService).create_user"):
        endpoint = _endpoint(proposed)
        endpoint.evidence = [EvidenceRef(file="svc/users.py", label="model")]
        _canonicalize_proposed_handlers([endpoint], {"repos": [INDEX]},
                                        ChangeVerificationResult(change=ChangeSet(base="main")))
        trace, _notes = _trace_route(endpoint=endpoint, repos=[RepoRef(name="app", root=str(tmp_path))],
                                     handler_index={"repos": [INDEX]}, options=VerifyChangeOptions(llm_policy="never"))
        for part in ("matched_endpoint", "target_route"):  # the proposal's own record differs by design
            for holder in (trace.get("layered_contract", {}), trace.get("layered_expansion", {})):
                (holder.get(part) or {}).pop("evidence", None)
        traces.append(trace)
    assert traces[0] == traces[1]
    # and the flow is real: the handler resolved and its body was followed
    assert "normalize" in str(traces[0]) and "save" in str(traces[0])
