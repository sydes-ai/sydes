"""Passive, opt-in evaluation tracing for `sydes verify-change`.

OBSERVABILITY ONLY. This module never influences analysis, never decides
anything, and never adds work Sydes would not otherwise do — it only writes
down, as structured JSON, what already happened: which CBM operations ran,
which LLM calls were made and with what prompts/responses, which impact
candidates were accepted or rejected and why, how obligations/tests resolved,
and the final risk/verdict/reasons Sydes already computed. No new provider
calls are made to gather this data, and nothing here can change a verdict,
a prompt, a budget, or an accepted/rejected impact.

Disabled by default. Set `SYDES_TRACE_DIR=/absolute/path` to an existing or
creatable directory to enable it; every writer in this module is a complete
no-op (checked fresh on every call, so it can be toggled between runs in the
same process, e.g. in tests) when that variable is unset. All writes are
best-effort: any failure while tracing (a bad path, a permissions error, an
unpicklable value) is swallowed here and never propagated — a broken trace
directory must never break a real analysis.

PRIVACY / SAFETY: trace output can contain full prompts, model responses,
and raw CBM payloads, which may embed source snippets from the repository
being analyzed. Enable `SYDES_TRACE_DIR` only in trusted, local evaluation
or debugging environments — never point it at a shared or public location.

Output shape written under `SYDES_TRACE_DIR`::

    run.json                    one JSON object: run metadata + final decision
    llm_calls.jsonl              one line per LLM call (request/response/latency)
    cbm_calls.jsonl               one line per CBM tool call (args/result summary)
    impact_decisions.jsonl        one line per impact candidate/finding decision
    verification_decisions.jsonl  one line per obligation/flow verification-modeling decision
    test_decisions.jsonl          one line per obligation's test-execution outcome
    graph_slices.jsonl            one line per bounded GraphSlice build (seeds/calls/caps/truncation)
    index_decisions.jsonl         one line per repository index-mode decision (fast vs. retried full)
    raw/llm/<call_id>.json        full prompt+response for one LLM call
    raw/cbm/<call_id>.json        full raw CBM response for one CBM call

A category with no activity in a given run simply has no file (or an empty
one, if it was truncated at run start and never appended to) — neither is
treated as an error by anything that reads these artifacts.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

TRACE_DIR_ENV_VAR = "SYDES_TRACE_DIR"

_RUN_JSON = "run.json"
_LLM_CALLS = "llm_calls.jsonl"
_CBM_CALLS = "cbm_calls.jsonl"
_IMPACT_DECISIONS = "impact_decisions.jsonl"
_VERIFICATION_DECISIONS = "verification_decisions.jsonl"
_TEST_DECISIONS = "test_decisions.jsonl"
_GRAPH_SLICES = "graph_slices.jsonl"
_INDEX_DECISIONS = "index_decisions.jsonl"

_JSONL_FILES = (
    _LLM_CALLS, _CBM_CALLS, _IMPACT_DECISIONS, _VERIFICATION_DECISIONS, _TEST_DECISIONS,
    _GRAPH_SLICES, _INDEX_DECISIONS,
)


def is_enabled() -> bool:
    """Whether tracing is currently on. Re-checked every call, on purpose:

    lets a single process (e.g. a test suite) toggle `SYDES_TRACE_DIR`
    between runs without any cached state to reset.
    """
    return bool(os.environ.get(TRACE_DIR_ENV_VAR, "").strip())


def _trace_dir() -> Path | None:
    raw = os.environ.get(TRACE_DIR_ENV_VAR, "").strip()
    return Path(raw).expanduser() if raw else None


def new_call_id(prefix: str = "") -> str:
    """A stable, unique id for one call/event. Safe to invoke even when
    tracing is disabled (it never touches the filesystem)."""
    token = uuid.uuid4().hex[:16]
    return f"{prefix}_{token}" if prefix else token


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


def _sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Best-effort conversion to something `json.dumps` can serialize.

    Never raises — anything it cannot recognize becomes its `repr()` rather
    than blocking the rest of the record from being written. Depth-limited so
    an unexpectedly self-referential or very deep object cannot hang tracing.
    """
    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(item, _depth=_depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            (key if isinstance(key, str) else str(key)): _sanitize(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _sanitize(to_dict(), _depth=_depth + 1)
        except Exception:  # noqa: BLE001 - tracing must never fail the caller
            pass
    model_dump = getattr(value, "model_dump", None)  # pydantic v2 BaseModel
    if callable(model_dump):
        try:
            return _sanitize(model_dump(mode="json"), _depth=_depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _sanitize(dataclasses.asdict(value), _depth=_depth + 1)
        except Exception:  # noqa: BLE001
            pass
    try:
        return json.loads(json.dumps(value))
    except Exception:  # noqa: BLE001
        return repr(value)


def _ensure_dirs(trace_dir: Path) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "raw" / "llm").mkdir(parents=True, exist_ok=True)
    (trace_dir / "raw" / "cbm").mkdir(parents=True, exist_ok=True)


def _append_jsonl(filename: str, record: dict[str, Any]) -> None:
    trace_dir = _trace_dir()
    if trace_dir is None:
        return
    try:
        _ensure_dirs(trace_dir)
        line = json.dumps(_sanitize(record), default=str)
        with (trace_dir / filename).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - best-effort, must never raise
        pass


def _write_raw(kind: str, call_id: str, payload: Any) -> str | None:
    """Write a full (potentially large) payload under `raw/<kind>/<call_id>.json`.

    Returns the path relative to the trace directory, or `None` if tracing is
    disabled or the write failed — callers reference this path from their
    compact JSONL record rather than inlining the payload there.
    """
    trace_dir = _trace_dir()
    if trace_dir is None:
        return None
    relative = f"raw/{kind}/{call_id}.json"
    try:
        _ensure_dirs(trace_dir)
        (trace_dir / relative).write_text(
            json.dumps(_sanitize(payload), default=str, indent=2), encoding="utf-8"
        )
        return relative
    except Exception:  # noqa: BLE001
        return None


def _write_json(filename: str, data: dict[str, Any]) -> None:
    trace_dir = _trace_dir()
    if trace_dir is None:
        return
    try:
        _ensure_dirs(trace_dir)
        (trace_dir / filename).write_text(
            json.dumps(_sanitize(data), default=str, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def _read_json(filename: str) -> dict[str, Any]:
    trace_dir = _trace_dir()
    if trace_dir is None:
        return {}
    try:
        text = (trace_dir / filename).read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def start_run(*, run_id: str, options: Any, repos: Any) -> None:
    """Mark the start of one traced `verify-change` run.

    A no-op when tracing is disabled. When enabled, truncates this run's
    JSONL files (a trace directory holds exactly one run's artifacts — reuse
    a fresh directory per run, as the eval harness already does per
    `--run-tag`) and writes the initial `run.json` with run metadata, so a
    directory shows a run started even if the process later crashes before
    `record_final_decision` runs.
    """
    if not is_enabled():
        return
    trace_dir = _trace_dir()
    if trace_dir is None:
        return
    try:
        _ensure_dirs(trace_dir)
        for filename in _JSONL_FILES:
            (trace_dir / filename).write_text("", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    _write_json(_RUN_JSON, {
        "run_id": run_id,
        "started_at": _now_iso(),
        "ended_at": None,
        "options": _sanitize(options),
        "repos": _sanitize(repos),
        "final_decision": None,
    })


def record_final_decision(*, run_id: str, risk: str, verdict: str, headline: str,
                           counts: Any, reasons: list[str]) -> None:
    """Serialize the final risk/verdict/counts/reasons Sydes already
    computed (`_compute_summary`). Adds no new decision logic — this is a
    read-only snapshot of values that already determined the report."""
    if not is_enabled():
        return
    existing = _read_json(_RUN_JSON)
    existing["run_id"] = existing.get("run_id") or run_id
    existing["ended_at"] = _now_iso()
    existing["final_decision"] = {
        "risk": risk,
        "verdict": verdict,
        "headline": headline,
        "counts": _sanitize(counts),
        "reasons": list(reasons),
    }
    _write_json(_RUN_JSON, existing)


def record_llm_call(
    *, call_id: str, stage: str, provider: str, model: str,
    request: Any, response_text: str | None, error: str | None,
    latency_ms: float, usage: dict[str, Any] | None = None,
) -> None:
    """One LLM call: stage/purpose, provider/model, the exact request sent,
    the raw response (or error), latency, and token/cost usage when the
    provider returned it (never fetched separately — no new provider call
    is ever made to obtain this)."""
    if not is_enabled():
        return
    raw_path = _write_raw("llm", call_id, {
        "request": _sanitize(request), "response_text": response_text, "error": error,
    })
    _append_jsonl(_LLM_CALLS, {
        "call_id": call_id,
        "timestamp": _now_iso(),
        "stage": stage,
        "provider": provider,
        "model": model,
        "latency_ms": round(latency_ms, 3),
        "success": error is None,
        "error": error,
        "usage": usage or {},
        "raw_path": raw_path,
    })


def record_cbm_call(
    *, call_id: str, operation: str, arguments: Any, duration_ms: float,
    success: bool, error: str | None, result_summary: Any, raw_response: Any,
) -> None:
    """One CBM tool call: operation/tool name, arguments, duration,
    success/error, a compact summary, plus the full raw response written to
    `raw/cbm/<call_id>.json` (never inlined into the JSONL line)."""
    if not is_enabled():
        return
    raw_path = _write_raw("cbm", call_id, raw_response)
    _append_jsonl(_CBM_CALLS, {
        "call_id": call_id,
        "timestamp": _now_iso(),
        "operation": operation,
        "arguments": _sanitize(arguments),
        "duration_ms": round(duration_ms, 3),
        "success": success,
        "error": error,
        "result_summary": _sanitize(result_summary),
        "raw_path": raw_path,
    })


def record_graph_slice(
    *, seed_symbols: list[str], graph_calls_used: int, node_count: int, edge_count: int,
    truncated: bool, truncation_reason: str | None, depth_reached: int,
    remote_calls_avoided: int | None = None,
) -> None:
    """One bounded `GraphSlice` build (`code_intelligence.graph_slice`): how
    many seeds it started from, how many CBM `query_graph` calls it actually
    spent, the resulting node/edge counts, whether a cap was hit, and — when
    computable — an estimate of the whole-repository-sweep calls this avoided.
    Purely descriptive: never influences the slice itself."""
    if not is_enabled():
        return
    _append_jsonl(_GRAPH_SLICES, {
        "timestamp": _now_iso(),
        "seed_symbols": _sanitize(seed_symbols),
        "graph_calls_used": graph_calls_used,
        "node_count": node_count,
        "edge_count": edge_count,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "depth_reached": depth_reached,
        "remote_calls_avoided": remote_calls_avoided,
    })


#: How many canonical seed identities to name in a trace record. Enough to
#: debug an identity mismatch, far short of dumping a symbol table.
_MAX_TRACED_SEEDS = 20


def record_seed_selection(
    *, changed_symbol_seeds: int, route_handler_seeds: int,
    deduplicated_seeds: int, dropped_auxiliary_seeds: int,
) -> None:
    """How the seed set for one bounded slice was chosen, before any
    canonicalization. Counts only: enough to tell a change-local seed set
    from a repository-wide one without listing hundreds of handlers."""
    if not is_enabled():
        return
    _append_jsonl(_GRAPH_SLICES, {
        "timestamp": _now_iso(),
        "event": "seed_selection",
        "changed_symbol_seed_count": changed_symbol_seeds,
        "route_handler_seed_count": route_handler_seeds,
        "deduplicated_seed_count": deduplicated_seeds,
        "dropped_auxiliary_seed_count": dropped_auxiliary_seeds,
        "final_requested_seed_count": deduplicated_seeds,
    })


def record_seed_resolution(
    *, requested: int, canonical: int, unresolved: list[str],
    ambiguous: dict[str, list[str]], canonical_seeds: list[str],
    unresolved_changed: list[str] | None = None,
    unresolved_auxiliary: list[str] | None = None,
) -> None:
    """How display seed names mapped onto CBM's canonical graph identities.

    The check that catches an identity mismatch: a run where `requested` is
    healthy but `canonical` is 0 explains a zero-edge slice, and the two are
    otherwise indistinguishable from the slice record alone."""
    if not is_enabled():
        return
    _append_jsonl(_GRAPH_SLICES, {
        "timestamp": _now_iso(),
        "event": "seed_resolution",
        "requested_seed_count": requested,
        "canonical_seed_count": canonical,
        "unresolved_seed_count": len(unresolved),
        "unresolved_changed_seed_count": len(unresolved_changed or []),
        "unresolved_auxiliary_seed_count": len(unresolved_auxiliary or []),
        "unresolved_seeds": _sanitize(unresolved[:_MAX_TRACED_SEEDS]),
        "ambiguous_seed_count": len(ambiguous),
        "canonical_seeds": _sanitize(canonical_seeds[:_MAX_TRACED_SEEDS]),
    })


def record_graph_slice_fallback(
    *, reason: str, seed_count: int, call_edges: int, usage_edges: int,
) -> None:
    """One fall-back from bounded slice retrieval to the repository-wide
    CALLS/USAGE sweep, with the failure that caused it. Written to the same
    `graph_slices.jsonl` stream so a reader sees slice activity and its
    fallbacks in one place."""
    if not is_enabled():
        return
    _append_jsonl(_GRAPH_SLICES, {
        "timestamp": _now_iso(),
        "event": "fallback_to_full_graph",
        "reason": reason,
        "seed_symbols": [],
        "seed_count": seed_count,
        "call_edges": call_edges,
        "usage_edges": usage_edges,
    })


def record_index_mode_decision(
    *, repo: str, initial_mode: str, retried: bool, retry_reason: str | None,
    excluded_dir_count: int, triggering_changed_files: list[str], decided_mode: str,
) -> None:
    """One repository's fast/full index-mode decision.

    CBM's `mode="fast"` index can exclude entire directories; when a changed
    file falls inside one, `CBMCodeIntelligence.build_or_update` retries that
    repository once with `mode="full"`. Recorded unconditionally (not only
    on retry) so an aggregate view can answer "how often does this happen"
    across many runs, not just spot it in the one run that hit it.
    `triggering_changed_files` is bounded to the files that actually caused
    the retry — inherently small, since a real change touches few files —
    never a dump of the exclusion or symbol tables.
    """
    if not is_enabled():
        return
    _append_jsonl(_INDEX_DECISIONS, {
        "timestamp": _now_iso(),
        "repo": repo,
        "initial_mode": initial_mode,
        "retried": retried,
        "retry_reason": retry_reason,
        "excluded_dir_count": excluded_dir_count,
        "triggering_changed_files": _sanitize(triggering_changed_files[:_MAX_TRACED_SEEDS]),
        "decided_mode": decided_mode,
    })


def record_impact_decision(
    *, changed_symbol: str, candidate_label: str, kind: str, source: str,
    status: str, accepted: bool, rejection_reason: str, corroborated: bool | None,
    confidence: float | None, reason: str, evidence: Any = None,
) -> None:
    """One impact candidate/finding decision: whether it came from
    deterministic structural discovery or the LLM guide, whether it was
    accepted, and — if not — exactly why. This is what makes it possible to
    reconstruct, for any single candidate, whether CBM found it structurally,
    the LLM proposed it, it was rejected, or an accepted one later turned out
    to be `unsupported_or_partial`, without inferring any of that from
    aggregate counters."""
    if not is_enabled():
        return
    _append_jsonl(_IMPACT_DECISIONS, {
        "timestamp": _now_iso(),
        "changed_symbol": changed_symbol,
        "candidate_label": candidate_label,
        "kind": kind,
        "source": source,
        "status": status,
        "accepted": accepted,
        "rejection_reason": rejection_reason,
        "corroborated": corroborated,
        "confidence": confidence,
        "reason": reason,
        "evidence": _sanitize(evidence),
    })


def record_verification_decision(
    *, impact_id: str, label: str, status: str, verification_model_status: str,
    reason: str, obligations: int = 0,
) -> None:
    """One verification-modeling decision for an accepted impact/flow: did it
    get modeled for verification, and if not, why not."""
    if not is_enabled():
        return
    _append_jsonl(_VERIFICATION_DECISIONS, {
        "timestamp": _now_iso(),
        "impact_id": impact_id,
        "label": label,
        "status": status,
        "verification_model_status": verification_model_status,
        "reason": reason,
        "obligations": obligations,
    })


def record_test_decision(
    *, flow_id: str, obligation_id: str, obligation_description: str,
    mapped_tests: list[str], supporting_tests: list[str],
    status: str, reason: str | None,
) -> None:
    """One obligation's test-mapping/execution outcome: which tests were
    mapped, which merely support the flow without asserting the behavior,
    and the pass/fail/unverified/unknown status with its exact reason."""
    if not is_enabled():
        return
    _append_jsonl(_TEST_DECISIONS, {
        "timestamp": _now_iso(),
        "flow_id": flow_id,
        "obligation_id": obligation_id,
        "obligation_description": obligation_description,
        "mapped_tests": list(mapped_tests),
        "supporting_tests": list(supporting_tests),
        "status": status,
        "reason": reason,
    })


class timer:
    """Tiny context manager: `with timer() as t: ...` then `t.ms` after exit."""

    def __enter__(self) -> "timer":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *_exc: object) -> None:
        self.ms = (time.perf_counter() - self._start) * 1000.0
