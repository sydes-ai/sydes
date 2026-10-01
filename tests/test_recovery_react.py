"""`sydes.recovery.react.extract_turn` -- parsing one agent turn out of raw
LLM text, tolerant of the malformed shapes real models actually produce."""

from __future__ import annotations

import pytest

from sydes.recovery.react import extract_turn
from sydes.recovery.schema import RecoveryError


def test_extracts_a_clean_json_object():
    payload = extract_turn('{"tool": "read_file", "args": {"path": "a.py"}}', max_chars=4000)
    assert payload == {"tool": "read_file", "args": {"path": "a.py"}}


def test_strips_markdown_code_fences():
    text = '```json\n{"tool": "read_file", "args": {}}\n```'
    payload = extract_turn(text, max_chars=4000)
    assert payload == {"tool": "read_file", "args": {}}


def test_survives_a_dangling_extra_closing_brace_after_a_complete_object():
    """A real recovery run against an open-source PR (Netflix/dispatch)
    produced exactly this shape: a complete, valid JSON object followed by
    one unmatched trailing '}' -- the model nested 'candidate_tests' as a
    sibling of 'final' instead of inside it, then left an extra brace
    dangling at the very end. `rfind("}")` (the previous approach) can't
    tell that trailing brace apart from a matching one; a balanced scan
    from the first '{' can."""
    raw = (
        '{"final": {"candidate_path": {"entrypoint": "scheduled job x", '
        '"target_node": "a", "nodes": [{"symbol": "a", "file": "s.py"}]}}, '
        '"candidate_tests": []}}'
    )
    payload = extract_turn(raw, max_chars=4000)
    assert payload["final"]["candidate_path"]["target_node"] == "a"
    assert payload["candidate_tests"] == []


def test_survives_trailing_prose_after_the_json_object():
    raw = '{"tool": "read_file", "args": {"path": "a.py"}} — investigating further.'
    payload = extract_turn(raw, max_chars=4000)
    assert payload == {"tool": "read_file", "args": {"path": "a.py"}}


def test_braces_inside_a_string_value_do_not_confuse_the_scanner():
    raw = '{"final": {"relationship": "uses {not a brace} and says \\"hi\\""}}'
    payload = extract_turn(raw, max_chars=4000)
    assert payload["final"]["relationship"] == 'uses {not a brace} and says "hi"'


def test_unbalanced_object_that_never_closes_raises_recovery_error():
    with pytest.raises(RecoveryError):
        extract_turn('{"tool": "read_file", "args": {"path": "a.py"', max_chars=4000)


def test_no_json_object_at_all_raises_recovery_error():
    with pytest.raises(RecoveryError):
        extract_turn("not json and no braces here", max_chars=4000)


def test_non_object_json_raises_recovery_error():
    with pytest.raises(RecoveryError):
        extract_turn("[1, 2, 3]", max_chars=4000)


def test_a_long_valid_final_answer_is_not_cut_at_max_chars():
    """Observed in CI with openai:gpt-5.1: a ~6k-char final answer was truncated to
    max_chars before parsing and rejected as 'not a JSON object'."""
    import json

    raw = json.dumps({"final": {"status": "established", "evidence": ["x" * 300] * 20}})
    assert len(raw) > 4000
    assert extract_turn(raw, max_chars=4000)["final"]["status"] == "established"


def test_error_says_what_the_reply_looked_like():
    with pytest.raises(RecoveryError, match=r"27 chars, starts 'not json and no braces here'"):
        extract_turn("not json and no braces here", max_chars=4000)


def test_one_malformed_turn_is_re_asked_once_then_the_loop_continues():
    from types import SimpleNamespace

    from sydes.llm.client import LLMResponse
    from sydes.recovery.react import run_react_loop

    replies = iter(["Sure! Let me think about it.", '{"final": {"ok": true}}'])

    class _Client:
        prompts: list[str] = []

        def generate(self, request):
            self.prompts.append(request.prompt)
            return LLMResponse(text=next(replies))

    client = _Client()
    stats = SimpleNamespace(turns=0, llm_calls=0, latency_ms=0.0, prompt_tokens=0, completion_tokens=0, files_read=[])
    result = run_react_loop(
        client=client, tools=None, system_prompt="s", initial_prompt="p", max_turns=3,
        max_response_chars=4000, stats=stats, parse_final=lambda text: text,
    )
    assert result == '{"ok": true}'
    assert stats.llm_calls == 2 and "could not be used" in client.prompts[1]


def test_two_malformed_replies_in_a_row_still_fail():
    from types import SimpleNamespace

    from sydes.llm.client import LLMResponse
    from sydes.recovery.react import run_react_loop

    class _Client:
        def generate(self, request):
            return LLMResponse(text="no json")

    stats = SimpleNamespace(turns=0, llm_calls=0, latency_ms=0.0, prompt_tokens=0, completion_tokens=0, files_read=[])
    with pytest.raises(RecoveryError, match="not a JSON object"):
        run_react_loop(
            client=_Client(), tools=None, system_prompt="s", initial_prompt="p", max_turns=3,
            max_response_chars=4000, stats=stats, parse_final=lambda text: text,
        )
