"""Repo-wide symbol index and call graph used to expand a change into behavior.

Sydes already indexes JS/TS handler symbols for route-candidate directories via
`sydes.trace.handler_symbols`; that adapter is reused here verbatim. Python is
added through the same `HandlerSymbolExtractor` shape, and the index is widened
to the whole repository because a change can land anywhere, not only in a route
directory.

The call graph is deliberately conservative: an edge is only recorded when the
callee name resolves to exactly one plausible symbol, or when an import in the
calling file points at the callee's file. Ambiguous names are dropped rather
than guessed, so downstream flows do not accumulate speculative edges.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re

from sydes.trace.handler_symbols.js_ts import JsTsHandlerSymbolExtractor
from sydes.verify.repo_scan import RepoScan, ScannedFile

_CALL_RE = re.compile(r"(?<![\w.])(?P<name>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*\(")
_JS_CALL_NOISE = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "function",
    "require",
    "typeof",
    "await",
    "console.log",
    "console.error",
    "console.warn",
    "JSON.parse",
    "JSON.stringify",
    "Number",
    "String",
    "Boolean",
    "Array",
    "Object.keys",
    "Object.values",
    "Object.assign",
    "parseInt",
    "parseFloat",
    "Promise.all",
    "Promise.resolve",
    "Promise.reject",
    "String.raw",
    "Math.max",
    "Math.min",
    "setTimeout",
    "setInterval",
}
_PY_CALL_NOISE = {
    "print",
    "len",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "range",
    "enumerate",
    "isinstance",
    "getattr",
    "setattr",
    "hasattr",
    "super",
    "open",
    "sorted",
    "any",
    "all",
    "zip",
    "map",
    "filter",
    "type",
    "format",
    "join",
    "append",
    "strip",
    "split",
    "get",
    "Field",
}

_MAX_AMBIGUOUS_CANDIDATES = 3

_JS_CONSTRUCTOR_BINDING = re.compile(
    r"(?:const|let|var)\s+(?P<variable>[A-Za-z_]\w*)\s*(?::\s*\w+\s*)?=\s*new\s+(?P<type>[A-Za-z_]\w*)"
)

# Object-literal handler maps (`const authController = { login: async (req, res) => {} }`)
# are a very common Express/Node shape that the shared handler-symbol extractor
# does not model. They are supplemented here rather than by changing the shared
# adapter, which `routes`/`trace` also depend on.
_OBJECT_LITERAL_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_]\w*)\s*=\s*\{\s*$"
)
_OBJECT_METHOD_RE = re.compile(
    r"^(?P<indent>\s+)(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<async>async\s+)?"
    r"(?:function\s*)?\(([^)]*)\)\s*(?:=>\s*)?\{"
)


@dataclass(slots=True)
class Symbol:
    """One indexed function/method/class in a repository."""

    id: str
    repo: str
    file: str
    name: str
    kind: str
    language: str
    start_line: int
    end_line: int
    qualified_name: str | None = None
    class_name: str | None = None
    decorators: list[str] = field(default_factory=list)
    calls: list[tuple[str, int, str]] = field(default_factory=list)
    exported: bool = False

    @property
    def display_name(self) -> str:
        """Preferred human-facing symbol name."""
        return self.qualified_name or self.name

    def contains_line(self, line: int) -> bool:
        """True when a source line falls within this symbol's span."""
        return self.start_line <= line <= self.end_line


@dataclass(slots=True)
class FileImport:
    """One import statement resolved (where possible) to a repo file."""

    local: str
    imported: str
    source: str
    resolved_file: str | None = None


@dataclass(slots=True)
class CallEdge:
    """Resolved call relation between two indexed symbols."""

    caller_id: str
    callee_id: str
    call_text: str
    file: str
    line: int
    snippet: str
    resolution: str


@dataclass(slots=True)
class SymbolIndex:
    """Symbols, imports, and the resolved call graph for one repository."""

    repo: str
    root: Path
    symbols: dict[str, Symbol] = field(default_factory=dict)
    imports_by_file: dict[str, list[FileImport]] = field(default_factory=dict)
    # file -> {variable: constructing type}, so `service = RefundService()`
    # lets `service.retry_refund()` resolve to `RefundService.retry_refund`.
    receiver_types: dict[str, dict[str, str]] = field(default_factory=dict)
    edges: list[CallEdge] = field(default_factory=list)
    callers_of: dict[str, list[CallEdge]] = field(default_factory=lambda: defaultdict(list))
    callees_of: dict[str, list[CallEdge]] = field(default_factory=lambda: defaultdict(list))
    notes: list[str] = field(default_factory=list)

    def symbols_in_file(self, file_path: str) -> list[Symbol]:
        """Return symbols declared in a file, ordered by position."""
        return sorted(
            (item for item in self.symbols.values() if item.file == file_path),
            key=lambda item: item.start_line,
        )

    def symbol_at(self, file_path: str, line: int) -> Symbol | None:
        """Return the innermost symbol containing a line, if any."""
        matches = [
            item
            for item in self.symbols.values()
            if item.file == file_path and item.contains_line(line)
        ]
        if not matches:
            return None
        # Prefer the tightest span so a method wins over its enclosing class.
        return min(matches, key=lambda item: item.end_line - item.start_line)


def _symbol_id(repo: str, file_path: str, name: str, line: int) -> str:
    """Build a stable symbol identifier."""
    return f"{repo}:{file_path}:{name}:{line}"


def _decorator_text(node: ast.AST) -> str:
    """Render a Python decorator expression back to compact source text."""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - decorator rendering is best-effort
        return ""


def _dotted_name(node: ast.AST) -> str | None:
    """Render a dotted attribute/name expression, or None for complex calls."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        if prefix is None:
            return node.attr
        return f"{prefix}.{node.attr}"
    return None


def _python_calls(node: ast.AST, source_lines: list[str]) -> list[tuple[str, int, str]]:
    """Collect dotted call names inside a Python function body."""
    calls: list[tuple[str, int, str]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _dotted_name(child.func)
        if not name:
            continue
        last = name.rsplit(".", 1)[-1]
        if name in _PY_CALL_NOISE or last in _PY_CALL_NOISE:
            continue
        line = getattr(child, "lineno", 0)
        snippet = ""
        if 0 < line <= len(source_lines):
            snippet = source_lines[line - 1].strip()
        calls.append((name, line, snippet[:220]))
    return calls


def _resolve_python_import(root: Path, importer: str, module: str | None, level: int) -> str | None:
    """Resolve a Python import to a repo-relative file path when it is local."""
    importer_path = Path(importer)
    if level and level > 0:
        base = importer_path.parent
        for _ in range(level - 1):
            base = base.parent
        parts = module.split(".") if module else []
    else:
        if not module:
            return None
        base = Path(".")
        parts = module.split(".")

    candidate_dir = base
    for part in parts:
        candidate_dir = candidate_dir / part

    for candidate in (
        candidate_dir.with_suffix(".py"),
        candidate_dir / "__init__.py",
    ):
        normalized = Path(*[p for p in candidate.parts if p != "."])
        if (root / normalized).is_file():
            return normalized.as_posix()
    return None


def _index_python_file(index: SymbolIndex, scanned: ScannedFile) -> None:
    """Index Python symbols, imports, and intra-function calls via AST."""
    try:
        tree = ast.parse(scanned.text)
    except SyntaxError:
        index.notes.append(f"python_parse_failed={scanned.path}")
        return

    source_lines = scanned.text.splitlines()
    imports: list[FileImport] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_python_import(index.root, scanned.path, alias.name, 0)
                imports.append(
                    FileImport(
                        local=alias.asname or alias.name.split(".")[0],
                        imported=alias.name,
                        source=alias.name,
                        resolved_file=resolved,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_python_import(index.root, scanned.path, node.module, node.level or 0)
            for alias in node.names:
                imports.append(
                    FileImport(
                        local=alias.asname or alias.name,
                        imported=alias.name,
                        source=node.module or ".",
                        resolved_file=resolved,
                    )
                )
    index.imports_by_file[scanned.path] = imports

    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        constructor = _dotted_name(node.value.func)
        if not constructor:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = constructor.rsplit(".", 1)[-1]
        if isinstance(node.targets[0], ast.Attribute):
            attribute = node.targets[0]
            if isinstance(attribute.value, ast.Name) and attribute.value.id == "self":
                bindings[f"self.{attribute.attr}"] = constructor.rsplit(".", 1)[-1]
    index.receiver_types[scanned.path] = bindings

    def _add_function(node: ast.AST, class_name: str | None) -> None:
        name = getattr(node, "name", None)
        if not isinstance(name, str):
            return
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        decorators = [
            _decorator_text(item) for item in getattr(node, "decorator_list", []) or []
        ]
        qualified = f"{class_name}.{name}" if class_name else name
        symbol = Symbol(
            id=_symbol_id(index.repo, scanned.path, qualified, start),
            repo=index.repo,
            file=scanned.path,
            name=name,
            kind="class_method" if class_name else "function",
            language="python",
            start_line=start,
            end_line=end,
            qualified_name=qualified,
            class_name=class_name,
            decorators=[item for item in decorators if item],
            calls=_python_calls(node, source_lines),
            exported=not name.startswith("_"),
        )
        index.symbols[symbol.id] = symbol

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            start = node.lineno
            end = node.end_lineno or start
            class_symbol = Symbol(
                id=_symbol_id(index.repo, scanned.path, node.name, start),
                repo=index.repo,
                file=scanned.path,
                name=node.name,
                kind="class",
                language="python",
                start_line=start,
                end_line=end,
                qualified_name=node.name,
                decorators=[_decorator_text(item) for item in node.decorator_list],
                exported=True,
            )
            index.symbols[class_symbol.id] = class_symbol
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    _add_function(child, node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _add_function(node, None)


def _js_symbol_end_line(symbol: dict, ordered_starts: list[int], total_lines: int) -> int:
    """Infer an end line for a JS/TS symbol when the extractor did not set one."""
    end = symbol.get("end_line")
    if isinstance(end, int) and end >= int(symbol.get("start_line") or 0):
        return end
    start = int(symbol.get("start_line") or symbol.get("line") or 1)
    following = [line for line in ordered_starts if line > start]
    if following:
        return max(start, min(following) - 1)
    return total_lines


def _js_calls(text_lines: list[str], start: int, end: int) -> list[tuple[str, int, str]]:
    """Collect call names inside a JS/TS symbol body by line scanning."""
    calls: list[tuple[str, int, str]] = []
    for line_no in range(start, min(end, len(text_lines)) + 1):
        line = text_lines[line_no - 1]
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for match in _CALL_RE.finditer(line):
            name = match.group("name")
            last = name.rsplit(".", 1)[-1]
            if name in _JS_CALL_NOISE or last in _JS_CALL_NOISE:
                continue
            calls.append((name, line_no, stripped[:220]))
    return calls


def _brace_block_end(lines: list[str], start_line: int) -> int:
    """Find the line where a brace block opened on `start_line` closes."""
    depth = 0
    started = False
    for line_no in range(start_line, len(lines) + 1):
        line = lines[line_no - 1]
        depth += line.count("{") - line.count("}")
        if not started and "{" in line:
            started = True
        if started and depth <= 0:
            return line_no
    return len(lines)


def _object_literal_symbols(
    index: SymbolIndex, scanned: ScannedFile, text_lines: list[str], language: str
) -> list[Symbol]:
    """Extract object-literal methods (`{ login: async (req, res) => {} }`)."""
    symbols: list[Symbol] = []
    owner: str | None = None
    owner_end = 0

    for line_no, line in enumerate(text_lines, start=1):
        declaration = _OBJECT_LITERAL_DECL_RE.match(line)
        if declaration:
            owner = declaration.group("name")
            owner_end = _brace_block_end(text_lines, line_no)
            continue
        if owner is None or line_no > owner_end:
            owner = None
            continue
        method = _OBJECT_METHOD_RE.match(line)
        if method is None:
            continue
        name = method.group("name")
        end = _brace_block_end(text_lines, line_no)
        qualified = f"{owner}.{name}"
        symbols.append(
            Symbol(
                id=_symbol_id(index.repo, scanned.path, qualified, line_no),
                repo=index.repo,
                file=scanned.path,
                name=name,
                kind="object_method",
                language=language,
                start_line=line_no,
                end_line=end,
                qualified_name=qualified,
                class_name=owner,
                calls=_js_calls(text_lines, line_no, end),
                exported=True,
            )
        )
    return symbols


def _index_js_file(index: SymbolIndex, scanned: ScannedFile, extractor: JsTsHandlerSymbolExtractor) -> None:
    """Index JS/TS symbols by reusing the existing handler-symbol adapter."""
    payload = extractor.extract_file(index.root, scanned.path, scanned.text).to_dict()
    text_lines = scanned.text.splitlines()
    total_lines = len(text_lines)

    index.imports_by_file[scanned.path] = [
        FileImport(
            local=str(item.get("local") or ""),
            imported=str(item.get("imported") or ""),
            source=str(item.get("source") or ""),
            resolved_file=item.get("resolved_file"),
        )
        for item in payload.get("imports", [])
        if isinstance(item, dict)
    ]

    raw_symbols = [item for item in payload.get("symbols", []) if isinstance(item, dict)]
    ordered_starts = sorted(
        int(item.get("start_line") or item.get("line") or 1) for item in raw_symbols
    )

    for raw in raw_symbols:
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        start = int(raw.get("start_line") or raw.get("line") or 1)
        end = _js_symbol_end_line(raw, ordered_starts, total_lines)
        kind = str(raw.get("kind") or "function")
        qualified = raw.get("qualified_name") or name
        calls: list[tuple[str, int, str]] = []
        if kind != "class":
            calls = _js_calls(text_lines, start, end)
        symbol = Symbol(
            id=_symbol_id(index.repo, scanned.path, str(qualified), start),
            repo=index.repo,
            file=scanned.path,
            name=name,
            kind=kind,
            language=str(raw.get("language") or "javascript"),
            start_line=start,
            end_line=end,
            qualified_name=str(qualified),
            class_name=str(qualified).rsplit(".", 1)[0] if "." in str(qualified) else None,
            decorators=[str(item) for item in raw.get("decorators", []) if isinstance(item, str)],
            calls=calls,
            exported=bool(raw.get("exported")),
        )
        index.symbols[symbol.id] = symbol

    language = "typescript" if scanned.extension in {".ts", ".tsx"} else "javascript"
    for symbol in _object_literal_symbols(index, scanned, text_lines, language):
        index.symbols.setdefault(symbol.id, symbol)

    index.receiver_types[scanned.path] = {
        match.group("variable"): match.group("type")
        for match in _JS_CONSTRUCTOR_BINDING.finditer(scanned.text)
    }


def _candidate_symbols_for_call(
    index: SymbolIndex,
    caller: Symbol,
    call_name: str,
    by_name: dict[str, list[Symbol]],
    by_qualified: dict[str, list[Symbol]],
) -> tuple[Symbol | None, str]:
    """Resolve one call name to a single symbol, or give up rather than guess."""
    parts = call_name.split(".")
    last = parts[-1]

    exact_qualified = by_qualified.get(call_name, [])
    if len(exact_qualified) == 1:
        return exact_qualified[0], "qualified_name"

    if len(parts) >= 2:
        # `refundService.retryRefund` -> match `RefundService.retryRefund`, and
        # `service.retryRefund` where `service = RefundService()` was bound.
        receiver = parts[-2]
        bound_type = index.receiver_types.get(caller.file, {}).get(
            ".".join(parts[:-1])
        ) or index.receiver_types.get(caller.file, {}).get(receiver)
        for candidate_receiver, resolution in ((receiver, "receiver_class_match"), (bound_type, "receiver_binding")):
            if not candidate_receiver:
                continue
            suffix_matches = [
                symbol
                for symbol in by_name.get(last, [])
                if symbol.class_name and symbol.class_name.lower() == candidate_receiver.lower()
            ]
            if len(suffix_matches) == 1:
                return suffix_matches[0], resolution

    imports = index.imports_by_file.get(caller.file, [])
    name_matches = by_name.get(last, [])
    if not name_matches:
        return None, "not_found"

    same_file = [item for item in name_matches if item.file == caller.file and item.id != caller.id]
    if len(same_file) == 1:
        return same_file[0], "same_file"

    if len(parts) >= 2:
        # A member call only resolves through the receiver's own import. Without
        # that link, a same-named function elsewhere in the repo is a
        # coincidence, not a call — `db.query()` must not bind to a bundled
        # asset that happens to define `query`.
        receiver = parts[-2]
        bound_type = index.receiver_types.get(caller.file, {}).get(receiver)
        receiver_names = {receiver, bound_type} - {None}
        receiver_files = {
            item.resolved_file
            for item in imports
            if item.resolved_file and item.local in receiver_names
        }
        receiver_matches = [item for item in name_matches if item.file in receiver_files]
        if len(receiver_matches) == 1:
            return receiver_matches[0], "receiver_import_resolved"
        return None, "receiver_unresolved"

    imported_files = {item.resolved_file for item in imports if item.resolved_file}
    imported_matches = [item for item in name_matches if item.file in imported_files]
    if len(imported_matches) == 1:
        return imported_matches[0], "import_resolved"

    if len(name_matches) <= _MAX_AMBIGUOUS_CANDIDATES:
        return None, "ambiguous"
    return None, "no_import_evidence"


def build_symbol_index(scan: RepoScan) -> SymbolIndex:
    """Build the symbol index and resolved call graph for a scanned repository."""
    index = SymbolIndex(repo=scan.repo, root=scan.root)
    extractor = JsTsHandlerSymbolExtractor()

    for scanned in scan.files:
        if scanned.extension == ".py":
            _index_python_file(index, scanned)
        elif scanned.extension in extractor.extensions:
            _index_js_file(index, scanned, extractor)

    by_name: dict[str, list[Symbol]] = defaultdict(list)
    by_qualified: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in index.symbols.values():
        by_name[symbol.name].append(symbol)
        if symbol.qualified_name:
            by_qualified[symbol.qualified_name].append(symbol)

    unresolved = 0
    for caller in index.symbols.values():
        seen: set[tuple[str, str]] = set()
        for call_name, line, snippet in caller.calls:
            callee, resolution = _candidate_symbols_for_call(
                index, caller, call_name, by_name, by_qualified
            )
            if callee is None or callee.id == caller.id:
                unresolved += 1
                continue
            key = (caller.id, callee.id)
            if key in seen:
                continue
            seen.add(key)
            edge = CallEdge(
                caller_id=caller.id,
                callee_id=callee.id,
                call_text=call_name,
                file=caller.file,
                line=line,
                snippet=snippet,
                resolution=resolution,
            )
            index.edges.append(edge)
            index.callers_of[callee.id].append(edge)
            index.callees_of[caller.id].append(edge)

    index.notes.append(f"symbols_indexed={len(index.symbols)}")
    index.notes.append(f"call_edges_resolved={len(index.edges)}")
    index.notes.append(f"call_names_unresolved={unresolved}")
    return index
