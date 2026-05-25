"""Lightweight JS/TS handler symbol index for layered trace expansion foundations."""

from __future__ import annotations

from collections import Counter
import re
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.repo_map import IGNORED_DIRS, build_repo_map
from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    classify_candidate_file_role,
)

SUPPORTED_EXTS = {".ts", ".tsx", ".js", ".jsx"}
LANGUAGE_BY_EXT = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
}
_MAX_FILE_SIZE = 2_000_000

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
_REQUIRE_NAMED_RE = re.compile(
    r"^\s*(?:const|let|var)\s*\{(?P<named>[^}]+)\}\s*=\s*require\(\s*['\"](?P<source>[^'\"]+)['\"]\s*\)"
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
_EXPORT_NAMED_RE = re.compile(r"^\s*export\s*\{(?P<named>[^}]+)\}\s*;?")
_MODULE_EXPORT_RE = re.compile(r"^\s*module\.exports\s*=\s*(?P<symbol>[A-Za-z_]\w*)\s*;?")
_EXPORTS_ASSIGN_RE = re.compile(
    r"^\s*exports\.(?P<export_name>[A-Za-z_]\w*)\s*=\s*(?P<symbol>[A-Za-z_]\w*)\s*;?"
)

_METHOD_RE = re.compile(
    r"^\s*(?P<prefix>(?:(?:public|private|protected|readonly|static|async)\s+)*)"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^\)]*)\)\s*\{"
)


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


def resolve_local_import(
    repo_root: Path, importer_relative_path: str, source: str
) -> str | None:
    """Resolve local JS/TS import to a repo-relative source file path."""
    if not source.startswith("."):
        return None
    importer_dir = (repo_root / importer_relative_path).parent
    source_path = (importer_dir / source).resolve()
    candidates: list[Path] = []
    if source_path.suffix:
        candidates.append(source_path)
    else:
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(source_path.with_suffix(ext))
        for index_name in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            candidates.append(source_path / index_name)
    for candidate in candidates:
        try:
            rel = candidate.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if candidate.is_file():
            return rel
    return None


def _select_preferred_dirs(repo_map_payload: dict) -> set[str]:
    preferred_dirs: set[str] = set()
    for key in (
        "candidate_route_dirs",
        "candidate_backend_dirs",
        "candidate_controller_dirs",
    ):
        for item in repo_map_payload.get(key, []):
            if isinstance(item, str) and item and item != ".":
                preferred_dirs.add(item)
    for entry in repo_map_payload.get("entrypoint_candidates", []):
        if isinstance(entry, str) and entry:
            parent = str(Path(entry).parent).replace("\\", "/")
            if parent and parent != ".":
                preferred_dirs.add(parent)
    return preferred_dirs


def _should_include(relative_path: str, preferred_dirs: set[str]) -> bool:
    if not preferred_dirs:
        return True
    for directory in preferred_dirs:
        if relative_path == directory or relative_path.startswith(directory + "/"):
            return True
    parent = str(Path(relative_path).parent).replace("\\", "/")
    for directory in preferred_dirs:
        if parent == directory or parent.startswith(directory + "/"):
            return True
    return False


def _extract_for_file(root: Path, relative_path: str, text: str) -> dict:
    ext = Path(relative_path).suffix.lower()
    language = LANGUAGE_BY_EXT.get(ext, "unknown")
    imports: list[dict] = []
    exports: list[dict] = []
    symbols: list[dict] = []

    class_stack: list[dict[str, int | str]] = []
    pending_class: str | None = None
    pending_class_export_kind: str | None = None
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

        import_default_named_match = _IMPORT_DEFAULT_NAMED_RE.search(raw_line)
        if import_default_named_match:
            source = import_default_named_match.group("source")
            default_local = import_default_named_match.group("default")
            imports.append(
                {
                    "local": default_local,
                    "imported": "default",
                    "source": source,
                    "kind": "default",
                    "resolved_path": resolve_local_import(root, relative_path, source),
                }
            )
            for imported, local in _parse_named_items(
                import_default_named_match.group("named")
            ):
                imports.append(
                    {
                        "local": local,
                        "imported": imported,
                        "source": source,
                        "kind": "named",
                        "resolved_path": resolve_local_import(root, relative_path, source),
                    }
                )
        else:
            default_match = _IMPORT_DEFAULT_RE.search(raw_line)
            if default_match:
                source = default_match.group("source")
                imports.append(
                    {
                        "local": default_match.group("local"),
                        "imported": "default",
                        "source": source,
                        "kind": "default",
                        "resolved_path": resolve_local_import(root, relative_path, source),
                    }
                )
            namespace_match = _IMPORT_NAMESPACE_RE.search(raw_line)
            if namespace_match:
                source = namespace_match.group("source")
                imports.append(
                    {
                        "local": namespace_match.group("local"),
                        "imported": "*",
                        "source": source,
                        "kind": "namespace",
                        "resolved_path": resolve_local_import(root, relative_path, source),
                    }
                )
            named_match = _IMPORT_NAMED_RE.search(raw_line)
            if named_match:
                source = named_match.group("source")
                for imported, local in _parse_named_items(named_match.group("named")):
                    imports.append(
                        {
                            "local": local,
                            "imported": imported,
                            "source": source,
                            "kind": "named",
                            "resolved_path": resolve_local_import(root, relative_path, source),
                        }
                    )

        require_match = _REQUIRE_DEFAULT_RE.search(raw_line)
        if require_match:
            source = require_match.group("source")
            imports.append(
                {
                    "local": require_match.group("local"),
                    "imported": "default",
                    "source": source,
                    "kind": "require",
                    "resolved_path": resolve_local_import(root, relative_path, source),
                }
            )
        require_named_match = _REQUIRE_NAMED_RE.search(raw_line)
        if require_named_match:
            source = require_named_match.group("source")
            for imported, local in _parse_named_items(require_named_match.group("named")):
                imports.append(
                    {
                        "local": local,
                        "imported": imported,
                        "source": source,
                        "kind": "require_named",
                        "resolved_path": resolve_local_import(root, relative_path, source),
                    }
                )

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
            class_export_kind = None

        if class_name:
            symbols.append(
                {
                    "name": class_name,
                    "kind": "class",
                    "file": relative_path,
                    "line": idx,
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
                    "file": relative_path,
                    "line": idx,
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
                        "file": relative_path,
                        "line": idx,
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
                            "file": relative_path,
                            "line": idx,
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
                    "file": relative_path,
                    "line": idx,
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
                        "file": relative_path,
                        "line": idx,
                        "signature": f"const {fn_name}({const_arrow_match.group('params').strip()}) =>",
                        "async": bool(const_arrow_match.group("async")),
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

        in_class = bool(class_stack)
        if in_class:
            current_class = class_stack[-1]
            method_match = _METHOD_RE.search(raw_line)
            if method_match:
                method_name = method_match.group("name")
                if method_name != "constructor":
                    prefix = method_match.group("prefix") or ""
                    is_static = "static" in prefix.split()
                    is_async = "async" in prefix.split()
                    class_name_for_method = str(current_class["name"])
                    symbols.append(
                        {
                            "name": method_name,
                            "qualified_name": f"{class_name_for_method}.{method_name}",
                            "kind": "class_method",
                            "parent": class_name_for_method,
                            "static": is_static,
                            "async": is_async,
                            "file": relative_path,
                            "line": idx,
                            "signature": stripped.rstrip("{").strip(),
                            "exported": True,
                            "export_kind": current_class.get("export_kind"),
                        }
                    )

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
            class_stack.pop()

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
        if isinstance(name, str) and name in exported_by_symbol and symbol.get("kind") != "class_method":
            symbol["exported"] = True
            symbol["export_kind"] = exported_by_symbol[name]

    return {
        "path": relative_path,
        "language": language,
        "imports": imports,
        "exports": exports,
        "symbols": symbols,
    }


def build_handler_symbol_index(repo: RepoRef) -> dict:
    """Build a lightweight handler symbol index for one repository."""
    root = Path(repo.root).expanduser().resolve()
    repo_map_payload = build_repo_map(repo)
    preferred_dirs = _select_preferred_dirs(repo_map_payload)

    files: list[dict] = []
    summary_counter = Counter(
        {
            "files_indexed": 0,
            "classes": 0,
            "class_methods": 0,
            "functions": 0,
            "imports": 0,
            "exports": 0,
            "symbols": 0,
        }
    )

    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
        for filename in filenames:
            path = dirpath / filename
            rel = path.relative_to(root).as_posix()
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTS:
                continue
            role = classify_candidate_file_role(rel)
            if role != FILE_ROLE_SOURCE_ROUTE_CANDIDATE:
                continue
            if not _should_include(rel, preferred_dirs):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_SIZE:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            entry = _extract_for_file(root, rel, text)
            files.append(entry)
            summary_counter["files_indexed"] += 1
            summary_counter["imports"] += len(entry["imports"])
            summary_counter["exports"] += len(entry["exports"])
            summary_counter["symbols"] += len(entry["symbols"])
            for symbol in entry["symbols"]:
                kind = symbol.get("kind")
                if kind == "class":
                    summary_counter["classes"] += 1
                elif kind == "class_method":
                    summary_counter["class_methods"] += 1
                elif kind == "function":
                    summary_counter["functions"] += 1

    files.sort(key=lambda item: item["path"])
    return {
        "version": "v1",
        "repo": repo.name,
        "root": str(root),
        "files": files,
        "summary": dict(summary_counter),
    }


def build_handler_symbol_index_batch(repos: list[RepoRef]) -> dict:
    """Build handler symbol indexes for all repositories."""
    repo_indexes = [build_handler_symbol_index(repo) for repo in repos]
    summary_counter = Counter(
        {
            "files_indexed": 0,
            "classes": 0,
            "class_methods": 0,
            "functions": 0,
            "imports": 0,
            "exports": 0,
            "symbols": 0,
        }
    )
    for repo_index in repo_indexes:
        repo_summary = repo_index.get("summary", {})
        for key in summary_counter:
            summary_counter[key] += int(repo_summary.get(key, 0))
    return {"version": "v1", "repos": repo_indexes, "summary": dict(summary_counter)}

