#!/usr/bin/env python3
"""Phase C orchestrator: run the mutation oracle against a real dependency
(the Settings/Duration chain Phase B already verified structurally) and
against a known-FALSE candidate (as a noise/specificity control), on the
real Kokoro-FastAPI `py-real-01-max-output-duration` branch.

Reports results as `behavioral_coverage` evidence -- CONFIRMED, NOT_FLIPPED
(real per Phase A/B, but not covered by these mapped tests), or N/A for the
negative control -- never as a gate on Phase B's structural verification.

Not wired into any production path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mutation_oracle import MutationSpec, run_mutation_oracle  # noqa: E402

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
TARGET_BRANCH = "py-real-01-max-output-duration"
MAPPED_TESTS = ["api/tests/test_request_bounds.py"]


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


# The real dependency Phase B verified end-to-end: `_within_duration_ceiling`
# is the AfterValidator behind `Duration`, itself a field on
# `OpenAISpeechRequest`, the request model `create_speech` accepts. A
# classic mutmut-style relational-operator mutation (`>` -> `>=`) shifts the
# ceiling boundary by exactly one value -- directly targeted by
# `test_max_duration_seconds_accepts_at_server_ceiling` (currently expects
# value == ceiling to be ACCEPTED).
POSITIVE_CASE = MutationSpec(
    file="api/src/structures/schemas.py",
    line=73,
    original="if value > settings.max_output_duration_s:",
    mutated="if value >= settings.max_output_duration_s:",
    description="relational-operator mutation (> -> >=) on the duration ceiling check",
)

# A known-FALSE candidate from the prior 8-case fixture (PR6): model_status
# has no real connection to the Settings/Duration chain. Negating its own
# unrelated guard condition should NOT flip any duration-mapped test --
# a true-negative / specificity control, not a hypothesis.
NEGATIVE_CASE = MutationSpec(
    file="api/src/routers/development.py",
    line=536,
    original="if not settings.allow_dev_unload:",
    mutated="if settings.allow_dev_unload:",
    description="condition negation on an UNRELATED guard (model_status's own kill-switch)",
)


def _report(label: str, result) -> dict:
    print(f"\n=== {label}: {result.spec.description} ===")
    print(f"  {result.spec.file}:{result.spec.line}")
    print(f"  baseline (unmutated) passed: {result.baseline_passed}")
    print(f"  mutated passed: {result.mutated_passed}")
    print(f"  flipped: {result.flipped}")
    return {
        "label": label,
        "file": result.spec.file,
        "line": result.spec.line,
        "description": result.spec.description,
        "baseline_passed": result.baseline_passed,
        "mutated_passed": result.mutated_passed,
        "flipped": result.flipped,
    }


def main() -> int:
    status = _git(["status", "--porcelain"])
    if status:
        print(f"ERROR: {REPO_ROOT} has uncommitted changes, refusing to proceed:\n{status}")
        return 1
    original_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    _git(["checkout", "--quiet", TARGET_BRANCH])

    try:
        positive = run_mutation_oracle(REPO_ROOT, VENV_PYTHON, POSITIVE_CASE, MAPPED_TESTS)
        negative = run_mutation_oracle(REPO_ROOT, VENV_PYTHON, NEGATIVE_CASE, MAPPED_TESTS)
    finally:
        _git(["checkout", "--quiet", original_branch])

    positive_report = _report("POSITIVE (real dependency)", positive)
    negative_report = _report("NEGATIVE (false-candidate control)", negative)

    positive_report["behavioral_coverage"] = "CONFIRMED" if positive.flipped else "NOT_FLIPPED"
    negative_report["behavioral_coverage"] = "N/A (correctly no effect)" if not negative.flipped else "UNEXPECTED_FLIP"

    print(f"\nPositive case behavioral_coverage: {positive_report['behavioral_coverage']}")
    print(f"Negative case behavioral_coverage: {negative_report['behavioral_coverage']}")

    out = {"positive": positive_report, "negative": negative_report}
    out_path = Path(__file__).parent / "phase_c_results.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path} (repo restored to {original_branch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
