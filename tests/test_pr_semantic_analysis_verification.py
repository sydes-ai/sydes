"""Citation verification and structured `verification_state` for the
PR-level semantic pass (`pr_semantic_analysis.py`).

Motivated by a real, empirically-observed failure: a live semantic-analysis
run on an unrelated CMS diff fabricated a completely fictitious "order
expiration can now vary by sales channel" narrative at 0.9 confidence, with
nothing downstream to catch it before it reached a terminal report as a
confident "Likely behavioral changes" bullet. The fix has two parts, both
covered here: (1) every `behavior_changes[]` claim may carry literal
citations, re-checked against the real repository before counting for
anything; (2) a deterministic, model-confidence-independent
`verification_state` rolls that up, with a distinct `indeterminate` axis
the model can flag (via a fixed, machine-readable reason) when the real
answer depends on something the repository itself cannot say.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.models import (
    INDETERMINATE_DEPLOYMENT_CONFIG_REQUIRED,
    SEMANTIC_VERIFICATION_INDETERMINATE,
    SEMANTIC_VERIFICATION_PARTIALLY_VERIFIED,
    SEMANTIC_VERIFICATION_UNVERIFIED,
    SEMANTIC_VERIFICATION_VERIFIED,
    ChangeSemanticAnalysis,
    ChangeSet,
    ChangeVerificationResult,
    SemanticBehaviorChange,
    SemanticCitation,
)
from sydes.verify.pr_semantic_analysis import (
    _apply_verification,
    _parse_indeterminate,
    parse_semantic_analysis,
)


def _write(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# 1. Citation parsing
# --------------------------------------------------------------------------

def test_behavior_change_citations_parse_into_semantic_citation_objects() -> None:
    raw = {
        "behavior_changes": [
            {
                "description": "real change",
                "citations": [{"file": "a.py", "line": 3, "quoted_text": "real_downstream_call()"}],
            },
        ],
    }
    analysis = parse_semantic_analysis(raw)
    citations = analysis.behavior_changes[0].citations
    assert citations == [SemanticCitation(file="a.py", line=3, quoted_text="real_downstream_call()")]
    # Unverified until `_apply_verification` runs against a real repo.
    assert analysis.behavior_changes[0].citations_verified == 0


def test_malformed_citation_entries_are_dropped_individually() -> None:
    raw = {
        "behavior_changes": [
            {
                "description": "real change",
                "citations": [
                    {"file": "a.py", "line": 3, "quoted_text": "a real quoted line"},
                    {"file": "a.py", "quoted_text": "missing line number"},
                    {"line": 4, "quoted_text": "missing file"},
                    {"file": "a.py", "line": 5, "quoted_text": "x"},  # too short
                ],
            },
        ],
    }
    analysis = parse_semantic_analysis(raw)
    citations = analysis.behavior_changes[0].citations
    assert len(citations) == 1
    assert citations[0].quoted_text == "a real quoted line"


def test_behavior_change_with_no_citations_field_is_unaffected() -> None:
    raw = {"behavior_changes": [{"description": "real change"}]}
    analysis = parse_semantic_analysis(raw)
    assert analysis.behavior_changes[0].citations == []


# --------------------------------------------------------------------------
# 2. Indeterminate parsing
# --------------------------------------------------------------------------

def test_parse_indeterminate_accepts_a_valid_reason() -> None:
    is_indeterminate, reason, detail = _parse_indeterminate(
        {"is_indeterminate": True, "reason": "deployment_config_required", "detail": "depends on config"}
    )
    assert is_indeterminate is True
    assert reason == INDETERMINATE_DEPLOYMENT_CONFIG_REQUIRED
    assert detail == "depends on config"


def test_parse_indeterminate_false_ignores_reason_and_detail() -> None:
    is_indeterminate, reason, detail = _parse_indeterminate(
        {"is_indeterminate": False, "reason": "deployment_config_required", "detail": "irrelevant"}
    )
    assert is_indeterminate is False
    assert reason is None
    assert detail == ""


def test_parse_indeterminate_defaults_an_invalid_reason_rather_than_dropping_the_flag() -> None:
    is_indeterminate, reason, detail = _parse_indeterminate(
        {"is_indeterminate": True, "reason": "made_up_reason"}
    )
    assert is_indeterminate is True
    assert reason == "insufficient_repository_evidence"


def test_parse_indeterminate_absent_object_is_not_indeterminate() -> None:
    assert _parse_indeterminate(None) == (False, None, "")
    assert _parse_indeterminate("not a dict") == (False, None, "")


def test_parse_semantic_analysis_sets_verification_state_from_indeterminate_flag() -> None:
    raw = {
        "change_summary": "x",
        "indeterminate": {"is_indeterminate": True, "reason": "runtime_only_behavior"},
    }
    analysis = parse_semantic_analysis(raw)
    assert analysis.verification_state == SEMANTIC_VERIFICATION_INDETERMINATE
    assert analysis.indeterminate_reason == "runtime_only_behavior"


def test_parse_semantic_analysis_defaults_to_unverified_without_indeterminate() -> None:
    analysis = parse_semantic_analysis({"change_summary": "x"})
    assert analysis.verification_state == SEMANTIC_VERIFICATION_UNVERIFIED
    assert analysis.indeterminate_reason is None


# --------------------------------------------------------------------------
# 3. End-to-end citation verification against a real repo
# --------------------------------------------------------------------------

def test_apply_verification_marks_a_real_verifiable_citation_as_verified(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "def handler():\n    return real_downstream_call()\n")
    analysis = ChangeSemanticAnalysis(
        behavior_changes=[
            SemanticBehaviorChange(
                description="real change",
                citations=[SemanticCitation(file="a.py", line=2, quoted_text="return real_downstream_call()")],
            ),
        ],
    )
    verified = _apply_verification(analysis, repo_root=tmp_path)
    assert verified.verification_state == SEMANTIC_VERIFICATION_VERIFIED
    assert verified.behavior_changes[0].citations_verified == 1
    assert len(verified.behavior_changes[0].citation_notes) == 1


def test_apply_verification_marks_a_fabricated_citation_as_unverified(tmp_path: Path) -> None:
    """The regression case: a citation that names a real file but quotes
    text that was never there -- exactly the shape of the H3 hallucination
    (the description talked about something the diff never showed)."""
    _write(tmp_path, "a.py", "def handler():\n    return real_downstream_call()\n")
    analysis = ChangeSemanticAnalysis(
        behavior_changes=[
            SemanticBehaviorChange(
                description="order expiration can now vary by sales channel",
                citations=[SemanticCitation(file="a.py", line=2, quoted_text="this text was never here")],
            ),
        ],
    )
    verified = _apply_verification(analysis, repo_root=tmp_path)
    assert verified.verification_state == SEMANTIC_VERIFICATION_UNVERIFIED
    assert verified.behavior_changes[0].citations_verified == 0


def test_apply_verification_with_no_citations_anywhere_is_unverified(tmp_path: Path) -> None:
    analysis = ChangeSemanticAnalysis(
        behavior_changes=[SemanticBehaviorChange(description="a claim with no citations at all")],
    )
    verified = _apply_verification(analysis, repo_root=tmp_path)
    assert verified.verification_state == SEMANTIC_VERIFICATION_UNVERIFIED


def test_apply_verification_mixed_outcomes_is_partially_verified(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "real_line_here = 1\n")
    analysis = ChangeSemanticAnalysis(
        behavior_changes=[
            SemanticBehaviorChange(
                description="one real claim",
                citations=[SemanticCitation(file="a.py", line=1, quoted_text="real_line_here = 1")],
            ),
            SemanticBehaviorChange(
                description="one fabricated claim",
                citations=[SemanticCitation(file="a.py", line=1, quoted_text="fabricated nonsense")],
            ),
        ],
    )
    verified = _apply_verification(analysis, repo_root=tmp_path)
    assert verified.verification_state == SEMANTIC_VERIFICATION_PARTIALLY_VERIFIED


def test_apply_verification_never_overrides_an_indeterminate_state(tmp_path: Path) -> None:
    """Citations are still verified and recorded for transparency, but a
    model-flagged indeterminate analysis stays indeterminate regardless of
    how its citations check out -- verification and indeterminacy are
    different questions (see `_apply_verification`'s docstring)."""
    _write(tmp_path, "a.py", "real_line_here = 1\n")
    analysis = ChangeSemanticAnalysis(
        verification_state=SEMANTIC_VERIFICATION_INDETERMINATE,
        indeterminate_reason=INDETERMINATE_DEPLOYMENT_CONFIG_REQUIRED,
        behavior_changes=[
            SemanticBehaviorChange(
                description="a well-cited mechanism trace",
                citations=[SemanticCitation(file="a.py", line=1, quoted_text="real_line_here = 1")],
            ),
        ],
    )
    verified = _apply_verification(analysis, repo_root=tmp_path)
    assert verified.verification_state == SEMANTIC_VERIFICATION_INDETERMINATE
    assert verified.indeterminate_reason == INDETERMINATE_DEPLOYMENT_CONFIG_REQUIRED
    # Still recorded, for transparency, even though it didn't change the state.
    assert verified.behavior_changes[0].citations_verified == 1


def test_apply_verification_with_no_behavior_changes_is_unverified(tmp_path: Path) -> None:
    analysis = ChangeSemanticAnalysis(change_summary="nothing to say")
    verified = _apply_verification(analysis, repo_root=tmp_path)
    assert verified.verification_state == SEMANTIC_VERIFICATION_UNVERIFIED


# --------------------------------------------------------------------------
# 4. Full generate_pr_semantic_analysis wiring
# --------------------------------------------------------------------------

def test_generate_pr_semantic_analysis_applies_verification_end_to_end(tmp_path: Path) -> None:
    from sydes.core.models import RepoRef
    from sydes.discover.file_facts import build_structural_index
    from sydes.llm.client import LLMRequest, LLMResponse
    from sydes.verify.analyzer import attribute_changed_symbols
    from sydes.verify.git_change import resolve_change_set
    from sydes.verify.pr_semantic_analysis import generate_pr_semantic_analysis
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _write(repo, "orders.py", "def handler():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    _write(repo, "orders.py", "def handler():\n    return real_downstream_call()\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=repo, check=True)

    change = resolve_change_set(repo_name="api", repo_root=repo, base="HEAD~1")
    index = build_structural_index([RepoRef(name="api", root=str(repo))], root=tmp_path / "store", persist=False)
    change.symbols = attribute_changed_symbols(change, index.handler_symbol_batch)

    response_text = json.dumps({
        "change_summary": "handler now delegates to a real downstream call",
        "behavior_changes": [
            {
                "description": "handler behavior changed",
                "citations": [{"file": "orders.py", "line": 2, "quoted_text": "return real_downstream_call()"}],
                "confidence": 0.8,
            },
        ],
    })

    class _Stub:
        def generate(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(text=response_text)

    analysis, _notes = generate_pr_semantic_analysis(change=change, repo_root=repo, llm_client=_Stub())

    assert analysis is not None
    assert analysis.verification_state == SEMANTIC_VERIFICATION_VERIFIED
    assert analysis.behavior_changes[0].citations_verified == 1


# --------------------------------------------------------------------------
# 5. Terminal rendering — the surface the H3 hallucination actually reached
# --------------------------------------------------------------------------

def _result_with(analysis: ChangeSemanticAnalysis) -> ChangeVerificationResult:
    return ChangeVerificationResult(change=ChangeSet(base="main"), pr_semantic_analysis=analysis)


def test_default_report_labels_an_unverified_analysis_in_the_section_header() -> None:
    analysis = ChangeSemanticAnalysis(
        change_summary="a claim with no evidence",
        behavior_changes=[SemanticBehaviorChange(description="uncited claim")],
    )
    report = render_verify_change_terminal(_result_with(analysis))
    assert "CHANGE ANALYSIS (unverified)" in report
    assert "unverified hypotheses" in report


def test_default_report_labels_a_verified_analysis_without_the_unverified_caveat() -> None:
    analysis = ChangeSemanticAnalysis(
        change_summary="a well-cited claim",
        behavior_changes=[SemanticBehaviorChange(description="cited claim", citations_verified=1,
                                                   citations=[SemanticCitation(file="a.py", line=1, quoted_text="x" * 10)])],
        verification_state=SEMANTIC_VERIFICATION_VERIFIED,
    )
    report = render_verify_change_terminal(_result_with(analysis))
    assert "CHANGE ANALYSIS (citations verified)" in report
    assert "unverified hypotheses" not in report


def test_default_report_promotes_indeterminate_to_the_top_of_the_section() -> None:
    """The exact finding this action set out to fix: H3's schema/deployment
    -dependence must be promoted to a top-level, unmissable signal rather
    than buried under an unqualified 'Likely behavioral changes' list."""
    analysis = ChangeSemanticAnalysis(
        change_summary="media fields now populate for non-localized i18n prefill",
        behavior_changes=[SemanticBehaviorChange(description="i18n prefill now includes media fields")],
        verification_state=SEMANTIC_VERIFICATION_INDETERMINATE,
        indeterminate_reason=INDETERMINATE_DEPLOYMENT_CONFIG_REQUIRED,
        indeterminate_detail="depends on which content types have non-localized media fields configured",
    )
    report = render_verify_change_terminal(_result_with(analysis))
    assert "CHANGE ANALYSIS (indeterminate)" in report
    assert "Cannot be established from this repository alone" in report
    assert "deployment" in report.lower()
    assert "depends on which content types" in report
    # The indeterminate notice appears before the speculative bullets, not after.
    assert report.index("Cannot be established") < report.index("i18n prefill now includes media fields")


def test_verbose_report_shows_citation_verification_detail(tmp_path: Path) -> None:
    analysis = ChangeSemanticAnalysis(
        behavior_changes=[
            SemanticBehaviorChange(
                description="a claim",
                citations=[SemanticCitation(file="a.py", line=1, quoted_text="fabricated text")],
                citations_verified=0,
                citation_notes=["quoted text not found near a.py:1 (checked lines 1-6)"],
            ),
        ],
        verification_state=SEMANTIC_VERIFICATION_UNVERIFIED,
    )
    report = render_verify_change_terminal(_result_with(analysis), verbose=True)
    assert "Verification: unverified" in report
    assert "citations: 0/1 verified" in report
    assert "a.py:1" in report
    assert "quoted text not found" in report
