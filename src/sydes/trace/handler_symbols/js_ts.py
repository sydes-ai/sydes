"""JavaScript/TypeScript handler symbol extractor adapter."""

from __future__ import annotations

import re
from pathlib import Path

from sydes.trace.handler_symbols.common import FileSymbols
from sydes.trace.handler_symbols.resolver import resolve_local_import

_IMPORT_DEFAULT_RE = re.compile(
    r"^\s*import\s+(?P<local>[A-Za-z_]\w*)\s+from\s+['\"](?P<source>[^'\"]+)['\"]"
)
_IMPORT_NAMESPACE_RE = re.compile(
    r"^\s*import\s+\*\s+as\s+(?P<local>[A-Za-z_]\w*)\s+from\s+['\"](?P<source>[^'\"]+)['\"]"
)
_IMPORT_NAMED_RE = re.compile(
    r"^\s*import\s*\{(?P<named>[^}]+)\}\s*from\s*['\"](?P<source>[^'\"]+)['\"]"
)
_IMPORT_DEFAULT_NAMED_RE = re.compile(
    r"^\s*import\s+(?P<default>[A-Za-z_]\w*)\s*,\s*\{(?P<named>[^}]+)\}\s*from\s*['\"](?P<source>[^'\"]+)['\"]"
)
_REQUIRE_DEFAULT_RE = re.compile(
    r"^\s*(?:const|let|var)\s+(?P<local>[A-Za-z_]\w*)\s*=\s*require\(\s*['\"](?P<source>[^'\"]+)['\"]\s*\)"
)

_EXPORT_DEFAULT_CLASS_RE = re.compile(r"^\s*export\s+default\s+class\s+(?P<name>[A-Za-z_]\w*)")
_EXPORT_CLASS_RE = re.compile(r"^\s*export\s+class\s+(?P<name>[A-Za-z_]\w*)")
_CLASS_RE = re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)")
_EXPORT_DEFAULT_SYMBOL_RE = re.compile(r"^\s*export\s+default\s+(?P<symbol>[A-Za-z_]\w*)\s*;?")
_EXPORT_DEFAULT_FUNCTION_RE = re.compile(
    r"^\s*export\s+default\s+(?P<async>async\s+)?function\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^\)]*)\)"
)
_EXPORT_FUNCTION_RE = re.compile(
    r"^\s*export\s+(?P<async>async\s+)?function\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^\)]*)\)"
)
_FUNCTION_RE = re.compile(
    r"^\s*(?P<async>async\s+)?function\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^\)]*)\)"
)
_EXPORT_CONST_ARROW_RE = re.compile(
    r"^\s*export\s+const\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<async>async\s+)?\((?P<params>[^\)]*)\)\s*=>"
)
_CONST_ARROW_RE = re.compile(
    r"^\s*const\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<async>async\s+)?\((?P<params>[^\)]*)\)\s*=>"
)
_CONST_FUNCTION_RE = re.compile(
    r"^\s*const\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<async>async\s+)?function\s*\((?P<params>[^\)]*)\)\s*\{"
)
_EXPORT_NAMED_RE = re.compile(r"^\s*export\s*\{(?P<named>[^}]+)\}\s*;?")
_MODULE_EXPORT_RE = re.compile(r"^\s*module\.exports\s*=\s*(?P<symbol>[A-Za-z_]\w*)\s*;?")
_EXPORTS_ASSIGN_RE = re.compile(
    r"^\s*exports\.(?P<export_name>[A-Za-z_]\w*)\s*=\s*(?P<symbol>[A-Za-z_]\w*)\s*;?"
)

_METHOD_RE = re.compile(
    r"^\s*(?P<prefix>(?:(?:public|private|protected|readonly|static|async)\s+)*)"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^\)]*)\)\s*\{"
)
_METHOD_SIGNATURE_RE = re.compile(
    r"^\s*(?P<prefix>(?:(?:public|private|protected|readonly|static|async|override)\s+)*)"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*?)\)\s*(?::\s*[^={]+)?\s*\{",
    re.DOTALL,
)
_METHOD_HEADER_START_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|readonly|static|async|override)\s+)*[A-Za-z_]\w*\s*\("
)
_DECORATOR_RE = re.compile(r"^\s*@(?P<name>[A-Za-z_]\w*)")


def _parse_named_items(named_chunk: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for raw in named_chunk.split(","):
        part = raw.strip()
        if not part:
            continue
        if " as " in part:
            imported, local = [item.strip() for item in part.split(" as ", 1)]
        else:
            imported = part
            local = part
        if imported and local:
            items.append((imported, local))
    return items


def _count_braces(line: str) -> tuple[int, int]:
    return line.count("{"), line.count("}")


def _build_import(
    repo_root: Path, relative_path: str, local: str, imported: str, source: str, kind: str
) -> dict:
    return {
        "local": local,
        "imported": imported,
        "source": source,
        "kind": kind,
        "resolved_file": resolve_local_import(repo_root, relative_path, source),
    }


class JsTsHandlerSymbolExtractor:
    """JS/TS implementation of the generic handler symbol extractor interface."""

    language = "javascript_typescript"
    extensions = {".ts", ".tsx", ".js", ".jsx"}

    def _extract_imports(
        self, repo_root: Path, relative_path: str, raw_line: str
    ) -> list[dict]:
        imports: list[dict] = []
        default_named_match = _IMPORT_DEFAULT_NAMED_RE.search(raw_line)
        if default_named_match:
            source = default_named_match.group("source")
            imports.append(
                _build_import(
                    repo_root,
                    relative_path,
                    default_named_match.group("default"),
                    "default",
                    source,
                    "default",
                )
            )
            for imported, local in _parse_named_items(default_named_match.group("named")):
                imports.append(
                    _build_import(repo_root, relative_path, local, imported, source, "named")
                )
            return imports

        default_match = _IMPORT_DEFAULT_RE.search(raw_line)
        if default_match:
            source = default_match.group("source")
            imports.append(
                _build_import(
                    repo_root,
                    relative_path,
                    default_match.group("local"),
                    "default",
                    source,
                    "default",
                )
            )
        namespace_match = _IMPORT_NAMESPACE_RE.search(raw_line)
        if namespace_match:
            source = namespace_match.group("source")
            imports.append(
                _build_import(
                    repo_root,
                    relative_path,
                    namespace_match.group("local"),
                    "*",
                    source,
                    "namespace",
                )
            )
        named_match = _IMPORT_NAMED_RE.search(raw_line)
        if named_match:
            source = named_match.group("source")
            for imported, local in _parse_named_items(named_match.group("named")):
                imports.append(
                    _build_import(repo_root, relative_path, local, imported, source, "named")
                )
        require_match = _REQUIRE_DEFAULT_RE.search(raw_line)
        if require_match:
            source = require_match.group("source")
            imports.append(
                _build_import(
                    repo_root,
                    relative_path,
                    require_match.group("local"),
                    "default",
                    source,
                    "require",
                )
            )
        return imports

    def extract_file(self, repo_root: Path, relative_path: str, text: str) -> FileSymbols:
        ext = Path(relative_path).suffix.lower()
        language = "typescript" if ext in {".ts", ".tsx"} else "javascript"
        imports: list[dict] = []
        exports: list[dict] = []
        symbols: list[dict] = []

        class_stack: list[dict[str, int | str]] = []
        method_stack: list[dict[str, int]] = []
        pending_class: str | None = None
        pending_class_export_kind: str | None = None
        pending_decorators: list[str] = []
        pending_method_lines: list[str] = []
        pending_method_start_line: int | None = None
        brace_depth = 0

        for idx, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                open_braces, close_braces = _count_braces(raw_line)
                brace_depth += open_braces - close_braces
                continue
            if stripped.startswith("//") or stripped.startswith("*"):
                open_braces, close_braces = _count_braces(raw_line)
                brace_depth += open_braces - close_braces
                continue

            imports.extend(self._extract_imports(repo_root, relative_path, raw_line))

            export_default_class_match = _EXPORT_DEFAULT_CLASS_RE.search(raw_line)
            export_class_match = _EXPORT_CLASS_RE.search(raw_line)
            class_match = _CLASS_RE.search(raw_line)
            class_name = None
            class_export_kind = None
            if export_default_class_match:
                class_name = export_default_class_match.group("name")
                class_export_kind = "default"
                exports.append({"kind": "default", "symbol": class_name})
            elif export_class_match:
                class_name = export_class_match.group("name")
                class_export_kind = "named"
                exports.append({"kind": "named", "symbol": class_name})
            elif class_match:
                class_name = class_match.group("name")

            if class_name:
                symbols.append(
                    {
                        "name": class_name,
                        "kind": "class",
                        "language": language,
                        "file": relative_path,
                        "line": idx,
                        "start_line": idx,
                        "end_line": None,
                        "exported": class_export_kind is not None,
                        "export_kind": class_export_kind,
                    }
                )
                pending_class = class_name
                pending_class_export_kind = class_export_kind

            export_default_fn_match = _EXPORT_DEFAULT_FUNCTION_RE.search(raw_line)
            if export_default_fn_match:
                fn_name = export_default_fn_match.group("name")
                symbols.append(
                    {
                        "name": fn_name,
                        "kind": "function",
                        "language": language,
                        "file": relative_path,
                        "line": idx,
                        "start_line": idx,
                        "end_line": None,
                        "signature": f"function {fn_name}({export_default_fn_match.group('params').strip()})",
                        "async": bool(export_default_fn_match.group("async")),
                        "exported": True,
                        "export_kind": "default",
                    }
                )
                exports.append({"kind": "default", "symbol": fn_name})
            else:
                export_fn_match = _EXPORT_FUNCTION_RE.search(raw_line)
                if export_fn_match:
                    fn_name = export_fn_match.group("name")
                    symbols.append(
                        {
                            "name": fn_name,
                            "kind": "function",
                            "language": language,
                            "file": relative_path,
                            "line": idx,
                            "start_line": idx,
                            "end_line": None,
                            "signature": f"function {fn_name}({export_fn_match.group('params').strip()})",
                            "async": bool(export_fn_match.group("async")),
                            "exported": True,
                            "export_kind": "named",
                        }
                    )
                    exports.append({"kind": "named", "symbol": fn_name})
                else:
                    fn_match = _FUNCTION_RE.search(raw_line)
                    if fn_match:
                        fn_name = fn_match.group("name")
                        symbols.append(
                            {
                                "name": fn_name,
                                "kind": "function",
                                "language": language,
                                "file": relative_path,
                                "line": idx,
                                "start_line": idx,
                                "end_line": None,
                                "signature": f"function {fn_name}({fn_match.group('params').strip()})",
                                "async": bool(fn_match.group("async")),
                                "exported": False,
                                "export_kind": None,
                            }
                        )

            export_const_arrow_match = _EXPORT_CONST_ARROW_RE.search(raw_line)
            if export_const_arrow_match:
                fn_name = export_const_arrow_match.group("name")
                symbols.append(
                    {
                        "name": fn_name,
                        "kind": "function",
                        "language": language,
                        "file": relative_path,
                        "line": idx,
                        "start_line": idx,
                        "end_line": None,
                        "signature": f"const {fn_name}({export_const_arrow_match.group('params').strip()}) =>",
                        "async": bool(export_const_arrow_match.group("async")),
                        "exported": True,
                        "export_kind": "named",
                    }
                )
                exports.append({"kind": "named", "symbol": fn_name})
            else:
                const_arrow_match = _CONST_ARROW_RE.search(raw_line)
                if const_arrow_match:
                    fn_name = const_arrow_match.group("name")
                    symbols.append(
                        {
                            "name": fn_name,
                            "kind": "function",
                            "language": language,
                            "file": relative_path,
                            "line": idx,
                            "start_line": idx,
                            "end_line": None,
                            "signature": f"const {fn_name}({const_arrow_match.group('params').strip()}) =>",
                            "async": bool(const_arrow_match.group("async")),
                            "exported": False,
                            "export_kind": None,
                        }
                    )

            const_function_match = _CONST_FUNCTION_RE.search(raw_line)
            if const_function_match:
                fn_name = const_function_match.group("name")
                symbols.append(
                    {
                        "name": fn_name,
                        "kind": "function",
                        "language": language,
                        "file": relative_path,
                        "line": idx,
                        "start_line": idx,
                        "end_line": None,
                        "signature": f"const {fn_name} = function({const_function_match.group('params').strip()})",
                        "async": bool(const_function_match.group("async")),
                        "exported": False,
                        "export_kind": None,
                    }
                )

            export_default_symbol_match = _EXPORT_DEFAULT_SYMBOL_RE.search(raw_line)
            if export_default_symbol_match:
                exports.append({"kind": "default", "symbol": export_default_symbol_match.group("symbol")})

            export_named_match = _EXPORT_NAMED_RE.search(raw_line)
            if export_named_match:
                for imported, _local in _parse_named_items(export_named_match.group("named")):
                    exports.append({"kind": "named", "symbol": imported})

            module_export_match = _MODULE_EXPORT_RE.search(raw_line)
            if module_export_match:
                exports.append({"kind": "commonjs", "symbol": module_export_match.group("symbol")})

            exports_assign_match = _EXPORTS_ASSIGN_RE.search(raw_line)
            if exports_assign_match:
                exports.append({"kind": "commonjs_named", "symbol": exports_assign_match.group("symbol")})

            if class_stack:
                decorator_match = _DECORATOR_RE.search(raw_line)
                if decorator_match and not pending_method_lines:
                    pending_decorators.append(decorator_match.group("name"))
                else:
                    if not pending_method_lines and _METHOD_HEADER_START_RE.search(raw_line):
                        pending_method_lines = [raw_line.rstrip("\n")]
                        pending_method_start_line = idx
                    elif pending_method_lines:
                        pending_method_lines.append(raw_line.rstrip("\n"))

                    method_blob = "\n".join(pending_method_lines) if pending_method_lines else raw_line
                    method_match = _METHOD_SIGNATURE_RE.search(method_blob)
                    if method_match and "{" in method_blob and method_match.group("name") != "constructor":
                        prefix = method_match.group("prefix") or ""
                        current_class = class_stack[-1]
                        method_name = method_match.group("name")
                        start_line = pending_method_start_line or idx
                        signature_line = " ".join(line.strip() for line in pending_method_lines) if pending_method_lines else stripped
                        symbols.append(
                            {
                                "name": method_name,
                                "qualified_name": f"{current_class['name']}.{method_name}",
                                "kind": "class_method",
                                "parent": str(current_class["name"]),
                                "language": language,
                                "static": "static" in prefix.split(),
                                "async": "async" in prefix.split(),
                                "file": relative_path,
                                "line": start_line,
                                "start_line": start_line,
                                "end_line": None,
                                "signature": signature_line.strip(),
                                "decorators": list(pending_decorators),
                                "exported": True,
                                "export_kind": current_class.get("export_kind"),
                            }
                        )
                        method_stack.append(
                            {
                                "symbol_index": len(symbols) - 1,
                                "start_depth": brace_depth + 1,
                            }
                        )
                        pending_method_lines = []
                        pending_method_start_line = None
                        pending_decorators = []
                    elif pending_method_lines and (stripped.endswith(";") or stripped.startswith("if ")):
                        # Not a method signature after all.
                        pending_method_lines = []
                        pending_method_start_line = None
                        pending_decorators = []

            open_braces, close_braces = _count_braces(raw_line)
            previous_depth = brace_depth
            brace_depth += open_braces - close_braces

            if pending_class and previous_depth < brace_depth:
                class_stack.append(
                    {
                        "name": pending_class,
                        "start_depth": previous_depth + 1,
                        "export_kind": pending_class_export_kind,
                    }
                )
                pending_class = None
                pending_class_export_kind = None

            while class_stack and brace_depth < int(class_stack[-1]["start_depth"]):
                popped = class_stack.pop()
                for symbol in reversed(symbols):
                    if symbol.get("kind") == "class" and symbol.get("name") == popped.get("name") and symbol.get("end_line") is None:
                        symbol["end_line"] = idx
                        break
            while method_stack and brace_depth < int(method_stack[-1]["start_depth"]):
                popped = method_stack.pop()
                symbol_index = popped.get("symbol_index")
                if isinstance(symbol_index, int) and 0 <= symbol_index < len(symbols):
                    if symbols[symbol_index].get("end_line") is None:
                        symbols[symbol_index]["end_line"] = idx

        exported_by_symbol: dict[str, str] = {}
        for export in exports:
            symbol = export.get("symbol")
            kind = export.get("kind")
            if isinstance(symbol, str) and symbol:
                if kind == "default":
                    exported_by_symbol[symbol] = "default"
                else:
                    exported_by_symbol.setdefault(symbol, "named")

        for symbol in symbols:
            name = symbol.get("name")
            if (
                isinstance(name, str)
                and name in exported_by_symbol
                and symbol.get("kind") != "class_method"
            ):
                symbol["exported"] = True
                symbol["export_kind"] = exported_by_symbol[name]

        return FileSymbols(
            path=relative_path,
            language=language,
            imports=imports,
            exports=exports,
            symbols=symbols,
        )
