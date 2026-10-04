"""Compose one framework-mediated dispatch edge from static and runtime evidence.

EXPERIMENT, one rule: NestJS CQRS command handlers. Static analysis sees both sides of a
command dispatch but not the edge between them:

    DeleteUserHttpController.deleteUser:  this.commandBus.execute(new DeleteUserCommand(...))
    @CommandHandler(DeleteUserCommand) class DeleteUserService { execute(command) {...} }

and DiffGenome's runtime contract (`relation: "through_external"`) reports, from some test:

    <caller> invoked CommandBus.execute with argument shape DeleteUserCommand, and
    DeleteUserService.execute was entered while that invocation was active.

The runtime fact does not say CommandBus.execute called the handler, nor that the
production call site was observed; the static facts do not say the framework dispatches
there. Only when all of them line up -- the same external owner and member, the same
argument type at a static call site, the runtime callee being that type's sole decorated
handler's `execute` -- is one edge composed: `call site's method -> handler.execute`,
tagged `sydes:composed-dispatch` and walked as `composed_dispatch`, never as a call or as
an observed runtime call. Anything missing or ambiguous composes nothing and is reported
as a possible dispatch with the evidence that was insufficient.

Keyed on the rule's vocabulary (CommandBus / execute / @CommandHandler), never on
repository paths or class names. QueryBus, EventBus and other frameworks are out of scope.
`tree_sitter`/`tree_sitter_language_pack` are required Sydes dependencies, imported
lazily; if a broken environment lacks them the rule is not evaluated and says so
(`PARSER_UNAVAILABLE`), and the analyzer reports the analysis as partial.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMPOSED_DISPATCH_SOURCE = "sydes:composed-dispatch"
RULE = "nestjs_cqrs_command_handler"
_BUS_OWNER = "CommandBus"
_BUS_MEMBER = "execute"
_HANDLER_DECORATOR = "CommandHandler"
_HANDLER_MEMBER = "execute"
_REGISTRY_MODULE = "CqrsModule"
_TS_SUFFIXES = (".ts", ".tsx")
_SKIP_DIRS = {"node_modules", "dist", "build", "coverage", ".git"}
_MAX_FILES = 4000


@dataclass(frozen=True)
class HandlerDecl:
    """`@CommandHandler(T)` on class `handler_class`, which declares `execute`."""

    message_type: str
    handler_class: str
    file: str
    line: int
    has_member: bool


@dataclass(frozen=True)
class ProducerSite:
    """`<receiver>.<member>(<argument>)` inside `owner_class.method`, with the receiver's
    declared type and the argument's type as written in the same class/method."""

    owner_class: str
    method: str
    method_line: int
    file: str
    line: int
    receiver_type: str
    member: str
    argument_type: str


PARSER_UNAVAILABLE = "structural parser (tree-sitter) unavailable"


def _get_parser():
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return None
    return get_parser


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _field(node: Any, name: str) -> Any:
    return node.child_by_field_name(name)


def _walk(node: Any):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _type_name(annotation: Any, src: bytes) -> str | None:
    """`: CommandBus` -> `CommandBus`; anything but a plain named type -> None."""
    if annotation is None:
        return None
    named = [c for c in annotation.children if c.type in ("type_identifier", "identifier")]
    return _text(named[0], src) if len(named) == 1 and len(annotation.named_children) == 1 else None


def _enclosing(node: Any, kind: str) -> Any:
    current = node.parent
    while current is not None and current.type != kind:
        current = current.parent
    return current


def extract_handlers(source: str, *, file: str, get_parser: Any) -> list[HandlerDecl]:
    src = source.encode("utf-8")
    tree = get_parser("typescript").parse(src)
    out: list[HandlerDecl] = []
    for node in _walk(tree.root_node):
        if node.type != "class_declaration":
            continue
        name = _field(node, "name")
        decorators = [c for c in node.children if c.type == "decorator"]
        if node.parent is not None and node.parent.type == "export_statement":
            decorators += [c for c in node.parent.children if c.type == "decorator"]
        for decorator in decorators:
            call = next((c for c in decorator.children if c.type == "call_expression"), None)
            if call is None or _text(_field(call, "function"), src) != _HANDLER_DECORATOR:
                continue
            args = [a for a in _field(call, "arguments").named_children]
            if len(args) != 1 or args[0].type != "identifier" or name is None:
                continue
            body = _field(node, "body")
            members = {
                _text(_field(m, "name"), src)
                for m in (body.named_children if body is not None else [])
                if m.type == "method_definition" and _field(m, "name") is not None
            }
            out.append(HandlerDecl(
                message_type=_text(args[0], src), handler_class=_text(name, src), file=file,
                line=node.start_point[0] + 1, has_member=_HANDLER_MEMBER in members,
            ))
    return out


def _class_field_types(cls: Any, src: bytes) -> dict[str, str]:
    """Declared types of a class's fields: `x: T` fields and constructor parameter
    properties (`private readonly x: T`)."""
    types: dict[str, str] = {}
    body = _field(cls, "body")
    for member in body.named_children if body is not None else []:
        if member.type == "public_field_definition":
            name, kind = _field(member, "name"), _type_name(_field(member, "type"), src)
            if name is not None and kind:
                types[_text(name, src)] = kind
        if member.type == "method_definition" and _text(_field(member, "name"), src) == "constructor":
            for param in _field(member, "parameters").named_children:
                if not any(c.type == "accessibility_modifier" or _text(c, src) == "readonly"
                           for c in param.children):
                    continue
                pattern, kind = _field(param, "pattern"), _type_name(_field(param, "type"), src)
                if pattern is not None and pattern.type == "identifier" and kind:
                    types[_text(pattern, src)] = kind
    return types


def _argument_type(arg: Any, method: Any, src: bytes) -> str | None:
    """`new T(...)` -> T; an identifier bound in the same method by `const v = new T(...)`
    or declared `v: T` -> T; anything else -> None (never guessed)."""
    if arg.type == "new_expression":
        ctor = _field(arg, "constructor")
        return _text(ctor, src) if ctor is not None and ctor.type == "identifier" else None
    if arg.type != "identifier":
        return None
    name = _text(arg, src)
    found: set[str] = set()
    for node in _walk(method):
        if node.type in ("variable_declarator", "required_parameter") and node.start_byte < arg.start_byte:
            bound = _field(node, "name") or _field(node, "pattern")
            if bound is None or _text(bound, src) != name:
                continue
            declared = _type_name(_field(node, "type"), src)
            value = _field(node, "value")
            if declared:
                found.add(declared)
            elif value is not None and value.type == "new_expression":
                ctor = _field(value, "constructor")
                if ctor is not None and ctor.type == "identifier":
                    found.add(_text(ctor, src))
    return found.pop() if len(found) == 1 else None


def extract_producers(source: str, *, file: str, get_parser: Any) -> list[ProducerSite]:
    src = source.encode("utf-8")
    tree = get_parser("typescript").parse(src)
    out: list[ProducerSite] = []
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        callee = _field(node, "function")
        if callee is None or callee.type != "member_expression":
            continue
        receiver, prop = _field(callee, "object"), _field(callee, "property")
        method = _enclosing(node, "method_definition")
        cls = _enclosing(node, "class_declaration")
        if receiver is None or prop is None or method is None or cls is None:
            continue
        # the receiver's declared type: `this.<field>` typed on the class
        if receiver.type != "member_expression" or _text(_field(receiver, "object"), src) != "this":
            continue
        receiver_type = _class_field_types(cls, src).get(_text(_field(receiver, "property"), src))
        args = _field(node, "arguments").named_children
        if not receiver_type or len(args) != 1:
            continue
        argument_type = _argument_type(args[0], method, src)
        if not argument_type:
            continue
        out.append(ProducerSite(
            owner_class=_text(_field(cls, "name"), src), method=_text(_field(method, "name"), src),
            method_line=method.start_point[0] + 1, file=file, line=node.start_point[0] + 1,
            receiver_type=receiver_type, member=_text(prop, src), argument_type=argument_type,
        ))
    return out


def _registered_in_cqrs_module(sources: dict[str, str], handler_class: str) -> bool:
    """Strengthener only (never required): some `@Module` file importing CqrsModule names
    the handler class. Textual, and reported as such."""
    return any(
        "@Module(" in text and _REGISTRY_MODULE in text and handler_class in text
        for text in sources.values()
    )


def ts_files(repo_root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(repo_root.rglob("*")):
        if len(out) >= _MAX_FILES:
            break
        rel = path.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in rel.parts) or path.suffix not in _TS_SUFFIXES:
            continue
        if path.name.endswith(".d.ts") or not path.is_file():
            continue
        out.append(rel.as_posix())
    return out


def _short(symbol: str) -> str:
    parts = symbol.split(":", 1)[-1].split(".")
    return ".".join(parts[-2:])


def compose_dispatch_edges(
    bridges: list[Any], *, repo_root: Path, files: list[str], resolve: Any, repo: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """`(call edges to add, composed edge records, notes)`.

    `bridges`: `RuntimeExternalBridge`s from the runtime contract. `resolve(file, name,
    line)` maps a static definition to the code-intelligence symbol (`{"qualified",
    "line"}`) or None. Every predicate is required; a miss is a note, never an edge."""
    get_parser = _get_parser()
    rule_bridges = [b for b in bridges if b.owner == _BUS_OWNER and b.member == _BUS_MEMBER]
    if get_parser is None or not files:
        return [], [], ([f"{RULE}: not evaluated ({PARSER_UNAVAILABLE})"]
                        if get_parser is None and rule_bridges else [])
    sources: dict[str, str] = {}
    handlers: dict[str, list[HandlerDecl]] = defaultdict(list)
    producers: dict[str, list[ProducerSite]] = defaultdict(list)
    for rel in files:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources[rel] = text
        if _HANDLER_DECORATOR in text:
            for h in extract_handlers(text, file=rel, get_parser=get_parser):
                handlers[h.message_type].append(h)
        if _BUS_MEMBER in text:
            for p in extract_producers(text, file=rel, get_parser=get_parser):
                if p.receiver_type == _BUS_OWNER and p.member == _BUS_MEMBER:
                    producers[p.argument_type].append(p)

    edges: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    notes: list[str] = []
    composed_types: set[str] = set()
    for bridge in rule_bridges:
        for shape in bridge.arg_shapes:
            if len(shape) != 1:
                notes.append(f"{RULE}: {bridge.owner}.{bridge.member} observed with "
                             f"{len(shape)} argument(s), not one message: not composed")
                continue
            message = shape[0]
            decls = handlers.get(message, [])
            if len(decls) != 1:
                notes.append(f"{RULE}: possible dispatch of {message}: "
                             + ("no" if not decls else f"{len(decls)}")
                             + f" @{_HANDLER_DECORATOR}({message}) class(es); not composed")
                continue
            handler = decls[0]
            callee_short = f"{handler.handler_class}.{_HANDLER_MEMBER}"
            if not (handler.has_member and bridge.callee.get("file") == handler.file
                    and _short(str(bridge.callee.get("symbol"))) == callee_short):
                notes.append(f"{RULE}: possible dispatch of {message}: the code observed within "
                             f"{bridge.owner}.{bridge.member} ({_short(str(bridge.callee.get('symbol')))}) "
                             f"is not {callee_short}; not composed")
                continue
            sites = producers.get(message, [])
            if not sites:
                notes.append(f"{RULE}: possible dispatch of {message} to {callee_short}: no static "
                             f"{_BUS_OWNER}.{_BUS_MEMBER}({message}) call site found; not composed")
                continue
            callee_ref = resolve(handler.file, _HANDLER_MEMBER, None, handler.handler_class)
            for site in sites:
                caller_ref = resolve(site.file, site.method, site.method_line, site.owner_class)
                if caller_ref is None or callee_ref is None:
                    notes.append(f"{RULE}: {site.owner_class}.{site.method} -> {callee_short}: "
                                 "an end is not in the symbol index; not composed")
                    continue
                composed_types.add(message)
                record = {
                    "from": f"{site.owner_class}.{site.method}",
                    "to": callee_short,
                    "kind": "semantic_dispatch",
                    "evidence": "composed",
                    "rule": RULE,
                    "static": {
                        "producer": {"file": site.file, "line": site.line,
                                     "call": f"{site.receiver_type}.{site.member}",
                                     "argument_type": message},
                        "handler": {"file": handler.file, "line": handler.line,
                                    "decorator": f"{_HANDLER_DECORATOR}({message})"},
                        "registered_in_cqrs_module": _registered_in_cqrs_module(
                            sources, handler.handler_class),
                    },
                    "runtime": {
                        "relation": "through_external",
                        "via": {"symbol": bridge.symbol, "owner": bridge.owner,
                                "member": bridge.member, "arg_shape": message},
                        "callee": bridge.callee.get("symbol"),
                        # who invoked the external code at runtime: a test frame here, not
                        # the production call site this edge starts from
                        "observed_caller": {"symbol": bridge.caller.get("symbol"),
                                            "origin": bridge.caller.get("origin")},
                        "tests": list(bridge.tests)[:5],
                        "tests_total": bridge.tests_total,
                    },
                }
                records.append(record)
                observed_from = ("a test" if bridge.caller.get("origin") == "test"
                                 else _short(str(bridge.caller.get("symbol"))))
                edges.append({
                    "repo": repo,
                    "caller_file": site.file, "caller_symbol": site.method,
                    "caller_qualified_name": caller_ref["qualified"], "caller_line": caller_ref["line"],
                    "callee_file": handler.file, "callee_symbol": _HANDLER_MEMBER,
                    "callee_qualified_name": callee_ref["qualified"], "callee_line": callee_ref["line"],
                    "source": COMPOSED_DISPATCH_SOURCE,
                    # structured, so trace steps built from this edge can say what it is
                    "composed": {"rule": RULE, "via": f"{bridge.owner}.{bridge.member}",
                                 "message": message},
                    "evidence": (
                        f"composed ({RULE}): {site.receiver_type}.{site.member}({message}) at "
                        f"{site.file}:{site.line}; @{_HANDLER_DECORATOR}({message}) on "
                        f"{handler.handler_class} ({handler.file}:{handler.line}); runtime: "
                        f"{callee_short} observed executing within {bridge.owner}.{bridge.member}"
                        f"({message}), invoked from {observed_from}, in {bridge.tests_total} test(s)"
                    ),
                })
    # static-only matches the runtime did not support: possible, never composed
    for message, sites in sorted(producers.items()):
        if message in composed_types or len(handlers.get(message, [])) != 1:
            continue
        handler = handlers[message][0]
        notes.append(f"{RULE}: possible dispatch {sites[0].owner_class}.{sites[0].method} -> "
                     f"{handler.handler_class}.{_HANDLER_MEMBER} ({message}): static evidence only, "
                     "no through_external runtime observation; not composed")
    return edges, records, notes
