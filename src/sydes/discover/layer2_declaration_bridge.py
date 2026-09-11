"""Layer 2: generic, syntactic declaration-reference edges, bridged into
`StructuralFacts.usage_edges` alongside `member_call_bridge.py` and
`interface_bridge.py` — same discipline (a synthetic edge added only when
unambiguous, never guessed), same additive-only contract.

Ported from `experiments/layer2_generic_edges/python_extractors.py` and
`experiments/claim_verifier/verifier.py`, validated there against a real
production repo (Kokoro-FastAPI) across three research rounds. Three
relation kinds only — `call_argument_reference`, `class_field_type_reference`,
`function_parameter_type_reference` — a name passed as a call argument, a
class field annotation, or a function parameter annotation, referencing
another symbol this file (or a one-hop import) already defines. No
framework or library name anywhere in the algorithm. `reads_member`
(member-access + receiver-type resolution) is deliberately NOT included
this round — its `used_symbol` is a resolved type, not literal text, and
carrying the citation text it actually needs is a larger schema change
left for a later increment.

Gated behind `SYDES_LAYER2_GENERIC_EDGES` (default off). Every candidate
edge is citation-verified — the cited file/line must actually contain the
claimed symbol as a whole identifier — before being admitted; an edge that
fails citation is silently dropped, never a diagnostic-only warning that
could be mistaken for admitted evidence.

Scoped to the changed Python files in the diff (plus, for cross-file
parameter-type resolution, the already-computed import records in
`structural.symbol_index` — no second full-repo AST sweep), matching the
existing "bounded neighborhood, not whole-repository" performance
discipline this integration point (`_attach_bounded_graph_edges`) already
enforces for CALLS/USAGE edges generally.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from sydes.discover.layer2_shared import (
    LAYER2_ENV_VAR,
    LAYER2_SOURCE,
    citation_verified,
    defining_file_for,
    files_by_path,
    layer2_generic_edges_enabled,
    qualified_name_for,
)

CALL_ARGUMENT_REFERENCE = "call_argument_reference"
CLASS_FIELD_TYPE_REFERENCE = "class_field_type_reference"
FUNCTION_PARAMETER_TYPE_REFERENCE = "function_parameter_type_reference"

_SYMBOL_KINDS = ("function", "class", "class_method")


# --- same-file extractors (ast-shape recognizers, no framework/library name) ----


def _local_definitions(tree: ast.Module) -> set[str]:
    """Every name this file gives meaning to: function/class defs, AND
    assignment targets -- a type alias (`Duration = Annotated[...]`) is a
    plain ast.Assign, not a def, and must still count or the extractors
    below silently miss it (a real bug found and fixed during the research
    round this was ported from)."""
    defs = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assigned = {
        t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
    }
    return defs | assigned


def _extract_class_field_type_references(source: str, tree: ast.Module, *, file: str) -> list[dict[str, Any]]:
    local_names = _local_definitions(tree)
    edges: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            for sub in ast.walk(stmt.annotation):
                if isinstance(sub, ast.Name) and sub.id in local_names and sub.id != node.name:
                    edges.append({
                        "kind": CLASS_FIELD_TYPE_REFERENCE,
                        "user_file": file, "user_symbol": node.name,
                        "used_file": file, "used_symbol": sub.id,
                        "line": stmt.lineno,
                    })
    return edges


def _extract_function_parameter_type_references(source: str, tree: ast.Module, *, file: str) -> list[dict[str, Any]]:
    local_names = _local_definitions(tree)
    edges: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        all_args = list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
        for arg in all_args:
            if arg.annotation is None:
                continue
            for sub in ast.walk(arg.annotation):
                if isinstance(sub, ast.Name) and sub.id in local_names and sub.id != node.name:
                    edges.append({
                        "kind": FUNCTION_PARAMETER_TYPE_REFERENCE,
                        "user_file": file, "user_symbol": node.name,
                        "used_file": file, "used_symbol": sub.id,
                        "line": node.lineno,
                    })
    return edges


def _owner_for_call(node: ast.AST, ancestors: list[ast.AST]) -> tuple[str, int] | None:
    for anc in reversed(ancestors):
        if isinstance(anc, ast.Assign):
            targets = [t.id for t in anc.targets if isinstance(t, ast.Name)]
            if targets:
                return targets[0], anc.lineno
        if isinstance(anc, ast.AnnAssign) and isinstance(anc.target, ast.Name):
            return anc.target.id, anc.lineno
        if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return anc.name, anc.lineno
    return None


def _extract_call_argument_references(source: str, tree: ast.Module, *, file: str) -> list[dict[str, Any]]:
    local_names = _local_definitions(tree)
    edges: list[dict[str, Any]] = []

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def ancestors_of(node: ast.AST) -> list[ast.AST]:
        chain = []
        current = node
        while current in parents:
            current = parents[current]
            chain.append(current)
        return list(reversed(chain))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        referenced_args = [
            a.id for a in node.args if isinstance(a, ast.Name) and a.id in local_names
        ] + [
            kw.value.id for kw in node.keywords
            if isinstance(kw.value, ast.Name) and kw.value.id in local_names
        ]
        if not referenced_args:
            continue
        owner = _owner_for_call(node, ancestors_of(node))
        if owner is None:
            continue
        owner_name, _owner_line = owner
        for used in referenced_args:
            if used == owner_name:
                continue
            edges.append({
                "kind": CALL_ARGUMENT_REFERENCE,
                "user_file": file, "user_symbol": owner_name,
                "used_file": file, "used_symbol": used,
                "line": node.lineno,
            })
    return edges


# --- cross-file resolution for function_parameter_type_reference, reusing --
# --- Sydes' own already-computed symbol_index (no second full-repo scan) ---


def _extract_cross_file_parameter_type_references(
    source: str, tree: ast.Module, *, file: str, files_by_path: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    file_item = files_by_path.get(file) or {}
    local_names = {
        s.get("name") for s in file_item.get("symbols", []) or []
        if s.get("kind") in _SYMBOL_KINDS
    }
    imports = file_item.get("imports", []) or []
    edges: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        all_args = list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
        for arg in all_args:
            if arg.annotation is None:
                continue
            for sub in ast.walk(arg.annotation):
                if not isinstance(sub, ast.Name):
                    continue
                name = sub.id
                if name in local_names:
                    continue  # same-file case already covered above
                import_entry = next(
                    (imp for imp in imports if imp.get("local") == name and imp.get("resolved_file")),
                    None,
                )
                if import_entry is None:
                    continue
                resolved_file = str(import_entry["resolved_file"])
                defining_file = defining_file_for(resolved_file, name, files_by_path)
                edges.append({
                    "kind": FUNCTION_PARAMETER_TYPE_REFERENCE,
                    "user_file": file, "user_symbol": node.name,
                    "used_file": defining_file, "used_symbol": name,
                    "line": node.lineno,
                    "resolution": "cross_file_import",
                })
    return edges


# --- entry point ------------------------------------------------------------


def bridge_layer2_declaration_reference_edges(
    *, repo: str, repo_root: Path, changed_python_files: list[str], symbol_index: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract, citation-verify, and return new `usage_edges`-shaped dicts
    for the three declaration-reference relation kinds, scoped to the
    changed Python files in this diff. Returns `[]` when disabled or no
    Python files changed -- always additive, never a mutation of any
    existing fact."""
    if not layer2_generic_edges_enabled():
        return []
    py_files = [f for f in changed_python_files if f.endswith(".py")]
    if not py_files:
        return []

    by_path = files_by_path(symbol_index)
    source_cache: dict[str, list[str]] = {}
    candidates: list[dict[str, Any]] = []

    for rel in py_files:
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        candidates.extend(_extract_class_field_type_references(source, tree, file=rel))
        candidates.extend(_extract_function_parameter_type_references(source, tree, file=rel))
        candidates.extend(_extract_call_argument_references(source, tree, file=rel))
        candidates.extend(
            _extract_cross_file_parameter_type_references(source, tree, file=rel, files_by_path=by_path)
        )

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
