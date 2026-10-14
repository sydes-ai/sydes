"""EXPERIMENT: bridge NestJS CQRS-style command/event dispatch for
TypeScript -- the exact gap documented in the real TS-H-01 calibration
case (`sydes-examples/.github`'s `examples/hard-v1/ts-h-01.yaml`):

    POST /v1/users -> CreateUserHttpController.create
      -> this.commandBus.execute(new CreateUserCommand(body))
      -> [FRAMEWORK-MEDIATED DISPATCH, no direct call in source text]
      -> @CommandHandler(CreateUserCommand) CreateUserService.execute
      -> ... -> Address.validate()

Not a generalization of the existing declaration-reference edges (a
linear "X's declaration names Y" chain) -- this is a CONVERGENCE: two
independent sites (a decorated handler class, and a message construction
site) both name the SAME message type, and only the framework connects
them at runtime. Modeled as a synthetic CALL edge (same precedent as
member_call_bridge.py / interface_bridge.py), not a usage edge.

Scoped, named vocabulary -- @CommandHandler/@EventsHandler/@QueryHandler,
NestJS/@nestjs/cqrs's actual three decorators -- same precedent as
deterministic_routes.py's per-framework decorator tables, not a claim
this covers every dispatch-style framework generically.

Bridging is unambiguous-only: a message type handled by more than one
decorated class is left unresolved rather than guessed at.

Research/validation only. Not wired into any production path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

DISPATCH_DECORATOR_NAMES = ("CommandHandler", "EventsHandler", "QueryHandler")
BRIDGE_SOURCE = "ts_command_dispatch_bridge"

_OWNER_NODE_TYPES = ("class_declaration", "function_declaration", "method_definition")
_OWNER_NAME_TYPES = ("type_identifier", "identifier", "property_identifier")


@dataclass(frozen=True)
class DispatchHandler:
    message_type: str
    handler_class: str
    file: str
    line: int


@dataclass(frozen=True)
class MessageConstruction:
    message_type: str
    owner: str
    owner_line: int
    file: str
    line: int


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _owner_name(node: Node, src: bytes) -> str | None:
    for child in node.children:
        if child.type in _OWNER_NAME_TYPES:
            return _text(child, src)
    return None


def _find_owner(node: Node, src: bytes) -> tuple[str, int] | None:
    current = node.parent
    while current is not None:
        if current.type in _OWNER_NODE_TYPES:
            name = _owner_name(current, src)
            if name:
                return name, current.start_point[0] + 1
        current = current.parent
    return None


def _class_after_decorator(decorator: Node, src: bytes) -> tuple[str, int] | None:
    """The class this decorator applies to: the first `class_declaration`
    appearing after `decorator` among its own siblings -- handles multiple
    stacked decorators (each is its own sibling preceding the class) and
    both `export class X` (siblings under `export_statement`) and bare
    `class X` (siblings directly under the enclosing block)."""
    parent = decorator.parent
    if parent is None:
        return None
    siblings = parent.children
    idx = siblings.index(decorator)
    # No early break: `export`/`default` keyword tokens are legitimate
    # siblings between a decorator and the class it applies to (a real bug
    # found empirically -- breaking on the first non-decorator sibling hit
    # the `export` token before ever reaching `class_declaration`). Siblings
    # are already scoped to the SAME immediate parent, so scanning all of
    # them for the first class_declaration carries no risk of matching an
    # unrelated, far-away class.
    for sibling in siblings[idx + 1:]:
        if sibling.type == "class_declaration":
            name = _owner_name(sibling, src)
            if name:
                return name, sibling.start_point[0] + 1
    return None


def extract_dispatch_handlers(source: str, *, file: str) -> list[DispatchHandler]:
    parser = get_parser("typescript")
    src = source.encode("utf-8")
    tree = parser.parse(src)
    out: list[DispatchHandler] = []

    def walk(node: Node) -> None:
        if node.type == "decorator":
            call = next((c for c in node.children if c.type == "call_expression"), None)
            if call is not None:
                callee = call.children[0] if call.children else None
                if callee is not None and callee.type == "identifier" and _text(callee, src) in DISPATCH_DECORATOR_NAMES:
                    args = next((c for c in call.children if c.type == "arguments"), None)
                    if args is not None:
                        arg_nodes = [c for c in args.children if c.type in ("identifier", "type_identifier")]
                        if len(arg_nodes) == 1:
                            message_type = _text(arg_nodes[0], src)
                            cls = _class_after_decorator(node, src)
                            if cls is not None:
                                handler_class, line = cls
                                out.append(DispatchHandler(
                                    message_type=message_type, handler_class=handler_class,
                                    file=file, line=line,
                                ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return out


def extract_message_constructions(
    source: str, *, file: str, known_message_types: set[str],
) -> list[MessageConstruction]:
    parser = get_parser("typescript")
    src = source.encode("utf-8")
    tree = parser.parse(src)
    out: list[MessageConstruction] = []

    def walk(node: Node) -> None:
        if node.type == "new_expression":
            ctor = next((c for c in node.children if c.type == "identifier"), None)
            if ctor is not None:
                type_name = _text(ctor, src)
                if type_name in known_message_types:
                    owner = _find_owner(node, src)
                    if owner is not None:
                        owner_name, owner_line = owner
                        out.append(MessageConstruction(
                            message_type=type_name, owner=owner_name, owner_line=owner_line,
                            file=file, line=node.start_point[0] + 1,
                        ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return out


def bridge_dispatch_edges(repo_root: Path, ts_files: list[str]) -> list[dict[str, Any]]:
    handlers: list[DispatchHandler] = []
    for rel in ts_files:
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        handlers.extend(extract_dispatch_handlers(source, file=rel))

    handlers_by_type: dict[str, list[DispatchHandler]] = {}
    for h in handlers:
        handlers_by_type.setdefault(h.message_type, []).append(h)
    # Unambiguous only: a message type claimed by more than one decorated
    # class is left unbridged, matching member_call_bridge.py's discipline.
    unambiguous_types = {t: hs[0] for t, hs in handlers_by_type.items() if len(hs) == 1}

    constructions: list[MessageConstruction] = []
    for rel in ts_files:
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        constructions.extend(extract_message_constructions(
            source, file=rel, known_message_types=set(unambiguous_types),
        ))

    edges: list[dict[str, Any]] = []
    for c in constructions:
        handler = unambiguous_types[c.message_type]
        edges.append({
            "caller_file": c.file, "caller_symbol": c.owner, "caller_qualified_name": "",
            "caller_line": c.owner_line,
            "callee_file": handler.file, "callee_symbol": handler.handler_class,
            "callee_qualified_name": "", "callee_line": handler.line,
            "source": BRIDGE_SOURCE,
            "evidence": f"{c.message_type} constructed at {c.file}:{c.line}, "
                        f"handled by @{'/'.join(DISPATCH_DECORATOR_NAMES)}({c.message_type}) "
                        f"on {handler.handler_class} at {handler.file}:{handler.line}",
        })
    return edges
