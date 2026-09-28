"""Java handler symbol extractor adapter.

Implements the same `HandlerSymbolExtractor` contract as the JS/TS and Python
adapters, so Java participates in the existing shared symbol index, handler
resolution, and call-following machinery rather than needing a traversal of
its own. Java previously had no adapter at all (`_extractor_registry()`
listed only JS/TS and Python) -- any changed `.java` file yielded zero
symbols regardless of what it declared, so nothing downstream (route
matching, obligation derivation, test mapping) ever had anything to attach
to for a Java change.

Extraction is regex- and brace-depth-based, the same approach the JS/TS
adapter uses (Java has no stdlib AST available here, unlike Python) --
deliberately not a full parser. Class/method signatures spanning multiple
lines (long generic bounds, multi-line `throws` clauses split across lines)
are a known gap, matching the JS/TS adapter's own documented limitations for
comparably unusual multi-line shapes.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from sydes.trace.handler_symbols.common import FileSymbols

_PACKAGE_RE = re.compile(r"^\s*package\s+(?P<name>[\w.]+)\s*;")
_IMPORT_RE = re.compile(
    r"^\s*import\s+(?P<static>static\s+)?(?P<fqcn>[\w.]+?)(?P<wildcard>\.\*)?\s*;"
)

_CLASS_LIKE_RE = re.compile(
    r"(?:^|\s)(?:public|private|protected|static|final|abstract|sealed|non-sealed|strictfp|\s)*"
    r"\b(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)"
)

_ANNOTATION_RE = re.compile(r"^\s*@(?P<name>[A-Za-z_][\w.]*)\s*(?:\((?P<args>.*)\))?\s*$")

#: Common `<...>`, keeps the previous line's method/return-type text before
#: the parameter list, still line-local (no cross-line accumulation for a
#: return type that is itself split across lines -- a known gap, see the
#: module docstring).
_METHOD_RE = re.compile(
    r"(?P<prefix>(?:public|private|protected|static|final|abstract|synchronized|"
    r"native|default|transient|volatile|\s)*)"
    r"(?:<[^>]*>\s*)?"
    r"(?P<return_type>[\w.\[\]<>,?]+)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?:throws\s+[\w.,\s]+)?"
    r"\s*\{"
)

#: A constructor has no return type -- only recognized once its name is
#: confirmed to equal the class it is declared inside (see call site).
_CONSTRUCTOR_RE = re.compile(
    r"(?P<prefix>(?:public|private|protected|\s)*)"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?:throws\s+[\w.,\s]+)?"
    r"\s*\{"
)

#: Control-flow keywords a bare `word (` line could otherwise be mistaken
#: for a zero-return-type method/constructor by `_METHOD_RE`/`_CONSTRUCTOR_RE`.
_CONTROL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "try", "else", "do", "return",
    "new", "throw", "synchronized", "assert",
}


def _count_braces(line: str) -> tuple[int, int]:
    """Count `{`/`}` on one line -- no string/char/comment awareness, the
    same lightweight heuristic the JS/TS adapter's own counter uses."""
    return line.count("{"), line.count("}")


@lru_cache(maxsize=32)
def _java_source_roots(repo_root: str) -> tuple[Path, ...]:
    """Every `src/main/java` and `src/test/java` root under this repo.

    A multi-module Maven/Gradle repository has one such root per module, not
    one for the whole checkout -- caching by `repo_root` avoids re-walking
    the whole tree for every file/import this extractor processes.
    """
    root = Path(repo_root)
    roots = list(root.glob("**/src/main/java")) + list(root.glob("**/src/test/java"))
    return tuple(roots)


def resolve_java_import(repo_root: Path, fqcn: str) -> str | None:
    """Resolve a fully-qualified Java class name to a repo-relative file.

    Only ever resolves to a file actually present under one of the repo's
    `src/main/java`/`src/test/java` roots -- a class outside those roots
    (a third-party dependency) correctly yields `None`.
    """
    if not fqcn:
        return None
    parts = fqcn.split(".")
    # A nested/inner class is imported as `Outer.Inner`; only `Outer` has its
    # own file. Walk outward until a candidate file actually exists.
    for end in range(len(parts), 0, -1):
        candidate_rel = "/".join(parts[:end]) + ".java"
        for source_root in _java_source_roots(str(repo_root)):
            candidate = source_root / candidate_rel
            if candidate.is_file():
                try:
                    return candidate.relative_to(repo_root).as_posix()
                except ValueError:
                    continue
    return None


class JavaHandlerSymbolExtractor:
    """Java implementation of the generic handler symbol extractor interface."""

    language = "java"
    extensions = {".java"}

    def extract_file(self, repo_root: Path, relative_path: str, text: str) -> FileSymbols:
        imports: list[dict] = []
        exports: list[dict] = []
        symbols: list[dict] = []

        class_stack: list[dict[str, int | str]] = []
        method_stack: list[dict[str, int]] = []
        pending_decorators: list[str] = []
        brace_depth = 0

        lines = text.splitlines()
        for idx, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                open_braces, close_braces = _count_braces(raw_line)
                brace_depth += open_braces - close_braces
                continue

            package_match = _PACKAGE_RE.search(raw_line)
            if package_match and not class_stack:
                imports.append(
                    {
                        "local": None,
                        "imported": None,
                        "source": package_match.group("name"),
                        "kind": "package",
                        "resolved_file": None,
                    }
                )

            import_match = _IMPORT_RE.search(raw_line)
            if import_match and import_match.group("wildcard") is None:
                fqcn = import_match.group("fqcn")
                simple_name = fqcn.rsplit(".", 1)[-1]
                imports.append(
                    {
                        "local": simple_name,
                        "imported": fqcn,
                        "source": fqcn,
                        "kind": "static" if import_match.group("static") else "named",
                        "resolved_file": resolve_java_import(repo_root, fqcn),
                    }
                )

            annotation_match = _ANNOTATION_RE.search(raw_line)
            if annotation_match:
                pending_decorators.append(annotation_match.group("name"))
                continue

            class_match = _CLASS_LIKE_RE.search(raw_line)
            pending_class: str | None = None
            if class_match:
                pending_class = class_match.group("name")
                parent_name = str(class_stack[-1]["name"]) if class_stack else None
                qualified_name = f"{parent_name}.{pending_class}" if parent_name else pending_class
                is_public = "public" in stripped.split()
                symbols.append(
                    {
                        "name": pending_class,
                        "kind": "class",
                        "language": self.language,
                        "file": relative_path,
                        "line": idx,
                        "start_line": idx,
                        "end_line": None,
                        "parent": parent_name,
                        "qualified_name": qualified_name,
                        "exported": is_public,
                        "export_kind": "public" if is_public else None,
                        "decorators": list(pending_decorators),
                    }
                )
                if is_public and not parent_name:
                    exports.append({"kind": "public", "symbol": pending_class})
                pending_decorators = []
            elif class_stack:
                current_class = class_stack[-1]
                method_match = _METHOD_RE.search(raw_line)
                method_name = None
                params = ""
                if method_match and method_match.group("name") not in _CONTROL_KEYWORDS:
                    method_name = method_match.group("name")
                    params = method_match.group("params")
                    prefix = method_match.group("prefix") or ""
                else:
                    ctor_match = _CONSTRUCTOR_RE.search(raw_line)
                    if ctor_match and ctor_match.group("name") == current_class["name"]:
                        method_name = ctor_match.group("name")
                        params = ctor_match.group("params")
                        prefix = ctor_match.group("prefix") or ""
                if method_name:
                    is_public = "public" in prefix.split()
                    symbols.append(
                        {
                            "name": method_name,
                            "kind": "class_method",
                            "language": self.language,
                            "file": relative_path,
                            "line": idx,
                            "start_line": idx,
                            "end_line": None,
                            "parent": str(current_class["name"]),
                            "qualified_name": f"{current_class['name']}.{method_name}",
                            "signature": f"{method_name}({params.strip()})",
                            "static": "static" in prefix.split(),
                            "exported": is_public,
                            "export_kind": "public" if is_public else None,
                            "decorators": list(pending_decorators),
                        }
                    )
                    method_stack.append(
                        {"symbol_index": len(symbols) - 1, "start_depth": brace_depth + 1}
                    )
                    pending_decorators = []
                elif not annotation_match:
                    pending_decorators = []

            open_braces, close_braces = _count_braces(raw_line)
            previous_depth = brace_depth
            brace_depth += open_braces - close_braces

            if pending_class and previous_depth < brace_depth:
                class_stack.append({"name": pending_class, "start_depth": previous_depth + 1})

            while class_stack and brace_depth < int(class_stack[-1]["start_depth"]):
                popped = class_stack.pop()
                for symbol in reversed(symbols):
                    if (
                        symbol.get("kind") == "class"
                        and symbol.get("name") == popped.get("name")
                        and symbol.get("end_line") is None
                    ):
                        symbol["end_line"] = idx
                        break
            while method_stack and brace_depth < int(method_stack[-1]["start_depth"]):
                popped = method_stack.pop()
                symbol_index = popped.get("symbol_index")
                if isinstance(symbol_index, int) and 0 <= symbol_index < len(symbols):
                    if symbols[symbol_index].get("end_line") is None:
                        symbols[symbol_index]["end_line"] = idx

        return FileSymbols(
            path=relative_path,
            language=self.language,
            imports=imports,
            exports=exports,
            symbols=symbols,
        )
