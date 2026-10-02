"""Checked behavioral evidence given to the review models (experimental, opt-in)."""

from __future__ import annotations

import json
from pathlib import Path

from sydes.behavioral.review_context import (
    GUIDANCE,
    checked_evidence_block,
    load_genome_summary,
    review_preamble,
)
from sydes.llm.client import LLMRequest, LLMResponse
from sydes.verify import pr_semantic_analysis
from sydes.verify.llm_findings import build_change_context, generate_code_findings
from sydes.verify.models import ChangedFile, ChangeSet


def _summary() -> dict:
    return {
        "format": "diffgenome-genome-summary/1",
        "decision_rules": [
            {"id": "d1", "status": "verified", "entity": "token.Payload.Valid", "file": "token/payload.go",
             "line": 54, "source": "payload.Type != tokenType", "meaning": "issued_type != expected_type",
             "outcome_true": "token.Payload.Valid returned-error", "when_true": "MODEL PROSE EFFECT",
             "agreeing_tests": 11, "at_changed_line": True},
            {"id": "d2", "status": "supported", "entity": "gapi.hasPermission", "file": "gapi/authorization.go",
             "line": 45, "source": "!hasPermission(r)", "meaning": "!has_permission", "agreeing_tests": 5},
            {"id": "d3", "status": "hypothesis", "entity": "x", "file": "x.go", "line": 1,
             "source": "HYPOTHESIS SOURCE", "meaning": "h"},
        ],
        "identities": [
            {"name": "expected_type", "status": "verified", "same_value_at": ["VerifyToken arg:t", "Valid arg:t"]},
            {"name": "role_flow", "status": "supported", "same_value_at": ["A", "B"]},
        ],
        "literals": [{"name": "is_access", "at": "Valid arg:t", "equals": 1, "written_at": "token/payload.go:20"}],
        "transitions": [],
        "consistency": {"scenarios": 20, "consistent": 20, "indeterminate": 0, "contradicted": 0},
        "accounting": {"relevant_executions_checked": 51, "scenario_predictions_checked": 20,
                       "executions_listed_in_artifact": 7},
        "statuses": {"verified": 13, "supported": 60, "hypothesis": 1, "rejected": 2, "contradicted_not_exported": 0},
        "unknowns": [{"what": "dependency-level import ordering", "why": "relation not modelled"}],
    }


def test_block_keeps_statuses_and_excludes_unestablished_content() -> None:
    block = checked_evidence_block(_summary())
    verified, rest = block.split("Supported (", 1)
    supported, unknown = rest.split("Unknown / not established", 1)
    assert "token/payload.go:54" in verified and "[line changed by this diff]" in verified
    assert "matched the prediction in 11 test(s)" in verified
    assert "Valid arg:t" in verified and "token/payload.go:20" in verified
    assert "gapi/authorization.go:45" in supported
    assert "dependency-level import ordering" in unknown and "(NOT facts)" in unknown
    assert "3 further claim(s) were not established" in unknown
    assert "51 executions" in block and "20 of 20" in block
    # never: model prose, hypotheses, unverified identities
    for absent in ("MODEL PROSE EFFECT", "HYPOTHESIS SOURCE", "role_flow"):
        assert absent not in block


def test_nothing_checked_means_nothing_added() -> None:
    assert review_preamble(None) == ""
    assert checked_evidence_block({"format": "diffgenome-genome-summary/1", "decision_rules": []}) == ""


def test_load_summary_is_graceful(tmp_path: Path) -> None:
    assert load_genome_summary(None) is None
    assert load_genome_summary(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_genome_summary(bad) is None
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"format": "diffgenome-change/1", "genome": {"format": "x/9"}}))
    assert load_genome_summary(other) is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"format": "diffgenome-change/1", "genome": _summary()}))
    assert load_genome_summary(good)["accounting"]["relevant_executions_checked"] == 51


class _Stub:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(request.prompt)
        return LLMResponse(text='{"version":"v1","findings":[]}')


def _review_context() -> dict:
    change = ChangeSet(base="main", files=[ChangedFile(repo="api", path="token/payload.go", role="source")])
    return build_change_context(change=change, flows=[], verification=[], diff_text="diff --git a b")


def test_code_review_prompt_unchanged_without_evidence_and_extended_with_it() -> None:
    plain, empty, fed = _Stub(), _Stub(), _Stub()
    generate_code_findings(context=_review_context(), llm_client=plain)
    generate_code_findings(context=_review_context(), llm_client=empty, checked_behavior_preamble="")
    generate_code_findings(
        context=_review_context(), llm_client=fed, checked_behavior_preamble=review_preamble(_summary())
    )
    assert plain.prompts == empty.prompts
    assert GUIDANCE in fed.prompts[0] and "token/payload.go:54" in fed.prompts[0]
    assert "UNKNOWN / NOT ESTABLISHED items are NOT facts" in fed.prompts[0]
    assert GUIDANCE not in plain.prompts[0]


def test_semantic_prompt_unchanged_without_evidence_and_extended_with_it() -> None:
    ctx = {"version": "v1", "files": [], "symbols": [], "diff": "diff --git a b"}
    assert pr_semantic_analysis._bounded_prompt(ctx) == pr_semantic_analysis._bounded_prompt(ctx, "")
    fed = pr_semantic_analysis._bounded_prompt(ctx, review_preamble(_summary()))
    assert "CHECKED BEHAVIORAL EVIDENCE (DiffGenome" in fed
    assert fed.index("CHECKED BEHAVIORAL EVIDENCE") < fed.index("Context:")


def test_evidence_never_costs_the_model_any_diff() -> None:
    """Over budget, the diff is truncated identically with and without the evidence block."""
    from sydes.verify import llm_findings

    big = {"version": "v1", "files": [], "symbols": [], "diff": "x" * 60_000}
    pre = review_preamble(_summary())
    a = pr_semantic_analysis._bounded_prompt(big)
    b = pr_semantic_analysis._bounded_prompt(big, pre)
    assert a.split("\nContext:\n", 1)[1] == b.split("\nContext:\n", 1)[1]
    ca = llm_findings._bounded_prompt("H", big)
    cb = llm_findings._bounded_prompt("H" + pre, big, extra_budget=len(pre))
    assert ca.split("\nContext:\n", 1)[1] == cb.split("\nContext:\n", 1)[1]
