"""An explicit `@RequestMapping` method is read, never replaced by a guessed GET."""

from __future__ import annotations

import pytest

from sydes.core.models import CandidateFileRead, ReadFileSnippet
from sydes.discover.deterministic_routes import (
    UNRESOLVED_METHOD,
    _parse_spring_mapping,
    extract_deterministic_routes,
)


@pytest.mark.parametrize(("annotation", "methods"), [
    ('@RequestMapping(path = "/users", method = RequestMethod.POST)', ["POST"]),
    ('@RequestMapping(path = "/users", method = POST)', ["POST"]),  # statically imported constant
    ('@RequestMapping(value = "/u", method = {RequestMethod.GET, HEAD})', ["GET", "HEAD"]),
    ('@RequestMapping(path = "/u", method = verbs())', [UNRESOLVED_METHOD]),  # explicit, unreadable
    ('@RequestMapping(path = "/u", method = CUSTOM)', [UNRESOLVED_METHOD]),
    ('@RequestMapping("/u")', ["GET"]),  # omitted: unchanged by this fix
    ('@PostMapping("/u")', ["POST"]),
])
def test_request_method_forms(annotation: str, methods: list[str]) -> None:
    assert _parse_spring_mapping(annotation)[0] == methods


SOURCE = """\
package io.example.api;

import static org.springframework.web.bind.annotation.RequestMethod.POST;

@RestController
public class UsersApi {
  @RequestMapping(path = "/users", method = POST)
  public ResponseEntity createUser(@Valid @RequestBody RegisterParam registerParam) {
    return null;
  }

  @RequestMapping(path = "/users/login", method = POST)
  public ResponseEntity userLogin(@Valid @RequestBody LoginParam loginParam) {
    return null;
  }
}
"""


def test_a_statically_imported_method_yields_the_declared_routes() -> None:
    path = "src/main/java/io/example/api/UsersApi.java"
    read = CandidateFileRead(repo="app", relative_path=path, role="source_route_candidate",
                             snippet=ReadFileSnippet(repo="app", relative_path=path, text=SOURCE,
                                                     line_count=SOURCE.count("\n"), char_count=len(SOURCE)))
    endpoints, _ = extract_deterministic_routes([read])
    assert sorted((e.method, e.path, e.handler) for e in endpoints) == [
        ("POST", "/users", "createUser"), ("POST", "/users/login", "userLogin")]
