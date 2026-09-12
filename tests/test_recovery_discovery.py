"""`sydes.recovery.discovery` — Stage A in isolation: proposes a candidate
path/tests, no evidence-citation responsibility. See
`tests/test_recovery_schema.py` for the strict parsing rules this stage's
output is held to; these tests exercise the tool-use loop wrapper itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryRunStats
from sydes.recovery.context import RecoveryContext
from sydes.recovery.discovery import _build_initial_prompt, discover_candidate
from sydes.recovery.schema import RecoveryError


def _context() -> RecoveryContext:
    return RecoveryContext(
        reason_first_pass_stopped="no established path", gap_kinds=("no_established_flow",),
        diff_summary="- modified app/handler.ts", changed_symbols=(), current_result_summary="",
        cbm_fragments="", known_entrypoints=(), test_candidates=(), unresolved_gaps=(),
    )


class SequencedClient:
    def __init__(self, responses: list[dict | str]) -> None:
        self._responses = list(responses)

    def generate(self, request: LLMRequest) -> LLMResponse:
        payload = self._responses.pop(0)
        return LLMResponse(text=payload if isinstance(payload, str) else json.dumps(payload))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "handler.ts").write_text("content\n")
    return tmp_path


def _tools(repo: Path):
    from sydes.recovery.tools import RepoTools
    return RepoTools(repo)


def test_discover_candidate_returns_path_and_tests(repo: Path):
    client = SequencedClient([
        {"final": {
            "candidate_path": {"entrypoint": "GET /x", "target_node": "b", "nodes": [{"symbol": "a"}, {"symbol": "b", "file": "handler.ts"}]},
            "candidate_tests": [{"file": "x.spec.ts", "test": "t", "covers": "c"}],
        }}
    ])
    stats = RecoveryRunStats()
    path, tests = discover_candidate(_context(), tools=_tools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert [n.symbol for n in path.nodes] == ["a", "b"]
    assert path.nodes[1].file == "handler.ts"
    assert len(tests) == 1
    assert stats.llm_calls == 1


def test_discover_candidate_can_return_no_path(repo: Path):
    client = SequencedClient([{"final": {"candidate_path": None, "candidate_tests": []}}])
    stats = RecoveryRunStats()
    path, tests = discover_candidate(_context(), tools=_tools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert path is None
    assert tests == []


def test_discover_candidate_malformed_output_raises_recovery_error(repo: Path):
    client = SequencedClient(["nonsense, not json"])
    stats = RecoveryRunStats()
    with pytest.raises(RecoveryError):
        discover_candidate(_context(), tools=_tools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)


def test_discover_candidate_empty_nodes_path_degrades_to_none_not_error(repo: Path):
    """A genuinely malformed candidate (no nodes at all) must degrade
    gracefully to 'no candidate proposed', not crash the whole recovery
    attempt before Stage B or the retry budget ever runs."""
    client = SequencedClient([
        {"final": {"candidate_path": {"entrypoint": "x", "target_node": "a", "nodes": []}, "candidate_tests": []}}
    ])
    stats = RecoveryRunStats()
    path, tests = discover_candidate(_context(), tools=_tools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert path is None
    assert tests == []


def test_discover_candidate_genuine_single_node_zero_hop_path_is_valid(repo: Path):
    """A real PR (a scheduled/cron job with no separate dispatcher symbol
    to name) produced exactly this shape -- a single node is a legitimate
    answer, not automatically a lazy non-answer. See
    `sydes.recovery.agent._direct_entrypoint_edge` for how it's verified."""
    client = SequencedClient([
        {"final": {
            "candidate_path": {"entrypoint": "scheduled job x", "target_node": "a", "nodes": [{"symbol": "a", "file": "handler.ts"}]},
            "candidate_tests": [],
        }}
    ])
    stats = RecoveryRunStats()
    path, tests = discover_candidate(_context(), tools=_tools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["a"]
    assert tests == []


def test_prompt_shows_no_routes_available_when_repo_known_routes_empty():
    prompt = _build_initial_prompt(_context())
    assert "repo_known_routes" in prompt
    assert "(none available)" in prompt


def test_prompt_lists_repo_known_routes_when_present():
    context = RecoveryContext(
        reason_first_pass_stopped="no established path", gap_kinds=("no_established_flow",),
        diff_summary="- modified app/handler.ts", changed_symbols=(), current_result_summary="",
        cbm_fragments="", known_entrypoints=(), test_candidates=(), unresolved_gaps=(),
        repo_known_routes=("POST /v1/users -> UserController.createUser (src/user/controller.ts)",),
    )
    prompt = _build_initial_prompt(context)
    assert "POST /v1/users -> UserController.createUser (src/user/controller.ts)" in prompt
    assert "(none available)" not in prompt
