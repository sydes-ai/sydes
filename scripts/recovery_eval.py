#!/usr/bin/env python3
"""Evaluate the `sydes.recovery` prototype against real, already-captured
first-pass results and their real repository checkouts.

This is a dev/experiment script, not part of the shipped package: it loads
a `sydes-result.json` produced by a real prior `verify-change` run (not a
fresh one — this experiment is about the recovery *pass*, not about
re-spending provider budget re-running the first pass) and points the
recovery agent at the real local repository checkout that produced it.

Usage:
    uv run python scripts/recovery_eval.py \\
        --result /path/to/sydes-result.json \\
        --repo /path/to/local/repo/checkout \\
        --model openai:gpt-4.1-mini \\
        --out /tmp/recovery-eval-<case>.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sydes.llm.client import LLMClientError, create_default_llm_client
from sydes.recovery.agent import recover
from sydes.recovery.context import build_context
from sydes.recovery.merge import build_recovery_view
from sydes.recovery.schema import RecoveryError
from sydes.recovery.trigger import evaluate_trigger
from sydes.verify.models import ChangeVerificationResult


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path, help="Path to a real sydes-result.json")
    parser.add_argument("--repo", required=True, type=Path, help="Local repository checkout root")
    parser.add_argument("--model", default="openai:gpt-4.1-mini")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--case-name", default=None)
    args = parser.parse_args()

    raw = json.loads(args.result.read_text(encoding="utf-8"))
    result = ChangeVerificationResult.model_validate(raw)
    case_name = args.case_name or args.result.stem

    print(f"=== {case_name} ===")
    print(f"first pass: verdict={result.summary.verdict} risk={result.summary.risk}")
    print(f"first pass counts: {result.summary.counts.model_dump()}")

    trigger = evaluate_trigger(result)
    if trigger is None:
        print("no trigger: first pass already has a complete established path; recovery skipped.")
        return 0

    print(f"trigger: {trigger.gap_kinds} -- {trigger.reason}")

    try:
        client = create_default_llm_client(args.model, stage="ai_recovery_eval")
    except LLMClientError as exc:
        print(f"could not create LLM client: {exc}")
        return 1

    context = build_context(result, trigger)
    started = time.perf_counter()
    try:
        outcome = recover(context, repo_root=args.repo, client=client, trigger_reason=trigger.reason)
    except RecoveryError as exc:
        print(f"recovery failed: {exc}")
        return 1
    wall_ms = (time.perf_counter() - started) * 1000.0

    print(f"\nstatus: {outcome.result.status}")
    print(f"turns: {outcome.stats.turns}  llm_calls: {outcome.stats.llm_calls}  verify_retries: {outcome.stats.verify_retries}")
    print(f"tokens: prompt={outcome.stats.prompt_tokens} completion={outcome.stats.completion_tokens}")
    print(f"latency (LLM time): {outcome.stats.latency_ms:.0f}ms  wall time: {wall_ms:.0f}ms")
    print(f"files read: {outcome.stats.files_read}")
    print(f"tool calls: {[(c.tool, c.args) for c in outcome.stats.tool_calls]}")

    print("\nrecovered paths:")
    for path in outcome.result.recovered_paths:
        print(f"  [{path.status}] {path.entrypoint}  (target_node={path.target_node})")
        for edge in path.edges:
            print(f"    EDGE [{edge.status}/{edge.provenance}] {edge.from_symbol} -> {edge.to_symbol}")
            print(f"      relationship: {edge.relationship}")
            for ev in edge.evidence:
                print(f"      evidence: {ev.file}:{ev.line_start}-{ev.line_end} -- {ev.fact}")
            if edge.rejection_reason:
                print(f"      verifier: {edge.rejection_reason}")
        if path.unresolved_suffix:
            print("    unresolved_suffix (needed but not proven):")
            for edge in path.unresolved_suffix:
                print(f"      {edge.from_symbol} -> {edge.to_symbol}: {edge.rejection_reason}")

    print("\nrecovered tests:")
    for t in outcome.result.recovered_tests:
        print(f"  [{t.status}] {t.file} :: {t.test} -- covers: {t.covers}")
        for ev in t.evidence:
            print(f"      evidence: {ev.file}:{ev.line_start}-{ev.line_end} -- {ev.fact}")
        if t.rejection_reason:
            print(f"      verifier: {t.rejection_reason}")

    print("\ncorrected first-pass claims:")
    for c in outcome.result.corrected_first_pass_claims:
        print(f"  {c.claim}: {c.original!r} -> {c.corrected!r}")

    print("\nunresolved:")
    for u in outcome.result.unresolved:
        print(f"  {u.question} (missing: {u.missing_evidence})")

    if args.out:
        payload = {
            "case_name": case_name,
            "trigger": {"gap_kinds": list(trigger.gap_kinds), "reason": trigger.reason},
            "recovery": build_recovery_view(outcome.result),
            "stats": {
                "turns": outcome.stats.turns,
                "llm_calls": outcome.stats.llm_calls,
                "verify_retries": outcome.stats.verify_retries,
                "prompt_tokens": outcome.stats.prompt_tokens,
                "completion_tokens": outcome.stats.completion_tokens,
                "latency_ms": outcome.stats.latency_ms,
                "wall_ms": wall_ms,
                "files_read": outcome.stats.files_read,
                "tool_calls": [{"tool": c.tool, "args": c.args, "ok": c.ok} for c in outcome.stats.tool_calls],
            },
        }
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
