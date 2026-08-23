"""What "affected" means, as data.

An impact result has to survive being disagreed with. A developer reading
"`POST /cases/{case_id}` is affected" will ask *why*, and an answer of "the
graph said so" is not reviewable. So every affected entrypoint carries the
ordered path that reached it, the relationship type at each step, and the name
of the strategy that proposed it.

Kept deliberately small. This layer answers one question — which entrypoints
could this change reach — and hands the answer downstream. Obligations,
evidence and verdicts are not decided here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: How one node was reached from the previous one. These are relationship
#: kinds observed in the structural facts, not framework concepts.
RELATION_DIRECT = "direct"
RELATION_CALLS = "calls"
RELATION_USAGE = "usage"
RELATION_DECORATOR_REFERENCE = "decorator_reference"
RELATION_SIGNATURE_REFERENCE = "signature_reference"
#: A caller/callee edge the deterministic graph never reported, added only
#: after source inspection confirmed it in text. Kept distinct from
#: `RELATION_CALLS` so a reader can always tell a graph fact from a
#: guide-directed source finding, no matter how far downstream it travels.
RELATION_SOURCE_CONFIRMED = "source_confirmed"

#: Which strategy proposed a path. Recorded so a surprising result can be
#: attributed to the rule that produced it rather than to the system at large.
STRATEGY_DIRECT_ENTRYPOINT = "direct_entrypoint"
STRATEGY_CALL_REACHABILITY = "call_reachability"
STRATEGY_USAGE_REACHABILITY = "usage_reachability"
STRATEGY_DECORATOR_REFERENCE = "decorator_reference"
STRATEGY_SIGNATURE_REFERENCE = "signature_reference"
#: A path containing at least one `RELATION_SOURCE_CONFIRMED` hop. Recorded as
#: its own strategy, never folded into `STRATEGY_CALL_REACHABILITY`, because a
#: reader must be able to see that one hop of this path came from guided
#: source inspection rather than the graph alone.
STRATEGY_GUIDED_INVESTIGATION = "guided_investigation"

#: An entrypoint's kind, as far as the facts support. `unknown` is a real
#: answer: a decorated symbol that is plainly an entrypoint but whose framework
#: Sydes does not recognise is more useful reported as unknown than guessed
#: into the wrong bucket.
ENTRYPOINT_HTTP = "http_route"
ENTRYPOINT_DECORATED = "decorated"
ENTRYPOINT_UNKNOWN = "unknown"

#: Whether the traversal that produced a result ran to completion.
COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_TRUNCATED = "truncated"
COMPLETENESS_UNRESOLVED = "unresolved"


#: Separator for a synthetic identity key. Not a valid identifier character in
#: any language Sydes or CBM parses, so it cannot collide with a real name.
_KEY_SEP = "\u241f"


#: How an `ImpactStep` came to exist. Every step defaults to `DETERMINISTIC`;
#: only a step added after the guide loop's source inspection confirmed it
#: carries `LLM_GUIDED_SOURCE_CONFIRMED`. This is provenance, not confidence \u2014
#: a reader can trust a deterministic step outright and must check the source
#: reference on a guided one.
PROVENANCE_DETERMINISTIC = "deterministic"
PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED = "llm_guided_source_confirmed"


#: Guide invocation policy. `off` runs the M2 deterministic system exactly as
#: before M3 existed; `auto` invokes the guide only on the structural triggers
#: below; `always` is a development knob and still cannot override
#: deterministic evidence \u2014 it only widens when the guide is asked.
GUIDE_OFF = "off"
GUIDE_AUTO = "auto"
GUIDE_ALWAYS = "always"
GUIDE_POLICIES = (GUIDE_OFF, GUIDE_AUTO, GUIDE_ALWAYS)

#: Why a changed symbol's impact is not yet a complete path. These are the
#: only conditions the guide loop treats as worth an LLM turn in `auto` mode \u2014
#: a symbol that already resolved deterministically never reaches the guide.
REASON_NO_ENTRYPOINT_REACHED = "no_entrypoint_reached"
REASON_TRAVERSAL_TRUNCATED = "traversal_truncated"
REASON_AMBIGUOUS_SYMBOL_IDENTITY = "ambiguous_symbol_identity"
REASON_PARTIAL_PATH_DEAD_END = "partial_path_dead_end"
REASON_MULTIPLE_UNRESOLVED_CANDIDATES = "multiple_unresolved_candidates"
GUIDE_TRIGGER_REASONS = frozenset({
    REASON_NO_ENTRYPOINT_REACHED, REASON_TRAVERSAL_TRUNCATED,
    REASON_AMBIGUOUS_SYMBOL_IDENTITY, REASON_PARTIAL_PATH_DEAD_END,
    REASON_MULTIPLE_UNRESOLVED_CANDIDATES,
})

#: The bounded action vocabulary. The guide chooses exactly one of these per
#: turn; anything else is a malformed decision and fails closed. Every action
#: executes through a capability Sydes or CBM already has \u2014 none of them let
#: the model construct a query, only select among fixed ones.
ACTION_TRACE_CALLERS = "trace_callers"
ACTION_TRACE_USAGES = "trace_usages"
ACTION_INSPECT_SYMBOL = "inspect_symbol"
ACTION_INSPECT_ENCLOSING_FUNCTION = "inspect_enclosing_function"
ACTION_INSPECT_SOURCE_SPAN = "inspect_source_span"
ACTION_FIND_DECORATOR_REFERENCES = "find_decorator_references"
ACTION_FIND_SIGNATURE_REFERENCES = "find_signature_references"
ACTION_INSPECT_NEARBY_ENTRYPOINTS = "inspect_nearby_entrypoints"
ACTION_STOP_UNRESOLVED = "stop_unresolved"
INVESTIGATION_ACTIONS = frozenset({
    ACTION_TRACE_CALLERS, ACTION_TRACE_USAGES, ACTION_INSPECT_SYMBOL,
    ACTION_INSPECT_ENCLOSING_FUNCTION, ACTION_INSPECT_SOURCE_SPAN,
    ACTION_FIND_DECORATOR_REFERENCES, ACTION_FIND_SIGNATURE_REFERENCES,
    ACTION_INSPECT_NEARBY_ENTRYPOINTS, ACTION_STOP_UNRESOLVED,
})
#: Actions that require `target` to name a symbol the question already
#: surfaced. Only `INSPECT_NEARBY_ENTRYPOINTS` and `STOP_UNRESOLVED` can act
#: without one (the former discovers new candidates by file, not by name).
ACTIONS_REQUIRING_TARGET = INVESTIGATION_ACTIONS - {
    ACTION_INSPECT_NEARBY_ENTRYPOINTS, ACTION_STOP_UNRESOLVED,
}
#: Source-confirming actions answer a concrete relationship — "does `target`'s
#: source reference `sought_symbol`?" — not just "inspect `target`". These are
#: the only actions that require a `sought_symbol`, chosen from the
#: `ImpactQuestion`'s `candidate_origins`, never picked by the loop itself.
ACTIONS_REQUIRING_SOUGHT_SYMBOL = frozenset({
    ACTION_INSPECT_SYMBOL, ACTION_INSPECT_ENCLOSING_FUNCTION, ACTION_INSPECT_SOURCE_SPAN,
})


@dataclass(frozen=True)
class SymbolIdentity:
    """What makes one symbol the same symbol, everywhere in impact analysis.

    Bare short name is deliberately excluded from equality: two functions
    named `update` in different modules are different symbols, and a graph
    that cannot tell them apart cannot bound its own traversal — which is
    exactly what happened before this type existed. Identity resolves in three
    tiers, from most to least certain:

      1. `qualified_name` present -> identity is (repo, qualified_name).
         Stable, and what CBM supplies for essentially every call/usage edge.
      2. `qualified_name` absent but a line number is known -> identity is
         (repo, file, short_name, line). A file rarely defines the same name
         twice at the same line, so this stays scoped without inventing a
         qualifier CBM never reported.
      3. Neither present -> identity is (repo, file, short_name), and it is
         marked unresolved. Still scoped to one file — never to a bare name
         across the whole repository — but two same-named, same-file, same-
         line-less symbols would collide here, so `resolved` says so and
         callers must not treat this identity as safe to fan out from.

    `short_name` is kept only for display; it plays no part in equality.
    """

    repo: str
    file: str
    qualified_name: str = ""
    short_name: str = ""
    line: int | None = None

    def __eq__(self, other: object) -> bool:
        # Equality follows `key`, not field-by-field comparison: once a
        # qualified name is known it alone determines identity, and `line` is
        # provenance rather than a distinguishing property at that point.
        if not isinstance(other, SymbolIdentity):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    @property
    def resolved(self) -> bool:
        """False only for the tier-3 fallback, where the key may be shared."""
        return bool(self.qualified_name) or self.line is not None

    @property
    def key(self) -> str:
        """A deterministic string key, safe to use as a dict/set member."""
        if self.qualified_name:
            return _KEY_SEP.join((self.repo, "qn", self.qualified_name))
        if self.line is not None:
            return _KEY_SEP.join(
                (self.repo, "fl", self.file, self.short_name, str(self.line))
            )
        return _KEY_SEP.join((self.repo, "fn", self.file, self.short_name))

    @property
    def label(self) -> str:
        """Human-readable form for evidence and diagnostics."""
        return self.qualified_name or f"{self.file}:{self.short_name}"

    @classmethod
    def from_fields(
        cls,
        *,
        repo: str,
        file: str,
        qualified_name: str | None = None,
        short_name: str | None = None,
        line: int | None = None,
    ) -> "SymbolIdentity":
        """Build an identity from whatever a fact or a caller happened to have.

        `short_name` is derived from `qualified_name` when only that is given,
        so a caller never has to duplicate the split-on-dot itself.
        """
        qualified = str(qualified_name or "").strip()
        name = str(short_name or "").strip() or (
            qualified.rsplit(".", 1)[-1] if qualified else ""
        )
        return cls(repo=str(repo), file=str(file), qualified_name=qualified,
                   short_name=name, line=line)


@dataclass(frozen=True)
class ImpactStep:
    """One hop: the symbol reached, and the relationship that reached it."""

    symbol: str
    qualified_name: str
    file: str
    relation: str
    #: Free-text support for this hop — a decorator fragment, a signature
    #: substring — so the step can be checked without re-querying the graph.
    evidence: str = ""
    #: False when this hop's identity came from the tier-3 fallback (no
    #: qualified name, no line). The step is still reported — dropping it
    #: would silently hide a real edge — but a reader should not treat it as
    #: certainly the same symbol every time it recurs.
    identity_resolved: bool = True
    #: `PROVENANCE_DETERMINISTIC` for every step the graph itself produced;
    #: `PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED` only for a step whose edge was
    #: missing from the graph and was instead confirmed by reading source.
    #: The guide never sets this on its own say-so — only the investigation
    #: executor does, after finding the reference in text.
    provenance: str = PROVENANCE_DETERMINISTIC

    def describe(self) -> str:
        return f"{self.relation}:{self.qualified_name or self.symbol}"


@dataclass(frozen=True)
class ImpactPath:
    """An ordered walk from a changed symbol to an entrypoint."""

    steps: tuple[ImpactStep, ...]
    strategy: str

    @property
    def length(self) -> int:
        return len(self.steps)

    @property
    def relations(self) -> tuple[str, ...]:
        return tuple(step.relation for step in self.steps)

    def describe(self) -> str:
        """A one-line rendering, suitable for a diagnostics section."""
        return " -> ".join(step.describe() for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "length": self.length,
            "steps": [
                {
                    "symbol": step.symbol,
                    "qualified_name": step.qualified_name,
                    "file": step.file,
                    "relation": step.relation,
                    "evidence": step.evidence,
                    "identity_resolved": step.identity_resolved,
                    "provenance": step.provenance,
                }
                for step in self.steps
            ],
        }


@dataclass
class AffectedEntrypoint:
    """One entrypoint a change could reach, with the reasons it was reached."""

    repo: str
    symbol: str
    qualified_name: str
    file: str
    kind: str = ENTRYPOINT_UNKNOWN
    route_method: str | None = None
    route_path: str | None = None
    #: Every changed symbol that reached this entrypoint.
    changed_symbols: list[str] = field(default_factory=list)
    #: Every distinct path that reached it. Kept whole rather than reduced to a
    #: shortest path: two different relationships reaching the same handler are
    #: two different reasons to look at it.
    paths: list[ImpactPath] = field(default_factory=list)

    @property
    def strategies(self) -> list[str]:
        """Which strategies proposed this entrypoint, in stable order."""
        return sorted({path.strategy for path in self.paths})

    @property
    def label(self) -> str:
        """How a person would name this entrypoint."""
        if self.route_method and self.route_path:
            return f"{self.route_method} {self.route_path}"
        return self.qualified_name or self.symbol

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "label": self.label,
            "symbol": self.symbol,
            "qualified_name": self.qualified_name,
            "file": self.file,
            "kind": self.kind,
            "route_method": self.route_method,
            "route_path": self.route_path,
            "changed_symbols": sorted(self.changed_symbols),
            "strategies": self.strategies,
            "paths": [path.to_dict() for path in self.paths],
        }


@dataclass
class UnresolvedImpact:
    """A changed symbol whose reach could not be established.

    Recorded rather than dropped. "No entrypoint found" and "the traversal ran
    out of room" look identical downstream unless the difference is kept, and
    only one of them means the change is safely inert.
    """

    repo: str
    symbol: str
    reason: str
    detail: str = ""
    #: True once the guide loop has looked at this symbol, regardless of
    #: outcome. Distinguishes "the guide tried and found nothing" from "the
    #: guide was never asked" — both remain unresolved, but only one of them
    #: means the investigation budget was actually spent here.
    guide_investigated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "symbol": self.symbol,
                "reason": self.reason, "detail": self.detail,
                "guide_investigated": self.guide_investigated}


@dataclass
class ImpactResult:
    """Everything the interpreter concluded about one change."""

    affected: list[AffectedEntrypoint] = field(default_factory=list)
    unresolved: list[UnresolvedImpact] = field(default_factory=list)
    completeness: str = COMPLETENESS_COMPLETE
    #: Counters and limits, for diagnostics.
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def http_entrypoints(self) -> list[AffectedEntrypoint]:
        return [item for item in self.affected if item.kind == ENTRYPOINT_HTTP]

    def to_dict(self) -> dict[str, Any]:
        return {
            "completeness": self.completeness,
            "affected": [item.to_dict() for item in self.affected],
            "unresolved": [item.to_dict() for item in self.unresolved],
            "metrics": dict(self.metrics),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# M3 — the LLM guide's investigation loop
#
# Everything below is data, not behaviour: `ImpactQuestion` is what the guide
# is told, `InvestigationDecision` is what it may answer, and
# `InvestigationEvidence` is what the deterministic executor found when it
# carried that answer out. None of these types call an LLM, call CBM, or read
# a file — they only shape what crosses those boundaries, so the boundary
# itself stays inspectable and testable without a live model or a live graph.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactQuestion:
    """What the guide is told about one unresolved changed symbol.

    Every field here is either a primitive or a tuple of strings: the guide
    never receives a live graph handle, a file handle, or anything it could
    use to go beyond the bounded action vocabulary. `partial_paths` and
    `nearby_facts` are human-readable renderings of facts the deterministic
    pass already collected — the guide is shown evidence, never asked to
    invent it.
    """

    repo: str
    changed_symbol: str
    qualified_name: str
    file: str
    reason: str
    partial_paths: tuple[str, ...] = ()
    nearby_facts: tuple[str, ...] = ()
    candidate_entrypoints: tuple[str, ...] = ()
    #: Names legal as `InvestigationDecision.sought_symbol` right now — the
    #: meaningful (really-defined) nodes on the unresolved frontier, not
    #: picked down to one in advance. A source-confirming action must name
    #: its relationship target from this list, never from thin air.
    candidate_origins: tuple[str, ...] = ()
    source_context: str = ""
    remaining_budget: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "changed_symbol": self.changed_symbol,
            "qualified_name": self.qualified_name,
            "file": self.file,
            "reason": self.reason,
            "partial_paths": list(self.partial_paths),
            "nearby_facts": list(self.nearby_facts),
            "candidate_entrypoints": list(self.candidate_entrypoints),
            "candidate_origins": list(self.candidate_origins),
            "source_context": self.source_context,
            "remaining_budget": self.remaining_budget,
        }


@dataclass(frozen=True)
class UnresolvedFrontier:
    """What is known about one changed symbol's unresolved reach.

    Exists so no part of the guide loop ever picks "the" origin by list
    position (`dead_ends[-1]`, as M3's first cut did). `frontier_nodes` are
    every dead-end node backed by a real definition, ordered deepest-first;
    `pseudo_or_low_value_nodes` are dead ends this index could not find a
    definition for (a module attribute, an import target, anything CBM
    surfaced without a body) — kept for transparency, never offered as a
    `sought_symbol`. `candidate_origins` is the guide-facing subset: which
    names are legal to investigate a relationship against right now.
    """

    start_symbol: str
    frontier_nodes: tuple[str, ...] = ()
    partial_paths: tuple[str, ...] = ()
    pseudo_or_low_value_nodes: tuple[str, ...] = ()
    candidate_origins: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_symbol": self.start_symbol,
            "frontier_nodes": list(self.frontier_nodes),
            "partial_paths": list(self.partial_paths),
            "pseudo_or_low_value_nodes": list(self.pseudo_or_low_value_nodes),
            "candidate_origins": list(self.candidate_origins),
        }


@dataclass(frozen=True)
class InvestigationDecision:
    """One guide turn's answer: exactly one action, on at most one target.

    `target` must name a symbol or file the corresponding `ImpactQuestion`
    already surfaced (in `partial_paths`, `nearby_facts`, or
    `candidate_entrypoints`) or the changed symbol itself — the executor
    rejects anything else rather than trust a name the guide introduced on
    its own.

    `sought_symbol` is required only for the source-confirming actions
    (`ACTIONS_REQUIRING_SOUGHT_SYMBOL`): it names the *other* half of the
    relationship being checked — "does `target`'s source reference
    `sought_symbol`?" — and must come from the question's
    `candidate_origins`, not from `target`'s own vocabulary.
    """

    action: str
    target: str = ""
    sought_symbol: str = ""
    rationale: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "target": self.target,
            "sought_symbol": self.sought_symbol,
            "rationale": self.rationale, "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class InvestigationEvidence:
    """What the executor found when it carried out one decision.

    `found` is the only field the guide loop acts on: a decision that
    produced no evidence leaves the changed symbol unresolved, no matter how
    plausible its rationale was. `provenance` records where the evidence
    came from (a graph re-query vs. a source read) so a step built from it
    carries that same distinction forward. `line`/`matched_text` for a
    source-confirming action always describe the exact line the sought
    identifier was actually found on — never a containing block's start line
    whose own excerpt doesn't contain the evidence.
    """

    action: str
    target: str
    found: bool
    ambiguous: bool
    detail: str
    provenance: str
    sought_symbol: str = ""
    file: str = ""
    line: int | None = None
    matched_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "target": self.target, "found": self.found,
            "ambiguous": self.ambiguous, "detail": self.detail,
            "provenance": self.provenance, "sought_symbol": self.sought_symbol,
            "file": self.file, "line": self.line,
            "matched_text": self.matched_text,
        }
