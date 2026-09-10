#!/usr/bin/env python3
"""EXPERIMENT harness for `sydes.recovery.likely_impact_evidence`.

Evaluates whether the evidence-checker can distinguish a same-class/file-
neighborhood FALSE "likely, not fully established" candidate from a
legitimate one, against a small, hand-curated set of real candidates
pulled from two real PRs (sydes-examples/Kokoro-FastAPI#6 and #7) plus one
naturally-occurring candidate that should genuinely PROMOTE (a Pydantic
`AfterValidator` wiring — real indirection a plain call-graph trace
would not see).

This is a ONE-SHOT evaluation run, not a tuning loop: do not iterate the
prompt against these exact cases until they all pass (see the task's own
explicit "avoid overfitting" instruction) — run once, record the result
honestly, and decide from that whether the checker is reliable enough to
wire into production.

Usage:
    uv run python scripts/evaluate_likely_impact_evidence.py \\
        --repo /path/to/local/Kokoro-FastAPI/checkout \\
        --model openai:gpt-4.1-mini \\
        --out /tmp/likely-impact-eval.json

`--repo` must be a git checkout with `py-real-01-max-output-duration` and
`py-real-02-normalizer-voice-maintenance` as real local or remote-tracked
branches -- the script checks out each candidate's branch before running
its check, and restores the original branch when done (or on failure).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sydes.llm.client import LLMClientError, create_default_llm_client
from sydes.recovery.likely_impact_evidence import (
    EvidenceCheckStats,
    LikelyImpactCandidate,
    check_candidate_evidence,
)
from sydes.recovery.tools import RepoTools


@dataclass(frozen=True)
class EvalCase:
    name: str
    branch: str
    candidate: LikelyImpactCandidate
    # "legitimate" -- oracle says the connection genuinely exists (should
    # promote); "false" -- oracle says it does NOT exist (should suppress).
    # No case here has a genuinely-undecidable oracle; that limitation is
    # recorded honestly in the report rather than manufactured.
    oracle: str


# ---------------------------------------------------------------------------
# The eval set. Six known-FALSE candidates (four from PR1, two from PR2,
# all confirmed by direct source inspection during the real fork runs) and
# one known-LEGITIMATE candidate (PR1's own `_within_duration_ceiling`
# inferred impact, which genuinely IS wired to POST /audio/speech through
# Pydantic's AfterValidator mechanism -- real indirection, not a plain
# call-graph edge) so the set is not one-sided.
# ---------------------------------------------------------------------------

CASES: list[EvalCase] = [
    EvalCase(
        name="pr1_dev_model_false",
        branch="py-real-01-max-output-duration",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="GET /dev/model",
            candidate_label="model_status",
            proposed_target="generate_audio_stream",
            changed_files=(
                "api/src/services/tts_service.py",
                "api/src/routers/openai_compatible.py",
                "api/src/structures/schemas.py",
                "api/src/core/config.py",
            ),
        ),
    ),
    EvalCase(
        name="pr1_generate_from_phonemes_false",
        branch="py-real-01-max-output-duration",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="POST /dev/generate_from_phonemes",
            candidate_label="generate_from_phonemes",
            proposed_target="generate_audio_stream",
            changed_files=(
                "api/src/services/tts_service.py",
                "api/src/routers/openai_compatible.py",
            ),
        ),
    ),
    EvalCase(
        name="pr1_dev_reload_false",
        branch="py-real-01-max-output-duration",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="POST /dev/reload",
            candidate_label="reload_model",
            proposed_target="generate_audio_stream",
            changed_files=("api/src/services/tts_service.py",),
        ),
    ),
    EvalCase(
        name="pr1_dev_unload_false",
        branch="py-real-01-max-output-duration",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="POST /dev/unload",
            candidate_label="unload_model",
            proposed_target="generate_audio_stream",
            changed_files=("api/src/services/tts_service.py",),
        ),
    ),
    EvalCase(
        name="pr1_within_duration_ceiling_legitimate",
        branch="py-real-01-max-output-duration",
        oracle="legitimate",
        candidate=LikelyImpactCandidate(
            entry_label="POST /audio/speech",
            candidate_label="_within_duration_ceiling",
            proposed_target="_within_duration_ceiling",
            changed_files=("api/src/structures/schemas.py",),
        ),
    ),
    EvalCase(
        name="pr2_convert_audio_false",
        branch="py-real-02-normalizer-voice-maintenance",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="POST /audio/speech",
            candidate_label="AudioService.convert_audio",
            proposed_target="handle_url",
            changed_files=(
                "api/src/services/text_processing/normalizer.py",
                "api/src/inference/voice_manager.py",
            ),
        ),
    ),
    EvalCase(
        name="pr2_trim_audio_false",
        branch="py-real-02-normalizer-voice-maintenance",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="POST /audio/speech",
            candidate_label="AudioService.trim_audio",
            proposed_target="handle_url",
            changed_files=(
                "api/src/services/text_processing/normalizer.py",
                "api/src/inference/voice_manager.py",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Held-out cases, added ONLY after the prompt was revised in response
    # to the first run's false suppression -- these were never used to
    # shape that revision, so they're a genuine (if small) check that the
    # fix generalizes rather than merely memorizing the one failure.
    # ------------------------------------------------------------------
    EvalCase(
        name="pr1_settings_legitimate_heldout",
        branch="py-real-01-max-output-duration",
        oracle="legitimate",
        candidate=LikelyImpactCandidate(
            entry_label="POST /audio/speech",
            candidate_label="Settings",
            proposed_target="Settings.max_output_duration_s",
            changed_files=("api/src/core/config.py", "api/src/structures/schemas.py"),
        ),
    ),
    EvalCase(
        name="pr1_settings_wrong_entrypoint_false_heldout",
        branch="py-real-01-max-output-duration",
        oracle="false",
        candidate=LikelyImpactCandidate(
            entry_label="POST /dev/generate_from_phonemes",
            candidate_label="Settings",
            proposed_target="Settings.max_output_duration_s",
            changed_files=("api/src/core/config.py", "api/src/structures/schemas.py"),
        ),
    ),
]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--model", default="openai:gpt-4.1-mini")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    status = _git(args.repo, "status", "--porcelain")
    if status:
        print(f"ERROR: {args.repo} has uncommitted changes, refusing to switch branches:\n{status}")
        return 1
    original_branch = _git(args.repo, "rev-parse", "--abbrev-ref", "HEAD")

    try:
        client = create_default_llm_client(args.model, stage="likely_impact_evidence_eval")
    except LLMClientError as exc:
        print(f"could not create LLM client: {exc}")
        return 1

    results: list[dict] = []
    try:
        for case in CASES:
            _git(args.repo, "checkout", "--quiet", case.branch)
            tools = RepoTools(args.repo)
            stats = EvidenceCheckStats()
            started = time.perf_counter()
            outcome = check_candidate_evidence(
                case.candidate, tools=tools, client=client, max_turns=6, stats=stats
            )
            wall_ms = (time.perf_counter() - started) * 1000.0

            false_suppression = case.oracle == "legitimate" and outcome.decision == "suppress"
            false_retention = case.oracle == "false" and outcome.decision == "retain"
            correctly_promoted = case.oracle == "legitimate" and outcome.decision == "promote"
            correctly_suppressed = case.oracle == "false" and outcome.decision == "suppress"

            row = {
                "name": case.name,
                "candidate": f"{case.candidate.entry_label} -> {case.candidate.proposed_target}",
                "oracle": case.oracle,
                "decision": outcome.decision,
                "reason": outcome.reason,
                "evidence_files": list(outcome.evidence_files),
                "false_suppression": false_suppression,
                "false_retention": false_retention,
                "correctly_promoted": correctly_promoted,
                "correctly_suppressed": correctly_suppressed,
                "turns": stats.turns,
                "llm_calls": stats.llm_calls,
                "prompt_tokens": stats.prompt_tokens,
                "completion_tokens": stats.completion_tokens,
                "latency_ms": stats.latency_ms,
                "wall_ms": wall_ms,
            }
            results.append(row)
            print(
                f"[{case.oracle:10}] {case.name:38} -> {outcome.decision:8} "
                f"({stats.turns} turns, {wall_ms:.0f}ms) :: {outcome.reason[:100]}"
            )
    finally:
        _git(args.repo, "checkout", "--quiet", original_branch)

    n = len(results)
    false_suppressions = sum(1 for r in results if r["false_suppression"])
    false_retentions = sum(1 for r in results if r["false_retention"])
    correctly_suppressed = sum(1 for r in results if r["correctly_suppressed"])
    correctly_promoted = sum(1 for r in results if r["correctly_promoted"])
    total_prompt_tokens = sum(r["prompt_tokens"] for r in results)
    total_completion_tokens = sum(r["completion_tokens"] for r in results)
    total_wall_ms = sum(r["wall_ms"] for r in results)

    print("\n=== SUMMARY ===")
    print(f"cases: {n}")
    print(f"correctly suppressed known-false candidates: {correctly_suppressed}/6")
    print(f"correctly promoted the one legitimate candidate: {correctly_promoted}/1")
    print(f"false suppressions (legitimate marked suppress -- DANGEROUS): {false_suppressions}")
    print(f"false retentions (false candidate left as retain -- safe but unhelpful): {false_retentions}")
    print(f"total tokens: prompt={total_prompt_tokens} completion={total_completion_tokens}")
    print(f"total wall time: {total_wall_ms:.0f}ms")

    if args.out:
        payload = {
            "model": args.model,
            "cases": results,
            "summary": {
                "n": n,
                "correctly_suppressed": correctly_suppressed,
                "correctly_promoted": correctly_promoted,
                "false_suppressions": false_suppressions,
                "false_retentions": false_retentions,
                "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens,
                "total_wall_ms": total_wall_ms,
            },
        }
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
