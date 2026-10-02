"""Choose which existing tests DiffGenome runs for a change, when the caller named none.

Python only. Deterministic, no model: test files are ranked by how directly they touch the
change, then cut to a budget so a large suite (Baserow: thousands of database tests) stays
affordable:

1. test files the change itself adds or modifies (always kept, up to the budget);
2. test files that name a changed function, weighted by how specific the name is (a name
   that appears in many test files, like `undo` or `get`, says little);
3. test files that import a changed module.

The result is forwarded to DiffGenome as ordinary arguments (`--tests` plus `--pytest-arg`),
so DiffGenome is unchanged. Whatever is selected is recorded and reported: absence of
execution is always relative to the tests that were run.
"""

from __future__ import annotations

import ast
import math
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BUDGET = 10  # test files
#: a changed function called by more application functions than this is a widely used helper:
#: its tests are found directly, and expanding through every caller would select half the suite
MAX_WRAPPERS_PER_FUNCTION = 10
DEFAULT_MAX_TESTS = 100  # test functions across the selected files (counted statically); DiffGenome's
# post-processing grows much faster than linearly: 55 Baserow tests take ~1 min, 300 did not
# finish in 15 (9.7 GB)
#: a changed name found in more than this share of all test files is too generic to rank by
#: (`get`, `do` across a large suite); small suites are never filtered below _GENERIC_FLOOR
#: files, otherwise a name used by two of ten test files would be dropped
_GENERIC_SHARE = 0.05
_GENERIC_FLOOR = 5
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


@dataclass
class TestSelection:
    files: list[str] = field(default_factory=list)
    #: file -> why it was selected ("changed in this diff", "calls restore_row, get_view_or_none", ...)
    reasons: dict[str, str] = field(default_factory=dict)
    candidates: int = 0  # test files that matched at all, before the budget
    budget: int = DEFAULT_BUDGET
    max_tests: int = DEFAULT_MAX_TESTS
    tests_selected: int = 0  # test functions in the selected files (static count)
    changed_functions: list[str] = field(default_factory=list)
    #: file -> "changed" | "direct" | "transitive" | "imports"
    tiers: dict[str, str] = field(default_factory=dict)
    #: changed functions whose callers were not expanded (too many: a widely used helper)
    transitive_skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "auto",
            "files": list(self.files),
            "reasons": dict(self.reasons),
            "candidates": self.candidates,
            "budget": self.budget,
            "max_tests": self.max_tests,
            "tests_selected": self.tests_selected,
            "tiers": dict(self.tiers),
            "transitive_skipped": list(self.transitive_skipped),
        }


def is_python_test_file(path: str) -> bool:
    name = Path(path).name
    if not name.endswith(".py") or name == "conftest.py":
        return False
    return name.startswith("test_") or name.endswith("_test.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _changed_lines(repo: Path, base: str, head: str, path: str) -> set[int]:
    lines: set[int] = set()
    for m in _HUNK.finditer(_git(repo, "diff", "-U0", base, head, "--", path)):
        start, count = int(m.group(1)), int(m.group(2) or "1")
        lines.update(range(start, start + max(count, 1)))
    return lines


def _changed_functions(source: str, lines: set[int]) -> set[tuple[str, str | None]]:
    """(function name, enclosing class or None) for every function a changed line falls in."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[tuple[str, str | None]] = set()

    def visit(node: ast.AST, cls: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                end = getattr(child, "end_lineno", None) or child.lineno
                if any(child.lineno <= n <= end for n in lines) and not (
                    child.name.startswith("__") and child.name.endswith("__")
                ):
                    found.add((child.name, cls))
                visit(child, cls)
            else:
                visit(child, cls)

    visit(tree, None)
    return found


def _module_suffix(path: str) -> str | None:
    """`backend/src/baserow/core/trash/handler.py` -> `core.trash.handler` (the last three parts:
    specific enough to match an import, independent of where the source root starts)."""
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if len(parts) < 2:
        return None
    return ".".join(parts[-3:])


_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test", re.M)


def _wrappers(
    repo: Path, source_files: list[str], functions: set[tuple[str, str | None]],
) -> dict[tuple[str, str | None], set[tuple[str, str | None]]]:
    """Application functions whose own body calls a changed function (same rule as for tests:
    a call by name, and for a method its class named), keyed by the changed function. Only
    source files that mention a changed name are parsed."""
    names = {n for n, _ in functions}
    calls = {n: re.compile(rf"\b{re.escape(n)}\s*\(") for n in names}
    out: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {fn: set() for fn in functions}
    for path in source_files:
        try:
            text = (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(f"{n}(" in text or re.search(calls[n], text) for n in names):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()

        def visit(node: ast.AST, cls: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    visit(child, child.name)
                elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    body = "\n".join(lines[child.lineno - 1 : (child.end_lineno or child.lineno)])
                    for fn in functions:
                        name, fcls = fn
                        if (child.name, cls) == fn or not calls[name].search(body):
                            continue
                        # a method must be reached through its class: self./cls. inside the
                        # class itself, or the class named in the body
                        if fcls is not None and cls != fcls and not re.search(rf"\b{re.escape(fcls)}\b", body):
                            continue
                        if not (child.name.startswith("__") and child.name.endswith("__")):
                            out[fn].add((child.name, cls))
                    visit(child, cls)

        visit(tree, None)
    return out


def select_python_tests(
    repo: Path, base: str, head: str, *, budget: int = DEFAULT_BUDGET,
    max_tests: int = DEFAULT_MAX_TESTS,
) -> TestSelection:
    changed = [
        p for p in _git(repo, "diff", "--name-only", "--diff-filter=AMR", base, head).splitlines()
        if p.endswith(".py")
    ]
    all_tests = [p for p in _git(repo, "ls-files", "*.py").splitlines() if is_python_test_file(p)]
    existing = set(all_tests)
    selection = TestSelection(budget=budget, max_tests=max_tests)

    changed_tests = [p for p in changed if p in existing and (repo / p).is_file()]
    functions: set[tuple[str, str | None]] = set()
    modules: set[str] = set()
    for path in changed:
        if path in existing or not (repo / path).is_file():
            continue
        try:
            source = (repo / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        functions |= _changed_functions(source, _changed_lines(repo, base, head, path))
        suffix = _module_suffix(path)
        if suffix:
            modules.add(suffix)
    selection.changed_functions = sorted(f"{c}.{n}" if c else n for n, c in functions)

    texts: dict[str, str] = {}
    for path in all_tests:
        try:
            texts[path] = (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # a function counts as used by a test file when the file calls it by name and, for a
    # method, also names its class
    calls = {n: re.compile(rf"\b{re.escape(n)}\s*\(") for n, _ in functions}
    classes = {c: re.compile(rf"\b{re.escape(c)}\b") for _, c in functions if c}

    def uses(text: str, fn: tuple[str, str | None]) -> bool:
        name, cls = fn
        return bool(calls[name].search(text)) and (cls is None or bool(classes[cls].search(text)))

    df = {fn: sum(1 for t in texts.values() if uses(t, fn)) for fn in functions}
    total = max(len(texts), 1)
    weight = {
        fn: math.log(total / d) for fn, d in df.items() if 0 < d <= max(_GENERIC_FLOOR, _GENERIC_SHARE * total)
    }
    imports = {m: re.compile(rf"(?:from|import)\s+[\w.]*\b{re.escape(m)}\b") for m in modules}

    # tier 3 inputs: one level of callers in application code (wrapper -> changed function)
    sources = [p for p in _git(repo, "ls-files", "*.py").splitlines() if p not in existing]
    wrappers_of = _wrappers(repo, sources, functions)
    wrappers: dict[tuple[str, str | None], tuple[str, str | None]] = {}  # wrapper -> changed fn
    for fn, ws in sorted(wrappers_of.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
        if len(ws) > MAX_WRAPPERS_PER_FUNCTION:
            selection.transitive_skipped.append(f"{fn[1]}.{fn[0]}" if fn[1] else fn[0])
            continue
        for w in sorted(ws, key=lambda x: (x[1] or "", x[0])):
            wrappers.setdefault(w, fn)
    wcalls = {n: re.compile(rf"\b{re.escape(n)}\s*\(") for n, _ in wrappers}
    wclasses = {c: re.compile(rf"\b{re.escape(c)}\b") for _, c in wrappers if c}

    def uses_wrapper(text: str, w: tuple[str, str | None]) -> bool:
        name, cls = w
        return bool(wcalls[name].search(text)) and (cls is None or bool(wclasses[cls].search(text)))

    wdf = {w: sum(1 for t in texts.values() if uses_wrapper(t, w)) for w in wrappers}
    wweight = {
        w: math.log(total / d) for w, d in wdf.items() if 0 < d <= max(_GENERIC_FLOOR, _GENERIC_SHARE * total)
    }

    def label(fn: tuple[str, str | None]) -> str:
        return f"{fn[1]}.{fn[0]}" if fn[1] else fn[0]

    direct: list[tuple[float, str, str]] = []
    transitive: list[tuple[float, str, str]] = []
    imports_only: list[tuple[float, str, str]] = []
    changed_scored: list[tuple[float, str]] = []
    for path, text in texts.items():
        if path in changed_tests:
            changed_scored.append((sum(weight[fn] for fn in weight if uses(text, fn)), path))
            continue
        hits = sorted((fn for fn in weight if uses(text, fn)), key=lambda fn: (-weight[fn], label(fn)))
        if hits:
            shown = [label(fn) for fn in hits[:3]]
            reason = f"calls {', '.join(shown)}" + (f" +{len(hits) - 3}" if len(hits) > 3 else "")
            direct.append((sum(weight[fn] for fn in hits), path, reason))
            continue
        whits = sorted((w for w in wweight if uses_wrapper(text, w)), key=lambda w: (-wweight[w], label(w)))
        if whits:
            shown = [f"{label(wrappers[w])} via {label(w)}" for w in whits[:2]]
            reason = "reaches " + ", ".join(shown) + (f" +{len(whits) - 2}" if len(whits) > 2 else "")
            transitive.append((sum(wweight[w] for w in whits), path, reason))
            continue
        imported = [m for m, pat in imports.items() if pat.search(text)]
        if imported:
            imports_only.append((0.5, path, f"imports {imported[0]}"))
    for tier in (direct, transitive, imports_only):
        tier.sort(key=lambda t: (-t[0], t[1]))
    changed_scored.sort(key=lambda t: (-t[0], t[1]))
    changed_tests = [path for _score, path in changed_scored]
    # importing a changed module is weak evidence (a utility module is imported everywhere):
    # only a fallback when nothing changes, calls or reaches the changed functions
    fallback = imports_only if not (changed_tests or direct or transitive) else []
    selection.candidates = len(changed_tests) + len(direct) + len(transitive) + len(fallback)

    def add(path: str, reason: str, tier: str) -> None:
        selection.files.append(path)
        selection.reasons[path] = reason
        selection.tiers[path] = tier
        selection.tests_selected += len(_TEST_DEF.findall(texts.get(path, "")))

    # 1. The PR's own changed test files always run (within the file budget): they are its
    #    declared verification (requests f8bec2f7: a 233-test file had crowded out two others).
    for path in changed_tests[:budget]:
        add(path, "changed in this diff", "changed")
    # 2. direct callers, 3. callers of a direct application caller (depth 1), import-only
    #    as a fallback; max_tests bounds all of them together, tier by tier, and a file that
    #    does not fit is skipped so smaller relevant files still can.
    for tier_name, tier in (("direct", direct), ("transitive", transitive), ("imports", fallback)):
        for _score, path, reason in tier:
            if len(selection.files) >= budget:
                return selection
            n = len(_TEST_DEF.findall(texts.get(path, "")))
            if selection.tests_selected + n > max_tests:
                continue
            add(path, reason, tier_name)
    return selection


def has_tests_arg(args: list[str]) -> bool:
    return any(a == "--tests" or a.startswith("--tests=") for a in args)


def runtime_of(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "--runtime" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--runtime="):
            return a.split("=", 1)[1]
    return None


def with_selected_tests(args: list[str], selection: TestSelection) -> list[str]:
    """DiffGenome's own arguments for the selection: the first file as `--tests`, the rest as
    extra pytest paths."""
    if not selection.files:
        return list(args)
    first, *rest = selection.files
    extra = ["--tests", first]
    if rest:
        extra.append("--pytest-arg=" + shlex.join(rest))
    return [*args, *extra]
