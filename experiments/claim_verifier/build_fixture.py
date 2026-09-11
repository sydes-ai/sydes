#!/usr/bin/env python3
"""Phase B fixture: regenerate real Layer 2 edges FRESH against whatever
Kokoro-FastAPI content is on disk right now (rather than trusting a JSON
snapshot possibly written against a different git branch/commit), so the
claim verifier is checked against ground truth it can actually re-read.

Reuses experiments/layer2_generic_edges' extractors as-is (no copy, no
fork) -- this experiment is about verifying claims, not re-deriving them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SYDES_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SYDES_ROOT / "experiments" / "layer2_generic_edges"))
from python_extractors import extract_all_same_file, resolve_cross_file_parameter_types  # noqa: E402
from member_access_extractor import extract_member_access_edges  # noqa: E402

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")
# The Duration/Settings chain (this fixture's known real multi-hop case)
# only exists on this PR branch, not on master -- same branch prior Layer 2
# experiments used for it.
TARGET_BRANCH = "py-real-01-max-output-duration"
FILES_TO_SCAN = [
    "api/src/structures/schemas.py",
    "api/src/core/config.py",
    "api/src/routers/openai_compatible.py",
    "api/src/routers/development.py",
]


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _member_access_edges_with_citation_text(source: str, *, file: str) -> list[dict]:
    """Same relation `member_access_extractor.edges_only` produces, but
    keeping the one field it discards that a citation verifier actually
    needs: the literal `receiver.member` text, not just the resolved type
    it names -- see verifier.py's own docstring on why `used_symbol` alone
    can never be checked for this edge kind."""
    entries = extract_member_access_edges(source, file=file, repo_root=REPO_ROOT)
    return [
        {
            "kind": "reads_member",
            "user_file": e["file"], "user_symbol": e["function"],
            "used_file": None, "used_symbol": e["resolved_type"],
            "line": e["line"], "provenance": e["provenance"],
            "citation_text": f"{e['receiver']}.{e['member']}",
        }
        for e in entries if e["status"] == "resolved"
    ]


def build() -> list[dict]:
    edges: list[dict] = []
    for rel in FILES_TO_SCAN:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        edges.extend(extract_all_same_file(source, file=rel))
        edges.extend(_member_access_edges_with_citation_text(source, file=rel))
    edges.extend(resolve_cross_file_parameter_types(REPO_ROOT))
    return edges


def main() -> int:
    status = _git(["status", "--porcelain"])
    if status:
        print(f"ERROR: {REPO_ROOT} has uncommitted changes, refusing to switch branches:\n{status}")
        return 1
    original_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])

    _git(["checkout", "--quiet", TARGET_BRANCH])
    try:
        edges = build()
    finally:
        _git(["checkout", "--quiet", original_branch])

    out_path = Path(__file__).parent / "fresh_edges.json"
    out_path.write_text(json.dumps(edges, indent=2), encoding="utf-8")
    print(f"wrote {len(edges)} edges (regenerated live against {TARGET_BRANCH}, restored to {original_branch}) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
