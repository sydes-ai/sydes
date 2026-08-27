"""Normalized boundary evidence — Increment C.2.

Increment C.1 fixed boundary *precision* (tests and `main` stopped being
emitted as production boundaries). It left *recall* low: real-PR evaluation
showed route-registration methods, decorated handlers and service surfaces
producing no boundary at all. Inspection found the cause is not the
traversal or the eligibility predicate — it is that architectural facts
Sydes already computes never reach the boundary layer in a usable shape.

This module is the small normalization layer that fixes that, in the same
spirit as a compiler IR: many source/framework syntaxes are already reduced
by Sydes' own extractors into a handful of raw fact shapes; this reduces
*those* into one tiny transport-neutral vocabulary that
`boundary_discovery._classify` can reason over without knowing any framework.

    StructuralFacts  ->  BoundaryEvidence  ->  existing ranked discovery

What was already available, and where it was going:

- `facts.entrypoints[]` carries `route_method`/`route_path` and verbatim
  `decorators` text (CBM). Boundary discovery already read this; it stays
  the strongest, most direct evidence source.
- `facts.route_index` carries, per file, every route-registration call site
  Sydes' own deterministic extractor found (`route_calls[]`: receiver,
  method, path, handler_hint, line), plus router `containers[]`,
  `mount_calls[]` and explicit `exports[]` statements. It is populated by
  BOTH backends — CBM computes it through Sydes' own extractor, since CBM
  models no route composition. Boundary discovery never looked at it at all.
  That omission is the single largest recall gap C.2 closes: a changed
  method that *registers* routes now has grounded API evidence without any
  global route-file search, attributed to it by line span.
- `facts.symbol_index` carries `start_line`/`end_line` per symbol, which is
  what lets a route-registration call site at line N be attributed to the
  symbol whose body encloses it, rather than merely to a file.

Deliberate non-goals, per the increment's scope: no new parser, no new
regex library, no framework plugin, no LLM call, no CBM call. Everything
here reads facts already in memory.

Soundness rules this module must never break:
- A semantic hint is not structural evidence and never enters here — this
  module's inputs are `StructuralFacts` only; it has no access to
  `pr_semantic_analysis` and no parameter through which one could arrive.
- Generic decoration is not async evidence. A symbol that is merely
  `decorated=True`, with decorator text matching none of the small
  recognized vocabularies, yields NO evidence rather than a guess.
- A raw `exported` flag is not public-surface evidence. For Python it is
  literally "the name does not start with an underscore"
  (`handler_symbols/python.py::_is_exported`), which is why C.1's
  `exported + cross-directory` rule over-fired. Only an *explicit export
  statement* recorded in `route_index` counts as strong here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sydes.impact.models import (
    BOUNDARY_API,
    BOUNDARY_ASYNC,
    BOUNDARY_CALLABLE,
    BOUNDARY_SUBTYPE_EVENT_HANDLER,
    BOUNDARY_SUBTYPE_HTTP,
    BOUNDARY_SUBTYPE_PUBLIC_CALLABLE,
    BOUNDARY_SUBTYPE_ROUTE_REGISTRATION,
    BOUNDARY_SUBTYPE_SCHEDULED_JOB,
    EDGE_STRENGTH_MEDIUM,
    EDGE_STRENGTH_STRONG,
    EDGE_STRENGTH_WEAK,
    SymbolIdentity,
)

if TYPE_CHECKING:
    from sydes.code_intelligence.base import StructuralFacts

#: Where one piece of normalized evidence came from. Provenance only — the
#: strength field decides what it may establish.
EVIDENCE_SOURCE_ROUTE_METADATA = "route_metadata"
EVIDENCE_SOURCE_DECORATOR = "decorator"
EVIDENCE_SOURCE_REGISTRATION = "registration"
EVIDENCE_SOURCE_EXPORT = "export"

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Decorator/registration keyword vocabularies. Small and generic on
#: purpose: these are the words frameworks across languages actually use for
#: these two concepts, not a per-framework encyclopedia. A decorator whose
#: tokens hit none of them produces no async evidence at all.
_SCHEDULED_JOB_KEYWORDS = frozenset({
    "task", "cron", "schedule", "scheduled", "periodic", "celery", "job",
    "worker", "beat", "shared_task", "interval", "repeat",
})
_EVENT_HANDLER_KEYWORDS = frozenset({
    "signal", "receiver", "subscribe", "subscriber", "consumer", "listener",
    "on_event", "event_handler", "queue", "dispatch", "handles", "hook",
})


@dataclass(frozen=True)
class BoundaryEvidence:
    """One normalized architectural fact about a single symbol.

    Internal to the impact layer — `AffectedBoundary` remains the only
    product-facing boundary model. Several pieces of evidence may exist for
    one symbol; `boundary_discovery` picks the strongest.
    """

    kind: str  # BOUNDARY_API | BOUNDARY_CALLABLE | BOUNDARY_ASYNC
    subtype: str
    source: str  # EVIDENCE_SOURCE_*
    strength: str  # EDGE_STRENGTH_STRONG | _MEDIUM | _WEAK
    #: Short, human-readable support — a route string, a decorator fragment,
    #: an export statement kind. Never a raw source dump.
    detail: str = ""
    #: For registration evidence: the handler symbol this registration names,
    #: when the extractor captured one.
    target_symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subtype": self.subtype,
            "source": self.source,
            "strength": self.strength,
            "detail": self.detail,
            "target_symbol": self.target_symbol,
        }


#: Rank used to pick the single strongest evidence for a symbol.
_STRENGTH_ORDER = {EDGE_STRENGTH_STRONG: 3, EDGE_STRENGTH_MEDIUM: 2, EDGE_STRENGTH_WEAK: 1}


def strength_rank(evidence: BoundaryEvidence) -> int:
    return _STRENGTH_ORDER.get(evidence.strength, 0)


def _tokens(text: str) -> frozenset[str]:
    """Identifier tokens, plus their `_`-separated parts — real decorators
    are commonly compound (`shared_task`, `signal_receiver`, `on_event`)."""
    out: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(text or ""):
        word = match.group(0).lower()
        out.add(word)
        out.update(part for part in word.split("_") if part)
    return frozenset(out)


def _async_evidence_from_decorator(text: str) -> BoundaryEvidence | None:
    """Async evidence from decorator/registration text, or `None`.

    `None` — not a weak/unknown async guess — is the answer for a decorator
    whose meaning this vocabulary does not recognize. Generic decoration
    stays generic; that is the C.1 precision guarantee, preserved here."""
    if not text:
        return None
    tokens = _tokens(text)
    if tokens & _SCHEDULED_JOB_KEYWORDS:
        return BoundaryEvidence(
            kind=BOUNDARY_ASYNC, subtype=BOUNDARY_SUBTYPE_SCHEDULED_JOB,
            source=EVIDENCE_SOURCE_DECORATOR, strength=EDGE_STRENGTH_STRONG,
            detail=text[:120],
        )
    if tokens & _EVENT_HANDLER_KEYWORDS:
        return BoundaryEvidence(
            kind=BOUNDARY_ASYNC, subtype=BOUNDARY_SUBTYPE_EVENT_HANDLER,
            source=EVIDENCE_SOURCE_DECORATOR, strength=EDGE_STRENGTH_STRONG,
            detail=text[:120],
        )
    return None


@dataclass
class BoundaryEvidenceIndex:
    """Normalized boundary evidence for one `StructuralFacts`, built once
    per `interpret()` and looked up by symbol identity.

    Keyed two ways because the two fact families identify symbols
    differently: `facts.entrypoints` carries a qualified name, while
    `route_index` knows only a file and a line (which is resolved to an
    enclosing symbol name through `symbol_index` spans).
    """

    #: (file, short_name) -> evidence. The only lookup key that both fact
    #: families can produce.
    by_file_symbol: dict[tuple[str, str], list[BoundaryEvidence]] = field(default_factory=dict)

    def _add(self, file: str, symbol: str, evidence: BoundaryEvidence) -> None:
        if not file or not symbol:
            return
        self.by_file_symbol.setdefault((file, symbol), []).append(evidence)

    def for_identity(self, identity: SymbolIdentity) -> list[BoundaryEvidence]:
        """Every normalized evidence record for this symbol, strongest first."""
        found = list(self.by_file_symbol.get((identity.file, identity.short_name), ()))
        if not found and identity.qualified_name:
            # A qualified name like `Class.method` is attributed by its own
            # short name in `symbol_index`; try that tail too.
            tail = identity.qualified_name.rsplit(".", 1)[-1]
            if tail != identity.short_name:
                found = list(self.by_file_symbol.get((identity.file, tail), ()))
        return sorted(found, key=lambda item: -strength_rank(item))

    def strongest(self, identity: SymbolIdentity) -> BoundaryEvidence | None:
        found = self.for_identity(identity)
        return found[0] if found else None


def _symbol_spans(facts: "StructuralFacts", repo: str | None) -> dict[str, list[tuple[int, int, str]]]:
    """`file -> [(start_line, end_line, symbol_name)]`, for attributing a
    route-registration call site to the symbol whose body encloses it."""
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for repo_index in facts.symbol_index.get("repos", []) or []:
        if repo is not None and repo_index.get("repo") not in (None, repo):
            continue
        for file_item in repo_index.get("files", []) or []:
            path = str(file_item.get("path") or "")
            if not path:
                continue
            for symbol in file_item.get("symbols", []) or []:
                start = symbol.get("start_line")
                end = symbol.get("end_line") or start
                name = str(symbol.get("name") or "")
                if isinstance(start, int) and isinstance(end, int) and name:
                    spans.setdefault(path, []).append((start, end, name))
    for items in spans.values():
        # Innermost-first: a method's span sits inside its class's span, and
        # the method is the more precise attribution for a call site.
        items.sort(key=lambda item: (item[1] - item[0], item[0]))
    return spans


def _enclosing_symbol(spans: dict[str, list[tuple[int, int, str]]], file: str, line: int) -> str | None:
    for start, end, name in spans.get(file, ()):  # innermost-first
        if start <= line <= end:
            return name
    return None


def build_boundary_evidence(
    facts: "StructuralFacts", repo: str | None = None,
) -> BoundaryEvidenceIndex:
    """Normalize every boundary-shaped structural fact Sydes already has.

    Reads only `facts` — no CBM call, no LLM call, no file read, no parsing.
    """
    index = BoundaryEvidenceIndex()

    # --- 1. Route metadata + decorators on declared entrypoints ----------
    # The most direct evidence there is: the backend already told us this
    # symbol carries a route, or captured its decorator text verbatim.
    for entry in facts.entrypoints:
        if repo is not None and entry.get("repo") not in (None, repo):
            continue
        file = str(entry.get("file") or "")
        symbol = str(entry.get("symbol") or "")
        method = entry.get("route_method")
        path = entry.get("route_path")
        if method or path:
            index._add(file, symbol, BoundaryEvidence(
                kind=BOUNDARY_API, subtype=BOUNDARY_SUBTYPE_HTTP,
                source=EVIDENCE_SOURCE_ROUTE_METADATA, strength=EDGE_STRENGTH_STRONG,
                detail=f"{method or 'ANY'} {path or ''}".strip(),
            ))
        async_evidence = _async_evidence_from_decorator(str(entry.get("decorators") or ""))
        if async_evidence is not None:
            index._add(file, symbol, async_evidence)

    # --- 2. Route registration call sites (the previously-unread facts) --
    # `route_index` records every route/mount/container call Sydes' own
    # deterministic extractor found, with the line it sits on. Attributing
    # that line to its enclosing symbol turns "this file registers routes"
    # into "this SYMBOL registers routes" — grounded API evidence for a
    # changed route-registration method, with no global route search.
    spans = _symbol_spans(facts, repo)
    for repo_index in facts.route_index.get("repos", []) or []:
        if repo is not None and repo_index.get("repo") not in (None, repo):
            continue
        for file_item in repo_index.get("files", []) or []:
            path = str(file_item.get("path") or "")
            if not path:
                continue
            for call in file_item.get("route_calls", []) or []:
                line = call.get("line")
                if not isinstance(line, int):
                    continue
                owner = _enclosing_symbol(spans, path, line)
                if owner is None:
                    # A module-level route call belongs to no symbol; the
                    # handler it names is still covered below.
                    continue
                method = str(call.get("method") or "").upper()
                route_path = str(call.get("path") or "")
                index._add(path, owner, BoundaryEvidence(
                    kind=BOUNDARY_API, subtype=BOUNDARY_SUBTYPE_ROUTE_REGISTRATION,
                    source=EVIDENCE_SOURCE_REGISTRATION, strength=EDGE_STRENGTH_STRONG,
                    detail=f"registers {method} {route_path}".strip(),
                    target_symbol=str(call.get("handler_hint") or "") or None,
                ))
            # A handler named by a route call is itself an HTTP boundary,
            # even when the backend attached no route metadata to it.
            for call in file_item.get("route_calls", []) or []:
                handler = str(call.get("handler_hint") or "")
                if not handler:
                    continue
                method = str(call.get("method") or "").upper()
                route_path = str(call.get("path") or "")
                index._add(path, handler, BoundaryEvidence(
                    kind=BOUNDARY_API, subtype=BOUNDARY_SUBTYPE_HTTP,
                    source=EVIDENCE_SOURCE_REGISTRATION, strength=EDGE_STRENGTH_STRONG,
                    detail=f"{method} {route_path}".strip(),
                ))

    # --- 3. Explicit export statements -----------------------------------
    # The ONLY public-surface signal strong enough to establish a callable
    # boundary. Distinct from `symbol_index`'s `exported` bool, which for
    # Python is a naming convention rather than a declaration — see the
    # module docstring.
    for repo_index in facts.route_index.get("repos", []) or []:
        if repo is not None and repo_index.get("repo") not in (None, repo):
            continue
        for file_item in repo_index.get("files", []) or []:
            path = str(file_item.get("path") or "")
            for export in file_item.get("exports", []) or []:
                symbol = str(export.get("symbol") or "")
                kind = str(export.get("kind") or "named")
                if not symbol:
                    continue
                index._add(path, symbol, BoundaryEvidence(
                    kind=BOUNDARY_CALLABLE, subtype=BOUNDARY_SUBTYPE_PUBLIC_CALLABLE,
                    source=EVIDENCE_SOURCE_EXPORT, strength=EDGE_STRENGTH_STRONG,
                    detail=f"explicit {kind} export",
                ))

    return index
