"""The CBM structural fact layer for Track A: one API over every relation family we use.

Ordinary traversal reads CALLS/USAGE through the bounded graph slice. When it stalls at a
framework boundary, `structural_enrichment` asks this layer for the other families CBM
0.11 knows, each kept as what it is:

* `relations(seeds)` -- every relation of a Track A type touching the seeds, both
  directions, with its properties, as `Relation`s: CALLS (explicit invocation; args, line,
  confidence, resolution strategy), CALL_REFERENCE (a callable referenced/passed), USAGE
  (broader use), DECORATES (decorator text), HANDLES (handler -> Route), HTTP_CALLS (route
  declarations and outbound HTTP), CONFIGURES (config/env access), INHERITS / IMPLEMENTS /
  OVERRIDE, THROWS / RAISES, WRITES, DATA_FLOWS, DEPENDS_ON, ASYNC_CALLS.
* `direct(left, right)` -- any relation of any type directly between two symbol sets.
* `calls_path(sources, targets)` -- a bounded CALLS path (1..N hops): `trace_path`'s
  answer, batched for many pairs, with no LLM in between.
* `search(pattern, ...)` -- CBM's own source search: the enclosing symbol, its span and
  its source, for facts the graph does not hold (a call's receiver, an argument value).
* `capabilities()` -- the edge types this project's graph actually contains
  (`get_graph_schema`), so a family the installed CBM never emits is not queried and its
  absence is reported as a capability gap, not as "no relation".

Families not used here, and why (measured on CBM 0.11.0): TESTS (test mapping belongs to
Test Evidence, which this layer does not change); SEMANTICALLY_RELATED, SIMILAR_TO,
FILE_CHANGES_WITH (similarity and co-change history, not structure); DEFINES /
DEFINES_METHOD / CONTAINS_* / IMPORTS (already read through the symbol and import sweeps);
`is_entry_point` (false for every route handler in all three studied repositories);
`detect_changes` (its changed files equal the diff; its inbound impact found only a test
module where Sydes' attribution finds the changed symbols); `get_architecture` (overview is
a node count); `trace_path` itself (subsumed by `calls_path` plus the graph slice: same
CALLS-only answer, one request for many pairs).

Measured CBM 0.11.0 limits the callers work around rather than paper over: no HANDLES /
Route for a route whose decorator argument is an expression (NestJS `@Delete(routes.x)`);
HANDLES paths omit an `include_router` mount prefix; DECORATES keeps one edge per decorator
name (a repeated `@ApiResponse` survives only in the decorator sweep); CONFIGURES records
configuration keys and environment access, never a registration; overloads collapse to one
symbol, so a search hit inside one is placed in the class; INHERITS can resolve to a node in
a configuration file; a `search_code` excerpt of a large symbol covers only its first match;
`query_graph` cannot `ORDER BY type(r)`.

Everything is batched (one request per family per wave, all seeds together), cached per
session (relations per seed; paths, searches and schema per question) and counted. A failed
query is noted and costs only its fact.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: The relation families Track A reasons about, in the order a reader would want them.
TRACK_A_RELATIONS = (
    "CALLS", "CALL_REFERENCE", "USAGE", "DECORATES", "HANDLES", "HTTP_CALLS", "CONFIGURES",
    "INHERITS", "IMPLEMENTS", "OVERRIDE", "THROWS", "RAISES", "WRITES", "DATA_FLOWS",
    "DEPENDS_ON", "ASYNC_CALLS",
)
MAX_SEARCHES = 24
PATH_HOPS = 5


@dataclass(frozen=True)
class Relation:
    source: str
    source_label: str
    source_file: str
    type: str
    target: str
    target_label: str
    target_file: str
    props: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @staticmethod
    def from_row(row: list[str]) -> Relation:
        try:
            props = json.loads(row[7]) if row[7] else {}
        except (ValueError, TypeError):
            props = {}
        return Relation(row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                        props if isinstance(props, dict) else {})


class CBMFacts:
    """Structural facts for one indexed repository, batched and cached for the session."""

    def __init__(self, client: Any, project: str) -> None:
        self.client = client
        self.project = project
        self._capabilities: set[str] | None = None
        self._relations: dict[str, list[Relation]] = {}
        self._direct: dict[tuple[frozenset[str], frozenset[str]], list[list[str]]] = {}
        self._paths: dict[tuple[frozenset[str], frozenset[str], int], list[list[str]]] = {}
        self._searches: dict[tuple[str, str | None, str], dict[str, Any] | None] = {}
        self.requests: Counter[str] = Counter()
        self.cache_hits: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.notes: list[str] = []

    # -- capability -------------------------------------------------------

    def capabilities(self) -> set[str]:
        if self._capabilities is not None:
            self.cache_hits["schema"] += 1
        return self._schema()

    def _schema(self) -> set[str]:
        if self._capabilities is None:
            self.requests["schema"] += 1
            try:
                self._capabilities = set(self.client.graph_capabilities(self.project).get("edge_types", {}))
            except Exception as exc:  # noqa: BLE001 - a missing schema must not stop the analysis
                self.notes.append(f"graph schema unavailable ({exc}); every relation family queried")
                self._capabilities = set(TRACK_A_RELATIONS)
        return self._capabilities

    def supports(self, relation_type: str) -> bool:
        return relation_type in self._schema()

    # -- relations --------------------------------------------------------

    def relations(self, seeds: list[str]) -> dict[str, list[Relation]]:
        """Relations touching each seed (cached per seed; only unseen seeds are fetched,
        all of them in one request)."""
        wanted = sorted({s for s in seeds if s})
        missing = [s for s in wanted if s not in self._relations]
        if len(missing) < len(wanted):
            self.cache_hits["relations"] += len(wanted) - len(missing)
        if missing:
            types = [t for t in TRACK_A_RELATIONS if self.supports(t)]
            self.requests["relations"] += 1
            rows = self._guard("relations", lambda: self.client.relations(self.project, missing, types), []) if types else []
            for seed in missing:
                self._relations[seed] = []
            seen: set[tuple[str, ...]] = set()
            for row in rows:
                relation = Relation.from_row(row)
                identity = (*row[:7], json.dumps(relation.props, sort_keys=True))
                if identity in seen:  # the same edge reached from both of its seeds
                    continue
                seen.add(identity)
                for end in (relation.source, relation.target):
                    if end in self._relations:
                        self._relations[end].append(relation)
        return {seed: list(self._relations.get(seed, [])) for seed in wanted}

    # -- paths ------------------------------------------------------------

    def direct(self, left: list[str], right: list[str]) -> list[list[str]]:
        key = (frozenset(left), frozenset(right))
        if key in self._direct:
            self.cache_hits["direct"] += 1
        else:
            self.requests["direct"] += 1
            self._direct[key] = self._guard(
                "direct relation", lambda: self.client.direct_relations(self.project, sorted(left), sorted(right)), [])
        return self._direct[key]

    def calls_path(self, sources: list[str], targets: list[str], max_hops: int = PATH_HOPS) -> list[list[str]]:
        key = (frozenset(sources), frozenset(targets), max_hops)
        if key in self._paths:
            self.cache_hits["calls_path"] += 1
        else:
            self.requests["calls_path"] += 1
            self._paths[key] = self._guard("CALLS path", lambda: self.client.calls_paths(
                self.project, sorted(sources), sorted(targets), max_hops=max_hops), [])
        return self._paths[key]

    # -- source -----------------------------------------------------------

    def search(self, pattern: str, path_filter: str | None = None, mode: str = "full") -> dict[str, Any] | None:
        """CBM source search, capped at MAX_SEARCHES per session; None once the cap is
        reached (the caller then says the fact was not looked up)."""
        key = (pattern, path_filter, mode)
        if key in self._searches:
            self.cache_hits["search"] += 1
            return self._searches[key]
        if self.requests["search"] >= MAX_SEARCHES:
            self.notes.append(f"source search budget ({MAX_SEARCHES}) reached; `{pattern}` not looked up")
            return None
        self.requests["search"] += 1
        self._searches[key] = self._guard("source search", lambda: self.client.search_code(
            self.project, pattern, path_filter=path_filter, mode=mode), None)
        return self._searches[key]

    def _guard(self, family: str, call: Any, fallback: Any) -> Any:
        """A CBM error costs the fact, never the analysis: noted, and the caller sees
        "not looked up" (None) or no relations, never a fabricated answer."""
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - a failed query costs its fact, not the run
            self.errors[family] += 1
            self.notes.append(f"CBM {family} query failed ({_first_line(exc)})")
            return fallback

    def stats(self) -> dict[str, Any]:
        return {"requests": dict(self.requests), "cache_hits": dict(self.cache_hits),
                "errors": dict(self.errors), "total_requests": sum(self.requests.values())}


def _first_line(exc: Exception, limit: int = 160) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
