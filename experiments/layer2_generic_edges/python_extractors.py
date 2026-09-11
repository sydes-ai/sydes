"""Layer 2: generic, syntactic declaration-reference extractors for Python.

Three edge kinds, each a pure AST-shape recognizer with no framework or
symbol name anywhere in the algorithm:

  call_argument_reference    -- a bare name passed as an argument to ANY
                                 call expression, attributed to the nearest
                                 enclosing "owner" (a module-level/class-body
                                 assignment target, or an enclosing function/
                                 class, or -- for a bare call statement
                                 inside a function body -- that function).
                                 Generalizes and subsumes the narrower
                                 "assignment_argument_reference" extractor
                                 from the first experiment: it now also
                                 catches a bare registration-style call
                                 statement with no assignment at all (e.g.
                                 `app.add_middleware(SomeClass)`).

  class_field_type_reference -- a class-body annotated assignment
                                 (`field: SomeType`) references a locally-
                                 defined name in its annotation.

  function_parameter_type_reference -- a function parameter's annotation
                                 references a name. Same-file resolution is
                                 built directly; CROSS-file resolution
                                 reuses Sydes' own already-computed import
                                 records (`resolved_file` on each file's
                                 `imports` entries, from
                                 `discover.file_facts.build_structural_index`)
                                 rather than reimplementing import
                                 resolution -- the same principle
                                 `trace.call_follower._resolve_call` already
                                 applies to CALL edges, applied here to
                                 parameter-type edges instead.

Not wired into any production path. Not imported by normal Sydes runtime.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

CALL_ARGUMENT_REFERENCE = "call_argument_reference"
CLASS_FIELD_TYPE_REFERENCE = "class_field_type_reference"
FUNCTION_PARAMETER_TYPE_REFERENCE = "function_parameter_type_reference"


def _local_definitions(tree: ast.Module) -> set[str]:
    """Every name this file gives meaning to: function/class defs, AND
    assignment targets (module-, class-, or function-scoped) -- a type
    alias (`Duration = Annotated[...]`) is a plain ast.Assign, not a def,
    and must still count as a locally-defined name or every declaration-
    reference extractor below silently misses it."""
    defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    return defs | assigned


def extract_class_field_type_references(source: str, *, file: str) -> list[dict]:
    tree = ast.parse(source)
    local_names = _local_definitions(tree)
    edges: list[dict] = []
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


def extract_function_parameter_type_references(source: str, *, file: str) -> list[dict]:
    """Same-file resolution only -- see module docstring; cross-file
    resolution is a separate pass in `resolve_cross_file_parameter_types`."""
    tree = ast.parse(source)
    local_names = _local_definitions(tree)
    edges: list[dict] = []
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
    """Walk outward from a Call node's ancestor chain to find the nearest
    "owner": an assignment target (module- or class-body-level), or the
    nearest enclosing function/class. Returns (owner_name, owner_lineno) or
    None if no sensible owner exists (e.g. a call inside another call's
    argument list with nothing but expression context around it -- rare,
    and honestly left unattributed rather than guessed)."""
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


def extract_call_argument_references(source: str, *, file: str) -> list[dict]:
    tree = ast.parse(source)
    local_names = _local_definitions(tree)
    edges: list[dict] = []

    # Build a parent-chain map via a manual walk (ast.walk loses ancestry).
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
        owner_name, owner_line = owner
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


def extract_all_same_file(source: str, *, file: str) -> list[dict]:
    return (
        extract_class_field_type_references(source, file=file)
        + extract_function_parameter_type_references(source, file=file)
        + extract_call_argument_references(source, file=file)
    )


# ---------------------------------------------------------------------------
# Cross-file resolution for function_parameter_type_reference, reusing
# Sydes' own already-computed import records rather than a new resolver.
# ---------------------------------------------------------------------------


def resolve_cross_file_parameter_types(repo_root: Path) -> list[dict]:
    """For every function parameter annotation across the repo whose name is
    NOT locally defined in that file, check the file's own `imports` (as
    already extracted by Sydes' native structural index, which resolves
    `from X import Y` to a real file path) and emit a resolved edge when the
    import target is unambiguous."""
    from sydes.core.models import RepoRef
    from sydes.discover.file_facts import build_structural_index

    index = build_structural_index([RepoRef(name="app", root=str(repo_root))], persist=False)
    batch = index.handler_symbol_batch or {}

    imports_by_file: dict[str, list[dict]] = {}
    defined_names_by_file: dict[str, set[str]] = {}
    for repo_payload in batch.get("repos", []) or []:
        for file_item in repo_payload.get("files", []) or []:
            path = file_item.get("path")
            imports_by_file[path] = file_item.get("imports", []) or []
            defined_names_by_file[path] = {
                s.get("name") for s in file_item.get("symbols", []) or []
                if s.get("kind") in ("function", "class", "class_method")
            }

    edges: list[dict] = []
    for repo_payload in batch.get("repos", []) or []:
        for file_item in repo_payload.get("files", []) or []:
            path = file_item.get("path")
            if not path or not path.endswith(".py"):
                continue
            abs_path = repo_root / path
            if not abs_path.is_file():
                continue
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            local_names = defined_names_by_file.get(path, set())
            tree = ast.parse(source)
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
                            continue  # same-file case already covered elsewhere
                        import_entry = next(
                            (imp for imp in imports_by_file.get(path, [])
                             if imp.get("local") == name and imp.get("resolved_file")),
                            None,
                        )
                        if import_entry is None:
                            continue
                        edges.append({
                            "kind": FUNCTION_PARAMETER_TYPE_REFERENCE,
                            "user_file": path, "user_symbol": node.name,
                            "used_file": import_entry["resolved_file"], "used_symbol": name,
                            "line": node.lineno,
                            "resolution": "cross_file_import",
                        })
    return edges
