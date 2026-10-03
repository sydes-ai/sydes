"""One composed dispatch edge from static + runtime `through_external` evidence (experiment).

domain-driven-hexagon TS-S-01: CBM sees `DeleteUserHttpController.deleteUser` construct a
`DeleteUserCommand` and call `this.commandBus.execute(command)`, and separately
`@CommandHandler(DeleteUserCommand) DeleteUserService`, but not the framework-mediated edge
between them. DiffGenome's contract now reports, from a focused test, that
`DeleteUserService.execute` executed within `CommandBus.execute(DeleteUserCommand)`
(`relation: "through_external"`). These fixtures encode that shape with neutral names: the
rule keys on CommandBus/execute/@CommandHandler, never on repository paths or class names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.behavioral.runtime import RuntimeEvidence
from sydes.code_intelligence.base import StructuralFacts
from sydes.discover.dispatch_composition import (
    COMPOSED_DISPATCH_SOURCE,
    compose_dispatch_edges,
    ts_files,
)
from sydes.impact.interpreter import _FactIndex
from sydes.impact.models import (
    PROVENANCE_COMPOSED_STATIC_RUNTIME,
    RELATION_COMPOSED_DISPATCH,
    RELATION_OBSERVED_RUNTIME,
    SymbolIdentity,
)

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_language_pack")

PRODUCER = """\
import { CommandBus } from '@nestjs/cqrs';
import { RemoveItemCommand } from './remove-item.service';

export class RemoveItemController {
  constructor(private readonly commandBus: CommandBus) {}

  async remove(id: string): Promise<void> {
    const command = new RemoveItemCommand({ itemId: id });
    await this.commandBus.execute(command);
  }
}
"""

HANDLER = """\
import { CommandHandler } from '@nestjs/cqrs';

export class RemoveItemCommand {
  readonly itemId: string;
  constructor(props: RemoveItemCommand) { this.itemId = props.itemId; }
}

@CommandHandler({message})
export class RemoveItemService {
  async execute(command: RemoveItemCommand): Promise<void> {}
}
"""

MODULE = """\
import { Module } from '@nestjs/common';
import { CqrsModule } from '@nestjs/cqrs';
import { RemoveItemService } from './remove-item.service';

@Module({ imports: [CqrsModule], providers: [RemoveItemService] })
export class ItemModule {}
"""

CALLER = "js:test/remove-item.spec.ts::removes an item"
CALLEE = "js:src/remove-item.service.RemoveItemService.execute"
QUALIFIED = {
    ("src/remove-item.controller.ts", "remove"): "app.src.remove-item.controller.RemoveItemController.remove",
    ("src/remove-item.service.ts", "execute"): "app.src.remove-item.service.RemoveItemService.execute",
}


def _repo(tmp_path: Path, message: str = "RemoveItemCommand") -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "remove-item.controller.ts").write_text(PRODUCER)
    (tmp_path / "src" / "remove-item.service.ts").write_text(HANDLER.replace("{message}", message))
    (tmp_path / "src" / "item.module.ts").write_text(MODULE)
    return tmp_path


def _edge(*, arg: str = "RemoveItemCommand", relation: bool = True, owner: str = "CommandBus") -> dict:
    edge = {
        "caller": {"symbol": CALLER, "file": None, "line": None, "origin": "test"},
        "callee": {"symbol": CALLEE, "file": "src/remove-item.service.ts", "line": 10, "origin": "repo"},
        "executions": 1, "tests": [CALLER], "tests_total": 1,
    }
    if relation:
        edge["relation"] = "through_external"
        edge["via"] = {"symbol": f"js:external:{owner}.execute", "origin": "external", "owner": owner,
                       "member": "execute", "arg_shapes": [[arg]], "exits": {"returned": 1}}
    return edge


def _runtime(*edges: dict) -> RuntimeEvidence:
    return RuntimeEvidence({"format": "diffgenome-runtime/1", "changed_functions": [], "edges": list(edges)})


def _resolve(file: str, name: str, line: int | None, owner_class: str | None) -> dict | None:
    qualified = QUALIFIED.get((file, name))
    return {"qualified": qualified, "line": 1} if qualified else None


def _compose(repo: Path, runtime: RuntimeEvidence):
    return compose_dispatch_edges(
        runtime.external_bridges(), repo_root=repo, files=ts_files(repo), resolve=_resolve, repo="app"
    )


def test_a_static_call_site_runtime_bridge_and_handler_compose_one_edge(tmp_path: Path) -> None:
    edges, records, notes = _compose(_repo(tmp_path), _runtime(_edge()))
    assert len(edges) == 1 and len(records) == 1, notes
    edge, record = edges[0], records[0]
    assert edge["source"] == COMPOSED_DISPATCH_SOURCE
    assert (edge["caller_qualified_name"], edge["callee_qualified_name"]) == (
        QUALIFIED[("src/remove-item.controller.ts", "remove")],
        QUALIFIED[("src/remove-item.service.ts", "execute")],
    )
    assert record["from"] == "RemoveItemController.remove" and record["to"] == "RemoveItemService.execute"
    assert record["kind"] == "semantic_dispatch" and record["evidence"] == "composed"
    assert record["rule"] == "nestjs_cqrs_command_handler"
    assert record["static"]["producer"] == {"file": "src/remove-item.controller.ts", "line": 9,
                                            "call": "CommandBus.execute",
                                            "argument_type": "RemoveItemCommand"}
    assert record["static"]["handler"]["decorator"] == "CommandHandler(RemoveItemCommand)"
    assert record["static"]["registered_in_cqrs_module"] is True
    assert record["runtime"]["relation"] == "through_external"
    assert record["runtime"]["via"] == {"symbol": "js:external:CommandBus.execute", "owner": "CommandBus",
                                        "member": "execute", "arg_shape": "RemoveItemCommand"}


def test_b_a_different_runtime_argument_composes_nothing(tmp_path: Path) -> None:
    edges, records, notes = _compose(_repo(tmp_path), _runtime(_edge(arg="OtherCommand")))
    assert edges == [] and records == []
    assert any("possible dispatch of OtherCommand" in n for n in notes)


def test_c_a_handler_for_a_different_message_composes_nothing(tmp_path: Path) -> None:
    edges, records, _ = _compose(_repo(tmp_path, message="OtherCommand"), _runtime(_edge()))
    assert edges == [] and records == []


def test_d_without_relation_through_external_the_rule_does_not_fire(tmp_path: Path) -> None:
    runtime = _runtime(_edge(relation=False))
    assert runtime.external_bridges() == []
    edges, records, notes = _compose(_repo(tmp_path), runtime)
    assert edges == [] and records == []
    # static evidence alone is reported as possible, never composed
    assert any("static evidence only" in n for n in notes)


def test_other_external_owners_are_out_of_scope(tmp_path: Path) -> None:
    edges, _, _ = _compose(_repo(tmp_path), _runtime(_edge(owner="QueryBus")))
    assert edges == []


def test_two_handlers_for_one_message_are_left_unresolved(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "second.service.ts").write_text(
        HANDLER.replace("{message}", "RemoveItemCommand").replace("RemoveItemService", "SecondService")
        .replace("export class RemoveItemCommand", "class Unused"))
    edges, _, notes = _compose(repo, _runtime(_edge()))
    assert edges == [] and any("2 @CommandHandler(RemoveItemCommand)" in n for n in notes)


def test_e_provenance_says_the_production_call_site_was_not_observed(tmp_path: Path) -> None:
    edges, records, _ = _compose(_repo(tmp_path), _runtime(_edge()))
    runtime = records[0]["runtime"]
    assert runtime["observed_caller"] == {"symbol": CALLER, "origin": "test"}
    assert "invoked from a test" in edges[0]["evidence"]
    # walked as its own relation, never as a call or an observed runtime call
    index = _FactIndex(StructuralFacts(call_edges=edges), "app")
    callee = SymbolIdentity.from_fields(
        repo="app", file="src/remove-item.service.ts",
        qualified_name=QUALIFIED[("src/remove-item.service.ts", "execute")], short_name="execute", line=1,
    )
    [(relation, caller, extra)] = index.inbound(callee)
    assert relation == RELATION_COMPOSED_DISPATCH != RELATION_OBSERVED_RUNTIME
    assert caller.qualified_name == QUALIFIED[("src/remove-item.controller.ts", "remove")]
    assert extra["evidence"].startswith("composed (nestjs_cqrs_command_handler)")
    assert PROVENANCE_COMPOSED_STATIC_RUNTIME == "composed_static_runtime"


def test_f_ordinary_runtime_edges_are_unchanged_and_bridges_are_not_calls() -> None:
    plain = _edge(relation=False)
    plain["caller"] = {"symbol": "js:src/a.A.run", "file": "src/a.ts", "line": 3, "origin": "repo"}
    bridged = _edge()
    bridged["caller"] = dict(plain["caller"])
    index = {"repos": [{"repo": "app", "files": [
        {"path": "src/a.ts", "symbols": [{"name": "run", "cbm_qualified_name": "app.A.run",
                                          "start_line": 3, "end_line": 9}]},
        {"path": "src/remove-item.service.ts", "symbols": [
            {"name": "execute", "cbm_qualified_name": "app.S.execute", "start_line": 10, "end_line": 12}]},
    ]}]}
    legacy, stats = _runtime(plain).observed_call_edges(index, "app", [])
    assert [(e["caller_qualified_name"], e["callee_qualified_name"]) for e in legacy] == [
        ("app.A.run", "app.S.execute")]
    assert "through_external_not_calls" not in stats
    none, stats = _runtime(bridged).observed_call_edges(index, "app", [])
    assert none == [] and stats["through_external_not_calls"] == 1
    # a through_external hop is not drawn as a call in people-facing observed paths
    changed = [{"symbol": CALLEE, "executed": True, "file": "src/remove-item.service.ts"}]

    def paths(edge: dict) -> list:
        return RuntimeEvidence({"format": "diffgenome-runtime/1", "changed_functions": changed,
                                "edges": [edge]}).observed_paths()

    assert [[s["name"] for s in p] for p in paths(plain)] == [["A.run", "RemoveItemService.execute"]]
    assert paths(bridged) == []


def test_the_report_labels_a_composed_hop_as_composed() -> None:
    from sydes.report.verify_terminal import _flow_chain_lines
    from sydes.verify.models import AffectedFlow

    composed = {"rule": "nestjs_cqrs_command_handler", "via": "CommandBus.execute",
                "message": "RemoveItemCommand"}
    flow = AffectedFlow(id="f", entry_label="DELETE /items/:id", steps=[
        {"kind": "handler", "symbol": "remove"},
        {"kind": "service_call", "symbol": "RemoveItemService.execute",
         "metadata": {"source": "layered_trace_contract", "relation": "composed_dispatch",
                      "composed": composed}},
    ])
    assert _flow_chain_lines(flow, {"execute"}) == [
        "  → remove",
        "  → [dispatch via CommandBus.execute(RemoveItemCommand), composed]",
        "  → execute [changed]",
    ]
