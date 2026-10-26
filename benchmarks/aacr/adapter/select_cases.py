"""Build the fixed AACR-Bench case lists Sydes is evaluated on.

Selection is mechanical and never looks at how Sydes performs:

1. Start from a published, externally chosen subset: OpenQodex's AACR split
   (`benchmark/splits/aacr-v1.json`, Apache-2.0), whose `dev-smoke` takes the
   smallest development PR per language and whose `dev-laptop` takes 12
   development PRs from each third by change size. Their split assigns whole
   repository groups to dev/test by a salted hash, so it is neutral to us.
2. Keep the cases whose `project_main_language` is one Sydes supports
   (`SUPPORTED_LANGUAGES`), in the subset's own order.

Each case records only what a reviewer may see: repository, PR, base and head
commits, language and size. Reference comments are never copied here, so
nothing downstream of this file can leak them into a Sydes run.

    uv run --no-project python benchmarks/aacr/adapter/select_cases.py \
        --aacr-dataset <aacr checkout>/dataset/positive_samples.json \
        --split <openqodex checkout>/benchmark/splits/aacr-v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

#: Languages Sydes has been validated on end to end. A language outside this set
#: is excluded from the Sydes-supported subsets, never scored as a miss.
SUPPORTED_LANGUAGES = ("Python", "TypeScript", "JavaScript", "Java", "Go", "Rust")

_PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")
HERE = Path(__file__).resolve().parent.parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_name(pr_url: str) -> str:
    """OpenQodex's case name for a PR: `<owner>__<repo>-<number>`."""
    match = _PR_URL.match(pr_url)
    if match is None:
        raise ValueError(f"not a GitHub PR URL: {pr_url}")
    return f"{match[1]}__{match[2]}-{match[3]}"


def instance_id(repo: str, head: str) -> str:
    """AACR's own instance id (its converter): `<owner>__<name>@<head[:7]>`."""
    return f"{repo.replace('/', '__')}@{head[:7]}"


def build(dataset: list[dict], subset: list[str]) -> list[dict]:
    by_name = {case_name(r["githubPrUrl"]): r for r in dataset}
    cases = []
    for name in subset:
        record = by_name.get(name)
        if record is None:
            raise ValueError(f"subset case {name} is not in the AACR dataset")
        if record["project_main_language"] not in SUPPORTED_LANGUAGES:
            continue
        owner, repo, number = _PR_URL.match(record["githubPrUrl"]).groups()
        cases.append({
            "case_id": name,
            "instance_id": instance_id(f"{owner}/{repo}", record["target_commit"]),
            "repo": f"{owner}/{repo}",
            "pr": int(number),
            "pr_url": record["githubPrUrl"],
            "language": record["project_main_language"],
            "base_commit": record["source_commit"],
            "head_commit": record["target_commit"],
            "change_line_count": record["change_line_count"],
        })
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aacr-dataset", type=Path, required=True)
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=HERE / "cases")
    args = parser.parse_args()

    dataset = json.loads(args.aacr_dataset.read_text())
    split = json.loads(args.split.read_text())
    source = {
        "aacr_dataset_sha256": _sha256(args.aacr_dataset),
        "split_file_sha256": _sha256(args.split),
        "split_name": split.get("name"),
        "split_dataset_revision": (split.get("dataset") or {}).get("revision"),
        "supported_languages": list(SUPPORTED_LANGUAGES),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    for subset in ("dev-smoke", "dev-laptop"):
        cases = build(dataset, split["subsets"][subset])
        out = args.out / f"{subset}-supported.json"
        out.write_text(json.dumps({
            "version": 1,
            "subset": f"openqodex aacr-v1 {subset}, Sydes-supported languages",
            "rule": ("OpenQodex's published subset, in its order, keeping only cases whose "
                     "project_main_language is in supported_languages. No case is chosen by Sydes' results."),
            "source": source,
            "cases": cases,
        }, indent=2) + "\n")
        print(f"{out}: {len(cases)} of {len(split['subsets'][subset])} cases")


if __name__ == "__main__":
    main()
