"""AACR-Bench harness for Sydes Code Review (bug-finding quality only).

    AACR case -> checkout at head -> sydes verify-change (released package)
      -> Code Review findings only -> AACR result file -> AACR's own evaluator

Stages (run from the Sydes repository root):

    uv run --no-project --python 3.12 python benchmarks/aacr/adapter/aacr.py prepare
    ... aacr.py review    --cases benchmarks/aacr/cases/dev-smoke-supported.json --run <run-id>
    ... aacr.py eval      --cases <same file> --run <run-id>
    ... aacr.py summarize --run <run-id>
    ... aacr.py replay    --cases <file> --run <id> --source-run <snapshot run> --allocation two_round --sydes-ref <ref>
    ... aacr.py variance  --runs <id-1> <id-2> <id-3>

Leakage rule: `review` reads only repository, PR number and commits from the
case file (which holds no reference comments), and Sydes sees only the
checkout. AACR's references enter only in `eval`, after every review exists.

Environment: OPENAI_API_KEY (Sydes and the judge relay); SYDES_AACR_CACHE
(clone cache, default ~/.cache/sydes-aacr); SYDES_AACR_PRICE_IN /
SYDES_AACR_PRICE_OUT (USD per million tokens, optional, for a cost estimate).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge_relay import from_env as start_relay  # noqa: E402
from to_aacr import to_aacr_result  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "aacr.json").read_text())
CACHE = Path(os.environ.get("SYDES_AACR_CACHE", Path.home() / ".cache" / "sydes-aacr"))
RESULTS = ROOT / "results"


def sh(*args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None,
       timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, check=check, text=True, capture_output=True, env=env, timeout=timeout)


def safe_id(instance_id: str) -> str:
    """AACR's own result-file name rule (evaluation/config.py)."""
    return instance_id.replace("/", "__")


# ----------------------------------------------------------------------------- AACR checkout


def aacr_checkout(commit: str | None = None) -> Path:
    commit = commit or CONFIG["aacr"]["commit"]
    path = CACHE / f"aacr-bench-{commit[:12]}"
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", CONFIG["aacr"]["clone_url"], str(path))
    sh("git", "-c", "advice.detachedHead=false", "checkout", "-q", "--force", commit, cwd=path)
    return path


def prepare(commit: str | None = None) -> Path:
    """AACR at the pinned commit, its own venv and locked requirements, and its own
    converter's standard dataset (from the dataset file committed at that commit)."""
    checkout = aacr_checkout(commit)
    evaluation = checkout / "evaluation"
    venv = evaluation / ".venv"
    if not (venv / "bin" / "python").exists():
        sh("uv", "venv", "-q", str(venv), "--python", CONFIG["aacr"]["python"], cwd=evaluation)
        sh("uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), "-r", "requirements.txt", cwd=evaluation)
    dataset = evaluation / "data" / "aacr_bench.jsonl"
    sh(str(venv / "bin" / "python"), "-B", "-m", "converters.aacr_bench", "--input",
       str(checkout / CONFIG["aacr"]["dataset"]), "--output", str(dataset), "--validate", cwd=evaluation)
    print(f"AACR {commit or CONFIG['aacr']['commit']} ready: {dataset}")
    return evaluation


# ----------------------------------------------------------------------------- case checkout


def repo_checkout(case: dict) -> Path:
    path = CACHE / "repos" / case["repo"].replace("/", "__")
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"https://github.com/{case['repo']}.git", str(path))
    for sha in (case["base_commit"], case["head_commit"]):
        if sh("git", "cat-file", "-e", f"{sha}^{{commit}}", cwd=path, check=False).returncode:
            if sh("git", "fetch", "-q", "--filter=blob:none", "origin", sha, cwd=path, check=False).returncode:
                sh("git", "fetch", "-q", "--filter=blob:none", "origin", f"pull/{case['pr']}/head", cwd=path)
    return path


def review_base(path: Path, base: str, head: str) -> tuple[str, dict]:
    """What the PR diff is taken against: the base itself when it is an ancestor of
    the head, else their merge base -- the three-dot diff AACR's own reviewers use
    (`/code-review base...head`). Several merge bases: newest committer date first,
    then the smallest commit id (OpenQodex's frozen rule)."""
    bases = sh("git", "merge-base", "--all", base, head, cwd=path, check=False).stdout.split()
    if sh("git", "merge-base", "--is-ancestor", base, head, cwd=path, check=False).returncode == 0:
        return base, {"rule": "base is an ancestor of head", "merge_bases": bases}
    if not bases:
        raise RuntimeError("base and head share no history")
    dated = sorted(bases, key=lambda b: (-int(sh("git", "show", "-s", "--format=%ct", b, cwd=path).stdout.strip()), b))
    return dated[0], {"rule": "merge base (newest committer date, then smallest id)", "merge_bases": bases}


_PROVIDER_FAILURE = re.compile(r"insufficient_quota|credit_balance_exhausted|Error code: 429|rate.?limit", re.IGNORECASE)


def provider_errors(sydes: dict | None, investigation: dict | None) -> list[str]:
    """Why a case is not benchmark data: any LLM/provider failure in Sydes' own stages or
    the Investigator. A degraded run (Sydes carries on without a stage) never counts."""
    reasons = []
    totals = (((sydes or {}).get("run_metrics") or {}).get("llm_totals")) or {}
    if int(totals.get("errors") or 0) > 0:
        reasons.append(f"{totals['errors']} LLM call error(s) in run_metrics")
    if investigation and investigation.get("errors"):
        reasons.append(f"{len(investigation['errors'])} investigator error(s)")
    hits = [d for d in (sydes or {}).get("diagnostics") or [] if isinstance(d, str) and _PROVIDER_FAILURE.search(d)]
    if hits:
        reasons.append(f"provider failure in diagnostics: {hits[0][:160]}")
    return reasons


def clean(path: Path) -> None:
    sh("git", "reset", "-q", "--hard", cwd=path, check=False)
    sh("git", "clean", "-q", "-fdx", cwd=path, check=False)


# ----------------------------------------------------------------------------- review


def sydes_build(ref: str) -> tuple[str, str]:
    """A git ref of this Sydes repository, exported with `git archive` (committed files
    only, never the working tree), as a `uv tool run --from` source and its commit."""
    repo = ROOT.parent.parent
    commit = sh("git", "rev-parse", f"{ref}^{{commit}}", cwd=repo).stdout.strip()
    target = CACHE / "sydes-builds" / commit
    if not (target / "pyproject.toml").exists():
        target.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=repo, check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    return str(target), commit


def review(cases_file: Path, run: str, only: list[str] | None = None, sydes_ref: str | None = None,
           extra_args: list[str] | None = None) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    cases = json.loads(cases_file.read_text())["cases"]
    out = RESULTS / run
    for sub in ("sydes", "aacr", "telemetry", "logs"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    s = dict(CONFIG["sydes"])
    if sydes_ref:
        s["package"], s["git_commit"] = sydes_build(sydes_ref)
        s["git_ref"] = sydes_ref
    if extra_args:
        s["args"] = [*s["args"], *extra_args]
    (out / "run.json").write_text(json.dumps({
        "run": run, "cases_file": str(cases_file.resolve().relative_to(ROOT.parent.parent)), "sydes": s,
        "aacr_commit": CONFIG["aacr"]["commit"], "boundary": "code_findings", "started_at": time.strftime("%FT%TZ", time.gmtime()),
    }, indent=2) + "\n")
    # Sydes must not mistake this harness's environment for the target repository's.
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT")}
    for case in cases:
        if only and case["case_id"] not in only:
            continue
        sid = safe_id(case["instance_id"])
        if (out / "aacr" / f"{sid}.json").exists():
            print(f"skip {case['case_id']} (done)")
            continue
        (out / "invalid" / f"{sid}.json").unlink(missing_ok=True)  # an invalid case is reviewed again
        path = repo_checkout(case)
        base, base_info = review_base(path, case["base_commit"], case["head_commit"])
        clean(path)
        sh("git", "-c", "advice.detachedHead=false", "checkout", "-q", "--force", "--detach", case["head_commit"], cwd=path)
        result_json = out / "sydes" / f"{sid}.json"
        command = ["uv", "tool", "run", "--quiet", "--from", s["package"], "sydes", "verify-change",
                   "--base", base, "--repo", "app=.", *s["args"], "--model", s["model"], "--json", str(result_json)]
        print(f"review {case['case_id']} ({case['language']}, {case['change_line_count']} lines) ...", flush=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=path, env=env, text=True, capture_output=True, timeout=s["timeout_minutes"] * 60)
            status = "completed" if proc.returncode == 0 and result_json.exists() else f"failed(exit {proc.returncode})"
            log = proc.stdout + "\n--- stderr ---\n" + proc.stderr
        except subprocess.TimeoutExpired as exc:
            status, log = "failed(timeout)", f"{exc.stdout or ''}\n--- stderr ---\n{exc.stderr or ''}"
        elapsed = time.monotonic() - started
        clean(path)
        (out / "logs" / f"{sid}.log").write_text(log if isinstance(log, str) else str(log))
        sydes = json.loads(result_json.read_text()) if result_json.exists() else None
        sidecar = result_json.with_name(result_json.stem + ".investigation.json")
        art = json.loads(sidecar.read_text()) if sidecar.exists() else None
        invalid = provider_errors(sydes, art)
        if invalid:
            # not benchmark data: kept for debugging under invalid/, never where the evaluator reads
            status = "INVALID(provider errors)"
        target = out / ("invalid" if invalid else "aacr")
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{sid}.json").write_text(json.dumps(
            to_aacr_result(case["instance_id"], sydes, duration_seconds=elapsed, status=status), indent=2) + "\n")
        tele = telemetry(case, base, base_info, status, elapsed, sydes)
        tele["invalid_reasons"] = invalid
        if art is not None:  # written by `--investigator`; summarised here, kept in full beside the result
            tele["investigation"] = {"hypotheses": len(art.get("hypotheses") or []), "lookups": len(art.get("lookups") or []),
                                     "confirmed": len(art.get("confirmed") or []), "refuted": len(art.get("refuted") or []),
                                     "unknown": len(art.get("unknown") or []), "usage": art.get("usage")}
        (out / "telemetry" / f"{sid}.json").write_text(json.dumps(tele, indent=2) + "\n")
        print(f"  {status} in {elapsed:.0f}s, {len((sydes or {}).get('code_findings') or [])} finding(s)"
              + (f" -- excluded: {'; '.join(invalid)}" if invalid else ""))


def replay(cases_file: Path, run: str, source_run: str, allocation: str, sydes_ref: str,
           only: list[str] | None = None) -> None:
    """Allocation experiment: the Investigator -> Verifier again, under `allocation`, on the
    packet + hypotheses snapshot a `review --sydes-arg=--investigator-snapshot` run wrote.
    Same checkout, same model, same output layout, so `eval` and `summarize` run unchanged.
    A case invalid in the source run is invalid here too."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    cases = json.loads(cases_file.read_text())["cases"]
    source, out = RESULTS / source_run, RESULTS / run
    for sub in ("sydes", "aacr", "telemetry", "logs"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    s = dict(CONFIG["sydes"])
    s["package"], s["git_commit"] = sydes_build(sydes_ref)
    s["git_ref"] = sydes_ref
    (out / "run.json").write_text(json.dumps({
        "run": run, "cases_file": str(cases_file.resolve().relative_to(ROOT.parent.parent)), "sydes": s,
        "replay": {"source_run": source_run, "allocation": allocation},
        "aacr_commit": CONFIG["aacr"]["commit"], "boundary": "code_findings", "started_at": time.strftime("%FT%TZ", time.gmtime()),
    }, indent=2) + "\n")
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT")}
    for case in cases:
        if only and case["case_id"] not in only:
            continue
        sid = safe_id(case["instance_id"])
        if (out / "aacr" / f"{sid}.json").exists():
            print(f"skip {case['case_id']} (done)")
            continue
        (out / "invalid" / f"{sid}.json").unlink(missing_ok=True)
        snapshot = source / "sydes" / f"{sid}.snapshot.json"
        source_tele = source / "telemetry" / f"{sid}.json"
        source_invalid = (json.loads(source_tele.read_text()).get("invalid_reasons") or []) if source_tele.exists() else ["no source telemetry"]
        if not snapshot.exists():
            source_invalid = [*source_invalid, "no snapshot in the source run"]
        path = repo_checkout(case)
        base, base_info = review_base(path, case["base_commit"], case["head_commit"])
        result_json = out / "sydes" / f"{sid}.json"
        print(f"replay {case['case_id']} ({allocation}) ...", flush=True)
        started, log, sydes, art = time.monotonic(), "", None, None
        if snapshot.exists():
            clean(path)
            sh("git", "-c", "advice.detachedHead=false", "checkout", "-q", "--force", "--detach", case["head_commit"], cwd=path)
            command = ["uv", "run", "--no-project", "--quiet", "--with", s["package"], "python", "-m",
                       "sydes.verify.investigation_replay", "--snapshot", str(snapshot.resolve()), "--allocation", allocation,
                       "--model", s["model"], "--json", str(result_json.resolve())]
            try:
                proc = subprocess.run(command, cwd=path, env=env, text=True, capture_output=True, check=False, timeout=s["timeout_minutes"] * 60)
                status = "completed" if proc.returncode == 0 and result_json.exists() else f"failed(exit {proc.returncode})"
                log = proc.stdout + "\n--- stderr ---\n" + proc.stderr
            except subprocess.TimeoutExpired as exc:
                status, log = "failed(timeout)", f"{exc.stdout or ''}\n--- stderr ---\n{exc.stderr or ''}"
            clean(path)
            sydes = json.loads(result_json.read_text()) if result_json.exists() else None
            sidecar = result_json.with_name(result_json.stem + ".investigation.json")
            art = json.loads(sidecar.read_text()) if sidecar.exists() else None
        else:
            status = "failed(no snapshot)"
        elapsed = time.monotonic() - started
        (out / "logs" / f"{sid}.log").write_text(log if isinstance(log, str) else str(log))
        invalid = [*(f"source run: {r}" for r in source_invalid), *provider_errors(sydes, art)]
        if invalid:
            status = "INVALID(provider errors)"
        target = out / ("invalid" if invalid else "aacr")
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{sid}.json").write_text(json.dumps(
            to_aacr_result(case["instance_id"], sydes, duration_seconds=elapsed, status=status), indent=2) + "\n")
        tele = telemetry(case, base, base_info, status, elapsed, sydes)
        tele["invalid_reasons"] = invalid
        tele["replay"] = {"source_run": source_run, "allocation": allocation}
        if art is not None:
            tele["investigation"] = {"hypotheses": len(art.get("hypotheses") or []), "lookups": len(art.get("lookups") or []),
                                     "confirmed": len(art.get("confirmed") or []), "refuted": len(art.get("refuted") or []),
                                     "unknown": len(art.get("unknown") or []), "usage": art.get("usage")}
        (out / "telemetry" / f"{sid}.json").write_text(json.dumps(tele, indent=2) + "\n")
        print(f"  {status} in {elapsed:.0f}s, {len((sydes or {}).get('code_findings') or [])} finding(s)"
              + (f" -- excluded: {'; '.join(invalid)}" if invalid else ""))


def telemetry(case: dict, base: str, base_info: dict, status: str, elapsed: float, sydes: dict | None) -> dict:
    """Everything about the run that AACR does not score, kept apart from the submission."""
    sydes = sydes or {}
    metrics = sydes.get("run_metrics") or {}
    by_purpose = {k: v.get("calls", 0) for k, v in (metrics.get("llm_by_purpose") or {}).items()}
    semantic = sydes.get("pr_semantic_analysis") or {}
    return {
        "case_id": case["case_id"], "instance_id": case["instance_id"], "language": case["language"],
        "status": status, "wall_seconds": round(elapsed, 1), "review_base": base, "review_base_info": base_info,
        "code_review_status": sydes.get("code_review_status"),
        "code_findings": len(sydes.get("code_findings") or []),
        "not_submitted": {"local_risks": len(semantic.get("local_risks") or []),
                          "affected_flows": len(sydes.get("affected_flows") or [])},
        "llm_totals": metrics.get("llm_totals"), "llm_calls_by_purpose": by_purpose,
        "cbm": metrics.get("cbm"), "sydes_wall_seconds": metrics.get("wall_seconds"),
        "verdict": (sydes.get("summary") or {}).get("verdict"),
    }


# ----------------------------------------------------------------------------- eval


def evaluate(cases_file: Path, run: str, rounds: int) -> None:
    """AACR's own evaluator, unchanged, on this run's result files: once with its
    default (file-level) scoring and once with its legacy per-comment scoring."""
    evaluation = prepare()
    out = RESULTS / run
    wanted = {c["instance_id"] for c in json.loads(cases_file.read_text())["cases"]}
    rows = [json.loads(line) for line in (evaluation / "data" / "aacr_bench.jsonl").read_text().splitlines() if line.strip()]
    dataset = out / "dataset.jsonl"  # AACR's converter output, filtered to this run's cases
    dataset.write_text("".join(json.dumps(r) + "\n" for r in rows if r["instance_id"] in wanted))
    missing = wanted - {r["instance_id"] for r in rows}
    if missing:
        raise SystemExit(f"cases not in AACR's converted dataset: {sorted(missing)}")
    relay = start_relay()
    env = {**os.environ, "JUDGE_BASE_URL": relay.base_url, "JUDGE_API_KEY": "relay-placeholder",
           "JUDGE_MODEL": CONFIG["judge"]["model"], "JUDGE_USE_MOCK": "false", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("VIRTUAL_ENV", None)
    try:
        for mode, flags in (("file_level", []), ("legacy", ["--use-deprecated-score"])):
            target = out / f"eval-{mode}"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(out / "aacr", target)
            proc = subprocess.run(
                [str(evaluation / ".venv" / "bin" / "python"), "-B", "-m", "pipeline", "run", "--stage", "eval",
                 "--reviewer", CONFIG["aacr"]["reader"], "--dataset", str(dataset), "--results-dir", str(target),
                 "--run-id", run, "--line-k", str(CONFIG["aacr"]["line_k"]), "--eval-rounds", str(rounds), *flags],
                cwd=evaluation, env=env, text=True, capture_output=True)
            (target / "evaluator.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
            print(f"eval {mode}: exit {proc.returncode}")
            if proc.returncode:
                print(proc.stderr[-2000:])
    finally:
        relay.stop()
    (out / "judge.json").write_text(json.dumps({"model": CONFIG["judge"]["model"], **relay.counts}, indent=2) + "\n")


# ----------------------------------------------------------------------------- validate


REVIEW_CASE_FIELDS = {"case_id", "instance_id", "repo", "pr", "pr_url", "language", "base_commit", "head_commit",
                      "change_line_count"}


def validate(cases_file: Path, run: str | None = None) -> None:
    """The case file holds only what a reviewer may see and matches AACR's own
    conversion exactly; with --run, AACR's evaluator read every result file."""
    data = json.loads(cases_file.read_text())
    problems = []
    for case in data["cases"]:
        extra = set(case) - REVIEW_CASE_FIELDS
        if extra:
            problems.append(f"{case['case_id']}: fields a reviewer must not see: {sorted(extra)}")
        if case["language"] not in data["source"]["supported_languages"]:
            problems.append(f"{case['case_id']}: language {case['language']} is outside the declared rule")
    converted = prepare() / "data" / "aacr_bench.jsonl"
    rows = {r["instance_id"]: r for r in map(json.loads, filter(str.strip, converted.read_text().splitlines()))}
    for case in data["cases"]:
        row = rows.get(case["instance_id"])
        if row is None:
            problems.append(f"{case['case_id']}: not in AACR's converted dataset")
        elif (row["repo"], row["base_commit"], row["head_commit"]) != (case["repo"], case["base_commit"], case["head_commit"]):
            problems.append(f"{case['case_id']}: repo/base/head differ from AACR's")
    if run:
        for mode in ("file_level", "legacy"):
            summary = _metrics(RESULTS / run / f"eval-{mode}")
            if summary.get("evaluated_instances") != len(data["cases"]) or summary.get("missing_instances"):
                problems.append(f"{run} {mode}: evaluator read {summary.get('evaluated_instances')} of "
                                f"{len(data['cases'])} result files")
    print("\n".join(problems) or f"{cases_file.name}: {len(data['cases'])} cases valid" + (f"; {run} accepted by evaluator" if run else ""))
    if problems:
        raise SystemExit(1)


# ----------------------------------------------------------------------------- summary


def _metrics(folder: Path) -> dict:
    files = sorted(folder.glob("metrics_*.json"))
    average = [f for f in files if f.name.endswith("_average.json")]
    chosen = (average or files)[-1:] if files else []
    if not chosen:
        return {}
    data = json.loads(chosen[0].read_text())
    summary = dict(data.get("summary") or data)
    generated = summary.get("generated_notes") or 0
    matched = summary.get("matched_generated_notes", summary.get("matched_semantic_notes"))
    if generated and matched is not None:
        # Derived, not an AACR field: share of submitted comments that matched no reference.
        summary["derived_noise_rate"] = round((generated - matched) / generated, 3)
    return summary


def summarize(run: str) -> dict:
    out = RESULTS / run
    every = [json.loads(p.read_text()) for p in sorted((out / "telemetry").glob("*.json"))]
    tele = [t for t in every if not t.get("invalid_reasons")]
    n = len(tele) or 1
    tokens_in = sum((t.get("llm_totals") or {}).get("input_tokens", 0) for t in tele)
    tokens_out = sum((t.get("llm_totals") or {}).get("output_tokens", 0) for t in tele)
    calls = sum((t.get("llm_totals") or {}).get("calls", 0) for t in tele)
    price_in, price_out = os.environ.get("SYDES_AACR_PRICE_IN"), os.environ.get("SYDES_AACR_PRICE_OUT")
    cost = (tokens_in * float(price_in) + tokens_out * float(price_out)) / 1e6 / n if price_in and price_out else None
    summary = {
        "run": run,
        "metrics_file_level": _metrics(out / "eval-file_level"),
        "metrics_legacy": _metrics(out / "eval-legacy"),
        "cases": [{k: t[k] for k in ("case_id", "language", "status", "wall_seconds", "code_findings", "code_review_status")}
                  for t in tele],
        "per_pr": {
            "wall_seconds": round(sum(t["wall_seconds"] for t in tele) / n, 1),
            "llm_calls": round(calls / n, 1),
            "input_tokens": round(tokens_in / n), "output_tokens": round(tokens_out / n),
            "estimated_cost_usd": round(cost, 4) if cost is not None else "unavailable (set SYDES_AACR_PRICE_IN/OUT)",
            "llm_calls_by_purpose": dict(sum((Counter(t.get("llm_calls_by_purpose") or {}) for t in tele), Counter())),
        },
        "findings_submitted": sum(t["code_findings"] for t in tele),
        "invalid_cases": [{"case_id": t["case_id"], "reasons": t["invalid_reasons"]} for t in every if t.get("invalid_reasons")],
        "judge": json.loads((out / "judge.json").read_text()) if (out / "judge.json").exists() else None,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary


# ----------------------------------------------------------------------------- variance


_WORD = re.compile(r"[a-z0-9_]+")


def _same(a: dict, b: dict) -> bool:
    """Two findings are the same when they name the same file and lie within 3
    lines of each other. Without lines on both, their titles must share at least
    30% of their words. Wording alone never splits two findings at one place
    (runs phrase the same issue differently), and ground truth is never used."""
    if (a.get("path") or "") != (b.get("path") or ""):
        return False
    la, lb = a.get("start_line"), b.get("start_line")
    if la is not None and lb is not None:
        return abs(la - lb) <= 3
    wa = set(_WORD.findall(a["content"].splitlines()[0].lower()))
    wb = set(_WORD.findall(b["content"].splitlines()[0].lower()))
    return la is None and lb is None and bool(wa | wb) and len(wa & wb) / len(wa | wb) >= 0.3


def variance(runs: list[str]) -> dict:
    report: dict = {"runs": runs, "cases": {}, "metrics": {}}
    ids = sorted({p.name for r in runs for p in (RESULTS / r / "aacr").glob("*.json")})
    for sid in ids:
        per_run = []
        for r in runs:
            p = RESULTS / r / "aacr" / sid
            per_run.append(json.loads(p.read_text())["review"]["comments"] if p.exists() else None)
        clusters: list[dict] = []
        for index, comments in enumerate(per_run):
            for c in comments or []:
                home = next((k for k in clusters if index not in k["runs"] and _same(k["finding"], c)), None)
                if home is None:
                    clusters.append({"finding": c, "runs": {index}})
                else:
                    home["runs"].add(index)
        counts = Counter(len(k["runs"]) for k in clusters)
        report["cases"][sid] = {
            "findings_per_run": [None if c is None else len(c) for c in per_run],
            "in_all": counts.get(len(runs), 0), "in_two": counts.get(2, 0) if len(runs) > 2 else None,
            "one_off": counts.get(1, 0),
            "titles": [{"title": k["finding"]["content"].splitlines()[0][:90], "runs": sorted(k["runs"])} for k in clusters],
        }
    for r in runs:
        tele = [t for t in (json.loads(p.read_text()) for p in (RESULTS / r / "telemetry").glob("*.json"))
                if not t.get("invalid_reasons")]
        report["metrics"][r] = {"file_level": _metrics(RESULTS / r / "eval-file_level"),
                                "legacy": _metrics(RESULTS / r / "eval-legacy"),
                                "input_tokens": sum((t.get("llm_totals") or {}).get("input_tokens", 0) for t in tele),
                                "output_tokens": sum((t.get("llm_totals") or {}).get("output_tokens", 0) for t in tele),
                                "wall_seconds": round(sum(t["wall_seconds"] for t in tele), 1)}
    out = RESULTS / f"variance-{'-'.join(runs)}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="AACR-Bench harness for Sydes Code Review")
    sub = parser.add_subparsers(dest="stage", required=True)
    sub.add_parser("prepare")
    for name in ("review", "eval"):
        p = sub.add_parser(name)
        p.add_argument("--cases", type=Path, required=True)
        p.add_argument("--run", required=True)
        if name == "review":
            p.add_argument("--only", nargs="*", help="case ids to review (default: all in the file)")
            p.add_argument("--sydes-ref", help="run this git ref of the Sydes repository instead of the released package")
            p.add_argument("--sydes-arg", action="append", default=[], help="extra verify-change argument (repeatable)")
        else:
            p.add_argument("--rounds", type=int, default=CONFIG["aacr"]["eval_rounds"])
    r = sub.add_parser("replay", help="allocation experiment: re-investigate a snapshot run's hypotheses")
    r.add_argument("--cases", type=Path, required=True)
    r.add_argument("--run", required=True)
    r.add_argument("--source-run", required=True, help="a review run made with --sydes-arg=--investigator-snapshot")
    r.add_argument("--allocation", choices=["sequential", "two_round"], required=True)
    r.add_argument("--sydes-ref", required=True)
    r.add_argument("--only", nargs="*")
    v = sub.add_parser("validate")
    v.add_argument("--cases", type=Path, required=True)
    v.add_argument("--run")
    sub.add_parser("summarize").add_argument("--run", required=True)
    sub.add_parser("variance").add_argument("--runs", nargs="+", required=True)
    args = parser.parse_args()
    if args.stage == "prepare":
        prepare()
    elif args.stage == "review":
        review(args.cases, args.run, args.only, args.sydes_ref, args.sydes_arg)
    elif args.stage == "replay":
        replay(args.cases, args.run, args.source_run, args.allocation, args.sydes_ref, args.only)
    elif args.stage == "eval":
        evaluate(args.cases, args.run, args.rounds)
    elif args.stage == "validate":
        validate(args.cases, args.run)
    elif args.stage == "summarize":
        summarize(args.run)
    else:
        variance(args.runs)


if __name__ == "__main__":
    main()
