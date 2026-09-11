"""Layer 2, cross-language: member/attribute access + STRICTLY BOUNDED
receiver-type resolution, generalizing member_access_extractor.py (Python)
over tree-sitter for TypeScript, Java, Go, and Rust.

Same discipline as the Python version: receiver type is accepted ONLY from
a direct, same-function declaration (a locally declared variable's own
type annotation, or a locally declared variable's constructor-like
initializer), never from cross-file import chasing (out of scope for this
generalization pass), never from data-flow/points-to reasoning. Anything
else is UNKNOWN. No framework or symbol name anywhere in the algorithm.

Each language's declaration SHAPE differs (this is expected and stated in
the experiment plan -- "do not assume all languages can support the exact
same relation equally"), so each gets its own small, explicit recognizer;
what's shared is the discipline (same-scope only, no guessing) and the
output shape.

Not wired into any production path.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

READS_MEMBER = "reads_member"

# TYPE-like definitions only -- deliberately excludes function/method
# definitions. A real false positive surfaced during this experiment
# (a local variable named `info` matched an UNRELATED method also named
# `info` in a different scanned file, purely by name coincidence) is
# exactly the failure mode the experiment's own safety rule bans: string-
# name coincidence is not receiver-type evidence. Restricting the "receiver
# IS ALREADY a known name" shortcut -- and the declaration-based resolvers,
# which only ever look for a TYPE anyway (a constructor call or a type
# annotation both name a type, never a plain function) -- to type-only
# definitions removes this whole class of mistake, not just the one
# instance found.
TYPE_DEFINITION_NODE_TYPES: dict[str, tuple[str, ...]] = {
    "typescript": ("class_declaration", "interface_declaration", "type_alias_declaration", "enum_declaration"),
    "java": ("class_declaration", "interface_declaration", "enum_declaration"),
    "go": ("type_spec",),
    "rust": ("struct_item", "enum_item", "trait_item"),
}
TYPE_DEFINITION_NAME_TYPES: dict[str, tuple[str, ...]] = {
    "typescript": ("type_identifier",),
    "java": ("identifier",),
    "go": ("type_identifier",),
    "rust": ("type_identifier",),
}


def known_type_names(sources: dict[str, str], *, language: str) -> set[str]:
    """Only class/struct/interface/enum/trait names -- see the module-level
    note above for why function/method names must NOT be included here."""
    def_types = TYPE_DEFINITION_NODE_TYPES[language]
    name_types = TYPE_DEFINITION_NAME_TYPES[language]
    parser = get_parser(language)
    names: set[str] = set()

    def walk(node: Node, src: bytes):
        if node.type in def_types:
            for child in node.children:
                if child.type in name_types:
                    names.add(_text(child, src))
                    break
        for child in node.children:
            walk(child, src)

    for source in sources.values():
        src = source.encode("utf-8")
        walk(parser.parse(src).root_node, src)
    return names


@dataclass(frozen=True)
class MemberAccessConfig:
    access_node_types: tuple[str, ...]
    receiver_child_type: str  # the node type identifying a bare-identifier receiver
    member_child_types: tuple[str, ...]
    owner_node_types: tuple[str, ...]  # function/method-like, for attributing the access
    owner_name_types: tuple[str, ...]


CONFIGS: dict[str, MemberAccessConfig] = {
    "typescript": MemberAccessConfig(
        access_node_types=("member_expression",),
        receiver_child_type="identifier",
        member_child_types=("property_identifier",),
        owner_node_types=("method_definition", "function_declaration"),
        owner_name_types=("property_identifier", "identifier"),
    ),
    "java": MemberAccessConfig(
        access_node_types=("field_access",),
        receiver_child_type="identifier",
        member_child_types=("identifier",),
        owner_node_types=("method_declaration", "constructor_declaration"),
        owner_name_types=("identifier",),
    ),
    "go": MemberAccessConfig(
        access_node_types=("selector_expression",),
        receiver_child_type="identifier",
        member_child_types=("field_identifier",),
        owner_node_types=("function_declaration", "method_declaration"),
        owner_name_types=("identifier",),
    ),
    "rust": MemberAccessConfig(
        access_node_types=("field_expression",),
        receiver_child_type="identifier",
        member_child_types=("field_identifier",),
        owner_node_types=("function_item",),
        owner_name_types=("identifier",),
    ),
}


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _owner_name(node: Node, cfg: MemberAccessConfig, src: bytes) -> str | None:
    for child in node.children:
        if child.type in cfg.owner_name_types:
            return _text(child, src)
    return None


def _find_owner(node: Node, cfg: MemberAccessConfig, src: bytes) -> tuple[str, Node] | None:
    current = node.parent
    while current is not None:
        if current.type in cfg.owner_node_types:
            name = _owner_name(current, cfg, src)
            if name:
                return name, current
        current = current.parent
    return None


def _resolve_receiver_typescript(owner_node: Node, receiver: str, known: set[str], src: bytes) -> str | None:
    candidates: set[str] = set()
    for node in _walk(owner_node):
        if node.type == "variable_declarator":
            name_child = next((c for c in node.children if c.type == "identifier"), None)
            if name_child is None or _text(name_child, src) != receiver:
                continue
            type_ann = next((c for c in node.children if c.type == "type_annotation"), None)
            if type_ann is not None:
                type_id = next((c for c in _walk(type_ann) if c.type == "type_identifier"), None)
                if type_id is not None and _text(type_id, src) in known:
                    candidates.add(_text(type_id, src))
            new_expr = next((c for c in node.children if c.type == "new_expression"), None)
            if new_expr is not None:
                ctor = next((c for c in new_expr.children if c.type == "identifier"), None)
                if ctor is not None and _text(ctor, src) in known:
                    candidates.add(_text(ctor, src))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _resolve_receiver_java(owner_node: Node, receiver: str, known: set[str], src: bytes) -> str | None:
    candidates: set[str] = set()
    for node in _walk(owner_node):
        if node.type != "local_variable_declaration":
            continue
        declared_type = next((c for c in node.children if c.type == "type_identifier"), None)
        for decl in node.children:
            if decl.type != "variable_declarator":
                continue
            name_child = next((c for c in decl.children if c.type == "identifier"), None)
            if name_child is not None and _text(name_child, src) == receiver:
                if declared_type is not None and _text(declared_type, src) in known:
                    candidates.add(_text(declared_type, src))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _resolve_receiver_go(owner_node: Node, receiver: str, known: set[str], src: bytes) -> str | None:
    candidates: set[str] = set()
    for node in _walk(owner_node):
        if node.type != "short_var_declaration":
            continue
        lhs = next((c for c in node.children if c.type == "expression_list"), None)
        exprs = [c for c in node.children if c.type == "expression_list"]
        if len(exprs) < 2:
            continue
        lhs_names = [gc.type == "identifier" and _text(gc, src) for gc in exprs[0].children]
        if receiver not in lhs_names:
            continue
        rhs = exprs[1]
        composite = next((c for c in _walk(rhs) if c.type == "composite_literal"), None)
        if composite is not None:
            type_id = next((c for c in composite.children if c.type == "type_identifier"), None)
            if type_id is not None and _text(type_id, src) in known:
                candidates.add(_text(type_id, src))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _resolve_receiver_rust(owner_node: Node, receiver: str, known: set[str], src: bytes) -> str | None:
    candidates: set[str] = set()
    for node in _walk(owner_node):
        if node.type != "let_declaration":
            continue
        name_child = next((c for c in node.children if c.type == "identifier"), None)
        if name_child is None or _text(name_child, src) != receiver:
            continue
        value = next((c for c in node.children if c.type == "call_expression"), None)
        if value is not None:
            scoped = next((c for c in value.children if c.type == "scoped_identifier"), None)
            if scoped is not None:
                type_id = next((c for c in scoped.children if c.type == "identifier"), None)
                if type_id is not None and _text(type_id, src) in known:
                    candidates.add(_text(type_id, src))
    return next(iter(candidates)) if len(candidates) == 1 else None


_RESOLVERS = {
    "typescript": _resolve_receiver_typescript,
    "java": _resolve_receiver_java,
    "go": _resolve_receiver_go,
    "rust": _resolve_receiver_rust,
}


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def extract_member_access(
    source: str, *, file: str, language: str, known_names: set[str],
) -> list[dict]:
    cfg = CONFIGS[language]
    parser = get_parser(language)
    src = source.encode("utf-8")
    tree = parser.parse(src)
    results: list[dict] = []
    seen: set[tuple] = set()

    for node in _walk(tree.root_node):
        if node.type not in cfg.access_node_types:
            continue
        # Positional, not just type-matched: in Java's `field_access`, the
        # receiver and the member are BOTH plain `identifier` nodes, so a
        # type-only match would find the same (first) node for both. The
        # grammars dumped for this experiment are consistently
        # `[receiver, '.', member]` -- first matching child is the
        # receiver, LAST matching child is the member.
        named_children = [c for c in node.children if c.type != "."]
        if len(named_children) < 2:
            continue
        receiver_node, member_node = named_children[0], named_children[-1]
        if receiver_node.type != cfg.receiver_child_type or member_node.type not in cfg.member_child_types:
            continue
        receiver = _text(receiver_node, src)
        member = _text(member_node, src)
        owner = _find_owner(node, cfg, src)
        if owner is None:
            continue
        owner_name, owner_node = owner
        key = (owner_name, receiver, member)
        if key in seen:
            continue
        seen.add(key)

        if receiver in known_names:
            # The receiver identifier IS ITSELF a known class/type name --
            # a static/class-level access (`RabbitConsts.SOME_CONST`,
            # Java/Go/Rust's "qualify by type name directly" idiom). This
            # is not a guess: no inference is needed at all when the
            # receiver already unambiguously names a real symbol in the
            # codebase, unlike every other resolver below which infers an
            # unknown variable's type from its declaration.
            resolved = receiver
        else:
            resolved = _RESOLVERS[language](owner_node, receiver, known_names, src)
        results.append({
            "kind": READS_MEMBER, "language": language, "file": file,
            "function": owner_name, "receiver": receiver, "member": member,
            "line": node.start_point[0] + 1,
            "status": "resolved" if resolved else "unknown",
            "resolved_type": resolved,
        })
    return results
