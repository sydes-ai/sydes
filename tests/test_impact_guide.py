"""The guide's contract: one action, strictly validated, or a closed failure.

`LLMImpactGuide` never gets to guess. These tests pin the parsing/validation
boundary directly, without a live model: a fake `LLMClient` stands in for the
provider, and every case checks either a specific `InvestigationDecision` or
that malformed/out-of-contract output raises `GuideError` rather than being
coerced into something plausible.
"""

from __future__ import annotations

import pytest

from sydes.impact.guide import (
    _SYSTEM_PROMPT,
    FixedImpactGuide,
    GuideError,
    LLMImpactGuide,
    build_guide_prompt,
    parse_guide_decision,
)
from sydes.impact.models import (
    ACTION_INSPECT_ENCLOSING_FUNCTION,
    ACTION_STOP_UNRESOLVED,
    ACTION_TRACE_CALLERS,
    REASON_PARTIAL_PATH_DEAD_END,
    ImpactQuestion,
)
from sydes.llm.client import LLMClientError, LLMResponse


class _FakeLLMClient:
    def __init__(self, text: str | Exception) -> None:
        self._text = text

    def generate(self, request):  # noqa: ANN001 - matches LLMClient protocol
        if isinstance(self._text, Exception):
            raise self._text
        return LLMResponse(text=self._text)


def _question(**overrides) -> ImpactQuestion:
    base = dict(
        repo="app", changed_symbol="process_chat_response", qualified_name="",
        file="app/chat.py", reason=REASON_PARTIAL_PATH_DEAD_END,
        partial_paths=("calls:process_chat_response",),
        known_files=("app/chat.py",), known_entrypoints=(),
        attempted_actions=(), candidate_entrypoints=("chat_completion",),
        candidate_origins=("process_chat_response",),
        source_context="", remaining_budget=3,
    )
    base.update(overrides)
    return ImpactQuestion(**base)


def test_valid_action_selection_parses_cleanly() -> None:
    decision = parse_guide_decision(
        '{"action": "inspect_enclosing_function", "target": "chat_completion", '
        '"sought_symbol": "process_chat_response", '
        '"rationale": "check whether it calls the dead end"}'
    )
    assert decision.action == ACTION_INSPECT_ENCLOSING_FUNCTION
    assert decision.target == "chat_completion"
    assert decision.sought_symbol == "process_chat_response"


def test_stop_unresolved_needs_no_target() -> None:
    decision = parse_guide_decision('{"action": "STOP_UNRESOLVED"}')
    assert decision.action == ACTION_STOP_UNRESOLVED
    assert decision.target == ""


def test_action_is_case_insensitive() -> None:
    decision = parse_guide_decision('{"action": "TRACE_CALLERS", "target": "helper"}')
    assert decision.action == ACTION_TRACE_CALLERS


def test_markdown_fenced_json_is_unwrapped() -> None:
    decision = parse_guide_decision(
        '```json\n{"action": "stop_unresolved"}\n```'
    )
    assert decision.action == ACTION_STOP_UNRESOLVED


def test_non_json_output_fails_closed() -> None:
    with pytest.raises(GuideError):
        parse_guide_decision("I think you should check the caller.")


def test_unsupported_action_fails_closed() -> None:
    with pytest.raises(GuideError):
        parse_guide_decision('{"action": "run_shell_command", "target": "rm -rf /"}')


def test_missing_required_target_fails_closed() -> None:
    with pytest.raises(GuideError):
        parse_guide_decision('{"action": "trace_callers"}')


def test_source_action_missing_sought_symbol_fails_closed() -> None:
    with pytest.raises(GuideError):
        parse_guide_decision('{"action": "inspect_enclosing_function", "target": "chat_completion"}')


def test_non_source_action_does_not_require_sought_symbol() -> None:
    decision = parse_guide_decision('{"action": "trace_callers", "target": "helper"}')
    assert decision.sought_symbol == ""


def test_json_list_instead_of_object_fails_closed() -> None:
    with pytest.raises(GuideError):
        parse_guide_decision('[{"action": "stop_unresolved"}]')


def test_llm_guide_returns_a_decision_from_a_fake_client() -> None:
    client = _FakeLLMClient(
        '{"action": "inspect_source_span", "target": "chat_completion", '
        '"sought_symbol": "process_chat_response"}'
    )
    guide = LLMImpactGuide(client)
    decision = guide.investigate(_question())
    assert decision.action == "inspect_source_span"
    assert decision.target == "chat_completion"
    assert decision.sought_symbol == "process_chat_response"


def test_llm_guide_wraps_provider_failure_as_guide_error() -> None:
    client = _FakeLLMClient(LLMClientError("provider unreachable"))
    guide = LLMImpactGuide(client)
    with pytest.raises(GuideError):
        guide.investigate(_question())


def test_llm_guide_wraps_malformed_output_as_guide_error() -> None:
    client = _FakeLLMClient("not json at all")
    guide = LLMImpactGuide(client)
    with pytest.raises(GuideError):
        guide.investigate(_question())


def test_fixed_guide_always_stops() -> None:
    guide = FixedImpactGuide()
    decision = guide.investigate(_question())
    assert decision.action == ACTION_STOP_UNRESOLVED


def test_prompt_names_only_supplied_candidates() -> None:
    question = _question()
    prompt = build_guide_prompt(question)
    assert "chat_completion" in prompt
    assert "process_chat_response" in prompt


# --- Reviewer-grade `entrypoint` label contract ------------------------------
#
# These pin the system prompt's own words, not a downstream code check: label
# quality is a contract fixed by asking the model clearly, not by filtering
# its answer afterward. See guide.py's `_SYSTEM_PROMPT`.

def test_system_prompt_demands_a_reviewer_facing_behavior_label() -> None:
    """The contract must explicitly ask for behavior language, not just an
    identifier — the exact gap real PR runs exposed."""
    assert "BEHAVIOR LABEL" in _SYSTEM_PROMPT
    assert "reviewer-facing" in _SYSTEM_PROMPT
    assert "plain domain language" in _SYSTEM_PROMPT


def test_system_prompt_explicitly_forbids_bare_symbol_names() -> None:
    """Must name the failure mode directly: a bare identifier is never an
    acceptable label, regardless of which specific identifier it is — a
    structural rule, not a list of known-bad names."""
    assert "never be only a bare function/method/class/file/module name" in _SYSTEM_PROMPT
    assert "regardless of what that name is" in _SYSTEM_PROMPT


def test_system_prompt_forbids_generic_restatement_templates() -> None:
    """"X behavior changes" and its relatives must be named as invalid —
    they restate that something changed without saying what."""
    assert '"X behavior changes"' in _SYSTEM_PROMPT
    assert '"changed function behavior"' in _SYSTEM_PROMPT


def test_system_prompt_keeps_label_and_reason_distinct() -> None:
    """The label must stay a short phrase; the causal explanation belongs
    entirely in `reason`, never stuffed into the label."""
    assert "a different field from `reason`" in _SYSTEM_PROMPT
    assert "never carry the causal explanation itself" in _SYSTEM_PROMPT


def test_system_prompt_permits_domain_overlap_with_the_changed_symbol() -> None:
    """A label must not be rejected merely for sharing a domain word with
    the changed symbol — only for being nothing but that symbol restated."""
    assert "Reusing a domain word that also appears in the changed symbol" in _SYSTEM_PROMPT
    assert "is fine" in _SYSTEM_PROMPT


def test_system_prompt_still_rejects_self_referential_restatement() -> None:
    """The pre-existing self-reference contract (a candidate must not just
    be the changed symbol itself) must still be present alongside the new
    label-quality language — this task tightens the contract, it does not
    replace the earlier fix."""
    assert "the changed symbol itself" in _SYSTEM_PROMPT
    assert "restate its own input" in _SYSTEM_PROMPT or "restating the changed symbol" in _SYSTEM_PROMPT \
        or "renamed, rephrased, or reworded version of the changed symbol" in _SYSTEM_PROMPT


def test_llm_guide_makes_exactly_one_provider_call_per_investigate() -> None:
    """Tightening the prompt must not add a second call (e.g. a self-check
    or retry pass) — the guide's cost model is unchanged."""
    class _CountingClient:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, request):  # noqa: ANN001 - matches LLMClient protocol
            self.calls += 1
            return LLMResponse(text='{"action": "stop_unresolved"}')

    client = _CountingClient()
    guide = LLMImpactGuide(client)
    guide.investigate(_question())
    assert client.calls == 1


def test_llm_guide_still_parses_a_reviewer_grade_label_candidate() -> None:
    """A model following the tightened contract returns a multi-word,
    non-identifier label — this must still parse exactly as before; the
    prompt change touches instructions only, never the response schema."""
    client = _FakeLLMClient(
        '{"action": "infer_impact", "candidates": [{"entrypoint": '
        '"cached listing results going stale after a write", '
        '"confidence": 0.6, "reason": "the write path no longer invalidates the cache"}]}'
    )
    guide = LLMImpactGuide(client)
    decision = guide.investigate(_question())
    assert len(decision.candidates) == 1
    assert decision.candidates[0].entrypoint_label == "cached listing results going stale after a write"


def test_candidate_based_on_changed_symbols_parses_from_a_json_array() -> None:
    """`based_on_changed_symbols` — the whole-change/blank-symbol grounding
    field — parses a JSON array of strings into a tuple, stripped."""
    client = _FakeLLMClient(
        '{"action": "infer_impact", "candidates": [{"entrypoint": '
        '"Merge operations now survive caller cancellation across several entrypoints", '
        '"confidence": 0.8, "reason": "both changed symbols run detached from the request context", '
        '"based_on_changed_symbols": ["MergePullRequest", " UpdatePullRequest "]}]}'
    )
    guide = LLMImpactGuide(client)
    decision = guide.investigate(_question())
    assert len(decision.candidates) == 1
    assert decision.candidates[0].based_on_changed_symbols == ("MergePullRequest", "UpdatePullRequest")


def test_candidate_without_based_on_changed_symbols_defaults_to_empty() -> None:
    """Backward compatibility: a guide response predating this field (or one
    that simply omits it) must still parse — the field defaults to `()`,
    never a parse failure."""
    client = _FakeLLMClient(
        '{"action": "infer_impact", "candidates": [{"entrypoint": "GET /cases", '
        '"confidence": 0.6, "reason": "shares a query helper"}]}'
    )
    guide = LLMImpactGuide(client)
    decision = guide.investigate(_question())
    assert len(decision.candidates) == 1
    assert decision.candidates[0].based_on_changed_symbols == ()
