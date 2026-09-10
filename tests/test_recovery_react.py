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
