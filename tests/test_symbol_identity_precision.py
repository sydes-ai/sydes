"""Symbol-identity precision: a false PROVEN impact is worse than an
unresolved one.

RS-S-01 (Rocket) exposed this concretely: a changed symbol named `new`
resolved, via bare-name matching with no file/module context, to unrelated
functions also named `new` in completely different example crates —
producing "PROVEN" impacts for code that never called the change at all.
A milder form of the same defect inflated JAVA-S-01's "proven behaviors"
with unrelated same-named methods even though its genuine flow was
already correct.

Three call sites collapsed identity to a bare name with no safeguard once
file-scoped narrowing came up empty or ambiguous:

- `_FactIndex.entrypoints_named` (impact/interpreter.py) — DIRECT_ENTRYPOINT
  lookup, which used to fall back to every same-named entrypoint repo-wide
  when none existed in the changed symbol's own file. The fix still allows
  a fallback, but only when the bare name is globally unique repo-wide —
  needed for Go's handler-by-reference route registration, whose reported
  entrypoint `file` is the registration site, not the handler's own
  definition site.
- `_match_endpoint_candidate` (verify/analyzer.py) — flow construction,
  which tried route method+path *before* file, so an extremely common
  REST shape like `DELETE /{id}` (reused across unrelated services) could
  win over the entrypoint's own known file.
- `resolve_trace_target` (discover/target_match.py) — the same
  method+path-first shortcut, reachable from the same flow-construction
  call site once it started passing a `file` hint.
- `_FactIndex.entrypoints_referencing` / `entrypoints_with_signature_reference`
  (DECORATOR_REFERENCE / SIGNATURE_REFERENCE) — a live JAVA-S-01 run found
  a fourth site: these scan every entrypoint's captured decorator/signature
  *text* for a changed symbol's bare name, and a route path is a string
  literal (`@PostMapping("/save")`), not a symbol reference — a changed
  method literally named `save` matched every unrelated controller whose
  route happens to be `/save`. A genuine reference is always a bare
  identifier (`Depends(Guard([AdminOnlyPermission]))`, a Rust handler's own
  `PasteId<'_>` parameter type), never inside quotes, so excluding
  identifiers found only inside string literals removes the false matches
  without touching real ones.

None of these problems are bare names themselves — `new`/`delete`/`save`
are ordinary, common identifiers in every language here. The fix is never
to ban a name; it is to require that a bare-name match not silently
override a file the caller already, concretely knows.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.core.models import EndpointCandidate
from sydes.discover.target_match import resolve_trace_target
from sydes.impact.interpreter import (
    ENTRYPOINT_HTTP,
    ImpactInterpreter,
    _FactIndex,
    _identifiers_outside_string_literals,
)
from sydes.impact.models import AffectedEntrypoint
from sydes.verify.analyzer import _match_endpoint_candidate

REPO = "app"


def _entry(symbol: str, file: str, **overrides) -> dict:
    base = {
        "repo": REPO, "qualified_name": f"{file}::{symbol}", "symbol": symbol,
        "file": file, "line": 1, "route_method": "GET", "route_path": "/x",
        "decorators": "", "signature": "",
    }
    base.update(overrides)
    return base


def _facts(entrypoints: list[dict]) -> StructuralFacts:
    return StructuralFacts(entrypoints=entrypoints, provides_call_graph=True, backend="cbm")


# --------------------------------------------------------------------------
# 1/3/8. entrypoints_named: DIRECT_ENTRYPOINT must not cross files
# --------------------------------------------------------------------------


def test_same_bare_name_in_two_unrelated_files_is_not_resolved_by_direct_entrypoint() -> None:
    """The exact RS-S-01 shape: a changed symbol named `new`, with no
    entrypoint of that name in its own file, must not resolve to an
    unrelated `new` elsewhere — even though one exists."""
    index = _FactIndex(_facts([
        _entry("new", "crates/json/src/main.rs"),
        _entry("new", "crates/todo/src/main.rs"),
    ]), REPO)
    assert index.entrypoints_named("new", "crates/pastebin/src/paste_id.rs") == []


def test_same_bare_name_in_two_different_modules_is_not_resolved() -> None:
    """Same defect, phrased as module/package separation rather than crate
    separation — the file boundary is what matters, not the language's own
    naming for the enclosing scope."""
    index = _FactIndex(_facts([
        _entry("save", "billing/service.py"),
        _entry("save", "inventory/service.py"),
    ]), REPO)
    assert index.entrypoints_named("save", "auth/service.py") == []


def test_unnarrowed_lookup_is_unchanged_when_no_file_is_known() -> None:
    """The `file=None` path (a caller with no file context at all) keeps its
    existing behavior — this fix narrows a *known* file, it does not make
    every bare-name lookup stricter than callers already rely on."""
    index = _FactIndex(_facts([
        _entry("new", "crates/json/src/main.rs"),
        _entry("new", "crates/todo/src/main.rs"),
    ]), REPO)
    assert len(index.entrypoints_named("new")) == 2


def test_entrypoints_named_still_resolves_the_correct_same_file_entrypoint() -> None:
    """The positive case: a changed symbol that genuinely *is* an
    entrypoint in its own file still resolves — this fix removes an unsafe
    fallback, not the safe, common case it was layered on top of."""
    index = _FactIndex(_facts([
        _entry("new", "crates/json/src/main.rs"),
        _entry("list_items", "app/views.py"),
    ]), REPO)
    found = index.entrypoints_named("list_items", "app/views.py")
    assert len(found) == 1
    assert found[0]["file"] == "app/views.py"


def test_globally_unique_bare_name_still_resolves_across_a_file_mismatch() -> None:
    """Go's handler-by-reference registration (`router.POST("/transfers",
    server.createTransfer)`) reports its entrypoint's `file` as the
    *registration* site (`api/server.go`), not the *definition* site the
    changed symbol is attributed to (`api/transfer.go`) — a real GO-S-01
    regression this fix introduced before this safeguard was added. There
    is no actual ambiguity to protect against here: exactly one entrypoint
    named `createTransfer` exists anywhere in the repo, so the file
    mismatch is a quirk of the route-derived bridge, not a same-name
    collision with something unrelated."""
    index = _FactIndex(_facts([
        _entry("createTransfer", "api/server.go", source="route_index"),
    ]), REPO)
    found = index.entrypoints_named("createTransfer", "api/transfer.go")
    assert len(found) == 1
    assert found[0]["file"] == "api/server.go"


def test_two_files_sharing_a_bare_name_still_declines_even_if_one_is_the_changed_file() -> None:
    """The Rust/Java case must still win when there IS real ambiguity: two
    candidates sharing a bare name in genuinely different files is not
    resolved just because the file lookup came up empty — only a globally
    *unique* bare name is safe to fall back to."""
    index = _FactIndex(_facts([
        _entry("save", "billing/service.py"),
        _entry("save", "inventory/service.py"),
    ]), REPO)
    assert index.entrypoints_named("save", "auth/service.py") == []


def test_direct_entrypoint_end_to_end_does_not_cross_crates() -> None:
    """Full ImpactInterpreter.interpret() reproduction of the RS-S-01
    DIRECT_ENTRYPOINT false positive: two unrelated entrypoints named `new`
    must not become PROVEN impacts of a changed `new` in a third file."""
    result = ImpactInterpreter().interpret(
        [{"name": "new", "file": "crates/pastebin/src/paste_id.rs", "repo": REPO}],
        _facts([
            _entry("new", "crates/json/src/main.rs", route_path="/"),
            _entry("new", "crates/todo/src/main.rs", route_path="/"),
        ]),
        repo=REPO,
    )
    assert result.affected == []


# --------------------------------------------------------------------------
# 3/10. _match_endpoint_candidate: flow construction must not cross files
# --------------------------------------------------------------------------


def test_match_endpoint_candidate_declines_a_different_file_even_with_one_candidate() -> None:
    """The exact RS-S-01 flow-construction bug: a known entrypoint file
    (pastebin's own `main.rs`) contradicts the only method+path candidate
    discovered (an unrelated crate's route sharing `DELETE /{id}`, a common
    generic REST shape) — this is contradicting evidence, not safe
    uniqueness, and must not be treated as a match."""
    entrypoint = AffectedEntrypoint(
        repo=REPO, symbol="delete", qualified_name="",
        file="examples/pastebin/src/main.rs", kind=ENTRYPOINT_HTTP,
        route_method="DELETE", route_path="/{id}",
    )
    candidates = [
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/databases/src/diesel_mysql.rs", repo=REPO),
    ]
    assert _match_endpoint_candidate(entrypoint, candidates) is None


def test_match_endpoint_candidate_resolves_the_real_same_file_route() -> None:
    """The positive case: when the true candidate is actually discoverable,
    an exact file+handler match still wins immediately."""
    entrypoint = AffectedEntrypoint(
        repo=REPO, symbol="delete", qualified_name="",
        file="examples/pastebin/src/main.rs", kind=ENTRYPOINT_HTTP,
        route_method="DELETE", route_path="/{id}",
    )
    candidates = [
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/databases/src/diesel_mysql.rs", repo=REPO),
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/pastebin/src/main.rs", repo=REPO),
    ]
    match = _match_endpoint_candidate(entrypoint, candidates)
    assert match is not None
    assert match.file == "examples/pastebin/src/main.rs"


def test_match_endpoint_candidate_declines_when_still_ambiguous_within_the_same_file() -> None:
    """Two candidates sharing both the known file and the method+path is a
    genuinely different, unresolved kind of ambiguity — still not a case
    to guess on."""
    entrypoint = AffectedEntrypoint(
        repo=REPO, symbol="handler", qualified_name="",
        file="app/routes.py", kind=ENTRYPOINT_HTTP,
        route_method="GET", route_path="/x",
    )
    candidates = [
        EndpointCandidate(method="GET", path="/x", handler="handler_a", file="app/routes.py", repo=REPO),
        EndpointCandidate(method="GET", path="/x", handler="handler_b", file="app/routes.py", repo=REPO),
    ]
    assert _match_endpoint_candidate(entrypoint, candidates) is None


def test_match_endpoint_candidate_falls_back_to_method_path_with_no_known_file() -> None:
    """An entrypoint with no file at all (never observed in practice today,
    but the model allows it) keeps the old unambiguous-single-match
    fallback — nothing to compare against, so a lone candidate still wins."""
    entrypoint = AffectedEntrypoint(
        repo=REPO, symbol="handler", qualified_name="", file="",
        kind=ENTRYPOINT_HTTP, route_method="GET", route_path="/x",
    )
    candidates = [
        EndpointCandidate(method="GET", path="/x", handler="handler", file="app/routes.py", repo=REPO),
    ]
    match = _match_endpoint_candidate(entrypoint, candidates)
    assert match is not None
    assert match.file == "app/routes.py"


# --------------------------------------------------------------------------
# 10. resolve_trace_target: the same file-hint precision, for flow tracing
# --------------------------------------------------------------------------


def test_resolve_trace_target_declines_a_contradicting_file_hint() -> None:
    candidates = [
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/databases/src/diesel_mysql.rs", repo=REPO),
    ]
    result = resolve_trace_target(
        candidates, path="/{id}", method="DELETE", file="examples/pastebin/src/main.rs",
    )
    assert result.selected is None


def test_resolve_trace_target_still_resolves_a_matching_file_hint() -> None:
    candidates = [
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/databases/src/diesel_mysql.rs", repo=REPO),
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/pastebin/src/main.rs", repo=REPO),
    ]
    result = resolve_trace_target(
        candidates, path="/{id}", method="DELETE", file="examples/pastebin/src/main.rs",
    )
    assert result.selected is not None
    assert result.selected.file == "examples/pastebin/src/main.rs"


def test_resolve_trace_target_unchanged_when_no_file_hint_is_given() -> None:
    """Every existing caller (e.g. `sydes trace`'s user-facing lookup, which
    has no concrete file to compare against) keeps its exact prior
    ambiguous-match behavior — this fix only tightens the one call site
    that now supplies a file."""
    candidates = [
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/databases/src/diesel_mysql.rs", repo=REPO, confidence=0.9),
        EndpointCandidate(method="DELETE", path="/{id}", handler="delete",
                          file="examples/pastebin/src/main.rs", repo=REPO, confidence=0.5),
    ]
    result = resolve_trace_target(candidates, path="/{id}", method="DELETE")
    assert result.selected is not None
    assert result.selected.file == "examples/databases/src/diesel_mysql.rs"
    assert "Multiple candidate endpoints matched target" in " ".join(result.notes)


# --------------------------------------------------------------------------
# 7. Python: unrelated modules sharing a function name
# --------------------------------------------------------------------------


def test_python_function_name_duplicated_in_unrelated_modules_produces_no_false_path() -> None:
    result = ImpactInterpreter().interpret(
        [{"name": "validate", "file": "billing/rules.py", "repo": REPO}],
        _facts([
            _entry("validate", "inventory/api.py", route_path="/inventory"),
            _entry("validate", "auth/api.py", route_path="/auth"),
        ]),
        repo=REPO,
    )
    assert result.affected == []


# --------------------------------------------------------------------------
# 9. Same-file call *with* enough context still resolves — the fix must
# only remove the unsafe fallback, not reachability itself.
# --------------------------------------------------------------------------


def test_same_file_call_with_a_real_call_edge_still_resolves() -> None:
    """If the backend had reported the real `upload -> PasteId::new` call
    edge, CALL_REACHABILITY would already find it — RS-S-01's remaining gap
    is that CBM never reports this edge at all (a call-graph *coverage*
    gap), not that Sydes' own matching is now too strict to use it."""
    result = ImpactInterpreter().interpret(
        [{"name": "new", "file": "examples/pastebin/src/paste_id.rs",
          "qualified_name": "PasteId.new", "repo": REPO}],
        StructuralFacts(
            entrypoints=[_entry("upload", "examples/pastebin/src/main.rs",
                                 route_method="POST", route_path="/",
                                 qualified_name="main.upload")],
            call_edges=[{
                "repo": REPO,
                "caller_file": "examples/pastebin/src/main.rs", "caller_symbol": "upload",
                "caller_qualified_name": "main.upload", "caller_line": 21,
                "callee_file": "examples/pastebin/src/paste_id.rs", "callee_symbol": "new",
                "callee_qualified_name": "PasteId.new", "callee_line": 22,
            }],
            provides_call_graph=True, backend="cbm",
        ),
        repo=REPO,
    )
    assert len(result.affected) == 1
    assert result.affected[0].file == "examples/pastebin/src/main.rs"
    assert result.affected[0].symbol == "upload"
    assert result.affected[0].status == "proven"


# --------------------------------------------------------------------------
# DECORATOR_REFERENCE / SIGNATURE_REFERENCE: route-path string literals
# must not be mistaken for symbol references
# --------------------------------------------------------------------------


def test_identifiers_outside_string_literals_drops_a_route_path_match() -> None:
    """The exact JAVA-S-01 shape: `save` recurs as a URL path segment, not
    as a symbol reference — a plain identifier scan can't tell the
    difference, but stripping quoted text first can. `PostMapping` (the
    annotation name, a bare identifier) is correctly kept; only `save`
    (inside the quoted path) is dropped."""
    assert _identifiers_outside_string_literals('@PostMapping("/save")') == ["PostMapping"]


def test_identifiers_outside_string_literals_keeps_a_bare_dependency_reference() -> None:
    """The legitimate case this must not break: a dependency named directly,
    never inside quotes."""
    tokens = _identifiers_outside_string_literals(
        "@router.put(dependencies=[Depends(Guard([AdminOnlyPermission]))])"
    )
    assert "AdminOnlyPermission" in tokens


def test_identifiers_outside_string_literals_keeps_a_bare_type_in_a_signature() -> None:
    """RS-S-01's genuine signal: a Rust handler's own parameter type, never
    quoted."""
    assert "PasteId" in _identifiers_outside_string_literals("(id: PasteId<'_>)")


def test_decorator_reference_ignores_a_same_named_route_path_elsewhere() -> None:
    """End-to-end: a changed method named `save` must not connect to an
    unrelated controller merely because that controller's own route path
    happens to be `/save` — that is a URL segment, not a reference to this
    method."""
    result = ImpactInterpreter().interpret(
        [{"name": "save", "file": "app/service/impl/UserServiceImpl.java",
          "qualified_name": "UserServiceImpl.save", "repo": REPO}],
        _facts([
            _entry("createUser", "app/service/impl/SysUserServiceImpl.java",
                    route_method=None, route_path=None,
                    decorators='@PostMapping("/save")'),
        ]),
    )
    assert result.affected == []


def test_signature_reference_still_resolves_a_genuine_bare_type() -> None:
    """The positive case: a real handler parameter type, never quoted,
    still resolves via SIGNATURE_REFERENCE."""
    result = ImpactInterpreter().interpret(
        [{"name": "new", "file": "examples/pastebin/src/paste_id.rs",
          "qualified_name": "PasteId.new", "repo": REPO}],
        _facts([
            _entry("delete", "examples/pastebin/src/main.rs",
                    route_method="DELETE", route_path="/{id}",
                    signature="(id: PasteId<'_>)", decorators='#[delete("/<id>")]'),
        ]),
    )
    assert [item.label for item in result.affected] == ["DELETE /{id}"]


# --------------------------------------------------------------------------
# CALL_REACHABILITY: a short `Class.method` qualified name must not collide
# across unrelated modules that happen to reuse the same class name
# --------------------------------------------------------------------------


def test_call_reachability_does_not_cross_unrelated_modules_sharing_a_class_name() -> None:
    """A live JAVA-S-01 run exposed this: `demo-orm-jdbctemplate`'s
    `UserServiceImpl.save` and `demo-cache-redis`'s own, unrelated
    `UserServiceImpl.save` share the exact same short qualified name
    (Sydes' own `Class.method` convention). A caller of the *other*
    module's `save` must not be treated as reaching the changed one just
    because both normalize to the same bare qualified name."""
    result = ImpactInterpreter().interpret(
        [{"name": "save", "file": "demo-orm-jdbctemplate/service/impl/UserServiceImpl.java",
          "qualified_name": "UserServiceImpl.save", "repo": REPO}],
        StructuralFacts(
            entrypoints=[
                _entry("save", "demo-orm-jdbctemplate/controller/UserController.java",
                       route_method="POST", route_path="/user",
                       qualified_name="UserController.save"),
                _entry("createUser", "demo-cache-redis/controller/UserController.java",
                       route_method="POST", route_path="/redis/user",
                       qualified_name="UserController.createUser"),
            ],
            call_edges=[
                {
                    "repo": REPO,
                    "caller_file": "demo-orm-jdbctemplate/controller/UserController.java",
                    "caller_symbol": "save", "caller_qualified_name": "UserController.save",
                    "callee_file": "demo-orm-jdbctemplate/service/impl/UserServiceImpl.java",
                    "callee_symbol": "save", "callee_qualified_name": "UserServiceImpl.save",
                },
                {
                    "repo": REPO,
                    "caller_file": "demo-cache-redis/controller/UserController.java",
                    "caller_symbol": "createUser", "caller_qualified_name": "UserController.createUser",
                    "callee_file": "demo-cache-redis/service/impl/UserServiceImpl.java",
                    "callee_symbol": "save", "callee_qualified_name": "UserServiceImpl.save",
                },
            ],
            provides_call_graph=True, backend="cbm",
        ),
        repo=REPO,
    )
    labels = {item.label for item in result.affected}
    assert labels == {"POST /user"}
    assert "POST /redis/user" not in labels


def test_call_reachability_still_resolves_when_the_qualified_name_is_unique() -> None:
    """The positive case this must not break: a short qualified name that
    genuinely maps to only one file still resolves normally."""
    result = ImpactInterpreter().interpret(
        [{"name": "save", "file": "demo-orm-jdbctemplate/service/impl/UserServiceImpl.java",
          "qualified_name": "UserServiceImpl.save", "repo": REPO}],
        StructuralFacts(
            entrypoints=[
                _entry("save", "demo-orm-jdbctemplate/controller/UserController.java",
                       route_method="POST", route_path="/user",
                       qualified_name="UserController.save"),
            ],
            call_edges=[{
                "repo": REPO,
                "caller_file": "demo-orm-jdbctemplate/controller/UserController.java",
                "caller_symbol": "save", "caller_qualified_name": "UserController.save",
                "callee_file": "demo-orm-jdbctemplate/service/impl/UserServiceImpl.java",
                "callee_symbol": "save", "callee_qualified_name": "UserServiceImpl.save",
            }],
            provides_call_graph=True, backend="cbm",
        ),
        repo=REPO,
    )
    assert [item.label for item in result.affected] == ["POST /user"]
