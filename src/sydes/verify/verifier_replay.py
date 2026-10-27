"""Experiment only: decide frozen Verifier inputs again under one Verifier prompt.

    python -m sydes.verify.verifier_replay --inputs X.verifier-input.json \
        --prompt strict --model openai:... --json OUT.json

Runs in the target repository's checkout (the current directory, or --repo-root), which is
only read, to verify quotes. Writes OUT.json -- `code_findings`, `code_review_status`,
`run_metrics` (the Verifier's own LLM usage) and `diagnostics` -- and OUT.investigation.json
beside it. No Finder, no Investigator, no lookups: only Verifier -> check_confirmation -> findings.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sydes.llm.client import LLMClientError
from sydes.verify import investigation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--prompt", choices=tuple(investigation.VERIFIER_PROMPTS), required=True)
    parser.add_argument("--model")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--json", type=Path, required=True, dest="json_output")
    args = parser.parse_args(argv)

    frozen = json.loads(args.inputs.read_text(encoding="utf-8"))
    status = "completed"
    try:
        findings, artifact, notes = investigation.replay_verifier(frozen, repo_root=args.repo_root.resolve(),
                                                                  model_spec=args.model, verifier_prompt=args.prompt)
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
