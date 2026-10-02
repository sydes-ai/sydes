"""`sydes runtime-detect`: what runtime evidence would use, without running any test.

Deterministic: the Python project and environment detection (with `.sydes.yml` overrides),
the DiffGenome arguments it produces, and the automatic test selection for the change. With
`--prepare DIR` it first builds the test environment (explicit; installs the project's
declared dependencies) and then detects with it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from sydes.behavioral.python_project import (
    OVERRIDE_FILE,
    DetectionError,
    changed_files,
    detect_python_project,
    diffgenome_args,
    prepare_environment,
)
from sydes.behavioral.test_selection import select_python_tests, with_selected_tests


def runtime_detect_command(
    repo: Annotated[Path, typer.Option("--repo", help="Repository root.")] = Path("."),
    base: Annotated[str, typer.Option("--base", help="Base ref; the change is merge-base..HEAD.")] = "main",
    test_budget: Annotated[int, typer.Option("--runtime-test-budget")] = 10,
    prepare: Annotated[
        Path | None,
        typer.Option("--prepare", help="Create the test environment here first (explicit install)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    repo = repo.resolve()

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    head = git("rev-parse", "HEAD")
    merge_base = git("merge-base", base, head)
    changed = changed_files(repo, merge_base, head)
    prepared: list[str] = []
    try:
        python = None
        if prepare is not None:
            python, prepared = prepare_environment(repo, changed, prepare)
        project = detect_python_project(repo, changed, prepared_python=python)
    except DetectionError as exc:
        if as_json:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=1))
        else:
            typer.echo(f"Runtime evidence: not available\n  {exc}")
        raise typer.Exit(code=2) from exc
    args = diffgenome_args(project, changed)
    selection = select_python_tests(repo, merge_base, head, budget=test_budget)
    full = with_selected_tests(args, selection)
    if as_json:
        typer.echo(json.dumps({
            "ok": True, "project": project.as_dict(), "prepared": prepared,
            "selection": selection.as_dict(), "diffgenome_args": full,
        }, indent=1))
        return

    def row(label: str, value: object, name: str | None = None) -> str:
        origin = f"  [{project.origin.get(name, 'inferred')}]" if name else ""
        return f"  {label:<16}{value}{origin}"

    rel = lambda p: p.replace(str(repo) + "/", "")  # noqa: E731
    lines = [
        "Detected Python project",
        row("Project root:", project.project_root, "project_root"),
        row("Python:", f"{rel(project.python or '?')} ({project.python_version})", "python"),
        row("Source roots:", ", ".join(project.source_roots) or "-", "source_roots"),
        row("Test roots:", ", ".join(project.test_roots) or "-", "test_roots"),
        row("Import roots:", ", ".join(project.import_roots) or "none", "import_roots"),
        row("Pytest config:", project.pytest_config or "none"),
        row("Env:", ", ".join(sorted(project.env)) or "none", "env" if project.env else None),
        row("Loopback:", "allowed" if project.allow_loopback else "no"),
        row("Confidence:", project.confidence),
        f"  Overrides:      {OVERRIDE_FILE}: " + (
            ", ".join(k for k, o in project.origin.items() if o == OVERRIDE_FILE) or "none"
        ),
        "",
        f"Selected tests ({len(selection.files)} file(s), ~{selection.tests_selected} test functions, "
        f"{selection.candidates} candidate file(s)):",
        *[f"  {f}  — {selection.reasons[f]}" for f in selection.files],
        "",
        "Why:",
        *[f"  {k}: {v}" for k, v in project.reasons.items()],
    ]
    if project.rejected_interpreters:
        lines += ["", "Rejected interpreters:", *[f"  {rel(r)}" for r in project.rejected_interpreters]]
    if project.warnings:
        lines += ["", "Warnings:", *[f"  {w}" for w in project.warnings]]
    if prepared:
        lines += ["", "Prepared environment with:", *[f"  {rel(c)}" for c in prepared]]
    lines += ["", "DiffGenome arguments:", "  " + rel(shlex.join(full))]
    if not selection.files:
        lines += ["", "No test file changes or calls the changed functions: runtime evidence would be unavailable."]
    typer.echo("\n".join(lines))
