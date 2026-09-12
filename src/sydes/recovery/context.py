"""Assembling what the recovery agent is shown before its first turn.

Almost everything here is read from the already-computed
`ChangeVerificationResult` and the diff it carries — no graph query, no
source read. The agent's own tool loop (see
`sydes.recovery.tools`/`agent`) is what does unrestricted repository
investigation; this module only frames the starting question.

One deliberate exception: `declarative_entrypoints`, a small, targeted,
deterministic scan (`sydes.recovery.entrypoint_heuristic`) of the files
right next to what changed. This exists because of a measured, specific
failure — discovery never once proposed the real HTTP entrypoint across 5
live TS runs, because that entrypoint's controller file was never itself
part of the diff (a route's controller is routinely a SIBLING of the
handler it dispatches to, not the changed file). Handing discovery this
one fact up front, instead of hoping its own bounded search stumbles onto
it, is what actually fixes that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sydes.recovery.entrypoint_heuristic import find_declarative_entrypoints, nearby_files
from sydes.recovery.schema import EntityRef
from sydes.recovery.trigger import RecoveryTrigger
from sydes.verify.models import ChangeVerificationResult


@dataclass(frozen=True)
class RecoveryContext:
    """Plain-text fields ready to drop into the recovery prompt. Kept as
    strings/tuples rather than passing the pydantic result through
    directly, so the prompt-building code in `agent.py` has no reason to
    reach back into `ChangeVerificationResult` internals itself."""

    reason_first_pass_stopped: str
    gap_kinds: tuple[str, ...]
    diff_summary: str
    changed_symbols: tuple[str, ...]
    current_result_summary: str
    cbm_fragments: str
    known_entrypoints: tuple[str, ...]
    test_candidates: tuple[str, ...]
    unresolved_gaps: tuple[str, ...]
    #: The exact repo-relative file paths this diff actually changed, taken
    #: directly from `ChangeVerificationResult.change.symbols`/`.files` —
    #: never a guess. This is the ground truth
    #: `sydes.recovery.verify`'s identity layer checks a path's
    #: `target_node` against: an entity whose resolved file is not in this
    #: set cannot be the actually-changed behavior, no matter how
    #: plausible its name looks.
    changed_files: tuple[str, ...] = ()
    #: `"GET findUsers (path/to/controller.ts:24) class_prefix=... path=..."`
    #: style hints from `sydes.recovery.entrypoint_heuristic` — a
    #: decorator-shape match, never a resolved/verified fact. `path` and
    #: `class_prefix` are best-effort and often symbolic (an unresolved
    #: routes-config constant, not a literal string) — never used for
    #: identity; only `file` + `symbol` are.
    declarative_entrypoints: tuple[str, ...] = ()
    #: Every route the run's own deterministic, language-agnostic route
    #: discovery found in this repo — not filtered to ones already tied to
    #: the diff (see `known_entrypoints` above for that narrower set).
    #: Opt-in (see `build_context`'s `include_repo_routes`): empty unless
    #: explicitly requested, so this is additive to discovery's prompt, not
    #: a default behavior change. A hint, exactly like
    #: `declarative_entrypoints` above — Stage B still has to prove the
    #: connection from any of these to the changed behavior; naming a route
    #: here is not by itself evidence of anything.
    repo_known_routes: tuple[str, ...] = ()
    #: Structured counterpart of `changed_symbols` above -- the diff's own
    #: ground-truth changed symbols as `EntityRef`s, `qualified_name` set
    #: to `ChangedSymbol.cbm_qualified_name` when the first pass already
    #: resolved it. Exists for `sydes.recovery.graph_path`, which needs a
    #: real entity to seed a graph query with, not a formatted prompt
    #: string; not itself shown in any prompt.
    changed_symbol_entities: tuple[EntityRef, ...] = ()
    #: Structured counterpart of `known_entrypoints` + `repo_known_routes`
    #: combined -- every entrypoint this run knows about, already-diff-tied
    #: or not, as `EntityRef`s a graph query can seed from. Same
    #: `sydes.recovery.graph_path`-only purpose as `changed_symbol_entities`.
    entrypoint_entities: tuple[EntityRef, ...] = ()


def _diff_summary(result: ChangeVerificationResult) -> str:
    lines: list[str] = []
    for f in result.change.files:
        hunks = ", ".join(f"L{h.start_line}-{h.end_line}" for h in f.hunks) or "(no hunk detail)"
        lines.append(f"- {f.change_type} {f.path} [{f.role or 'unknown'}] hunks: {hunks}")
    return "\n".join(lines) if lines else "(no changed files recorded)"


def _changed_symbols(result: ChangeVerificationResult) -> tuple[str, ...]:
    out: list[str] = []
    for symbol in result.change.symbols:
        qname = symbol.qualified_name or symbol.name
        out.append(f"{symbol.name} ({qname}) in {symbol.file}:{symbol.start_line or '?'}")
    return tuple(out)


def _current_result_summary(result: ChangeVerificationResult) -> str:
    counts = result.summary.counts
    return (
        f"verdict={result.summary.verdict} risk={result.summary.risk} "
        f"analysis_status={result.analysis_status} "
        f"affected_flows={counts.affected_flows} "
        f"mapped_tests={counts.mapped_tests} tests_executed={counts.tests_executed} "
        f"unresolved_changed_symbols={result.unresolved_changed_symbols}"
    )


def _cbm_fragments(result: ChangeVerificationResult) -> str:
    """Everything the first pass already found, compactly — the agent's
    starting hypothesis to verify, extend, or correct, never a boundary on
    what it may go look at."""
    lines: list[str] = []
    for flow in result.affected_flows:
        lines.append(
            f"AffectedFlow: {flow.entry_label} handler={flow.handler} "
            f"impact_status={flow.impact_status} analysis_status={flow.analysis_status} "
            f"changed_nodes={[n.symbol for n in flow.changed_nodes]}"
        )
    for boundary in result.affected_boundaries:
        lines.append(
            f"AffectedBoundary: kind={boundary.kind} symbol={boundary.symbol} "
            f"status={boundary.status} label={boundary.label!r} file={boundary.file} "
            f"distance={boundary.distance}"
        )
    for impact in result.accepted_impacts:
        lines.append(
            f"AcceptedImpact: {impact.label!r} status={impact.status} "
            f"route={impact.route_method or ''} {impact.route_path or ''} "
            f"changed_symbols={impact.changed_symbols}"
        )
    return "\n".join(lines) if lines else "(first pass found no flows, boundaries, or accepted impacts)"


def _known_entrypoints(result: ChangeVerificationResult) -> tuple[str, ...]:
    out: set[str] = set()
    for flow in result.affected_flows:
        out.add(flow.entry_label)
    for impact in result.accepted_impacts:
        if impact.route_method and impact.route_path:
            out.add(f"{impact.route_method} {impact.route_path}")
    return tuple(sorted(out))


def _changed_symbol_entities(result: ChangeVerificationResult) -> tuple[EntityRef, ...]:
    out: list[EntityRef] = []
    for symbol in result.change.symbols:
        if not symbol.file:
            continue
        out.append(EntityRef(
            symbol=symbol.qualified_name or symbol.name, file=symbol.file,
            qualified_name=symbol.cbm_qualified_name,
        ))
    return tuple(out)


def _entrypoint_entities(result: ChangeVerificationResult, known_routes: list) -> tuple[EntityRef, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[EntityRef] = []
    for flow in result.affected_flows:
        if not flow.handler:
            continue
        file = flow.artifact_refs.get("route_file") or flow.artifact_refs.get("handler_file") or ""
        key = (flow.handler, file)
        if not file or key in seen:
            continue
        seen.add(key)
        out.append(EntityRef(symbol=flow.handler, file=file))
    for route in known_routes:
        if not (route.handler and route.file):
            continue
        key = (route.handler, route.file)
        if key in seen:
            continue
        seen.add(key)
        out.append(EntityRef(symbol=route.handler, file=route.file))
    return tuple(out)


def _changed_files(result: ChangeVerificationResult) -> tuple[str, ...]:
    out: set[str] = set()
    for symbol in result.change.symbols:
        if symbol.file:
            out.add(symbol.file)
    for f in result.change.files:
        if f.path and f.role != "test_usage_candidate":
            out.add(f.path)
    return tuple(sorted(out))


def _declarative_entrypoints(repo_root: Path | None, changed_files: tuple[str, ...]) -> tuple[str, ...]:
    if repo_root is None or not changed_files:
        return ()
    try:
        files = nearby_files(repo_root, changed_files)
        found = find_declarative_entrypoints(repo_root, files)
    except OSError:
        return ()
    # Path/method lead, symbol trails: when a class overloads a method name
    # (two entries share `symbol`), what actually tells them apart is the
    # route each is decorated with -- leading with that keeps it the first
    # thing read, not a trailing detail after an apparently-duplicate name.
    return tuple(
        f"{ep.http_method} {ep.path!r} -> {ep.symbol} ({ep.file}:{ep.line}) class_prefix={ep.class_prefix!r}"
        for ep in found
    )


def _repo_known_routes(result: ChangeVerificationResult) -> tuple[str, ...]:
    """Every deterministically-discovered route this run found for the
    repo, formatted the same way `declarative_entrypoints` is — method,
    path, handler, file, nothing framework-specific. Only routes with a
    complete method/path/handler/file are shown; an incomplete candidate
    is not a usable anchor and would just add noise. Order and dedup by
    (method, path, handler, file) since discovery can legitimately surface
    the same route more than once across passes."""
    seen: set[tuple[str, str, str, str]] = set()
    out: list[str] = []
    for route in result.known_routes:
        if not (route.method and route.path and route.handler and route.file):
            continue
        key = (route.method, route.path, route.handler, route.file)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{route.method} {route.path} -> {route.handler} ({route.file})")
    return tuple(out)


def build_context(
    result: ChangeVerificationResult, trigger: RecoveryTrigger, *, repo_root: Path | None = None,
    include_repo_routes: bool = False,
) -> RecoveryContext:
    """`repo_root` is optional so callers that don't need
    `declarative_entrypoints` (or are constructing a context in a test)
    aren't forced to supply one — omitting it just leaves that field
    empty, never an error.

    `include_repo_routes` gates `repo_known_routes` (see that field's own
    docstring on `RecoveryContext`) — off by default, so this is additive
    to what discovery is shown, never a default behavior change."""
    changed_files = _changed_files(result)
    return RecoveryContext(
        reason_first_pass_stopped=trigger.reason,
        gap_kinds=trigger.gap_kinds,
        diff_summary=_diff_summary(result),
        changed_symbols=_changed_symbols(result),
        current_result_summary=_current_result_summary(result),
        cbm_fragments=_cbm_fragments(result),
        known_entrypoints=_known_entrypoints(result),
        test_candidates=trigger.extra_test_candidate_files,
        unresolved_gaps=tuple(result.analysis_notes),
        changed_files=changed_files,
        declarative_entrypoints=_declarative_entrypoints(repo_root, changed_files),
        repo_known_routes=_repo_known_routes(result) if include_repo_routes else (),
        changed_symbol_entities=_changed_symbol_entities(result),
        entrypoint_entities=_entrypoint_entities(result, result.known_routes),
    )
