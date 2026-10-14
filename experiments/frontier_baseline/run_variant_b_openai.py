"""Variant B (frontier model alone, OpenAI) for the 3 remaining hard-v1
cases. Frozen generic prompt, no Sydes facts, no oracle hints. Agentic
(read_file/list_dir/grep tools against the real repo). 2 repeats per case.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from openai_agentic import run_agentic  # noqa: E402

FROZEN_PROMPT = (
    "Analyze this diff in the repository. Identify the system entrypoints "
    "and behaviors affected by the change. Trace evidence-backed paths from "
    "those entrypoints to the changed behavior. Identify relevant tests. "
    "Explicitly state anything you could not establish. Cite every "
    "nontrivial hop with source evidence.\n\n"
    "You have list_dir/read_file/grep tools to explore the repository "
    "yourself. Use them as needed before answering.\n\n"
    "## The diff\n\n```diff\n{diff}\n```\n"
)

CASES = {
    "go-h-01": {
        "repo_root": "/Users/ksnaik/sample_repos/simplebank",
        "base": "master", "head": "go-h-01-skip-verify-email-if-already-verified",
        "diff_paths": ["worker/task_send_verify_email.go"],
    },
    "java-h-01": {
        "repo_root": "/Users/ksnaik/sample_repos/spring-boot-demo",
        "base": "master", "head": "java-h-01-jwt-grace-window-after-refresh",
        "diff_paths": ["**/JwtUtil.java"],
    },
    "rust-h-01": {
        "repo_root": "/Users/ksnaik/sample_repos/realworld-axum-sqlx",
        "base": "main", "head": "rs-h-01-jwt-grace-period-after-expiry",
        "diff_paths": ["src/http/extractor.rs"],
    },
}


def get_diff(repo_root: str, base: str, head: str, paths: list[str]) -> str:
    return subprocess.run(
        ["git", "diff", base, head, "--", *paths],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout


def main() -> None:
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    for case_name, cfg in CASES.items():
        diff = get_diff(cfg["repo_root"], cfg["base"], cfg["head"], cfg["diff_paths"])
        prompt = FROZEN_PROMPT.format(diff=diff)
        for rep in (1, 2):
            print(f"--- {case_name} variant B openai run {rep} ---", flush=True)
            result = run_agentic(repo_root=Path(cfg["repo_root"]), prompt=prompt)
            out_path = out_dir / f"{case_name}_B_openai_run{rep}.json"
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(f"wrote {out_path} ({result['tool_calls']} tool calls, "
                  f"{len(result['final_answer'])} chars)", flush=True)


if __name__ == "__main__":
    main()
