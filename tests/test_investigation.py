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
