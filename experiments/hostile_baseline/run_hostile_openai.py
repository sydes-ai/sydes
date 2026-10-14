"""Hostile-round benchmark, OpenAI side. Variant B (frontier alone) and
Variant C (frontier + oracle-free Sydes facts), 2 repeats each, across the
3 hostile cases. Frozen prompt (with the coverage-protocol addition) from
frozen_prompt.py; agentic tool loop from the prior round's openai_agentic.py
(unmodified, reused as-is). No new deterministic bridges/mechanisms are
built anywhere in this script, per this round's explicit instruction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "frontier_baseline"))
sys.path.insert(0, str(Path(__file__).parent))

from openai_agentic import run_agentic  # noqa: E402
from cases import CASES, get_diff  # noqa: E402
from frozen_prompt import FROZEN_PROMPT  # noqa: E402
from sydes_context import CONTEXT_BY_CASE  # noqa: E402


def main() -> None:
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    for case_name, cfg in CASES.items():
        diff = get_diff(cfg["git_root"], cfg["sha"])
        base_prompt = FROZEN_PROMPT.format(diff=diff)

        for rep in (1, 2):
            out_path = out_dir / f"{case_name}_B_openai_run{rep}.json"
            if out_path.exists():
                print(f"skip (exists): {out_path}", flush=True)
                continue
            print(f"--- {case_name} variant B (frontier alone) openai run {rep} ---", flush=True)
            result = run_agentic(repo_root=Path(cfg["repo_root"]), prompt=base_prompt)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(f"wrote {out_path} ({result['tool_calls']} tool calls, {len(result['final_answer'])} chars)", flush=True)

        c_prompt = base_prompt + "\n" + CONTEXT_BY_CASE[case_name]
        for rep in (1, 2):
            out_path = out_dir / f"{case_name}_C_openai_run{rep}.json"
            if out_path.exists():
                print(f"skip (exists): {out_path}", flush=True)
                continue
            print(f"--- {case_name} variant C (frontier + Sydes facts) openai run {rep} ---", flush=True)
            result = run_agentic(repo_root=Path(cfg["repo_root"]), prompt=c_prompt)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(f"wrote {out_path} ({result['tool_calls']} tool calls, {len(result['final_answer'])} chars)", flush=True)


if __name__ == "__main__":
    main()
