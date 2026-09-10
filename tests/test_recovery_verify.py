"""`sydes.recovery.verify` — edge-level Layer 1 (deterministic) + Layer 2
(adversarial LLM) verification, the shortest-sufficient-path truncation
algorithm, and test-evidence verification.

Includes the adversarial cases the refinement explicitly asked for: same-
module co-registration with no dispatch evidence, matching names in
different files with no reference, a framework-shaped decorator/handler
pair with no message-type evidence tying them together, an explicit call
(accept), an explicit registration mapping a callback to a runtime trigger
(accept), a bad trailing edge after a valid prefix (prefix preserved,
suffix dropped), an unnecessary extra edge beyond `target_node` (dropped
outright, not even reported as unresolved), fabricated/mismatched evidence
lines (rejected without an LLM call), a test that only imports its target
(rejected), and a test that directly invokes it (accepted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryRunStats
from sydes.recovery.schema import (
    RecoveredEdge,
    RecoveredEvidence,
    RecoveredNode,
    RecoveredPath,
    RecoveredTest,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
)
from sydes.recovery.tools import RepoTools
from sydes.recovery.verify import verify_recovery_result


class ScriptedVerifier:
    """Returns one scripted response per call, in the order the two
    verifier passes (edges, then tests) are invoked."""

    def __init__(self, *responses: dict) -> None:
        self._responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if not self._responses:
            raise AssertionError("ScriptedVerifier exhausted its scripted responses")
        return LLMResponse(text=self._responses.pop(0))


def _edge(from_: str, to: str, *, evidence: list[RecoveredEvidence] | None = None, relationship: str = "calls") -> RecoveredEdge:
    return RecoveredEdge(**{"from": from_, "to": to}, relationship=relationship, evidence=evidence or [])


def _path(entrypoint: str, target_node: str, node_symbols: list[str], edges: list[RecoveredEdge], *, file: str = "a.ts") -> RecoveredPath:
    return RecoveredPath(
        entrypoint=entrypoint, target_node=target_node,
        nodes=[RecoveredNode(symbol=s, file=file) for s in node_symbols],
        edges=edges,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "a.ts").write_text(
        "\n".join(f"line {i}: filler" for i in range(1, 5))
        + "\nline 5: route.register('handler', handlerFn)\n"
        + "line 6: export function handlerFn() { doWork(); }\n"
        + "line 7: const x = OtherThing;\n"
        + "line 8: class HandlerRegistrar {}\n"
    )
    return tmp_path


def _stats() -> RecoveryRunStats:
    return RecoveryRunStats()


# 1. Same-module co-registration, no dispatch evidence -> reject edge
def test_same_module_coregistration_with_no_dispatch_evidence_is_rejected(repo: Path):
    edge = _edge(
        "ControllerX", "HandlerY",
        evidence=[RecoveredEvidence(file="a.ts", line_start=1, line_end=2, fact="ControllerX and HandlerY declared filler")],
    )
    path = _path("GET /x", "HandlerY", ["ControllerX", "HandlerY"], [edge])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier(
        {"verdicts": [{"index": 0, "accept": False, "reason": "both symbols merely co-declared, no dispatch shown"}]}
    )
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_UNRESOLVED
    assert verified.recovered_paths[0].unresolved_suffix[0].rejection_reason


# 2. Matching names in different files, no call/reference -> reject edge
def test_matching_names_in_different_files_with_no_reference_is_rejected(repo: Path):
    (repo / "b.ts").write_text("line1: unrelated\nline2: also unrelated\n")
    edge = _edge(
        "SameName", "SameName",
        evidence=[
            RecoveredEvidence(file="a.ts", line_start=1, line_end=1, fact="declares SameName here"),
            RecoveredEvidence(file="b.ts", line_start=1, line_end=2, fact="declares SameName there too"),
        ],
    )
    path = _path("GET /x", "SameName", ["Entry", "SameName"], [_edge("Entry", "SameName", evidence=edge.evidence)])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier(
        {"verdicts": [{"index": 0, "accept": False, "reason": "same name in two files is not evidence of a real reference"}]}
    )
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_UNRESOLVED


# 3. Decorator + handler class, no evidence tying message type to dispatch site -> reject full path
def test_decorator_and_handler_with_no_message_type_evidence_rejects_whole_path(repo: Path):
    e1 = _edge("Route", "Controller", evidence=[RecoveredEvidence(file="a.ts", line_start=5, line_end=5, fact="route.register('handler', handlerFn)")])
    e2 = _edge("Controller", "HandlerRegistrar", evidence=[RecoveredEvidence(file="a.ts", line_start=8, line_end=8, fact="class HandlerRegistrar decorated as a handler")])
    path = _path("GET /x", "HandlerRegistrar", ["Route", "Controller", "HandlerRegistrar"], [e1, e2])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier(
        {"verdicts": [
            {"index": 0, "accept": True, "reason": "explicit registration call"},
            {"index": 1, "accept": False, "reason": "decorator presence alone does not show this message type dispatches here"},
        ]}
    )
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_PARTIAL
    assert [n.symbol for n in verified.recovered_paths[0].nodes] == ["Route", "Controller"]


# 4. Explicit function call -> accept
def test_explicit_function_call_is_accepted(repo: Path):
    edge = _edge("handlerFn", "doWork", evidence=[RecoveredEvidence(file="a.ts", line_start=6, line_end=6, fact="handlerFn calls doWork()")])
    path = _path("GET /x", "doWork", ["handlerFn", "doWork"], [edge])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "direct call shown"}]})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_ESTABLISHED


# 5. Explicit registration mapping callback to runtime trigger -> accept
def test_explicit_registration_mapping_to_runtime_trigger_is_accepted(repo: Path):
    edge = _edge("route", "handlerFn", evidence=[RecoveredEvidence(file="a.ts", line_start=5, line_end=5, fact="route.register('handler', handlerFn)")])
    path = _path("trigger", "handlerFn", ["route", "handlerFn"], [edge])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "explicit registration callback"}]})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_ESTABLISHED


# 6. One bad trailing edge after a valid prefix -> preserve prefix, drop suffix
def test_bad_trailing_edge_after_valid_prefix_preserves_prefix(repo: Path):
    e1 = _edge("route", "handlerFn", evidence=[RecoveredEvidence(file="a.ts", line_start=5, line_end=6, fact="route registers handlerFn")])
    e2 = _edge("handlerFn", "doWork", evidence=[RecoveredEvidence(file="a.ts", line_start=6, line_end=6, fact="handlerFn mentions doWork")])
    path = _path("GET /x", "doWork", ["route", "handlerFn", "doWork"], [e1, e2])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier(
        {"verdicts": [
            {"index": 0, "accept": True, "reason": "explicit registration"},
            {"index": 1, "accept": False, "reason": "mention alone does not prove a call"},
        ]}
    )
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    result_path = verified.recovered_paths[0]
    assert result_path.status == STATUS_PARTIAL
    assert [n.symbol for n in result_path.nodes] == ["route", "handlerFn"]
    assert len(result_path.unresolved_suffix) == 1
    assert result_path.unresolved_suffix[0].to_symbol == "doWork"


# 7. Unnecessary extra edge beyond target_node -> shortest-sufficient path retained, dropped outright
def test_unnecessary_extra_edge_beyond_target_is_dropped_even_if_accepted(repo: Path):
    e1 = _edge("route", "handlerFn", evidence=[RecoveredEvidence(file="a.ts", line_start=5, line_end=6, fact="route registers handlerFn")])
    e2 = _edge("handlerFn", "OtherThing", evidence=[RecoveredEvidence(file="a.ts", line_start=7, line_end=7, fact="handlerFn near OtherThing")])
    path = _path("GET /x", "handlerFn", ["route", "handlerFn", "OtherThing"], [e1, e2])
    draft = RecoveryResult(recovered_paths=[path])
    # Even if the verifier would ACCEPT edge 1 (handlerFn -> OtherThing), it must never appear:
    # target_node is handlerFn, reached after edge 0.
    client = ScriptedVerifier(
        {"verdicts": [
            {"index": 0, "accept": True, "reason": "explicit registration"},
            {"index": 1, "accept": True, "reason": "would have been accepted, but is unnecessary padding"},
        ]}
    )
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    result_path = verified.recovered_paths[0]
    assert result_path.status == STATUS_ESTABLISHED
    assert [n.symbol for n in result_path.nodes] == ["route", "handlerFn"]
    assert result_path.unresolved_suffix == []


# 8. Fake or mismatched evidence lines -> reject without an LLM call
def test_fabricated_line_range_is_rejected_deterministically(repo: Path):
    edge = _edge("route", "handlerFn", evidence=[RecoveredEvidence(file="a.ts", line_start=900, line_end=901, fact="route registers handlerFn")])
    path = _path("GET /x", "handlerFn", ["route", "handlerFn"], [edge])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_UNRESOLVED
    assert client.calls == 0  # rejected before ever asking the verifier


def test_evidence_lexically_unrelated_to_claim_is_rejected_deterministically(repo: Path):
    # Cites real, readable lines, but neither the edge symbols nor the
    # stated fact's own words appear anywhere in them.
    edge = _edge(
        "route", "handlerFn",
        evidence=[RecoveredEvidence(file="a.ts", line_start=1, line_end=2, fact="performs an unrelated database migration step")],
    )
    path = _path("GET /x", "handlerFn", ["route", "handlerFn"], [edge])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_UNRESOLVED
    assert client.calls == 0


# 9/10. Recovered test evidence rigor
def test_recovered_test_that_only_imports_target_is_rejected(repo: Path):
    (repo / "spec.ts").write_text("import { handlerFn } from './a';\ndescribe('unrelated', () => {});\n")
    test = RecoveredTest(
        file="spec.ts", test="unrelated suite", covers="handlerFn behavior",
        evidence=[RecoveredEvidence(file="spec.ts", line_start=1, line_end=1, fact="imports handlerFn")],
    )
    draft = RecoveryResult(recovered_tests=[test])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": False, "reason": "import only, no call or assertion shown"}]})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.recovered_tests[0].status == "rejected"


def test_recovered_test_that_directly_invokes_changed_behavior_is_accepted(repo: Path):
    (repo / "spec.ts").write_text("import { handlerFn } from './a';\nit('works', () => { expect(handlerFn()).toBe(1); });\n")
    test = RecoveredTest(
        file="spec.ts", test="works", covers="handlerFn behavior",
        evidence=[RecoveredEvidence(file="spec.ts", line_start=2, line_end=2, fact="calls handlerFn() and asserts on its result")],
    )
    draft = RecoveryResult(recovered_tests=[test])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "direct call and assertion shown"}]})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.recovered_tests[0].status == "accepted"


def test_verifier_giving_no_verdict_for_an_edge_is_treated_as_rejection(repo: Path):
    edge = _edge("route", "handlerFn", evidence=[RecoveredEvidence(file="a.ts", line_start=5, line_end=6, fact="route registers handlerFn")])
    path = _path("GET /x", "handlerFn", ["route", "handlerFn"], [edge])
    draft = RecoveryResult(recovered_paths=[path])
    client = ScriptedVerifier({"verdicts": []})
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=_stats())
    assert verified.status == STATUS_UNRESOLVED
