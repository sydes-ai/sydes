"""Unit tests for the Phase C mutation oracle harness, using tiny synthetic
git repos (restoration relies on `git checkout --`, so a real repo is
needed, not just a bare tmp_path)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mutation_oracle import MutationSpec, apply_mutation, restore_file, run_mutation_oracle  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


@pytest.fixture
def toy_repo(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "lib.py").write_text(
        "def within_limit(value):\n"
        "    if value > 10:\n"
        "        raise ValueError('too big')\n"
        "    return value\n",
        encoding="utf-8",
    )
    (root / "test_lib.py").write_text(
        "from lib import within_limit\n"
        "\n"
        "def test_accepts_at_limit():\n"
        "    assert within_limit(10) == 10\n"
        "\n"
        "def test_rejects_above_limit():\n"
        "    try:\n"
        "        within_limit(11)\n"
        "        assert False, 'should have raised'\n"
        "    except ValueError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_apply_mutation_matches_indent_and_replaces_line(toy_repo):
    spec = MutationSpec(
        file="lib.py", line=2, original="if value > 10:",
        mutated="if value >= 10:", description="boundary shift",
    )
    apply_mutation(toy_repo, spec)
    content = (toy_repo / "lib.py").read_text(encoding="utf-8")
    assert "if value >= 10:" in content
    assert content.startswith("def within_limit")
    restore_file(toy_repo, "lib.py")
    assert "if value > 10:" in (toy_repo / "lib.py").read_text(encoding="utf-8")


def test_apply_mutation_refuses_when_line_has_drifted(toy_repo):
    spec = MutationSpec(
        file="lib.py", line=2, original="if value > 999:",  # wrong -- does not match real content
        mutated="if value >= 999:", description="boundary shift",
    )
    with pytest.raises(ValueError, match="drifted"):
        apply_mutation(toy_repo, spec)


def test_oracle_confirms_flip_for_a_real_dependency(toy_repo):
    venv_python = Path(sys.executable)
    spec = MutationSpec(
        file="lib.py", line=2, original="if value > 10:",
        mutated="if value >= 10:", description="boundary shift",
    )
    result = run_mutation_oracle(toy_repo, venv_python, spec, ["test_lib.py"])
    assert result.baseline_passed
    assert not result.mutated_passed
    assert result.flipped
    # File must be restored exactly, not left mutated.
    assert "if value > 10:" in (toy_repo / "lib.py").read_text(encoding="utf-8")


def test_oracle_reports_not_flipped_for_an_unrelated_mutation(toy_repo):
    """A mutation to code the mapped tests never exercise must NOT flip --
    the true-negative / specificity case, mirroring the real Phase C
    control against `model_status`."""
    (toy_repo / "lib.py").write_text(
        "def within_limit(value):\n"
        "    if value > 10:\n"
        "        raise ValueError('too big')\n"
        "    return value\n"
        "\n"
        "def unrelated_helper(flag):\n"
        "    if not flag:\n"
        "        return 'off'\n"
        "    return 'on'\n",
        encoding="utf-8",
    )
    _git(toy_repo, "add", "-A")
    _git(toy_repo, "commit", "-q", "-m", "add unrelated helper")

    venv_python = Path(sys.executable)
    spec = MutationSpec(
        file="lib.py", line=7, original="if not flag:",
        mutated="if flag:", description="condition negation on unrelated code",
    )
    result = run_mutation_oracle(toy_repo, venv_python, spec, ["test_lib.py"])
    assert result.baseline_passed
    assert result.mutated_passed  # unaffected
    assert not result.flipped


def test_oracle_refuses_when_target_file_has_uncommitted_changes(toy_repo):
    (toy_repo / "lib.py").write_text("dirty\n", encoding="utf-8")
    venv_python = Path(sys.executable)
    spec = MutationSpec(file="lib.py", line=1, original="dirty", mutated="dirty2", description="x")
    with pytest.raises(RuntimeError, match="uncommitted"):
        run_mutation_oracle(toy_repo, venv_python, spec, ["test_lib.py"])
