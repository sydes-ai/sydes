"""Evidence-constrained code review: Finder -> Investigator -> Verifier.

Caps hold, every lookup is recorded with its reason and provenance, only a CONFIRMED,
source-verified, change-anchored claim becomes a finding, nothing outside the evidence
packet reaches a prompt, the artifact serialises, and the default review is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sydes.llm.client import LLMResponse
from sydes.recovery.tools import RepoTools
from sydes.verify import investigation as inv
from sydes.verify.analyzer import VerifyChangeOptions

SOURCE = """\
def stream(writer, source):
    try:
        for chunk in source:
            yield writer.write(chunk)
    finally:
        writer.close()
"""
DIFF = """\
diff --git a/app/stream.py b/app/stream.py
--- a/app/stream.py
+++ b/app/stream.py
@@ -1,4 +1,6 @@
 def stream(writer, source):
+    try:
+        for chunk in source:
+            yield writer.write(chunk)
+    finally:
+        writer.close()
"""


CONSUMER = """\
def consume(gen):
    for item in gen:
        return item
"""
HELPER = """\


def helper():
    return compute_total(1)
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "stream.py").write_text(SOURCE + HELPER)  # lines 2-6 changed, 1 and 7+ unchanged
    (tmp_path / "app" / "consumer.py").write_text(CONSUMER)      # unchanged file
    return tmp_path


def context(**extra) -> dict:
    return {"change": {"files": [{"path": "app/stream.py", "changed_ranges": [[2, 6]]}],
                       "symbols": [{"file": "app/stream.py", "name": "stream", "kind": "function", "start_line": 1,
                                    "end_line": 6, "changed_region": SOURCE}]},
            "diff": DIFF, "related_code": [], **extra}


RESULT = SimpleNamespace(affected_flows=[], test_executions=[], ci_suite=None, behavioral=None, verification_gaps=[],
                         unresolved_changed_symbol_names=["stream"], pr_semantic_analysis=None)


class Scripted:
    """Replies per stage, chosen by the system prompt; records every prompt."""

    def __init__(self, finder: dict, investigator: list[dict] | None = None, verifier: list[dict] | None = None) -> None:
        self.queues = {"FINDER": [finder], "INVESTIGATOR": list(investigator or []), "VERIFIER": list(verifier or [])}
        self.prompts: list[str] = []

    def generate(self, request):
        role = next(r for r in ("FINDER", "INVESTIGATOR", "VERIFIER") if f"You are the {r}" in (request.system or ""))
        self.prompts.append(request.prompt)
        queue = self.queues[role]
        reply = queue.pop(0) if queue else ({"final": {"summary": "done"}} if role == "INVESTIGATOR" else {"status": "UNKNOWN"})
        return LLMResponse(text=json.dumps(reply), usage={"prompt_tokens": 10, "completion_tokens": 4})


def hypothesis(n: int, line: int = 6) -> dict:
    return {"claim": f"claim {n}", "why_suspected": "w", "file": "app/stream.py", "line": line, "severity": "P1",
            "initial_evidence": ["E1"], "questions_to_resolve": ["q"]}


REAL = {"source": "L1", "file": "app/stream.py", "line": 6, "quote": "writer.close()"}
INVENTED = {"source": "L1", "file": "app/stream.py", "line": 6, "quote": "writer.flush_and_release(force=True)"}
CHANGED = {"source": "E1", "file": "app/stream.py", "line": 3, "quote": "for chunk in source:"}
CONFIRM = {"status": "CONFIRMED", "supported_claim": "the writer closes only on exhaustion", "reasoning": "r",
           "defect_evidence": [REAL], "changed_evidence": [CHANGED], "causal_link": "the change moved close into finally"}


def run(repo: Path, client: Scripted, **kwargs):
    return inv.run(context=kwargs.pop("ctx", context()), result=RESULT, repo_root=repo, llm_client=client,
                   tools=RepoTools(repo), **kwargs)


def test_the_finder_is_capped_at_five_hypotheses(repo: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(n) for n in range(8)]})
    _, artifact, _ = run(repo, client)
    assert [h["id"] for h in artifact["hypotheses"]] == ["H1", "H2", "H3", "H4", "H5"]
    assert all(h["initial_evidence"] == ["E1"] for h in artifact["hypotheses"])


def test_lookups_are_capped_per_pr_and_recorded_with_reason_and_provenance(repo: Path) -> None:
    read = {"tool": "read_source", "args": {"path": "app/stream.py", "start_line": 1, "end_line": 6}, "why": "see the cleanup"}
    tests = {"tool": "tests_for", "args": {"term": "stream"}, "why": "is it tested"}
    client = Scripted({"hypotheses": [hypothesis(1), hypothesis(2)]}, investigator=[read, tests, read, read, read, read])
    _, artifact, _ = run(repo, client, max_lookups=3)
    lookups = artifact["lookups"]
    assert len(lookups) == 3 and artifact["budget"] == {"max_hypotheses": 5, "max_lookups": 3, "used_lookups": 3,
                                                         "max_lookups_per_hypothesis": 4}
    assert [(l["tool"], l["provenance"], l["why"]) for l in lookups[:2]] == [
        ("read_source", "source_read", "see the cleanup"), ("tests_for", "test_evidence", "is it tested")]
    assert lookups[0]["hypothesis"] == "H1" and "writer.close()" in lookups[0]["result_excerpt"]
    assert {e["kind"] for e in artifact["packet"]} >= {"diff", "changed_symbol", "gap"}


def test_only_a_confirmed_source_verified_anchored_claim_becomes_a_finding(repo: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(1), hypothesis(2), hypothesis(3)]},
                      verifier=[CONFIRM,
                                {"status": "REFUTED", "reasoning": "callers exhaust it", "refuting": [{"source": "E1"}]},
                                {**CONFIRM, "defect_evidence": [INVENTED]}])
    findings, artifact, notes = run(repo, client)
    assert [(f.file, f.line, f.title, f.source) for f in findings] == [
        ("app/stream.py", 6, "the writer closes only on exhaustion", "investigator")]
    assert artifact["confirmed"] == ["H1"] and artifact["refuted"] == ["H2"] and artifact["unknown"] == ["H3"]
    by_id = {o["hypothesis"]: o for o in artifact["outcomes"]}
    assert any("no defect evidence could be verified" in m for m in by_id["H3"]["missing"])
    assert by_id["H1"]["verified_defect_evidence"][0]["quote"] == "writer.close()"
    assert by_id["H1"]["causal_link"] == "the change moved close into finally"
    assert "confirmed=1 refuted=1 unknown=1 findings=1" in notes[0]


def _single(repo: Path, verdict: dict, *, file: str = "app/stream.py", line: int = 6):
    client = Scripted({"hypotheses": [{**hypothesis(1), "file": file, "line": line}]}, verifier=[verdict])
    return run(repo, client)


def test_a_defect_on_a_changed_line_is_confirmed(repo: Path) -> None:
    findings, artifact, _ = _single(repo, CONFIRM)
    assert artifact["confirmed"] == ["H1"] and [(f.file, f.line) for f in findings] == [("app/stream.py", 6)]


def test_a_defect_on_an_unchanged_adjacent_line_caused_by_changed_code_is_confirmed(repo: Path) -> None:
    unchanged = {"source": "L1", "file": "app/stream.py", "line": 1, "quote": "def stream(writer, source):"}
    findings, artifact, _ = _single(repo, {**CONFIRM, "defect_evidence": [unchanged]}, line=1)
    assert artifact["confirmed"] == ["H1"] and [(f.file, f.line) for f in findings] == [("app/stream.py", 1)]


def test_a_defect_in_an_unchanged_consumer_file_caused_by_the_change_is_confirmed(repo: Path) -> None:
    consumer = {"source": "L2", "file": "app/consumer.py", "line": 3, "quote": "return item"}
    findings, artifact, _ = _single(repo, {**CONFIRM, "defect_evidence": [consumer]}, file="app/consumer.py", line=3)
    assert artifact["confirmed"] == ["H1"] and [(f.file, f.line) for f in findings] == [("app/consumer.py", 3)]
    assert findings[0].impact == "Caused by the change: the change moved close into finally"


@pytest.mark.parametrize("tie", [
    {"changed_evidence": []},  # nothing from the change
    {"changed_evidence": [{"source": "L3", "file": "app/stream.py", "line": 10, "quote": "return compute_total(1)"}]},
    {"causal_link": ""},       # change quoted, but no causal link stated
])
def test_an_unrelated_pre_existing_defect_in_unchanged_code_is_not_confirmed(repo: Path, tie: dict) -> None:
    consumer = {"source": "L2", "file": "app/consumer.py", "line": 3, "quote": "return item"}
    findings, artifact, _ = _single(repo, {**CONFIRM, "defect_evidence": [consumer], **tie}, file="app/consumer.py", line=3)
    assert findings == [] and artifact["unknown"] == ["H1"]
    assert any(m.startswith("downgraded from CONFIRMED") for m in artifact["outcomes"][0]["missing"])


def test_unknown_is_never_turned_into_a_finding(repo: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(1)]},
                      verifier=[{"status": "UNKNOWN", "missing": ["whether callers exhaust the stream"]}])
    findings, artifact, _ = run(repo, client)
    assert findings == [] and artifact["unknown"] == ["H1"]


def test_the_verifier_does_not_see_the_investigators_reasons(repo: Path) -> None:
    read = {"tool": "read_source", "args": {"path": "app/stream.py"}, "why": "SECRET-INVESTIGATOR-REASON"}
    client = Scripted({"hypotheses": [hypothesis(1)]}, investigator=[read], verifier=[CONFIRM])
    run(repo, client)
    verifier_prompt = client.prompts[-1]
    assert "SECRET-INVESTIGATOR-REASON" not in verifier_prompt and "writer.close()" in verifier_prompt


def test_nothing_outside_the_evidence_packet_reaches_any_prompt(repo: Path) -> None:
    ctx = context(reference_comments=[{"text": "AACR-REFERENCE-SENTINEL"}], expected_findings="EXPECTED-SENTINEL")
    client = Scripted({"hypotheses": [hypothesis(1)]}, verifier=[CONFIRM])
    run(repo, client, ctx=ctx)
    assert client.prompts and not any("SENTINEL" in p for p in client.prompts)


def test_the_artifact_serialises_with_its_required_fields(repo: Path, tmp_path: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(1)]}, verifier=[CONFIRM])
    _, artifact, _ = run(repo, client)
    out = tmp_path / "x.investigation.json"
    out.write_text(json.dumps(artifact))
    loaded = json.loads(out.read_text())
    assert loaded["version"] == inv.VERSION
    assert set(loaded) >= {"hypotheses", "lookups", "confirmed", "refuted", "unknown", "budget", "outcomes", "packet"}
    assert loaded["budget"]["max_lookups"] == inv.MAX_LOOKUPS


def test_without_the_flag_code_review_is_unchanged() -> None:
    options = VerifyChangeOptions()
    assert options.investigator is False and options.investigation_out is None and options.code_review is False


# ----------------------------------------------------------------------------- allocation experiment

READ = {"tool": "read_source", "args": {"path": "app/stream.py", "start_line": 1, "end_line": 6}, "why": "SECRET-INVESTIGATOR-REASON"}
UNKNOWN = {"status": "UNKNOWN", "missing": ["m"]}


def _per_hypothesis(artifact: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in artifact["lookups"]:
        counts[record["hypothesis"]] = counts.get(record["hypothesis"], 0) + 1
    return counts


def test_two_round_gives_every_hypothesis_two_lookups_before_any_gets_more(repo: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(n) for n in range(5)]}, investigator=[READ] * 40,
                      verifier=[UNKNOWN] * 10)
    _, artifact, _ = run(repo, client, allocation="two_round")
    assert _per_hypothesis(artifact) == {"H1": 4, "H2": 2, "H3": 2, "H4": 2, "H5": 2}
    assert artifact["budget"]["used_lookups"] == 12 and artifact["budget"]["allocation"] == "two_round"
    h1 = next(o for o in artifact["outcomes"] if o["hypothesis"] == "H1")
    assert [r["round"] for r in h1["rounds"]] == [1, 2] and h1["rounds"][1]["verified_again"]
    assert h1["rounds"][1]["new_nonempty_lookups"] == ["L11", "L12"]
    assert not any("SECRET-INVESTIGATOR-REASON" in p for p in client.prompts if p.startswith("{"))


def test_two_round_skips_decided_hypotheses_and_caps_each_at_six(repo: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(1), hypothesis(2)]}, investigator=[READ] * 40,
                      verifier=[CONFIRM, UNKNOWN, UNKNOWN])
    _, artifact, _ = run(repo, client, allocation="two_round")
    assert _per_hypothesis(artifact) == {"H1": 2, "H2": 6}
    assert [len(o["rounds"]) for o in artifact["outcomes"]] == [1, 2]
    assert sum(1 for p in client.prompts if p.startswith("{")) == 3  # verifier prompts: H1, H2, H2 again


def test_two_round_does_not_redraw_a_verdict_without_new_lookups(repo: Path) -> None:
    final = {"final": {"summary": "nothing more to look up"}}
    client = Scripted({"hypotheses": [hypothesis(1)]}, investigator=[READ, READ, final], verifier=[UNKNOWN, CONFIRM])
    _, artifact, _ = run(repo, client, allocation="two_round")
    outcome = artifact["outcomes"][0]
    assert outcome["status"] == "UNKNOWN" and outcome["rounds"][1] == {
        "round": 2, "lookups": [], "verified_again": False, "new_nonempty_lookups": [], "status": "UNKNOWN",
        "status_changed": False, "missing": []}


def test_sequential_is_the_default_and_leaves_the_artifact_unchanged(repo: Path) -> None:
    client = Scripted({"hypotheses": [hypothesis(1)]}, investigator=[READ] * 8, verifier=[UNKNOWN])
    _, artifact, _ = run(repo, client)
    assert _per_hypothesis(artifact) == {"H1": 4}
    assert "allocation" not in artifact["budget"] and "rounds" not in artifact["outcomes"][0] and "replay" not in artifact


def test_snapshot_and_replay_investigate_the_same_hypotheses_without_the_finder(repo: Path, tmp_path: Path) -> None:
    snapshot_path = tmp_path / "x.snapshot.json"
    ctx = context(reference_comments=[{"text": "AACR-REFERENCE-SENTINEL"}])
    first = Scripted({"hypotheses": [hypothesis(1), hypothesis(2)]}, verifier=[CONFIRM, UNKNOWN])
    run(repo, first, ctx=ctx, snapshot_out=snapshot_path)
    text = snapshot_path.read_text()
    snapshot = json.loads(text)
    assert "SENTINEL" not in text and [h["id"] for h in snapshot["hypotheses"]] == ["H1", "H2"]
    assert any(e["kind"] == "diff" and "writer.close()" in e["text"] for e in snapshot["packet"])

    again = Scripted({"hypotheses": [hypothesis(9)]}, investigator=[READ] * 20, verifier=[CONFIRM, UNKNOWN, UNKNOWN])
    findings, artifact, _ = inv.replay(snapshot, repo_root=repo, llm_client=again, tools=RepoTools(repo),
                                       allocation="two_round")
    assert again.queues["FINDER"] and [h["claim"] for h in artifact["hypotheses"]] == ["claim 1", "claim 2"]
    assert artifact["replay"]["allocation"] == "two_round" and [f.line for f in findings] == [6]


def test_a_finder_failure_in_the_snapshot_stays_an_error_of_the_replay(repo: Path) -> None:
    snapshot = {"version": inv.SNAPSHOT_VERSION, "packet": [], "hypotheses": [], "changed_ranges": {},
                "finder_errors": ["finder: Request timed out."]}
    _, artifact, _ = inv.replay(snapshot, repo_root=repo, llm_client=Scripted({"hypotheses": []}), tools=RepoTools(repo))
    assert artifact["errors"] == ["finder: Request timed out."]


# ----------------------------------------------------------------------------- Verifier strictness (experiment)

#: sha256 of VERIFIER_SYSTEM at c54d97e, before the strictness experiment.
CONTROL_VERIFIER_SHA256 = "b58a341f9c78c45106cc8e62d860b08099087b28ae137f30e1204223be03daff"


class Recording(Scripted):
    """Scripted, also recording each request's system prompt with its user prompt."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[tuple[str, str]] = []

    def generate(self, request):
        self.requests.append((request.system or "", request.prompt))
        return super().generate(request)

    def verifier_requests(self) -> list[tuple[str, str]]:
        return [(system, prompt) for system, prompt in self.requests if "You are the VERIFIER" in system]


def test_the_default_verifier_prompt_is_byte_identical_to_the_control() -> None:
    import hashlib
    assert hashlib.sha256(inv.VERIFIER_SYSTEM.encode()).hexdigest() == CONTROL_VERIFIER_SHA256
    assert inv.VERIFIER_PROMPTS["control"] is inv.VERIFIER_SYSTEM


def test_the_strict_prompt_is_the_control_plus_only_the_frozen_paragraph() -> None:
    strict = inv.VERIFIER_SYSTEM_STRICT
    assert strict != inv.VERIFIER_SYSTEM
    assert strict.replace(inv.VERIFIER_STANDARD_OF_PROOF + " ", "", 1) == inv.VERIFIER_SYSTEM
    assert "Do not confirm on plausibility. Standard of proof." in strict
    assert "count only when they are present in the supplied evidence" in inv.VERIFIER_STANDARD_OF_PROOF


def test_the_strict_prompt_is_used_only_when_selected(repo: Path) -> None:
    default = Recording({"hypotheses": [hypothesis(1)]}, verifier=[CONFIRM])
    _, plain, _ = run(repo, default)
    assert [s for s, _ in default.verifier_requests()] == [inv.VERIFIER_SYSTEM] and "verifier_prompt" not in plain
    strict = Recording({"hypotheses": [hypothesis(1)]}, verifier=[CONFIRM])
    _, artifact, _ = run(repo, strict, verifier_prompt="strict")
    assert [s for s, _ in strict.verifier_requests()] == [inv.VERIFIER_SYSTEM_STRICT]
    assert artifact["verifier_prompt"] == "strict"
    assert VerifyChangeOptions().investigator_verifier_prompt == "control"
    with pytest.raises(ValueError):
        run(repo, Scripted({"hypotheses": []}), verifier_prompt="lenient")


@pytest.mark.parametrize("prompt", ["control", "strict"])
def test_check_confirmation_downgrades_an_unsupported_confirmed_under_either_prompt(repo: Path, prompt: str) -> None:
    unsupported = [{**CONFIRM, "defect_evidence": [INVENTED]}, {**CONFIRM, "changed_evidence": []},
                   {**CONFIRM, "causal_link": ""}, {**CONFIRM, "supported_claim": ""}]
    client = Scripted({"hypotheses": [hypothesis(n) for n in range(1, 5)]}, verifier=unsupported)
    findings, artifact, _ = run(repo, client, verifier_prompt=prompt)
    assert findings == [] and artifact["unknown"] == ["H1", "H2", "H3", "H4"]
    assert all(any(m.startswith("downgraded from CONFIRMED") for m in o["missing"]) for o in artifact["outcomes"])


LONG = "".join(f"value_{n} = {n}  # padding so the lookup result is longer than its excerpt\n" for n in range(60))


def test_frozen_verifier_inputs_replay_to_the_identical_verifier_request(repo: Path, tmp_path: Path) -> None:
    (repo / "app" / "long.py").write_text(LONG)
    read = {"tool": "read_source", "args": {"path": "app/long.py"}, "why": "SECRET-INVESTIGATOR-REASON"}
    ctx = context(reference_comments=[{"text": "AACR-REFERENCE-SENTINEL"}])
    live = Recording({"hypotheses": [hypothesis(1), hypothesis(2)]}, investigator=[read, read],
                     verifier=[CONFIRM, {"status": "UNKNOWN", "missing": ["m"]}])
    frozen_path = tmp_path / "x.verifier-input.json"
    _, live_artifact, _ = run(repo, live, ctx=ctx, verifier_input_out=frozen_path)
    text = frozen_path.read_text()
    frozen = json.loads(text)
    assert "SENTINEL" not in text and "SECRET-INVESTIGATOR-REASON" not in text
    full = frozen["inputs"]["H1"]["lookup_results"][0]["result"]
    assert "value_59 = 59" in full and len(full) > 600  # not the 600-character excerpt

    again = Recording({"hypotheses": [hypothesis(9)]}, verifier=[CONFIRM, {"status": "UNKNOWN", "missing": ["m"]}])
    findings, artifact, _ = inv.replay_verifier(frozen, repo_root=repo, llm_client=again)
    assert again.verifier_requests() == live.verifier_requests()
    assert [f.line for f in findings] == [6] and artifact["confirmed"] == ["H1"] and artifact["unknown"] == ["H2"]
    assert [o["lookups"] for o in artifact["outcomes"]] == [o["lookups"] for o in live_artifact["outcomes"]]
    assert artifact["verifier_replay"]["verifier_prompt"] == "control" and "verifier_prompt" not in artifact


def test_verifier_replay_makes_no_finder_investigator_or_lookup_call(repo: Path, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    frozen_path = tmp_path / "x.verifier-input.json"
    run(repo, Scripted({"hypotheses": [hypothesis(1)]}, investigator=[READ], verifier=[CONFIRM]),
        verifier_input_out=frozen_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("no lookup or tool during verifier replay")

    monkeypatch.setattr(inv, "RepoTools", forbidden)
    monkeypatch.setattr(inv.Lookups, "run", forbidden)
    monkeypatch.setattr(inv, "investigate_one", forbidden)
    monkeypatch.setattr(inv, "find", forbidden)
    client = Recording({"hypotheses": [hypothesis(9)]}, investigator=[READ] * 5, verifier=[CONFIRM])
    _, artifact, _ = inv.replay_verifier(json.loads(frozen_path.read_text()), repo_root=repo, llm_client=client,
                                         verifier_prompt="strict")
    assert artifact["usage"]["by_stage"] == {"verifier": 1} and artifact["verifier_prompt"] == "strict"
    assert [s for s, _ in client.requests] == [inv.VERIFIER_SYSTEM_STRICT]


def test_errors_recorded_at_the_freeze_stay_errors_of_the_verifier_replay(repo: Path) -> None:
    frozen = {"version": inv.VERIFIER_INPUT_VERSION, "packet": [], "hypotheses": [hypothesis(1) | {"id": "H1"}],
              "changed_ranges": {}, "lookups": [], "budget": {}, "outcome_lookups": {}, "inputs": {},
              "errors": ["H1: Request timed out."]}
    findings, artifact, _ = inv.replay_verifier(frozen, repo_root=repo, llm_client=Scripted({"hypotheses": []}))
    assert findings == [] and artifact["errors"] == ["H1: Request timed out."] and artifact["unknown"] == ["H1"]
