"""Tests for bridging a call through a Java field-injected dependency to its
declared type's method -- the Java counterpart of `member_call_bridge.py`,
built for the real spring-boot-demo#2 case where `MonitorController.
kickoutOnlineUser()` calls `monitorService.kickout(names)` (a field declared
`@Autowired private MonitorService monitorService;`), and no backend/mode
ever produces a CALLS edge for it: `_resolve_call`'s import-based resolution
has no way to connect the instance-variable name `monitorService` to the
`MonitorService` import's local alias.
"""

from __future__ import annotations

from pathlib import Path

from sydes.discover.java_field_bridge import (
    JAVA_FIELD_BRIDGE_SOURCE,
    bridge_java_field_call_edges,
)

REPO = "app"


def _class_file(
    class_name: str, field_decls: list[str], methods: dict[str, str],
) -> tuple[str, list[dict]]:
    """A minimal Java class with the given field declaration lines and named
    methods, whose bodies are the given text. Returns (file_text, symbols)
    with correctly computed `start_line`/`end_line` for the class and every
    method -- the same shape `_symbols_for` produces."""
    lines = [f"public class {class_name} {{"]
    symbols: list[dict] = []

    for decl in field_decls:
        lines.append(f"    {decl}")

    for method_name, body in methods.items():
        lines.append(f"    public void {method_name}() {{")
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
    return bridge_java_field_call_edges(symbol_index, [])


def test_field_injected_call_without_this_prefix_is_bridged(tmp_path: Path) -> None:
    """1. Idiomatic Java field access omits `this.` -- the common shape
    `@Autowired private MonitorService monitorService;` + `monitorService.
    kickout(names);` must bridge without it."""
    controller = _class_file(
        "MonitorController",
        ["@Autowired", "private MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "redisUtil.delete(names);"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })

    assert len(bridged) == 1
    edge = bridged[0]
    assert edge["caller_file"] == "controller/MonitorController.java"
    assert edge["caller_symbol"] == "kickoutOnlineUser"
    assert edge["callee_file"] == "service/MonitorService.java"
    assert edge["callee_symbol"] == "kickout"
    assert edge["callee_qualified_name"] == "MonitorService.kickout"
    assert edge["source"] == JAVA_FIELD_BRIDGE_SOURCE
    assert edge["bridge_receiver"] == "monitorService"
    assert edge["bridge_receiver_type"] == "MonitorService"
    assert edge["bridge_target_method"] == "kickout"


def test_this_qualified_call_is_also_bridged(tmp_path: Path) -> None:
    """2. `this.monitorService.kickout(...)` is recognized the same as the
    bare form."""
    controller = _class_file(
        "MonitorController",
        ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": "this.monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "MonitorService.kickout"


def test_constructor_injected_final_field_is_bridged(tmp_path: Path) -> None:
    """3. `private final MonitorService monitorService;` (constructor
    injection, the modern Spring-recommended idiom) is recognized the same
    as an `@Autowired` field."""
    controller = _class_file(
        "MonitorController",
        ["private final MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "MonitorService.kickout"


def test_multiple_dependencies_only_bridge_the_one_actually_called(tmp_path: Path) -> None:
    """4. A class with two injected fields bridges only the one the method
    body actually calls through."""
    controller = _class_file(
        "MonitorController",
        ["@Autowired", "private MonitorService monitorService;",
         "@Autowired", "private UserService userService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    monitor_service = _class_file("MonitorService", [], {"kickout": "return;"})
    user_service = _class_file("UserService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": monitor_service,
        "service/UserService.java": user_service,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_file"] == "service/MonitorService.java"


def test_same_method_name_across_two_service_classes_disambiguates_by_receiver_type(
    tmp_path: Path,
) -> None:
    """5. Two services sharing a bare method name -- the receiver's declared
    type must pick the right one."""
    controller = _class_file(
        "MonitorController",
        ["private MonitorService monitorService;", "private UserService userService;"],
        {
            "kickoutOnlineUser": "monitorService.kickout(names);",
            "removeUser": "userService.kickout(id);",
        },
    )
    monitor_service = _class_file("MonitorService", [], {"kickout": "return;"})
    user_service = _class_file("UserService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": monitor_service,
        "service/UserService.java": user_service,
    })
    by_caller = {edge["caller_symbol"]: edge for edge in bridged}
    assert len(bridged) == 2
    assert by_caller["kickoutOnlineUser"]["callee_qualified_name"] == "MonitorService.kickout"
    assert by_caller["removeUser"]["callee_qualified_name"] == "UserService.kickout"


def test_ambiguous_type_name_across_modules_is_not_bridged(tmp_path: Path) -> None:
    """6. Two unrelated classes named `MonitorService` in different
    packages/modules -- no single class to resolve to, so no bridge."""
    controller = _class_file(
        "MonitorController", ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service_a = _class_file("MonitorService", [], {"kickout": "return;"})
    service_b = _class_file("MonitorService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "moduleA/MonitorService.java": service_a,
        "moduleB/MonitorService.java": service_b,
    })
    assert bridged == []


def test_local_variable_is_not_mistaken_for_a_field(tmp_path: Path) -> None:
    """7. A local variable with no access modifier is never a field
    declaration in Java -- `MonitorService monitorService = ...;` inside a
    method body must not be bridged."""
    controller = _class_file(
        "MonitorController", [],
        {"kickoutOnlineUser": "MonitorService monitorService = get(); monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    assert bridged == []


def test_call_site_only_inside_a_string_or_comment_is_not_bridged(tmp_path: Path) -> None:
    """8. Literal text `monitorService.kickout(` inside a string or a
    comment is not a real call site."""
    controller = _class_file(
        "MonitorController", ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": '// monitorService.kickout(names);\nString note = "monitorService.kickout(";'},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    assert bridged == []


def test_unrelated_class_with_same_method_name_is_not_contaminated(tmp_path: Path) -> None:
    """9. An unrelated class also defining `kickout` (not the declared
    receiver type) must never be the bridge target."""
    controller = _class_file(
        "MonitorController", ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    unrelated = _class_file("UnrelatedThing", [], {"kickout": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
        "other/UnrelatedThing.java": unrelated,
    })
    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "MonitorService.kickout"


def test_missing_target_method_is_not_bridged(tmp_path: Path) -> None:
    """10. The declared type exists but has no method of the called name."""
    controller = _class_file(
        "MonitorController", ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"onlineUser": "return;"})
    bridged = _bridge(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    assert bridged == []


def test_already_native_edge_is_not_duplicated(tmp_path: Path) -> None:
    controller = _class_file(
        "MonitorController", ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    symbol_index = _write_symbol_index(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    native_edges = [{
        "caller_file": "controller/MonitorController.java", "caller_symbol": "kickoutOnlineUser",
        "callee_file": "service/MonitorService.java", "callee_symbol": "kickout",
    }]
    assert bridge_java_field_call_edges(symbol_index, native_edges) == []


def test_no_java_files_short_circuits_cleanly() -> None:
    assert bridge_java_field_call_edges({}, []) == []


def test_bridged_edge_uses_canonical_qualified_name_when_the_target_symbol_has_one(
    tmp_path: Path,
) -> None:
    controller = _class_file(
        "MonitorController", ["private MonitorService monitorService;"],
        {"kickoutOnlineUser": "monitorService.kickout(names);"},
    )
    service = _class_file("MonitorService", [], {"kickout": "return;"})
    symbol_index = _write_symbol_index(tmp_path, {
        "controller/MonitorController.java": controller,
        "service/MonitorService.java": service,
    })
    for file_item in symbol_index["repos"][0]["files"]:
        if file_item["path"] == "service/MonitorService.java":
            for symbol in file_item["symbols"]:
                if symbol["name"] == "kickout":
                    symbol["cbm_qualified_name"] = "com.example.service.MonitorService.kickout"

    bridged = bridge_java_field_call_edges(symbol_index, [])

    assert len(bridged) == 1
    assert bridged[0]["callee_qualified_name"] == "com.example.service.MonitorService.kickout"


def test_real_monitor_controller_shape_is_bridged(tmp_path: Path) -> None:
    """Regression for the real spring-boot-demo#2 case: the controller has
    several field declarations (some annotated, some not) and the actual
    call site is preceded by validation branches in the same method body --
    the call site must still be found and bridged."""
    controller_text = "\n".join([
        "package com.xkcoding.rbac.security.controller;",
        "",
        "@RestController",
        "public class MonitorController {",
        "    @Autowired",
        "    private MonitorService monitorService;",
        "",
        "    @DeleteMapping(\"/online/user/kickout\")",
        "    public ApiResponse kickoutOnlineUser(@RequestBody List<String> names) {",
        "        if (CollUtil.isEmpty(names)) {",
        "            throw new SecurityException(Status.PARAM_NOT_NULL);",
        "        }",
        "        monitorService.kickout(names);",
        "        return ApiResponse.ofSuccess();",
        "    }",
        "}",
        "",
    ])
    controller_symbols = [
        {"name": "MonitorController", "kind": "class", "start_line": 4, "end_line": 15},
        {
            "name": "kickoutOnlineUser", "kind": "class_method", "parent": "MonitorController",
            "qualified_name": "MonitorController.kickoutOnlineUser",
            "start_line": 9, "end_line": 14,
        },
    ]
    service = _class_file("MonitorService", [], {"kickout": "return;"})

    tmp_path.joinpath("controller").mkdir()
    tmp_path.joinpath("controller/MonitorController.java").write_text(controller_text, encoding="utf-8")
    symbol_index = _write_symbol_index(tmp_path, {"service/MonitorService.java": service})
    symbol_index["repos"][0]["files"].append({
        "path": "controller/MonitorController.java",
        "symbols": [{**s, "file": "controller/MonitorController.java"} for s in controller_symbols],
    })

    bridged = bridge_java_field_call_edges(symbol_index, [])
    assert len(bridged) == 1
    assert bridged[0]["caller_symbol"] == "kickoutOnlineUser"
    assert bridged[0]["callee_qualified_name"] == "MonitorService.kickout"
