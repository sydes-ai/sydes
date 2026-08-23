"""Spike harness: index, query, mutate, re-index, and record timings.

Deliberately standalone — it imports no `sydes` module, so it cannot affect
normal execution. See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

CACHE = Path.home() / ".cache" / "codebase-memory-mcp"


def cbm(binary: str, tool: str, args: dict) -> dict:
    """Invoke one tool through the headless CLI and return its structured payload."""
    proc = subprocess.run(
        [binary, "cli", "--json", tool, json.dumps(args)],
        capture_output=True, text=True, timeout=1800,
    )
    payload = json.loads(proc.stdout or "{}")
    return payload.get("structuredContent", payload)


def timed(fn, *args, **kwargs) -> tuple[object, float]:
    start = time.perf_counter()
    return fn(*args, **kwargs), time.perf_counter() - start


def db_for(repo: Path) -> Path | None:
    """The project database CBM derives from an absolute repo path."""
    slug = str(repo.resolve()).strip("/").replace("/", "-").replace("_", "_")
    candidate = CACHE / f"{slug}.db"
    return candidate if candidate.exists() else None


def counts(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        return {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("nodes", "edges", "file_hashes")
        }
    finally:
        conn.close()


def edge_set(db: Path) -> set[str]:
    """Every edge as `type source -> target`, for cold-vs-incremental diffing."""
    conn = sqlite3.connect(db)
    try:
        return {
            f"{t} {s} -> {d}"
            for t, s, d in conn.execute(
                "SELECT e.type, s.qualified_name, t.qualified_name FROM edges e "
                "JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id"
            )
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cbm", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--touch", help="repo-relative file to modify for the incremental leg")
    options = parser.parse_args()
    repo = options.repo.resolve()

    result, cold = timed(cbm, options.cbm, "index_repository", {"repo_path": str(repo)})
    print(f"cold_index_seconds      {cold:8.2f}   nodes={result.get('nodes')} edges={result.get('edges')}")

    db = db_for(repo)
    if db is None:
        print("index database not found; cannot measure size or run queries")
        return
    print(f"index_size_bytes        {os.path.getsize(db):8d}")
    print(f"files_indexed           {counts(db)['file_hashes']:8d}")
    before = edge_set(db)

    conn = sqlite3.connect(db)
    _, latency = timed(lambda: [
        conn.execute(
            "SELECT t.name FROM edges e JOIN nodes s ON s.id=e.source_id "
            "JOIN nodes t ON t.id=e.target_id WHERE e.type='CALLS' AND s.name=?",
            ("create_student",),
        ).fetchall() for _ in range(50)
    ])
    conn.close()
    print(f"query_latency_ms        {latency / 50 * 1000:8.3f}   (direct sqlite)")

    if not options.touch:
        return
    target = repo / options.touch
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original + "\n# spike touch\n", encoding="utf-8")
        result, incremental = timed(cbm, options.cbm, "index_repository", {"repo_path": str(repo)})
        print(f"incremental_seconds     {incremental:8.2f}   nodes={result.get('nodes')} edges={result.get('edges')}")
    finally:
        target.write_text(original, encoding="utf-8")

    result, _ = timed(cbm, options.cbm, "index_repository", {"repo_path": str(repo)})
    after = edge_set(db)
    lost = sorted(before - after)
    print(f"edges_lost_after_roundtrip {len(lost)}")
    for edge in lost[:15]:
        print(f"  - {edge}")


if __name__ == "__main__":
    main()
