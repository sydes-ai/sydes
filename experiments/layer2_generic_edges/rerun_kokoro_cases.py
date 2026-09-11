#!/usr/bin/env python3
"""Rerun cases 5 and 6 (the two TRUE legitimate Likely cases) with Layer 2
generic edges added, checking whether the full chain from the changed
symbol to a known real entrypoint handler now assembles.

Also runs the same-file extractors across every changed file in the
diff for cases 1/3/4/7/8 as a NOISE check: do these generic extractors
spuriously connect any false candidate that real evidence (Phase 2)
already showed has no connection? A real edge count and a sample are
printed for manual inspection -- this is the "hairball or useful"
check requested before trusting the approach.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from python_extractors import extract_all_same_file, resolve_cross_file_parameter_types

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")

FILES_TO_SCAN = [
    "api/src/structures/schemas.py",
    "api/src/core/config.py",
    "api/src/routers/openai_compatible.py",
    "api/src/routers/development.py",
    "api/src/services/tts_service.py",
    "api/src/services/audio.py",
]


def build_edge_set() -> list[dict]:
    edges: list[dict] = []
    for rel in FILES_TO_SCAN:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        edges.extend(extract_all_same_file(source, file=rel))
    edges.extend(resolve_cross_file_parameter_types(REPO_ROOT))
    return edges


def backward_reachable(edges: list[dict], start_symbol: str, max_depth: int = 6) -> tuple[bool, list[str]]:
    """BFS backward (used -> user) from start_symbol, name-keyed (this
    experiment does not carry full qualified/file identity through the
    chain -- a known, documented simplification; see report)."""
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


def main() -> int:
    edges = build_edge_set()
    print(f"total Layer 2 edges extracted: {len(edges)}\n")

    for case_name, start in [
        ("5_settings", "max_output_duration_s"),
        ("6_within_duration_ceiling", "_within_duration_ceiling"),
    ]:
        reached, trail = backward_reachable(edges, start)
        print(f"{case_name}: reached_known_handler={reached}")
        if reached:
            print(f"  chain: {' -> '.join(trail)}")
        print()

    # Noise check: do the false candidates gain any spurious connection to
    # generate_audio/generate_audio_stream via these NEW edge kinds alone?
    print("--- noise check: false candidates ---")
    for target in ("generate_audio_stream", "generate_audio"):
        inbound_names = sorted({e["user_symbol"] for e in edges if e["used_symbol"] == target})
        print(f"inbound (Layer 2 only) to {target}: {inbound_names}")

    for false_handler in ("model_status", "generate_from_phonemes", "reload_model", "unload_model", "convert_audio", "trim_audio"):
        outbound_targets = sorted({e["used_symbol"] for e in edges if e["user_symbol"] == false_handler})
        print(f"Layer-2-only outbound from {false_handler}: {outbound_targets}")

    out_path = Path(__file__).parent / "layer2_kokoro_edges.json"
    out_path.write_text(json.dumps(edges, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path} ({len(edges)} edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
