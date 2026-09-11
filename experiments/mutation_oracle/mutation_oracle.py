"""Phase C: mutation testing as a REACHABILITY ORACLE -- an execution-based
confirmation step layered alongside Phase B's structural claim verifier, not
a gate on it.

Per explicit design decision this round: a mutation that fails to flip any
mapped test does NOT mean "unreachable" (the same asymmetry this whole
engagement has repeated from the start: absence of a signal proves
nothing). It means "syntactically real (per Phase A/B), but not
behaviorally covered by the tests currently mapped to it" -- which is
useful, actionable evidence (add a test), never a reason to block or
downgrade a structurally-verified promotion. Mutation results are reported
as a `behavioral_coverage` tier alongside `structural_status`, never
folded into a single pass/fail gate.

Mutations are applied to the REAL file on disk (in the target repo's own
checkout), one exact line replacement at a time, always inside a
try/finally that restores the file via `git checkout --` -- the same
git-safety discipline (status guard before switching state, restore after)
used throughout every prior experiment in this engagement.

Not wired into any production path.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MutationSpec:
    file: str          # repo-relative path
    line: int           # 1-indexed line to replace
    original: str       # exact expected current line content (whitespace-sensitive
                        # match required -- refuses to mutate if it doesn't match,
                        # rather than silently mutating the wrong line)
    mutated: str        # the replacement line content
    description: str    # human-readable mutation operator name


@dataclass(frozen=True)
class MutationResult:
    spec: MutationSpec
    baseline_passed: bool
    baseline_output: str
    mutated_passed: bool
    mutated_output: str
    flipped: bool  # True iff baseline passed and the mutation made it fail


def _git(repo_root: Path, args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run_pytest(repo_root: Path, venv_python: Path, test_ids: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        [str(venv_python), "-m", "pytest", *test_ids, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=repo_root, capture_output=True, text=True, timeout=120,
    )
    output = proc.stdout[-4000:] + proc.stderr[-1000:]
    return proc.returncode == 0, output


def apply_mutation(repo_root: Path, spec: MutationSpec) -> None:
    path = repo_root / spec.file
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    idx = spec.line - 1
    if not (0 <= idx < len(lines)):
        raise ValueError(f"line {spec.line} out of range for {spec.file}")
    current = lines[idx].rstrip("\n").rstrip("\r")
    if current.strip() != spec.original.strip():
        raise ValueError(
            f"refusing to mutate {spec.file}:{spec.line} -- expected "
            f"{spec.original!r}, found {current!r} (file has drifted since this spec was written)"
        )
    newline = "\n" if lines[idx].endswith("\n") else ""
    indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip(" "))]
    lines[idx] = f"{indent}{spec.mutated.strip()}{newline}"
    path.write_text("".join(lines), encoding="utf-8")


def restore_file(repo_root: Path, file: str) -> None:
    _git(repo_root, ["checkout", "--", file])


def run_mutation_oracle(
    repo_root: Path, venv_python: Path, spec: MutationSpec, test_ids: list[str],
) -> MutationResult:
    """Baseline (unmutated) run first -- a mutation "flip" is only
    meaningful evidence if the mapped tests actually passed before the
    mutation; a test that was already failing proves nothing about the
    mutation itself."""
    status = _git(repo_root, ["status", "--porcelain", "--", spec.file])
    if status:
        raise RuntimeError(f"{spec.file} has uncommitted changes -- refusing to mutate")

    baseline_passed, baseline_output = _run_pytest(repo_root, venv_python, test_ids)

    apply_mutation(repo_root, spec)
    try:
        mutated_passed, mutated_output = _run_pytest(repo_root, venv_python, test_ids)
    finally:
        restore_file(repo_root, spec.file)

    flipped = baseline_passed and not mutated_passed
    return MutationResult(
        spec=spec, baseline_passed=baseline_passed, baseline_output=baseline_output,
        mutated_passed=mutated_passed, mutated_output=mutated_output, flipped=flipped,
    )
