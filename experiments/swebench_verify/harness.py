"""Run Sydes over SWE-bench Verified tasks, twice per task.

The sequence per task is fixed:

    base_commit + production patch        -> sydes verify-change   (Run A)
    ... + official test_patch             -> sydes verify-change   (Run B)

Run A answers the question this experiment exists for: *before* seeing the
official tests, does Sydes name the obligation those tests were written to
verify? Run B asks whether it then recognises those tests as evidence.

Deliberately not a benchmark platform. No containers, no parallelism, no
scoring. Sydes is run exactly as a user would run it, and whatever it says is
recorded verbatim — including crashes and execution blockers, which are results
rather than problems to fix mid-run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time

CLONE_TIMEOUT = 900
SYDES_TIMEOUT = 900


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
    )


def ensure_clone(repo: str, cache: Path) -> Path:
    """Partial clone once per repository, reused across tasks.

    `--filter=blob:none` keeps the checkout fast while still allowing any commit
    to be materialised, which matters because tasks pin exact base commits.
    """
    target = cache / repo.replace("/", "__")
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["git", "clone", "--filter=blob:none", f"https://github.com/{repo}.git", str(target)],
        timeout=CLONE_TIMEOUT,
    )
    return target


def prepare_worktree(clone: Path, base_commit: str, workdir: Path) -> tuple[bool, str]:
    """Materialise the task's base commit in an isolated directory."""
    if workdir.exists():
        run(["git", "worktree", "remove", "--force", str(workdir)], cwd=clone, timeout=120)
    # A worktree directory removed outside git stays registered and blocks re-add.
    run(["git", "worktree", "prune"], cwd=clone, timeout=120)
    result = run(
        ["git", "worktree", "add", "--detach", str(workdir), base_commit], cwd=clone, timeout=300
    )
    if result.returncode != 0:
        fetched = run(["git", "fetch", "--all", "--tags"], cwd=clone, timeout=CLONE_TIMEOUT)
        result = run(
            ["git", "worktree", "add", "--detach", str(workdir), base_commit],
            cwd=clone,
            timeout=300,
        )
        if result.returncode != 0:
            return False, f"worktree_failed: {result.stderr.strip()[:300]} {fetched.stderr[:100]}"
    return True, ""


def apply_patch(workdir: Path, patch_text: str, label: str) -> tuple[bool, str]:
    """Apply a unified diff, committing it so verify-change sees a real diff."""
    patch_file = workdir / f".sydes_{label}.patch"
    patch_file.write_text(patch_text, encoding="utf-8")
    applied = run(["git", "apply", "-v", str(patch_file)], cwd=workdir, timeout=120)
    if applied.returncode != 0:
        retried = run(
            ["git", "apply", "--3way", str(patch_file)], cwd=workdir, timeout=120
        )
        if retried.returncode != 0:
            patch_file.unlink(missing_ok=True)
            return False, f"patch_failed({label}): {applied.stderr.strip()[:300]}"
    patch_file.unlink(missing_ok=True)
    return True, ""


def sydes_run(workdir: Path, sydes_repo: Path, out_json: Path, base: str) -> dict:
    """Invoke verify-change against the prepared tree and capture everything."""
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [
                "uv", "run", "sydes", "verify-change",
                "--base", base,
                "--llm-policy", "never",
                "--repo", f"task={workdir}",
                "--json", str(out_json),
            ],
            cwd=str(sydes_repo),
            capture_output=True,
            text=True,
            timeout=SYDES_TIMEOUT,
        )
        elapsed = time.perf_counter() - started
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr[-4000:],
            "seconds": round(elapsed, 2),
            "crashed": proc.returncode not in (0, 1),
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "sydes timed out",
            "seconds": SYDES_TIMEOUT,
            "crashed": True,
        }


def summarise(payload: dict | None) -> dict:
    """Pull the recorded fields out of a verification artifact."""
    if not payload:
        return {}
    summary = payload.get("summary", {})
    counts = summary.get("counts", {})
    flows = payload.get("affected_flows", []) or []
    obligations = [o for f in flows for o in (f.get("obligations") or [])]
    required = [o for o in obligations if o.get("required")]
    index_lines = [n for n in payload.get("diagnostics", []) if n.startswith("index_")]
    return {
        "verdict": summary.get("verdict"),
        "risk": summary.get("risk"),
        "analysis_status": payload.get("analysis_status"),
        "changed_files": [f.get("path") for f in payload.get("change", {}).get("files", [])],
        "changed_symbols": [
            f"{s.get('file')}:{s.get('qualified_name') or s.get('name')}"
            for s in payload.get("change", {}).get("symbols", [])
        ],
        "affected_flows": [
            {"entry": f.get("entry_label"), "status": f.get("status"),
             "handler": f.get("handler"), "analysis": f.get("analysis_status")}
            for f in flows
        ],
        "required_obligations": [
            {"statement": o.get("statement"), "status": o.get("status"),
             "kind": o.get("kind"), "origin": o.get("origin"),
             "introduced_by_change": o.get("introduced_by_change"),
             "mapped_tests": [t.get("name") for t in (o.get("mapped_tests") or [])],
             "supporting_tests": len(o.get("supporting_tests") or [])}
            for o in required
        ],
        "advisory_obligations": [
            o.get("statement") for o in obligations if not o.get("required")
        ],
        "counts": counts,
        "index": index_lines,
        "ci_suite": {
            "status": (payload.get("ci_suite") or {}).get("status"),
            "blocker": (payload.get("ci_suite") or {}).get("blocker"),
            "tests_passed": (payload.get("ci_suite") or {}).get("tests_passed"),
            "tests_failed": (payload.get("ci_suite") or {}).get("tests_failed"),
        } if payload.get("ci_suite") else None,
        "llm": {
            "calls": len(payload.get("llm_calls", []) or []),
            "model": payload.get("model_spec"),
        },
    }


def process(task: dict, sydes_repo: Path, cache: Path, workroot: Path, outdir: Path) -> dict:
    """Run both passes for one task and write its record."""
    iid = task["instance_id"]
    record: dict = {
        "instance_id": iid,
        "repo": task["repo"],
        "base_commit": task["base_commit"],
        "difficulty": task.get("difficulty"),
        "status": "ok",
        "failure_class": None,
        "failure_detail": None,
    }
    workdir = workroot / iid

    clone = ensure_clone(task["repo"], cache)
    ok, detail = prepare_worktree(clone, task["base_commit"], workdir)
    if not ok:
        record.update(status="failed", failure_class="environment", failure_detail=detail)
        return record

    # No branches: they live in the shared clone, so a name created for one task
    # collides with the next task's worktree and silently leaves the patch
    # commits sitting on the very ref used as the diff base. The base commit SHA
    # is unambiguous and needs no refs at all.
    run(["git", "config", "user.email", "harness@example.com"], cwd=workdir)
    run(["git", "config", "user.name", "harness"], cwd=workdir)

    ok, detail = apply_patch(workdir, task["patch"], "production")
    if not ok:
        record.update(status="failed", failure_class="patch_application", failure_detail=detail)
        return record
    run(["git", "add", "-A"], cwd=workdir, timeout=120)
    run(["git", "commit", "-qm", "production patch"], cwd=workdir, timeout=120)

    out_a = outdir / f"{iid}.runA.json"
    exec_a = sydes_run(workdir, sydes_repo, out_a, task["base_commit"])
    payload_a = json.loads(out_a.read_text()) if out_a.exists() else None
    record["run_a"] = {**summarise(payload_a), "exec": {k: v for k, v in exec_a.items() if k != "stdout"}}
    # The production patch is non-empty by construction, so a run that reports no
    # changed files means the tree was staged wrong. Fail loudly rather than
    # record a fictitious "no affected flow".
    if payload_a is not None and not record["run_a"].get("changed_files"):
        record.update(status="failed", failure_class="harness",
                      failure_detail="verify-change saw an empty diff; tree staging is wrong")
        return record
    (outdir / f"{iid}.runA.txt").write_text(exec_a["stdout"], encoding="utf-8")
    if exec_a["crashed"] or payload_a is None:
        record.update(status="failed", failure_class="sydes_crash",
                      failure_detail=exec_a["stderr"][-500:])
        return record

    ok, detail = apply_patch(workdir, task["test_patch"], "test")
    if not ok:
        record.update(status="partial", failure_class="patch_application", failure_detail=detail)
        return record
    run(["git", "add", "-A"], cwd=workdir, timeout=120)
    run(["git", "commit", "-qm", "official test patch"], cwd=workdir, timeout=120)

    out_b = outdir / f"{iid}.runB.json"
    exec_b = sydes_run(workdir, sydes_repo, out_b, task["base_commit"])
    payload_b = json.loads(out_b.read_text()) if out_b.exists() else None
    record["run_b"] = {**summarise(payload_b), "exec": {k: v for k, v in exec_b.items() if k != "stdout"}}
    (outdir / f"{iid}.runB.txt").write_text(exec_b["stdout"], encoding="utf-8")
    record["test_patch_files"] = [
        line.split(" b/")[-1]
        for line in task["test_patch"].splitlines()
        if line.startswith("diff --git")
    ]
    if exec_b["crashed"] or payload_b is None:
        record.update(status="partial", failure_class="sydes_crash",
                      failure_detail=exec_b["stderr"][-500:])
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--selected", required=True, type=Path)
    parser.add_argument("--sydes-repo", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--workroot", required=True, type=Path)
    parser.add_argument("--outdir", required=True, type=Path)
    options = parser.parse_args()

    rows = {r["instance_id"]: r for r in json.loads(options.dataset.read_text())}
    wanted = json.loads(options.selected.read_text())
    options.outdir.mkdir(parents=True, exist_ok=True)
    options.workroot.mkdir(parents=True, exist_ok=True)

    records = []
    for iid in wanted:
        print(f"=== {iid}", flush=True)
        started = time.perf_counter()
        try:
            record = process(rows[iid], options.sydes_repo, options.cache,
                             options.workroot, options.outdir)
        except Exception as exc:  # noqa: BLE001 - a harness bug is a recorded outcome
            record = {"instance_id": iid, "repo": rows[iid]["repo"], "status": "failed",
                      "failure_class": "harness", "failure_detail": f"{type(exc).__name__}: {exc}"}
        record["wall_seconds"] = round(time.perf_counter() - started, 1)
        records.append(record)
        (options.outdir / "records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"    {record['status']} ({record['wall_seconds']}s)", flush=True)

    print(f"\nwrote {len(records)} records -> {options.outdir / 'records.json'}")


if __name__ == "__main__":
    main()
