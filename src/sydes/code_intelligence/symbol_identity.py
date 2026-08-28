"""Resolving a display symbol name to CBM's canonical graph identity.

Real-run traces of the bounded graph-slice path showed `node_count: 0`,
`edge_count: 0`, `truncated: false` on repositories whose changed symbols
plainly had callers. The cause was an identity mismatch, not a traversal
bug:

    Sydes seed          CBM graph node
    ------------------  ---------------------------------------------
    MergePullRequest    code.example.io/svc/pull.MergePullRequest
    Resource.getThing   com.example.api.Resource.getThing

Sydes' `qualified_name` is a deliberately short *display* form (bare class
+ method, or just the short name for a plain function). CBM's edges are
keyed by a fully module/package-qualified name. An exact `IN` match between
the two can only ever return zero rows.

This module resolves the former to the latter, using only the symbol index
`build_or_update` already loaded — no extra CBM call, no LLM, no fuzzy
string distance. Every rule below is an exact structural match; the one
suffix rule is boundary-anchored and applied only when it is unique.

An unresolved seed stays unresolved. That is a distinct condition from "a
resolved seed with no edges", and the two must never be conflated: the
first says Sydes could not look, the second says it looked and found
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SeedRequest",
    "SeedResolution",
    "build_symbol_identity_index",
    "resolve_seed_identities",
]

#: How many canonical identities one ambiguous seed may contribute. An
#: overloaded or repeated name is legitimately several graph nodes, but a
#: name repeated across a large repository must not expand the seed set
#: without bound.
MAX_IDENTITIES_PER_SEED = 4


@dataclass(frozen=True)
class SeedRequest:
    """One thing to seed a graph slice from, with whatever identity evidence
    the caller happens to have. `file` is the strongest disambiguator and is
    supplied for both changed symbols and route handlers."""

    name: str
    file: str | None = None
    #: Sydes' display-qualified form (`Class.method`), when it has one.
    qualified_name: str | None = None


@dataclass
class SeedResolution:
    """Canonical identities for a seed set, plus what could not be resolved."""

    canonical: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    #: seed label -> the identities it expanded to, when more than one.
    ambiguous: dict[str, list[str]] = field(default_factory=dict)

    @property
    def resolved_count(self) -> int:
        return len(self.canonical)


@dataclass
class _SymbolIdentityIndex:
    """Lookup tables over the symbol index, built once per resolution."""

    #: Every canonical name CBM reported, for "the seed is already canonical".
    canonical_names: set[str] = field(default_factory=set)
    #: (file, short_name) -> canonical names.
    by_file_name: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    #: (parent_class, short_name) -> canonical names.
    by_parent_name: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    #: short_name -> canonical names, for the bounded unique-suffix rule.
    by_short_name: dict[str, list[str]] = field(default_factory=dict)


def build_symbol_identity_index(
    symbol_index: dict[str, Any], repo: str | None = None,
) -> _SymbolIdentityIndex:
    """Index the canonical names already present in `StructuralFacts
    .symbol_index`. Symbols the backend gave no canonical name are skipped:
    an absent identity cannot be guessed into existence."""
    index = _SymbolIdentityIndex()
    for repo_index in symbol_index.get("repos", []) or []:
        if repo is not None and repo_index.get("repo") not in (None, repo):
            continue
        for file_item in repo_index.get("files", []) or []:
            path = str(file_item.get("path") or "")
            for symbol in file_item.get("symbols", []) or []:
                canonical = symbol.get("cbm_qualified_name")
                if not isinstance(canonical, str) or not canonical:
                    continue
                name = str(symbol.get("name") or "")
                index.canonical_names.add(canonical)
                if path and name:
                    index.by_file_name.setdefault((path, name), []).append(canonical)
                parent = symbol.get("parent")
                if parent and name:
                    index.by_parent_name.setdefault((str(parent), name), []).append(canonical)
                if name:
                    index.by_short_name.setdefault(name, []).append(canonical)
    return index


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _unique_suffix_matches(index: _SymbolIdentityIndex, seed_label: str) -> list[str]:
    """Canonical names ending in `.<seed_label>` (or exactly it).

    Boundary-anchored on purpose: a bare `endswith` would match
    `doMergePullRequest` for a seed of `MergePullRequest`. Bounded by the
    already-built short-name table rather than scanning the graph, so this
    adds no query and cannot reintroduce repository-wide pagination.
    """
    tail = seed_label.rsplit(".", 1)[-1]
    candidates = index.by_short_name.get(tail, [])
    if not candidates:
        return []
    suffix = f".{seed_label}"
    matched = [
        name for name in candidates
        if name == seed_label or name.endswith(suffix)
    ]
    return _dedupe(matched)


def resolve_seed_identities(
    symbol_index: dict[str, Any],
    seeds: list[SeedRequest],
    *,
    repo: str | None = None,
    max_identities_per_seed: int = MAX_IDENTITIES_PER_SEED,
) -> SeedResolution:
    """Map display seeds onto CBM's canonical graph identities.

    Rules, strongest evidence first — the first rule that matches wins, and
    no rule falls through to a weaker one once a match exists:

    1. the seed is already a canonical name;
    2. exact (file, short name) — the strongest disambiguator, since two
       symbols sharing a name in one file is rare and a changed symbol
       always knows its file;
    3. exact (parent class, method name), for a `Class.method` display form;
    4. a unique, boundary-anchored suffix match.

    Rule 4 is applied only when it resolves to exactly one identity. A seed
    that matches several candidates under it is recorded as ambiguous and
    left unresolved rather than arbitrarily narrowed — picking one would
    silently attribute a change to the wrong symbol.

    A seed that matches several identities under rules 2 or 3 keeps them
    all (bounded): those are genuinely the same declaration site, e.g. an
    overload set, and dropping them would lose real structure.
    """
    index = build_symbol_identity_index(symbol_index, repo=repo)
    resolution = SeedResolution()
    seen: set[str] = set()

    def _accept(label: str, names: list[str]) -> None:
        kept = names[:max_identities_per_seed]
        if len(names) > 1:
            resolution.ambiguous[label] = kept
        for name in kept:
            if name not in seen:
                seen.add(name)
                resolution.canonical.append(name)

    for seed in seeds:
        label = seed.qualified_name or seed.name
        if not label:
            continue

        # 1. Already canonical.
        for candidate in (seed.qualified_name, seed.name):
            if candidate and candidate in index.canonical_names:
                _accept(label, [candidate])
                break
        else:
            # 2. Exact file + short name.
            matches: list[str] = []
            if seed.file and seed.name:
                matches = _dedupe(index.by_file_name.get((seed.file, seed.name), []))

            # 3. Exact parent class + method name, from a `Class.method` form.
            if not matches and seed.qualified_name and "." in seed.qualified_name:
                parent, _, method = seed.qualified_name.rpartition(".")
                matches = _dedupe(index.by_parent_name.get((parent, method), []))

            # 4. Bounded, uniqueness-checked suffix match.
            if not matches:
                suffix_matches = _unique_suffix_matches(index, label)
                if len(suffix_matches) == 1:
                    matches = suffix_matches
                elif len(suffix_matches) > 1:
                    # Genuinely ambiguous under the weakest rule: record it,
                    # resolve nothing.
                    resolution.ambiguous[label] = suffix_matches[:max_identities_per_seed]

            if matches:
                _accept(label, matches)
            else:
                resolution.unresolved.append(label)

    return resolution
