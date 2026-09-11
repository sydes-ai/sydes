#!/usr/bin/env python3
"""Phase B orchestrator: run the claim verifier against real, freshly
regenerated Layer 2 edges (`build_fixture.py`), verify the known
Settings/Duration chain end-to-end, and deliberately inject a fabricated
citation to prove the verifier actually rejects it -- not just a
hypothesis, a real corrupted input the same code path is run on.

Not wired into any production path.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from verifier import verify_edge_citation, verify_chain  # noqa: E402

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")
# Must match the branch `build_fixture.py` extracted `fresh_edges.json`
# from -- citations are only checkable against the SAME repo state the
# edges were derived from, not whatever branch happens to be checked out
# when this script runs (a real ordering bug, found empirically: the first
# run of this script silently verified real citations against master,
# after build_fixture.py had already restored it there, and rejected
# dozens of genuinely correct edges as a result).
TARGET_BRANCH = "py-real-01-max-output-duration"


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def load_edges() -> list[dict]:
    return json.loads((Path(__file__).parent / "fresh_edges.json").read_text(encoding="utf-8"))


def part1_bulk_citation_check(edges: list[dict]) -> dict:
    results = [verify_edge_citation(e, REPO_ROOT) for e in edges]
    verified = [r for r in results if r.verified]
    rejected = [r for r in results if not r.verified]
    print(f"[1/3] Citation check over {len(edges)} real, freshly-extracted edges: "
          f"{len(verified)} verified, {len(rejected)} rejected")
    for r in rejected[:10]:
        print(f"    REJECTED: {r.reason}  ({r.edge})")
    return {
        "total": len(edges), "verified": len(verified), "rejected": len(rejected),
        "rejected_samples": [{"edge": r.edge, "reason": r.reason} for r in rejected[:10]],
    }


_KNOWN_HANDLERS = ("create_speech", "create_captioned_speech", "create_dialogue")


def find_settings_chain(edges: list[dict]) -> list[dict]:
    """Assemble the known real chain in trail order: edges_in_order[i]'s
    user_symbol must equal edges_in_order[i+1]'s used_symbol. Several
    functions independently read Settings/reference OpenAISpeechRequest
    (e.g. `apply_alias_rate` alongside `create_speech`) -- prefer a path
    that reaches a known real route handler over an arbitrary first match,
    since that is the citeable case from every prior experiment round."""
    by_used: dict[str, list[dict]] = {}
    for e in edges:
        by_used.setdefault(e["used_symbol"], []).append(e)

    def search(current: str, seen: frozenset[str], depth: int) -> list[dict] | None:
        if depth > 6:
            return None
        candidates = by_used.get(current, [])
        # A candidate reaching a known handler directly wins immediately;
        # otherwise try each unseen candidate depth-first.
        direct = next((e for e in candidates if e["user_symbol"] in _KNOWN_HANDLERS), None)
        if direct is not None:
            return [direct]
        for e in candidates:
            nxt = e["user_symbol"]
            if nxt in seen:
                continue
            rest = search(nxt, seen | {nxt}, depth + 1)
            if rest is not None:
                return [e, *rest]
        return None

    return search("Settings", frozenset({"Settings"}), 0) or []


def part2_verify_known_chain(edges: list[dict]) -> dict:
    chain = find_settings_chain(edges)
    print(f"\n[2/3] Assembled chain ({len(chain)} hops): "
          + " -> ".join(["Settings"] + [e["user_symbol"] for e in chain]))

    result = verify_chain(chain, REPO_ROOT)
    print(f"    chain verified: {result.verified} ({result.reason})")
    for i, hop in enumerate(result.hops):
        print(f"    hop {i}: {hop.edge['user_symbol']} -> {hop.edge['used_symbol']}  "
              f"citation={hop.citation.verified}  adjacency={hop.adjacency_ok}  file_identity={hop.file_identity}")

    return {
        "chain_symbols": ["Settings"] + [e["user_symbol"] for e in chain],
        "verified": result.verified,
        "reason": result.reason,
        "hops": [
            {
                "edge": h.edge, "citation_verified": h.citation.verified,
                "citation_reason": h.citation.reason, "adjacency_ok": h.adjacency_ok,
                "file_identity": h.file_identity,
            }
            for h in result.hops
        ],
    }


def part3_fabricated_citation_is_rejected(edges: list[dict]) -> dict:
    real_edge = next(
        e for e in edges
        if e["kind"] == "class_field_type_reference" and verify_edge_citation(e, REPO_ROOT).verified
    )
    fabricated = copy.deepcopy(real_edge)
    fabricated["used_symbol"] = "ThisSymbolDoesNotExistAnywhereInTheFile"

    real_result = verify_edge_citation(real_edge, REPO_ROOT)
    fake_result = verify_edge_citation(fabricated, REPO_ROOT)

    print(f"\n[3/3] Fabricated-citation injection test:")
    print(f"    real edge   -> verified={real_result.verified}  ({real_result.reason})")
    print(f"    fabricated  -> verified={fake_result.verified}  ({fake_result.reason})")

    passed = real_result.verified and not fake_result.verified
    print(f"    TEST {'PASSED' if passed else 'FAILED'}: verifier "
          f"{'correctly distinguishes' if passed else 'FAILED TO distinguish'} real vs fabricated citations")

    return {
        "real_edge": real_edge, "real_verified": real_result.verified,
        "fabricated_edge": fabricated, "fabricated_verified": fake_result.verified,
        "test_passed": passed,
    }


def main() -> int:
    status = _git(["status", "--porcelain"])
    if status:
        print(f"ERROR: {REPO_ROOT} has uncommitted changes, refusing to switch branches:\n{status}")
        return 1
    original_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    _git(["checkout", "--quiet", TARGET_BRANCH])

    try:
        edges = load_edges()
        part1 = part1_bulk_citation_check(edges)
        part2 = part2_verify_known_chain(edges)
        part3 = part3_fabricated_citation_is_rejected(edges)
    finally:
        _git(["checkout", "--quiet", original_branch])

    out_path = Path(__file__).parent / "phase_b_results.json"
    out_path.write_text(json.dumps({"part1": part1, "part2": part2, "part3": part3}, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path} (repo restored to {original_branch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
