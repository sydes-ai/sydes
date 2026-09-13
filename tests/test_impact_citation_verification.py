"""Citation verification on guide-proposed `ImpactCandidate`s
(`citation_check.py`, wired into `ImpactInterpreter._apply_inferred_candidates`).

The one invariant under test throughout: verifying a citation is
record-only. It must never change whether a candidate is accepted,
corroborated, or promoted -- the canonical PROVEN/INFERRED result is
decided exclusively by the pre-existing corroboration pipeline, unchanged.
"""

from __future__ import annotations

from pathlib import Path

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.citation_check import verify_citation
from sydes.impact.guide import parse_guide_decision
from sydes.impact.interpreter import ImpactInterpreter
from sydes.impact.models import (
    ACTION_INFER_IMPACT,
    ACTION_STOP_UNRESOLVED,
    GUIDE_AUTO,
    IMPACT_STATUS_INFERRED,
    CandidateCitation,
    ImpactCandidate,
    InvestigationDecision,
)

REPO = "app"


class ScriptedGuide:
    def __init__(self, decisions: list) -> None:
        self._decisions = list(decisions)

    def investigate(self, question):
        return self._decisions.pop(0)


def facts(**kwargs) -> StructuralFacts:
    return StructuralFacts(
        call_edges=kwargs.get("call_edges", []),
        usage_edges=kwargs.get("usage_edges", []),
        entrypoints=kwargs.get("entrypoints", []),
        symbol_index=kwargs.get("symbol_index", {"repos": []}),
        provides_call_graph=True,
        backend="cbm",
    )


def changed(name: str, *, file: str = "app/svc.py") -> list[dict]:
    return [{"name": name, "file": file, "repo": REPO}]


# --- citation_check.py, standalone -----------------------------------------


def test_verify_citation_confirms_real_quoted_text(tmp_path: Path):
    (tmp_path / "a.py").write_text("def handler():\n    queue.publish('orders.created', payload)\n", encoding="utf-8")
    verified, reason = verify_citation(
        file="a.py", line=2, citation_text="queue.publish('orders.created', payload)", repo_root=tmp_path,
    )
    assert verified is True
    assert "confirmed" in reason


def test_verify_citation_rejects_fabricated_text(tmp_path: Path):
    (tmp_path / "a.py").write_text("def handler():\n    queue.publish('orders.created', payload)\n", encoding="utf-8")
    verified, reason = verify_citation(
        file="a.py", line=2, citation_text="this text was never in the file", repo_root=tmp_path,
    )
    assert verified is False
    assert "not found" in reason


def test_verify_citation_rejects_path_escaping_repo_root(tmp_path: Path):
    outside = tmp_path.parent / "secret.py"
    outside.write_text("PASSWORD = 'hunter2'\n", encoding="utf-8")
    verified, reason = verify_citation(
        file="../secret.py", line=1, citation_text="PASSWORD = 'hunter2'", repo_root=tmp_path,
    )
    assert verified is False
    assert "escapes" in reason


def test_verify_citation_rejects_too_short_text(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    verified, reason = verify_citation(file="a.py", line=1, citation_text="x", repo_root=tmp_path)
    assert verified is False
    assert "too short" in reason


def test_verify_citation_rejects_when_repo_root_missing():
    verified, reason = verify_citation(file="a.py", line=1, citation_text="something real", repo_root=None)
    assert verified is False
    assert "no repo_root" in reason


# ---------------------------------------------------------------------------
# Bounded-window recovery, added diagnosing `sydes-examples/nestjs-
# boilerplate#1`: `pr_semantic_analysis` citations (which call this same
# function) failed with "quoted text not found" even when the quoted text
# was real, because a stale/off-by-one/wrong-line model hint fell outside
# the original fixed ±5-line window. Recovering those must never also make
# genuinely fabricated text pass -- each test below pairs a recoverable
# case with one that must keep failing.
# ---------------------------------------------------------------------------


def test_verify_citation_recovers_real_text_at_a_stale_line_number(tmp_path: Path):
    """The model's claimed line is wrong (e.g. a pre-diff line number), but
    the exact quoted text is real, just elsewhere in the file."""
    lines = ["# padding\n"] * 40
    lines[35] = "export const APP_FALLBACK_LANGUAGE = 'en';\n"
    (tmp_path / "a.py").write_text("".join(lines), encoding="utf-8")
    verified, reason = verify_citation(
        file="a.py", line=5, citation_text="export const APP_FALLBACK_LANGUAGE = 'en';", repo_root=tmp_path,
    )
    assert verified is True
    assert "recovered" in reason
    assert "a.py:36" in reason or "36" in reason


def test_verify_citation_still_rejects_fabricated_text_anywhere_in_a_small_file(tmp_path: Path):
    """A wider search radius must not turn into a weaker check: text that
    was never in the file must still fail, no matter how far the search
    looks for it."""
    lines = ["# padding\n"] * 40
    (tmp_path / "a.py").write_text("".join(lines), encoding="utf-8")
    verified, reason = verify_citation(
        file="a.py", line=5, citation_text="this exact sentence was never written here", repo_root=tmp_path,
    )
    assert verified is False
    assert "not found" in reason


def test_verify_citation_picks_the_occurrence_nearest_the_hint_among_duplicates(tmp_path: Path):
    """The same text appears twice; the recovered citation must anchor to
    the occurrence closest to the model's hint, not simply the first one
    in the file."""
    lines = ["# padding\n"] * 100
    lines[9] = "return helmet();\n"     # line 10 -- far from the hint
    lines[74] = "return helmet();\n"    # line 75 -- outside the tight exact-line window (58±5),
                                          # but the nearest real occurrence once recovery kicks in
    (tmp_path / "a.py").write_text("".join(lines), encoding="utf-8")
    verified, reason = verify_citation(file="a.py", line=58, citation_text="return helmet();", repo_root=tmp_path)
    assert verified is True
    assert "65-75" in reason  # the window containing the near (line 75) occurrence
    assert "a.py:10" not in reason and "5-15" not in reason


def test_verify_citation_out_of_range_line_still_attempts_recovery(tmp_path: Path):
    """A stale line number can point past the end of a file that has since
    shrunk; recovery should still search rather than fail immediately on
    the out-of-range check alone."""
    lines = ["# padding\n"] * 20
    lines[18] = "const real_value = 42;\n"
    (tmp_path / "a.py").write_text("".join(lines), encoding="utf-8")
    verified, reason = verify_citation(
        file="a.py", line=500, citation_text="const real_value = 42;", repo_root=tmp_path,
    )
    assert verified is True


def test_verify_citation_recovers_in_a_long_file_via_targeted_search(tmp_path: Path):
    """A large file must not need an unbounded whole-file rescan to recover
    a citation that is only a modest distance from its (wrong) hint."""
    n = 15_000
    lines = [f"// line {i}\n" for i in range(n)]
    lines[12000] = "export function realHandler() { return true; }\n"
    (tmp_path / "big.ts").write_text("".join(lines), encoding="utf-8")
    verified, reason = verify_citation(
        file="big.ts",
        line=11950,
        citation_text="export function realHandler() { return true; }",
        repo_root=tmp_path,
    )
    assert verified is True
    assert "12001" in reason


# --- guide.py parsing --------------------------------------------------


def test_parse_guide_decision_parses_well_formed_citations():
    text = (
        '{"action": "infer_impact", "candidates": [{"entrypoint": "GET /x", '
        '"confidence": 0.5, "reason": "why", '
        '"citations": [{"file": "a.py", "line": 3, "citation_text": "real quoted line"}]}]}'
    )
    decision = parse_guide_decision(text)
    assert len(decision.candidates) == 1
    citations = decision.candidates[0].citations
    assert len(citations) == 1
    assert citations[0] == CandidateCitation(file="a.py", line=3, citation_text="real quoted line")


def test_parse_guide_decision_drops_malformed_citations_individually():
    text = (
        '{"action": "infer_impact", "candidates": [{"entrypoint": "GET /x", '
        '"confidence": 0.5, "reason": "why", '
        '"citations": [{"file": "a.py", "line": 3, "citation_text": "good one"}, '
        '{"file": "a.py", "citation_text": "missing line"}, '
        '{"line": 4, "citation_text": "missing file"}]}]}'
    )
    decision = parse_guide_decision(text)
    citations = decision.candidates[0].citations
    assert len(citations) == 1
    assert citations[0].citation_text == "good one"


def test_parse_guide_decision_candidate_with_no_citations_is_unaffected():
    text = '{"action": "infer_impact", "candidates": [{"entrypoint": "GET /x", "confidence": 0.5, "reason": "why"}]}'
    decision = parse_guide_decision(text)
    assert decision.candidates[0].citations == ()


# --- end-to-end through ImpactInterpreter -------------------------------


def test_citation_verification_does_not_change_acceptance(tmp_path: Path):
    """The core invariant: a candidate with a VERIFIED citation and a
    candidate with a FABRICATED citation must be accepted/rejected
    identically -- citation results are recorded, never load-bearing."""
    (tmp_path / "a.py").write_text("def leaf():\n    return real_downstream_call()\n", encoding="utf-8")

    f = facts()
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="some background job reading leaf's output",
                    confidence=0.4, reason="plausible shared dependency",
                    inference_type="semantic_indirect_dependency",
                    based_on_changed_symbols=("leaf",),
                    citations=(CandidateCitation(file="a.py", line=2, citation_text="real_downstream_call()"),),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, repo_root=tmp_path)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is True
    assert log_entry["citation_provided"] is True
    assert log_entry["citations_total"] == 1
    assert log_entry["citations_verified"] == 1


def test_fabricated_citation_still_gets_accepted_but_logged_as_unverified(tmp_path: Path):
    (tmp_path / "a.py").write_text("def leaf():\n    return real_downstream_call()\n", encoding="utf-8")

    f = facts()
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="some background job reading leaf's output",
                    confidence=0.4, reason="plausible shared dependency",
                    inference_type="semantic_indirect_dependency",
                    based_on_changed_symbols=("leaf",),
                    citations=(CandidateCitation(file="a.py", line=2, citation_text="this was never here"),),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, repo_root=tmp_path)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    # Same acceptance outcome as the verified-citation case above --
    # citation correctness never gates acceptance.
    assert len(result.affected) == 1
    assert result.affected[0].status == IMPACT_STATUS_INFERRED
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is True
    assert log_entry["citation_provided"] is True
    assert log_entry["citations_total"] == 1
    assert log_entry["citations_verified"] == 0


def test_candidate_with_no_citations_logs_citation_provided_false(tmp_path: Path):
    f = facts()
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="some background job reading leaf's output",
                    confidence=0.4, reason="plausible shared dependency",
                    inference_type="semantic_indirect_dependency",
                    based_on_changed_symbols=("leaf",),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, repo_root=tmp_path)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)

    log_entry = result.llm_candidate_log[0]
    assert log_entry["citation_provided"] is False
    assert log_entry["citations_total"] == 0
    assert log_entry["accepted"] is True


def test_rejected_candidate_still_carries_citation_fields(tmp_path: Path):
    """A candidate rejected for an unrelated reason (missing reason) must
    still carry citation fields in its log entry -- every log entry has the
    same schema regardless of which branch produced it."""
    (tmp_path / "a.py").write_text("x = real_thing\n", encoding="utf-8")
    f = facts()
    guide = ScriptedGuide([
        InvestigationDecision(
            action=ACTION_INFER_IMPACT,
            candidates=(
                ImpactCandidate(
                    entrypoint_label="GET /x", confidence=0.5, reason="",  # missing reason -> rejected
                    citations=(CandidateCitation(file="a.py", line=1, citation_text="real_thing"),),
                ),
            ),
        ),
        InvestigationDecision(action=ACTION_STOP_UNRESOLVED),
    ])
    interpreter = ImpactInterpreter(guide=guide, guide_policy=GUIDE_AUTO, repo_root=tmp_path)
    result = interpreter.interpret(changed("leaf"), f, repo=REPO)
    log_entry = result.llm_candidate_log[0]
    assert log_entry["accepted"] is False
    assert log_entry["rejection_reason"].startswith("missing_reason")
    assert log_entry["citation_provided"] is True
    assert log_entry["citations_verified"] == 1
