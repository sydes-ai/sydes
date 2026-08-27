"""PR-level semantic analysis (Increment A): one bounded LLM read of a
change as a whole, complementary to Sydes' structural analysis and never a
replacement for it.

Covers both levels: unit tests against `pr_semantic_analysis` directly (no
repo needed for parsing), and small full-pipeline tests against
`analyze_change` proving the hypothesis-isolation boundary holds in
practice — not just by construction in the model's own field defaults.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.file_facts import build_structural_index
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse, TracingLLMClient
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.analyzer import VerifyChangeOptions, analyze_change, attribute_changed_symbols
from sydes.verify.git_change import resolve_change_set
from sydes.verify.models import ORIGIN_LLM_HYPOTHESIS, VERDICT_VERIFIED
from sydes.verify.pr_semantic_analysis import (
    generate_pr_semantic_analysis,
    parse_semantic_analysis,
)


def _resolve_change_with_symbols(*, repo: Path, base: str, store_root: Path):
    """`resolve_change_set` alone leaves `symbols` empty — `analyze_change`
    attributes them separately via the shared structural index. Mirror that
    here so a test's `change.symbols` matches what a real run would see."""
    change = resolve_change_set(repo_name="api", repo_root=repo, base=base)
    index = build_structural_index(
        [RepoRef(name="api", root=str(repo))], root=store_root, persist=False,
    )
    change.symbols = attribute_changed_symbols(change, index.handler_symbol_batch)
    return change


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    return path


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _multi_file_change(path: Path) -> Path:
    """A synthetic, framework-neutral two-file change: a class method
    gaining a new parameter, and a sibling function gaining the same one."""
    repo = _init_repo(path)
    (repo / "orders.py").write_text(
        "class Order:\n    def set_expires(self):\n        return 1\n", encoding="utf-8",
    )
    (repo / "checkout.py").write_text("def reserve():\n    return True\n", encoding="utf-8")
    _commit_all(repo, "base")
    (repo / "orders.py").write_text(
        "class Order:\n    def set_expires(self, sales_channel=None):\n"
        "        if sales_channel:\n            return 2\n        return 1\n",
        encoding="utf-8",
    )
    (repo / "checkout.py").write_text(
        "def reserve(sales_channel=None):\n    return True\n", encoding="utf-8",
    )
    _commit_all(repo, "channel-specific expiry")
    return repo


class _CountingClient:
    """Returns one scripted response per call; records every call made."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.requests.append(request)
        return LLMResponse(text=self._text)


class _FailingClient:
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("provider unreachable")


_VALID_RESPONSE = json.dumps({
    "change_summary": "Order expiration can now vary by sales channel.",
    "behavior_changes": [
        {
            "description": "channel-specific order expiry",
            "changed_symbols": ["Order.set_expires"],
            "evidence": ["diff adds a sales_channel branch in set_expires"],
            "confidence": 0.7,
        },
    ],
    "important_symbols": [
        {"repo": "api", "file": "orders.py", "symbol": "Order.set_expires", "reason": "directly changed"},
    ],
    "investigation_hints": [
        {
            "description": "callers of Order.set_expires",
            "related_symbols": ["Order.set_expires"],
            "concepts": ["checkout", "expiration"],
            "likely_boundary_types": ["callable", "unknown"],
        },
    ],
    "likely_boundary_types": ["callable"],
    "local_risks": ["off-by-one in channel lookup"],
    "uncertainties": ["complete set of production callers is not established"],
})


def _no_reachable_provider(monkeypatch) -> None:
    """Force route discovery's own (unrelated) client construction to fail
    fast and hermetically, independent of this machine's local environment
    (e.g. a real Ollama server), without touching the injected
    `options.llm_client` this module's tests actually care about."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# --------------------------------------------------------------------------
# 1. Whole-change: one call for a multi-file change, never one per symbol
# --------------------------------------------------------------------------

def test_multi_file_change_produces_exactly_one_semantic_analysis_call(tmp_path: Path) -> None:
    repo = _multi_file_change(tmp_path / "repo")
    change = _resolve_change_with_symbols(repo=repo, base="HEAD~1", store_root=tmp_path / "store")
    assert len(change.symbols) >= 2  # multiple changed symbols across 2 files

    client = _CountingClient(_VALID_RESPONSE)
    analysis, _notes = generate_pr_semantic_analysis(change=change, repo_root=repo, llm_client=client)

    assert client.calls == 1
    assert analysis is not None
    assert analysis.origin == ORIGIN_LLM_HYPOTHESIS


# --------------------------------------------------------------------------
# 2. Structured parsing
# --------------------------------------------------------------------------

def test_valid_json_parses_into_the_semantic_analysis_model() -> None:
    analysis = parse_semantic_analysis(json.loads(_VALID_RESPONSE))
    assert analysis.change_summary == "Order expiration can now vary by sales channel."
    assert len(analysis.behavior_changes) == 1
    assert analysis.behavior_changes[0].confidence == 0.7
    assert analysis.important_symbols[0].reason == "directly changed"
    assert analysis.investigation_hints[0].likely_boundary_types == ["callable", "unknown"]
    assert analysis.likely_boundary_types == ["callable"]
    assert analysis.uncertainties == ["complete set of production callers is not established"]


def test_malformed_model_output_is_handled_conservatively(tmp_path: Path) -> None:
    repo = _multi_file_change(tmp_path / "repo")
    change = resolve_change_set(repo_name="api", repo_root=repo, base="HEAD~1")
    client = _CountingClient("not json at all, sorry")

    analysis, notes = generate_pr_semantic_analysis(change=change, repo_root=repo, llm_client=client)

    assert analysis is None
    assert any("unavailable" in note for note in notes)


def test_invalid_boundary_types_are_dropped_not_invented() -> None:
    """A structural rule, not a taxonomy: only the fixed vocabulary
    survives, everything else is silently dropped."""
    raw = json.loads(_VALID_RESPONSE)
    raw["likely_boundary_types"] = ["callable", "made_up_type", "database"]
    analysis = parse_semantic_analysis(raw)
    assert analysis.likely_boundary_types == ["callable"]


def test_behavior_change_with_no_description_is_dropped_but_others_survive() -> None:
    raw = {"behavior_changes": [{"description": "", "confidence": 0.5}, {"description": "real one"}]}
    analysis = parse_semantic_analysis(raw)
    assert len(analysis.behavior_changes) == 1
    assert analysis.behavior_changes[0].description == "real one"


def test_confidence_is_clamped_into_zero_to_one() -> None:
    raw = {"behavior_changes": [{"description": "x", "confidence": 5.0}]}
    analysis = parse_semantic_analysis(raw)
    assert analysis.behavior_changes[0].confidence == 1.0


# --------------------------------------------------------------------------
# 3. Hypothesis isolation — critical regression test
# --------------------------------------------------------------------------

def test_semantic_findings_alone_cannot_produce_proven_impact_flow_obligation_or_verified(
    tmp_path: Path, monkeypatch,
) -> None:
    """The semantic pass, run with no corroborating structural evidence,
    must leave `accepted_impacts`, `affected_flows`, obligations, and the
    verdict completely untouched — even though it confidently proposes
    behavior changes, important symbols, and investigation hints."""
    repo = _multi_file_change(tmp_path / "repo")
    _no_reachable_provider(monkeypatch)

    options = VerifyChangeOptions(
        base="HEAD~1", llm_policy="auto", run_tests=False,
        llm_client=_CountingClient(_VALID_RESPONSE),
    )
    result = analyze_change(repos=[RepoRef(name="api", root=str(repo))], options=options)

    assert result.pr_semantic_analysis is not None
    assert result.pr_semantic_analysis.behavior_changes  # it did propose something confidently

    # None of that confidence touched anything structural/verification-facing:
    assert result.accepted_impacts == []
    assert result.affected_flows == []
    assert result.summary.counts.obligations == 0
    assert result.summary.verdict != VERDICT_VERIFIED


# --------------------------------------------------------------------------
# 4. Structural failure graceful degradation
# --------------------------------------------------------------------------

def test_semantic_analysis_still_runs_when_changed_symbol_extraction_is_empty(tmp_path: Path) -> None:
    """Construct a change with real files/diff but zero changed symbols —
    the shape of a language/indexing gap (kept framework/language-neutral
    here via an unrecognized extension). The semantic pass must still run
    from the diff and file list alone, never refuse to answer."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "widget.unknownlang").write_text("widget v1\n", encoding="utf-8")
    _commit_all(repo, "base")
    (repo / "widget.unknownlang").write_text(
        "widget v2, now channel-aware and expiring differently\n", encoding="utf-8",
    )
    _commit_all(repo, "change")

    change = resolve_change_set(repo_name="api", repo_root=repo, base="HEAD~1")
    assert change.files  # a real diff exists
    assert change.symbols == []  # nothing attributed — no parser for this extension

    client = _CountingClient(_VALID_RESPONSE)
    analysis, _notes = generate_pr_semantic_analysis(change=change, repo_root=repo, llm_client=client)

    assert client.calls == 1
    assert analysis is not None
    assert analysis.change_summary


# --------------------------------------------------------------------------
# 5. LLM failure
# --------------------------------------------------------------------------

def test_llm_failure_leaves_structural_analysis_functional_and_marks_semantic_unavailable(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = _multi_file_change(tmp_path / "repo")
    _no_reachable_provider(monkeypatch)

    options = VerifyChangeOptions(
        base="HEAD~1", llm_policy="auto", run_tests=False,
        llm_client=_FailingClient(),
    )
    result = analyze_change(repos=[RepoRef(name="api", root=str(repo))], options=options)

    assert result.pr_semantic_analysis is None
    assert any("PR semantic analysis unavailable" in note for note in result.analysis_notes)
    # Structural analysis still completed and produced a real, conservative
    # verdict — a failed semantic pass never crashes or blanks the rest.
    assert result.change.symbols  # symbol attribution still worked
    assert result.summary.verdict != VERDICT_VERIFIED
    assert result.summary.verdict  # a real verdict was still computed


# --------------------------------------------------------------------------
# 6. Tracing
# --------------------------------------------------------------------------

def test_generate_pr_semantic_analysis_requests_the_pr_semantic_analysis_stage(
    tmp_path: Path, monkeypatch,
) -> None:
    """The call-site contract: when it builds its own client (no injected
    `llm_client`), it must ask for `stage="pr_semantic_analysis"` and
    `temperature=None` — never a hard-coded `temperature=0`."""
    captured: dict[str, object] = {}

    class _Stub:
        def generate(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(text=_VALID_RESPONSE)

    def _fake_create_default_llm_client(**kwargs):
        captured.update(kwargs)
        return _Stub()

    monkeypatch.setattr(
        "sydes.verify.pr_semantic_analysis.create_default_llm_client",
        _fake_create_default_llm_client,
    )

    repo = _multi_file_change(tmp_path / "repo")
    change = resolve_change_set(repo_name="api", repo_root=repo, base="HEAD~1")
    generate_pr_semantic_analysis(change=change, repo_root=repo)

    assert captured.get("stage") == "pr_semantic_analysis"
    assert captured.get("temperature") is None


def test_trace_shows_exactly_one_call_under_the_pr_semantic_analysis_stage(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    repo = _multi_file_change(tmp_path / "repo")
    change = resolve_change_set(repo_name="api", repo_root=repo, base="HEAD~1")

    inner = _CountingClient(_VALID_RESPONSE)
    traced_client = TracingLLMClient(inner, stage="pr_semantic_analysis", provider="fake", model="fake-model")

    analysis, _notes = generate_pr_semantic_analysis(change=change, repo_root=repo, llm_client=traced_client)

    assert analysis is not None
    assert inner.calls == 1
    lines = (tmp_path / "traces" / "llm_calls.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    semantic_calls = [item for item in records if item["stage"] == "pr_semantic_analysis"]
    assert len(semantic_calls) == 1
    assert semantic_calls[0]["success"] is True


# --------------------------------------------------------------------------
# 7. Serialization / reporting
# --------------------------------------------------------------------------

def test_serialization_and_concise_report_render_the_change_analysis_section(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = _multi_file_change(tmp_path / "repo")
    _no_reachable_provider(monkeypatch)

    options = VerifyChangeOptions(
        base="HEAD~1", llm_policy="auto", run_tests=False,
        llm_client=_CountingClient(_VALID_RESPONSE),
    )
    result = analyze_change(repos=[RepoRef(name="api", root=str(repo))], options=options)

    dumped = result.model_dump()
    assert dumped["pr_semantic_analysis"]["change_summary"] == (
        "Order expiration can now vary by sales channel."
    )
    assert dumped["pr_semantic_analysis"]["origin"] == ORIGIN_LLM_HYPOTHESIS

    report = render_verify_change_terminal(result)
    assert "CHANGE ANALYSIS" in report
    assert "Order expiration can now vary by sales channel." in report
    assert "channel-specific order expiry" in report
    assert "callers of Order.set_expires" in report
    assert "complete set of production callers is not established" in report
    assert "System impact" in report
    assert report.index("CHANGE ANALYSIS") < report.index("System impact")

    verbose_report = render_verify_change_terminal(result, verbose=True)
    assert "CHANGE ANALYSIS" in verbose_report


def test_report_omits_change_analysis_section_when_analysis_is_absent(tmp_path: Path) -> None:
    repo = _multi_file_change(tmp_path / "repo")
    change = resolve_change_set(repo_name="api", repo_root=repo, base="HEAD~1")
    from sydes.verify.models import ChangeVerificationResult

    result = ChangeVerificationResult(change=change)  # pr_semantic_analysis defaults to None
    report = render_verify_change_terminal(result)
    assert "CHANGE ANALYSIS" not in report
