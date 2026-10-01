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
DEFAULT_MAX_TESTS = 100  # test functions across the selected files (counted statically); DiffGenome's
# post-processing grows much faster than linearly: 55 Baserow tests take ~1 min, 300 did not
# finish in 15 (9.7 GB)
#: a changed name found in more than this share of all test files is too generic to rank by
_GENERIC_SHARE = 0.05
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

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "auto",
            "files": list(self.files),
            "reasons": dict(self.reasons),
            "candidates": self.candidates,
            "budget": self.budget,
            "max_tests": self.max_tests,
            "tests_selected": self.tests_selected,
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
        fn: math.log(total / d) for fn, d in df.items() if 0 < d <= max(1, _GENERIC_SHARE * total)
    }
    imports = {m: re.compile(rf"(?:from|import)\s+[\w.]*\b{re.escape(m)}\b") for m in modules}

    scored: list[tuple[float, str, str]] = []
    changed_scored: list[tuple[float, str]] = []
    for path, text in texts.items():
        if path in changed_tests:
            changed_scored.append((sum(weight[fn] for fn in weight if uses(text, fn)), path))
            continue
        hits = sorted((fn for fn in weight if uses(text, fn)), key=lambda fn: -weight[fn])
        imported = [m for m, pat in imports.items() if pat.search(text)]
        score = sum(weight[fn] for fn in hits) + (0.5 if imported else 0.0)
        if score <= 0:
            continue
        shown = [f"{c}.{n}" if c else n for n, c in hits[:3]]
        reason = (
            f"calls {', '.join(shown)}" + (f" +{len(hits) - 3}" if len(hits) > 3 else "")
            if hits else f"imports {imported[0]}"
        )
        scored.append((score, path, reason))
    scored.sort(key=lambda t: (-t[0], t[1]))
    # importing a changed module is weak evidence (a utility module is imported everywhere):
    # only a fallback when no test file changes or calls the changed functions
    callers = [t for t in scored if t[2].startswith("calls ")]
    if changed_tests or callers:
        scored = callers
    changed_scored.sort(key=lambda t: (-t[0], t[1]))
    changed_tests = [path for _score, path in changed_scored]
    selection.candidates = len(changed_tests) + len(scored)

    def add(path: str, reason: str) -> None:
        selection.files.append(path)
        selection.reasons[path] = reason
        selection.tests_selected += len(_TEST_DEF.findall(texts.get(path, "")))

    ranked = [(path, "changed in this diff") for path in changed_tests]
    ranked += [(path, reason) for _score, path, reason in scored]
    for path, reason in ranked:
        if len(selection.files) >= budget:
            break
        n = len(_TEST_DEF.findall(texts.get(path, "")))
        if selection.files and selection.tests_selected + n > max_tests:
            continue  # a smaller, lower-ranked file may still fit
        add(path, reason)
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
