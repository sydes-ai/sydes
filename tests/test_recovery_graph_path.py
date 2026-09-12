"""`sydes.recovery.graph_path` -- deterministic path proposal built
entirely from an already-indexed CBM graph, no LLM call involved. A fake
graph stands in for `CBMGraphTools`; these tests only exercise this
module's own reachability/bridge/assembly logic, not CBM itself (see
`tests/test_recovery_graph_tools.py` for that layer).
"""

from __future__ import annotations

from pathlib import Path

from sydes.code_intelligence.graph_slice import GraphSlice
from sydes.recovery.graph_path import propose_graph_path
from sydes.recovery.schema import EntityRef, STATUS_UNRESOLVED
from sydes.recovery.tools import RepoTools


class _FakeGraph:
    """Stands in for `CBMGraphTools`: `resolve_qualified_name`,
    `reachability_slice`, `decorated_symbols`, `methods_of` -- exactly the
    methods `propose_graph_path` calls, nothing else."""

    def __init__(self, *, qn_map=None, slices=None, decorated=None, methods=None):
        self.qn_map = qn_map or {}
        self.slices = slices or {}
        self.decorated_rows = decorated or []
        self.methods_map = methods or {}
        self.resolve_calls: list[tuple[str, str]] = []
        self.slice_calls: list[tuple[str, ...]] = []

    def resolve_qualified_name(self, bare_name, file):
        self.resolve_calls.append((bare_name, file))
        return self.qn_map.get((bare_name, file))

    def reachability_slice(self, seed_qualified_names, *, max_depth=6):
        self.slice_calls.append(tuple(sorted(seed_qualified_names)))
        return self.slices.get(tuple(sorted(seed_qualified_names)))

    def decorated_symbols(self):
        return self.decorated_rows

    def methods_of(self, class_qualified_name):
        return self.methods_map.get(class_qualified_name, [])


def _calls_edge(caller_qn, caller_file, caller_line, callee_qn, callee_file) -> dict:
    return {
        "caller_qualified_name": caller_qn, "caller_file": caller_file, "caller_line": caller_line,
        "callee_qualified_name": callee_qn, "callee_file": callee_file, "callee_line": None,
        "caller_symbol": caller_qn.rsplit(".", 1)[-1], "callee_symbol": callee_qn.rsplit(".", 1)[-1],
        "repo": "app", "source": "cbm_graph_slice",
    }


def _slice(qn_to_file: dict, edges: list[dict]) -> GraphSlice:
    """Builds a `GraphSlice` with the SAME node-key/value shape
    `sydes.code_intelligence.graph_slice.build_graph_slice` actually
    produces (`"{file}::{qualified_name}"` keys, `{"qualified_name",
    "file", "line"}` values) -- a fake with a different shape here would
    let `graph_path.py` silently rely on the wrong key format without any
    test catching it, exactly what happened before this helper existed.
    """
    nodes = {f"{file}::{qn}": {"qualified_name": qn, "file": file, "line": None} for qn, file in qn_to_file.items()}
    return GraphSlice(seed_symbols=tuple(qn_to_file), nodes=nodes, edges=edges, source_call_count=1, depth_reached=1)


def _repo(tmp_path: Path) -> RepoTools:
    return RepoTools(tmp_path)


def _write(tmp_path: Path, rel: str, text: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_returns_none_when_target_never_resolves(tmp_path: Path):
    graph = _FakeGraph(qn_map={})
    target = EntityRef(symbol="validate", file="address.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]
    assert propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path)) is None


def test_returns_none_when_no_entrypoint_resolves(tmp_path: Path):
    graph = _FakeGraph(qn_map={("validate", "address.ts"): "pkg.Address.validate"})
    target = EntityRef(symbol="validate", file="address.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]
    assert propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path)) is None
    # entrypoint resolution was attempted (not skipped) before giving up
    assert ("create", "controller.ts") in graph.resolve_calls


def test_returns_none_when_unreachable_and_no_decorator_bridge(tmp_path: Path):
    graph = _FakeGraph(
        qn_map={
            ("create", "controller.ts"): "pkg.Controller.create",
            ("validate", "address.ts"): "pkg.Address.validate",
        },
        slices={("pkg.Controller.create",): _slice({"pkg.Controller.create": "controller.ts"}, [])},
        decorated=[],
    )
    target = EntityRef(symbol="validate", file="address.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]
    assert propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path)) is None


def test_direct_calls_chain_becomes_a_recovered_path_with_real_evidence(tmp_path: Path):
    _write(tmp_path, "controller.ts", "line1\ncall middle()\nline3\n")
    _write(tmp_path, "middle.ts", "line1\ncall validate()\nline3\n")
    _write(tmp_path, "address.ts", "export function validate() {}\n")

    graph = _FakeGraph(
        qn_map={
            ("create", "controller.ts"): "pkg.Controller.create",
            ("validate", "address.ts"): "pkg.Address.validate",
        },
        slices={
            ("pkg.Controller.create",): _slice(
                {
                    "pkg.Controller.create": "controller.ts",
                    "pkg.Middle.middle": "middle.ts",
                    "pkg.Address.validate": "address.ts",
                },
                [
                    _calls_edge("pkg.Controller.create", "controller.ts", 2, "pkg.Middle.middle", "middle.ts"),
                    _calls_edge("pkg.Middle.middle", "middle.ts", 2, "pkg.Address.validate", "address.ts"),
                ],
            ),
        },
    )
    target = EntityRef(symbol="validate", file="address.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["create", "middle", "validate"]
    assert path.target_node == "validate"
    assert len(path.edges) == 2
    for edge in path.edges:
        assert edge.status == STATUS_UNRESOLVED  # draft only -- verify.py judges it, not this module
        assert edge.evidence and edge.evidence[0].fact


def test_prefers_pre_resolved_qualified_name_over_resolve_call(tmp_path: Path):
    _write(tmp_path, "controller.ts", "calls validate directly\n")
    _write(tmp_path, "address.ts", "export function validate() {}\n")
    graph = _FakeGraph(
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "controller.ts", "pkg.Address.validate": "address.ts"},
                [_calls_edge("pkg.Controller.create", "controller.ts", 1, "pkg.Address.validate", "address.ts")],
            ),
        },
    )
    target = EntityRef(symbol="validate", file="address.ts", qualified_name="pkg.Address.validate")
    entrypoints = [EntityRef(symbol="create", file="controller.ts", qualified_name="pkg.Controller.create")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    # qualified_name was supplied directly on both entities -- no resolve_qualified_name call needed at all
    assert graph.resolve_calls == []


def test_decorator_bridge_connects_an_otherwise_unreachable_target(tmp_path: Path):
    """Generic reflection/DI-style dispatch: the entrypoint reaches an
    intermediate symbol via ordinary CALLS, but the actual target is only
    reachable because SOME OTHER symbol's decorator source names that
    intermediate -- exactly the shape every decorator/annotation-argument
    binding shares, regardless of what any particular framework calls it.
    """
    _write(tmp_path, "controller.ts", "dispatches CreateThing\n")
    _write(tmp_path, "handler.ts", "@Handles(CreateThing)\nclass ThingHandler {}\n")
    _write(tmp_path, "validate.ts", "export function validate() {}\n")

    graph = _FakeGraph(
        qn_map={
            ("create", "controller.ts"): "pkg.Controller.create",
            ("validate", "validate.ts"): "pkg.Thing.validate",
        },
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "controller.ts", "pkg.CreateThing": "controller.ts"},
                [_calls_edge("pkg.Controller.create", "controller.ts", 1, "pkg.CreateThing", "controller.ts")],
            ),
            ("pkg.ThingHandler",): _slice(
                {"pkg.ThingHandler": "handler.ts", "pkg.Thing.validate": "validate.ts"},
                [_calls_edge("pkg.ThingHandler", "handler.ts", 1, "pkg.Thing.validate", "validate.ts")],
            ),
        },
        decorated=[
            {
                "qualified_name": "pkg.ThingHandler", "file": "handler.ts",
                "decorators": "@Handles(CreateThing)", "lines": "1-2",
            },
        ],
    )
    target = EntityRef(symbol="validate", file="validate.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["create", "CreateThing", "ThingHandler", "validate"]
    # the bridge hop's evidence is the decorator's own source, not a CALLS/USAGE site
    bridge_edge = path.edges[1]
    assert "@Handles(CreateThing)" in bridge_edge.evidence[0].fact
    assert bridge_edge.evidence[0].file == "handler.ts"


def test_decorator_bridge_on_a_class_continues_via_its_own_methods(tmp_path: Path):
    """The measured real-world shape: a class-level decorator/annotation
    has NO CALLS/USAGE edge of its own at all (a class declaration doesn't
    "call" anything) -- the edges that actually reach the target live on
    one of its METHODS. `methods_of` is what lets the search continue past
    a decorated class instead of dead-ending on it.
    """
    _write(tmp_path, "controller.ts", "dispatches CreateThing\n")
    _write(tmp_path, "handler.ts", "@Handles(CreateThing)\nclass ThingHandler {\n  execute() {}\n}\n")
    _write(tmp_path, "validate.ts", "export function validate() {}\n")

    graph = _FakeGraph(
        qn_map={
            ("create", "controller.ts"): "pkg.Controller.create",
            ("validate", "validate.ts"): "pkg.Thing.validate",
        },
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "controller.ts", "pkg.CreateThing": "controller.ts"},
                [_calls_edge("pkg.Controller.create", "controller.ts", 1, "pkg.CreateThing", "controller.ts")],
            ),
            # The class itself, seeded alone, has NOTHING -- only seeding
            # with its method (as `methods_of` supplies) finds the edge.
            ("pkg.ThingHandler",): _slice({}, []),
            ("pkg.ThingHandler", "pkg.ThingHandler.execute"): _slice(
                {"pkg.ThingHandler.execute": "handler.ts", "pkg.Thing.validate": "validate.ts"},
                [_calls_edge("pkg.ThingHandler.execute", "handler.ts", 3, "pkg.Thing.validate", "validate.ts")],
            ),
        },
        decorated=[
            {
                "qualified_name": "pkg.ThingHandler", "file": "handler.ts",
                "decorators": "@Handles(CreateThing)", "lines": "1-4",
            },
        ],
        methods={"pkg.ThingHandler": ["pkg.ThingHandler.execute"]},
    )
    target = EntityRef(symbol="validate", file="validate.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["create", "CreateThing", "ThingHandler", "execute", "validate"]
