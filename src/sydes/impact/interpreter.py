"""From changed symbols to the entrypoints they could reach.

This layer answers exactly one question and stops. It does not decide what must
be verified, does not run tests, and does not produce a verdict — those live
downstream and are unchanged.

The strategies are deliberately generic. Each one keys off a *kind of
structural fact* rather than a framework: a symbol carrying route metadata, a
call edge, a usage reference, an identifier appearing in captured decorator
text, an identifier appearing in a signature. No decorator name, framework, or
library is hardcoded, because a rule that recognises one framework by name
fails silently on the next one and is worse than a rule that admits it cannot
tell.

Traversal is bounded on every axis — depth, visited nodes, and a global work
budget — and every bound that bites is reported rather than absorbed. A result
that stopped early is marked truncated, because "no entrypoint reached" and
"gave up before reaching one" must not look the same to a reader.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
from typing import Any, Iterable

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_TRUNCATED,
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    ENTRYPOINT_UNKNOWN,
    RELATION_CALLS,
    RELATION_DECORATOR_REFERENCE,
    RELATION_DIRECT,
    RELATION_SIGNATURE_REFERENCE,
    RELATION_USAGE,
    STRATEGY_CALL_REACHABILITY,
    STRATEGY_DECORATOR_REFERENCE,
    STRATEGY_DIRECT_ENTRYPOINT,
    STRATEGY_SIGNATURE_REFERENCE,
    STRATEGY_USAGE_REACHABILITY,
    AffectedEntrypoint,
    ImpactPath,
    ImpactResult,
    ImpactStep,
    UnresolvedImpact,
)

#: Identifiers in decorator or signature text. Deliberately crude: this finds
#: *candidate* names, and a candidate only becomes a reference when it matches
#: a symbol the change actually touched.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Names that appear in almost every decorator and would match everything.
#: Excluded because a reference that matches every handler distinguishes none.
_UNINFORMATIVE = frozenset({
    "self", "cls", "None", "True", "False", "str", "int", "bool", "float",
    "list", "dict", "set", "tuple", "Any", "Optional", "Union", "List", "Dict",
    "async", "def", "class", "return", "await", "import", "from",
})


@dataclass(frozen=True)
class TraversalBudget:
    """Hard limits on how far interpretation will walk.

    Bounded traversal is a correctness property, not a performance one: an
    unbounded walk over a dense import graph reaches everything, and an answer
    that names every entrypoint names none.
    """

    max_depth: int = 4
    max_visited: int = 5000
    max_paths_per_entrypoint: int = 4


class ImpactInterpreter:
    """Interprets structural facts as reachable entrypoints.

    Construction is cheap and stateless; all the facts arrive at `interpret`.
    """

    def __init__(self, budget: TraversalBudget | None = None) -> None:
        self.budget = budget or TraversalBudget()

    # -- public API -------------------------------------------------------

    def interpret(
        self,
        changed_symbols: Iterable[dict[str, Any]],
        facts: StructuralFacts,
        *,
        repo: str | None = None,
    ) -> ImpactResult:
        """Find the entrypoints the changed symbols could reach.

        `changed_symbols` are mappings carrying at least `name`, and optionally
        `file` and `qualified_name` — the shape Sydes' change attribution
        already produces.
        """
        changed = [item for item in changed_symbols if item.get("name")]
        index = _FactIndex(facts, repo)
        result = ImpactResult()
        found: dict[str, AffectedEntrypoint] = {}
        truncated = False

        if not index.entrypoints:
            result.notes.append(
                "no entrypoint symbols were supplied by the backend; nothing "
                "could be reached even if the change is reachable in principle"
            )

        for symbol in changed:
            name = str(symbol["name"])
            reached = False

            for path, target in self._direct(symbol, index):
                self._record(found, target, name, path)
                reached = True

            for path, target, hit_limit in self._reachability(symbol, index):
                truncated = truncated or hit_limit
                if target is not None and path is not None:
                    self._record(found, target, name, path)
                    reached = True

            for path, target in self._decorator_references(symbol, index):
                self._record(found, target, name, path)
                reached = True

            for path, target in self._signature_references(symbol, index):
                self._record(found, target, name, path)
                reached = True

            if not reached:
                result.unresolved.append(
                    UnresolvedImpact(
                        repo=index.repo_of(symbol),
                        symbol=name,
                        reason="no_entrypoint_reached",
                        detail=(
                            "no route metadata, call path, usage reference, "
                            "decorator reference, or signature reference "
                            "connected this symbol to a known entrypoint"
                        ),
                    )
                )

        # Deterministic ordering: identical facts must produce an identical
        # report, or two runs of the same analysis cannot be compared.
        result.affected = sorted(
            found.values(), key=lambda item: (item.kind, item.label, item.qualified_name)
        )
        for entrypoint in result.affected:
            entrypoint.changed_symbols = sorted(set(entrypoint.changed_symbols))
            entrypoint.paths = sorted(
                entrypoint.paths, key=lambda path: (path.length, path.strategy, path.describe())
            )[: self.budget.max_paths_per_entrypoint]
        result.unresolved = sorted(result.unresolved, key=lambda item: item.symbol)
        result.completeness = (
            COMPLETENESS_TRUNCATED if truncated else COMPLETENESS_COMPLETE
        )
        result.metrics = {
            "changed_symbols": len(changed),
            "affected_entrypoints": len(result.affected),
            "http_entrypoints": len(result.http_entrypoints),
            "unresolved_symbols": len(result.unresolved),
            "known_entrypoints": len(index.entrypoints),
            "call_edges": len(facts.call_edges),
            "usage_edges": len(facts.usage_edges),
            "max_depth": self.budget.max_depth,
            "max_visited": self.budget.max_visited,
        }
        if truncated:
            result.notes.append(
                f"traversal hit a bound (depth {self.budget.max_depth}, "
                f"{self.budget.max_visited} nodes); reachable entrypoints "
                "beyond it are not listed"
            )
        return result

    # -- strategies -------------------------------------------------------

    def _direct(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath, dict[str, Any]]]:
        """DIRECT_ENTRYPOINT: the changed symbol *is* an entrypoint."""
        out = []
        for entry in index.entrypoints_named(str(symbol["name"]), symbol.get("file")):
            step = ImpactStep(
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                relation=RELATION_DIRECT,
                evidence=_route_evidence(entry),
            )
            out.append((ImpactPath((step,), STRATEGY_DIRECT_ENTRYPOINT), entry))
        return out

    def _reachability(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath | None, dict[str, Any] | None, bool]]:
        """CALL_REACHABILITY and USAGE_REACHABILITY.

        One bounded backward walk over both relationship kinds. They share a
        traversal because a real path can alternate — a changed class is *used*
        by a composing function which is *called* by a handler — and walking
        them separately would miss exactly those mixed paths.

        Only entrypoints terminate a path. Ordinary intermediate callers are
        recorded as steps, never reported as affected APIs in their own right.
        """
        start = str(symbol["name"])
        results: list[tuple[ImpactPath | None, dict[str, Any] | None, bool]] = []
        visited: set[str] = {start}
        queue: deque[tuple[str, tuple[ImpactStep, ...]]] = deque([(start, ())])
        hit_limit = False

        while queue:
            if len(visited) > self.budget.max_visited:
                hit_limit = True
                break
            current, trail = queue.popleft()
            if len(trail) >= self.budget.max_depth:
                # Reaching the depth bound is only a truncation if this node
                # still had unexplored inbound edges.
                if index.inbound(current):
                    hit_limit = True
                continue

            for relation, predecessor in index.inbound(current):
                name = predecessor["symbol"]
                if name in visited:
                    continue  # cycle guard
                visited.add(name)
                step = ImpactStep(
                    symbol=name,
                    qualified_name=predecessor.get("qualified_name", ""),
                    file=predecessor.get("file", ""),
                    relation=relation,
                    evidence=predecessor.get("evidence", ""),
                )
                extended = trail + (step,)
                entry = index.entrypoint_for(name, predecessor.get("qualified_name"))
                if entry is not None:
                    strategy = (
                        STRATEGY_CALL_REACHABILITY
                        if all(item.relation == RELATION_CALLS for item in extended)
                        else STRATEGY_USAGE_REACHABILITY
                    )
                    results.append((ImpactPath(extended, strategy), entry, False))
                    # An entrypoint terminates this path; anything reaching it
                    # from further out is a different, longer story.
                    continue

                # A symbol reached mid-traversal may itself be named in a
                # decorator, which is how a dependency composed inside another
                # dependency reaches its handlers. Checking only the originally
                # changed symbol would miss every composed case, because the
                # new symbol never appears in a decorator itself.
                for referrer in index.entrypoints_referencing_any(
                    _reference_names(name, predecessor)
                ):
                    reference_step = ImpactStep(
                        symbol=referrer["symbol"],
                        qualified_name=referrer.get("qualified_name", ""),
                        file=referrer.get("file", ""),
                        relation=RELATION_DECORATOR_REFERENCE,
                        evidence=_decorator_evidence(referrer.get("decorators", ""), name),
                    )
                    results.append((
                        ImpactPath(extended + (reference_step,),
                                   STRATEGY_DECORATOR_REFERENCE),
                        referrer,
                        False,
                    ))

                queue.append((name, extended))

        if hit_limit:
            results.append((None, None, True))
        return results

    def _decorator_references(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath, dict[str, Any]]]:
        """DECORATOR_REFERENCE: the changed symbol is named in a decorator.

        Generic by construction: it looks for the changed symbol's identifier
        inside whatever decorator text the backend captured, without knowing
        what any decorator means. A dependency declared in a decorator argument
        never produces a call edge, so this is the only structural trace of it.
        """
        name = str(symbol["name"])
        if name in _UNINFORMATIVE or len(name) < 3:
            return []
        out = []
        for entry in index.entrypoints_referencing(name):
            step = ImpactStep(
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                relation=RELATION_DECORATOR_REFERENCE,
                evidence=_decorator_evidence(entry.get("decorators", ""), name),
            )
            out.append((ImpactPath((step,), STRATEGY_DECORATOR_REFERENCE), entry))
        return out

    def _signature_references(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath, dict[str, Any]]]:
        """SIGNATURE_REFERENCE: a changed type named in an entrypoint signature.

        Conservative on purpose. It fires only when the backend actually
        recorded a signature containing the changed identifier — no ORM
        semantics are inferred, because CBM exposes none and inventing them
        would produce confident nonsense about persistence.
        """
        name = str(symbol["name"])
        if name in _UNINFORMATIVE or len(name) < 3:
            return []
        out = []
        for entry in index.entrypoints_with_signature_reference(name):
            step = ImpactStep(
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                relation=RELATION_SIGNATURE_REFERENCE,
                evidence=_signature_evidence(entry.get("signature", ""), name),
            )
            out.append((ImpactPath((step,), STRATEGY_SIGNATURE_REFERENCE), entry))
        return out

    # -- accumulation -----------------------------------------------------

    @staticmethod
    def _record(
        found: dict[str, AffectedEntrypoint],
        entry: dict[str, Any],
        changed_symbol: str,
        path: ImpactPath,
    ) -> None:
        key = entry.get("qualified_name") or entry["symbol"]
        entrypoint = found.get(key)
        if entrypoint is None:
            entrypoint = AffectedEntrypoint(
                repo=entry.get("repo", ""),
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                kind=_classify(entry),
                route_method=entry.get("route_method"),
                route_path=entry.get("route_path"),
            )
            found[key] = entrypoint
        entrypoint.changed_symbols.append(changed_symbol)
        if not any(existing.describe() == path.describe()
                   and existing.strategy == path.strategy
                   for existing in entrypoint.paths):
            entrypoint.paths.append(path)


def _classify(entry: dict[str, Any]) -> str:
    """An entrypoint's kind, only as far as the facts support.

    Route metadata makes it HTTP. Decorator text alone makes it *some* kind of
    declared entrypoint without saying which. Neither makes it unknown — and
    unknown is reported as unknown rather than assumed to be HTTP.
    """
    if entry.get("route_path") or entry.get("route_method"):
        return ENTRYPOINT_HTTP
    if entry.get("decorators"):
        return ENTRYPOINT_DECORATED
    return ENTRYPOINT_UNKNOWN


def _reference_names(symbol: str, node: dict[str, Any]) -> list[str]:
    """Names under which a reached symbol might be cited in a decorator.

    A dependency is usually declared by its class name while the symbol the
    graph reached is a method on that class, so the owning class is offered
    alongside the method's own name.
    """
    names = [symbol]
    qualified = str(node.get("qualified_name") or "")
    parts = qualified.split(".")
    if len(parts) >= 2 and parts[-2] and parts[-2] != symbol:
        names.append(parts[-2])
    return [name for name in names if name not in _UNINFORMATIVE and len(name) >= 3]


def _route_evidence(entry: dict[str, Any]) -> str:
    method = entry.get("route_method") or ""
    path = entry.get("route_path") or ""
    return f"{method} {path}".strip()


def _decorator_evidence(text: str, name: str) -> str:
    """The decorator fragment naming this symbol, for a reviewer to check."""
    for fragment in str(text).split("@"):
        if name in fragment:
            return ("@" + fragment.strip())[:200]
    return str(text)[:200]


def _signature_evidence(text: str, name: str) -> str:
    signature = str(text)
    position = signature.find(name)
    if position < 0:
        return signature[:200]
    return signature[max(0, position - 40): position + 60]


class _FactIndex:
    """Lookup structures over one `StructuralFacts`, built once per interpret."""

    def __init__(self, facts: StructuralFacts, repo: str | None) -> None:
        self.facts = facts
        self._repo = repo
        self.entrypoints: list[dict[str, Any]] = [
            entry for entry in facts.entrypoints
            if repo is None or entry.get("repo") == repo
        ]

        self._by_name: dict[str, list[dict[str, Any]]] = {}
        self._by_qualified: dict[str, dict[str, Any]] = {}
        for entry in self.entrypoints:
            self._by_name.setdefault(entry["symbol"], []).append(entry)
            qualified = entry.get("qualified_name")
            if qualified:
                self._by_qualified[qualified] = entry

        # Inbound adjacency, keyed by callee/used symbol name, combining both
        # relationship kinds so one traversal can alternate between them.
        self._inbound: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for edge in facts.call_edges:
            if repo is not None and edge.get("repo") != repo:
                continue
            self._inbound.setdefault(str(edge.get("callee_symbol")), []).append(
                (RELATION_CALLS, {
                    "symbol": str(edge.get("caller_symbol")),
                    "qualified_name": edge.get("caller_qualified_name", ""),
                    "file": edge.get("caller_file", ""),
                })
            )
        for edge in facts.usage_edges:
            if repo is not None and edge.get("repo") != repo:
                continue
            self._inbound.setdefault(str(edge.get("used_symbol")), []).append(
                (RELATION_USAGE, {
                    "symbol": str(edge.get("user_symbol")),
                    "qualified_name": edge.get("user_qualified_name", ""),
                    "file": edge.get("user_file", ""),
                })
            )
        # Stable adjacency order keeps traversal deterministic.
        for key, items in self._inbound.items():
            self._inbound[key] = sorted(
                items, key=lambda pair: (pair[0], pair[1]["symbol"], pair[1]["qualified_name"])
            )

    def repo_of(self, symbol: dict[str, Any]) -> str:
        return str(symbol.get("repo") or self._repo or "")

    def inbound(self, symbol: str) -> list[tuple[str, dict[str, Any]]]:
        return self._inbound.get(symbol, [])

    def entrypoints_named(self, name: str, file: str | None = None) -> list[dict[str, Any]]:
        """Entrypoints defined by this symbol name, narrowed by file if known."""
        candidates = self._by_name.get(name, [])
        if file:
            narrowed = [entry for entry in candidates if entry.get("file") == file]
            if narrowed:
                return narrowed
        return candidates

    def entrypoint_for(self, name: str, qualified: str | None) -> dict[str, Any] | None:
        if qualified and qualified in self._by_qualified:
            return self._by_qualified[qualified]
        candidates = self._by_name.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def entrypoints_referencing(self, name: str) -> list[dict[str, Any]]:
        """Entrypoints whose captured decorator text names this identifier."""
        out = []
        for entry in self.entrypoints:
            text = entry.get("decorators")
            if not text:
                continue
            if name in _IDENTIFIER_RE.findall(str(text)):
                out.append(entry)
        return out

    def entrypoints_referencing_any(self, names: list[str]) -> list[dict[str, Any]]:
        """Entrypoints whose decorator text names any of these identifiers."""
        if not names:
            return []
        seen: dict[str, dict[str, Any]] = {}
        for name in names:
            for entry in self.entrypoints_referencing(name):
                seen.setdefault(entry.get("qualified_name") or entry["symbol"], entry)
        return [seen[key] for key in sorted(seen)]

    def entrypoints_with_signature_reference(self, name: str) -> list[dict[str, Any]]:
        out = []
        for entry in self.entrypoints:
            text = entry.get("signature")
            if not text:
                continue
            if name in _IDENTIFIER_RE.findall(str(text)):
                out.append(entry)
        return out
