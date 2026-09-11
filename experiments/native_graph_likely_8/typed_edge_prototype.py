"""EXPERIMENTAL, minimal typed-edge extractor -- Phase 4.

Generic, syntactic-level extraction only: finds a bare, locally-defined
name referenced as an argument somewhere on the right-hand side of a
MODULE-LEVEL assignment, and emits a `usage_edge`-shaped dict (the exact
`StructuralFacts.usage_edges` shape Sydes already defines) from the
assignment target to that name.

No framework knowledge anywhere in this file: no "Annotated", no
"AfterValidator", no "pydantic", no "_within_duration_ceiling". The
algorithm only understands "assignment target" and "name referenced
inside the assignment's expression tree" -- both generic Python AST
concepts. This is deliberately narrower than a full field-type/validator
model (see the module docstring notes in graph_inventory.md, Phase 4
section, for what a complete version would still need): it captures
ONE hop -- "a locally-defined callable named inside a module-level
assignment's expression" -- which is exactly, and only, the hop that
Sydes' and CBM's real extractors were both empirically found (Phase 2)
to be missing nodes for.

Not wired into any production path. Not imported by normal Sydes runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

USAGE_KIND_ASSIGNMENT_ARGUMENT_REFERENCE = "assignment_argument_reference"


def extract_module_level_assignment_argument_usages(source: str, *, file: str) -> list[dict]:
    """Find every module-level `NAME = <expr containing other bare Names>`
    and emit one usage edge per distinct local name referenced in the RHS
    that could plausibly be a locally-defined callable/symbol (excludes
    builtins/dunders by a generic exclusion list, not a framework list).
    """
    tree = ast.parse(source)
    local_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    edges: list[dict] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not targets:
            continue
        referenced: set[str] = set()
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Name) and sub.id in local_names:
                referenced.add(sub.id)
        for target in targets:
            for used in referenced:
                if used == target:
                    continue
                edges.append({
                    "repo": None,  # filled in by the caller with the real repo name
                    "user_file": file,
                    "user_symbol": target,
                    "user_qualified_name": target,
                    "used_file": file,
                    "used_symbol": used,
                    "used_qualified_name": used,
                    "source": USAGE_KIND_ASSIGNMENT_ARGUMENT_REFERENCE,
                    "line": node.lineno,
                })
    return edges


def extract_class_field_type_usages(source: str, *, file: str) -> list[dict]:
    """Find every class-body annotated assignment (`field: SomeType` or
    `field: Optional[SomeType]`/`field: SomeWrapper[SomeType]`) and emit a
    usage edge from the CLASS to each locally-defined name appearing in the
    annotation. Generic AST pattern (`ast.AnnAssign` inside a `ClassDef`),
    no framework-specific handling of what the annotation wrapper is."""
    tree = ast.parse(source)
    local_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } | {
        t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
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
                        "repo": None,
                        "user_file": file,
                        "user_symbol": node.name,
                        "user_qualified_name": node.name,
                        "used_file": file,
                        "used_symbol": sub.id,
                        "used_qualified_name": sub.id,
                        "source": "class_field_type_reference",
                        "line": stmt.lineno,
                    })
    return edges


def extract_function_parameter_type_usages(source: str, *, file: str) -> list[dict]:
    """Find every function parameter annotation (`def f(x: SomeType)`) and
    emit a usage edge from the FUNCTION to each locally-defined name in the
    annotation. Same generic AST-pattern approach as the class-field
    extractor above, applied to `ast.arg.annotation` instead of
    `ast.AnnAssign` -- still no framework-specific handling."""
    tree = ast.parse(source)
    local_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
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
                        "repo": None,
                        "user_file": file,
                        "user_symbol": node.name,
                        "user_qualified_name": node.name,
                        "used_file": file,
                        "used_symbol": sub.id,
                        "used_qualified_name": sub.id,
                        "source": "function_parameter_type_reference",
                        "line": node.lineno,
                    })
    return edges


if __name__ == "__main__":
    import json
    import sys

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/Users/ksnaik/sample_repos/Kokoro-FastAPI/api/src/structures/schemas.py"
    )
    text = path.read_text(encoding="utf-8")
    edges = extract_module_level_assignment_argument_usages(text, file=str(path))
    edges += extract_class_field_type_usages(text, file=str(path))
    edges += extract_function_parameter_type_usages(text, file=str(path))
    print(json.dumps(edges, indent=2))
