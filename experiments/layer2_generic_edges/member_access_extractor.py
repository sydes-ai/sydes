"""Layer 2: generic member/attribute-access extraction with STRICTLY
BOUNDED receiver-type resolution.

New edge kind: READS_MEMBER (function F reads `receiver.member`, where
`receiver` resolved to a known local/imported class/type T) -> emitted as
F --READS_MEMBER--> T.

Receiver resolution uses ONLY direct, same-scope evidence, in this order:

  A. a parameter's own type annotation
  B. a local variable's own type annotation
  C. a local variable directly assigned a bare constructor call to a
     known class (`x = ClassName(...)`)
  D. a name imported (module-level OR function-local -- Python allows a
     deferred import inside a function body, a real pattern found in the
     actual Settings case) whose ORIGINAL BINDING, in the file it comes
     from, is itself resolved by (B) or (C) above -- exactly ONE hop of
     cross-file resolution, never chased further.

Explicitly NOT done, by design, per the experiment's own bound: no alias
chasing beyond the one import hop above, no interprocedural data-flow
tracking (a receiver passed as an argument into another function is never
followed), no reassignment tracking across branches, no points-to
analysis. If a receiver's binding is ambiguous (more than one plausible
type) or not found through any of the four sources above, the access is
reported as UNKNOWN -- no edge is fabricated. There is no rule anywhere in
this file keyed on the literal name "settings", "Settings", or any other
symbol/framework name.

Not wired into any production path.
"""

from __future__ import annotations

import ast
from pathlib import Path

READS_MEMBER = "reads_member"

RECEIVER_UNKNOWN = "unknown"
RECEIVER_AMBIGUOUS = "ambiguous"


def _known_class_names(tree: ast.Module) -> set[str]:
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _annotation_name(annotation: ast.AST) -> str | None:
    """A bare `Name` annotation only -- `Optional[X]`/`List[X]`-wrapped
    annotations are deliberately not unwrapped here: for a RECEIVER
    (something member access is performed on), the direct, unwrapped type
    is what actually matters, and guessing through a wrapper risks
    resolving to the wrapper itself by mistake."""
    if isinstance(annotation, ast.Name):
        return annotation.id
    return None


def _resolve_receiver_in_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef, receiver_name: str, known_classes: set[str],
) -> tuple[str, str | None]:
    """Try sources A (parameter annotation), B (local annotation), C
    (local constructor assignment) within one function's own body/params.
    Returns (status, resolved_class_or_None) where status is "resolved",
    "unknown", or "ambiguous"."""
    candidates: set[str] = set()

    all_args = list(func.args.args) + list(func.args.kwonlyargs) + list(func.args.posonlyargs)
    for arg in all_args:
        if arg.arg == receiver_name and arg.annotation is not None:
            name = _annotation_name(arg.annotation)
            if name and name in known_classes:
                candidates.add(name)

    for node in ast.walk(func):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == receiver_name:
            name = _annotation_name(node.annotation)
            if name and name in known_classes:
                candidates.add(name)
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if receiver_name in targets and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                if node.value.func.id in known_classes:
                    candidates.add(node.value.func.id)

    if not candidates:
        return RECEIVER_UNKNOWN, None
    if len(candidates) > 1:
        return RECEIVER_AMBIGUOUS, None
    return "resolved", next(iter(candidates))


def _local_import_source_module(func: ast.FunctionDef | ast.AsyncFunctionDef, receiver_name: str) -> str | None:
    """A function-local `from X import receiver_name` -- the deferred-
    import pattern the real Settings case actually uses. Returns the
    dotted module path (relative dots preserved) or None."""
    for node in ast.walk(func):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                if bound_name == receiver_name:
                    return ("." * node.level) + (node.module or "")
    return None


def _module_relative_to_file(current_file: Path, module_path: str) -> Path | None:
    """Best-effort resolution of a relative import (`..core.config`) to a
    real file, given the file doing the importing. Same-repo only; no
    package/site-packages resolution attempted -- out of scope."""
    if not module_path.startswith("."):
        return None  # only relative imports are attempted; absolute
        # package imports would need a real import-resolution pass, not a
        # one-hop bounded lookup, and are left UNKNOWN rather than guessed.
    dots = len(module_path) - len(module_path.lstrip("."))
    remainder = module_path[dots:]
    base = current_file.parent
    for _ in range(dots - 1):
        base = base.parent
    if remainder:
        base = base / Path(*remainder.split("."))
    candidate = base.with_suffix(".py")
    if candidate.is_file():
        return candidate
    candidate_init = base / "__init__.py"
    if candidate_init.is_file():
        return candidate_init
    return None


def extract_member_access_edges(source: str, *, file: str, repo_root: Path) -> list[dict]:
    """For every `receiver.member` read inside a function body, attempt
    STRICTLY BOUNDED receiver resolution (sources A-D above) and emit a
    READS_MEMBER edge only when resolution is unambiguous. Every result
    (resolved or not) is returned with an explicit status so a caller can
    see the UNKNOWN/AMBIGUOUS cases too, not just the successes."""
    tree = ast.parse(source)
    known_classes = _known_class_names(tree)
    results: list[dict] = []

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seen_in_func: set[tuple[str, str]] = set()
        for node in ast.walk(func):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            receiver_name = node.value.id
            member_name = node.attr
            if (receiver_name, member_name) in seen_in_func:
                continue
            seen_in_func.add((receiver_name, member_name))

            status, resolved = _resolve_receiver_in_function(func, receiver_name, known_classes)
            provenance = "python_ast_same_scope"

            if status == "unknown":
                # Source D: a function-local (or module-level within this
                # same file) import -- one hop, cross-file, into the
                # ORIGINAL file, checked with the SAME bounded sources.
                module_path = _local_import_source_module(func, receiver_name)
                if module_path is not None:
                    target_file = _module_relative_to_file(repo_root / file, module_path)
                    if target_file is not None:
                        try:
                            target_source = target_file.read_text(encoding="utf-8", errors="replace")
                            target_tree = ast.parse(target_source)
                        except (OSError, SyntaxError):
                            target_tree = None
                        if target_tree is not None:
                            target_known_classes = _known_class_names(target_tree)
                            module_candidates: set[str] = set()
                            for mod_node in ast.walk(target_tree):
                                if (
                                    isinstance(mod_node, ast.Assign)
                                    and any(isinstance(t, ast.Name) and t.id == receiver_name for t in mod_node.targets)
                                    and isinstance(mod_node.value, ast.Call)
                                    and isinstance(mod_node.value.func, ast.Name)
                                    and mod_node.value.func.id in target_known_classes
                                ):
                                    module_candidates.add(mod_node.value.func.id)
                                if (
                                    isinstance(mod_node, ast.AnnAssign)
                                    and isinstance(mod_node.target, ast.Name) and mod_node.target.id == receiver_name
                                ):
                                    name = _annotation_name(mod_node.annotation)
                                    if name and name in target_known_classes:
                                        module_candidates.add(name)
                            if len(module_candidates) == 1:
                                status, resolved = "resolved", next(iter(module_candidates))
                                provenance = "python_ast_one_hop_import"
                            elif len(module_candidates) > 1:
                                status = "ambiguous"

            entry = {
                "kind": READS_MEMBER,
                "file": file,
                "function": func.name,
                "receiver": receiver_name,
                "member": member_name,
                "line": node.lineno,
                "status": status,
                "resolved_type": resolved,
                "provenance": provenance if status == "resolved" else None,
            }
            results.append(entry)
    return results


def edges_only(entries: list[dict]) -> list[dict]:
    """Just the successfully-resolved subset, in the plain (user_symbol,
    used_symbol) edge shape the rest of this experiment's tooling uses."""
    return [
        {
            "kind": READS_MEMBER,
            "user_file": e["file"], "user_symbol": e["function"],
            "used_file": None, "used_symbol": e["resolved_type"],
            "line": e["line"], "provenance": e["provenance"],
        }
        for e in entries if e["status"] == "resolved"
    ]
