"""Variant C (frontier model + Sydes structured context, OpenAI), 1 repeat
per case (disclosed scope reduction). Same frozen prompt as B, plus the
oracle-free Sydes facts block appended -- no oracle/gt language."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from openai_agentic import run_agentic  # noqa: E402
from run_variant_b_openai import CASES, get_diff, FROZEN_PROMPT  # noqa: E402
from sydes_context import GO_CONTEXT, JAVA_CONTEXT, RUST_CONTEXT  # noqa: E402

CONTEXT_BY_CASE = {"go-h-01": GO_CONTEXT, "java-h-01": JAVA_CONTEXT, "rust-h-01": RUST_CONTEXT}


def main() -> None:
    out_dir = Path(__file__).parent / "results"
    for case_name, cfg in CASES.items():
        diff = get_diff(cfg["repo_root"], cfg["base"], cfg["head"], cfg["diff_paths"])
        prompt = FROZEN_PROMPT.format(diff=diff) + "\n" + CONTEXT_BY_CASE[case_name]
        print(f"--- {case_name} variant C openai run 1 ---", flush=True)
        result = run_agentic(repo_root=Path(cfg["repo_root"]), prompt=prompt)
        out_path = out_dir / f"{case_name}_C_openai_run1.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {out_path} ({result['tool_calls']} tool calls, {len(result['final_answer'])} chars)", flush=True)


if __name__ == "__main__":
    main()
