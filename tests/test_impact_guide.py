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
        nearby_facts=(), candidate_entrypoints=("chat_completion",),
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
