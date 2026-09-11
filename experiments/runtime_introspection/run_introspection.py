#!/usr/bin/env python3
"""Phase A: cross-check Sydes' own STATIC deterministic route composition
(`discover/route_index.py` + `discover/route_graph.py` +
`discover/route_entrypoints.py`) against the LIVE route table a real FastAPI
app resolves for itself at import time.

The point: rather than growing a rule engine to cover every syntax variant
of "register a route" (which explodes across frameworks/styles), ask a
framework that exposes introspection what it ACTUALLY resolved its own
routes to, and use any mismatch as a measured bug/gap in the static table --
not a hypothesis.

SAFETY, per explicit instruction for this round:
  - The worker subprocess (`_introspect_worker.py`) writes ONLY route
    metadata (path/methods/endpoint name) -- see its own docstring.
  - This script NEVER persists the worker's raw stdout/stderr to any file.
    It is captured in memory only, for local failure diagnosis, and is
    regex-scanned for secret-shaped substrings before any preview of it is
    ever printed.
  - Nothing here reads, prints, or serializes any environment variable or
    the target app's settings/config object.

Not wired into any production path. `main` is untouched; this only reads
the target fixture repo and Sydes' own discovery modules.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SYDES_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SYDES_ROOT / "src"))

from sydes.core.models import RepoRef  # noqa: E402
from sydes.discover.route_index import build_route_index_batch  # noqa: E402
from sydes.discover.route_graph import build_route_graph_facts_batch  # noqa: E402
from sydes.discover.route_entrypoints import entrypoints_from_route_graph  # noqa: E402

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")
REPO_NAME = "Kokoro-FastAPI"
TARGET_MODULE = "api.src.main"
TARGET_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# Any subprocess output preview is scanned against these before ever being
# displayed, and matches are replaced -- defense in depth on top of the
# worker already never touching settings/env in the first place.
_SECRET_LIKE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{10,}|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+|bearer\s+\S+)",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    return _SECRET_LIKE_RE.sub("[REDACTED]", text)


def run_static_baseline() -> list[dict]:
    repo = RepoRef(name=REPO_NAME, root=str(REPO_ROOT))
    route_index_batch = build_route_index_batch([repo])
    route_graph = build_route_graph_facts_batch([repo], route_index_batch=route_index_batch)
    entrypoints = entrypoints_from_route_graph(route_graph, [REPO_NAME])
    return entrypoints


def run_live_introspection() -> list[dict]:
    out_path = Path(__file__).parent / "_live_routes.json"
    if out_path.exists():
        out_path.unlink()
    worker = Path(__file__).parent / "_introspect_worker.py"
    # `api.src.main` needs REPO_ROOT itself on sys.path (matching this repo's
    # own container setup, which sets PYTHONPATH=/app:/app/api) -- nothing
    # else about the environment is deliberately changed.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [str(TARGET_VENV_PYTHON), str(worker), "--module", TARGET_MODULE, "--out", str(out_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    if proc.returncode != 0 or not out_path.is_file():
        preview = _redact((proc.stderr or "")[-1500:])
        raise RuntimeError(f"introspection worker failed (exit {proc.returncode}):\n{preview}")
    data = json.loads(out_path.read_text(encoding="utf-8"))
    out_path.unlink()  # ephemeral -- not left on disk beyond this run
    return data["routes"]


def _normalize_path(path: str) -> str:
    path = re.sub(r"\{[^}]+\}", "{param}", path)
    return path.rstrip("/") or "/"


def compare(static_entrypoints: list[dict], live_routes: list[dict]) -> dict:
    static_by_key: dict[tuple[str, str], list[dict]] = {}
    for entry in static_entrypoints:
        method = str(entry.get("route_method") or "").upper()
        path = _normalize_path(str(entry.get("route_path") or ""))
        static_by_key.setdefault((method, path), []).append(entry)

    live_by_key: dict[tuple[str, str], list[dict]] = {}
    for route in live_routes:
        path = _normalize_path(route["path"])
        for method in route["methods"] or ["GET"]:
            live_by_key.setdefault((method.upper(), path), []).append(route)

    matched = []
    static_only = []
    live_only = []

    for key, entries in static_by_key.items():
        if key in live_by_key:
            for entry in entries:
                matched.append({"key": key, "static_symbol": entry.get("symbol"), "static_file": entry.get("file")})
        else:
            for entry in entries:
                static_only.append({"key": key, "symbol": entry.get("symbol"), "file": entry.get("file")})

    for key, routes in live_by_key.items():
        if key not in static_by_key:
            for route in routes:
                live_only.append({"key": key, "endpoint": route.get("endpoint_qualname"), "module": route.get("endpoint_module")})

    return {
        "static_route_count": len(static_entrypoints),
        "live_route_count": sum(len(v) for v in live_by_key.values()),
        "matched_count": len(matched),
        "static_only_count": len(static_only),
        "live_only_count": len(live_only),
        "matched": matched,
        "static_only": static_only,  # static guessed a route the live app never registered
        "live_only": live_only,      # a real route the static table missed entirely
    }


def main() -> int:
    print(f"[1/3] Building static deterministic route composition for {REPO_NAME}...")
    static_entrypoints = run_static_baseline()
    print(f"    {len(static_entrypoints)} static route-derived entrypoints found")

    print(f"[2/3] Running live introspection worker under {TARGET_VENV_PYTHON}...")
    live_routes = run_live_introspection()
    print(f"    {len(live_routes)} live routes read back from app.routes")

    print("[3/3] Comparing...")
    result = compare(static_entrypoints, live_routes)

    out_path = Path(__file__).parent / "phase_a_comparison.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"\nmatched: {result['matched_count']}")
    print(f"static-only (static guessed, live never registered): {result['static_only_count']}")
    for item in result["static_only"]:
        print(f"  STATIC-ONLY {item['key']} -> {item['symbol']} ({item['file']})")
    print(f"live-only (real route, static table missed): {result['live_only_count']}")
    for item in result["live_only"]:
        print(f"  LIVE-ONLY {item['key']} -> {item['endpoint']} ({item['module']})")

    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
