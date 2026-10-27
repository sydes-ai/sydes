"""Experiment only: replay an Investigator snapshot under one lookup allocation.

    python -m sydes.verify.investigation_replay --snapshot X.snapshot.json \
        --allocation two_round --model openai:... --json OUT.json

Runs in the target repository's checkout (the current directory, or --repo-root).
Writes OUT.json -- a result with `code_findings`, `code_review_status`, `run_metrics`
(the replay's own LLM usage) and `diagnostics` -- and OUT.investigation.json beside it.
The Finder does not run: the snapshot's packet and hypotheses are investigated again.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sydes.llm.client import LLMClientError
from sydes.verify import investigation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--allocation", choices=investigation.ALLOCATIONS, required=True)
    parser.add_argument("--model")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--json", type=Path, required=True, dest="json_output")
    parser.add_argument("--verifier-prompt", choices=tuple(investigation.VERIFIER_PROMPTS), default="control")
    parser.add_argument("--verifier-input-out", type=Path,
                        help="also write the Verifier's exact inputs here, for sydes.verify.verifier_replay")
    args = parser.parse_args(argv)

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    status = "completed"
    try:
        findings, artifact, notes = investigation.replay(snapshot, repo_root=args.repo_root.resolve(),
                                                         model_spec=args.model, allocation=args.allocation,
                                                         verifier_prompt=args.verifier_prompt,
                                                         verifier_input_out=args.verifier_input_out)
    except LLMClientError as exc:  # client construction; per-call failures land in artifact["errors"]
        findings, artifact, notes, status = [], {"errors": [f"replay: {exc}"]}, [f"code_review_unavailable: {exc}"], "unavailable"
    usage = artifact.get("usage") or {}
    result = {
        "code_findings": [f.model_dump(mode="json") for f in findings],
        "code_review_status": status,
        "run_metrics": {"llm_totals": {"calls": usage.get("llm_calls", 0), "input_tokens": usage.get("prompt_tokens", 0),
                                       "output_tokens": usage.get("completion_tokens", 0), "errors": 0},
                        "llm_by_purpose": {stage: {"calls": n} for stage, n in (usage.get("by_stage") or {}).items()}},
        "diagnostics": notes,
    }
    args.json_output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    args.json_output.with_name(args.json_output.stem + ".investigation.json").write_text(
        json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
