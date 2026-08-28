"""Change-local seed selection for the bounded graph slice.

Real runs on a large repository requested 249 seeds for a ~10-symbol
change: seeding took every route handler `discover_endpoints` returned,
which is the whole repository's route set, not the part of it this change
could reach. Hop 1 then filled a full page with structure belonging to no
part of the change (`truncation_reason: "calls hop 1 filled a full page"`).

These tests pin the corrected policy: changed symbols are primary and never
dropped; route handlers are auxiliary, kept only on deterministic evidence
tying them to the change, deduplicated, and bounded.
"""

from __future__ import annotations

from types import SimpleNamespace

from sydes.verify.analyzer import (
    MAX_AUXILIARY_ROUTE_SEEDS,
    _select_graph_slice_seeds,
)


def _changed_symbol(name: str, file: str, qualified: str | None = None):
    return SimpleNamespace(name=name, file=file, qualified_name=qualified)


def _route(handler: str, file: str):
    return SimpleNamespace(handler=handler, file=file)


def _change(symbols, files=None):
    return SimpleNamespace(
        symbols=symbols,
        files=[SimpleNamespace(path=path) for path in (files or [])],
    )


def _routes(*items):
    return SimpleNamespace(routes=list(items))


# --------------------------------------------------------------------------
# 1-4. What is and is not seeded
# --------------------------------------------------------------------------


def test_changed_symbols_are_always_included() -> None:
    change = _change([
        _changed_symbol("alpha", "svc/a.go"),
        _changed_symbol("beta", "svc/b.go"),
    ], files=["svc/a.go", "svc/b.go"])

    selection = _select_graph_slice_seeds(change, _routes(), set())

    assert {seed.name for seed in selection.seeds} == {"alpha", "beta"}
    assert selection.changed_symbol_count == 2


def test_unrelated_repository_wide_route_handlers_are_excluded() -> None:
    """The core fix. A repository's other 300 routes have nothing to do with
    this change and must not consume slice budget."""
    change = _change([_changed_symbol("alpha", "svc/a.go")], files=["svc/a.go"])
    routes = _routes(*[
        _route(f"repo.Handler{i}", f"routers/web/r{i}.go") for i in range(300)
    ])

    selection = _select_graph_slice_seeds(change, routes, set())

    assert selection.route_handler_count == 0
    assert len(selection.seeds) == 1
    assert selection.seeds[0].name == "alpha"


def test_a_route_handler_in_a_changed_file_is_included() -> None:
    change = _change([_changed_symbol("alpha", "svc/a.go")], files=["routers/web/pull.go"])
    routes = _routes(
        _route("repo.MergePullRequest", "routers/web/pull.go"),
        _route("repo.Unrelated", "routers/web/other.go"),
    )

    selection = _select_graph_slice_seeds(change, routes, set())

    names = {seed.name for seed in selection.seeds}
    assert "MergePullRequest" in names
    assert "Unrelated" not in names


def test_a_route_handler_that_is_itself_a_changed_symbol_is_included() -> None:
    change = _change(
        [_changed_symbol("MergePullRequest", "routers/web/pull.go")],
        files=["routers/web/pull.go"],
    )
    routes = _routes(_route("repo.MergePullRequest", "routers/web/pull.go"))

    selection = _select_graph_slice_seeds(change, routes, set())

    assert "MergePullRequest" in {seed.name for seed in selection.seeds}


def test_a_route_handler_linked_by_reverse_reach_is_included() -> None:
    """`_candidate_route_files` is the deterministic evidence that a route
    file can reach the change; a handler there is genuinely relevant."""
    change = _change([_changed_symbol("alpha", "svc/a.go")], files=["svc/a.go"])
    routes = _routes(
        _route("repo.Reaches", "routers/web/reaches.go"),
        _route("repo.Unrelated", "routers/web/other.go"),
    )

    selection = _select_graph_slice_seeds(change, routes, {"routers/web/reaches.go"})

    names = {seed.name for seed in selection.seeds}
    assert "Reaches" in names
    assert "Unrelated" not in names


# --------------------------------------------------------------------------
# 5-6. Deduplication
# --------------------------------------------------------------------------


def test_duplicate_aliases_of_one_symbol_collapse_to_one_seed() -> None:
    """A changed symbol and the route handler naming it, in the same file,
    are one logical seed — not two."""
    change = _change(
        [_changed_symbol("MergePullRequest", "routers/web/pull.go")],
        files=["routers/web/pull.go"],
    )
    routes = _routes(
        _route("repo.MergePullRequest", "routers/web/pull.go"),
        _route("MergePullRequest", "routers/web/pull.go"),
    )

    selection = _select_graph_slice_seeds(change, routes, set())

    merge_seeds = [s for s in selection.seeds if s.name == "MergePullRequest"]
    assert len(merge_seeds) == 1


def test_same_name_symbols_in_different_files_stay_distinct() -> None:
    """Deduplication must never merge two genuinely different symbols."""
    change = _change([
        _changed_symbol("handle", "svc/a.go"),
        _changed_symbol("handle", "svc/b.go"),
    ], files=["svc/a.go", "svc/b.go"])

    selection = _select_graph_slice_seeds(change, _routes(), set())

    assert len(selection.seeds) == 2
    assert {seed.file for seed in selection.seeds} == {"svc/a.go", "svc/b.go"}


# --------------------------------------------------------------------------
# 7-8. Priority and the cap
# --------------------------------------------------------------------------


def test_changed_symbols_take_priority_over_auxiliary_route_seeds() -> None:
    """Every changed symbol survives even when far more routes are relevant
    than the auxiliary cap allows."""
    changed = [_changed_symbol(f"c{i}", f"svc/c{i}.go") for i in range(40)]
    change = _change(changed, files=[f"svc/c{i}.go" for i in range(40)])
    routes = _routes(*[
        _route(f"repo.H{i}", "svc/c0.go")
        for i in range(MAX_AUXILIARY_ROUTE_SEEDS * 3)
    ])

    selection = _select_graph_slice_seeds(change, routes, set())

    assert selection.changed_symbol_count == 40
    for symbol in changed:
        assert any(seed.name == symbol.name for seed in selection.seeds)


def test_the_cap_drops_only_lower_priority_auxiliary_seeds() -> None:
    change = _change([_changed_symbol("alpha", "svc/a.go")], files=["routers/web/r.go"])
    routes = _routes(*[
        _route(f"repo.H{i}", "routers/web/r.go")
        for i in range(MAX_AUXILIARY_ROUTE_SEEDS + 25)
    ])

    selection = _select_graph_slice_seeds(change, routes, set())

    assert selection.changed_symbol_count == 1
    assert selection.route_handler_count == MAX_AUXILIARY_ROUTE_SEEDS
    assert selection.dropped_auxiliary_count == 25
    # The changed symbol is never what gets dropped.
    assert any(seed.name == "alpha" for seed in selection.seeds)


def test_dropping_auxiliary_seeds_is_recorded_not_silent() -> None:
    change = _change([_changed_symbol("alpha", "svc/a.go")], files=["routers/web/r.go"])
    routes = _routes(*[
        _route(f"repo.H{i}", "routers/web/r.go")
        for i in range(MAX_AUXILIARY_ROUTE_SEEDS + 5)
    ])

    selection = _select_graph_slice_seeds(change, routes, set())

    assert selection.dropped_auxiliary_count == 5


# --------------------------------------------------------------------------
# 10. Route-flow tracing keeps the handler seed it needs
# --------------------------------------------------------------------------


def test_route_tracing_still_gets_the_handler_for_a_changed_route() -> None:
    """Route tracing walks OUTBOUND from a handler, so a route whose own
    file the change touched must still be seeded — narrowing must not
    silently disable flow tracing."""
    change = _change(
        [_changed_symbol("MergePullRequest", "routers/web/pull.go")],
        files=["routers/web/pull.go"],
    )
    routes = _routes(_route("repo.MergePullRequest", "routers/web/pull.go"))

    selection = _select_graph_slice_seeds(change, routes, set())

    assert any(seed.name == "MergePullRequest" for seed in selection.seeds)


# --------------------------------------------------------------------------
# Regression fixture modeled on the observed large-repository failure
# --------------------------------------------------------------------------


def test_regression_a_large_repository_does_not_produce_hundreds_of_seeds() -> None:
    """The observed shape: ~10 changed symbols, hundreds of repository
    routes, only a few genuinely related. Requested seeds must land in the
    low tens, not the hundreds that filled hop 1's first page."""
    changed = [_changed_symbol(f"Changed{i}", f"services/pull/f{i}.go") for i in range(10)]
    change = _change(changed, files=[f"services/pull/f{i}.go" for i in range(10)])
    routes = _routes(
        *[_route(f"repo.Handler{i}", f"routers/web/r{i}.go") for i in range(300)],
        _route("repo.MergePullRequest", "services/pull/f0.go"),
        _route("repo.Related", "routers/web/related.go"),
    )

    selection = _select_graph_slice_seeds(change, routes, {"routers/web/related.go"})

    assert selection.changed_symbol_count == 10
    assert selection.route_handler_count == 2
    assert len(selection.seeds) == 12
    assert len(selection.seeds) < 30, "must not regress toward hundreds of seeds"
    names = {seed.name for seed in selection.seeds}
    assert "MergePullRequest" in names and "Related" in names
    assert not any(name.startswith("Handler") for name in names)
