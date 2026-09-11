"""Layer 2 generic declaration-reference edges for TypeScript, Java, and Go
-- the same two relation kinds as `layer2_declaration_bridge.py`'s Python
support (a field/parameter type annotation, or a call argument, referencing
a known symbol), generalized over tree-sitter and parametrized per language
only by grammar node-type names, never by framework or symbol name. Ported
from `experiments/layer2_generic_edges/treesitter_extractors.py`, validated
there against real repos across all four languages with zero noise once the
`known_symbol_names` filter was added.

Rust deliberately not included this round.

SAME-FILE resolution only. Python's cross-file one-hop import resolution
(`resolve_cross_file_parameter_types`) has no validated equivalent here yet
-- a real, scoped follow-up, not invented in this pass.

`tree_sitter`/`tree_sitter_language_pack` are an optional extra
(`sydes[treesitter]`) imported lazily here so a Python-only install is
entirely unaffected by their absence -- callers get an empty edge list with
a clear reason, not an ImportError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sydes.discover.layer2_shared import (
    LAYER2_SOURCE,
    citation_verified,
    files_by_path,
    layer2_generic_edges_enabled,
    qualified_name_for,
)

FIELD_OR_PARAM_TYPE_REFERENCE = "field_or_param_type_reference"
CALL_ARGUMENT_REFERENCE = "call_argument_reference"

_EXT_TO_LANGUAGE = {
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
}


@dataclass(frozen=True)
class _LangConfig:
    owner_node_types: tuple[str, ...]
    name_node_types: tuple[str, ...]
    type_reference_node_types: tuple[str, ...]
    call_node_types: tuple[str, ...]
    argument_list_node_types: tuple[str, ...]
    bare_reference_node_types: tuple[str, ...]


_LANG_CONFIGS: dict[str, _LangConfig] = {
    "typescript": _LangConfig(
        owner_node_types=("class_declaration", "function_declaration", "method_definition"),
        name_node_types=("type_identifier", "identifier", "property_identifier"),
        type_reference_node_types=("type_identifier",),
        call_node_types=("call_expression", "new_expression"),
        argument_list_node_types=("arguments",),
        bare_reference_node_types=("identifier", "type_identifier"),
    ),
    "java": _LangConfig(
        owner_node_types=("class_declaration", "method_declaration", "constructor_declaration"),
        name_node_types=("identifier",),
        type_reference_node_types=("type_identifier",),
        call_node_types=("method_invocation", "object_creation_expression"),
        argument_list_node_types=("argument_list",),
        bare_reference_node_types=("identifier",),
    ),
    "go": _LangConfig(
        owner_node_types=("type_spec", "function_declaration", "method_declaration"),
        name_node_types=("type_identifier", "identifier", "field_identifier"),
        type_reference_node_types=("type_identifier",),
        call_node_types=("call_expression",),
        argument_list_node_types=("argument_list",),
        bare_reference_node_types=("identifier",),
    ),
}


def _get_tree_sitter():
    try:
        from tree_sitter import Node  # noqa: F401
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return None
    return get_parser


def _node_text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _owner_name(node: Any, cfg: _LangConfig, src: bytes) -> str | None:
    for child in node.children:
        if child.type in cfg.name_node_types:
            return _node_text(child, src)
    return None


def _find_owner(node: Any, cfg: _LangConfig, src: bytes) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in cfg.owner_node_types:
            name = _owner_name(current, cfg, src)
            if name:
                return name
        current = current.parent
    return None


def _extract_for_file(
    source: str, *, file: str, language: str, known_names: set[str], get_parser,
) -> list[dict[str, Any]]:
    cfg = _LANG_CONFIGS[language]
    parser = get_parser(language)
    src = source.encode("utf-8")
    tree = parser.parse(src)
    edges: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if node.type in cfg.type_reference_node_types:
            owner = _find_owner(node, cfg, src)
            used = _node_text(node, src)
            if owner and used and used != owner and used in known_names:
                edges.append({
                    "kind": FIELD_OR_PARAM_TYPE_REFERENCE, "user_file": file, "user_symbol": owner,
                    "used_file": file, "used_symbol": used, "line": node.start_point[0] + 1,
                })
        if node.type in cfg.call_node_types:
            for child in node.children:
                if child.type not in cfg.argument_list_node_types:
                    continue
                for arg in child.children:
                    if arg.type not in cfg.bare_reference_node_types:
                        continue
                    owner = _find_owner(node, cfg, src)
                    used = _node_text(arg, src)
                    if owner and used and used != owner and used in known_names:
                        edges.append({
                            "kind": CALL_ARGUMENT_REFERENCE, "user_file": file, "user_symbol": owner,
                            "used_file": file, "used_symbol": used, "line": node.start_point[0] + 1,
                        })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


def bridge_layer2_treesitter_edges(
    *, repo: str, repo_root: Path, changed_files: list[str], symbol_index: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract, citation-verify, and return new `usage_edges`-shaped dicts
    for TypeScript/Java/Go declaration-reference relations, scoped to the
    changed files in this diff. Returns `[]` when disabled, when
    `tree_sitter` isn't installed, or when no supported-language files
    changed."""
    if not layer2_generic_edges_enabled():
        return []
    targets = [
        (f, _EXT_TO_LANGUAGE[Path(f).suffix.lower()])
        for f in changed_files if Path(f).suffix.lower() in _EXT_TO_LANGUAGE
    ]
    if not targets:
        return []
    get_parser = _get_tree_sitter()
    if get_parser is None:
        return []

    by_path = files_by_path(symbol_index)
    # Known-symbol names sourced from the ALREADY-COMPUTED, repo-wide
    # symbol index -- not a re-parse of every file in the repo -- so a
    # cross-file type reference (an imported interface, say) still counts
    # as "known" without a second full-repo scan. This is the mandatory
    # noise filter: a real false positive (`isEmpty -> item`, a parameter
    # name) was found and fixed by adding this exact filter during the
    # research round this is ported from.
    known_names: set[str] = set()
    for file_item in by_path.values():
        for entry in file_item.get("symbols", []) or []:
            name = entry.get("name")
            if isinstance(name, str) and name:
                known_names.add(name)

    source_cache: dict[str, list[str]] = {}
    candidates: list[dict[str, Any]] = []
    for rel, language in targets:
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            candidates.extend(_extract_for_file(
                source, file=rel, language=language, known_names=known_names, get_parser=get_parser,
            ))
        except Exception:
            continue

    verified: list[dict[str, Any]] = []
    for edge in candidates:
        if not citation_verified(edge, repo_root, source_cache):
            continue
        verified.append({
            "repo": repo,
            "user_file": edge["user_file"], "user_symbol": edge["user_symbol"],
            "user_qualified_name": qualified_name_for(edge["user_file"], edge["user_symbol"], by_path),
            "used_file": edge["used_file"], "used_symbol": edge["used_symbol"],
            "used_qualified_name": qualified_name_for(edge["used_file"], edge["used_symbol"], by_path),
            "source": LAYER2_SOURCE,
        })
    return verified
