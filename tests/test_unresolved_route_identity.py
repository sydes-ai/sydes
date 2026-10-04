"""A route whose declared path or container prefix is not a literal was never established:
it is `<unresolved route>`, never `/`. A route that really is the root stays `/`, and a route
composed from literals is unchanged. Checked through every layer that builds or normalizes a
route path: decorator extraction, the route index, route-graph composition and endpoint
identity."""

from __future__ import annotations

from pathlib import Path

from sydes.core.models import (
    UNRESOLVED_ROUTE_PATH,
    CandidateFileRead,
    ReadFileSnippet,
    RepoRef,
)
from sydes.discover.deterministic_routes import extract_deterministic_routes
from sydes.discover.endpoints import _normalize_path, _normalize_path_identity
from sydes.discover.route_graph import build_route_graph_facts_from_route_index_batch
from sydes.discover.route_index import build_route_index_batch

SYMBOLIC = """\
@Controller(routesV1.version)
export class DeleteUserHttpController {
  @Delete(routesV1.user.delete)
  async deleteUser(@Param('id') id: string): Promise<void> {}
}
"""
ROOT = """\
@Controller()
export class HealthController {
  @Get('/')
  check() {}
}
"""
LITERAL = """\
@Controller('users')
export class UsersController {
  @Get(':id')
  getUser(@Param('id') id: string) {}
}
"""
SYMBOLIC_PATH_ONLY = """\
@Controller('users')
export class UsersController {
  @Delete(routes.user.delete)
  remove() {}
}
"""


def _extracted(text: str) -> set[tuple[str, str, str]]:
    read = CandidateFileRead(
        repo="api", relative_path="src/app.controller.ts", role="source_route_candidate",
        snippet=ReadFileSnippet(repo="api", relative_path="src/app.controller.ts", text=text,
                                line_count=len(text.splitlines()), char_count=len(text)),
    )
    endpoints, _ = extract_deterministic_routes([read])
    return {(e.method, e.path, e.handler) for e in endpoints}


def _composed(tmp_path: Path, text: str) -> set[tuple[str, str, str]]:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.controller.ts").write_text(text)
    index = build_route_index_batch([RepoRef(name="api", root=str(tmp_path))])
    graph = build_route_graph_facts_from_route_index_batch(index)
    return {(e.method, e.path, e.handler) for e in graph["_repo_endpoint_candidates"].get("api", [])}


def test_a_literal_root_route_stays_root(tmp_path: Path) -> None:
    assert ("GET", "/", "check") in _extracted(ROOT)
    assert ("GET", "/", "check") in _composed(tmp_path, ROOT)
    assert _normalize_path_identity("/") == "/"


def test_a_symbolic_route_is_unresolved_never_root(tmp_path: Path) -> None:
    assert _extracted(SYMBOLIC) == {("DELETE", UNRESOLVED_ROUTE_PATH, "deleteUser")}
    assert _composed(tmp_path, SYMBOLIC) == {("DELETE", UNRESOLVED_ROUTE_PATH, "deleteUser")}
    # a symbolic method path under a literal container is just as unestablished
    assert _extracted(SYMBOLIC_PATH_ONLY) == {("DELETE", UNRESOLVED_ROUTE_PATH, "remove")}


def test_no_normalizer_reinterprets_the_marker() -> None:
    assert UNRESOLVED_ROUTE_PATH != "/"
    assert _normalize_path(UNRESOLVED_ROUTE_PATH) == UNRESOLVED_ROUTE_PATH
    assert _normalize_path_identity(UNRESOLVED_ROUTE_PATH) == UNRESOLVED_ROUTE_PATH


def test_a_route_composed_from_literals_is_unchanged(tmp_path: Path) -> None:
    assert ("GET", "/users/{id}", "getUser") in _extracted(LITERAL)
    assert ("GET", "/users/{id}", "getUser") in _composed(tmp_path, LITERAL)
