"""Bridge a call through a typed, constructor-injected instance field to its
declared type's method.

`this.petService.create(pet)` in a TypeScript class whose constructor
declares `private petService: PetService` never produces a `CALLS` edge from
any backend tested (CBM's own type-aware tier resolves same-file calls only —
see the calibration report this bridge was built from). Every fact this
needs, though, already exists in Sydes' own structural data: the
constructor's parameter-property declaration names both the field
(`petService`) and its type (`PetService`), and the calling method's own
source, already read for other structural facts, contains the literal call
site.

This adds a synthetic edge alongside whatever the backend reported — never
replacing it — from the calling method straight to the declared type's
method, but only when every piece is unambiguous: exactly one class named
the declared type exists in the symbol index, and that class defines exactly
one method with the called name. More than one of either is a genuine
"don't guess" case, the same reasoning `interface_bridge.py` applies to an
interface with more than one implementation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: Marks a synthetic edge as bridged rather than backend-reported, so a
#: reader (or a future dedup pass) can always tell the two apart — the same
#: convention `interface_bridge.INTERFACE_BRIDGE_SOURCE` already uses.
MEMBER_CALL_BRIDGE_SOURCE = "member_call_bridge"

#: TypeScript string/template literals and comments — never a real call
#: site. Stripped before matching so a route path or a stale comment
#: mentioning `this.petService.create(` can't be mistaken for one; a real
#: call is always a bare, unquoted expression.
_STRING_OR_COMMENT_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'"
    r"|\"(?:[^\"\\]|\\.)*\""
    r"|`(?:[^`\\]|\\.)*`"
    r"|//[^\n]*"
    r"|/\*.*?\*/",
    re.DOTALL,
)

#: A TypeScript constructor parameter property: an access modifier and/or
#: `readonly` is what makes a constructor parameter *also* declare a class
#: field of the same name (`constructor(private petService: PetService)`) —
#: a bare `constructor(petService: PetService)` with no modifier is only a
#: parameter, creates no field, and must not match here. The type is
#: deliberately restricted to a single bare identifier: generics, unions and
#: array types need real type reasoning this bridge does not attempt.
_PARAMETER_PROPERTY_RE = re.compile(
    r"(?:@[A-Za-z_$][\w$]*(?:\([^()]*\))?\s+)*"
    r"(?:(?:public|private|protected)(?:\s+readonly)?|readonly(?:\s+(?:public|private|protected))?)"
    r"\s+(?P<name>[A-Za-z_$][\w$]*)\s*:\s*(?P<type>[A-Za-z_$][\w$]*)\b"
)

#: A direct member call through `this`: `this.petService.create(` or the
#: optional-chained `this.petService?.create(`. Both identifiers are matched
#: whole (`\w+`), so a computed/indexed call (`this.petService[method]()`)
#: never matches — there is no field or method name to read there at all.
_THIS_MEMBER_CALL_RE = re.compile(
    r"\bthis\.(?P<field>[A-Za-z_$][\w$]*)\??\.(?P<method>[A-Za-z_$][\w$]*)\s*\("
)


def _clean_source(text: str) -> str:
    """`text` with every string/template literal and comment blanked out to
    the same length, so line numbers and surrounding structure survive."""
    return _STRING_OR_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)


def _constructor_parameter_list(source: str) -> str | None:
    """The text between a `constructor`'s own parentheses, balanced-paren
    aware so a parameter decorator's own `(...)` doesn't truncate the scan
    early."""
    match = re.search(r"\bconstructor\s*\(", source)
    if not match:
        return None
    depth = 1
    start = match.end()
    for index in range(start, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start:index]
    return None


def _parameter_property_fields(constructor_source: str) -> dict[str, str]:
    """`{field_name: declared_type}` for every constructor parameter property
    in `constructor_source` — empty if none, or if this isn't a constructor
    at all."""
    params = _constructor_parameter_list(_clean_source(constructor_source))
    if not params:
        return {}
    fields: dict[str, str] = {}
    for match in _PARAMETER_PROPERTY_RE.finditer(params):
        fields[match.group("name")] = match.group("type")
    return fields


def _member_call_sites(method_source: str) -> list[tuple[str, str]]:
    """`[(field, method)]` for every `this.<field>.<method>(` call site in
    `method_source`, in source order, duplicates kept (the caller dedupes by
    the resolved target, not by call site)."""
    cleaned = _clean_source(method_source)
    return [
        (match.group("field"), match.group("method"))
        for match in _THIS_MEMBER_CALL_RE.finditer(cleaned)
    ]


def _read_span(root: Path, file_path: str, start_line: int | None, end_line: int | None) -> str | None:
    if not start_line or not end_line or end_line < start_line:
        return None
    try:
        lines = (root / file_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    # 1-indexed, inclusive — matches every other consumer of CBM line spans.
    return "\n".join(lines[max(start_line - 1, 0):end_line])


def bridge_member_call_edges(
    symbol_index: dict[str, Any], call_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Synthetic edges to append to `call_edges` — never a replacement for
    any of them. Returns only the new edges; the caller decides how to merge
    (mirrors `interface_bridge.bridge_interface_call_edges`'s split between
    computing and merging).

    Only TypeScript/JavaScript files are considered — this pattern
    (parameter-property injection, `this.field.method()`) is specific to
    that constructor-property syntax. `call_edges` is consulted only to
    avoid bridging a (caller, callee) pair the backend already reported
    natively — never to change which pairs are considered.
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
        constructors: list[dict[str, Any]] = []
        callers: list[dict[str, Any]] = []

        for file_item in repo_payload.get("files") or []:
            if not isinstance(file_item, dict):
                continue
            file_path = str(file_item.get("path") or "")
            if Path(file_path).suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"}:
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
                    if name == "constructor":
                        constructors.append(symbol)
                    else:
                        methods_by_class.setdefault(parent, {}).setdefault(name, []).append(symbol)
                        callers.append(symbol)

        for ctor in constructors:
            ctor_source = _read_span(
                root, str(ctor.get("file") or ""), ctor.get("start_line"), ctor.get("end_line"),
            )
            if not ctor_source:
                continue
            fields = _parameter_property_fields(ctor_source)
            if not fields:
                continue
            class_name = str(ctor.get("parent") or "")
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
                        continue
                    target_classes = classes_by_name.get(declared_type) or []
                    if len(target_classes) != 1:
                        # Absent, or ambiguous across files/modules — the
                        # same "don't guess" rule interface_bridge.py uses.
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
                        "caller_qualified_name": caller.get("qualified_name") or caller.get("name"),
                        "caller_line": caller.get("start_line"),
                        "callee_file": target.get("file"),
                        "callee_symbol": target.get("name"),
                        "callee_qualified_name": target.get("qualified_name") or target.get("name"),
                        "callee_line": target.get("start_line"),
                        "source": MEMBER_CALL_BRIDGE_SOURCE,
                        "bridge_reason": "constructor_typed_member_call",
                        "bridge_receiver": field_name,
                        "bridge_receiver_type": declared_type,
                        "bridge_target_method": method_name,
                    })
    return bridged
