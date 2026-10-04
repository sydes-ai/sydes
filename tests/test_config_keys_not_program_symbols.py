"""Workflow/config keys are document structure, not program symbols.

A code index may record a workflow's `on:` / `permissions:` / `jobs:` keys as symbols. They
must not become changed program symbols (which would count as unresolved impact and trigger
AI recovery). The changed configuration file itself stays in the change.
"""

from __future__ import annotations

from sydes.verify.analyzer import attribute_changed_symbols, is_program_source
from sydes.verify.models import CHANGE_ADDED, ChangedFile, ChangeSet, Hunk


def _index() -> dict:
    def sym(name: str, kind: str, start: int, end: int) -> dict:
        return {"name": name, "qualified_name": name, "kind": kind, "start_line": start, "end_line": end}

    return {"repos": [{"repo": "app", "files": [
        {"path": ".github/workflows/check.yml", "symbols": [sym("on", "variable", 3, 5), sym("permissions", "variable", 7, 9),
                                                             sym("jobs", "variable", 11, 20)]},
        {"path": "api/service.py", "symbols": [sym("handle", "function", 1, 10)]},
    ]}]}


def test_config_keys_never_become_changed_program_symbols() -> None:
    change = ChangeSet(base="main", files=[
        ChangedFile(repo="app", path=".github/workflows/check.yml", change_type=CHANGE_ADDED),
        ChangedFile(repo="app", path="api/service.py", hunks=[Hunk(start_line=3, end_line=4)]),
    ])
    symbols = attribute_changed_symbols(change, _index())
    assert [s.name for s in symbols] == ["handle"]
    # the workflow file is still part of the change, for change analysis and inferred impact
    assert any(f.path == ".github/workflows/check.yml" for f in change.files)


def test_program_source_classification() -> None:
    for path in ("api/service.py", "src/app.controller.ts", "Main.java", "cmd/main.go", "Makefile"):
        assert is_program_source(path)
    for path in (".github/workflows/ci.yml", "config/app.yaml", "package.json", "pyproject.toml",
                 "README.md", "settings.ini", ".env.production"):
        assert not is_program_source(path)
