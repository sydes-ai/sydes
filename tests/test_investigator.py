"""The Investigator (Finder/Verifier code review) accepts nothing on a model's word.

A candidate survives only with a quote the repository contains near the cited line,
anchored on a changed line, and confirmed by a separate verifier that also quotes code.
The default code-review mode stays the single pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMResponse
from sydes.recovery.tools import RepoTools
from sydes.verify import investigator as inv
from sydes.verify.analyzer import VerifyChangeOptions

SOURCE = """\
def stream(writer, source):
    try:
        for chunk in source:
            yield writer.write(chunk)
    finally:
        writer.close()


def wrap(inner):
    async def body():
        async for item in inner:
            yield item
    return body()
"""
DIFF = """\
diff --git a/app/stream.py b/app/stream.py
--- a/app/stream.py
+++ b/app/stream.py
@@ -1,4 +1,6 @@
 def stream(writer, source):
-    for chunk in source:
-        yield writer.write(chunk)
+    try:
+        for chunk in source:
+            yield writer.write(chunk)
+    finally:
+        writer.close()
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "stream.py").write_text(SOURCE)
    return tmp_path


def context() -> dict:
    return {"change": {"files": [{"path": "app/stream.py", "change_type": "modified", "role": "source_route_candidate",
                                  "changed_ranges": [[2, 6]]}],
                       "symbols": [{"file": "app/stream.py", "name": "stream", "start_line": 1, "end_line": 6,
                                    "changed_region": SOURCE[:120]}]},
            "diff": DIFF, "related_code": [], "review_questions": []}


class Scripted:
    """Answers finder and verifier sessions from fixed scripts, by system prompt."""

    def __init__(self, finder: list[dict], verifier: list[dict]) -> None:
        self.queues = {"FINDER": list(finder), "VERIFIER": list(verifier)}
        self.calls = {"FINDER": 0, "VERIFIER": 0}

    def generate(self, request):
        role = "FINDER" if "You are the FINDER" in (request.system or "") else "VERIFIER"
        self.calls[role] += 1
        return LLMResponse(text=json.dumps(self.queues[role].pop(0)), usage={"prompt_tokens": 10, "completion_tokens": 5})


REAL = {"file": "app/stream.py", "line": 6, "quote": "writer.close()"}
INVENTED = {"file": "app/stream.py", "line": 6, "quote": "writer.flush_and_close(force=True)"}


def candidate(title: str, line: int, evidence: list[dict]) -> dict:
    return {"file": "app/stream.py", "line": line, "severity": "P1", "title": title,
            "failure_scenario": "consumer abandons the stream", "evidence": evidence}


def run(repo: Path, client: Scripted):
    return inv.investigate(context=context(), repo_root=repo, llm_client=client, tools=RepoTools(repo))


def test_quote_must_be_in_the_file_near_the_cited_line(repo: Path) -> None:
    assert inv.quote_present(repo, "app/stream.py", 6, "writer.close()")
    assert inv.quote_present(repo, "app/stream.py", 4, "yield   writer.write(chunk)")  # whitespace-normalised
    assert not inv.quote_present(repo, "app/stream.py", 13, "writer.close()")  # far from the cited line
    assert not inv.quote_present(repo, "app/stream.py", 6, "writer.flush_and_close(force=True)")
    assert not inv.quote_present(repo, "app/stream.py", 6, "close")  # too short to be evidence
    assert not inv.quote_present(repo, "../outside.py", 1, "writer.close()")


def test_a_confirmed_candidate_with_a_real_quote_becomes_a_finding(repo: Path) -> None:
    client = Scripted(
        finder=[{"tool": "read_file", "args": {"path": "app/stream.py"}},
                {"final": {"candidates": [candidate("Writer closed only on exhaustion", 6, [REAL])]}}],
        verifier=[{"final": {"verdict": "confirmed", "reason": "finally runs only when closed",
                             "evidence": [{"file": "app/stream.py", "line": 5, "quote": "finally:"}]}}],
    )
    findings, notes = run(repo, client)
    [finding] = findings
    assert (finding.source, finding.file, finding.line, finding.severity) == ("investigator", "app/stream.py", 6, "P1")
    assert all(e.label.startswith("verified_quote") for e in finding.evidence)
    assert any(n.startswith("investigator: units=1 candidates=1") and "confirmed=1" in n for n in notes)


def test_candidates_without_a_real_quote_or_off_the_changed_lines_never_reach_the_verifier(repo: Path) -> None:
    client = Scripted(
        finder=[{"tool": "read_file", "args": {"path": "app/stream.py"}}, {"final": {"candidates": [candidate("Invented evidence", 6, [INVENTED]),
                                          candidate("Unchanged code", 11, [{"file": "app/stream.py", "line": 11,
                                                                            "quote": "async for item in inner:"}])]}}],
        verifier=[],
    )
    findings, notes = run(repo, client)
    assert findings == [] and client.calls["VERIFIER"] == 0
    summary = next(n for n in notes if n.startswith("investigator: "))
    assert "no_verified_quote=1" in summary and "off_changed_lines=1" in summary


@pytest.mark.parametrize("verdict", [
    {"verdict": "refuted", "reason": "caller always exhausts", "evidence": [REAL]},
    {"verdict": "uncertain", "reason": "cannot tell", "evidence": []},
    {"verdict": "confirmed", "reason": "trust me", "evidence": [INVENTED]},  # confirmed without a real quote
])
def test_only_a_quote_backed_confirmation_counts(repo: Path, verdict: dict) -> None:
    client = Scripted(finder=[{"tool": "read_file", "args": {"path": "app/stream.py"}}, {"final": {"candidates": [candidate("Leak", 6, [REAL])]}}],
                      verifier=[{"final": verdict}])
    findings, notes = run(repo, client)
    assert findings == []
    assert any("investigator_verdict(" in n for n in notes)


def test_the_llm_call_budget_is_respected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inv, "MAX_LLM_CALLS", 3)  # below one finder session's worst case
    client = Scripted(finder=[], verifier=[])
    findings, notes = run(repo, client)
    assert findings == [] and client.calls == {"FINDER": 0, "VERIFIER": 0}
    assert "budget_skipped=1" in next(n for n in notes if n.startswith("investigator: "))


def test_units_skip_test_and_deleted_files() -> None:
    ctx = context()
    ctx["change"]["files"] += [{"path": "tests/test_stream.py", "change_type": "modified", "role": "test_usage_candidate"},
                               {"path": "app/old.py", "change_type": "deleted"}]
    ctx["diff"] += "diff --git a/tests/test_stream.py b/tests/test_stream.py\n+x\ndiff --git a/app/old.py b/app/old.py\n-y\n"
    assert [u["file"] for u in inv.build_units(ctx)] == ["app/stream.py"]


def test_the_default_code_review_mode_is_the_single_pass() -> None:
    assert VerifyChangeOptions().code_review_mode == "single"


def test_a_finder_that_concludes_without_investigating_is_asked_once_more(repo: Path) -> None:
    client = Scripted(
        finder=[{"final": {"candidates": []}},  # no tool call at all
                {"tool": "read_file", "args": {"path": "app/stream.py"}},
                {"final": {"candidates": [candidate("Writer closed only on exhaustion", 6, [REAL])]}}],
        verifier=[{"final": {"verdict": "confirmed", "reason": "r",
                             "evidence": [{"file": "app/stream.py", "line": 5, "quote": "finally:"}]}}],
    )
    findings, _ = run(repo, client)
    assert client.calls["FINDER"] == 3 and [f.line for f in findings] == [6]


def test_reasoning_effort_is_set_only_where_none_is_configured() -> None:
    class Provider:
        reasoning_effort = None

    class Traced:
        def __init__(self, inner):
            self._inner = inner

    assert inv.with_reasoning_effort(Traced(Provider()), "high")._inner.reasoning_effort == "high"
    configured = Provider()
    configured.reasoning_effort = "xhigh"
    assert inv.with_reasoning_effort(configured, "high").reasoning_effort == "xhigh"
    assert not hasattr(inv.with_reasoning_effort(object(), "high"), "reasoning_effort")
