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
from sydes.recovery.schema import (
    EntityRef, ROOT_CANDIDATE_BOUNDARY, ROOT_VERIFIED_BOUNDARY, STATUS_UNRESOLVED,
)
from sydes.recovery.tools import RepoTools


class _FakeGraph:
    """Stands in for `CBMGraphTools`: `resolve_qualified_name`,
    `reachability_slice`, `decorated_symbols`, `methods_of`, `symbol_flags`
    -- exactly the methods `propose_graph_path` calls, nothing else."""

    def __init__(self, *, qn_map=None, slices=None, decorated=None, methods=None, flags=None):
        self.qn_map = qn_map or {}
        self.slices = slices or {}
        self.decorated_rows = decorated or []
        self.methods_map = methods or {}
        self.flags_map = flags or {}
        self.resolve_calls: list[tuple[str, str]] = []
        self.slice_calls: list[tuple[str, ...]] = []
        self.symbol_flags_calls: list[tuple[str, ...]] = []

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

    def symbol_flags(self, qualified_names):
        self.symbol_flags_calls.append(tuple(sorted(qualified_names)))
        return {qn: self.flags_map[qn] for qn in qualified_names if qn in self.flags_map}


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


def test_type_shaped_expansion_cap_does_not_arbitrarily_exclude_the_needed_class(tmp_path: Path):
    """Regression for a real, measured failure: `_MAX_CLASS_EXPANSIONS`
    used to be 20, and candidates are (still, for determinism) sorted by
    FULL qualified name -- a string dominated by an irrelevant path
    prefix, unrelated to which candidate is actually useful. With enough
    decoy type-shaped nodes sorting ahead of the one that matters (here,
    plain alphabetical luck: "aaa..." decoys before "pkg.Address"), the
    needed class fell outside the cap and its own methods -- the only way
    to reach the target -- never got expanded, deterministically failing
    every time no matter how many bridge rounds ran. Real case: 55
    type-shaped candidates in one round, the needed one excluded by the
    old cap of 20.
    """
    _write(tmp_path, "controller.ts", "new Address(props)\n")
    _write(tmp_path, "address.ts", "class Address {\n  validate() {}\n}\n")

    decoy_qns = [f"aaa.decoy.Decoy{i:02d}" for i in range(30)]  # 30 > the old cap of 20
    nodes = {"pkg.Controller.create": "controller.ts", "pkg.Address": "address.ts"}
    nodes.update({qn: "decoy.ts" for qn in decoy_qns})

    graph = _FakeGraph(
        qn_map={
            ("create", "controller.ts"): "pkg.Controller.create",
            ("validate", "address.ts"): "pkg.Address.validate",
        },
        slices={
            ("pkg.Controller.create",): _slice(
                nodes,
                [_calls_edge("pkg.Controller.create", "controller.ts", 1, "pkg.Address", "address.ts")],
            ),
        },
        methods={"pkg.Address": ["pkg.Address.validate"]},
    )
    target = EntityRef(symbol="validate", file="address.ts")
    entrypoints = [EntityRef(symbol="create", file="controller.ts")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["create", "Address", "validate"]


def test_evidence_prefers_the_occurrence_nearest_the_recorded_call_site(tmp_path: Path):
    """A callee's own declaration very often appears earlier in the file
    than the actual call site inside a different function -- citing the
    FIRST textual match (the declaration) instead of the occurrence
    nearest the caller's own recorded line doesn't show the interaction
    this edge claims. Regression for a real, measured case: a real repo's
    citation for a CALLS edge showed the callee's own declaration, not the
    call, and was (correctly) judged insufficient downstream.
    """
    lines = ["// decoy"] * 3
    lines.append("function target() {}")  # real line 4: the decoy/declaration match
    lines += ["// filler"] * 20
    lines.append("call target()")  # real line 25: the actual call site
    lines += ["// filler"] * 5
    _write(tmp_path, "mod.ts", "\n".join(lines) + "\n")

    graph = _FakeGraph(
        qn_map={("create", "controller.ts"): "pkg.Controller.create"},
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "controller.ts", "pkg.Mod.target": "mod.ts"},
                [_calls_edge("pkg.Controller.create", "mod.ts", 25, "pkg.Mod.target", "mod.ts")],
            ),
        },
    )
    target = EntityRef(symbol="target", file="mod.ts", qualified_name="pkg.Mod.target")
    entrypoints = [EntityRef(symbol="create", file="controller.ts", qualified_name="pkg.Controller.create")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    evidence = path.edges[0].evidence[0]
    assert evidence.line_start <= 25 <= evidence.line_end
    assert "call target()" in evidence.fact
    assert "function target()" not in evidence.fact


def test_evidence_citation_is_never_empty_when_the_file_read_is_truncated(tmp_path: Path):
    """`RepoTools.read_file` caps its total output at a fixed character
    budget -- a hint/match line beyond what was actually read must not
    produce a start>end or out-of-range slice that renders as an empty
    citation. Regression for a real, measured case: a real repo's citation
    came back `line_start=149, line_end=142` (reversed, empty) on a file
    long enough to be truncated before reaching the recorded line.
    """
    filler = "x" * 12
    lines = [f"line {i} {filler}" for i in range(1, 341)]
    lines[339] = "call farAwaySymbol()"  # real line 340 -- past the ~6000-char read budget
    _write(tmp_path, "mod.ts", "\n".join(lines) + "\n")

    graph = _FakeGraph(
        qn_map={("create", "controller.ts"): "pkg.Controller.create"},
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "controller.ts", "pkg.Mod.farAwaySymbol": "mod.ts"},
                [_calls_edge("pkg.Controller.create", "mod.ts", 340, "pkg.Mod.farAwaySymbol", "mod.ts")],
            ),
        },
    )
    target = EntityRef(symbol="farAwaySymbol", file="mod.ts", qualified_name="pkg.Mod.farAwaySymbol")
    entrypoints = [EntityRef(symbol="create", file="controller.ts", qualified_name="pkg.Controller.create")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    evidence = path.edges[0].evidence[0]
    assert evidence.fact.strip() != ""
    assert evidence.line_start is not None and evidence.line_end is not None
    assert evidence.line_start <= evidence.line_end


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


# ---------------------------------------------------------------------------
# Topology fallback: no known entrypoint at all (a background worker, a
# queue consumer -- nothing HTTP-route-shaped feeds `entrypoint_entities`
# today). Seeded from the TARGET instead, using the exact same
# `reachability_slice`/`_build_adjacency`/`_bfs_path` machinery -- the real
# case this exists for: Go's `main -> runTaskProcessor -> ... ->
# ProcessTaskSendVerifyEmail`, where `entrypoints` is empty.
# ---------------------------------------------------------------------------


def test_evidence_prefers_a_farther_forward_call_over_a_nearer_backward_declaration(tmp_path: Path):
    """Regression for a real, measured failure distinct from the
    first-match bug above: `hint_line` is the CALLER's own declaration
    line, so the real call can only appear AT OR AFTER it -- but a
    callee's own declaration living earlier in the file (an ordinary
    layout: helpers declared above the functions that use them) can still
    be numerically NEARER to the hint than the real, farther-but-forward
    call site. Plain nearest-by-distance picks the wrong one. Real shape:
    hint_line=100, callee declared at 70 (distance 30, backward), real
    call at 145 (distance 45, forward) -- naive nearest picks 70.
    """
    lines = ["// filler"] * 400
    lines[69] = "function farHelper() {}"  # real line 70: callee's own declaration, BEFORE the hint
    lines[144] = "call farHelper()"  # real line 145: the actual call site, AFTER the hint
    _write(tmp_path, "mod.ts", "\n".join(lines) + "\n")

    graph = _FakeGraph(
        qn_map={("create", "controller.ts"): "pkg.Controller.create"},
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "mod.ts", "pkg.Mod.farHelper": "mod.ts"},
                # caller_line=100: the caller's OWN declaration, not the
                # real call site at 145.
                [_calls_edge("pkg.Controller.create", "mod.ts", 100, "pkg.Mod.farHelper", "mod.ts")],
            ),
        },
    )
    target = EntityRef(symbol="farHelper", file="mod.ts", qualified_name="pkg.Mod.farHelper")
    entrypoints = [EntityRef(symbol="create", file="controller.ts", qualified_name="pkg.Controller.create")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    evidence = path.edges[0].evidence[0]
    assert "call farHelper()" in evidence.fact
    assert "function farHelper()" not in evidence.fact


def test_evidence_citation_reaches_a_real_call_beyond_the_whole_file_read_budget(tmp_path: Path):
    """Regression for a real, measured failure on a real fork PR: CBM's own
    recorded `caller_line` for a CALLS edge is very often the caller's OWN
    declaration line, not the exact call-site line a few dozen lines into
    its body (here: hint_line=300, the real call is at line 330) -- and
    `RepoTools.read_file`'s fixed character budget means a WHOLE-FILE read
    silently truncates before reaching either line on any normal-length
    file (a real ~800-line file truncated before line 170). The bounded,
    hint-centered read this function now tries FIRST must still find the
    real call 30 lines past the hint, where a whole-file read already
    cannot.
    """
    filler = "x" * 12
    lines = [f"line {i} {filler}" for i in range(1, 341)]
    lines[329] = "call farAwaySymbol()"  # real line 330 -- 30 lines past hint_line=300
    _write(tmp_path, "mod.ts", "\n".join(lines) + "\n")

    graph = _FakeGraph(
        qn_map={("create", "controller.ts"): "pkg.Controller.create"},
        slices={
            ("pkg.Controller.create",): _slice(
                {"pkg.Controller.create": "controller.ts", "pkg.Mod.farAwaySymbol": "mod.ts"},
                # caller_line=300: the DECLARATION line, not the real call
                # site at 330 -- exactly the real-world CBM shape.
                [_calls_edge("pkg.Controller.create", "mod.ts", 300, "pkg.Mod.farAwaySymbol", "mod.ts")],
            ),
        },
    )
    target = EntityRef(symbol="farAwaySymbol", file="mod.ts", qualified_name="pkg.Mod.farAwaySymbol")
    entrypoints = [EntityRef(symbol="create", file="controller.ts", qualified_name="pkg.Controller.create")]

    path = propose_graph_path(entrypoints, target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    evidence = path.edges[0].evidence[0]
    assert "call farAwaySymbol()" in evidence.fact
    assert evidence.line_start <= 330 <= evidence.line_end


def test_topology_fallback_discovers_a_verified_root_with_no_known_entrypoint(tmp_path: Path):
    """The core scenario this fallback exists for: `entrypoints=[]` (no
    HTTP route, no established flow reaches this target). Seeded from the
    target, the slice already contains the whole upstream chain; `main`
    survives as the only slice-local, non-test root, and CBM's own
    `is_entry_point` flag corroborates it as a genuine boundary -- so the
    root is reported as VERIFIED, not merely a candidate.
    """
    _write(tmp_path, "main.go", "func main() {\n  runTask()\n}\n")
    _write(tmp_path, "worker.go", "func runTask() {\n  validate()\n}\n")
    _write(tmp_path, "address.go", "func validate() {}\n")

    graph = _FakeGraph(
        slices={
            ("pkg.Address.validate",): _slice(
                {"pkg.main": "main.go", "pkg.runTask": "worker.go", "pkg.Address.validate": "address.go"},
                [
                    _calls_edge("pkg.main", "main.go", 2, "pkg.runTask", "worker.go"),
                    _calls_edge("pkg.runTask", "worker.go", 2, "pkg.Address.validate", "address.go"),
                ],
            ),
        },
        flags={"pkg.main": {"is_test": False, "is_entry_point": True}},
    )
    target = EntityRef(symbol="validate", file="address.go", qualified_name="pkg.Address.validate")

    path = propose_graph_path([], target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert path.entrypoint == "main"
    assert [n.symbol for n in path.nodes] == ["main", "runTask", "validate"]
    assert path.root_boundary_status == ROOT_VERIFIED_BOUNDARY
    assert path.status == STATUS_UNRESOLVED  # draft only -- verify.py still has to judge each edge


def test_topology_fallback_prefers_a_verified_root_over_a_shorter_unverified_one(tmp_path: Path):
    """Regression for a real, measured failure: a slice-local root that is
    only ONE hop from the target (e.g. an interface/virtual method's OWN
    declaration, connected to its concrete override by the very edge that
    relates them) is topologically closer than a genuine, deeper root like
    `main` -- naive shortest-path BFS over ALL candidates together picks
    the shorter, structurally-trivial one every time. Verified candidates
    (`is_entry_point=True`) must be tried FIRST, on their own, so a real
    root is never discarded in favor of a shorter but meaningless one.
    """
    _write(tmp_path, "main.go", "func main() {\n  runTask()\n}\n")
    _write(tmp_path, "worker.go", "func runTask() {\n  validate()\n}\n")
    _write(tmp_path, "address.go", "func validate() {}\n")

    graph = _FakeGraph(
        slices={
            ("pkg.Address.validate",): _slice(
                {
                    "pkg.main": "main.go", "pkg.runTask": "worker.go",
                    "pkg.Address.validate": "address.go", "pkg.Iface.validate": "address.go",
                },
                [
                    _calls_edge("pkg.main", "main.go", 2, "pkg.runTask", "worker.go"),
                    _calls_edge("pkg.runTask", "worker.go", 2, "pkg.Address.validate", "address.go"),
                    # The spurious one-hop shortcut: an unrelated, unverified
                    # node with a direct edge straight to the target.
                    _calls_edge("pkg.Iface.validate", "address.go", 1, "pkg.Address.validate", "address.go"),
                ],
            ),
        },
        flags={
            "pkg.main": {"is_test": False, "is_entry_point": True},
            "pkg.Iface.validate": {"is_test": False, "is_entry_point": False},
        },
    )
    target = EntityRef(symbol="validate", file="address.go", qualified_name="pkg.Address.validate")

    path = propose_graph_path([], target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert path.entrypoint == "main"
    assert [n.symbol for n in path.nodes] == ["main", "runTask", "validate"]
    assert path.root_boundary_status == ROOT_VERIFIED_BOUNDARY


def test_topology_fallback_root_without_is_entry_point_is_only_a_candidate(tmp_path: Path):
    """Same shape as above, but CBM has no `is_entry_point` signal for the
    discovered root (measured directly: this happens for real -- CBM
    flagged Go's `main` but not Java's or a real TS bootstrap function).
    The chain is still proposed, but the root must not be reported as an
    already-verified boundary just because the topology happened to find
    it."""
    _write(tmp_path, "main.go", "func main() {\n  runTask()\n}\n")
    _write(tmp_path, "worker.go", "func runTask() {\n  validate()\n}\n")
    _write(tmp_path, "address.go", "func validate() {}\n")

    graph = _FakeGraph(
        slices={
            ("pkg.Address.validate",): _slice(
                {"pkg.main": "main.go", "pkg.runTask": "worker.go", "pkg.Address.validate": "address.go"},
                [
                    _calls_edge("pkg.main", "main.go", 2, "pkg.runTask", "worker.go"),
                    _calls_edge("pkg.runTask", "worker.go", 2, "pkg.Address.validate", "address.go"),
                ],
            ),
        },
        # no flags at all -- CBM reports no is_entry_point signal here
    )
    target = EntityRef(symbol="validate", file="address.go", qualified_name="pkg.Address.validate")

    path = propose_graph_path([], target, graph=graph, tools=_repo(tmp_path))
    assert path is not None
    assert path.root_boundary_status == ROOT_CANDIDATE_BOUNDARY


def test_topology_fallback_excludes_a_test_caller_from_candidate_roots(tmp_path: Path):
    """A unit test calling the changed symbol directly is very often its
    ONLY caller in a bounded slice -- without excluding it via CBM's own
    `is_test`, it would look exactly like a legitimate root by the same
    in-degree-0 property alone. With no other candidate, the fallback must
    find nothing, not silently accept the test as a root."""
    _write(tmp_path, "address_test.go", "func TestValidate() {\n  validate()\n}\n")
    _write(tmp_path, "address.go", "func validate() {}\n")

    graph = _FakeGraph(
        slices={
            ("pkg.Address.validate",): _slice(
                {"pkg.TestValidate": "address_test.go", "pkg.Address.validate": "address.go"},
                [_calls_edge("pkg.TestValidate", "address_test.go", 2, "pkg.Address.validate", "address.go")],
            ),
        },
        flags={"pkg.TestValidate": {"is_test": True, "is_entry_point": False}},
    )
    target = EntityRef(symbol="validate", file="address.go", qualified_name="pkg.Address.validate")

    assert propose_graph_path([], target, graph=graph, tools=_repo(tmp_path)) is None


def test_topology_fallback_is_not_attempted_when_a_known_entrypoint_already_succeeds(tmp_path: Path):
    """No wasted cost: when the normal, known-entrypoint-seeded search
    already finds the target, the topology fallback (its own extra
    `reachability_slice` + `symbol_flags` calls) must never even run."""
    _write(tmp_path, "controller.ts", "call validate()\n")
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
    assert path.root_boundary_status == ROOT_VERIFIED_BOUNDARY
    assert ("pkg.Address.validate",) not in graph.slice_calls
    assert graph.symbol_flags_calls == []
