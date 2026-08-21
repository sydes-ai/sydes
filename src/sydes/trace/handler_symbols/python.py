"""Python handler symbol extractor adapter.

Implements the same `HandlerSymbolExtractor` contract as the JS/TS adapter, so
Python participates in the existing shared symbol index, handler resolution, and
call-following machinery rather than needing a traversal of its own.

Extraction is AST-based, so line spans are exact. Supplying a correct
`end_line` is what lets the shared body slicer skip its brace-scanning
fallback, which has no meaning in an indentation-delimited language.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sydes.trace.handler_symbols.common import FileSymbols

_INIT_MODULE = "__init__.py"


def _module_candidates(module: str) -> list[str]:
    """Repo-relative file candidates for a dotted module path."""
    parts = [part for part in module.split(".") if part]
    if not parts:
        return []
    base = "/".join(parts)
    return [f"{base}.py", f"{base}/{_INIT_MODULE}"]


def resolve_python_import(
    repo_root: Path, importer_relative_path: str, module: str | None, level: int = 0
) -> str | None:
    """Resolve a Python import to a repo-relative file path when it is local.

    Handles absolute imports rooted at the repository and explicit relative
    imports. An import that resolves outside the repository is third-party and
    correctly yields None.
    """
    importer = Path(importer_relative_path)
    candidates: list[str] = []

    if level and level > 0:
        base = importer.parent
        for _ in range(level - 1):
            base = base.parent
        prefix = base.as_posix()
        suffixes = _module_candidates(module) if module else [_INIT_MODULE]
        for suffix in suffixes:
            candidates.append(suffix if prefix in {"", "."} else f"{prefix}/{suffix}")
    elif module:
        candidates.extend(_module_candidates(module))
        # A package layout may place source under a single top-level directory.
        top = importer.parts[0] if len(importer.parts) > 1 else None
        if top:
            candidates.extend(f"{top}/{item}" for item in _module_candidates(module))

    for candidate in candidates:
        normalized = Path(candidate).as_posix()
        if (repo_root / normalized).is_file():
            return normalized
    return None


def _is_exported(name: str) -> bool:
    """Public-by-convention: Python has no export keyword."""
    return not name.startswith("_")


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a compact parameter signature for evidence."""
    params = [arg.arg for arg in node.args.args]
    if node.args.vararg:
        params.append(f"*{node.args.vararg.arg}")
    params.extend(arg.arg for arg in node.args.kwonlyargs)
    if node.args.kwarg:
        params.append(f"**{node.args.kwarg.arg}")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(params)})"


def _decorators(node: ast.AST) -> list[str]:
    """Render decorator expressions as source-like text."""
    rendered: list[str] = []
    for item in getattr(node, "decorator_list", []) or []:
        try:
            rendered.append(ast.unparse(item))
        except Exception:  # noqa: BLE001 - decorator rendering is best-effort
            continue
    return rendered


class PythonHandlerSymbolExtractor:
    """Python implementation of the generic handler symbol extractor interface."""

    language = "python"
    extensions = {".py"}

    def extract_file(self, repo_root: Path, relative_path: str, text: str) -> FileSymbols:
        """Extract symbols, imports, and exports from one Python source file."""
        imports: list[dict] = []
        exports: list[dict] = []
        symbols: list[dict] = []

        try:
            tree = ast.parse(text)
        except SyntaxError:
            # A file that does not parse contributes no symbols, but the caller
            # still records that it was seen.
            return FileSymbols(
                path=relative_path,
                language=self.language,
                imports=imports,
                exports=exports,
                symbols=symbols,
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    imports.append(
                        {
                            "local": local,
                            "imported": alias.name,
                            "source": alias.name,
                            "kind": "module",
                            "resolved_file": resolve_python_import(
                                repo_root, relative_path, alias.name, 0
                            ),
                        }
                    )
            elif isinstance(node, ast.ImportFrom):
                level = node.level or 0
                module_file = resolve_python_import(repo_root, relative_path, node.module, level)
                for alias in node.names:
                    local = alias.asname or alias.name
                    # `from package import module` binds a module, so prefer the
                    # submodule's own file when one exists.
                    submodule = resolve_python_import(
                        repo_root,
                        relative_path,
                        f"{node.module}.{alias.name}" if node.module else alias.name,
                        level,
                    )
                    imports.append(
                        {
                            "local": local,
                            "imported": alias.name,
                            "source": node.module or ".",
                            "kind": "named",
                            "resolved_file": submodule or module_file,
                        }
                    )

        def _add_function(
            node: ast.FunctionDef | ast.AsyncFunctionDef, parent: str | None
        ) -> None:
            start = node.lineno
            end = node.end_lineno or start
            name = node.name
            # A signature may span several lines; the first body statement is
            # where the body actually begins.
            body_start = node.body[0].lineno if node.body else start + 1
            record: dict = {
                "name": name,
                "kind": "class_method" if parent else "function",
                "language": self.language,
                "file": relative_path,
                "line": start,
                "start_line": start,
                "body_start_line": body_start,
                "end_line": end,
                "signature": _signature(node),
                "async": isinstance(node, ast.AsyncFunctionDef),
                "exported": _is_exported(name),
                "export_kind": "named" if _is_exported(name) else None,
                "decorators": _decorators(node),
            }
            if parent:
                record["parent"] = parent
                record["qualified_name"] = f"{parent}.{name}"
            symbols.append(record)
            if _is_exported(name) and parent is None:
                exports.append({"kind": "named", "symbol": name})

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                start = node.lineno
                end = node.end_lineno or start
                symbols.append(
                    {
                        "name": node.name,
                        "kind": "class",
                        "language": self.language,
                        "file": relative_path,
                        "line": start,
                        "start_line": start,
                        "end_line": end,
                        "exported": _is_exported(node.name),
                        "export_kind": "named" if _is_exported(node.name) else None,
                        "decorators": _decorators(node),
                    }
                )
                if _is_exported(node.name):
                    exports.append({"kind": "named", "symbol": node.name})
                for child in node.body:
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        _add_function(child, node.name)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                _add_function(node, None)

        return FileSymbols(
            path=relative_path,
            language=self.language,
            imports=imports,
            exports=exports,
            symbols=symbols,
        )
