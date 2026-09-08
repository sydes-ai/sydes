"""Tests for bridging a call through a TypeScript constructor-injected
field to its declared type's method — the generic fix for TS-S-02
(express-typescript-boilerplate), where `PetController.create()` calls
`this.petService.create(pet)` (a field typed via a constructor parameter
property), and no backend/mode tested ever produces a CALLS edge for it —
its type-aware resolution only covers same-file calls (see the calibration
report this bridge was built from).
"""

from __future__ import annotations

from pathlib import Path

from sydes.discover.member_call_bridge import (
    MEMBER_CALL_BRIDGE_SOURCE,
    bridge_member_call_edges,
)

REPO = "app"


def _class_file(
    class_name: str, ctor_params: str, methods: dict[str, str],
) -> tuple[str, list[dict]]:
    """A minimal TS class with one constructor and named methods, whose
    bodies are the given text (each already a call/statement, no further
    indentation needed). Returns (file_text, symbols) with correctly
    computed `start_line`/`end_line` for the class, its constructor, and
    every method — the same shape `_symbols_for` produces."""
    lines = [f"export class {class_name} {{"]
    symbols: list[dict] = []

    lines.append(f"    constructor({ctor_params}) {{")
    ctor_start = len(lines)
    lines.append("    }")
    ctor_end = len(lines)
    symbols.append({
        "name": "constructor", "kind": "class_method", "parent": class_name,
        "qualified_name": f"{class_name}.constructor",
        "start_line": ctor_start, "end_line": ctor_end,
    })

    for method_name, body in methods.items():
        lines.append(f"    {method_name}() {{")
        method_start = len(lines)
        for body_line in body.splitlines():
            lines.append(f"        {body_line}")
        lines.append("    }")
        method_end = len(lines)
        symbols.append({
            "name": method_name, "kind": "class_method", "parent": class_name,
            "qualified_name": f"{class_name}.{method_name}",
            "start_line": method_start, "end_line": method_end,
        })

    lines.append("}")
    class_end = len(lines)
    symbols.insert(0, {
        "name": class_name, "kind": "class",
        "start_line": 1, "end_line": class_end,
    })
    return "\n".join(lines) + "\n", symbols


def _write_symbol_index(
    tmp_path: Path, classes: dict[str, tuple[str, list[dict]]],
) -> dict:
    """`classes`: {relative_path: (file_text, symbols)}. Writes each file
    under `tmp_path` and returns the `symbol_index` shape
    `bridge_member_call_edges` reads."""
    files = []
    for relative_path, (text, symbols) in classes.items():
        full_path = tmp_path / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(text, encoding="utf-8")
        files.append({
            "path": relative_path,
            "symbols": [{**symbol, "file": relative_path} for symbol in symbols],
        })
    return {"repos": [{"repo": REPO, "root": str(tmp_path), "files": files}]}


def _bridge(tmp_path: Path, classes: dict[str, tuple[str, list[dict]]]) -> list[dict]:
    symbol_index = _write_symbol_index(tmp_path, classes)
    return bridge_member_call_edges(symbol_index, [])


def test_basic_constructor_parameter_property_is_bridged(tmp_path: Path) -> None:
    """1. `private petService: PetService` + `this.petService.create()`
    bridges to `PetService.create`."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })

    assert len(bridged) == 1
    edge = bridged[0]
    assert edge["caller_file"] == "controller/PetController.ts"
    assert edge["caller_symbol"] == "create"
    assert edge["callee_file"] == "service/PetService.ts"
    assert edge["callee_symbol"] == "create"
    assert edge["callee_qualified_name"] == "PetService.create"
    assert edge["source"] == MEMBER_CALL_BRIDGE_SOURCE
    assert edge["bridge_receiver"] == "petService"
    assert edge["bridge_receiver_type"] == "PetService"
    assert edge["bridge_target_method"] == "create"


def test_readonly_parameter_property_is_bridged(tmp_path: Path) -> None:
    """2. `private readonly petService: PetService` is recognized the same
    as `private petService: PetService`."""
    controller = _class_file(
        "PetController", "private readonly petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "PetService.create"


def test_multiple_dependencies_only_bridge_the_one_actually_called(tmp_path: Path) -> None:
    """3. A constructor with two typed dependencies bridges only the one the
    method body actually calls through."""
    controller = _class_file(
        "PetController",
        "private petService: PetService, private userService: UserService",
        {"create": "return this.petService.create(pet);"},
    )
    pet_service = _class_file("PetService", "", {"create": "return pet;"})
    user_service = _class_file("UserService", "", {"create": "return user;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": pet_service,
        "service/UserService.ts": user_service,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_file"] == "service/PetService.ts"
    assert bridged[0]["callee_qualified_name"] == "PetService.create"


def test_same_method_name_across_two_service_classes_disambiguates_by_receiver_type(
    tmp_path: Path,
) -> None:
    """4. `PetService.create` and `UserService.create` share a bare method
    name — the receiver's declared type must pick the right one."""
    controller = _class_file(
        "PetController",
        "private petService: PetService, private userService: UserService",
        {
            "create": "return this.petService.create(pet);",
            "createUser": "return this.userService.create(user);",
        },
    )
    pet_service = _class_file("PetService", "", {"create": "return pet;"})
    user_service = _class_file("UserService", "", {"create": "return user;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": pet_service,
        "service/UserService.ts": user_service,
    })
    by_caller = {edge["caller_symbol"]: edge for edge in bridged}
    assert len(bridged) == 2
    assert by_caller["create"]["callee_qualified_name"] == "PetService.create"
    assert by_caller["createUser"]["callee_qualified_name"] == "UserService.create"


def test_ambiguous_type_name_across_modules_is_not_bridged(tmp_path: Path) -> None:
    """5. Two unrelated classes named `PetService` in different modules —
    the declared type does not resolve to a single class, so no bridge is
    made rather than guessing."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service_a = _class_file("PetService", "", {"create": "return pet;"})
    service_b = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "moduleA/PetService.ts": service_a,
        "moduleB/PetService.ts": service_b,
    })
    assert bridged == []


def test_missing_type_annotation_is_not_bridged(tmp_path: Path) -> None:
    """6. A bare constructor parameter with no access modifier declares no
    class field in TypeScript at all — must not be treated as one."""
    controller = _class_file(
        "PetController", "petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert bridged == []


def test_dynamic_computed_call_is_not_bridged(tmp_path: Path) -> None:
    """7. `this.petService[method]()` is a computed member call — there is
    no method name to read, so it must not match."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService[method](pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert bridged == []


def test_local_variable_call_without_a_proven_field_is_not_bridged(tmp_path: Path) -> None:
    """8. `petService.create()` (no `this.`) is not proven to be the
    constructor-injected field at all — must not be bridged."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "const petService = getPetService(); return petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert bridged == []


def test_call_site_only_inside_a_string_or_comment_is_not_bridged(tmp_path: Path) -> None:
    """9. Literal text `this.petService.create(` inside a string or a
    comment is not a real call site."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": '// this.petService.create(pet);\nconst note = "this.petService.create(";\nreturn null;'},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert bridged == []


def test_unrelated_class_with_same_method_name_is_not_contaminated(tmp_path: Path) -> None:
    """10. An unrelated class also defining `create` (not the declared
    receiver type) must never be the bridge target."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    unrelated = _class_file("UnrelatedThing", "", {"create": "return null;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
        "other/UnrelatedThing.ts": unrelated,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "PetService.create"


def test_missing_target_method_is_not_bridged(tmp_path: Path) -> None:
    """11. The declared type exists but has no method of the called name —
    nothing to bridge to."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"find": "return null;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert bridged == []


def test_optional_chained_call_is_bridged(tmp_path: Path) -> None:
    """12. `this.petService?.create()` — optional chaining does not change
    which method is being called, so it is explicitly supported the same
    as the plain form."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService?.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    bridged = _bridge(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "PetService.create"


def test_already_native_edge_is_not_duplicated(tmp_path: Path) -> None:
    """A (caller, callee) pair the backend already reported natively is not
    bridged a second time."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    symbol_index = _write_symbol_index(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    native_edges = [{
        "caller_file": "controller/PetController.ts", "caller_symbol": "create",
        "callee_file": "service/PetService.ts", "callee_symbol": "create",
    }]
    assert bridge_member_call_edges(symbol_index, native_edges) == []


def test_no_typescript_files_short_circuits_cleanly() -> None:
    assert bridge_member_call_edges({}, []) == []


def test_bridged_edge_uses_canonical_qualified_name_when_the_target_symbol_has_one(
    tmp_path: Path,
) -> None:
    """A changed symbol's identity now prefers CBM's own canonical qualified
    name over Sydes' short `Class.method` form whenever one is known (see
    `SymbolIdentity.canonical_qualified_name`) — a bridged edge that still
    carried only the short form would never match a changed symbol reached
    only through it. Locks in the same fix `test_interface_bridge.py`
    verifies for the Java bridge."""
    controller = _class_file(
        "PetController", "private petService: PetService",
        {"create": "return this.petService.create(pet);"},
    )
    service = _class_file("PetService", "", {"create": "return pet;"})
    symbol_index = _write_symbol_index(tmp_path, {
        "controller/PetController.ts": controller,
        "service/PetService.ts": service,
    })
    for file_item in symbol_index["repos"][0]["files"]:
        if file_item["path"] == "service/PetService.ts":
            for symbol in file_item["symbols"]:
                if symbol["name"] == "create":
                    symbol["cbm_qualified_name"] = (
                        "project.service.PetService.PetService.create"
                    )

    bridged = bridge_member_call_edges(symbol_index, [])

    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "project.service.PetService.PetService.create"
