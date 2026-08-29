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
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    ENTRYPOINT_UNKNOWN,
    ImpactCandidate,
    InvestigationDecision,
    InvestigationEvidence,
    RELATION_CALLS,
    RELATION_USAGE,
    SymbolIdentity,
)
from sydes.trace.function_body_slicer import slice_resolved_handler_body
from sydes.verify.symbol_attribution_span import (
    language_for_attribution,
    symbol_attribution_span,
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: A candidate's `entrypoint_label` is free text, but when it looks like an
#: HTTP route ("GET /cases") it is worth recognising as one — corroboration
#: (and later route reconciliation) can then match it by method+path exactly
#: like a deterministically discovered route.
_ROUTE_LABEL_RE = re.compile(
    r"^\s*(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+(/\S*)\s*$", re.IGNORECASE,
)


def parse_route_label(label: str) -> tuple[str, str] | None:
    """`"GET /cases"` -> `("GET", "/cases")`; anything else -> `None`."""
    match = _ROUTE_LABEL_RE.match(label)
    if not match:
        return None
    return match.group(1).upper(), match.group(2)

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


#: How much of a changed symbol's own source to show the guide up front — a
#: handful of statements, not the whole function. Keeps the question small
#: while still giving the model *some* real code to reason from.
_PREVIEW_MAX_STATEMENTS = 6
_PREVIEW_MAX_CHARS = 400
#: Raw source lines kept on each side of a changed line, when change
#: positions are known. Small on purpose: enough to read the change in
#: context, never enough to re-show the symbol.
_PREVIEW_CONTEXT_LINES = 3
#: Upper bound on raw lines emitted for the diff-aware path, before the
#: character cap. Keeps several changed regions representable without any
#: one of them expanding the prompt.
_PREVIEW_MAX_LINES = 14
#: Marks a preview that does not begin at the symbol's first line, so a
#: reader (and the model) can tell selected evidence from a head-of-body
#: read. Language-independent by construction.
_PREVIEW_ELISION = "..."


def _normalized_ranges(raw: Any) -> list[tuple[int, int]]:
    """`[(start, end), ...]` of positive, ordered line ranges, or `[]`.

    Tolerant of the shapes a caller may already hold (tuples, lists, or
    `Hunk`-like objects with `start_line`/`end_line`) so this stage never
    forces a new model on the change layer.
    """
    ranges: list[tuple[int, int]] = []
    for item in raw or ():
        start = end = None
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            start, end = item[0], item[1]
        else:
            start = getattr(item, "start_line", None)
            end = getattr(item, "end_line", None)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start <= 0 or end <= 0:
            continue
        ranges.append((min(start, end), max(start, end)))
    return sorted(set(ranges))


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse overlapping/adjacent line windows, ascending."""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(windows):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def source_preview(
    identity: SymbolIdentity,
    facts: Any,
    repo_root: Path | None,
    *,
    changed_line_ranges: Any = None,
) -> str:
    """A short, bounded preview of one symbol's source, or "".

    Used only to seed `ImpactQuestion.source_context` with concrete code
    before the guide's first turn — not a general-purpose reader.

    `changed_line_ranges` (the diff hunks covering this symbol's file) makes
    the preview show *the region that actually changed* rather than the
    opening of the symbol. That distinction is not cosmetic: on a large
    symbol the change can sit far past the declaration, and an opening-of-
    body preview then presents unchanged neighbouring code as if it were the
    change — evidence a model will faithfully, and wrongly, reason from.
    Observed doing exactly that on a real PR whose only change was ~19 lines
    into a 64-line handler.

    The diff-aware path reads raw lines rather than the statement slicer's
    output, deliberately: the slicer normalizes and can merge tens of raw
    lines into a single "statement" (a Go `if` block became one 19-line
    statement in the case above), which is fine for identifier matching but
    has too little resolution to isolate a change. Raw lines also make the
    attached-declaration-metadata case fall out for free — a changed
    decorator or attribute above the declaration is simply a changed line
    inside the symbol's attribution span, needing no separate handling.

    Falls back to exactly the previous behavior — the first
    `_PREVIEW_MAX_STATEMENTS` sliced statements — when no usable range
    information is supplied, or when the supplied ranges do not touch this
    symbol at all. Bounded in every path: never more than
    `_PREVIEW_MAX_LINES` lines or `_PREVIEW_MAX_CHARS` characters.
    """
    if repo_root is None:
        return ""
    span = _symbol_dict_for(identity, facts)
    if span is None:
        return ""

    def _head_of_body() -> str:
        sliced = slice_resolved_handler_body(
            repo_root=repo_root, handler_name=identity.short_name, symbol=span,
            language=span.get("language"),
        )
        if sliced is None:
            return ""
        texts = [
            str(s.get("text") or "")
            for s in sliced.get("statements", [])[:_PREVIEW_MAX_STATEMENTS]
        ]
        return " ".join(text for text in texts if text)[:_PREVIEW_MAX_CHARS]

    ranges = _normalized_ranges(changed_line_ranges)
    if not ranges:
        return _head_of_body()
    changed_preview = changed_region_source(span, ranges, repo_root=repo_root)
    return changed_preview if changed_preview else _head_of_body()


def changed_region_source(
    span: dict[str, Any],
    ranges: list[tuple[int, int]],
    *,
    repo_root: Path,
    context_lines: int = _PREVIEW_CONTEXT_LINES,
    max_lines: int = _PREVIEW_MAX_LINES,
    max_chars: int = _PREVIEW_MAX_CHARS,
) -> str:
    """Raw source around every changed line inside this symbol, or "".

    Public because two independent branches need the same answer to "which
    source does this change actually touch": the impact guide's preview and
    the code-review context builder. The budgets are parameters rather than
    constants so a caller may show more per symbol without either branch
    re-implementing the selection — there must be exactly one definition of
    a changed region, not one per consumer. Defaults reproduce the impact
    guide's original bounds exactly.

    Returns "" (so the caller falls back) whenever the symbol's own bounds
    are unknown or the supplied ranges touch none of its lines — the
    per-file hunks legitimately cover sibling symbols too, and presenting an
    arbitrary window would be worse than the head-of-body default.

    The symbol's lower bound is `symbol_attribution_span`, the same function
    that decided this symbol counts as changed at all, so attached
    declaration metadata (a decorator, a Rust outer attribute) is inside the
    window and an unrelated change merely *somewhere* above the declaration
    is not. One definition of "belongs to this symbol", reused rather than
    re-guessed.
    """
    file = str(span.get("file") or "")
    declaration_line = span.get("start_line")
    if not file or not isinstance(declaration_line, int) or declaration_line <= 0:
        return ""
    try:
        lines = (repo_root / file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    attribution_start, _ = symbol_attribution_span(
        start_line=declaration_line, end_line=span.get("end_line"),
        file_lines=lines, language=language_for_attribution(file),
    )
    low_bound = attribution_start if isinstance(attribution_start, int) else declaration_line
    end_line = span.get("end_line")
    high_bound = end_line if isinstance(end_line, int) and end_line >= declaration_line else len(lines)
    high_bound = min(high_bound, len(lines))
    if low_bound > high_bound:
        return ""

    windows: list[tuple[int, int]] = []
    for low, high in ranges:
        start = max(low, low_bound)
        stop = min(high, high_bound)
        if start > stop:
            continue  # this hunk belongs to a different symbol in the file
        windows.append((
            max(start - context_lines, low_bound),
            min(stop + context_lines, high_bound),
        ))
    if not windows:
        return ""

    merged = _merge_windows(windows)
    parts: list[str] = []
    emitted = 0
    for index, (low, high) in enumerate(merged):
        if emitted >= max_lines:
            break
        if index == 0 and low > low_bound:
            parts.append(_PREVIEW_ELISION)
        elif index > 0:
            parts.append(_PREVIEW_ELISION)
        for number in range(low, high + 1):
            if emitted >= max_lines:
                break
            text = lines[number - 1].strip()
            if text:
                parts.append(text)
                emitted += 1
    if emitted == 0:
        return ""
    return " ".join(parts)[:max_chars]


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

    # -- semantic-candidate corroboration ------------------------------------

    def reaches_from_changed(self, candidate_symbol: str, changed_names: frozenset[str]) -> bool:
        """Whether any already-loaded call or usage edge connects one of this
        PR's changed symbols to `candidate_symbol`, by bare short name — the
        same identity granularity `ImpactCandidate.entrypoint_symbol` is ever
        offered at (see its own docstring: the guide is shown bare short
        names only, never a qualified name or file).

        Scans `self._facts.call_edges`/`usage_edges` — already loaded for
        this run by the deterministic pass, the same lists `_FactIndex`
        itself was built from — so this adds no new query, no new traversal,
        and no new source read. A real edge from a changed symbol is
        structural corroboration in its own right, distinct from (and
        reused alongside) `corroborate_candidates`'s narrower route/entrypoint
        match.
        """
        if not candidate_symbol or not changed_names:
            return False
        for edge in self._facts.call_edges:
            if (
                str(edge.get("caller_symbol") or "") in changed_names
                and str(edge.get("callee_symbol") or "") == candidate_symbol
            ):
                return True
        for edge in self._facts.usage_edges:
            if (
                str(edge.get("user_symbol") or "") in changed_names
                and str(edge.get("used_symbol") or "") == candidate_symbol
            ):
                return True
        return False

    def corroborate_candidates(
        self, candidates: tuple[ImpactCandidate, ...],
    ) -> list[dict[str, Any]]:
        """Cheap corroboration for each `ACTION_INFER_IMPACT` candidate.

        Deliberately not a search: every candidate is checked only against
        `self._index.entrypoints`, the same already-loaded, already-known
        entrypoint list every other action in this module reads from — no
        new traversal, no new source read, no new query. Corroboration
        raises confidence in a claim; it never manufactures the deterministic
        path `IMPACT_STATUS_PROVEN` requires, and an unmatched candidate is
        still returned (not dropped) so the caller can record it as an
        uncorroborated inference.

        One result dict per candidate, in the same order, each carrying
        `corroborated`, `detail`, and the best-known `route_method`/
        `route_path`/`symbol`/`qualified_name`/`file`/`kind` for building an
        `AffectedEntrypoint` from it.
        """
        return [self._corroborate_one(candidate) for candidate in candidates]

    def _corroborate_one(self, candidate: ImpactCandidate) -> dict[str, Any]:
        """Corroborate by the strongest identity actually available.

        `ImpactCandidate` never carries a qualified name, repo, or file —
        the guide contract only ever shows the model bare short symbol
        names (`ImpactQuestion.candidate_entrypoints`/`known_entrypoints`
        are built from `SymbolIdentity.short_name`), so those stronger
        tiers cannot be checked here; a route's exact method+path is the
        strongest signal this function can actually see, so it is tried
        first. A bare symbol name is the weakest identity available — a
        short name like `update` or `handler` can legitimately collide
        across a repository — so it is only ever accepted when it names
        exactly one known entrypoint. More than one match is ambiguity, not
        corroboration: reported as such, never resolved by picking the
        first result, and never allowed to make a hallucinated
        `entrypoint_symbol` look stronger merely because an unrelated
        same-named symbol happens to exist elsewhere in the repository.
        """
        parsed_route = parse_route_label(candidate.entrypoint_label)

        match: dict[str, Any] | None = None
        if parsed_route:
            method, path = parsed_route
            match = next(
                (
                    e for e in self._index.entrypoints
                    if (e.get("route_method") or "").upper() == method
                    and (e.get("route_path") or "") == path
                ),
                None,
            )

        ambiguous_symbol_count = 0
        if match is None and candidate.entrypoint_symbol:
            symbol_matches = [
                e for e in self._index.entrypoints if e.get("symbol") == candidate.entrypoint_symbol
            ]
            if len(symbol_matches) == 1:
                match = symbol_matches[0]
            elif len(symbol_matches) > 1:
                ambiguous_symbol_count = len(symbol_matches)

        if match is not None:
            route_method = match.get("route_method") or (parsed_route[0] if parsed_route else None)
            route_path = match.get("route_path") or (parsed_route[1] if parsed_route else None)
            return {
                "corroborated": True,
                "detail": (
                    f"matches known entrypoint {match.get('symbol')!r}"
                    + (f" ({route_method} {route_path})" if route_method and route_path else "")
                ),
                "route_method": route_method, "route_path": route_path,
                "symbol": str(match.get("symbol") or candidate.entrypoint_symbol or ""),
                "qualified_name": str(match.get("qualified_name") or ""),
                "file": str(match.get("file") or ""),
                "repo": str(match.get("repo") or ""),
                "kind": ENTRYPOINT_HTTP if route_method and route_path else ENTRYPOINT_DECORATED,
                "ambiguous": False,
            }

        route_method, route_path = parsed_route if parsed_route else (None, None)
        detail = (
            f"{ambiguous_symbol_count} known entrypoints share the symbol name "
            f"{candidate.entrypoint_symbol!r}; ambiguous, not corroborated"
            if ambiguous_symbol_count
            else "no known entrypoint or route matches this candidate"
        )
        return {
            "corroborated": False,
            "detail": detail,
            "route_method": route_method, "route_path": route_path,
            "symbol": candidate.entrypoint_symbol or candidate.entrypoint_label,
            "qualified_name": "", "file": "", "repo": "",
            "kind": ENTRYPOINT_HTTP if parsed_route else ENTRYPOINT_UNKNOWN,
            #: True only when `entrypoint_symbol` matched *more than one*
            #: already-known entrypoint — the symbol is genuinely known to
            #: Sydes, just not uniquely resolvable to one record. Distinct
            #: from a plain no-match: the grounding gate in `interpreter.py`
            #: treats this as evidence the guide named something real, not a
            #: symbol pulled from nowhere the structural facts ever indexed.
            "ambiguous": bool(ambiguous_symbol_count),
        }
