"""Code review is an independent branch: it sees the changed code and
nothing Sydes concluded about the system, and nothing it concludes reaches
the system-impact pipeline.

The two directions matter for different reasons. Inbound, a system
conclusion in the code-review packet could bias a defect judgement or
launder an inferred impact into something that reads like a bug. Outbound,
a code finding reaching impact/grounding/verification would make an advisory
LLM opinion load-bearing for the verdict. Both are enforced structurally —
`build_code_review_context` cannot accept a flow, and nothing downstream
reads `result.code_findings` — and pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.verify.analyzer import VerifyChangeOptions, analyze_change
from sydes.verify.llm_findings import (
    MAX_FINDINGS,
    _CODE_FINDINGS_HEADER,
    build_code_review_context,
    generate_code_findings,
)
from sydes.verify.models import ChangedFile, ChangedSymbol, ChangeSet, Hunk

REPO = "svc"


# --- helpers ---------------------------------------------------------------


class _StubLLM:
    """Returns one canned response and records every prompt it was given."""

    def __init__(self, payload: dict | str) -> None:
        self._text = payload if isinstance(payload, str) else json.dumps(payload)
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.prompts.append(request.prompt)
        return LLMResponse(text=self._text)


class _FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise LLMClientError("provider exploded")


def _change(*, path: str = "svc.py", hunks: list[tuple[int, int]], symbol_lines: tuple[int, int]) -> ChangeSet:
    return ChangeSet(
        base="main", head="head",
        files=[ChangedFile(
            repo=REPO, path=path, change_type="modified", role="source_route_candidate",
            added_lines=2, removed_lines=1,
            hunks=[Hunk(start_line=low, end_line=high) for low, high in hunks],
        )],
        symbols=[ChangedSymbol(
            id=f"{REPO}:{path}:handle", repo=REPO, file=path, name="handle",
            qualified_name="handle", kind="function", language="python",
            start_line=symbol_lines[0], end_line=symbol_lines[1], changed_lines=2,
        )],
    )


def _write_large_function(root: Path, path: str = "svc.py") -> None:
    """Function at line 10; the only change is at line 40."""
    lines = ["# header"] * 9
    lines.append("def handle(request):")
    for n in range(11, 40):
        lines.append(f"    step_{n} = compute_{n}(request)")
    lines.append("    result = apply_policy(request, DENY_ALL)")   # line 40
    for n in range(41, 61):
        lines.append(f"    trailing_{n} = finalize_{n}(result)")
    lines.append("    return result")                               # line 61
    (root / path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _finding(**overrides) -> dict:
    base = {
        "severity": "P1", "title": "policy inverted", "file": "svc.py", "line": 40,
        "explanation": "the deny branch is taken for allowed requests",
        "impact": "an authorized request receives 403",
        "evidence_snippet": "result = apply_policy(request, DENY_ALL)",
    }
    base.update(overrides)
    return base


# --- 3/5/6. Context contains the change, centred on changed regions --------


def test_context_contains_diff_files_symbols_and_changed_regions(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    context = build_code_review_context(
        change=_change(hunks=[(40, 40)], symbol_lines=(10, 61)),
        diff_text="diff --git a/svc.py b/svc.py\n+    result = apply_policy(request, DENY_ALL)",
        repo_root=tmp_path,
    )

    assert context["diff"]
    assert context["change"]["files"][0]["path"] == "svc.py"
    assert context["change"]["files"][0]["changed_ranges"] == [[40, 40]]
    symbol = context["change"]["symbols"][0]
    assert symbol["name"] == "handle"
    assert symbol["changed_line_ranges"] == [[40, 40]]
    assert "changed_region" in symbol


def test_changed_region_shows_the_change_not_the_symbol_head(tmp_path: Path) -> None:
    """Test 5: the large-symbol case. The region must contain line 40, not
    merely the opening of a function that starts at line 10."""
    _write_large_function(tmp_path)
    context = build_code_review_context(
        change=_change(hunks=[(40, 40)], symbol_lines=(10, 61)),
        diff_text="d", repo_root=tmp_path,
    )
    region = context["change"]["symbols"][0]["changed_region"]

    assert "apply_policy" in region
    assert "DENY_ALL" in region
    assert "step_11" not in region


def test_changed_decorator_metadata_appears_in_the_region(tmp_path: Path) -> None:
    """Test 6: attached declaration metadata is what attribution used to mark
    the symbol changed, so it must be inside the region."""
    (tmp_path / "views.py").write_text(
        "@register_model_view(DataFile)\n"                    # line 1
        "@method_decorator(never_cache, name='dispatch')\n"   # line 2  <- the change
        "class DataFileView(generic.ObjectView):\n"           # line 3
        "    queryset = DataFile.objects.all()\n",            # line 4
        encoding="utf-8",
    )
    change = ChangeSet(
        base="main", files=[ChangedFile(
            repo=REPO, path="views.py", change_type="modified",
            hunks=[Hunk(start_line=2, end_line=2)],
        )],
        symbols=[ChangedSymbol(
            id="s", repo=REPO, file="views.py", name="DataFileView", kind="class",
            language="python", start_line=3, end_line=4,
        )],
    )
    context = build_code_review_context(change=change, diff_text="d", repo_root=tmp_path)

    region = context["change"]["symbols"][0]["changed_region"]
    assert "method_decorator" in region
    assert "never_cache" in region


# --- 4. Context must NOT contain any system conclusion ---------------------


def test_context_excludes_every_system_analysis_conclusion(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    context = build_code_review_context(
        change=_change(hunks=[(40, 40)], symbol_lines=(10, 61)),
        diff_text="d", repo_root=tmp_path,
    )

    for forbidden in (
        "affected_flows", "existing_verification", "accepted_impacts", "impacts",
        "affected_boundaries", "boundaries", "pr_semantic_analysis", "semantic_analysis",
        "verification_gaps", "obligations", "verdict", "risk",
    ):
        assert forbidden not in context, f"{forbidden} must not be in the code-review context"

    # Nothing nested carries them either — check the serialized payload.
    serialized = json.dumps(context)
    for forbidden in ("affected_flows", "existing_verification", "verification_gap", "obligation"):
        assert forbidden not in serialized


def test_context_builder_cannot_accept_system_conclusions() -> None:
    """Independence is enforced by the signature, not by discipline."""
    with pytest.raises(TypeError):
        build_code_review_context(  # type: ignore[call-arg]
            change=_change(hunks=[(1, 1)], symbol_lines=(1, 2)),
            diff_text="d", flows=[], verification=[],
        )


# --- 7/8/9. Finding validation --------------------------------------------


def _context(tmp_path: Path) -> dict:
    _write_large_function(tmp_path)
    return build_code_review_context(
        change=_change(hunks=[(40, 40)], symbol_lines=(10, 61)),
        diff_text="diff --git a/svc.py b/svc.py\n+    result = apply_policy(request, DENY_ALL)",
        repo_root=tmp_path,
    )


def test_finding_referencing_unknown_file_is_rejected(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": [_finding(file="not_in_change.py")]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert findings == []
    assert any("unknown file" in w for w in warnings)


def test_finding_on_an_unchanged_line_elsewhere_in_the_file_is_rejected(tmp_path: Path) -> None:
    """Test 8: code review's question is about the patch. A defect claimed at
    line 12 — real code, but untouched by this change — is out of scope."""
    stub = _StubLLM({"version": "v1", "findings": [_finding(line=12, evidence_snippet=None)]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert findings == []
    assert any("outside every changed range" in w for w in warnings)


def test_finding_on_a_valid_changed_line_is_accepted(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": [_finding()]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == 1
    assert findings[0].file == "svc.py"
    assert findings[0].line == 40
    assert findings[0].severity == "P1"
    assert findings[0].impact == "an authorized request receives 403"
    assert not any("Rejected" in w for w in warnings)


def test_finding_without_a_line_in_a_changed_file_is_rejected(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": [_finding(line=None)]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert findings == []
    assert any("no line in a changed file" in w for w in warnings)


# --- 10. Evidence snippet --------------------------------------------------


def test_fabricated_evidence_snippet_is_dropped_but_the_finding_survives(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": [
        _finding(evidence_snippet="if user.is_admin: bypass_all_checks()  # never in the diff"),
    ]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == 1, "a grounded finding must not die for a bad optional snippet"
    assert findings[0].evidence[0].snippet is None
    assert any("unverifiable evidence snippet" in w for w in warnings)


def test_verbatim_snippet_is_kept_even_with_different_indentation(tmp_path: Path) -> None:
    """Normalization removes formatting variance only — never a near-match."""
    stub = _StubLLM({"version": "v1", "findings": [
        _finding(evidence_snippet="result   =   apply_policy(request,  DENY_ALL)"),
    ]})
    findings, _ = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == 1
    assert findings[0].evidence[0].snippet is not None


# --- 11/12. Severity and the finding cap ----------------------------------


def test_invalid_severity_is_normalized_conservatively_with_a_warning(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": [_finding(severity="CRITICAL")]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == 1
    assert findings[0].severity == "P3", "an unnamed severity must not be promoted"
    assert any("invalid severity" in w for w in warnings)


def test_max_findings_is_applied_after_severity_sorting(tmp_path: Path) -> None:
    """Test 12: a P0 arriving last must survive; an earlier P3 must not
    displace it."""
    raw = [_finding(severity="P3", title=f"low {n}") for n in range(MAX_FINDINGS)]
    raw.append(_finding(severity="P0", title="critical arriving last"))
    stub = _StubLLM({"version": "v1", "findings": raw})

    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == MAX_FINDINGS
    assert findings[0].severity == "P0"
    assert findings[0].title == "critical arriving last"
    assert any("Truncated" in w for w in warnings)


# --- 13. Silence -----------------------------------------------------------


def test_empty_findings_produce_no_noise(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": []})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert findings == []
    assert not any("Rejected" in w or "Dropped" in w for w in warnings)


def test_prompt_never_mentions_affected_system_flows(tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": []})
    generate_code_findings(context=_context(tmp_path), llm_client=stub)

    prompt = stub.prompts[0]
    assert "affected system flows" not in prompt
    assert "affected_flows" not in prompt
    assert "does this patch introduce a defect" in prompt.lower()


# --- 1/2/14/15. End-to-end branch behavior --------------------------------


_SERVICE_V1 = "def helper():\n    return 1\n"
_SERVICE_V2 = "def helper():\n    return 2\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "service.py").write_text(_SERVICE_V1, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    (root / "service.py").write_text(_SERVICE_V2, encoding="utf-8")
    return root


def _run(repo: Path, tmp_path: Path, *, code_review: bool, client=None):
    from sydes.core.models import RepoRef

    return analyze_change(
        repos=[RepoRef(name=REPO, root=str(repo))],
        options=VerifyChangeOptions(
            base="main", llm_policy="never", run_tests=False,
            code_review=code_review, llm_client=client,
        ),
    )


def test_code_review_disabled_makes_zero_calls(repo: Path, tmp_path: Path) -> None:
    stub = _StubLLM({"version": "v1", "findings": []})
    result = _run(repo, tmp_path, code_review=False, client=stub)

    assert stub.calls == 0, "code review must not run unless explicitly enabled"
    assert result.code_findings == []


def test_code_review_enabled_invokes_the_stage_and_populates_findings(
    repo: Path, tmp_path: Path,
) -> None:
    stub = _StubLLM({"version": "v1", "findings": [{
        "severity": "P1", "title": "return value changed",
        "file": "service.py", "line": 2,
        "explanation": "helper now returns 2", "impact": "callers expecting 1 misbehave",
    }]})
    result = _run(repo, tmp_path, code_review=True, client=stub)

    assert stub.calls == 1
    assert len(result.code_findings) == 1
    assert result.code_findings[0].title == "return value changed"


def test_code_findings_do_not_alter_any_system_analysis_output(
    repo: Path, tmp_path: Path,
) -> None:
    """Test 14: identical fixture, code_review off vs on. Every system-impact
    output must be byte-identical; only `code_findings` may differ."""
    off = _run(repo, tmp_path / "a", code_review=False)
    stub = _StubLLM({"version": "v1", "findings": [{
        "severity": "P0", "title": "critical defect", "file": "service.py", "line": 2,
        "explanation": "x", "impact": "y",
    }]})
    on = _run(repo, tmp_path / "b", code_review=True, client=stub)

    assert len(on.code_findings) == 1, "the finding must actually have been produced"
    assert off.code_findings == []

    assert [i.id for i in on.accepted_impacts] == [i.id for i in off.accepted_impacts]
    assert [b.id for b in on.affected_boundaries] == [b.id for b in off.affected_boundaries]
    assert [f.id for f in on.affected_flows] == [f.id for f in off.affected_flows]
    assert on.summary.verdict == off.summary.verdict
    assert on.summary.risk == off.summary.risk
    assert on.summary.counts.obligations == off.summary.counts.obligations
    assert on.summary.counts.obligations_failed == off.summary.counts.obligations_failed
    assert [g.behavior for g in on.verification_gaps] == [g.behavior for g in off.verification_gaps]


def test_code_review_provider_failure_does_not_fail_the_run(repo: Path, tmp_path: Path) -> None:
    """Test 15: advisory means advisory — a provider failure degrades to
    zero findings and a diagnostic, never a failed verification."""
    failing = _FailingLLM()
    baseline = _run(repo, tmp_path / "a", code_review=False)
    result = _run(repo, tmp_path / "b", code_review=True, client=failing)

    assert failing.calls == 1
    assert result.code_findings == []
    assert any("code_review_unavailable" in d for d in result.diagnostics)
    # The verdict and impacts are exactly what they were without code review.
    assert result.summary.verdict == baseline.summary.verdict
    assert [i.id for i in result.accepted_impacts] == [i.id for i in baseline.accepted_impacts]


# --- Calibration: a defect must be demonstrated, not merely plausible -------
#
# Root cause this pins (ory/kratos#4277, manually tested): the code-review
# branch emitted "[P1] Incorrect JSON patch path restriction allows
# unauthorized credential password update" for a diff that narrows a JSON
# Patch deny-list from `/credentials/**` to `/credentials/oidc/**`, with its
# own accompanying test intentionally flipped from expecting failure to
# expecting success. Nothing in the supplied evidence showed that
# authorization was actually absent elsewhere — the system-analysis branch
# correctly called this a risk/uncertainty; code review promoted it to a
# concrete defect it could not actually demonstrate.
#
# The fix is prompt-level calibration, not new validation: `_validate_findings`
# is deterministic (file/line/snippet/severity) and has no way to judge
# whether a *security* claim is actually supported — that judgement has to
# happen before the model writes the finding. These tests therefore pin two
# things: (a) the prompt itself carries the calibration language for exactly
# the scenarios that were miscalibrated, and (b) the deterministic pipeline
# still passes through a well-formed, *actually* grounded finding of every
# type this task requires to remain reportable — proving the fix narrows
# what the model is told to claim, not what the pipeline is able to accept.


# 1. Security-sensitive change with no demonstrated missing authorization.
def test_prompt_forbids_inferring_missing_authorization_from_sensitivity_alone() -> None:
    header = _CODE_FINDINGS_HEADER
    assert "do not infer" in header.lower()
    assert "merely because" in header.lower()
    assert "security-sensitive" in header.lower()
    # The exact unsupported phrasing the Kratos finding used must be named
    # as invalid, not just described abstractly.
    assert "may allow unauthorized access" in header
    assert "should probably have validation" in header
    assert "not automatically a vulnerability" in header.lower() or "not a defect on its own" in header.lower()


def test_kratos_pattern_context_carries_the_calibration_guidance(tmp_path: Path) -> None:
    """Reconstructs the actual shape that produced the false finding: a JSON
    Patch deny-list narrowed from a broad path to a specific subpath, with
    the accompanying test flipped from expecting failure to expecting
    success — and no evidence anywhere showing an authorization control was
    removed. The prompt actually built for this exact context must carry the
    calibration language, not just the header constant in isolation."""
    (tmp_path / "handler.go").write_text(
        "func (h *Handler) patch(w http.ResponseWriter, r *http.Request) {\n"
        "\tpatchedIdentity := WithAdminMetadataInJSON(*identity)\n"
        '\tif err := jsonx.ApplyJSONPatch(requestBody, &patchedIdentity, "/id", '
        '"/credentials/oidc/**"); err != nil {\n'
        "\t\treturn\n"
        "\t}\n"
        "}\n",
        encoding="utf-8",
    )
    change = ChangeSet(
        base="main", files=[ChangedFile(
            repo=REPO, path="handler.go", change_type="modified",
            hunks=[Hunk(start_line=3, end_line=3)],
        )],
        symbols=[ChangedSymbol(
            id="s", repo=REPO, file="handler.go", name="patch", kind="function",
            language="go", start_line=1, end_line=6,
        )],
    )
    context = build_code_review_context(
        change=change,
        diff_text=(
            "diff --git a/handler.go b/handler.go\n"
            '-\tif err := jsonx.ApplyJSONPatch(requestBody, &patchedIdentity, "/id", "/credentials/**")\n'
            '+\tif err := jsonx.ApplyJSONPatch(requestBody, &patchedIdentity, "/id", "/credentials/oidc/**")\n'
        ),
        repo_root=tmp_path,
    )
    stub = _StubLLM({"version": "v1", "findings": []})
    findings, warnings = generate_code_findings(context=context, llm_client=stub)

    assert findings == []
    assert not any("Rejected" in w for w in warnings)
    prompt = stub.prompts[0]
    assert "do not infer" in prompt.lower()
    assert "security-sensitive" in prompt.lower()
    assert "/credentials/oidc/**" in prompt  # the real change is in the packet the rule applies to


# 2. Visible authorization bypass remains a valid, reportable finding.
def test_visible_authorization_bypass_remains_a_valid_category(tmp_path: Path) -> None:
    header = _CODE_FINDINGS_HEADER
    assert "visibly deleted or bypassed" in header

    stub = _StubLLM({"version": "v1", "findings": [_finding(
        title="permission check removed before privileged update",
        explanation="the isAdmin check that gated this branch was deleted in this diff",
        impact="any authenticated user reaches the privileged update with no role check",
        evidence_snippet="result = apply_policy(request, DENY_ALL)",
    )]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == 1, "a genuinely demonstrated bypass must still be accepted"
    assert not any("Rejected" in w for w in warnings)


# 3. Speculative race language with no demonstrated concurrent execution.
def test_speculative_race_language_is_named_as_invalid_in_the_prompt() -> None:
    header = _CODE_FINDINGS_HEADER
    assert "this could introduce a race" in header.lower()
    assert "no concurrent execution shown" in header.lower()


# 4. Concrete null/bounds/arithmetic defects remain valid categories.
def test_concrete_defect_categories_remain_valid_and_acceptable(tmp_path: Path) -> None:
    header = _CODE_FINDINGS_HEADER
    for category in ("null/nil/undefined", "off-by-one or bounds", "incorrect arithmetic"):
        assert category in header

    stub = _StubLLM({"version": "v1", "findings": [_finding(
        title="denominator can be zero",
        explanation="quantity_per_unit divides the total with no zero check",
        impact="a variant with quantity_per_unit=0 raises a division error on this request path",
    )]})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert len(findings) == 1
    assert not any("Rejected" in w for w in warnings)


# 5. Vendure-style "could be risky under concurrency" reasoning stays silence
# unless the supplied code proves a defect.
def test_generic_concurrency_risk_language_is_named_as_invalid_in_the_prompt() -> None:
    header = _CODE_FINDINGS_HEADER
    assert "this could break transactions" in header.lower()
    assert "evidentiary standard" in header.lower()
    assert "concurrency or locking defect visible in the supplied evidence" in header.lower()


def test_vendure_style_silence_still_passes_cleanly(tmp_path: Path) -> None:
    """Calibrated silence (the real Vendure result) must never itself be
    treated as a validation problem."""
    stub = _StubLLM({"version": "v1", "findings": []})
    findings, warnings = generate_code_findings(context=_context(tmp_path), llm_client=stub)

    assert findings == []
    assert not any("Rejected" in w or "Dropped" in w for w in warnings)
