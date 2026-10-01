"""Runtime evidence (`diffgenome-runtime/1`) as a first-class input to Sydes: reachability,
test mapping, verification gaps and the AI-recovery decision. Sydes reads only the contract."""

from __future__ import annotations

import json
from pathlib import Path

from sydes.behavioral.answers import runtime_executed_any, runtime_open_symbols
from sydes.behavioral.attach import _apply_runtime
from sydes.behavioral.context import BehavioralContext, review_preamble
from sydes.behavioral.models import STATUS_AVAILABLE, BehavioralEvidence
from sydes.behavioral.render import render_terminal
from sydes.behavioral.runtime import EDGE_SOURCE, RuntimeEvidence
from sydes.code_intelligence.base import StructuralFacts
from sydes.impact import ImpactInterpreter
from sydes.impact.models import (
    PROVENANCE_RUNTIME_OBSERVED,
    RELATION_OBSERVED_RUNTIME,
    STRATEGY_RUNTIME_OBSERVED_REACHABILITY,
)
from sydes.recovery.trigger import GAP_MISSING_TEST_MAPPING, GAP_UNRESOLVED_CHANGED_SYMBOLS, answered_by_behavioral
from sydes.verify.models import ChangeSet, ChangeVerificationResult, VerificationGap

REPO = "app"


# -- the interpreter: an observed edge is walked like a static one, and says so ----------------


def _edge(caller: str, callee: str, **extra) -> dict:
    return {
        "repo": REPO,
        "caller_file": "app/svc.py", "caller_symbol": caller, "caller_qualified_name": f"proj.app.{caller}",
        "caller_line": 1,
        "callee_file": "app/svc.py", "callee_symbol": callee, "callee_qualified_name": f"proj.app.{callee}",
        "callee_line": 20,
        **extra,
    }


def _interpret(edges: list[dict]):
    facts = StructuralFacts(
        call_edges=edges,
        entrypoints=[{
            "repo": REPO, "qualified_name": "proj.app.handler", "symbol": "handler", "file": "app/svc.py",
            "line": 1, "route_method": "POST", "route_path": "/x", "decorators": "", "signature": "",
        }],
        symbol_index={"repos": [{"repo": REPO, "files": []}]},
        route_index={"repos": [{"repo": REPO, "files": []}]},
        provides_call_graph=True, backend="cbm",
    )
    return ImpactInterpreter().interpret([{"name": "service", "file": "app/svc.py", "repo": REPO}], facts, repo=REPO)


def test_observed_edge_lets_sydes_reach_its_own_entrypoint_and_says_so() -> None:
    assert not _interpret([]).affected
    result = _interpret([_edge("handler", "service", source=EDGE_SOURCE, evidence="observed in 2 tests")])
    [entry] = result.affected
    [path] = entry.paths
    assert path.strategy == STRATEGY_RUNTIME_OBSERVED_REACHABILITY
    assert path.steps[0].relation == RELATION_OBSERVED_RUNTIME
    assert path.steps[0].provenance == PROVENANCE_RUNTIME_OBSERVED


def test_static_edge_keeps_its_static_label() -> None:
    [entry] = _interpret([_edge("handler", "service")]).affected
    assert entry.paths[0].strategy != STRATEGY_RUNTIME_OBSERVED_REACHABILITY


# -- the contract ---------------------------------------------------------------------------------


def _loc(symbol: str, line: int, origin: str = "repo", file: str = "app/svc.py") -> dict:
    return {"symbol": symbol, "file": file, "line": line, "origin": origin}


def _contract() -> dict:
    return {
        "format": "diffgenome-runtime/1",
        "universe": {"test_scope": "tests/", "executions": 3, "passed": 3, "failed": 0, "tests": []},
        "changed_functions": [
            {**_loc("py:app.svc.service", 20), "executed": True, "calls": 2, "tests": ["t::a", "t::b"],
             "tests_total": 2, "exits": {"returned": 2}, "raised_inside": {}, "arg_shapes": [], "callers": [],
             "stand_ins": {"py:db.save": 1},
             "changed_sites": [{"site": "br:1", "line": 22, "predicate": "amount > 0", "true": 2, "false": 0}]},
            {**_loc("py:app.svc.only_tested", 40), "executed": True, "calls": 1, "tests": ["t::c"],
             "tests_total": 1, "exits": {"returned": 1}, "raised_inside": {}, "arg_shapes": [], "callers": [],
             "stand_ins": {}, "changed_sites": []},
            {**_loc("py:app.svc.unused", 60), "executed": False, "calls": 0, "tests": [], "tests_total": 0,
             "exits": {}, "raised_inside": {}, "arg_shapes": [], "callers": [], "stand_ins": {}, "changed_sites": []},
        ],
        "edges": [
            {"caller": _loc("py:tests.test_x", 1, "test", "tests/test_x.py"), "callee": _loc("py:app.svc.handler", 1),
             "executions": 2, "tests": ["t::a"], "tests_total": 2},
            {"caller": _loc("py:app.svc.handler", 1), "callee": _loc("py:app.svc.service", 20),
             "executions": 2, "tests": ["t::a"], "tests_total": 2},
            {"caller": _loc("py:tests.test_x", 1, "test", "tests/test_x.py"), "callee": _loc("py:app.svc.only_tested", 40),
             "executions": 1, "tests": ["t::c"], "tests_total": 1},
        ],
        "boundaries": [],
        "gaps": [
            {"kind": "function_not_executed", **_loc("py:app.svc.unused", 60)},
            {"kind": "branch_outcome_not_observed", **_loc("py:app.svc.service", 22), "predicate": "amount > 0",
             "outcome": "false"},
            {"kind": "stand_in_reached", **_loc("py:app.svc.service", 20), "target": "py:db.save", "calls": 1},
        ],
        "not_reported": ["values (only type shapes and opaque fingerprints are recorded)"],
    }


def _index() -> dict:
    def s(name: str, start: int, end: int) -> dict:
        return {"name": name, "start_line": start, "end_line": end, "cbm_qualified_name": f"proj.app.{name}"}

    return {"repos": [{"repo": REPO, "files": [{"path": "app/svc.py", "symbols": [
        s("handler", 1, 5), s("service", 20, 30), s("only_tested", 40, 45), s("unused", 60, 65),
    ]}]}]}


def test_contract_is_read_only_when_present_and_versioned() -> None:
    assert RuntimeEvidence.from_artifact({"format": "diffgenome-change/1"}) is None
    assert RuntimeEvidence.from_artifact({"runtime": {"format": "other/9"}}) is None
    rt = RuntimeEvidence.from_artifact({"runtime": _contract()})
    assert rt is not None and rt.test_scope == "tests/"
    assert [f["symbol"] for f in rt.not_executed()] == ["py:app.svc.unused"]
    assert rt.tests() == ["t::a", "t::b", "t::c"]


def test_reachability_edges_resolve_to_index_symbols_and_skip_test_frames_and_static_duplicates() -> None:
    rt = RuntimeEvidence(_contract())
    edges, stats = rt.observed_call_edges(_index(), REPO, existing=[])
    assert [(e["caller_symbol"], e["callee_symbol"], e["source"]) for e in edges] == [("handler", "service", EDGE_SOURCE)]
    static = [{"caller_qualified_name": "proj.app.handler", "callee_qualified_name": "proj.app.service"}]
    edges, stats = rt.observed_call_edges(_index(), REPO, existing=static)
    assert edges == [] and stats["already_static"] == 1
    assert rt.candidate_files() == {"app/svc.py"}


def test_entry_roots_are_topmost_application_frames() -> None:
    rt = RuntimeEvidence(_contract())
    assert [r["symbol"] for r in rt.entry_roots("py:app.svc.service")] == ["py:app.svc.handler"]
    assert rt.entry_roots("py:app.svc.only_tested") == []  # reached from test code only


def test_runtime_gaps_are_verification_gaps_with_runtime_source() -> None:
    gaps = RuntimeEvidence(_contract()).verification_gaps()
    assert all(isinstance(g, VerificationGap) and g.source == "runtime" for g in gaps)
    text = " | ".join(g.behavior for g in gaps)
    assert "no existing test executed it" in text
    assert "never observed false" in text
    assert "only through a stand-in" in text


def test_recovery_needed_only_where_runtime_cannot_answer() -> None:
    rt = RuntimeEvidence(_contract())
    # service: executed with an application entry root -> answered; unused: never ran;
    # only_tested: ran from tests only; RUN_LOCK_KEY: not a changed function -> stays open
    assert rt.unresolved_needing_recovery(["service", "unused", "only_tested", "RUN_LOCK_KEY"]) == [
        "unused", "only_tested", "RUN_LOCK_KEY",
    ]


def _result_with_runtime(unresolved: list[str]) -> ChangeVerificationResult:
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    result.unresolved_changed_symbols = len(unresolved)
    result.unresolved_changed_symbol_names = unresolved
    ev = BehavioralEvidence(status=STATUS_AVAILABLE)
    _apply_runtime(result, ev, RuntimeEvidence(_contract()))
    result.behavioral = ev
    return result


def test_attach_adds_runtime_gaps_without_touching_the_verdict() -> None:
    result = _result_with_runtime(["service"])
    verdict = result.summary.verdict
    assert [g.source for g in result.verification_gaps] == ["runtime"] * 3
    assert result.summary.counts.verification_gaps == 3
    assert result.summary.verdict == verdict
    assert runtime_executed_any(result.behavioral)


def test_answered_gaps_follow_the_runtime_evidence() -> None:
    answered = answered_by_behavioral(_result_with_runtime(["service"]))
    assert GAP_MISSING_TEST_MAPPING in answered and GAP_UNRESOLVED_CHANGED_SYMBOLS in answered
    still = _result_with_runtime(["service", "unused"])
    assert GAP_UNRESOLVED_CHANGED_SYMBOLS not in answered_by_behavioral(still)
    assert runtime_open_symbols(still.behavioral, ["service", "unused"]) == ["unused"]


def test_render_shows_runtime_section() -> None:
    result = _result_with_runtime([])
    text = "\n".join(render_terminal(result.behavioral))
    assert "Runtime evidence (existing tests run against the change; scope: tests/)" in text
    assert "✓ service" in text and "entered via handler" in text
    assert "✗ unused" in text and "no existing test executed it" in text


def test_context_loads_from_artifact_and_preamble_is_contract_based(tmp_path: Path) -> None:
    art = tmp_path / "a.json"
    art.write_text(json.dumps({"format": "diffgenome-change/1", "runtime": _contract()}))
    ctx = BehavioralContext.load(art)
    assert ctx is not None
    assert "Changed functions NO test executed: unused" in review_preamble(ctx)
    art.write_text(json.dumps({"format": "diffgenome-change/1"}))
    assert BehavioralContext.load(art) is None
    assert BehavioralContext.load(tmp_path / "missing.json") is None


def test_intermediate_frame_entered_from_tests_is_also_a_root() -> None:
    c = _contract()
    c["edges"] += [
        {"caller": _loc("py:app.svc.service", 20), "callee": _loc("py:app.svc.deep", 50),
         "executions": 1, "tests": ["t::a"], "tests_total": 1},
        {"caller": _loc("py:tests.test_y", 1, "test", "tests/test_y.py"), "callee": _loc("py:app.svc.service", 20),
         "executions": 1, "tests": ["t::d"], "tests_total": 1},
    ]
    roots = {r["symbol"] for r in RuntimeEvidence(c).entry_roots("py:app.svc.deep")}
    assert roots == {"py:app.svc.handler", "py:app.svc.service"}


def test_diffgenome_dependency_is_found_beside_the_interpreter(tmp_path: Path, monkeypatch) -> None:
    import sys

    from sydes.behavioral import diffgenome_adapter as adapter

    (tmp_path / "diffgenome").write_text("")
    monkeypatch.delenv(adapter.COMMAND_ENV_VAR, raising=False)
    monkeypatch.setattr(adapter.shutil, "which", lambda _: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert adapter.resolve_command() == [str(tmp_path / "diffgenome")]
    monkeypatch.setenv(adapter.COMMAND_ENV_VAR, "uv run diffgenome")
    assert adapter.resolve_command() == ["uv", "run", "diffgenome"]


def test_impact_reached_only_through_observed_edges_is_labelled_runtime_observed() -> None:
    from sydes.verify.analyzer import _impact_provenance

    [observed] = _interpret([_edge("handler", "service", source=EDGE_SOURCE, evidence="observed")]).affected
    [static] = _interpret([_edge("handler", "service")]).affected
    assert _impact_provenance(observed) == PROVENANCE_RUNTIME_OBSERVED
    assert _impact_provenance(static) == "structural"


def test_observed_paths_run_from_entry_root_to_changed_code() -> None:
    summary = RuntimeEvidence(_contract()).summary()
    assert summary["paths"] == [[{"name": "handler", "changed": False}, {"name": "service", "changed": True}]]
    assert summary["tests_exercised"] == 3


def test_live_run_selects_python_tests_when_none_are_given(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from sydes.behavioral import diffgenome_adapter
    from sydes.behavioral.test_selection import TestSelection
    from sydes.cli import verify_change

    root = tmp_path / "r"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["commit", "-q", "--allow-empty", "-m", "a"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    seen: list[list[str]] = []
    monkeypatch.setattr(diffgenome_adapter, "run_diffgenome", lambda req: seen.append(req.runtime_args) or ({}, []))
    monkeypatch.setattr(
        "sydes.behavioral.test_selection.select_python_tests",
        lambda *a, **k: TestSelection(files=["t/test_a.py", "t/test_b.py"], reasons={"t/test_a.py": "changed in this diff", "t/test_b.py": "calls f"}),
    )
    _, note, selection = verify_change._run_diffgenome_before_analysis(root, "HEAD", "--runtime python", 0, tmp_path / "o")
    assert note == "" and selection is not None and selection["files"] == ["t/test_a.py", "t/test_b.py"]
    assert seen[-1] == ["--runtime", "python", "--tests", "t/test_a.py", "--pytest-arg=t/test_b.py"]
    # tests given explicitly: used as is, no selection
    _, _, selection = verify_change._run_diffgenome_before_analysis(root, "HEAD", "--runtime python --tests x.py", 0, tmp_path / "o")
    assert selection is None and seen[-1] == ["--runtime", "python", "--tests", "x.py"]


def test_failed_live_run_is_reported_not_rerun(tmp_path: Path, monkeypatch) -> None:
    from sydes.behavioral import attach as attach_mod

    monkeypatch.setattr(attach_mod, "run_diffgenome", lambda req: (_ for _ in ()).throw(AssertionError("reran")))
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    ev = attach_mod.attach_behavioral_evidence(
        result, repo_root=tmp_path, provider="diffgenome", artifact_path=None,
        runtime_args="--runtime python", unavailable_reason="DiffGenome timed out after 900s",
    )
    assert ev.status != STATUS_AVAILABLE and ev.reason == "DiffGenome timed out after 900s"
