"""Canonical CBM graph-identity resolution for graph-slice seeds.

Pins the fix for the observed recall bug: real runs reported
`node_count: 0, edge_count: 0, truncated: false` because Sydes seeded
bounded edge queries with its own short/display symbol names while CBM's
edges are keyed by fully module/package-qualified names, so exact `IN`
matching could never hit.
"""

from __future__ import annotations

from sydes.code_intelligence.symbol_identity import (
    SEED_KIND_CHANGED,
    SEED_KIND_ROUTE,
    SeedRequest,
    resolve_seed_identities,
)


def _symbol(name: str, *, canonical: str | None = None, parent: str | None = None) -> dict:
    symbol: dict = {"name": name, "kind": "function"}
    if canonical:
        symbol["cbm_qualified_name"] = canonical
    if parent:
        symbol["parent"] = parent
        symbol["kind"] = "class_method"
        symbol["qualified_name"] = f"{parent}.{name}"
    return symbol


def _index(files: dict[str, list[dict]], repo: str = "app") -> dict:
    return {
        "repos": [{
            "repo": repo,
            "files": [
                {"path": path, "symbols": symbols} for path, symbols in files.items()
            ],
        }]
    }


# --------------------------------------------------------------------------
# 1-3. The resolution rules
# --------------------------------------------------------------------------


def test_a_short_changed_symbol_resolves_to_the_canonical_qualified_name() -> None:
    """The exact Gitea shape: the diff yields `MergePullRequest`, CBM's graph
    node is `code.example.io/svc/pull.MergePullRequest`."""
    index = _index({"services/pull/merge.go": [
        _symbol("MergePullRequest", canonical="code.example.io/svc/pull.MergePullRequest"),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="MergePullRequest", file="services/pull/merge.go"),
    ])

    assert resolution.canonical == ["code.example.io/svc/pull.MergePullRequest"]
    assert resolution.unresolved == []


def test_a_class_method_symbol_resolves_to_its_canonical_name() -> None:
    """The exact Fineract shape: Sydes' display form is the bare
    `Resource.method`, CBM's is the fully package-qualified name."""
    canonical = "com.example.api.MakercheckersApiResource.getExtraCriteria"
    index = _index({"api/Makercheckers.java": [
        _symbol("getExtraCriteria", canonical=canonical, parent="MakercheckersApiResource"),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(
            name="getExtraCriteria", file="api/Makercheckers.java",
            qualified_name="MakercheckersApiResource.getExtraCriteria",
        ),
    ])

    assert resolution.canonical == [canonical]


def test_a_seed_that_is_already_canonical_is_accepted_unchanged() -> None:
    canonical = "code.example.io/svc/pull.MergePullRequest"
    index = _index({"merge.go": [_symbol("MergePullRequest", canonical=canonical)]})

    resolution = resolve_seed_identities(index, [SeedRequest(name=canonical)])

    assert resolution.canonical == [canonical]


def test_a_class_qualified_seed_resolves_without_a_file_hint() -> None:
    """Rule 3: parent class + method, when the file is unknown."""
    canonical = "com.example.api.Resource.handle"
    index = _index({"api/Resource.java": [
        _symbol("handle", canonical=canonical, parent="Resource"),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="handle", qualified_name="Resource.handle"),
    ])

    assert resolution.canonical == [canonical]


# --------------------------------------------------------------------------
# 4-6. Ambiguity and unresolved seeds
# --------------------------------------------------------------------------


def test_duplicate_short_names_in_different_files_are_not_conflated() -> None:
    """The file hint is the disambiguator: two `handle`s must not merge."""
    index = _index({
        "a/svc.go": [_symbol("handle", canonical="mod/a.handle")],
        "b/svc.go": [_symbol("handle", canonical="mod/b.handle")],
    })

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="handle", file="a/svc.go"),
    ])

    assert resolution.canonical == ["mod/a.handle"]
    assert "mod/b.handle" not in resolution.canonical


def test_an_ambiguous_suffix_match_is_never_silently_chosen() -> None:
    """Without a file hint, a name in two places resolves to neither —
    picking one would attribute the change to the wrong symbol."""
    index = _index({
        "a/svc.go": [_symbol("handle", canonical="mod/a.handle")],
        "b/svc.go": [_symbol("handle", canonical="mod/b.handle")],
    })

    resolution = resolve_seed_identities(index, [SeedRequest(name="handle")])

    assert resolution.canonical == []
    # Unresolved AND explained: nothing was seeded from it, and `ambiguous`
    # records why, so the caller's uncertainty reporting is accurate rather
    # than silently treating the seed as explored.
    assert resolution.unresolved == ["handle"]
    assert set(resolution.ambiguous["handle"]) == {"mod/a.handle", "mod/b.handle"}


def test_an_unknown_seed_stays_unresolved_rather_than_invented() -> None:
    index = _index({"svc.go": [_symbol("known", canonical="mod.known")]})

    resolution = resolve_seed_identities(index, [SeedRequest(name="absent")])

    assert resolution.canonical == []
    assert resolution.unresolved == ["absent"]


def test_a_symbol_without_a_canonical_name_is_not_usable_as_a_seed() -> None:
    """A backend that reported no canonical identity cannot be resolved
    against — the identity is absent, not guessable."""
    index = _index({"svc.go": [_symbol("thing")]})  # no cbm_qualified_name

    resolution = resolve_seed_identities(index, [SeedRequest(name="thing", file="svc.go")])

    assert resolution.canonical == []
    assert resolution.unresolved == ["thing"]


def test_the_suffix_rule_is_boundary_anchored_not_a_substring_match() -> None:
    """`doMergePullRequest` must not satisfy a seed of `MergePullRequest`."""
    index = _index({"svc.go": [
        _symbol("doMergePullRequest", canonical="mod/pull.doMergePullRequest"),
    ]})

    resolution = resolve_seed_identities(index, [SeedRequest(name="MergePullRequest")])

    assert resolution.canonical == []
    assert resolution.unresolved == ["MergePullRequest"]


def test_overloads_in_one_file_keep_every_defensible_identity() -> None:
    index = _index({"api/R.java": [
        _symbol("handle", canonical="com.example.R.handle#1", parent="R"),
        _symbol("handle", canonical="com.example.R.handle#2", parent="R"),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="handle", file="api/R.java"),
    ])

    assert set(resolution.canonical) == {"com.example.R.handle#1", "com.example.R.handle#2"}
    assert "handle" in resolution.ambiguous


def test_the_identity_count_per_seed_is_bounded() -> None:
    index = _index({"api/R.java": [
        _symbol("handle", canonical=f"com.example.R.handle#{i}", parent="R")
        for i in range(50)
    ]})

    resolution = resolve_seed_identities(
        index, [SeedRequest(name="handle", file="api/R.java")], max_identities_per_seed=3,
    )

    assert len(resolution.canonical) == 3


# --------------------------------------------------------------------------
# 7-8. Route handlers and batching
# --------------------------------------------------------------------------


def test_a_route_handler_seed_canonicalizes_the_same_way() -> None:
    """Route handlers arrive as `repo.MergePullRequest`-style display names
    and are as unmatchable as a short name until resolved."""
    canonical = "code.example.io/routers/repo.MergePullRequest"
    index = _index({"routers/repo/pull.go": [
        _symbol("MergePullRequest", canonical=canonical),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="MergePullRequest", file="routers/repo/pull.go",
                    qualified_name="repo.MergePullRequest"),
    ])

    assert resolution.canonical == [canonical]


def test_multiple_seeds_canonicalize_and_deduplicate_together() -> None:
    index = _index({
        "a.go": [_symbol("alpha", canonical="mod/a.alpha")],
        "b.go": [_symbol("beta", canonical="mod/b.beta")],
    })

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="alpha", file="a.go"),
        SeedRequest(name="beta", file="b.go"),
        SeedRequest(name="alpha", file="a.go"),  # duplicate
    ])

    assert resolution.canonical == ["mod/a.alpha", "mod/b.beta"]


def test_an_empty_symbol_index_resolves_nothing_and_claims_nothing() -> None:
    resolution = resolve_seed_identities({}, [SeedRequest(name="anything")])

    assert resolution.canonical == []
    assert resolution.unresolved == ["anything"]


# --------------------------------------------------------------------------
# Auxiliary route aliases are accounted for separately
# --------------------------------------------------------------------------


def test_an_unresolved_route_alias_is_not_counted_as_an_unresolved_change() -> None:
    """The observed Gitea shape: all 10 changed symbols resolved, while 3
    auxiliary route aliases (`repo.MergePullRequest` and friends) did not.
    Conflating the two made a healthy run read as a recall failure."""
    canonical = "code.example.io/services/pull.MergePullRequest"
    index = _index({"services/pull/merge.go": [
        _symbol("MergePullRequest", canonical=canonical),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="MergePullRequest", file="services/pull/merge.go",
                    kind=SEED_KIND_CHANGED),
        # The route table declares the handler in a different file, and that
        # handler is a genuinely different Go function.
        SeedRequest(name="MergePullRequest", file="routers/web/web.go",
                    qualified_name="repo.MergePullRequest", kind=SEED_KIND_ROUTE),
    ])

    assert resolution.canonical == [canonical]
    assert resolution.unresolved_changed == []
    assert resolution.unresolved_auxiliary == ["repo.MergePullRequest"]


def test_an_unresolved_changed_symbol_is_still_reported_as_such() -> None:
    """The alias split must never hide a real hole in the change itself."""
    index = _index({"svc.go": [_symbol("known", canonical="mod.known")]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="missing", file="svc.go", kind=SEED_KIND_CHANGED),
        SeedRequest(name="alias", file="routes.go", kind=SEED_KIND_ROUTE),
    ])

    assert resolution.unresolved_changed == ["missing"]
    assert resolution.unresolved_auxiliary == ["alias"]
    assert set(resolution.unresolved) == {"missing", "alias"}


def test_a_route_alias_that_does_resolve_is_not_reported_unresolved() -> None:
    canonical = "code.example.io/routers/web/repo.MergePullRequest"
    index = _index({"routers/web/repo/pull.go": [
        _symbol("MergePullRequest", canonical=canonical),
    ]})

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="MergePullRequest", file="routers/web/repo/pull.go",
                    qualified_name="repo.MergePullRequest", kind=SEED_KIND_ROUTE),
    ])

    assert resolution.canonical == [canonical]
    assert resolution.unresolved_auxiliary == []


def test_seed_kind_never_changes_which_identity_is_resolved() -> None:
    """Kind affects reporting only. The same seed must resolve identically
    whichever bucket it is in."""
    canonical = "mod.thing"
    index = _index({"svc.go": [_symbol("thing", canonical=canonical)]})

    as_changed = resolve_seed_identities(
        index, [SeedRequest(name="thing", file="svc.go", kind=SEED_KIND_CHANGED)])
    as_route = resolve_seed_identities(
        index, [SeedRequest(name="thing", file="svc.go", kind=SEED_KIND_ROUTE)])

    assert as_changed.canonical == as_route.canonical == [canonical]


def test_an_ambiguous_auxiliary_alias_remains_unresolved() -> None:
    """Ambiguity rules are untouched: a route alias matching two symbols is
    still refused, not guessed."""
    index = _index({
        "a/svc.go": [_symbol("handle", canonical="mod/a.handle")],
        "b/svc.go": [_symbol("handle", canonical="mod/b.handle")],
    })

    resolution = resolve_seed_identities(index, [
        SeedRequest(name="handle", file="routes.go", kind=SEED_KIND_ROUTE),
    ])

    assert resolution.canonical == []
    assert resolution.unresolved_auxiliary == ["handle"]
    assert set(resolution.ambiguous["handle"]) == {"mod/a.handle", "mod/b.handle"}
