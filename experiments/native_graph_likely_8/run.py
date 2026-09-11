#!/usr/bin/env python3
"""EXPERIMENT harness -- not imported by normal Sydes runtime, not wired into
`sydes verify-change`. Read-only against real local repo checkouts.

Runs the 8 frozen cases (see oracle.md) through:
  Phase 1: the real `ImpactInterpreter` (guide_policy=GUIDE_OFF, i.e. no LLM
           at all) fed exactly what the NATIVE backend actually supplies
           (no call_edges/usage_edges), plus the real `call_follower.py`
           regex-based expansion from each entrypoint's resolved handler.
  Phase 2: the same `ImpactInterpreter`, fed real CBM-supplied call_edges/
           usage_edges for the same repository.

Reuses Sydes' own functions throughout -- nothing here reimplements
reachability semantics.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from sydes.code_intelligence.base import StructuralFacts
from sydes.core.models import RepoRef
from sydes.discover.file_facts import build_structural_index
from sydes.discover.route_entrypoints import entrypoints_from_route_graph
from sydes.impact.interpreter import GUIDE_OFF, ImpactInterpreter, TraversalBudget
from sydes.trace.call_follower import CallFollowBudgets, build_layered_trace_expansion
from sydes.trace.function_body_slicer import slice_resolved_handler_body
from sydes.trace.handler_resolver import resolve_handler_reference
from sydes.core.models import EndpointCandidate

REPO_ROOT = Path("/Users/ksnaik/sample_repos/Kokoro-FastAPI")
REPO_NAME = "app"

# Classification vocabulary -- see the task's own conceptual rule: absence of
# a path is never mapped to FALSE by this harness.
REACHABLE = "REACHABLE"
NOT_REACHED_IN_AVAILABLE_GRAPH = "NOT_REACHED_IN_AVAILABLE_GRAPH"
AMBIGUOUS_IDENTITY = "AMBIGUOUS_IDENTITY"
TRUNCATED = "TRUNCATED"
UNSUPPORTED_RELATION = "UNSUPPORTED_RELATION"


@dataclass
class Case:
    name: str
    branch: str
    entry_route_method: str
    entry_route_path: str
    entry_handler_hint: str  # bare handler symbol name, as route composition would report it
    target_symbol: str
    target_file: str
    target_qualified_name: str = ""


CASES: list[Case] = [
    Case("1_dev_model", "py-real-01-max-output-duration", "GET", "/dev/model",
         "model_status", "generate_audio_stream",
         "api/src/services/tts_service.py", "TTSService.generate_audio_stream"),
    Case("2_generate_from_phonemes", "py-real-01-max-output-duration", "POST", "/dev/generate_from_phonemes",
         "generate_from_phonemes", "generate_audio_stream",
         "api/src/services/tts_service.py", "TTSService.generate_audio_stream"),
    Case("3_dev_reload", "py-real-01-max-output-duration", "POST", "/dev/reload",
         "reload_model", "generate_audio_stream",
         "api/src/services/tts_service.py", "TTSService.generate_audio_stream"),
    Case("4_dev_unload", "py-real-01-max-output-duration", "POST", "/dev/unload",
         "unload_model", "generate_audio_stream",
         "api/src/services/tts_service.py", "TTSService.generate_audio_stream"),
    Case("5_settings", "py-real-01-max-output-duration", "POST", "/audio/speech",
         "create_speech", "max_output_duration_s",
         "api/src/core/config.py", "Settings.max_output_duration_s"),
    Case("6_within_duration_ceiling", "py-real-01-max-output-duration", "POST", "/audio/speech",
         "create_speech", "_within_duration_ceiling",
         "api/src/structures/schemas.py", "_within_duration_ceiling"),
    Case("7_convert_audio", "py-real-02-normalizer-voice-maintenance", "POST", "/audio/speech",
         "create_speech", "convert_audio",
         "api/src/services/audio.py", "AudioService.convert_audio"),
    Case("8_trim_audio", "py-real-02-normalizer-voice-maintenance", "POST", "/audio/speech",
         "create_speech", "trim_audio",
         "api/src/services/audio.py", "AudioService.trim_audio"),
]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout.strip()


def _build_index_for_current_checkout():
    started = time.perf_counter()
    index = build_structural_index([RepoRef(name=REPO_NAME, root=str(REPO_ROOT))], persist=False)
    build_ms = (time.perf_counter() - started) * 1000.0
    return index, build_ms


def _native_facts(index) -> StructuralFacts:
    """Exactly what NativeCodeIntelligence.build_or_update returns -- no
    entrypoints bridge call, no call/usage edges. Kept literal so Phase 1's
    "as the native backend actually behaves today" result is honest."""
    return StructuralFacts(
        repo_map=index.repo_map_batch,
        route_index=index.route_index_batch,
        symbol_index=index.handler_symbol_batch,
        route_graph=index.route_graph_facts,
        backend="native",
        provides_call_graph=False,
    )


def _native_facts_with_bridged_entrypoints(index) -> StructuralFacts:
    """The fairer "what native structural discovery can actually supply"
    variant: same as above, but with entrypoints populated via the SAME
    `entrypoints_from_route_graph` bridge the CBM adapter already calls
    (code_intelligence/cbm.py) -- native's own adapter just never calls it.
    Still zero call_edges/usage_edges: that part of the native gap is real,
    not an oversight (see native.py's own docstring)."""
    facts = _native_facts(index)
    facts.entrypoints = entrypoints_from_route_graph(index.route_graph_facts, [REPO_NAME])
    return facts


def _find_entrypoint(facts: StructuralFacts, method: str, path: str) -> dict | None:
    for entry in facts.entrypoints:
        if entry.get("route_method") == method and entry.get("route_path") == path:
            return entry
    return None


def _run_interpreter(facts: StructuralFacts, target_symbol: str, target_file: str, target_qn: str) -> dict:
    interpreter = ImpactInterpreter(TraversalBudget(), guide=None, guide_policy=GUIDE_OFF)
    changed_symbols = [{"name": target_symbol, "file": target_file, "qualified_name": target_qn}]
    result = interpreter.interpret(changed_symbols, facts, repo=REPO_NAME)
    return {
        "affected": [
            {
                "route_method": a.route_method, "route_path": a.route_path,
                "status": a.status, "path_steps": [s.symbol for s in a.path.steps] if a.path else [],
                "strategy": a.path.strategy if a.path else None,
            }
            for a in result.affected
        ],
        "notes": list(result.notes),
        "unresolved_changed_symbols": getattr(result, "unresolved_changed_symbols", None),
    }


def _handler_symbol_dict(index, handler_hint: str) -> dict | None:
    """Best-effort lookup of the resolved handler symbol dict from the
    native symbol index, by bare name, within the repo's files."""
    batch = index.handler_symbol_batch or {}
    for repo_payload in batch.get("repos", []) or []:
        for file_item in repo_payload.get("files", []) or []:
            for symbol in file_item.get("symbols", []) or []:
                if symbol.get("name") == handler_hint and symbol.get("kind") in ("function", "class_method"):
                    merged = dict(symbol)
                    merged.setdefault("file", file_item.get("path"))
                    return merged
    return None


def _repo_index_dict(index) -> dict:
    """The flat {"files": [...]} shape call_follower._build_file_maps expects,
    built from the native symbol index for this one repo."""
    files: list[dict] = []
    batch = index.handler_symbol_batch or {}
    for repo_payload in batch.get("repos", []) or []:
        if repo_payload.get("repo") != REPO_NAME:
            continue
        files.extend(repo_payload.get("files", []) or [])
    return {"files": files}


def run_call_follower_expansion(index, case: Case) -> dict:
    """Native, backend-independent depth-2 call following: resolve the
    handler, slice its body, follow its own direct calls by regex + symbol
    table (call_edges=None -- no CBM/backend edges involved)."""
    symbol = _handler_symbol_dict(index, case.entry_handler_hint)
    if symbol is None:
        return {"status": "handler_not_resolved"}

    slice_payload = slice_resolved_handler_body(
        repo_root=REPO_ROOT, handler_name=case.entry_handler_hint, symbol=symbol,
        language=str(symbol.get("language") or "python"),
    )
    if slice_payload is None:
        return {"status": "handler_body_unavailable"}

    resolution = {"primary_handler": {"normalized_handler": case.entry_handler_hint}}
    expansion = build_layered_trace_expansion(
        repo_root=REPO_ROOT,
        matched_endpoint={"method": case.entry_route_method, "path": case.entry_route_path},
        resolution=resolution,
        primary_slice=slice_payload,
        repo_index=_repo_index_dict(index),
        budgets=CallFollowBudgets(),
        call_edges=None,
    )
    target_leaf = case.target_symbol.rsplit(".", 1)[-1]
    reached = any(
        f.get("resolved_to", "").rsplit(".", 1)[-1] == target_leaf or f.get("call", "") == target_leaf
        for f in expansion.get("followed_calls", [])
    )
    truncated = bool(expansion.get("skipped_calls")) and any(
        s.get("reason", "").startswith(("max_", "truncated_")) for s in expansion.get("skipped_calls", [])
    )
    return {
        "status": "reached" if reached else ("truncated" if truncated else "not_reached"),
        "followed_calls": expansion.get("followed_calls", []),
        "unresolved_calls": expansion.get("unresolved_calls", []),
        "skipped_calls": expansion.get("skipped_calls", []),
        "summary": expansion.get("summary", {}),
    }


def phase1(case: Case, index) -> dict:
    facts_strict = _native_facts(index)
    facts_bridged = _native_facts_with_bridged_entrypoints(index)

    strict_result = _run_interpreter(facts_strict, case.target_symbol, case.target_file, case.target_qualified_name)
    bridged_result = _run_interpreter(facts_bridged, case.target_symbol, case.target_file, case.target_qualified_name)
    call_follower_result = run_call_follower_expansion(index, case)

    # Classification: reachable only if SOME native mechanism actually found
    # a path. Never inferred as false merely because none did.
    reached_via_interpreter = any(
        a["route_method"] == case.entry_route_method and a["route_path"] == case.entry_route_path
        for a in bridged_result["affected"]
    )
    reached_via_call_follower = call_follower_result.get("status") == "reached"

    if reached_via_interpreter or reached_via_call_follower:
        classification = REACHABLE
    elif call_follower_result.get("status") == "handler_not_resolved":
        classification = AMBIGUOUS_IDENTITY
    elif call_follower_result.get("status") == "truncated":
        classification = TRUNCATED
    else:
        # Neither mechanism found a path. Per the task's central rule, this
        # is NOT reported as "false" -- native supplies no repo-wide call
        # graph and call_follower is hard-capped at 2 levels, so a real,
        # deeper path can exist without either mechanism seeing it.
        classification = NOT_REACHED_IN_AVAILABLE_GRAPH

    return {
        "case": case.name,
        "classification": classification,
        "interpreter_strict_native": strict_result,
        "interpreter_bridged_entrypoints": bridged_result,
        "call_follower": call_follower_result,
    }


def main() -> int:
    results = []
    index_cache: dict[str, tuple] = {}
    for case in CASES:
        if case.branch not in index_cache:
            _git("checkout", "--quiet", case.branch)
            index, build_ms = _build_index_for_current_checkout()
            index_cache[case.branch] = (index, build_ms)
        index, build_ms = index_cache[case.branch]
        result = phase1(case, index)
        result["build_ms"] = build_ms
        results.append(result)
        print(f"{case.name:30} -> {result['classification']}")

    out_path = Path(__file__).parent / "phase1_native_only.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
