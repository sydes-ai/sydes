"""`_select_via_impact_interpreter`'s backstop for a route Sydes already
discovered and merged into `routes.routes` (deterministically, or via the
LLM-discovery fallback) but which the impact interpreter's own reachability
analysis never produced a matching "http_route" entrypoint for.

Confirmed against a real run (`sydes-examples/realworld-axum-sqlx`, frozen
0.2.0b10 evaluation batch): the LLM-discovery fallback found the exact real
route (`POST /api/articles`, confidence 0.97, correct evidence quoting the
real source line) and it landed in `known_routes` — but the final
`AffectedFlow` still had `method: null, path: null`, because nothing carried
that verified candidate through to `selected`. The flow that ultimately
appeared came from AI-recovery's own, separate, route-blind flow
constructor (`recovery.canonical_merge`), which has no method/path fields
to lose it into in the first place.

This test reproduces that shape generically -- a made-up route/handler/
file, no Axum or Rust anywhere -- to prove the fix is a structural backstop
in the selection step, not a per-framework patch.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.core.models import EndpointCandidate
from sydes.verify.analyzer import VerifyChangeOptions, _select_via_impact_interpreter
from sydes.verify.models import ChangedFile, ChangedSymbol, ChangeSet

REPO = "app"


class _Routes:
    """Minimal stand-in for whatever `analyze_change` passes as `routes` --
    only the `.routes` attribute is read by `_select_via_impact_interpreter`."""

    def __init__(self, routes: list[EndpointCandidate]) -> None:
        self.routes = routes


def _change(*, file: str, symbol_name: str) -> ChangeSet:
    return ChangeSet(
        base="main",
        head="abc123",
        files=[ChangedFile(repo=REPO, path=file)],
        symbols=[ChangedSymbol(id=f"{REPO}:{file}:{symbol_name}", repo=REPO, file=file, name=symbol_name)],
    )


def _no_entrypoints_facts() -> StructuralFacts:
    """CBM-shaped facts where the structural backend supplied a call graph
    but found no entrypoints at all for this handler -- the same shape a
    routing library's declaration syntax not being recognized would
    produce, without asserting anything about *why* it found none."""
    return StructuralFacts(
        call_edges=[],
        usage_edges=[],
        entrypoints=[],
        symbol_index={"repos": []},
        provides_call_graph=True,
        backend="cbm",
    )


def test_backstop_carries_a_fallback_discovered_route_into_selection():
    """The core regression: a route that only exists in `routes.routes`
    (standing in for the LLM-discovery fallback, or any future deterministic
    extractor) — with the interpreter finding nothing — must still come out
    of `_select_via_impact_interpreter` with its real method/path/handler
    intact, not be dropped."""
    handler_file = "src/http/widgets.rs"
    candidate = EndpointCandidate(
        method="POST",
        path="/api/widgets",
        handler="create_widget",
        file=handler_file,
        repo=REPO,
        confidence=0.97,
        status="llm_discovered",
    )
    change = _change(file=handler_file, symbol_name="create_widget")

    selected, impact_result, _notes = _select_via_impact_interpreter(
        change=change,
        routes=_Routes([candidate]),
        structural=_no_entrypoints_facts(),
        repo_name=REPO,
        options=VerifyChangeOptions(),
        repo_root=None,
    )

    assert impact_result.affected == []  # confirms the interpreter alone found nothing
    assert len(selected) == 1
    recovered = selected[0]
    assert recovered.method == "POST"
    assert recovered.path == "/api/widgets"
    assert recovered.handler == "create_widget"
    assert recovered.file == handler_file


def test_backstop_does_not_duplicate_a_route_the_interpreter_already_selected():
    """If the interpreter's own reachability analysis *did* already produce
    this exact route, the backstop must not add a second copy."""
    handler_file = "src/http/widgets.rs"
    candidate = EndpointCandidate(
        method="POST", path="/api/widgets", handler="create_widget",
        file=handler_file, repo=REPO, confidence=1.0,
    )
    change = _change(file=handler_file, symbol_name="create_widget")

    # An entrypoint fact shaped so the interpreter's own deterministic path
    # already reaches and classifies it as this exact http_route -- the
    # "already selected normally" case the backstop must stay a no-op for.
    facts = StructuralFacts(
        call_edges=[],
        usage_edges=[],
        entrypoints=[
            {
                "repo": REPO,
                "file": handler_file,
                "symbol": "create_widget",
                "qualified_name": "app.create_widget",
                "kind": "http_route",
                "method": "POST",
                "path": "/api/widgets",
            }
        ],
        symbol_index={"repos": []},
        provides_call_graph=True,
        backend="cbm",
    )

    selected, impact_result, _notes = _select_via_impact_interpreter(
        change=change,
        routes=_Routes([candidate]),
        structural=facts,
        repo_name=REPO,
        options=VerifyChangeOptions(),
        repo_root=None,
    )

    assert len(impact_result.affected) == 1  # the interpreter found it on its own this time
    assert len(selected) == 1  # backstop did not add a duplicate


def test_backstop_ignores_a_route_whose_handler_was_not_changed():
    """A route already known to Sydes (e.g. from an earlier scan) whose
    handler this diff never touched must not be pulled in -- the backstop
    is scoped to the change, exactly like every other obligation-derivation
    path in this codebase."""
    candidate = EndpointCandidate(
        method="GET", path="/api/unrelated", handler="unrelated_handler",
        file="src/http/unrelated.rs", repo=REPO, confidence=0.9,
    )
    change = _change(file="src/http/widgets.rs", symbol_name="create_widget")

    selected, _impact_result, _notes = _select_via_impact_interpreter(
        change=change,
        routes=_Routes([candidate]),
        structural=_no_entrypoints_facts(),
        repo_name=REPO,
        options=VerifyChangeOptions(),
        repo_root=None,
    )

    assert selected == []
