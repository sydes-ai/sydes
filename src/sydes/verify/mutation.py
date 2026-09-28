"""Targeted, comparator-class mutation verification for change-introduced
validation obligations.

Phase 1 of the test-understanding capability evaluation (2026-09-26):
deliberately narrow, not a mutation-testing engine. It only flips a
strict/non-strict comparison operator (`>`<->`>=`, `<`<->`<=`) found on a
line inside the diff's OWN hunks, for an `introduced_by_change` validation
obligation whose own mapped test already passed unmutated. Reuses
`test_execution.execute_mapped_test` unmodified -- no new test-running
infrastructure. The mutated file is always restored, even if execution
raises, before this function returns control.

Evidence semantics: see `MutationResult`'s docstring in `verify.models`. A
surviving mutant is reported as a possible gap, never as proof the behavior
is untested -- the mapped-test set may simply be incomplete, or the mutant
may be semantically equivalent to the original in ways this module makes no
attempt to detect.

Opt-in only (`--mutation-verify`, off by default); capped at
`_MAX_MUTATIONS_PER_RUN` obligations per run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import re

from sydes.verify.models import (
    OBLIGATION_VALIDATION,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    AffectedFlow,
    ChangeVerificationResult,
    Hunk,
    MutationResult,
    VerificationObligation,
)
from sydes.verify.source_files import RepoFiles
from sydes.verify.test_execution import ExecutionSettings, detect_frameworks, execute_mapped_test

_MAX_MUTATIONS_PER_RUN = 3

# Longer operators checked first so `>=`/`<=` are never partially matched as
# a bare `>`/`<` plus a stray `=`. The look-around excludes `==`, `!=`,
# `->`, `=>`, and doubled tokens (`<<`, `>>`) -- deliberately conservative:
# missing a comparator just means this obligation is skipped (safe), while
# matching the wrong token would mutate something unrelated.
_COMPARATOR_RE = re.compile(r"(?<![<>=!-])(>=|<=)(?!=)|(?<![<>=!-])([<>])(?![<>=])")

_FLIP = {">": ">=", ">=": ">", "<": "<=", "<=": "<"}


def _find_comparator_in_hunks(
    repo_root: Path, file: str, hunks: list[Hunk]
) -> tuple[int, str, int, int] | None:
    """First comparator token found on a line inside `hunks` for `file`:
    `(1-based line number, operator, match start, match end)`, or `None`."""
    path = repo_root / file
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for hunk in sorted(hunks, key=lambda h: h.start_line):
        for line_no in range(hunk.start_line, hunk.end_line + 1):
            if line_no < 1 or line_no > len(lines):
                continue
            match = _COMPARATOR_RE.search(lines[line_no - 1])
            if match:
                op = match.group(1) or match.group(2)
                return line_no, op, match.start(), match.end()
    return None


@contextmanager
def _mutated_file(
    repo_root: Path, file: str, line_no: int, start: int, end: int, new_op: str
) -> Iterator[None]:
    """Write the mutated line, yield, then restore the original file
    content verbatim -- unconditionally, even if the caller raises."""
    path = repo_root / file
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    target = lines[line_no - 1]
    ending = ""
    body = target
    for eol in ("\r\n", "\n", "\r"):
        if target.endswith(eol):
            ending = eol
            body = target[: -len(eol)]
            break
    lines[line_no - 1] = body[:start] + new_op + body[end:] + ending
    try:
        path.write_text("".join(lines), encoding="utf-8")
        yield
    finally:
        path.write_text(original, encoding="utf-8")


def _candidate_files_in_priority_order(
    obligation: VerificationObligation, changed_file_hunks: dict[str, list[Hunk]]
) -> list[tuple[str, str]]:
    """Changed files to search for a mutable comparator, most-relevant first.

    Returns `(file, location_basis)` pairs (see `MutationResult.location_basis`).
    The obligation's own evidence is searched first. A validation's boundary
    check and the exception it raises are commonly split across two files in
    one module (e.g. a route handler that raises, and a service module that
    holds the actual comparison) -- an unrelated changed file sharing a
    directory with the evidence is checked next, before the broad,
    unscoped "any other changed file in this diff" fallback this module
    always had. This never asserts the chosen file is semantically related
    to the obligation, only that it is the least-arbitrary of the files
    actually available -- see `location_basis` on the result.
    """
    evidence_files: list[str] = []
    seen: set[str] = set()
    for ev in obligation.evidence:
        if ev.file and ev.file in changed_file_hunks and ev.file not in seen:
            evidence_files.append(ev.file)
            seen.add(ev.file)

    evidence_dirs = {str(Path(f).parent) for f in evidence_files}
    same_dir = sorted(
        file
        for file in changed_file_hunks
        if file not in seen and str(Path(file).parent) in evidence_dirs
    )
    seen.update(same_dir)

    remaining = sorted(file for file in changed_file_hunks if file not in seen)

    return (
        [(file, "evidence") for file in evidence_files]
        + [(file, "same_directory") for file in same_dir]
        + [(file, "other_changed_file") for file in remaining]
    )


def _eligible_obligations(
    result: ChangeVerificationResult,
) -> list[tuple[VerificationObligation, AffectedFlow]]:
    """`introduced_by_change` validation obligations whose own mapped test
    already passed unmutated -- the only shape this MVP checks."""
    out: list[tuple[VerificationObligation, AffectedFlow]] = []
    for flow in result.affected_flows:
        for obligation in flow.obligations:
            if (
                obligation.kind == OBLIGATION_VALIDATION
                and obligation.introduced_by_change
                and obligation.status == VERIFICATION_PASSED
                and obligation.mapped_tests
            ):
                out.append((obligation, flow))
    return out


def run_mutation_verification(
    result: ChangeVerificationResult,
    *,
    repo_root: Path,
    repo_files: RepoFiles,
    changed_file_hunks: dict[str, list[Hunk]],
    settings: ExecutionSettings,
) -> None:
    """Attach `obligation.mutation` for up to `_MAX_MUTATIONS_PER_RUN`
    eligible obligations (see `_eligible_obligations`). An obligation with
    no comparator reachable in its own evidence file or any changed file's
    hunks is left with `mutation` unset -- "nothing to check", not "checked
    and clean"."""
    detections = detect_frameworks(repo_files)
    budget = _MAX_MUTATIONS_PER_RUN

    for obligation, _flow in _eligible_obligations(result):
        if budget <= 0:
            break
        found: tuple[str, int, str, int, int, str] | None = None
        for file, basis in _candidate_files_in_priority_order(obligation, changed_file_hunks):
            hunks = changed_file_hunks.get(file)
            if not hunks:
                continue
            located = _find_comparator_in_hunks(repo_root, file, hunks)
            if located:
                line_no, op, start, end = located
                found = (file, line_no, op, start, end, basis)
                break
        if found is None:
            continue
        file, line_no, op, start, end, location_basis = found
        new_op = _FLIP[op]
        test = obligation.mapped_tests[0]

        try:
            with _mutated_file(repo_root, file, line_no, start, end, new_op):
                execution = execute_mapped_test(
                    test=test, detections=detections, repo_root=repo_root, settings=settings,
                )
        except Exception as exc:  # noqa: BLE001 - one obligation's failure must never
            # abort mutation checking (or the whole analysis run) for the
            # rest; the file itself is still guaranteed reverted by
            # `_mutated_file`'s `finally` regardless of what raised here.
            obligation.mutation = MutationResult(
                file=file, line=line_no, original_operator=op, mutated_operator=new_op,
                status="execution_blocked", detail=f"Could not apply or run mutation: {exc}",
                mapped_test_id=test.id, location_basis=location_basis,
            )
            budget -= 1
            continue

        if execution.blocker is not None:
            status, detail = "execution_blocked", (execution.reason or execution.blocker)
        elif execution.status == VERIFICATION_FAILED:
            status = "mutation_killed"
            detail = "Mapped test failed under the mutated boundary -- it protects this behavior."
        elif execution.status == VERIFICATION_PASSED:
            status = "mutation_survived"
            detail = (
                f"Mapped test still passed with the boundary flipped (`{op}` -> `{new_op}`) -- "
                "a possible coverage gap at this exact boundary, not proof it is untested."
            )
        else:
            status, detail = "execution_blocked", "Mutated execution did not produce a clear pass/fail."

        obligation.mutation = MutationResult(
            file=file, line=line_no, original_operator=op, mutated_operator=new_op,
            status=status, detail=detail, mapped_test_id=test.id,
            location_basis=location_basis,
        )
        budget -= 1
