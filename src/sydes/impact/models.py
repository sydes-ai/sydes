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
#: A step built directly from an LLM's semantic inference — no graph edge, no
#: source read. Never claims proof; it exists so a reader can always tell a
#: proven hop from an inferred one at a glance, same as `RELATION_SOURCE_CONFIRMED`
#: distinguishes a guided source read from a plain graph edge.
RELATION_LLM_INFERRED = "llm_inferred"

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
#: A path built from an LLM semantic-inference candidate. Distinct from
#: `STRATEGY_GUIDED_INVESTIGATION` (which still ends in a real confirmed
#: graph/source relationship) — this strategy carries no such proof, only a
#: model's rationale and confidence, however corroboration may have gone.
STRATEGY_LLM_SEMANTIC_INFERENCE = "llm_semantic_inference"

#: An entrypoint's kind, as far as the facts support. `unknown` is a real
#: answer: a decorated symbol that is plainly an entrypoint but whose framework
#: Sydes does not recognise is more useful reported as unknown than guessed
#: into the wrong bucket.
ENTRYPOINT_HTTP = "http_route"
ENTRYPOINT_DECORATED = "decorated"
ENTRYPOINT_UNKNOWN = "unknown"

#: Increment C: the small, transport-neutral boundary taxonomy a
#: `DiscoveredBoundary` may be classified as. `unknown`/`external` are
#: fallback/reporting kinds — this task spends no real effort discovering
#: `external` boundaries. See `sydes.impact.boundary_discovery`.
BOUNDARY_API = "api"
BOUNDARY_CALLABLE = "callable"
BOUNDARY_ASYNC = "async"
BOUNDARY_EXTERNAL = "external"
BOUNDARY_UNKNOWN = "unknown"

#: Optional, evidence-grounded refinement of a boundary's kind. Never
#: invented beyond what structural facts already show — `http` from route
#: metadata already captured, `scheduled_job`/`event_handler` from a small,
#: generic decorator-keyword match (see `boundary_discovery._ASYNC_KEYWORDS`),
#: `public_library` only from an exported symbol reached across a module/
#: directory boundary (Increment C.1 — a same-directory export alone is not
#: strong enough evidence; see `boundary_discovery._crosses_module_boundary`).
BOUNDARY_SUBTYPE_HTTP = "http"
BOUNDARY_SUBTYPE_SCHEDULED_JOB = "scheduled_job"
BOUNDARY_SUBTYPE_EVENT_HANDLER = "event_handler"
BOUNDARY_SUBTYPE_PUBLIC_LIBRARY = "public_library"
#: Increment C.2. `route_registration`: the symbol itself registers routes
#: (from `route_index` route-call sites attributed to their enclosing
#: symbol). `public_callable`: an explicit export *statement* names this
#: symbol — the only public-surface signal strong enough to establish a
#: callable boundary, as distinct from `symbol_index`'s `exported` bool,
#: which is a naming convention in at least one supported language.
BOUNDARY_SUBTYPE_ROUTE_REGISTRATION = "route_registration"
BOUNDARY_SUBTYPE_PUBLIC_CALLABLE = "public_callable"

#: How strong the weakest edge on a boundary's accepted path was. A boundary
#: reached ONLY through an import-only or signature/type-only reference is
#: never emitted at all (see `boundary_discovery._MIN_ADMIT_EDGE_STRENGTH`) —
#: this vocabulary exists so an emitted boundary's own strength stays visible
#: even though every emitted one already cleared that bar.
EDGE_STRENGTH_STRONG = "strong"
EDGE_STRENGTH_MEDIUM = "medium"
EDGE_STRENGTH_WEAK = "weak"

#: Whether the traversal that produced a result ran to completion.
COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_TRUNCATED = "truncated"
COMPLETENESS_UNRESOLVED = "unresolved"

#: Whether one affected entrypoint has a deterministic graph/source path
#: (`PROVEN`) or came from the guide's own semantic reasoning with no such
#: path (`INFERRED`). Proof, not confidence: an `INFERRED` entry may carry a
#: high model confidence and still be `INFERRED` — this field never changes
#: just because the model sounded sure. See `AffectedEntrypoint.status`.
IMPACT_STATUS_PROVEN = "proven"
IMPACT_STATUS_INFERRED = "inferred"


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
#: An inferred candidate whose entrypoint matched something already present
#: in the known facts (a declared route/entrypoint) — cheap corroboration,
#: not a graph path. Still `IMPACT_STATUS_INFERRED`, never promoted to
#: `PROVEN`: corroboration raises confidence in the claim, it does not
#: manufacture the deterministic path `PROVEN` requires.
PROVENANCE_LLM_INFERRED_CORROBORATED = "llm_inferred_corroborated"
#: An inferred candidate with no matching known fact at all — the model's
#: reasoning alone. Recorded rather than discarded, exactly as uncorroborated
#: as its name says.
PROVENANCE_LLM_INFERRED_UNCORROBORATED = "llm_inferred_uncorroborated"


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
#: The primary M3 action: direct semantic impact inference. Rather than
#: picking one target/relationship to mechanically check, the guide proposes
#: zero or more `ImpactCandidate`s — entrypoints or behaviors it believes are
#: plausibly affected, with its own confidence and rationale attached. The
#: graph-navigation actions above remain available for the guide to gather
#: more context first, but are no longer the only way a turn can conclude.
ACTION_INFER_IMPACT = "infer_impact"
INVESTIGATION_ACTIONS = frozenset({
    ACTION_TRACE_CALLERS, ACTION_TRACE_USAGES, ACTION_INSPECT_SYMBOL,
    ACTION_INSPECT_ENCLOSING_FUNCTION, ACTION_INSPECT_SOURCE_SPAN,
    ACTION_FIND_DECORATOR_REFERENCES, ACTION_FIND_SIGNATURE_REFERENCES,
    ACTION_INSPECT_NEARBY_ENTRYPOINTS, ACTION_STOP_UNRESOLVED, ACTION_INFER_IMPACT,
})
#: Actions that require `target` to name a symbol the question already
#: surfaced. Only `INSPECT_NEARBY_ENTRYPOINTS`, `STOP_UNRESOLVED`, and
#: `INFER_IMPACT` can act without one (the first discovers new candidates by
#: file, not by name; the last supplies `candidates` instead of a target).
ACTIONS_REQUIRING_TARGET = INVESTIGATION_ACTIONS - {
    ACTION_INSPECT_NEARBY_ENTRYPOINTS, ACTION_STOP_UNRESOLVED, ACTION_INFER_IMPACT,
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
    #: `IMPACT_STATUS_PROVEN` (default) for every deterministic strategy;
    #: `IMPACT_STATUS_INFERRED` only for an entry the guide proposed with no
    #: deterministic path. If the same entrypoint is ever found both ways,
    #: PROVEN wins and the record stays PROVEN — see `_record`/`_record_inferred`
    #: in `interpreter.py`, which enforce that at merge time, not here.
    status: str = IMPACT_STATUS_PROVEN
    #: The fields below are populated only when `status == IMPACT_STATUS_INFERRED`.
    #: Advisory metadata from the model, never proof — `label`/`strategies`
    #: continue to describe *what* was found; these describe how sure the
    #: model was and why, for a reader deciding how much to trust an entry
    #: with no graph/source path behind it.
    llm_confidence: float | None = None
    llm_reason: str = ""
    llm_inference_type: str = ""
    llm_uncertainty: str = ""
    #: Whether cheap corroboration (a match against an already-known fact —
    #: never a new search) found something for this inferred candidate. Still
    #: not proof; still `IMPACT_STATUS_INFERRED`. See
    #: `PROVENANCE_LLM_INFERRED_CORROBORATED`.
    corroborated: bool = False

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
            "status": self.status,
            "llm_confidence": self.llm_confidence,
            "llm_reason": self.llm_reason,
            "llm_inference_type": self.llm_inference_type,
            "llm_uncertainty": self.llm_uncertainty,
            "corroborated": self.corroborated,
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
    #: One compact, JSON-safe record per semantic-inference candidate the
    #: guide ever proposed in this run — never a giant prompt/response dump.
    #: Each entry: changed_symbol, turn, candidate entrypoint/symbol,
    #: confidence, rationale, inference_type, uncertainty, corroborated,
    #: corroboration_evidence, accepted, rejection_reason. Exists so every
    #: guide turn can be reconstructed and evaluated after the fact, not just
    #: summarized by the aggregate `metrics` counters.
    llm_candidate_log: list[dict[str, Any]] = field(default_factory=list)
    #: Increment C: typed, transport-neutral boundaries the ranked frontier
    #: walk in `boundary_discovery` found — always deterministic/structural
    #: (see `DiscoveredBoundary.status`), never derived from
    #: `llm_candidate_log`. Complementary to `affected`/`http_entrypoints`,
    #: not a replacement: an HTTP boundary here and an `AffectedEntrypoint`
    #: of `kind=ENTRYPOINT_HTTP` above may describe the same real route from
    #: two different callers.
    boundaries: list["DiscoveredBoundary"] = field(default_factory=list)
    #: One compact record per frontier candidate that was either emitted as a
    #: boundary or notably rejected (weak evidence, budget exhaustion) — not
    #: one per graph node visited. See `boundary_discovery.discover_boundaries`.
    boundary_decisions: list[dict[str, Any]] = field(default_factory=list)

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
            "llm_candidate_log": list(self.llm_candidate_log),
            "boundaries": [item.to_dict() for item in self.boundaries],
        }


@dataclass
class DiscoveredBoundary:
    """One typed, transport-neutral software boundary the ranked frontier
    walk found reachable from a changed symbol — see
    `sydes.impact.boundary_discovery`.

    Deliberately small and flat, not a hierarchy: every boundary this pass
    emits was reached via a real, walked structural edge (`RELATION_CALLS`/
    `RELATION_USAGE`, never a signature/type-only reference), so `status` is
    always `IMPACT_STATUS_PROVEN` — this pass never fabricates an edge, and
    never invents behavioral evidence from a semantic hint alone. Usable even
    when no HTTP route exists; an HTTP boundary here (`kind=BOUNDARY_API`,
    `subtype=BOUNDARY_SUBTYPE_HTTP`) is a parallel, complementary view of the
    same route an `AffectedFlow` may also model, never a substitute for one.
    """

    id: str
    kind: str  # BOUNDARY_API | BOUNDARY_CALLABLE | BOUNDARY_ASYNC | BOUNDARY_UNKNOWN
    repo: str
    file: str
    symbol: str
    qualified_name: str = ""
    label: str = ""
    subtype: str | None = None
    changed_symbols: list[str] = field(default_factory=list)
    #: The path that reached this boundary — same `ImpactPath` type
    #: `AffectedEntrypoint.paths` already uses, so a boundary's evidence
    #: renders and serializes identically to any other reached path.
    path: ImpactPath | None = None
    distance: int = 0
    #: The weakest edge relation on the accepted path — `EDGE_STRENGTH_*`.
    #: Never `weak`: a path admitted only through an import/signature-only
    #: reference is rejected before it is ever recorded here.
    evidence_strength: str = EDGE_STRENGTH_MEDIUM
    #: Deterministic, structural — see the class docstring. Kept as a field
    #: (rather than a constant) only so a reader never has to special-case
    #: this type against `AffectedEntrypoint.status`.
    status: str = IMPACT_STATUS_PROVEN
    #: The ranking score that selected this candidate over others — advisory
    #: diagnostics only, never itself evidence.
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subtype": self.subtype,
            "repo": self.repo,
            "file": self.file,
            "symbol": self.symbol,
            "qualified_name": self.qualified_name,
            "label": self.label or self.qualified_name or self.symbol,
            "changed_symbols": sorted(set(self.changed_symbols)),
            "path": self.path.to_dict() if self.path is not None else None,
            "distance": self.distance,
            "evidence_strength": self.evidence_strength,
            "status": self.status,
            "score": round(self.score, 3),
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
    use to go beyond the bounded action vocabulary — it is shown evidence,
    never asked to invent it. Deliberately bounded: this is a small, curated
    context for one changed symbol, never a repository dump or the whole
    diff.
    """

    repo: str
    changed_symbol: str
    qualified_name: str
    file: str
    reason: str
    #: Human-readable renderings of the trails the deterministic walk
    #: actually found before stalling.
    partial_paths: tuple[str, ...] = ()
    #: Files already known to be structurally relevant (the changed symbol's
    #: own file, plus every dead-end node's file) — legal for
    #: `INSPECT_NEARBY_ENTRYPOINTS`'s `target`.
    known_files: tuple[str, ...] = ()
    #: Already-known entrypoints among the names surfaced so far (a subset
    #: of `candidate_entrypoints`) — context for "is there already a route
    #: nearby", without a separate query.
    known_entrypoints: tuple[str, ...] = ()
    #: Every action this turn's symbol has already tried, with its outcome,
    #: oldest first — so the guide can see what already failed instead of
    #: repeating it.
    attempted_actions: tuple[str, ...] = ()
    candidate_entrypoints: tuple[str, ...] = ()
    #: Names legal as `InvestigationDecision.sought_symbol` right now — the
    #: meaningful (really-defined) nodes on the unresolved frontier, not
    #: picked down to one in advance. A source-confirming action must name
    #: its relationship target from this list, never from thin air.
    candidate_origins: tuple[str, ...] = ()
    #: A short, bounded preview of the changed symbol's own current source —
    #: a handful of statements, not the whole function or file — so the
    #: guide has some concrete code to reason from up front.
    source_context: str = ""
    remaining_budget: int = 0
    #: Other symbols changed by this same PR (short names, bounded/deduped) —
    #: whole-change context so the guide reasons about this one unresolved
    #: symbol as part of the actual change, not in isolation. Never a second
    #: diff dump: just names, so the model knows what else moved together.
    other_changed_symbols: tuple[str, ...] = ()
    #: Compact labels of impacts already accepted this run (deterministic or
    #: previously-inferred), bounded/deduped — lets the guide see what
    #: deterministic evidence already established before it reasons about
    #: this symbol, instead of reasoning from a blank slate every turn.
    accepted_impacts_so_far: tuple[str, ...] = ()
    #: True for exactly one turn per `interpret()` call: the whole-change
    #: pass that runs before any per-symbol turn. When set, `changed_symbol`
    #: is a neutral marker rather than one real symbol, and
    #: `other_changed_symbols`/`unresolved_symbols` carry the *complete*
    #: lists (not "every symbol but this one") — see `build_guide_prompt`.
    is_whole_change: bool = False
    #: Short names of every changed symbol still unresolved by the
    #: deterministic pass, bounded/deduped — only populated on the
    #: whole-change turn, so the guide can see the shape of what is still
    #: unknown, not just what has already been accepted.
    unresolved_symbols: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "changed_symbol": self.changed_symbol,
            "qualified_name": self.qualified_name,
            "file": self.file,
            "reason": self.reason,
            "partial_paths": list(self.partial_paths),
            "known_files": list(self.known_files),
            "known_entrypoints": list(self.known_entrypoints),
            "attempted_actions": list(self.attempted_actions),
            "candidate_entrypoints": list(self.candidate_entrypoints),
            "candidate_origins": list(self.candidate_origins),
            "source_context": self.source_context,
            "remaining_budget": self.remaining_budget,
            "other_changed_symbols": list(self.other_changed_symbols),
            "accepted_impacts_so_far": list(self.accepted_impacts_so_far),
            "is_whole_change": self.is_whole_change,
            "unresolved_symbols": list(self.unresolved_symbols),
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


def _clamp_confidence(value: float) -> float:
    """Model confidence is advisory metadata, not math — but a value outside
    [0, 1] is either a formatting slip or a model that doesn't know what a
    probability is, and either way should not propagate uninspected."""
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class ImpactCandidate:
    """One semantic impact hypothesis, straight from a guide turn.

    Not evidence by itself. `entrypoint_label` is free text the guide chose
    (a route like "GET /cases", or a plain description of a behavior when no
    route applies) — the executor may find it matches something already
    known (corroboration) or may find nothing, and either way the candidate
    is preserved as `IMPACT_STATUS_INFERRED`, never silently dropped for
    lack of proof.
    """

    entrypoint_label: str
    entrypoint_symbol: str = ""
    confidence: float = 0.0
    reason: str = ""
    inference_type: str = ""
    uncertainty: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _clamp_confidence(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entrypoint_label": self.entrypoint_label,
            "entrypoint_symbol": self.entrypoint_symbol,
            "confidence": self.confidence,
            "reason": self.reason,
            "inference_type": self.inference_type,
            "uncertainty": self.uncertainty,
        }


@dataclass(frozen=True)
class InvestigationDecision:
    """One guide turn's answer.

    Two shapes, by `action`:

    - `ACTION_INFER_IMPACT` (the primary M3 path): `candidates` carries zero
      or more `ImpactCandidate`s — the guide's own semantic hypotheses about
      what this changed symbol plausibly affects. `target`/`sought_symbol`
      are unused for this action.
    - Any other action (the legacy graph-navigation vocabulary, kept for
      context-gathering turns): `target` must name a symbol or file the
      corresponding `ImpactQuestion` already surfaced (in `partial_paths`,
      `nearby_facts`, or `candidate_entrypoints`) or the changed symbol
      itself — the executor rejects anything else rather than trust a name
      the guide introduced on its own. `sought_symbol` is required only for
      the source-confirming actions (`ACTIONS_REQUIRING_SOUGHT_SYMBOL`): it
      names the *other* half of the relationship being checked — "does
      `target`'s source reference `sought_symbol`?" — and must come from the
      question's `candidate_origins`, never from `target`'s own vocabulary.
    """

    action: str
    target: str = ""
    sought_symbol: str = ""
    rationale: str = ""
    candidates: tuple[ImpactCandidate, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    #: Optional, meaningful only on the one whole-change `ACTION_INFER_IMPACT`
    #: turn (`ImpactQuestion.is_whole_change`): names, in the guide's own
    #: priority order, which still-unresolved changed symbols it judges worth
    #: a closer, targeted look. Never a command — the interpreter still owns
    #: which of these actually get a follow-up turn, bounded by the same
    #: `GuideBudget` as everything else; an empty tuple means "no opinion,"
    #: not "nothing is worth investigating."
    follow_up_symbols: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "target": self.target,
            "sought_symbol": self.sought_symbol,
            "rationale": self.rationale,
            "candidates": [c.to_dict() for c in self.candidates],
            "parameters": dict(self.parameters),
            "follow_up_symbols": list(self.follow_up_symbols),
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
