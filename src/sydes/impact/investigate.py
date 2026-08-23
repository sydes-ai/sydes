"""Carrying out one `InvestigationDecision` deterministically.

`InvestigationExecutor` is the only thing in M3 that touches the graph or a
repository file. The guide chooses an action and a target; the executor
resolves that target against facts the deterministic pass already surfaced
(never a name the guide invented), runs exactly the capability the action
names, and reports what it found as `InvestigationEvidence`. It never asks a
model anything and never decides whether a route is affected — that
decision, if one is ever made from a guided finding, happens back in
`ImpactInterpreter` and only from the evidence recorded here.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Protocol

from sydes.impact.models import (
    ACTION_FIND_DECORATOR_REFERENCES,
    ACTION_FIND_SIGNATURE_REFERENCES,
    ACTION_INSPECT_ENCLOSING_FUNCTION,
    ACTION_INSPECT_NEARBY_ENTRYPOINTS,
    ACTION_INSPECT_SOURCE_SPAN,
    ACTION_INSPECT_SYMBOL,
    ACTION_STOP_UNRESOLVED,
    ACTION_TRACE_CALLERS,
    ACTION_TRACE_USAGES,
    InvestigationDecision,
    InvestigationEvidence,
    RELATION_CALLS,
    RELATION_USAGE,
    SymbolIdentity,
)
from sydes.trace.function_body_slicer import slice_resolved_handler_body

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: CBM's entrypoint/edge facts carry no `language` field, so a source slice
#: needs a fallback. File extension is enough to tell an indentation-delimited
#: language from a brace-delimited one, which is all `slice_resolved_handler_body`
#: needs to find a body's end without a parser.
_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".go": "go", ".rb": "ruby", ".php": "php", ".cs": "csharp", ".kt": "kotlin",
}


def _language_for(file: str) -> str:
    return _LANGUAGE_BY_SUFFIX.get(Path(file).suffix.lower(), "unknown")

PROVENANCE_CBM_CALL_GRAPH = "cbm_call_graph"
PROVENANCE_CBM_USAGE_GRAPH = "cbm_usage_graph"
PROVENANCE_CBM_DECORATOR_TEXT = "cbm_decorator_text"
PROVENANCE_CBM_SIGNATURE_TEXT = "cbm_signature_text"
PROVENANCE_CBM_ENTRYPOINT_INDEX = "cbm_entrypoint_index"
PROVENANCE_SOURCE_INSPECTION = "source_inspection"
PROVENANCE_EXECUTOR_REJECTED = "executor_rejected"
PROVENANCE_GUIDE_STOPPED = "guide_stopped"


class _GraphIndex(Protocol):
    """The subset of `_FactIndex` the executor is allowed to call.

    A structural (not inheritance-based) contract on purpose: the executor
    depends on these lookups, not on `ImpactInterpreter`'s internals, so the
    two can be tested independently.
    """

    def inbound(self, identity: SymbolIdentity) -> list[tuple[str, SymbolIdentity, dict[str, Any]]]: ...
    def entrypoints_referencing(self, name: str) -> list[dict[str, Any]]: ...
    def entrypoints_with_signature_reference(self, name: str) -> list[dict[str, Any]]: ...

    entrypoints: list[dict[str, Any]]


def _symbol_dict_for(identity: SymbolIdentity, facts: Any) -> dict[str, Any] | None:
    """Resolve a bounded source span for an identity, without guessing.

    The shared symbol index (the same one `_FactIndex` was built from) is
    tried first: an exact (file, short_name) lookup, never a fuzzy one, and
    it carries a real `end_line`/`language` a brace-counting fallback cannot
    recover for an indentation-delimited language. More than one same-named
    candidate in that file is left unresolved rather than picked between. A
    call-edge-derived identity carries only its own declaration line, so that
    is the fallback when the symbol index has no matching entry (e.g. the
    identity came from a synthesized edge rather than a parsed file).
    """
    candidates = [
        item for item in facts.symbols_for_file(identity.repo, identity.file)
        if str(item.get("name") or "") == identity.short_name
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return None  # ambiguous: more than one same-named symbol in this file
    if identity.line is not None:
        return {
            "file": identity.file, "start_line": identity.line,
            "end_line": None, "language": _language_for(identity.file),
            "name": identity.short_name,
        }
    return None


def _locate_exact_line(
    repo_root: Path, file: str, line_start: int, line_end: int, needle: str,
) -> tuple[int, str] | None:
    """The first raw line in `[line_start, line_end]` naming `needle` as a
    whole identifier, with that line as its own excerpt.

    A lightweight re-read of the already-bounded region, not a new parser:
    the statement splitter normalizes whitespace and can merge many raw
    lines into one block, which is fine for identifier matching but wrong to
    cite as "the line" — this recovers the actual line so the reported
    excerpt always contains the match it claims to.
    """
    if line_start <= 0:
        return None
    try:
        text = (repo_root / file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    end = max(line_start, line_end) if line_end > 0 else line_start
    for line_no in range(line_start, min(end, len(lines)) + 1):
        raw = lines[line_no - 1]
        if needle in _IDENTIFIER_RE.findall(raw):
            return line_no, raw.strip()[:200]
    return None


class InvestigationExecutor:
    """Executes one `InvestigationDecision` against the loaded facts.

    `known` maps every name the current `ImpactQuestion` offered as a
    candidate (partial-path nodes, nearby entrypoints, the changed symbol
    itself) to its `SymbolIdentity`. A decision whose `target` is not in this
    map is rejected before anything is queried — the guide cannot cause a
    lookup on a name it invented.
    """

    def __init__(
        self, *, index: _GraphIndex, facts: Any, repo_root: Path | None,
    ) -> None:
        self._index = index
        self._facts = facts
        self._repo_root = repo_root

    def execute(
        self, decision: InvestigationDecision, *,
        known: dict[str, SymbolIdentity], origins: dict[str, SymbolIdentity],
    ) -> InvestigationEvidence:
        """`known` grounds `target` (any name/file the question surfaced);
        `origins` grounds `sought_symbol` (only the meaningful subset the
        question offered as `candidate_origins`) — deliberately two separate
        maps, so a source-confirming action cannot name a pseudo-node or an
        unrelated discovered name as the relationship it is checking."""
        action = decision.action
        if action == ACTION_STOP_UNRESOLVED:
            return InvestigationEvidence(
                action=action, target="", found=False, ambiguous=False,
                detail="guide chose to stop investigating this symbol",
                provenance=PROVENANCE_GUIDE_STOPPED,
            )
        if action == ACTION_INSPECT_NEARBY_ENTRYPOINTS:
            return self._inspect_nearby_entrypoints(decision, known=known)

        target_identity = known.get(decision.target)
        if target_identity is None:
            return InvestigationEvidence(
                action=action, target=decision.target, found=False, ambiguous=True,
                detail=(
                    f"target {decision.target!r} was not among the names this "
                    "question supplied; rejected without querying anything"
                ),
                provenance=PROVENANCE_EXECUTOR_REJECTED,
            )

        if action == ACTION_TRACE_CALLERS:
            return self._trace_relation(decision, target_identity, RELATION_CALLS, PROVENANCE_CBM_CALL_GRAPH)
        if action == ACTION_TRACE_USAGES:
            return self._trace_relation(decision, target_identity, RELATION_USAGE, PROVENANCE_CBM_USAGE_GRAPH)
        if action == ACTION_FIND_DECORATOR_REFERENCES:
            return self._find_referencing_entrypoints(
                decision, target_identity, self._index.entrypoints_referencing,
                PROVENANCE_CBM_DECORATOR_TEXT,
            )
        if action == ACTION_FIND_SIGNATURE_REFERENCES:
            return self._find_referencing_entrypoints(
                decision, target_identity, self._index.entrypoints_with_signature_reference,
                PROVENANCE_CBM_SIGNATURE_TEXT,
            )
        if action in (ACTION_INSPECT_SYMBOL, ACTION_INSPECT_ENCLOSING_FUNCTION, ACTION_INSPECT_SOURCE_SPAN):
            sought = origins.get(decision.sought_symbol)
            if sought is None:
                return InvestigationEvidence(
                    action=action, target=decision.target, found=False, ambiguous=True,
                    detail=(
                        f"sought_symbol {decision.sought_symbol!r} was not among "
                        "this question's candidate_origins; rejected without "
                        "querying anything"
                    ),
                    provenance=PROVENANCE_EXECUTOR_REJECTED,
                    sought_symbol=decision.sought_symbol,
                )
            return self._inspect_source(decision, target_identity, sought=sought)

        # `parse_guide_decision` already restricts `action` to
        # `INVESTIGATION_ACTIONS`, so reaching here means a caller constructed
        # a decision by hand with an action this executor has no handler for.
        raise ValueError(f"no executor handler for action {action!r}")

    # -- graph re-query actions --------------------------------------------

    def _trace_relation(
        self, decision: InvestigationDecision, target: SymbolIdentity,
        relation: str, provenance: str,
    ) -> InvestigationEvidence:
        edges = [item for item in self._index.inbound(target) if item[0] == relation]
        if not edges:
            return InvestigationEvidence(
                action=decision.action, target=decision.target, found=False, ambiguous=False,
                detail=f"no {relation} edge found in the graph for {decision.target!r}",
                provenance=provenance,
            )
        names = sorted({item[1].short_name for item in edges})
        return InvestigationEvidence(
            action=decision.action, target=decision.target, found=True, ambiguous=False,
            detail=f"graph reports {len(edges)} {relation} edge(s): {', '.join(names)}",
            provenance=provenance,
            file=next(iter(edges))[1].file,
        )

    def _find_referencing_entrypoints(
        self, decision: InvestigationDecision, target: SymbolIdentity,
        lookup: Any, provenance: str,
    ) -> InvestigationEvidence:
        matches = lookup(target.short_name)
        if not matches:
            return InvestigationEvidence(
                action=decision.action, target=decision.target, found=False, ambiguous=False,
                detail=f"no entrypoint text references {target.short_name!r}",
                provenance=provenance,
            )
        names = sorted(str(item.get("symbol") or "") for item in matches)
        return InvestigationEvidence(
            action=decision.action, target=decision.target, found=True, ambiguous=False,
            detail=f"{len(matches)} entrypoint(s) reference it: {', '.join(names)}",
            provenance=provenance,
        )

    def _inspect_nearby_entrypoints(
        self, decision: InvestigationDecision, *, known: dict[str, SymbolIdentity],
    ) -> InvestigationEvidence:
        # Restricted to a file already surfaced by the question (the changed
        # symbol's own file, or a dead-end node's file) — never an arbitrary
        # path the guide names.
        known_files = {identity.file for identity in known.values() if identity.file}
        target_file = decision.target or (next(iter(known_files), "") if known_files else "")
        if target_file not in known_files:
            return InvestigationEvidence(
                action=decision.action, target=target_file, found=False, ambiguous=True,
                detail=f"file {target_file!r} was not among this question's known files",
                provenance=PROVENANCE_EXECUTOR_REJECTED,
            )
        nearby = [entry for entry in self._index.entrypoints if entry.get("file") == target_file]
        for entry in nearby:
            identity = SymbolIdentity.from_fields(
                repo=str(entry.get("repo") or ""), file=str(entry.get("file") or ""),
                qualified_name=entry.get("qualified_name"), short_name=entry.get("symbol"),
                line=entry.get("line"),
            )
            known.setdefault(identity.short_name, identity)
        if not nearby:
            return InvestigationEvidence(
                action=decision.action, target=target_file, found=False, ambiguous=False,
                detail=f"no known entrypoints are declared in {target_file!r}",
                provenance=PROVENANCE_CBM_ENTRYPOINT_INDEX,
            )
        names = sorted(str(entry.get("symbol") or "") for entry in nearby)
        return InvestigationEvidence(
            action=decision.action, target=target_file, found=True, ambiguous=False,
            detail=f"{len(nearby)} entrypoint(s) in {target_file!r}: {', '.join(names)}",
            provenance=PROVENANCE_CBM_ENTRYPOINT_INDEX, file=target_file,
        )

    # -- source inspection actions ------------------------------------------

    def _inspect_source(
        self, decision: InvestigationDecision, target: SymbolIdentity,
        *, sought: SymbolIdentity,
    ) -> InvestigationEvidence:
        if self._repo_root is None:
            return InvestigationEvidence(
                action=decision.action, target=decision.target, found=False, ambiguous=False,
                detail="no repository root available to read source from",
                provenance=PROVENANCE_SOURCE_INSPECTION, sought_symbol=decision.sought_symbol,
            )
        span = _symbol_dict_for(target, self._facts)
        if span is None:
            return InvestigationEvidence(
                action=decision.action, target=decision.target, found=False, ambiguous=True,
                detail=f"no unambiguous declaration span found for {decision.target!r}",
                provenance=PROVENANCE_SOURCE_INSPECTION, sought_symbol=decision.sought_symbol,
            )
        sliced = slice_resolved_handler_body(
            repo_root=self._repo_root, handler_name=decision.target, symbol=span,
            language=span.get("language"),
        )
        if sliced is None:
            return InvestigationEvidence(
                action=decision.action, target=decision.target, found=False, ambiguous=False,
                detail=f"could not slice a body for {decision.target!r} in {span.get('file')}",
                provenance=PROVENANCE_SOURCE_INSPECTION, sought_symbol=decision.sought_symbol,
                file=str(span.get("file") or ""),
            )
        file = str(span.get("file") or "")
        for statement in sliced.get("statements", []):
            text = str(statement.get("text") or "")
            if sought.short_name not in _IDENTIFIER_RE.findall(text):
                continue
            # The statement splitter can merge a very large block into one
            # normalized, whitespace-collapsed "statement" spanning hundreds
            # of raw lines — citing its `line_start` would point at whatever
            # happens to open that block, not at the actual reference. A
            # direct scan of the raw lines in this statement's own span finds
            # the real line, so the reported evidence always contains what it
            # claims to.
            located = _locate_exact_line(
                self._repo_root, file,
                int(statement.get("line_start") or 0), int(statement.get("line_end") or 0),
                sought.short_name,
            )
            exact_line, excerpt = located if located is not None else (
                statement.get("line_start"), text[:200],
            )
            return InvestigationEvidence(
                action=decision.action, target=decision.target, found=True, ambiguous=False,
                detail=(
                    f"source of {decision.target!r} references {sought.short_name!r} "
                    f"at line {exact_line}"
                ),
                provenance=PROVENANCE_SOURCE_INSPECTION, sought_symbol=decision.sought_symbol,
                file=file, line=exact_line, matched_text=excerpt,
            )
        return InvestigationEvidence(
            action=decision.action, target=decision.target, found=False, ambiguous=False,
            detail=f"source of {decision.target!r} does not reference {sought.short_name!r}",
            provenance=PROVENANCE_SOURCE_INSPECTION, sought_symbol=decision.sought_symbol, file=file,
        )
