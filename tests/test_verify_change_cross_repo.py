"""Cross-repo impact and unsupported-language fallback tests for verify-change."""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.core.models import RepoRef
from sydes.verify.cross_repo import detect_cross_repo_impacts
from sydes.verify.repo_scan import scan_repository
from sydes.verify.runtime import infer_runtime_dependencies
from sydes.verify.surface import FlowBuilder, build_system_surface
from sydes.verify.symbol_index import build_symbol_index

_SERVICE2 = """package com.example.service2;

@RestController
@RequestMapping("/goodreads")
class BookController {
\tprivate final WebClient client;

\t@GetMapping("/books")
\tFlux<Book> getBooks() {
\t\treturn client.get()
\t\t\t\t.uri("/db/books")
\t\t\t\t.retrieve()
\t\t\t\t.bodyToFlux(Book.class);
\t}
}
"""

_SERVICE1 = """package com.example.service1;

@RestController
@RequestMapping("/db")
class BookController {
\t@GetMapping("/books")
\tFlux<Book> getBooks() { return bookRepository.findAll(); }
}
"""


def _surface(name: str, root: Path):
    scan = scan_repository(name, root)
    index = build_symbol_index(scan)
    surface = build_system_surface(
        repo=RepoRef(name=name, root=str(root)), scan=scan, index=index, events=[]
    )
    return scan, index, surface


@pytest.fixture()
def two_services(tmp_path: Path) -> tuple[Path, Path]:
    """Two Spring services where service2 calls service1 over HTTP."""
    two = tmp_path / "service2" / "src" / "main" / "java"
    one = tmp_path / "service1" / "src" / "main" / "java"
    two.mkdir(parents=True)
    one.mkdir(parents=True)
    (two / "Service2Application.java").write_text(_SERVICE2, encoding="utf-8")
    (one / "Service1Application.java").write_text(_SERVICE1, encoding="utf-8")
    return tmp_path / "service2", tmp_path / "service1"


def test_unsupported_language_falls_back_to_the_enclosing_route(two_services) -> None:
    """A Java change with no symbol index still resolves to its route."""
    service2, _ = two_services
    scan, index, surface = _surface("service2", service2)

    builder = FlowBuilder(index=index, surface=surface, scan=scan, events=[])
    flows = builder.build([], set(), {"src/main/java/Service2Application.java": [(9, 12)]})

    assert [flow.entry_label for flow in flows] == ["GET /goodreads/books"]
    assert "symbol_index_unavailable_for_language" in flows[0].notes


def test_fluent_client_chain_names_the_outbound_path(two_services) -> None:
    """The path on a later chain line identifies the outbound call target."""
    service2, _ = two_services
    scan, index, surface = _surface("service2", service2)

    builder = FlowBuilder(index=index, surface=surface, scan=scan, events=[])
    flows = builder.build([], set(), {"src/main/java/Service2Application.java": [(9, 12)]})
    clients = [node.name for node in flows[0].nodes if node.kind == "client"]

    assert clients == ["HTTP /db/books"]


def test_outbound_call_matches_a_sibling_repository_route(two_services) -> None:
    """With both repos configured, the outbound call resolves to the owning repo."""
    service2, service1 = two_services
    scan2, index2, surface2 = _surface("service2", service2)
    _, _, surface1 = _surface("service1", service1)

    builder = FlowBuilder(index=index2, surface=surface2, scan=scan2, events=[])
    flows = builder.build([], set(), {"src/main/java/Service2Application.java": [(9, 12)]})
    impacts = detect_cross_repo_impacts(
        origin_repo="service2",
        flows=flows,
        surfaces={"service2": surface2, "service1": surface1},
    )

    assert [item.target_repo for item in impacts] == ["service1"]
    assert impacts[0].status == "verified"
    assert impacts[0].target_label == "service1::GET /db/books"


def test_outbound_call_without_a_configured_repo_stays_unresolved(two_services) -> None:
    """A single-repo run still represents the boundary, marked unresolved."""
    service2, _ = two_services
    scan, index, surface = _surface("service2", service2)

    builder = FlowBuilder(index=index, surface=surface, scan=scan, events=[])
    flows = builder.build([], set(), {"src/main/java/Service2Application.java": [(9, 12)]})
    impacts = detect_cross_repo_impacts(
        origin_repo="service2", flows=flows, surfaces={"service2": surface}
    )

    assert [item.target_repo for item in impacts] == [None]
    assert impacts[0].status == "unknown"


def test_cross_repo_target_becomes_a_runtime_requirement(two_services) -> None:
    """A service the flow calls must be running to exercise the change."""
    service2, service1 = two_services
    scan2, index2, surface2 = _surface("service2", service2)
    _, _, surface1 = _surface("service1", service1)

    builder = FlowBuilder(index=index2, surface=surface2, scan=scan2, events=[])
    flows = builder.build([], set(), {"src/main/java/Service2Application.java": [(9, 12)]})
    impacts = detect_cross_repo_impacts(
        origin_repo="service2",
        flows=flows,
        surfaces={"service2": surface2, "service1": surface1},
    )
    dependencies = infer_runtime_dependencies(
        scan=scan2, flows=flows, changed_files=set(), cross_repo_impacts=impacts
    )

    assert [item.name for item in dependencies] == ["service1 service"]
    assert dependencies[0].kind == "http_service"
