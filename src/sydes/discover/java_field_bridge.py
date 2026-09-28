"""Bridge a call through a Java field-injected dependency to its declared
type's method.

`monitorService.kickout(names)` in a Spring `@RestController` whose class
declares `@Autowired private MonitorService monitorService;` (or the
constructor-injected `private final MonitorService monitorService;`
equivalent) never produces a `CALLS` edge from the native backend's own
text-based resolution: `_resolve_call` in `call_follower.py` only resolves a
member call's receiver (`monitorService`) against an *import*'s local alias
(`import ... MonitorService;`, local name `MonitorService`) -- the field's
own instance-variable name is a different identifier entirely and is never
looked up anywhere. `member_call_bridge.py` solves the equivalent TypeScript
problem (`this.petService.create(...)` via a constructor parameter
property) but is scoped to `.ts/.tsx/.js/.jsx` files and a syntax Java does
not have; this is its Java-specific counterpart, same discipline.

This adds a synthetic edge alongside whatever the backend reported -- never
replacing it -- from the calling method straight to the declared type's
method, but only when every piece is unambiguous: exactly one class named
the declared field type exists in the symbol index, and that class defines
exactly one method with the called name. More than one of either is a
genuine "don't guess" case, the same reasoning `interface_bridge.py` and
`member_call_bridge.py` both apply.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: Marks a synthetic edge as bridged rather than backend-reported, so a
#: reader (or a future dedup pass) can always tell the two apart -- the same
#: convention `member_call_bridge.MEMBER_CALL_BRIDGE_SOURCE` already uses.
JAVA_FIELD_BRIDGE_SOURCE = "java_field_bridge"

_STRING_OR_COMMENT_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'"
    r"|\"(?:[^\"\\]|\\.)*\""
    r"|//[^\n]*"
    r"|/\*.*?\*/",
    re.DOTALL,
)

#: A field declaration: an access modifier is what distinguishes a genuine
#: field from a local variable, which Java forbids from carrying one -- so
#: this never matches inside a method body. The type is deliberately
#: restricted to a single bare identifier before any `<...>` generic
#: parameters; a union/array/wildcard type needs real type reasoning this
#: bridge does not attempt, the same restriction `member_call_bridge.py`
#: applies to its own constructor-parameter-property type.
_FIELD_DECLARATION_RE = re.compile(
    r"\b(?:private|protected|public)\s+(?:final\s+)?(?:static\s+)?"
    r"(?P<type>[A-Za-z_]\w*)(?:<[^;=]*>)?\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:=[^;]*)?;"
)

#: A member call through a field, with or without an explicit `this.` --
#: unlike TypeScript, idiomatic Java omits `this.` for an unambiguous field
#: access, so both `monitorService.kickout(` and `this.monitorService.kickout(`
#: must match. Whether `field` is actually a known field (not a local
#: variable or another class's static member) is checked by the caller
#: against the class's own extracted field set, never guessed here.
_MEMBER_CALL_RE = re.compile(
    r"\b(?:this\.)?(?P<field>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)\s*\("
)


def _clean_source(text: str) -> str:
    """`text` with every string/comment blanked to the same length, so line
    numbers and surrounding structure survive."""
    return _STRING_OR_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)


def _read_span(root: Path, file_path: str, start_line: int | None, end_line: int | None) -> str | None:
    if not start_line or not end_line or end_line < start_line:
        return None
    try:
        lines = (root / file_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return "\n".join(lines[max(start_line - 1, 0):end_line])


def _field_types(class_source: str) -> dict[str, str]:
    """`{field_name: declared_type}` for every field declaration directly in
    `class_source` (a whole class's own span, including its methods --
    method bodies cannot declare a modifier-carrying field, so this is safe
    without first stripping method bodies out)."""
    fields: dict[str, str] = {}
    for match in _FIELD_DECLARATION_RE.finditer(_clean_source(class_source)):
        fields[match.group("name")] = match.group("type")
    return fields


def _member_call_sites(method_source: str) -> list[tuple[str, str]]:
    """`[(field, method)]` for every `[this.]<field>.<method>(` call site in
    `method_source`, in source order, duplicates kept (the caller dedupes by
    the resolved target, not by call site)."""
    cleaned = _clean_source(method_source)
    return [
        (match.group("field"), match.group("method"))
        for match in _MEMBER_CALL_RE.finditer(cleaned)
    ]


def bridge_java_field_call_edges(
    symbol_index: dict[str, Any], call_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Synthetic edges to append to `call_edges` -- never a replacement for
    any of them. Returns only the new edges; the caller decides how to merge
    (mirrors `member_call_bridge.bridge_member_call_edges`'s split between
    computing and merging).

    Only `.java` files are considered -- this pattern (field injection, a
    bare or `this.`-qualified `field.method()`) is specific to Java's field
    declaration syntax. `call_edges` is consulted only to avoid bridging a
    (caller, callee) pair the backend already reported natively -- never to
    change which pairs are considered.
    """
    already_native = {
        (edge.get("caller_file"), edge.get("caller_symbol"), edge.get("callee_file"), edge.get("callee_symbol"))
        for edge in call_edges
    }
    bridged: list[dict[str, Any]] = []
    for repo_payload in symbol_index.get("repos") or []:
        if not isinstance(repo_payload, dict):
            continue
        repo_name = str(repo_payload.get("repo") or "")
        root_value = repo_payload.get("root")
        root = Path(root_value) if root_value else None
        if root is None:
            continue

        # Repo-wide indexes, built once per repo rather than per call site.
        classes_by_name: dict[str, list[dict[str, Any]]] = {}
        methods_by_class: dict[str, dict[str, list[dict[str, Any]]]] = {}
        callers: list[dict[str, Any]] = []

        for file_item in repo_payload.get("files") or []:
            if not isinstance(file_item, dict):
                continue
            file_path = str(file_item.get("path") or "")
            if Path(file_path).suffix.lower() != ".java":
                continue
            for symbol in file_item.get("symbols") or []:
                if not isinstance(symbol, dict):
                    continue
                symbol = {**symbol, "file": file_path}
                if symbol.get("kind") == "class":
                    classes_by_name.setdefault(str(symbol.get("name") or ""), []).append(symbol)
                elif symbol.get("kind") == "class_method":
                    parent = str(symbol.get("parent") or "")
                    name = str(symbol.get("name") or "")
                    methods_by_class.setdefault(parent, {}).setdefault(name, []).append(symbol)
                    callers.append(symbol)

        for class_name, class_symbols in classes_by_name.items():
            class_symbol = class_symbols[0] if len(class_symbols) == 1 else None
            if class_symbol is None:
                # More than one class with this name (across files) is the
                # same "don't guess" rule this bridge applies everywhere
                # else -- which concrete class's fields to read is itself
                # ambiguous.
                continue
            class_source = _read_span(
                root, str(class_symbol.get("file") or ""),
                class_symbol.get("start_line"), class_symbol.get("end_line"),
            )
            if not class_source:
                continue
            fields = _field_types(class_source)
            if not fields:
                continue
            for caller in callers:
                if str(caller.get("parent") or "") != class_name:
                    continue
                caller_source = _read_span(
                    root, str(caller.get("file") or ""), caller.get("start_line"), caller.get("end_line"),
                )
                if not caller_source:
                    continue
                seen_targets: set[str] = set()
                for field_name, method_name in _member_call_sites(caller_source):
                    declared_type = fields.get(field_name)
                    if not declared_type:
                        # Not a known field of this class -- a local
                        # variable, parameter, or another class's static
                        # member. Left alone rather than guessed at.
                        continue
                    target_classes = classes_by_name.get(declared_type) or []
                    if len(target_classes) != 1:
                        continue
                    target_methods = (methods_by_class.get(declared_type) or {}).get(method_name) or []
                    if len(target_methods) != 1:
                        continue
                    target = target_methods[0]
                    native_key = (caller.get("file"), caller.get("name"), target.get("file"), target.get("name"))
                    if native_key in already_native:
                        continue
                    dedup_key = f"{caller.get('file')}::{caller.get('name')}->{target.get('file')}::{target.get('name')}"
                    if dedup_key in seen_targets:
                        continue
                    seen_targets.add(dedup_key)
                    bridged.append({
                        "repo": repo_name,
                        "caller_file": caller.get("file"),
                        "caller_symbol": caller.get("name"),
                        "caller_qualified_name": (
                            caller.get("cbm_qualified_name")
                            or caller.get("qualified_name") or caller.get("name")
                        ),
                        "caller_line": caller.get("start_line"),
                        "callee_file": target.get("file"),
                        "callee_symbol": target.get("name"),
                        "callee_qualified_name": (
                            target.get("cbm_qualified_name")
                            or target.get("qualified_name") or target.get("name")
                        ),
                        "callee_line": target.get("start_line"),
                        "source": JAVA_FIELD_BRIDGE_SOURCE,
                        "bridge_reason": "field_injected_member_call",
                        "bridge_receiver": field_name,
                        "bridge_receiver_type": declared_type,
                        "bridge_target_method": method_name,
                    })
    return bridged
