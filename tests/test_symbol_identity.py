"""Canonical symbol identity, and the traversal defect it exists to fix.

Before this type existed, `ImpactInterpreter`'s adjacency was keyed by bare
symbol name: every function named `update` in the whole repository shared one
adjacency bucket, and a change to any one of them fanned traversal out across
every unrelated module that happened to define something with the same short
name. `dispatch#6118` — a change to `case_service.update` — turned into 420
"affected" results because of exactly this collapse.

These tests pin the identity contract in isolation, then reproduce the
collision at graph scale and confirm it no longer happens.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact import ImpactInterpreter
from sydes.impact.models import SymbolIdentity

REPO = "app"


# --------------------------------------------------------------------------
# SymbolIdentity contract
# --------------------------------------------------------------------------


def test_same_short_name_different_files_are_distinct() -> None:
    a = SymbolIdentity.from_fields(repo=REPO, file="a.py", qualified_name="a.update")
    b = SymbolIdentity.from_fields(repo=REPO, file="b.py", qualified_name="b.update")

    assert a != b
    assert a.key != b.key


def test_same_method_name_different_classes_are_distinct() -> None:
    a = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.Foo.update")
    b = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.Bar.update")

    assert a != b


def test_qualified_name_alone_determines_equality_when_present() -> None:
    """Two identities agreeing on qualified name are the same symbol."""
    a = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.Foo.run", line=5)
    b = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.Foo.run", line=99)

    assert a == b, "line should not matter once a qualified name is known"


def test_identity_is_deterministic_and_hashable() -> None:
    a = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.f")
    b = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.f")

    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_resolved_identity_requires_qualified_name_or_line() -> None:
    with_qn = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.f")
    with_line = SymbolIdentity.from_fields(repo=REPO, file="x.py", short_name="f", line=3)
    neither = SymbolIdentity.from_fields(repo=REPO, file="x.py", short_name="f")

    assert with_qn.resolved
    assert with_line.resolved
    assert not neither.resolved


def test_fallback_identity_is_still_scoped_by_file() -> None:
    """Even the weakest tier never merges symbols from different files."""
    a = SymbolIdentity.from_fields(repo=REPO, file="a.py", short_name="handle")
    b = SymbolIdentity.from_fields(repo=REPO, file="b.py", short_name="handle")

    assert a.key != b.key
    assert not a.resolved and not b.resolved


def test_short_name_survives_for_display_without_affecting_identity() -> None:
    identity = SymbolIdentity.from_fields(repo=REPO, file="x.py", qualified_name="x.Foo.run")

    assert identity.short_name == "run"
    assert identity.label == "x.Foo.run"


# --------------------------------------------------------------------------
# The collision, reproduced and fixed
# --------------------------------------------------------------------------


def _edge(caller_q, caller_file, callee_q, callee_file, repo=REPO):
    return {
        "repo": repo, "caller_file": caller_file,
        "caller_symbol": caller_q.rsplit(".", 1)[-1], "caller_qualified_name": caller_q,
        "callee_file": callee_file,
        "callee_symbol": callee_q.rsplit(".", 1)[-1], "callee_qualified_name": callee_q,
    }


def test_common_short_name_does_not_collapse_adjacency() -> None:
    """The exact shape of the #6118 defect: many `update`s, one bare name.

    Three unrelated modules each define their own `update`. A change to only
    one of them must reach only its own caller, not the other two.
    """
    edges = [
        _edge("mod_a.handler_a", "a.py", "mod_a.update", "a.py"),
        _edge("mod_b.handler_b", "b.py", "mod_b.update", "b.py"),
        _edge("mod_c.handler_c", "c.py", "mod_c.update", "c.py"),
    ]
    entrypoints = [
        {"repo": REPO, "qualified_name": "mod_a.handler_a", "symbol": "handler_a",
         "file": "a.py", "line": 1, "route_method": "GET", "route_path": "/a"},
        {"repo": REPO, "qualified_name": "mod_b.handler_b", "symbol": "handler_b",
         "file": "b.py", "line": 1, "route_method": "GET", "route_path": "/b"},
        {"repo": REPO, "qualified_name": "mod_c.handler_c", "symbol": "handler_c",
         "file": "c.py", "line": 1, "route_method": "GET", "route_path": "/c"},
    ]
    facts = StructuralFacts(call_edges=edges, entrypoints=entrypoints,
                            provides_call_graph=True, backend="cbm")

    result = ImpactInterpreter().interpret(
        [{"name": "update", "file": "b.py", "repo": REPO, "qualified_name": "mod_b.update"}],
        facts,
    )

    labels = {item.label for item in result.affected}
    assert labels == {"GET /b"}, f"only b's route should be reached, got {labels}"


def test_many_same_named_symbols_do_not_explode_the_result_set() -> None:
    """At larger scale: N unrelated `run`s must not produce N affected routes
    from a change to just one of them."""
    n = 50
    edges = [
        _edge(f"mod{i}.h{i}", f"f{i}.py", f"mod{i}.run", f"f{i}.py") for i in range(n)
    ]
    entrypoints = [
        {"repo": REPO, "qualified_name": f"mod{i}.h{i}", "symbol": f"h{i}",
         "file": f"f{i}.py", "line": 1, "route_method": "GET", "route_path": f"/{i}"}
        for i in range(n)
    ]
    facts = StructuralFacts(call_edges=edges, entrypoints=entrypoints,
                            provides_call_graph=True, backend="cbm")

    result = ImpactInterpreter().interpret(
        [{"name": "run", "file": "f7.py", "repo": REPO, "qualified_name": "mod7.run"}],
        facts,
    )

    assert [item.label for item in result.affected] == ["GET /7"]


# --------------------------------------------------------------------------
# Ambiguous edges: recorded, never fanned out
# --------------------------------------------------------------------------


def test_ambiguous_edge_endpoint_is_not_fanned_out_to_every_same_named_symbol() -> None:
    """No qualified name, no line: the endpoint genuinely cannot be told apart.

    The right behaviour is to stop at that node and say so, not to guess by
    matching every symbol that happens to share the name.
    """
    edges = [
        {"repo": REPO, "caller_file": "x.py", "caller_symbol": "helper",
         "caller_qualified_name": "", "callee_file": "x.py",
         "callee_symbol": "changed_fn", "callee_qualified_name": "x.changed_fn"},
    ]
    # Two unrelated entrypoints both happen to be named "helper".
    entrypoints = [
        {"repo": REPO, "qualified_name": "x.helper", "symbol": "helper", "file": "x.py",
         "line": 1, "route_method": "GET", "route_path": "/one"},
        {"repo": REPO, "qualified_name": "y.helper", "symbol": "helper", "file": "y.py",
         "line": 1, "route_method": "GET", "route_path": "/two"},
    ]
    facts = StructuralFacts(call_edges=edges, entrypoints=entrypoints,
                            provides_call_graph=True, backend="cbm")

    result = ImpactInterpreter().interpret(
        [{"name": "changed_fn", "file": "x.py", "repo": REPO}], facts,
    )

    # "helper" in x.py IS resolvable (file matches x.py's own "helper" entry,
    # via the identity-of-entry using file+name — same file, single match).
    labels = {item.label for item in result.affected}
    assert labels == {"GET /one"}, "the same-file match should resolve; the other must not"
    assert "GET /two" not in labels


def test_unresolvable_ambiguous_hop_is_recorded_in_metrics() -> None:
    """A hop that could not be resolved to a stable identity leaves a trace."""
    edges = [
        {"repo": REPO, "caller_file": "x.py", "caller_symbol": "mystery",
         "caller_qualified_name": "", "callee_file": "x.py",
         "callee_symbol": "changed_fn", "callee_qualified_name": "x.changed_fn"},
    ]
    facts = StructuralFacts(call_edges=edges, entrypoints=[],
                            provides_call_graph=True, backend="cbm")

    result = ImpactInterpreter().interpret(
        [{"name": "changed_fn", "file": "x.py", "repo": REPO}], facts,
    )

    assert result.metrics.get("ambiguous_edges", 0) >= 1
