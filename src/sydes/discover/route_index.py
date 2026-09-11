"""Deterministic route index artifact builder.

This index captures compact, route-like structural signals to support later
hierarchical discovery planning. It is not the final discovered route list.
"""

from __future__ import annotations

import re
import os
from collections import Counter
from pathlib import Path
from typing import Any

from sydes.core.models import RepoRef
from sydes.discover.deterministic_routes import (
    _SPRING_METHOD_RE,
    _SPRING_STATEMENT_KEYWORDS,
    _TS_CLASS_RE,
    _TS_CONTAINER_DECORATORS,
    _TS_METHOD_RE,
    _TS_STATEMENT_KEYWORDS,
    _TS_VERB_DECORATORS,
    _decorator_name,
    _decorator_path_arg,
    _parse_spring_mapping,
)
from sydes.discover.repo_map import IGNORED_DIRS, build_repo_map
from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    classify_candidate_file_role,
)

SUPPORTED_EXTS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".java",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".kt",
    ".rs",
}

LANGUAGE_BY_EXT = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".rs": "rust",
    ".kt": "kotlin",
}

_ROUTE_METHODS = "get|post|put|patch|delete|head|options|all"
# A declaration's own path may be empty, meaning "the container's prefix and
# nothing more". Only `/...` and the exactly-empty literal qualify, so ordinary
# string arguments are not mistaken for paths.
_ROUTE_CALL_RE = re.compile(
    rf"(?P<receiver>[A-Za-z_][\w\.]*)\s*\.\s*(?P<method>{_ROUTE_METHODS})\s*\(\s*['\"](?P<path>/[^'\"]*|)['\"]",
    re.IGNORECASE,
)
_MOUNT_CALL_RE = re.compile(
    r"(?P<receiver>[A-Za-z_][\w\.]*)\s*\.\s*use\s*\(\s*['\"](?P<prefix>/[^'\"]*)['\"]\s*,\s*(?P<args>[^\)]*)\)",
    re.IGNORECASE,
)
_ROUTER_DECL_RE = re.compile(
    r"(?:const|let|var)\s+(?P<symbol>[A-Za-z_][\w]*)\s*=\s*(?:[A-Za-z_][\w]*\.)?Router\s*\(\s*\)",
)

# --- Generic route-container / mount vocabulary -----------------------------
#
# A *container* is any object that owns route declarations and can be mounted
# into another container. A *mount* binds a child container into a parent,
# optionally contributing a path prefix.
#
# Recognition is by construct shape, not by library name:
#   - a container is an assignment whose right-hand side is a call that either
#     names a router-like factory, or carries a path-prefix keyword;
#   - a mount is a call on a receiver using one of the mount verbs below.
#
# The verb and factory vocabularies are the framework adapter surface. They feed
# the single shared container/mount representation; they do not create per-
# framework graphs.

CONTAINER_FACTORY_SUFFIXES = ("router", "blueprint", "routegroup", "routergroup", "namespace")
PREFIX_KEYWORDS = ("prefix", "url_prefix", "urlprefix", "path_prefix", "base_path", "basepath")
MOUNT_VERBS = (
    "use",
    "mount",
    "include_router",
    "register_blueprint",
    "add_router",
    "register_router",
    "add_subrouter",
    "add_namespace",
    "nest",
    "mountrouter",
    "userouter",
)

_CONTAINER_DECL_RE = re.compile(
    r"^\s*(?:const|let|var|final)?\s*(?P<symbol>[A-Za-z_]\w*)\s*(?::[^=]+)?=\s*"
    r"(?:await\s+)?(?:new\s+)?(?P<callee>[A-Za-z_][\w.]*)\s*\((?P<args>.*)$"
)
_PREFIX_KWARG_RE = re.compile(
    r"\b(?P<key>" + "|".join(PREFIX_KEYWORDS) + r")\s*[=:]\s*['\"](?P<prefix>/[^'\"]*)['\"]",
    re.IGNORECASE,
)
_MOUNT_VERB_RE = re.compile(
    r"(?P<receiver>[A-Za-z_][\w.]*)\s*\.\s*(?P<verb>"
    + "|".join(MOUNT_VERBS)
    + r")\s*\((?P<args>.*)$",
    re.IGNORECASE,
)
# Some frameworks declare a route with a generic verb and carry the HTTP
# method(s) in an argument instead of the call name. Same shared representation,
# one extra vocabulary entry.
GENERIC_ROUTE_VERBS = ("route", "add_url_rule", "api_route")

_GENERIC_ROUTE_CALL_RE = re.compile(
    r"(?P<receiver>[A-Za-z_][\w.]*)\s*\.\s*(?P<verb>"
    + "|".join(GENERIC_ROUTE_VERBS)
    + r")\s*\(\s*['\"](?P<path>/[^'\"]*|)['\"](?P<rest>.*)$"
)
_METHODS_ARG_RE = re.compile(r"methods\s*[=:]\s*[\[(](?P<methods>[^\])]*)[\])]", re.IGNORECASE)

_PATH_ARG_RE = re.compile(r"^['\"](?P<prefix>/[^'\"]*)['\"]$")
_IDENTIFIER_ARG_RE = re.compile(r"^[A-Za-z_][\w.]*$")

# Python import forms, needed so a mount can resolve the container it names.
_PY_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(?P<source>[.\w]+)\s+import\s+(?P<names>[\w,\s*]+)"
)
_PY_IMPORT_RE = re.compile(
    r"^\s*import\s+(?P<source>[.\w]+)(?:\s+as\s+(?P<alias>\w+))?\s*$"
)
_REQUIRE_IMPORT_RE = re.compile(
    r"(?:const|let|var)\s+(?P<local>[A-Za-z_]\w*)\s*=\s*require\(\s*['\"](?P<source>[^'\"]+)['\"]\s*\)"
)
_IMPORT_DEFAULT_RE = re.compile(
    r"import\s+(?P<local>[A-Za-z_][\w]*)\s+from\s+['\"](?P<source>[^'\"]+)['\"]"
)
_IMPORT_NAMED_RE = re.compile(
    r"import\s*\{(?P<named>[^}]+)\}\s*from\s*['\"](?P<source>[^'\"]+)['\"]"
)
_EXPORT_DEFAULT_RE = re.compile(r"export\s+default\s+(?P<symbol>[A-Za-z_][\w]*)")
_EXPORT_NAMED_RE = re.compile(r"export\s*\{(?P<named>[^}]+)\}")
_MODULE_EXPORT_RE = re.compile(r"module\.exports\s*=\s*(?P<symbol>[A-Za-z_][\w]*)")
_PATH_LITERAL_RE = re.compile(r"['\"](?P<path>/[^'\"\s]*)['\"]")

_MAX_FILE_SIZE = 2_000_000
_MAX_SNIPPET_CHARS = 300
_MAX_PATH_LITERALS_PER_FILE = 100


def _trim(text: str) -> str:
    text = " ".join(text.strip().split())
    if len(text) > _MAX_SNIPPET_CHARS:
        return text[: _MAX_SNIPPET_CHARS - 3] + "..."
    return text


def _path_in_dirs(relative_path: str, preferred_dirs: set[str]) -> bool:
    if not preferred_dirs:
        return True
    for directory in preferred_dirs:
        if relative_path == directory or relative_path.startswith(directory + "/"):
            return True
    return False


def _extract_imports(line: str) -> list[dict[str, str]]:
    imports: list[dict[str, str]] = []
    match = _IMPORT_DEFAULT_RE.search(line)
    if match:
        imports.append({"local": match.group("local"), "source": match.group("source")})
    match = _IMPORT_NAMED_RE.search(line)
    if match:
        source = match.group("source")
        for item in match.group("named").split(","):
            name = item.strip()
            if not name:
                continue
            imports.append({"local": name.split(" as ")[-1].strip(), "source": source})
    return imports


def _extract_exports(line: str) -> list[dict[str, str]]:
    exports: list[dict[str, str]] = []
    match = _EXPORT_DEFAULT_RE.search(line)
    if match:
        exports.append({"kind": "default", "symbol": match.group("symbol")})
    match = _EXPORT_NAMED_RE.search(line)
    if match:
        for item in match.group("named").split(","):
            name = item.strip()
            if not name:
                continue
            exports.append({"kind": "named", "symbol": name.split(" as ")[0].strip()})
    match = _MODULE_EXPORT_RE.search(line)
    if match:
        exports.append({"kind": "commonjs", "symbol": match.group("symbol")})
    return exports


def _join_open_calls(text: str) -> str:
    """Pull continuation lines up into their opening call.

    Container constructions and mount calls are frequently written across
    several lines. Content is moved up rather than removed, so every reported
    line number still points at the construct's first line.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        depth = line.count("(") - line.count(")")
        if depth <= 0 or "(" not in line:
            index += 1
            continue
        cursor = index + 1
        while cursor < len(lines) and depth > 0 and cursor - index <= 8:
            line = line.rstrip() + " " + lines[cursor].strip()
            depth += lines[cursor].count("(") - lines[cursor].count(")")
            lines[cursor] = ""
            cursor += 1
        lines[index] = line
        index = cursor if cursor > index else index + 1
    return "\n".join(lines)


def _looks_like_container_factory(callee: str) -> bool:
    """Return True when a callee name reads as a route-container factory."""
    leaf = callee.rsplit(".", 1)[-1].lower()
    return any(leaf.endswith(suffix) for suffix in CONTAINER_FACTORY_SUFFIXES)


def _extract_container_declaration(line: str) -> dict | None:
    """Detect a route container and any prefix it declares for itself."""
    match = _CONTAINER_DECL_RE.match(line)
    if match is None:
        return None
    callee = match.group("callee")
    args = match.group("args")
    prefix_match = _PREFIX_KWARG_RE.search(args)
    prefix = _normalize_prefix(prefix_match.group("prefix")) if prefix_match else ""
    if not _looks_like_container_factory(callee) and not prefix:
        return None
    return {"symbol": match.group("symbol"), "callee": callee, "prefix": prefix}


def _normalize_prefix(prefix: str | None) -> str:
    """Normalize a prefix literal, treating a bare root as no prefix."""
    if not prefix:
        return ""
    value = prefix.strip()
    if not value.startswith("/"):
        value = "/" + value
    value = re.sub(r"/+", "/", value)
    if len(value) > 1 and value.endswith("/"):
        value = value[:-1]
    return "" if value == "/" else value


def _extract_mount_call(line: str) -> dict | None:
    """Detect a mount binding a child container into a parent container."""
    match = _MOUNT_VERB_RE.search(line)
    if match is None:
        return None
    args_text = match.group("args")
    closing = args_text.rfind(")")
    if closing != -1:
        args_text = args_text[:closing]
    args = [item.strip() for item in _split_args(args_text) if item.strip()]

    prefix = ""
    kwarg_match = _PREFIX_KWARG_RE.search(args_text)
    if kwarg_match:
        prefix = _normalize_prefix(kwarg_match.group("prefix"))
    else:
        for arg in args:
            path_match = _PATH_ARG_RE.match(arg)
            if path_match:
                prefix = _normalize_prefix(path_match.group("prefix"))
                break

    child = None
    for arg in reversed(args):
        if _PREFIX_KWARG_RE.match(arg):
            continue
        if _IDENTIFIER_ARG_RE.match(arg):
            child = arg
            break

    if child is None and not prefix:
        return None
    return {"receiver": match.group("receiver"), "verb": match.group("verb"), "prefix": prefix, "child": child}


def _extract_python_imports(line: str) -> list[dict[str, str]]:
    """Extract Python import bindings so mounts can resolve their container."""
    imports: list[dict[str, str]] = []
    from_match = _PY_FROM_IMPORT_RE.match(line)
    if from_match:
        source = from_match.group("source")
        for raw in from_match.group("names").split(","):
            entry = raw.strip()
            if not entry or entry == "*":
                continue
            # `name as alias`: a mount always refers to the receiver by its
            # LOCAL (aliased) name -- `from .routers.x import router as
            # openai_router` is later looked up as `openai_router`, not
            # `router`. A plain `from X import name` has no `as`, so
            # `local` falls back to the name itself.
            imported, _, alias = entry.partition(" as ")
            imported = imported.strip()
            local = alias.strip() if alias else imported
            imports.append({"local": local, "imported": imported, "source": f"{source}.{imported}"})
            imports.append({"local": local, "imported": imported, "source": source})
        return imports
    plain_match = _PY_IMPORT_RE.match(line)
    if plain_match:
        source = plain_match.group("source")
        local = plain_match.group("alias") or source.split(".")[0]
        imports.append({"local": local, "imported": source, "source": source})
    return imports


# A declaration written as a decorator names no handler in its arguments: the
# handler is the callable it precedes. This is a structural relation between a
# declaration and the next declaration below it, independent of framework.
_DECLARED_CALLABLE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function|func|fn|sub)\s+(?P<name>[A-Za-z_]\w*)"
)
_DECORATOR_LINE_RE = re.compile(r"^\s*[@#\[]")
_MAX_DECORATOR_LOOKAHEAD = 12


def _handler_below_declaration(lines: list[str], start_index: int) -> str | None:
    """Find the callable a decorator-style declaration is attached to."""
    for offset in range(start_index, min(start_index + _MAX_DECORATOR_LOOKAHEAD, len(lines))):
        candidate = lines[offset]
        if not candidate.strip():
            continue
        match = _DECLARED_CALLABLE_RE.match(candidate)
        if match:
            return match.group("name")
        if _DECORATOR_LINE_RE.match(candidate):
            continue
        # Any other statement means the declaration was not decorating anything.
        return None
    return None


_TS_CLASS_NAME_RE = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)")
_JAVA_CLASS_NAME_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+)?(?:abstract\s+|final\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"
)
#: Annotations `_extract_spring_routes` (deterministic_routes.py) already
#: recognizes as route-verb declarations. Kept in sync with that function's
#: own check rather than re-derived from `_SPRING_METHOD_RE`/mapping tables,
#: since a route annotation and an ordinary Java annotation share no other
#: distinguishing shape.
_SPRING_ROUTE_ANNOTATIONS = ("@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping", "@PatchMapping", "@RequestMapping")

_JAVA_INTERFACE_NAME_RE = re.compile(r"^\s*(?:public\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)")
_JAVA_IMPLEMENTS_RE = re.compile(r"\bimplements\s+(?P<names>[^{]+)")


def _java_implemented_names(line: str) -> list[str]:
    """Bare interface names from a class declaration's `implements` clause,
    generics stripped (`implements Comparable<Foo>` -> `Comparable`)."""
    match = _JAVA_IMPLEMENTS_RE.search(line)
    if not match:
        return []
    names = []
    for raw in match.group("names").split(","):
        name = re.sub(r"<.*>", "", raw).strip()
        if name:
            names.append(name)
    return names


#: Rocket declares a route as an attribute directly on a free function —
#: `#[get("/<id>")]` above `fn retrieve(...)` — not on a method inside a
#: class/struct the way NestJS/routing-controllers/Spring do above. There is
#: therefore no natural container to look up a path prefix from; the mount
#: prefix a real app applies via `.mount("/prefix", routes![...])` is not
#: composed here (same simplification already made for a bare Spring
#: `@RestController` with no class-level `@RequestMapping`: the attribute's
#: own path is used as the whole path). A route mounted under a non-root
#: prefix will report an incomplete path rather than a wrong file/handler —
#: composing `routes![...]`/`.mount(...)` generically is a larger feature,
#: not this fix.
_ROCKET_ROUTE_ATTR_RE = re.compile(
    r'^\s*#\[\s*(?P<verb>get|post|put|delete|patch|head|options)\s*'
    r'\(\s*(?:path\s*=\s*)?"(?P<path>[^"]*)"',
    re.IGNORECASE,
)
_RUST_FN_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*\("
)
#: A container id shared across every file — it never resolves to a real
#: declared container (Rocket routes have none), so `route_graph.py` falls
#: back to its synthetic-receiver path with an empty prefix, same as an
#: unresolved receiver anywhere else. Scoped by (file, symbol) downstream, so
#: sharing this string across files never collides two different routes.
_ROCKET_SYNTHETIC_RECEIVER = "<rocket_route>"


def _normalize_rocket_path(path: str) -> str:
    """`<id>`/`<file..>` (Rocket's dynamic-segment syntax) -> `{id}`/`{file}`,
    matching the `{name}` convention every other framework's path already
    normalizes to."""
    return re.sub(r"<([A-Za-z_]\w*)\.{0,2}>", r"{\1}", path)


def _extract_index_for_file(relative_path: str, text: str, role: str) -> dict:
    ext = Path(relative_path).suffix.lower()
    language = LANGUAGE_BY_EXT.get(ext, "unknown")

    route_calls: list[dict] = []
    mount_calls: list[dict] = []
    router_symbols: list[str] = []
    containers: list[dict] = []
    imports: list[dict] = []
    exports: list[dict] = []
    path_literals: list[str] = []
    signals: set[str] = set()

    # Decorator-routing frameworks (NestJS, routing-controllers, tsoa) declare
    # routes as class + method decorators rather than as calls on a receiver,
    # so they need their own bit of state carried across lines: which class a
    # decorated method belongs to, and what path prefix that class declared
    # for itself. Each decorated method becomes a `route_calls` entry whose
    # `receiver` is the class name; the class itself becomes a container with
    # that prefix as its `own_prefix`. Both then flow through the same
    # container/mount composition already used for call-based frameworks.
    ts_pending_decorators: list[str] = []
    ts_current_class: str | None = None

    # Spring MVC is the same shape again (class-level `@RequestMapping` names
    # a prefix, method-level `@GetMapping`/etc. names a verb) but with Java
    # annotation/method syntax, so it needs its own parallel state rather
    # than sharing the TS tracking above — `@Controller` alone, for one,
    # means something different in each vocabulary.
    java_pending_annotations: list[str] = []
    java_current_class: str | None = None
    java_class_prefix: str = ""
    #: This file's own top-level type, if a Java class or interface
    #: declaration has been seen — first one wins (nested types are not
    #: modeled). Used to bridge a call to an interface method to its sole
    #: known implementation (see interface_bridge.py); has nothing to do
    #: with route composition above.
    java_type: dict[str, Any] | None = None

    #: Rocket's route attribute is consumed by the very next line (Rust
    #: attributes are not stacked the way TS/Java decorators commonly are;
    #: one route attribute per handler is the norm), so this only ever holds
    #: at most one pending `(verb, path, raw_attribute_line)` tuple rather
    #: than a list.
    rocket_pending_route: tuple[str, str, str] | None = None

    joined_lines = _join_open_calls(text).splitlines()
    for idx, raw_line in enumerate(joined_lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        # `#[...]` is Rust's attribute syntax (`#[get("/<id>")]`), not a
        # comment — Rust itself has no `#`-prefixed comment form, so this
        # can never collide with an actual comment in any supported language.
        if line.startswith("//") or (line.startswith("#") and not line.startswith("#[")):
            continue

        if line.startswith("@"):
            ts_pending_decorators.append(line)

        class_name_match = _TS_CLASS_NAME_RE.match(line)
        if class_name_match:
            ts_current_class = class_name_match.group("name")
            container_ann = next(
                (ann for ann in ts_pending_decorators if _decorator_name(ann) in _TS_CONTAINER_DECORATORS),
                None,
            )
            if container_ann and ts_current_class not in {item["symbol"] for item in containers}:
                prefix = _decorator_path_arg(container_ann) or ""
                containers.append(
                    {"symbol": ts_current_class, "prefix": prefix, "callee": "ts_decorator_controller"},
                )
                router_symbols.append(ts_current_class)
                signals.add("route_container:ts_decorator_controller")
            ts_pending_decorators = []

        ts_method_match = _TS_METHOD_RE.match(line)
        if ts_method_match and ts_method_match.group("name") in _TS_STATEMENT_KEYWORDS:
            ts_method_match = None
        if ts_pending_decorators and ts_method_match and ts_current_class:
            handler = ts_method_match.group("name")
            for ann in ts_pending_decorators:
                verb = _TS_VERB_DECORATORS.get(_decorator_name(ann))
                if not verb:
                    continue
                route_calls.append(
                    {
                        "receiver": ts_current_class,
                        "method": verb.lower(),
                        "path": _decorator_path_arg(ann) or "",
                        "handler_hint": handler,
                        "line": idx,
                        "snippet": _trim(f"{ann} {raw_line.strip()}"),
                    }
                )
                signals.add(f"route_call:{verb.lower()}")
            ts_pending_decorators = []
        elif ts_pending_decorators and not line.startswith("@"):
            ts_pending_decorators = []

        if line.startswith("@"):
            java_pending_annotations.append(line)

        if java_type is None:
            java_interface_match = _JAVA_INTERFACE_NAME_RE.match(line)
            if java_interface_match:
                java_type = {"kind": "interface", "name": java_interface_match.group("name"), "implements": []}

        java_class_match = _JAVA_CLASS_NAME_RE.match(line)
        if java_class_match:
            java_current_class = java_class_match.group("name")
            if java_type is None:
                java_type = {
                    "kind": "class",
                    "name": java_current_class,
                    "implements": _java_implemented_names(line),
                }
            class_ann = next(
                (ann for ann in java_pending_annotations if ann.startswith("@RequestMapping")), None,
            )
            if class_ann and java_current_class not in {item["symbol"] for item in containers}:
                _, prefix = _parse_spring_mapping(class_ann)
                java_class_prefix = _normalize_prefix(prefix)
                containers.append(
                    {"symbol": java_current_class, "prefix": java_class_prefix, "callee": "spring_controller"},
                )
                router_symbols.append(java_current_class)
                signals.add("route_container:spring_controller")
            java_pending_annotations = []

        java_method_match = _SPRING_METHOD_RE.match(line)
        if java_method_match and java_method_match.group("type") in _SPRING_STATEMENT_KEYWORDS:
            java_method_match = None
        if java_pending_annotations and java_method_match and java_current_class:
            handler = java_method_match.group("name")
            for ann in java_pending_annotations:
                if not ann.startswith(_SPRING_ROUTE_ANNOTATIONS):
                    continue
                methods, route_path = _parse_spring_mapping(ann)
                if route_path is None:
                    route_path = ""
                for method in methods:
                    route_calls.append(
                        {
                            "receiver": java_current_class,
                            "method": method.lower(),
                            "path": route_path,
                            "handler_hint": handler,
                            "line": idx,
                            "snippet": _trim(f"{ann} {raw_line.strip()}"),
                        }
                    )
                    signals.add(f"route_call:{method.lower()}")
            java_pending_annotations = []
        elif java_pending_annotations and not line.startswith("@"):
            java_pending_annotations = []

        rocket_attr_match = _ROCKET_ROUTE_ATTR_RE.match(line)
        if rocket_attr_match:
            rocket_pending_route = (
                rocket_attr_match.group("verb").lower(),
                rocket_attr_match.group("path"),
                line,
            )
        elif rocket_pending_route:
            fn_match = _RUST_FN_RE.match(line)
            if fn_match:
                verb, path, attr_line = rocket_pending_route
                route_calls.append(
                    {
                        "receiver": _ROCKET_SYNTHETIC_RECEIVER,
                        "method": verb,
                        "path": _normalize_rocket_path(path),
                        "handler_hint": fn_match.group("name"),
                        "line": idx,
                        "snippet": _trim(f"{attr_line}\n{raw_line.strip()}"),
                    }
                )
                signals.add(f"route_call:{verb}")
            rocket_pending_route = None

        for match in _ROUTER_DECL_RE.finditer(line):
            symbol = match.group("symbol")
            router_symbols.append(symbol)
            signals.add("router_instance:express.Router")

        container = _extract_container_declaration(line)
        if container is not None and container["symbol"] not in {
            item["symbol"] for item in containers
        }:
            containers.append(
                {
                    "symbol": container["symbol"],
                    "prefix": container["prefix"],
                    "callee": container["callee"],
                    "line": idx,
                    "snippet": _trim(raw_line),
                }
            )
            router_symbols.append(container["symbol"])
            signals.add(f"route_container:{container['callee']}")
            if container["prefix"]:
                signals.add("container_prefix")

        generic_route = _GENERIC_ROUTE_CALL_RE.search(line)
        if generic_route is not None:
            methods_match = _METHODS_ARG_RE.search(generic_route.group("rest"))
            declared_methods = (
                [item.strip().strip("'\"").lower() for item in methods_match.group("methods").split(",")]
                if methods_match
                else ["get"]
            )
            for declared_method in [item for item in declared_methods if item]:
                route_calls.append(
                    {
                        "receiver": generic_route.group("receiver"),
                        "method": declared_method,
                        "path": generic_route.group("path"),
                        "handler_hint": _extract_handler_hint_from_route_snippet(raw_line)
                        or _handler_below_declaration(joined_lines, idx),
                        "line": idx,
                        "snippet": _trim(raw_line),
                    }
                )
                signals.add(f"route_call:{declared_method}")

        for match in _ROUTE_CALL_RE.finditer(line):
            method = match.group("method").lower()
            path = match.group("path")
            receiver = match.group("receiver")
            handler_hint = _extract_handler_hint_from_route_snippet(
                raw_line
            ) or _handler_below_declaration(joined_lines, idx)
            route_calls.append(
                {
                    "receiver": receiver,
                    "method": method,
                    "path": path,
                    "handler_hint": handler_hint,
                    "line": idx,
                    "snippet": _trim(raw_line),
                }
            )
            signals.add(f"route_call:{method}")

        mount = _extract_mount_call(line)
        if mount is not None:
            mount_calls.append(
                {
                    "receiver": mount["receiver"],
                    "prefix": mount["prefix"],
                    "child": mount["child"],
                    "line": idx,
                    "snippet": _trim(raw_line),
                }
            )
            signals.add(f"mount_call:{mount['verb'].lower()}")

        imports.extend(_extract_imports(line))
        imports.extend(_extract_python_imports(line))
        for match in _REQUIRE_IMPORT_RE.finditer(line):
            imports.append(
                {
                    "local": match.group("local"),
                    "imported": match.group("local"),
                    "source": match.group("source"),
                }
            )
        exports.extend(_extract_exports(line))

        for match in _PATH_LITERAL_RE.finditer(line):
            candidate = match.group("path")
            if candidate.startswith("//"):
                continue
            if candidate.startswith("/http"):
                continue
            if candidate not in path_literals:
                path_literals.append(candidate)
            if len(path_literals) >= _MAX_PATH_LITERALS_PER_FILE:
                break

    if path_literals:
        signals.add("path_literals")
    if any(item.get("kind") == "default" for item in exports):
        signals.add("default_export")

    return {
        "path": relative_path,
        "language": language,
        "role": role,
        "signals": sorted(signals),
        "router_symbols": sorted(set(router_symbols)),
        "containers": containers,
        "route_calls": route_calls,
        "mount_calls": mount_calls,
        "imports": imports,
        "exports": exports,
        "path_literals": path_literals,
        "java_type": java_type,
    }


def _split_args(expr: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in expr:
        if quote is not None:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                args.append(part)
            buf = []
            continue
        buf.append(ch)
    part = "".join(buf).strip()
    if part:
        args.append(part)
    return args


def _extract_handler_hint_from_route_snippet(snippet: str) -> str | None:
    line = " ".join(snippet.strip().split())
    if "(" not in line or ")" not in line:
        return None
    start = line.find("(")
    end = line.rfind(")")
    if end <= start:
        return None
    args = _split_args(line[start + 1 : end])
    if len(args) < 2:
        return None
    candidate = args[-1].strip().rstrip(";")
    if not candidate:
        return None

    def _unwrap(expr: str) -> str | None:
        expr = expr.strip().rstrip(";")
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", expr):
            return expr
        if re.fullmatch(r"[A-Za-z_]\w*", expr):
            return expr
        call_match = re.fullmatch(r"[A-Za-z_]\w*\((.*)\)", expr)
        if call_match:
            inner = call_match.group(1).strip()
            if not inner:
                return None
            inner_args = _split_args(inner)
            if not inner_args:
                return None
            return _unwrap(inner_args[0])
        return None

    return _unwrap(candidate)


def build_route_index(
    repo: RepoRef, *, repo_map: dict | None = None, fact_cache: Any = None
) -> dict:
    """Build compact deterministic route-signal index for one repository."""
    root = Path(repo.root).expanduser().resolve()
    repo_map_payload = repo_map or build_repo_map(repo)
    preferred_dirs = {
        item for item in (
            repo_map_payload.get("candidate_route_dirs", [])
            + repo_map_payload.get("candidate_backend_dirs", [])
        )
        if item and item != "."
    }

    files: list[dict] = []
    files_indexed = 0
    files_with_route_calls = 0
    route_call_count = 0
    mount_call_count = 0
    router_symbol_count = 0

    for raw_dirpath, dirnames, filenames in os.walk(root):
        dirpath = Path(raw_dirpath)
        dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
        for filename in filenames:
            path = dirpath / filename
            rel = path.relative_to(root).as_posix()
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTS:
                continue
            role = classify_candidate_file_role(rel)
            if role != FILE_ROLE_SOURCE_ROUTE_CANDIDATE:
                continue
            # Repository-root files are always indexed: the application
            # entrypoint usually lives there, and that is where containers are
            # mounted together. Excluding it hides the root of the mount graph.
            at_repo_root = str(Path(rel).parent) == "."
            if (
                not at_repo_root
                and not _path_in_dirs(rel, preferred_dirs)
                and not _path_in_dirs(str(Path(rel).parent), preferred_dirs)
            ):
                continue
            entry = fact_cache.lookup(rel) if fact_cache is not None else None
            if entry is None:
                try:
                    if path.stat().st_size > _MAX_FILE_SIZE:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                entry = _extract_index_for_file(rel, text, role)
                if fact_cache is not None:
                    fact_cache.record(rel, entry)
            files.append(entry)
            files_indexed += 1
            if entry["route_calls"]:
                files_with_route_calls += 1
            route_call_count += len(entry["route_calls"])
            mount_call_count += len(entry["mount_calls"])
            router_symbol_count += len(entry["router_symbols"])

    files.sort(key=lambda item: item["path"])
    signal_counts = Counter()
    for item in files:
        for signal in item["signals"]:
            signal_counts[signal] += 1

    return {
        "repo": repo.name,
        "root": str(root),
        "files": files,
        "summary": {
            "files_indexed": files_indexed,
            "files_with_route_calls": files_with_route_calls,
            "route_call_count": route_call_count,
            "mount_call_count": mount_call_count,
            "router_symbol_count": router_symbol_count,
            "signal_counts": dict(sorted(signal_counts.items())),
        },
    }


def build_route_index_batch(repos: list[RepoRef], *, repo_map_batch: dict | None = None) -> dict:
    """Build deterministic route indexes for many repositories."""
    repo_maps_by_name: dict[str, dict] = {}
    if repo_map_batch and isinstance(repo_map_batch.get("repos"), list):
        for item in repo_map_batch["repos"]:
            name = item.get("repo") if isinstance(item, dict) else None
            if isinstance(name, str):
                repo_maps_by_name[name] = item

    indexes = [
        build_route_index(repo, repo_map=repo_maps_by_name.get(repo.name))
        for repo in repos
    ]
    totals = {
        "files_indexed": sum(item["summary"].get("files_indexed", 0) for item in indexes),
        "files_with_route_calls": sum(item["summary"].get("files_with_route_calls", 0) for item in indexes),
        "route_call_count": sum(item["summary"].get("route_call_count", 0) for item in indexes),
        "mount_call_count": sum(item["summary"].get("mount_call_count", 0) for item in indexes),
        "router_symbol_count": sum(item["summary"].get("router_symbol_count", 0) for item in indexes),
    }
    return {"version": "v1", "repos": indexes, "summary": totals}
