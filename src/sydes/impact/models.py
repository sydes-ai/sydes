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

#: Which strategy proposed a path. Recorded so a surprising result can be
#: attributed to the rule that produced it rather than to the system at large.
STRATEGY_DIRECT_ENTRYPOINT = "direct_entrypoint"
STRATEGY_CALL_REACHABILITY = "call_reachability"
STRATEGY_USAGE_REACHABILITY = "usage_reachability"
STRATEGY_DECORATOR_REFERENCE = "decorator_reference"
STRATEGY_SIGNATURE_REFERENCE = "signature_reference"

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

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "symbol": self.symbol,
                "reason": self.reason, "detail": self.detail}


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
