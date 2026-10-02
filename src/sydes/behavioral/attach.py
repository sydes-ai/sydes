"""The one integration point: attach behavioral evidence to a verification result.

Called by `sydes verify-change` after the structural pipeline, before AI
recovery. It never raises past itself: any failure to obtain or merge the
evidence is recorded as `BehavioralEvidence(status="unavailable", reason=...)`
and one note, and the structural result is left exactly as it was. Missing
behavioral evidence is never "no impact".
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import subprocess

from sydes.behavioral.diffgenome_adapter import (
    BehavioralUnavailable,
    DiffGenomeRequest,
    load_artifact,
    run_diffgenome,
)
from sydes.behavioral.answers import ancestors_of_changed, symbols_without_entry
from sydes.behavioral.merge import match_node, merge
from sydes.behavioral.models import STATUS_UNAVAILABLE, BehavioralEvidence
from sydes.behavioral.runtime import RuntimeEvidence
from sydes.verify.models import TIER_DECLARED, MappedTest

MATCH_RULE = "diffgenome:observed-execution"


def attach_behavioral_evidence(
    result,
    *,
    repo_root: Path,
    provider: str,
    artifact_path: Path | None = None,
    runtime_args: str = "",
    probe_budget: int = 0,
    out_dir: Path | None = None,
    timeout_seconds: float = 900.0,
    writer: str = "openai",
    use_runtime: bool = True,
    test_selection: dict | None = None,
    unavailable_reason: str | None = None,
    runtime_environment: dict | None = None,
) -> BehavioralEvidence:
    if provider != "diffgenome":
        ev = BehavioralEvidence(status=STATUS_UNAVAILABLE, reason=f"unknown behavioral provider {provider!r}")
        result.behavioral = ev
        return ev
    if artifact_path is None and unavailable_reason:
        # the live run already happened (before analysis) and produced nothing: never run
        # DiffGenome a second time here, with different arguments
        ev = BehavioralEvidence(status=STATUS_UNAVAILABLE, reason=unavailable_reason)
        result.behavioral = ev
        result.notes.append(f"Behavioral execution evidence unavailable: {ev.reason}")
        return ev
    try:
        if artifact_path is not None:
            artifact = load_artifact(artifact_path)
            log: list[str] = []
            source_path = str(artifact_path)
        else:
            diff = _diff_spec(result)
            request = DiffGenomeRequest(
                repo_root=repo_root, diff=diff,
                out_dir=out_dir or (Path.cwd() / ".sydes-behavioral"),
                runtime_args=shlex.split(runtime_args), probe_budget=probe_budget,
                timeout_seconds=timeout_seconds, writer=writer,
            )
            artifact, log = run_diffgenome(request)
            source_path = str(request.out_dir / "diffgenome-change.json")
        ev = merge(result, artifact, artifact_path=source_path)
        ev.changed_symbols_without_entry = symbols_without_entry(ev)
        runtime = RuntimeEvidence.from_artifact(artifact) if use_runtime else None
        if runtime is not None:
            _apply_runtime(result, ev, runtime)
            if test_selection and ev.runtime_evidence is not None:
                ev.runtime_evidence["test_selection"] = test_selection
            if runtime_environment and ev.runtime_evidence is not None:
                ev.runtime_evidence["environment"] = runtime_environment
        ev.notes.extend(log[-3:])
        added = _added_test_lines(result, repo_root)
        linked = link_supporting_tests(result, ev, artifact, added)
        if linked:
            ev.notes.append(
                f"{linked} obligation(s) on flows through the changed code now carry the "
                "executing tests as supporting evidence (not as verification)"
            )
    except BehavioralUnavailable as exc:
        _unlink_supporting_tests(result)
        ev = BehavioralEvidence(status=STATUS_UNAVAILABLE, reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - isolation: a merge bug must not fail the run
        _unlink_supporting_tests(result)
        ev = BehavioralEvidence(status=STATUS_UNAVAILABLE, reason=f"behavioral merge failed: {exc!r}")
    result.behavioral = ev
    if ev.status == STATUS_UNAVAILABLE:
        result.notes.append(f"Behavioral execution evidence unavailable: {ev.reason}")
    else:
        c = ev.counts
        result.notes.append(
            "Behavioral execution evidence (DiffGenome): "
            f"observed {c.get('observed_runtime', 0)}, reconstructed "
            f"{c.get('composed_state', 0) + c.get('composed_value', 0) + c.get('composed_arg_shape', 0) + c.get('composed_symbol', 0)}, "
            f"static-only {c.get('static_only', 0)}, gaps {c.get('gaps', 0)}, "
            f"tests on path {c.get('tests_on_behavioral_path', 0)}"
        )
    return ev


def _apply_runtime(result, ev: BehavioralEvidence, runtime: RuntimeEvidence) -> None:
    """First-class runtime evidence: kept on the result for reporting and the recovery
    decision, and its gaps added to the verification gaps (source "runtime"). Obligations and
    the verdict are untouched: observed execution is supporting evidence, not verification."""
    ev.runtime_evidence = runtime.summary()
    gaps = runtime.verification_gaps()
    result.verification_gaps = [g for g in result.verification_gaps if g.source != "runtime"] + gaps
    if result.summary is not None:
        result.summary.counts.verification_gaps = len(result.verification_gaps)
    executed = runtime.executed()
    ev.notes.append(
        f"runtime evidence: {len(executed)}/{len(runtime.functions)} changed function(s) executed by "
        f"{len(runtime.tests())} existing test(s) ({runtime.test_scope}); {len(gaps)} runtime gap(s)"
    )


def _diff_spec(result) -> str:
    """DiffGenome's revspec for the change: base..head when a head is known, else base."""
    change = result.change
    merge_base = getattr(change, "merge_base", None) or change.base
    head = getattr(change, "head", None)
    if head:
        return f"{merge_base}..{head}"
    return merge_base


def _unlink_supporting_tests(result) -> None:
    """Undo any partial linking so a failed attach leaves the structural result as it was."""
    for flow in getattr(result, "affected_flows", []) or []:
        for obligation in flow.obligations:
            obligation.supporting_tests = [
                t for t in obligation.supporting_tests if t.match_rule != MATCH_RULE
            ]


def _added_test_lines(result, repo_root: Path) -> str:
    """Added lines of the diff's changed test files (empty when git is unavailable).
    Used only to tell which observed tests the change itself introduced."""
    change = result.change
    test_files = [f.path for f in change.files if "test" in str(getattr(f, "role", "") or "")]
    if not test_files:
        return ""
    base = getattr(change, "merge_base", None) or change.base
    head = getattr(change, "head", None)
    spec = [base, head] if head else [base]
    try:
        proc = subprocess.run(
            ["git", "diff", "--unified=0", "--no-color", *spec, "--", *test_files],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(
        line[1:] for line in proc.stdout.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def _leaf(test_name: str) -> str:
    return re.split(r"::|/|\.", test_name)[-1]


def _introduced_by_diff(test_name: str, added: str) -> bool:
    """A test the diff itself introduced or edited: its leaf name (the case after the
    last `::`/`/`) occurs as a whole word in the diff's added test lines."""
    leaf = _leaf(test_name)
    return bool(added) and len(leaf) >= 4 and re.search(rf"\b{re.escape(leaf)}\b", added) is not None


def link_supporting_tests(result, ev: BehavioralEvidence, artifact: dict, added_lines: str = "") -> int:
    """Attach the existing tests DiffGenome observed executing the changed symbols to
    the required obligations of every affected flow that passes through those symbols,
    as `supporting_tests` at tier C ("exercises, no assertion established"). Supporting,
    not mapped: DiffGenome established that the test ran the changed code in isolation
    and passed, not that it asserts the obligation's statement; the verdict machinery
    never counts supporting tests as verification, so `summary.verdict` is unchanged. `match_rule` and `source_refs`
    say exactly where the evidence came from. Returns the number of obligations
    touched."""
    tests = ev.tests_on_behavioral_path
    if not tests:
        return 0
    nodes = list(artifact.get("nodes") or [])
    changed_ids = {n["id"] for n in nodes if n.get("changed")}
    on_path = ancestors_of_changed(ev)
    touched = 0
    for flow in getattr(result, "affected_flows", []) or []:
        # the flow is on the change's executed path when one of its steps is a changed
        # symbol or an executed/reconstructed ancestor of one
        on_flow = False
        for step in flow.steps or []:
            if isinstance(step, dict) and match_node(step, nodes) in on_path:
                on_flow = True
                break
        if not on_flow:
            continue
        for obligation in flow.obligations:
            if not obligation.required:
                continue
            # one key per test case, whichever naming the other source used
            existing = {
                (t.file, t.case_name or t.name): t
                for t in (*obligation.mapped_tests, *obligation.supporting_tests)
            }
            known = set(existing)
            added = False
            for name in tests:
                head, sep, case = name.partition("::")
                file = ev.test_files.get(name) or (head if sep else None)
                case = case if sep else name
                suite = case.split("/")[0].split("::")[0]
                key = (file, case)
                if key in known:
                    # already listed by Sydes itself: keep its entry, only record the fact
                    # that the diff introduced or edited this test
                    if key in existing and _introduced_by_diff(name, added_lines):
                        existing[key].changed_in_diff = True
                    continue
                known.add(key)
                obligation.supporting_tests.append(
                    MappedTest(
                        id=f"diffgenome:{name}", name=name, case_name=case,
                        file=file, suite=suite or None,
                        match_rule=MATCH_RULE, evidence_tier=TIER_DECLARED,
                        changed_in_diff=_introduced_by_diff(name, added_lines),
                        source_refs=["diffgenome:" + s for s in sorted(changed_ids)],
                    )
                )
                added = True
            if added:
                touched += 1
                if obligation.status == "unverified" and obligation.reason is None:
                    obligation.reason = (
                        f"{len(obligation.supporting_tests)} existing test(s) executed the "
                        "changed code in DiffGenome's isolated run; none is mapped as "
                        "verifying this statement"
                    )
    if touched:
        counts = result.summary.counts
        required = [o for f in result.affected_flows for o in f.obligations if o.required]
        counts.supporting_tests = sum(len(o.supporting_tests) for o in required)
        counts.tests_supporting_behavior = len(
            {t.name for o in required for t in o.supporting_tests}
            - {t.name for o in required for t in o.mapped_tests}
        )
    return touched
