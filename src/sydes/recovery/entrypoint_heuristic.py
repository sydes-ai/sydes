"""A single, generic, framework-name-free heuristic for declarative HTTP
entrypoints: a decorator/annotation whose own name IS (or ends with
"Mapping" and contains) an HTTP verb, attached to a method.

This exists because of a measured, specific gap: CBM's own graph engine
extracts route declarations (`get_architecture(aspects=["routes"])`) but
does not compose them with their handler method, and its own docstring for
`decorated_symbols` says so explicitly -- "whether a decorator makes a
symbol an entrypoint... is interpretation and belongs above this layer."
Directly testing CBM's `trace_path` (all three modes: calls, data_flow,
cross_service) against a live decorator-routed entrypoint found zero
callers in every mode; this is not a hypothetical gap.

Deliberately ONE shape rule, not a per-ecosystem catalog: matching is by
decorator SHAPE (a verb-like name, alone or with a common suffix word for
the same idea, near a method declaration), never by a named library,
class, or import. This is what keeps it generic across the many
in-the-wild decorator/annotation-based routing conventions that all
converge on this same shape, without a separate extractor to maintain per
convention -- and equally, it does not special-case any one of them by
name in code.

A route's PATH argument is often not a string literal at all (a shared
routes-config constant is common in real code) -- so this heuristic never
requires a resolvable literal path to report a match. Getting the
entrypoint SYMBOL right is the goal; the path text carried alongside it is
best-effort context, not a requirement, and is never used for identity
matching (identity is file + symbol, per `sydes.recovery.schema.EntityRef`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VERB_NAMES = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})

#: A decorator/annotation line: `@Word(...)` (TS/Python/Java/Kotlin) or
#: `[Word(...)]` (C#), with or without a trailing `()`.
_DECORATOR_RE = re.compile(r"^\s*[@\[]\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^)]*)\))?\s*\]?\s*$")

#: A loose method/function declaration -- enough to grab the name that
#: follows a decorator, across several C-family/Python-family syntaxes.
_METHOD_DECL_RE = re.compile(
    r"\b(?:function\s+|def\s+|public\s+|private\s+|protected\s+|internal\s+|"
    r"async\s+|static\s+|final\s+|override\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*\("
)

_CLASS_DECL_RE = re.compile(r"^\s*(?:export\s+)?(?:public\s+|internal\s+)?(?:abstract\s+|final\s+)?class\b")

_CODE_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".java", ".py", ".cs", ".go", ".rs", ".kt"})

#: How far above/below a decorator line this scans for the class it
#: belongs to, or the method it decorates -- small on purpose: a decorator
#: sits directly next to what it decorates by convention in every language
#: this matches, so a wide window would only invite false positives.
_LOOKAROUND_LINES = 6


@dataclass(frozen=True)
class DeclarativeEntrypoint:
    """One method found to be HTTP-reachable by decorator shape alone.

    `path` is best-effort and may be empty or symbolic (unresolved
    constant reference) -- never relied on for identity. `class_prefix` is
    the nearest enclosing class's own decorator argument text, if any,
    similarly best-effort.
    """

    file: str
    symbol: str
    http_method: str
    path: str
    class_prefix: str
    line: int


def _http_verb(decorator_name: str) -> str | None:
    lname = decorator_name.lower()
    if lname in _VERB_NAMES:
        return lname.upper()
    if lname.endswith("mapping"):
        verb = lname[: -len("mapping")]
        if verb in _VERB_NAMES:
            return verb.upper()
    return None


def _enclosing_class_prefix(lines: list[str], decorator_line_idx: int) -> str:
    """Walk upward from a route decorator to the nearest preceding class
    declaration, then look just above THAT for the class's own decorator
    and return its raw argument text (literal or symbolic) if it has one.
    """
    for idx in range(decorator_line_idx - 1, max(-1, decorator_line_idx - 200), -1):
        if not _CLASS_DECL_RE.match(lines[idx]):
            continue
        for back in range(idx - 1, max(-1, idx - _LOOKAROUND_LINES), -1):
            match = _DECORATOR_RE.match(lines[back].strip())
            if match:
                return (match.group(2) or "").strip()
            if lines[back].strip():
                break  # hit non-blank, non-decorator content -- no class decorator
        return ""
    return ""


def _method_after(lines: list[str], decorator_line_idx: int) -> tuple[str, int] | None:
    """The name of the first method/function declaration below this
    decorator, skipping past any other decorator lines in between -- a
    method commonly carries more than one (e.g. `@Get(...)` plus
    `@ApiResponse(...)`), and any of them may itself span several lines
    (an inline options object is common). Paren depth, not line-starts
    alone, is what actually tells a decorator's continuation line apart
    from the method declaration that follows it.
    """
    depth = lines[decorator_line_idx].count("(") - lines[decorator_line_idx].count(")")
    limit = min(len(lines), decorator_line_idx + 1 + _LOOKAROUND_LINES * 4)
    for j in range(decorator_line_idx + 1, limit):
        line = lines[j]
        stripped = line.strip()
        if depth > 0:
            depth += line.count("(") - line.count(")")
            continue
        if not stripped:
            continue
        if stripped.startswith("@") or stripped.startswith("["):
            depth += line.count("(") - line.count(")")
            continue
        match = _METHOD_DECL_RE.search(stripped)
        if match:
            return match.group(1), j
        return None  # first non-decorator content isn't a method decl -- give up
    return None


def find_declarative_entrypoints(repo_root: Path, files: list[str]) -> list[DeclarativeEntrypoint]:
    """Scan the given repo-relative files for the decorator-verb-near-a-
    method shape.

    `files` is caller-supplied on purpose -- this is meant to run over a
    small, targeted set (the changed files and their immediate directory
    neighbors), never a whole-repo sweep: the entrypoint controller for a
    changed handler is very often a SIBLING file, not the changed file
    itself (this is exactly what happened in the case that motivated this
    module -- the HTTP controller never appeared in the diff at all).
    """
    out: list[DeclarativeEntrypoint] = []
    seen: set[tuple[str, str, int]] = set()
    for rel_path in files:
        if Path(rel_path).suffix not in _CODE_EXTENSIONS:
            continue
        try:
            text = (repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            match = _DECORATOR_RE.match(line.strip())
            if not match:
                continue
            verb = _http_verb(match.group(1))
            if verb is None:
                continue
            found = _method_after(lines, idx)
            if found is None:
                continue
            symbol, decl_line = found
            own_path = (match.group(2) or "").strip().strip("'\"")
            key = (rel_path, symbol, decl_line)
            if key in seen:
                continue
            seen.add(key)
            out.append(DeclarativeEntrypoint(
                file=rel_path, symbol=symbol, http_method=verb, path=own_path,
                class_prefix=_enclosing_class_prefix(lines, idx), line=decl_line + 1,
            ))
    return out


def nearby_files(repo_root: Path, changed_files: tuple[str, ...], *, max_files: int = 40) -> list[str]:
    """Repo-relative paths worth scanning for a declarative entrypoint: each
    changed file's own containing directory, since a route's controller is
    routinely a sibling of the handler it dispatches to rather than the
    changed file itself. Capped, since this backs an LLM-context hint, not
    a full-repo index."""
    candidates: list[str] = []
    seen_dirs: set[Path] = set()
    for changed in changed_files:
        directory = (repo_root / changed).parent
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.suffix not in _CODE_EXTENSIONS:
                continue
            try:
                candidates.append(str(entry.relative_to(repo_root)))
            except ValueError:
                continue
            if len(candidates) >= max_files:
                return candidates
    return candidates
