#!/usr/bin/env python3
"""Phase 4/5: rerun both legitimate cases (5, 6) with the member-access
extractor added on top of the prior Layer 2 edges, and rerun the full
false-candidate noise regression across all 6 known-false candidates from
PR6/PR7.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from python_extractors import extract_all_same_file, resolve_cross_file_parameter_types
from member_access_extractor import extract_member_access_edges, edges_only

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")

# PR6 files (cases 1-6)
PR6_FILES = [
    "api/src/structures/schemas.py",
    "api/src/core/config.py",
    "api/src/routers/openai_compatible.py",
    "api/src/routers/development.py",
    "api/src/services/tts_service.py",
]

# PR7 files (cases 7-8) -- separate branch, scanned separately
PR7_FILES = [
    "api/src/services/text_processing/normalizer.py",
    "api/src/inference/voice_manager.py",
    "api/src/services/audio.py",
    "api/src/services/tts_service.py",
]


def build_edge_set(files: list[str]) -> tuple[list[dict], list[dict]]:
    """Returns (declaration_edges, member_access_raw_entries)."""
    decl_edges: list[dict] = []
    member_entries: list[dict] = []
    for rel in files:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        decl_edges.extend(extract_all_same_file(source, file=rel))
        member_entries.extend(extract_member_access_edges(source, file=rel, repo_root=REPO_ROOT))
    decl_edges.extend(resolve_cross_file_parameter_types(REPO_ROOT))
    return decl_edges, member_entries


def backward_reachable(edges: list[dict], start_symbol: str, max_depth: int = 6) -> tuple[bool, list[str]]:
    inbound: dict[str, list[str]] = {}
    for e in edges:
        inbound.setdefault(e["used_symbol"], []).append(e["user_symbol"])

    visited = {start_symbol}
    queue = deque([(start_symbol, [start_symbol])])
    while queue:
        current, trail = queue.popleft()
        if len(trail) > max_depth:
            continue
        for user in inbound.get(current, []):
            if user in visited:
                continue
            visited.add(user)
            new_trail = trail + [user]
            if user in ("create_speech", "create_captioned_speech", "create_dialogue"):
                return True, new_trail
            queue.append((user, new_trail))
    return False, []


def _git_checkout(branch: str) -> None:
    subprocess.run(["git", "checkout", "--quiet", branch], cwd=REPO_ROOT, check=True)


def main() -> int:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout
    if status.strip():
        print(f"ERROR: {REPO_ROOT} has uncommitted changes, refusing to switch branches:\n{status}")
        return 1
    original_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()

    _git_checkout("py-real-01-max-output-duration")
    decl_edges, member_entries = build_edge_set(PR6_FILES)
    member_edges = edges_only(member_entries)
    all_edges = decl_edges + member_edges

    print(f"declaration edges: {len(decl_edges)}, member-access entries: {len(member_entries)} "
          f"(resolved: {len(member_edges)}, unknown: {sum(1 for e in member_entries if e['status']=='unknown')}, "
          f"ambiguous: {sum(1 for e in member_entries if e['status']=='ambiguous')})\n")

    results = {}
    for case_name, start in [
        ("5_settings", "Settings"),
        ("6_within_duration_ceiling", "_within_duration_ceiling"),
    ]:
        reached, trail = backward_reachable(all_edges, start)
        print(f"{case_name}: reached_known_handler={reached}")
        if reached:
            print(f"  chain: {' -> '.join(trail)}")
        results[case_name] = {"reached": reached, "chain": trail}
        print()

    print("--- Phase 5: false-candidate noise regression (PR6 false candidates) ---")
    false_candidates_pr6 = ["model_status", "generate_from_phonemes", "reload_model", "unload_model"]
    noise_report = {}
    for false_handler in false_candidates_pr6:
        member_outbound = [e for e in member_edges if e["user_symbol"] == false_handler]
        decl_outbound = sorted({e["used_symbol"] for e in decl_edges if e["user_symbol"] == false_handler})
        print(f"{false_handler}: member-access edges = {member_outbound}, declaration edges = {decl_outbound}")
        noise_report[false_handler] = {
            "member_access_edges": member_outbound,
            "declaration_edges": decl_outbound,
            "connects_to_generate_audio_stream": any(
                e["used_symbol"] in ("generate_audio_stream", "generate_audio") for e in member_outbound
            ) or "generate_audio_stream" in decl_outbound or "generate_audio" in decl_outbound,
        }

    print("\n--- Phase 5: false-candidate noise regression (PR7 false candidates) ---")
    _git_checkout("py-real-02-normalizer-voice-maintenance")
    decl_edges_pr7, member_entries_pr7 = build_edge_set(PR7_FILES)
    member_edges_pr7 = edges_only(member_entries_pr7)
    for false_handler in ("convert_audio", "trim_audio"):
        member_outbound = [e for e in member_edges_pr7 if e["user_symbol"] == false_handler]
        decl_outbound = sorted({e["used_symbol"] for e in decl_edges_pr7 if e["user_symbol"] == false_handler})
        print(f"{false_handler}: member-access edges = {member_outbound}, declaration edges = {decl_outbound}")
        noise_report[false_handler] = {
            "member_access_edges": member_outbound,
            "declaration_edges": decl_outbound,
        }

    # Also: did the member-access extractor introduce noise ANYWHERE in the
    # scanned PR6 files (not just the 4 false handlers) pointing at the
    # changed target?
    spurious_into_target = [e for e in member_edges if e["used_symbol"] in ("generate_audio", "generate_audio_stream")]
    print(f"\nany member-access edge (from ANY function) into generate_audio/generate_audio_stream: {spurious_into_target}")

    out = {
        "reachability": results,
        "noise_report": noise_report,
        "spurious_into_generate_audio": spurious_into_target,
        "member_access_entries_pr6_full": member_entries,
        "unknown_count": sum(1 for e in member_entries if e["status"] == "unknown"),
        "ambiguous_count": sum(1 for e in member_entries if e["status"] == "ambiguous"),
        "resolved_count": sum(1 for e in member_entries if e["status"] == "resolved"),
    }
    out_path = Path(__file__).parent / "settings_reachability.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out_path}")

    _git_checkout(original_branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
