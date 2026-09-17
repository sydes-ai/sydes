"""`_unresolved_composition_note` -- the repo-attributed analysis note for
unresolved route composition.

`routes.notes` accumulates across every repo in one run, each entry already
prefixed `f"{repo.name}: ..."`; the final analysis-notes message used to
report "Route composition is unresolved in this repository" with no repo
name at all, unattributable in a multi-repo run.
"""

from __future__ import annotations

from sydes.verify.analyzer import _unresolved_composition_note


def test_no_note_when_nothing_is_unresolved() -> None:
    notes = ["app: discovery_coverage=strong score=0.9"]
    assert _unresolved_composition_note(notes) is None


def test_single_repo_is_named_directly() -> None:
    notes = [
        "app: discovery_coverage_reasons=route composition is unresolved: 1 of 2 route "
        "containers have no resolvable mount or declared prefix, so route paths may be incomplete",
    ]
    note = _unresolved_composition_note(notes)
    assert note == "Route composition is unresolved in app; some routes may be missing."


def test_multi_repo_run_names_only_the_affected_repo() -> None:
    """Regression: a two-repo run where only one repo has unresolved
    composition -- the note must name that repo specifically, not read as
    a generic, unattributable "this repository"."""
    notes = [
        "app: discovery_coverage=strong score=0.9",
        "worker: discovery_coverage_reasons=route composition is unresolved: 2 of 3 route "
        "containers have no resolvable mount or declared prefix, so route paths may be incomplete",
    ]
    note = _unresolved_composition_note(notes)
    assert note == "Route composition is unresolved in worker; some routes may be missing."
    assert "app" not in note


def test_multiple_affected_repos_are_all_named() -> None:
    notes = [
        "app: discovery_coverage_reasons=route composition is unresolved: 1 of 1 route "
        "containers have no resolvable mount or declared prefix, so route paths may be incomplete",
        "worker: discovery_coverage_reasons=route composition is unresolved: 2 of 3 route "
        "containers have no resolvable mount or declared prefix, so route paths may be incomplete",
    ]
    note = _unresolved_composition_note(notes)
    assert note == "Route composition is unresolved in app, worker; some routes may be missing."
