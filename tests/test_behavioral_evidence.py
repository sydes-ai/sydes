"""`sydes.behavioral` — consuming DiffGenome's change artifact without flattening
provenance, and degrading explicitly when it cannot be obtained. Fixtures use
only the generic result schema and a synthetic diffgenome-change/1 document;
no framework or language name appears in the module under test.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.behavioral.attach import attach_behavioral_evidence
from sydes.behavioral.diffgenome_adapter import BehavioralUnavailable, load_artifact
from sydes.behavioral.merge import merge, static_steps
from sydes.behavioral.models import (
    COMPOSED_STATE,
    GAP,
    OBSERVED_RUNTIME,
    STATUS_AVAILABLE,
    STATUS_UNAVAILABLE,
    UNRESOLVED,
)
from sydes.behavioral.render import render_markdown, render_terminal
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.models import (
    AffectedFlow,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    ChangeVerificationResult,
)

REPO = "app"


def _artifact() -> dict:
    return {
        "format": "diffgenome-change/1",
        "repository": {"root": "/r", "runtime": "x/test", "revision": "checkout"},
        "change": {"symbols": ["x:svc.Handler.create"], "symbols_never_executed": []},
        "nodes": [
            {"id": "x:web.Router.post", "name": "web.Router.post", "file": "web/router.py", "line": 3, "kind": "callable", "origin": "repo", "changed": False, "executed_by": 5},
            {"id": "x:svc.Handler.create", "name": "svc.Handler.create", "file": "svc/handler.py", "line": 10, "kind": "callable", "origin": "repo", "changed": True, "executed_by": 3},
            {"id": "x:svc.Handler.validate", "name": "svc.Handler.validate", "file": "svc/handler.py", "line": 30, "kind": "callable", "origin": "repo", "changed": False, "executed_by": 3},
            {"id": "x:store.Repo.save", "name": "store.Repo.save", "file": "store/repo.py", "line": 8, "kind": "callable", "origin": "repo", "changed": False, "executed_by": 2},
        ],
        "edges": [
            {"caller": "x:web.Router.post", "callee": "x:svc.Handler.create", "evidence": "observed", "distance": 1, "join": None, "state": "n/a", "exit": None, "probe_derived": False, "executions": ["tests/test_h.py::test_create_ok", "tests/test_h.py::test_create_bad"], "probes": [], "ambiguous": False, "rules": []},
            {"caller": "x:svc.Handler.create", "callee": "x:svc.Handler.validate", "evidence": "observed", "distance": 1, "join": None, "state": "n/a", "exit": None, "probe_derived": False, "executions": ["tests/test_h.py::test_create_ok"], "probes": [], "ambiguous": False, "rules": []},
            {"caller": "x:svc.Handler.create", "callee": "x:store.Repo.save", "evidence": "composed", "distance": 1, "join": "STATE", "state": "matched", "exit": "same", "probe_derived": False, "executions": ["tests/test_h.py::test_create_ok", "tests/test_repo.py::test_save"], "probes": [], "ambiguous": False, "rules": ["claim-member"]},
        ],
        "boundaries": [
            {"caller": "x:store.Repo.save", "target": "x:store.Repo.audit", "target_name": "store.Repo.audit", "kind": "gap", "rules": ["claim-member"], "distance": 2, "executions": []},
            {"caller": "x:svc.Handler.create", "target": "stand-in:mailer.send", "target_name": "mailer.send", "kind": "unresolved", "rules": ["no-claim"], "distance": 1, "executions": []},
        ],
        "executions": [],
        "ambiguous_seams": [{"caller": "x:svc.Handler.create", "target": "x:store.Repo.save", "accepted_candidates": 1, "rejected_candidates": 2, "rejection_reasons": {"state conflict: self.closed bool:false≠bool:true": 2}}],
        "rejected_candidates": [],
        "probes": [],
        "budget": {"writer": "none", "probes_requested": 0, "llm_calls": 0},
        "facts": {"structural_coverage_ratio_NOT_correctness": 0.8},
        "notes": [],
    }


def _result(steps: list[dict] | None = None) -> ChangeVerificationResult:
    change = ChangeSet(
        base="main", head="abc",
        symbols=[ChangedSymbol(id="app:svc/handler.py:create", repo=REPO, file="svc/handler.py", name="create", qualified_name="Handler.create")],
    )
    flows = []
    if steps is not None:
        flows.append(AffectedFlow(id="f1", entry_label="POST /things", steps=steps, sinks=[{"kind": "database", "operation": "insert", "name": "things"}]))
    return ChangeVerificationResult(change=change, affected_flows=flows)


STATIC_STEPS = [
    {"kind": "endpoint", "name": "POST /things", "file": "web/router.py", "symbol": "post"},
    {"kind": "handler", "name": "create", "file": "svc/handler.py", "symbol": "Handler.create"},
    {"kind": "service", "name": "notify", "file": "svc/notify.py", "symbol": "Notifier.notify"},
]


def test_merge_keeps_every_evidence_class_distinct() -> None:
    result = _result(STATIC_STEPS)
    ev = merge(result, _artifact(), artifact_path="a.json")
    assert ev.status == STATUS_AVAILABLE
    by = {(e.caller, e.callee): e for e in ev.edges}
    entry = by[("x:web.Router.post", "x:svc.Handler.create")]
    assert entry.evidence_class == OBSERVED_RUNTIME and entry.static_corroborated and not entry.runtime_only
    validate = by[("x:svc.Handler.create", "x:svc.Handler.validate")]
    assert validate.evidence_class == OBSERVED_RUNTIME and validate.runtime_only  # static analysis missed it
    save = by[("x:svc.Handler.create", "x:store.Repo.save")]
    assert save.evidence_class == COMPOSED_STATE and save.join == "STATE" and save.state == "matched"
    assert by[("x:store.Repo.save", "x:store.Repo.audit")].evidence_class == GAP
    assert by[("x:svc.Handler.create", "stand-in:mailer.send")].evidence_class == UNRESOLVED
    # the static step Notifier.notify has no runtime counterpart: possible, not proven
    assert [x.symbol for x in ev.static_only_steps] == ["Notifier.notify", "things"]
    assert ev.counts["static_steps"] == len(static_steps(result)) == 4  # 3 steps + 1 sink
    assert ev.counts["static_steps_executed"] == 2 and ev.counts["static_only"] == 2
    assert ev.tests_on_behavioral_path == ["tests/test_h.py::test_create_bad", "tests/test_h.py::test_create_ok", "tests/test_repo.py::test_save"]
    assert ev.changed_symbols_unmatched == []
    assert ev.seams[0].rejected_candidates == 2


def test_diff_symbol_missing_from_behavioral_run_is_reported_not_dropped() -> None:
    result = _result(STATIC_STEPS)
    result.change.symbols.append(ChangedSymbol(id="app:x.py:helper", repo=REPO, file="x.py", name="helper", qualified_name="helper"))
    # a changed test function is evidence, not behavior: never "unmatched"
    result.change.files.append(ChangedFile(repo=REPO, path="tests/test_h.py", role="test_usage_candidate"))
    result.change.symbols.append(ChangedSymbol(id="app:tests/test_h.py:test_create_ok", repo=REPO, file="tests/test_h.py", name="test_create_ok"))
    ev = merge(result, _artifact())
    assert ev.changed_symbols_unmatched == ["helper"]
    text = "\n".join(render_terminal(ev))
    assert "not seen by the behavioral run: helper" in text


def test_render_shows_grades_not_a_score() -> None:
    ev = merge(_result(STATIC_STEPS), _artifact())
    text = "\n".join(render_terminal(ev))
    assert "Reaches the change" in text and "web.Router.post" in text and "[changed]" in text
    assert "⇢ store.Repo.save    [reconstructed · STATE · state matched]" in text
    assert "Possible only" in text and "Notifier.notify  (svc/notify.py)" in text
    assert "gap: nothing executed this" in text and "unresolved stand-in" in text
    assert "3 existing test(s) executed the changed code" in text
    assert "probes: none (existing tests only)" in text
    assert "%" not in text and "confidence" not in text.lower()
    md = render_markdown(ev)
    assert md.startswith("### Behavioral effect") and "**Reaches the change" in md and "```" in md
    # the whole terminal report carries the section, after System impact
    result = _result(STATIC_STEPS)
    result.behavioral = ev
    report = render_verify_change_terminal(result)
    assert report.index("System impact") < report.index("Behavioral effect (executed evidence)")


def test_unavailable_is_explicit_and_never_no_impact(tmp_path: Path) -> None:
    result = _result(STATIC_STEPS)
    ev = attach_behavioral_evidence(result, repo_root=tmp_path, provider="diffgenome", artifact_path=tmp_path / "missing.json")
    assert ev.status == STATUS_UNAVAILABLE and "artifact not found" in (ev.reason or "")
    assert result.behavioral is ev and result.affected_flows  # structural result untouched
    assert any("Behavioral execution evidence unavailable" in n for n in result.notes)
    report = render_verify_change_terminal(result)
    assert "Behavioral execution evidence unavailable" in report and "nothing here is evidence of no impact" in report
    # DiffGenome not installed
    import os
    old = os.environ.get("SYDES_DIFFGENOME_COMMAND")
    os.environ["SYDES_DIFFGENOME_COMMAND"] = str(tmp_path / "no-such-diffgenome")
    try:
        ev2 = attach_behavioral_evidence(_result(STATIC_STEPS), repo_root=tmp_path, provider="diffgenome", runtime_args="--runtime x")
    finally:
        if old is None:
            del os.environ["SYDES_DIFFGENOME_COMMAND"]
        else:
            os.environ["SYDES_DIFFGENOME_COMMAND"] = old
    assert ev2.status == STATUS_UNAVAILABLE and "not installed" in (ev2.reason or "")
    # missing runtime configuration
    ev3 = attach_behavioral_evidence(_result(STATIC_STEPS), repo_root=tmp_path, provider="diffgenome")
    assert ev3.status == STATUS_UNAVAILABLE and "runtime configuration" in (ev3.reason or "")
    # not requested: nothing rendered
    assert "Behavioral" not in render_verify_change_terminal(_result(STATIC_STEPS))


def test_artifact_format_is_checked(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"format": "something/9"}))
    try:
        load_artifact(p)
    except BehavioralUnavailable as exc:
        assert "diffgenome-change/1" in str(exc)
    else:
        raise AssertionError("accepted a foreign artifact")


def test_attach_from_artifact_round_trips_to_json(tmp_path: Path) -> None:
    p = tmp_path / "diffgenome-change.json"
    p.write_text(json.dumps(_artifact()))
    result = _result(STATIC_STEPS)
    attach_behavioral_evidence(result, repo_root=tmp_path, provider="diffgenome", artifact_path=p)
    doc = json.loads(result.model_dump_json())
    assert doc["behavioral"]["status"] == "available"
    assert {e["evidence_class"] for e in doc["behavioral"]["edges"]} >= {OBSERVED_RUNTIME, COMPOSED_STATE, GAP, UNRESOLVED}
    assert doc["behavioral"]["static_only_steps"][0]["symbol"] == "Notifier.notify"
    assert ChangeVerificationResult.model_validate(doc).behavioral.counts["observed_runtime"] == 2


def test_observed_tests_become_supporting_evidence_not_verification(tmp_path: Path) -> None:
    from sydes.verify.models import VerificationObligation

    result = _result(STATIC_STEPS)
    flow = result.affected_flows[0]
    flow.obligations.append(VerificationObligation(id="o1", flow_id=flow.id, kind="validation", statement="rejects bad input", origin="trace_step", required=True))
    flow.obligations.append(VerificationObligation(id="o2", flow_id=flow.id, kind="validation", statement="advisory", origin="test_matrix", required=False))
    p = tmp_path / "diffgenome-change.json"
    p.write_text(json.dumps(_artifact()))
    verdict_before = result.summary.verdict
    attach_behavioral_evidence(result, repo_root=tmp_path, provider="diffgenome", artifact_path=p)
    o1, o2 = flow.obligations
    assert [t.name for t in o1.supporting_tests] == ["tests/test_h.py::test_create_bad", "tests/test_h.py::test_create_ok", "tests/test_repo.py::test_save"]
    assert all(t.match_rule == "diffgenome:observed-execution" and t.source_refs == ["diffgenome:x:svc.Handler.create"] for t in o1.supporting_tests)
    assert {t.evidence_tier for t in o1.supporting_tests} == {"C_declared"}  # executes, not asserts
    by_name = {t.name: t for t in o1.supporting_tests}
    assert by_name["tests/test_h.py::test_create_ok"].file == "tests/test_h.py"
    assert by_name["tests/test_h.py::test_create_ok"].case_name == "test_create_ok"
    assert o1.mapped_tests == [] and o1.status == "unverified"  # supporting, never verifying
    assert o2.supporting_tests == []  # advisory obligations untouched
    assert result.summary.verdict == verdict_before
    assert result.summary.counts.supporting_tests == 3 and result.summary.counts.tests_supporting_behavior == 3
    assert any("supporting evidence" in n for n in result.behavioral.notes)


def test_tests_introduced_by_the_diff_are_marked() -> None:
    from sydes.behavioral.attach import _introduced_by_diff

    added = '        {\n            name: "InsufficientBalance",\n'
    assert _introduced_by_diff("TestTransferAPI/InsufficientBalance", added)
    assert not _introduced_by_diff("TestTransferAPI/OK", added)
    assert _introduced_by_diff("tests/test_h.py::test_rejects_zero", "def test_rejects_zero():")
    assert not _introduced_by_diff("tests/test_h.py::test_rejects", "def test_rejects_zero():")


def test_failed_attach_leaves_no_partial_links(tmp_path: Path, monkeypatch) -> None:
    import sydes.behavioral.attach as attach_mod
    from sydes.verify.models import VerificationObligation

    result = _result(STATIC_STEPS)
    flow = result.affected_flows[0]
    flow.obligations.append(VerificationObligation(id="o1", flow_id=flow.id, kind="validation", statement="s", origin="trace_step", required=True))
    flow.obligations.append(VerificationObligation(id="o2", flow_id=flow.id, kind="validation", statement="t", origin="trace_step", required=True))
    p = tmp_path / "diffgenome-change.json"
    p.write_text(json.dumps(_artifact()))
    calls = {"n": 0}
    real = attach_mod._introduced_by_diff

    def flaky(name: str, added: str) -> bool:
        calls["n"] += 1
        if calls["n"] > 4:  # fail after the first obligation was already linked
            raise RuntimeError("boom")
        return real(name, added)

    monkeypatch.setattr(attach_mod, "_introduced_by_diff", flaky)
    ev = attach_behavioral_evidence(result, repo_root=tmp_path, provider="diffgenome", artifact_path=p)
    assert ev.status == STATUS_UNAVAILABLE and "boom" in (ev.reason or "")
    assert all(o.supporting_tests == [] for o in flow.obligations)


def test_diffgenome_refusal_reason_is_shown_verbatim(tmp_path: Path, monkeypatch) -> None:
    """A DiffGenome that refuses to run (no OS sandbox on the host) yields its own
    one-line reason, without the log prefix."""
    script = tmp_path / "fake-diffgenome"
    script.write_text(
        "#!/bin/sh\n"
        "echo '[diffgenome 12:00:00] refusing to run: no supported OS sandbox on this host (Linux); "
        "target tests are never executed unconfined' >&2\nexit 3\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("SYDES_DIFFGENOME_COMMAND", str(script))
    result = _result(STATIC_STEPS)
    ev = attach_behavioral_evidence(result, repo_root=tmp_path, provider="diffgenome", runtime_args="--runtime go --test-root api", out_dir=tmp_path / "out")
    assert ev.status == STATUS_UNAVAILABLE
    assert ev.reason == (
        "DiffGenome exited 3: refusing to run: no supported OS sandbox on this host (Linux); "
        "target tests are never executed unconfined"
    )


def test_missing_artifact_uses_diffgenome_status_reason(tmp_path: Path) -> None:
    """CI hand-off: the DiffGenome job refused (no sandbox on a Linux runner) and left
    only diffgenome-status.json; the Sydes job reports that reason, not 'missing file'."""
    (tmp_path / "diffgenome-status.json").write_text(json.dumps({
        "format": "diffgenome-status/1", "status": "refused",
        "reason": "refusing to run: no supported OS sandbox on this host (Linux); target tests are never executed unconfined",
    }))
    ev = attach_behavioral_evidence(_result(STATIC_STEPS), repo_root=tmp_path, provider="diffgenome", artifact_path=tmp_path / "diffgenome-change.json")
    assert ev.status == STATUS_UNAVAILABLE
    assert ev.reason.startswith("DiffGenome refused: refusing to run: no supported OS sandbox")


def test_failing_executions_are_separated_from_supporting_ones() -> None:
    art = _artifact()
    art["executions"] = [
        {"id": "tests/test_h.py::test_create_bad", "stimulus": "existing_test", "file": "tests/test_h.py", "outcome": "failed"},
        {"id": "tests/test_h.py::test_create_ok", "stimulus": "existing_test", "file": "tests/test_h.py", "outcome": "passed"},
    ]
    ev = merge(_result(STATIC_STEPS), art)
    assert ev.tests_failed_on_path == ["tests/test_h.py::test_create_bad"]
    assert "tests/test_h.py::test_create_bad" not in ev.tests_on_behavioral_path
    assert "FAILED in the isolated run: tests/test_h.py::test_create_bad" in "\n".join(render_terminal(ev))


def test_unmapped_bullet_only_when_nothing_is_mapped(tmp_path: Path) -> None:
    from sydes.verify.models import VerificationObligation

    p = tmp_path / "diffgenome-change.json"
    p.write_text(json.dumps(_artifact()))
    result = _result(STATIC_STEPS)
    result.affected_flows[0].obligations.append(VerificationObligation(id="o", flow_id="f1", kind="validation", statement="s", origin="trace_step", required=True))
    attach_behavioral_evidence(result, repo_root=tmp_path, provider="diffgenome", artifact_path=p)
    assert "none is mapped as asserting" in render_verify_change_terminal(result)
    result.summary.counts.mapped_tests = 2
    assert "none is mapped as asserting" not in render_verify_change_terminal(result)


def _genome_summary() -> dict:
    return {
        "format": "diffgenome-genome-summary/1",
        "decision_rules": [
            {"id": "d1", "status": "verified", "entity": "token.Payload.Valid", "file": "token/payload.go",
             "line": 55, "source": "payload.Type != tokenType", "meaning": "issued_type != expected_type",
             "outcome_true": "token.Payload.Valid returned-error"},
            {"id": "d2", "status": "supported", "entity": "gapi.hasPermission", "file": "gapi/authorization.go",
             "line": 60, "source": "role == userRole", "meaning": "role == userRole"},
        ],
        "identities": [{"name": "expected_type", "status": "verified",
                        "same_value_at": ["VerifyToken arg:tokenType", "Payload.Valid arg:tokenType"]}],
        "literals": [{"name": "is_access", "at": "Valid arg:tokenType", "equals": 1, "written_at": "token/payload.go:20"}],
        "consistency": {"scenarios": 20, "consistent": 20, "indeterminate": 0, "contradicted": 0},
        "statuses": {"verified": 13, "supported": 60, "hypothesis": 0, "rejected": 2, "contradicted_not_exported": 1},
        "unknowns": [{"what": "status codes inside error values", "why": "digest covers the message"}],
    }


def test_genome_summary_is_rendered_as_checked_rules_and_never_changes_the_graph() -> None:
    art = _artifact()
    plain = merge(_result(STATIC_STEPS), art)
    art["genome"] = _genome_summary()
    ev = merge(_result(STATIC_STEPS), art)
    assert ev.genome and ev.genome["format"] == "diffgenome-genome-summary/1"
    assert [e.model_dump() for e in ev.edges] == [e.model_dump() for e in plain.edges]
    text = "\n".join(render_terminal(ev))
    assert "Checked behavioral rules" in text
    assert "✔ verified   token.Payload.Valid token/payload.go:55 `payload.Type != tokenType`" in text
    assert "~ consistent gapi.hasPermission" in text
    assert "same value (verified): expected_type" in text
    assert "20/20 per-test prediction(s) exactly" in text
    assert "not shown: 3 claim(s)" in text
    assert "Checked behavioral rules" in render_markdown(ev)
    assert "Checked behavioral rules" not in "\n".join(render_terminal(plain))


def test_unknown_genome_format_is_ignored() -> None:
    art = _artifact()
    art["genome"] = {"format": "something-else/9", "decision_rules": [{"status": "verified"}]}
    ev = merge(_result(STATIC_STEPS), art)
    assert ev.genome is None
