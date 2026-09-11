"""Layer 2, cross-language: the SAME two semantic patterns as
python_extractors.py (field/param type-reference, call-argument-reference),
implemented once generically over tree-sitter, parametrized per language
only by grammar NODE-TYPE NAMES -- never by framework or symbol name.

This is deliberately the same kind of per-language (not per-framework)
table `discover/deterministic_routes.py` already uses for route decorator
regexes -- a precedented, narrow form of specialization, not a loophole.

Not wired into any production path.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

FIELD_OR_PARAM_TYPE_REFERENCE = "field_or_param_type_reference"
CALL_ARGUMENT_REFERENCE = "call_argument_reference"


@dataclass(frozen=True)
class LangConfig:
    # Node types whose name-child identifies an "owner" a reference can be
    # attributed to (class/struct/function/method-like definitions).
    owner_node_types: tuple[str, ...]
    # Within an owner_node_types node, the child node type holding its name.
    name_node_types: tuple[str, ...]
    # Node types that represent "a type was referenced here" (as opposed to
    # a call/value reference) -- these are looked for anywhere inside a
    # field/parameter declaration.
    type_reference_node_types: tuple[str, ...]
    # Node types representing a call expression.
    call_node_types: tuple[str, ...]
    # Node types representing a call's argument list (children of it that
    # are themselves a bare reference node are the call-argument edges).
    argument_list_node_types: tuple[str, ...]
    # Node types that count as "a bare reference" when found as a direct
    # argument (identifiers and/or type identifiers, language-dependent).
    bare_reference_node_types: tuple[str, ...]
    # Node types whose name-child counts as a "known symbol" repo-wide --
    # broader than owner_node_types (also interfaces/types/enums/traits),
    # used to filter noise: an ordinary local variable name passed as a
    # call argument must NOT produce an edge just because it's an
    # identifier -- only a name that is ALSO a real symbol defined
    # somewhere in the scanned repository is kept. This is the same filter
    # python_extractors.py already applies via `_local_definitions`; the
    # first cross-language run (see report) showed omitting it here
    # produces real noise (e.g. `isEmpty -> item`, a parameter name, not a
    # symbol).
    definition_node_types: tuple[str, ...]


LANG_CONFIGS: dict[str, LangConfig] = {
    "typescript": LangConfig(
        owner_node_types=("class_declaration", "function_declaration", "method_definition"),
        name_node_types=("type_identifier", "identifier", "property_identifier"),
        type_reference_node_types=("type_identifier",),
        call_node_types=("call_expression", "new_expression"),
        argument_list_node_types=("arguments",),
        bare_reference_node_types=("identifier", "type_identifier"),
        definition_node_types=(
            "class_declaration", "interface_declaration", "type_alias_declaration",
            "enum_declaration", "function_declaration",
        ),
    ),
    "java": LangConfig(
        owner_node_types=("class_declaration", "method_declaration", "constructor_declaration"),
        name_node_types=("identifier",),
        type_reference_node_types=("type_identifier",),
        call_node_types=("method_invocation", "object_creation_expression"),
        argument_list_node_types=("argument_list",),
        bare_reference_node_types=("identifier",),
        definition_node_types=(
            "class_declaration", "interface_declaration", "enum_declaration", "method_declaration",
        ),
    ),
    "go": LangConfig(
        owner_node_types=("type_spec", "function_declaration", "method_declaration"),
        name_node_types=("type_identifier", "identifier", "field_identifier"),
        type_reference_node_types=("type_identifier",),
        call_node_types=("call_expression",),
        argument_list_node_types=("argument_list",),
        bare_reference_node_types=("identifier",),
        definition_node_types=("type_spec", "function_declaration"),
    ),
    "rust": LangConfig(
        owner_node_types=("struct_item", "function_item"),
        name_node_types=("type_identifier", "identifier"),
        type_reference_node_types=("type_identifier",),
        call_node_types=("call_expression",),
        argument_list_node_types=("arguments",),
        bare_reference_node_types=("identifier",),
        definition_node_types=("struct_item", "enum_item", "trait_item", "function_item"),
    ),
}


def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _owner_name(node: Node, cfg: LangConfig, src: bytes) -> str | None:
    for child in node.children:
        if child.type in cfg.name_node_types:
            return _node_text(child, src)
    return None


def _find_owner(node: Node, cfg: LangConfig, src: bytes) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in cfg.owner_node_types:
            name = _owner_name(current, cfg, src)
            if name:
                return name
        current = current.parent
    return None


def known_symbol_names(sources: dict[str, str], *, language: str) -> set[str]:
    """Every name this repo (well, the scanned subset of it) gives meaning
    to: classes, interfaces, types, enums, traits, functions -- collected
    across ALL scanned files, not just one, so a cross-file type reference
    (e.g. an imported interface) still counts as known. An ordinary local
    variable or parameter name will essentially never coincide with one of
    these, which is what makes this an effective, generic noise filter
    without hardcoding any symbol or framework name."""
    cfg = LANG_CONFIGS[language]
    parser = get_parser(language)
    names: set[str] = set()

    def walk(node: Node, src: bytes):
        if node.type in cfg.definition_node_types:
            name = _owner_name(node, cfg, src)
            if name:
                names.add(name)
        for child in node.children:
            walk(child, src)

    for source in sources.values():
        src = source.encode("utf-8")
        walk(parser.parse(src).root_node, src)
    return names


def extract_field_and_parameter_type_references(
    source: str, *, file: str, language: str, known_names: set[str] | None = None,
) -> list[dict]:
    cfg = LANG_CONFIGS[language]
    parser = get_parser(language)
    src = source.encode("utf-8")
    tree = parser.parse(src)
    edges: list[dict] = []

    def walk(node: Node):
        if node.type in cfg.type_reference_node_types:
            owner = _find_owner(node, cfg, src)
            used = _node_text(node, src)
            if owner and used and used != owner and (known_names is None or used in known_names):
                edges.append({
                    "kind": FIELD_OR_PARAM_TYPE_REFERENCE,
                    "language": language, "file": file,
                    "user_symbol": owner, "used_symbol": used,
                    "line": node.start_point[0] + 1,
                })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


def extract_call_argument_references(
    source: str, *, file: str, language: str, known_names: set[str] | None = None,
) -> list[dict]:
    cfg = LANG_CONFIGS[language]
    parser = get_parser(language)
    src = source.encode("utf-8")
    tree = parser.parse(src)
    edges: list[dict] = []

    def walk(node: Node):
        if node.type in cfg.call_node_types:
            for child in node.children:
                if child.type in cfg.argument_list_node_types:
                    for arg in child.children:
                        if arg.type in cfg.bare_reference_node_types:
                            owner = _find_owner(node, cfg, src)
                            used = _node_text(arg, src)
                            if (
                                owner and used and used != owner
                                and (known_names is None or used in known_names)
                            ):
                                edges.append({
                                    "kind": CALL_ARGUMENT_REFERENCE,
                                    "language": language, "file": file,
                                    "user_symbol": owner, "used_symbol": used,
                                    "line": node.start_point[0] + 1,
                                })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


def extract_all(
    source: str, *, file: str, language: str, known_names: set[str] | None = None,
) -> list[dict]:
    return (
        extract_field_and_parameter_type_references(source, file=file, language=language, known_names=known_names)
        + extract_call_argument_references(source, file=file, language=language, known_names=known_names)
    )
