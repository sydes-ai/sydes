"""Automatic selection of the existing tests DiffGenome runs (Python, no `--tests` given)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sydes.behavioral.test_selection import (
    has_tests_arg,
    runtime_of,
    select_python_tests,
    with_selected_tests,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _write(root, "app/rows.py", (
        "class RowHandler:\n"
        "    def restore_row(self, row):\n"
        "        return row\n"
        "\n"
        "    def other(self):\n"
        "        return 1\n"
        "\n"
        "def undo(x):\n"
        "    return x\n"
    ))
    _write(root, "tests/test_rows.py", "from app.rows import RowHandler\n\ndef test_a():\n    RowHandler().restore_row(1)\n")
    _write(root, "tests/test_unrelated.py", "def test_b():\n    restore_row = 1\n")  # name, no call
    _write(root, "tests/test_other_class.py", "def test_c():\n    Thing().restore_row(1)\n")  # call, wrong class
    _write(root, "tests/test_changed.py", "def test_d():\n    assert True\n")
    _write(root, "tests/test_imports.py", "from app.rows import undo\n\ndef test_e():\n    pass\n")
    for i in range(30):  # `undo(` everywhere: too generic to rank by
        _write(root, f"tests/test_noise_{i}.py", "def test_n():\n    undo(1)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, "app/rows.py", (root / "app/rows.py").read_text().replace("return row", "return row or None").replace("return x", "return x or 0"))
    _write(root, "tests/test_changed.py", "def test_d():\n    assert 1\n")
    _git(root, "commit", "-qam", "change")
    return root, base, _git(root, "rev-parse", "HEAD")


def test_changed_tests_first_then_callers_of_changed_functions(repo) -> None:
    root, base, head = repo
    sel = select_python_tests(root, base, head, budget=3)
    assert sel.changed_functions == ["RowHandler.restore_row", "undo"]
    assert sel.files[0] == "tests/test_changed.py" and sel.reasons["tests/test_changed.py"] == "changed in this diff"
    assert sel.files[1] == "tests/test_rows.py" and sel.reasons["tests/test_rows.py"] == "calls RowHandler.restore_row"
    # a bare name, or the method on another class, does not count; generic `undo(` is not ranked
    assert "tests/test_unrelated.py" not in sel.files and "tests/test_other_class.py" not in sel.files
    assert not any("noise" in f for f in sel.files)
    # importing a changed module is only a fallback; here changed and calling tests exist
    assert sel.files == ["tests/test_changed.py", "tests/test_rows.py"]


def test_import_only_matches_are_a_fallback(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _write(root, "app/util.py", "def helper():\n    return 1\n")
    _write(root, "tests/test_util.py", "from app.util import helper as h\n\ndef test_x():\n    assert h\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, "app/util.py", "def helper():\n    return 2\n")
    _git(root, "commit", "-qam", "change")
    sel = select_python_tests(root, base, _git(root, "rev-parse", "HEAD"))
    assert sel.files == ["tests/test_util.py"] and sel.reasons["tests/test_util.py"] == "imports app.util"


def test_budget_caps_files_and_test_count(repo) -> None:
    root, base, head = repo
    assert select_python_tests(root, base, head, budget=1).files == ["tests/test_changed.py"]
    sel = select_python_tests(root, base, head, budget=10, max_tests=1)
    assert sel.files == ["tests/test_changed.py"] and sel.tests_selected == 1


def test_selection_is_forwarded_as_ordinary_diffgenome_arguments(repo) -> None:
    root, base, head = repo
    sel = select_python_tests(root, base, head, budget=2)
    args = with_selected_tests(["--runtime", "python"], sel)
    assert args == ["--runtime", "python", "--tests", "tests/test_changed.py", "--pytest-arg=tests/test_rows.py"]
    assert runtime_of(args) == "python" and has_tests_arg(args)
    assert not has_tests_arg(["--runtime", "python", "--test-root", "tests"])


def test_changed_test_files_are_never_dropped_by_the_test_budget(tmp_path: Path) -> None:
    """requests f8bec2f7: a changed 233-test file filled the budget and the changed
    test_help.py (which runs help._implementation) was left out."""
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _write(root, "app/help.py", "def info():\n    return 1\n")
    _write(root, "tests/test_big.py", "".join(f"def test_{i}():\n    pass\n" for i in range(150)))
    _write(root, "tests/test_help.py", "from app.help import info\n\ndef test_info():\n    assert info()\n")
    _write(root, "tests/test_calls.py", "from app.help import info\n\ndef test_x():\n    info()\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, "app/help.py", "def info():\n    return 2\n")
    _write(root, "tests/test_big.py", (root / "tests/test_big.py").read_text() + "\n")
    _write(root, "tests/test_help.py", (root / "tests/test_help.py").read_text() + "\n")
    _git(root, "commit", "-qam", "change")
    sel = select_python_tests(root, base, _git(root, "rev-parse", "HEAD"), max_tests=100)
    assert set(sel.files) == {"tests/test_big.py", "tests/test_help.py"}  # both changed files
    assert "tests/test_calls.py" not in sel.files  # the budget still bounds callers
