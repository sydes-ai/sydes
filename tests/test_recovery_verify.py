"""`sydes.recovery.verify` — Layer 0 (canonical identity), Layer 1
(deterministic evidence), and Layer 2 (adversarial LLM) verification, the
shortest-sufficient-path truncation algorithm, and independent path/test
verification.

Includes the required adversarial cases for canonical identity: same class
name in a different module (rejected deterministically, no LLM call even
needed), same method name in a different package (rejected), an exact
file-qualified entity with real evidence (accepted), an ambiguous
unresolved entity (never establishable), and the changed-target identity
preserved end to end (an edge reaching `target_node` must resolve to one
of the diff's own changed files). Plus the pre-existing edge/path/test
adversarial cases from earlier rounds, still exercised against the new
schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryRunStats
from sydes.recovery.schema import (
    EntityRef,
    RecoveredEdge,
    RecoveredPath,
    RecoveredTest,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    TEST_STATUS_ACCEPTED,
    TEST_STATUS_REJECTED,
)
from sydes.recovery.tools import RepoTools
from sydes.recovery.verify import verify_paths, verify_tests


class ScriptedVerifier:
    """Returns one scripted response per call, in the order path/test
    verification passes are invoked."""

    def __init__(self, *responses: dict) -> None:
        self._responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if not self._responses:
            raise AssertionError("ScriptedVerifier exhausted its scripted responses")
        return LLMResponse(text=self._responses.pop(0))


def _entity(symbol: str, file: str, qualified_name: str | None = None) -> EntityRef:
    return EntityRef(symbol=symbol, file=file, qualified_name=qualified_name)


def _edge(from_e: EntityRef, to_e: EntityRef, *, evidence=None, relationship: str = "calls") -> RecoveredEdge:
    return RecoveredEdge(**{"from": from_e, "to": to_e}, relationship=relationship, evidence=evidence or [])


def _path(entrypoint: str, target_node: str, nodes: list[EntityRef], edges: list[RecoveredEdge]) -> RecoveredPath:
    return RecoveredPath(entrypoint=entrypoint, target_node=target_node, nodes=nodes, edges=edges)


def _stats() -> RecoveryRunStats:
    return RecoveryRunStats()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "correct_module").mkdir()
    (tmp_path / "wrong_module").mkdir()
    (tmp_path / "correct_module" / "UserController.java").write_text(
        "line1\nclass UserController {\n  getUser() { return service.getUser(); }\n}\n"
    )
    (tmp_path / "wrong_module" / "UserController.java").write_text(
        "line1\nclass UserController {\n  getUser() { return other.getUser(); }\n}\n"
    )
    (tmp_path / "route.ts").write_text("route.register('handler', handlerFn)\nline2\nline3\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Layer 0 -- canonical entity identity (the required adversarial cases).
# ---------------------------------------------------------------------------


def test_same_class_name_different_module_rejected_deterministically(repo: Path):
    """The exact real-world failure this layer exists to prevent: a
    same-named `UserController` in an unrelated module must never
    establish a path to the actually-changed one."""
    route = _entity("route", "route.ts")
    wrong_controller = _entity("UserController.getUser", "wrong_module/UserController.java")
    edge = _edge(
        route, wrong_controller,
        evidence=[{"file": "wrong_module/UserController.java", "line_start": 1, "line_end": 3, "fact": "UserController.getUser handles the route"}],
    )
    path = _path("GET /user/{id}", "UserController.getUser", [route, wrong_controller], [edge])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "looks right"}]})
    changed_files = frozenset({"correct_module/UserController.java"})
    result = verify_paths([path], changed_files=changed_files, tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_UNRESOLVED
    assert client.calls == 0  # rejected before the LLM verifier ever ran
    assert "not one of the diff's actually-changed files" in result.paths[0].unresolved_suffix[0].rejection_reason


def test_same_method_name_different_package_rejected(repo: Path):
    a = _entity("route", "route.ts")
    wrong = _entity("getUser", "wrong_module/UserController.java", qualified_name="wrong.pkg.UserController.getUser")
    edge = _edge(a, wrong, evidence=[{"file": "wrong_module/UserController.java", "line_start": 2, "line_end": 3, "fact": "getUser method"}])
    path = _path("GET /x", "getUser", [a, wrong], [edge])
    changed_files = frozenset({"correct_module/UserController.java"})
    result = verify_paths(
        [path], changed_files=changed_files, tools=RepoTools(repo),
        client=ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]}), stats=_stats(),
    )
    assert result.status == STATUS_UNRESOLVED


def test_exact_file_qualified_entity_accepted_when_evidence_supports_it(repo: Path):
    route = _entity("route", "route.ts")
    correct_controller = _entity("UserController.getUser", "correct_module/UserController.java", qualified_name="correct.pkg.UserController")
    edge = _edge(
        route, correct_controller,
        evidence=[{"file": "correct_module/UserController.java", "line_start": 2, "line_end": 3, "fact": "UserController.getUser handles the route"}],
    )
    path = _path("GET /user/{id}", "UserController.getUser", [route, correct_controller], [edge])
    changed_files = frozenset({"correct_module/UserController.java"})
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "evidence at the right file"}]})
    result = verify_paths([path], changed_files=changed_files, tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_ESTABLISHED
    assert client.calls == 1


def test_ambiguous_entity_with_no_resolved_file_can_never_establish(repo: Path):
    route = _entity("route", "route.ts")
    ambiguous = _entity("UserController.getUser", "")  # Stage B could not resolve which file
    edge = _edge(route, ambiguous, evidence=[{"file": "correct_module/UserController.java", "line_start": 2, "line_end": 3, "fact": "handles the route"}])
    path = _path("GET /x", "UserController.getUser", [route, ambiguous], [edge])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_UNRESOLVED
    assert client.calls == 0
    assert "ambiguous identity" in result.paths[0].unresolved_suffix[0].rejection_reason


def test_changed_target_identity_preserved_end_to_end_for_non_target_edges(repo: Path):
    """Only the edge reaching `target_node` is held to the changed-files
    check -- an intermediate hop earlier in the chain is not itself the
    changed behavior and is judged on ordinary identity/evidence grounds
    only."""
    route = _entity("route", "route.ts")
    mid = _entity("Service", "correct_module/UserController.java")  # any real file, not necessarily a changed one
    target = _entity("UserController.getUser", "correct_module/UserController.java")
    e1 = _edge(route, mid, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "route registers handler"}])
    e2 = _edge(mid, target, evidence=[{"file": "correct_module/UserController.java", "line_start": 3, "line_end": 3, "fact": "calls getUser"}])
    path = _path("GET /x", "UserController.getUser", [route, mid, target], [e1, e2])
    changed_files = frozenset({"correct_module/UserController.java"})
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "ok"}, {"index": 1, "accept": True, "reason": "ok"}]})
    result = verify_paths([path], changed_files=changed_files, tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_ESTABLISHED


def test_no_changed_files_known_skips_the_changed_target_check(repo: Path):
    """When the caller has no changed-files list at all (e.g. a test
    fixture that doesn't set one), the changed-target check is simply
    skipped rather than rejecting everything -- it is an additional
    guard, not a replacement for the file-match check."""
    route = _entity("route", "route.ts")
    target = _entity("UserController.getUser", "correct_module/UserController.java")
    edge = _edge(route, target, evidence=[{"file": "correct_module/UserController.java", "line_start": 2, "line_end": 3, "fact": "UserController.getUser handles route"}])
    path = _path("GET /x", "UserController.getUser", [route, target], [edge])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "ok"}]})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_ESTABLISHED


# ---------------------------------------------------------------------------
# Layer 1/2 -- pre-existing edge-level adversarial cases, re-verified
# against the EntityRef schema.
# ---------------------------------------------------------------------------


def test_same_module_coregistration_with_no_dispatch_evidence_is_rejected(repo: Path):
    a = _entity("ControllerX", "route.ts")
    b = _entity("HandlerY", "route.ts")
    edge = _edge(a, b, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "ControllerX and HandlerY declared nearby"}])
    path = _path("GET /x", "HandlerY", [a, b], [edge])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": False, "reason": "both symbols merely co-declared, no dispatch shown"}]})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_UNRESOLVED


def test_bad_trailing_edge_after_valid_prefix_preserves_prefix(repo: Path):
    route = _entity("route", "route.ts")
    handler = _entity("handlerFn", "route.ts")
    extra = _entity("doWork", "route.ts")
    e1 = _edge(route, handler, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "route registers handlerFn"}])
    e2 = _edge(handler, extra, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "handlerFn mentions doWork"}])
    path = _path("GET /x", "doWork", [route, handler, extra], [e1, e2])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "ok"}, {"index": 1, "accept": False, "reason": "mention alone does not prove a call"}]})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    result_path = result.paths[0]
    assert result_path.status == STATUS_PARTIAL
    assert [n.symbol for n in result_path.nodes] == ["route", "handlerFn"]


def test_unnecessary_extra_edge_beyond_target_is_dropped_even_if_accepted(repo: Path):
    route = _entity("route", "route.ts")
    handler = _entity("handlerFn", "route.ts")
    extra = _entity("OtherThing", "route.ts")
    e1 = _edge(route, handler, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "route registers handlerFn"}])
    e2 = _edge(handler, extra, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "handlerFn near OtherThing"}])
    path = _path("GET /x", "handlerFn", [route, handler, extra], [e1, e2])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "ok"}, {"index": 1, "accept": True, "reason": "unnecessary padding"}]})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    result_path = result.paths[0]
    assert result_path.status == STATUS_ESTABLISHED
    assert [n.symbol for n in result_path.nodes] == ["route", "handlerFn"]
    assert result_path.unresolved_suffix == []


def test_fabricated_line_range_is_rejected_deterministically(repo: Path):
    route = _entity("route", "route.ts")
    handler = _entity("handlerFn", "route.ts")
    edge = _edge(route, handler, evidence=[{"file": "route.ts", "line_start": 900, "line_end": 901, "fact": "route registers handlerFn"}])
    path = _path("GET /x", "handlerFn", [route, handler], [edge])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_UNRESOLVED
    assert client.calls == 0


def test_verifier_giving_no_verdict_for_an_edge_is_treated_as_rejection(repo: Path):
    route = _entity("route", "route.ts")
    handler = _entity("handlerFn", "route.ts")
    edge = _edge(route, handler, evidence=[{"file": "route.ts", "line_start": 1, "line_end": 1, "fact": "route registers handlerFn"}])
    path = _path("GET /x", "handlerFn", [route, handler], [edge])
    client = ScriptedVerifier({"verdicts": []})
    result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_UNRESOLVED


# ---------------------------------------------------------------------------
# Test recovery -- fully independent verification.
# ---------------------------------------------------------------------------


def test_recovered_test_that_only_imports_target_is_rejected(repo: Path):
    (repo / "spec.ts").write_text("import { handlerFn } from './route';\ndescribe('unrelated', () => {});\n")
    target = _entity("handlerFn", "route.ts")
    test = RecoveredTest(file="spec.ts", test="unrelated suite", covers="handlerFn behavior", target=target,
                          evidence=[{"file": "spec.ts", "line_start": 1, "line_end": 1, "fact": "imports handlerFn"}])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": False, "reason": "import only"}]})
    result = verify_tests([test], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.tests[0].status == TEST_STATUS_REJECTED


def test_recovered_test_that_directly_invokes_changed_behavior_is_accepted(repo: Path):
    (repo / "spec.ts").write_text("import { handlerFn } from './route';\nit('works', () => { expect(handlerFn()).toBe(1); });\n")
    target = _entity("handlerFn", "route.ts")
    test = RecoveredTest(file="spec.ts", test="works", covers="handlerFn behavior", target=target,
                          evidence=[{"file": "spec.ts", "line_start": 2, "line_end": 2, "fact": "calls handlerFn() and asserts"}])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "direct call and assertion"}]})
    result = verify_tests([test], changed_files=frozenset(), tools=RepoTools(repo), client=client, stats=_stats())
    assert result.tests[0].status == TEST_STATUS_ACCEPTED
    assert result.status == STATUS_ESTABLISHED


def test_recovered_test_with_wrong_module_target_is_rejected_deterministically(repo: Path):
    """A test claiming to cover a same-named entity in the wrong module is
    rejected by the identity layer, same as a path edge would be."""
    (repo / "spec.ts").write_text("it('works', () => { expect(getUser()).toBe(1); });\n")
    wrong_target = _entity("getUser", "wrong_module/UserController.java")
    test = RecoveredTest(file="spec.ts", test="works", covers="getUser behavior", target=wrong_target,
                          evidence=[{"file": "wrong_module/UserController.java", "line_start": 2, "line_end": 3, "fact": "getUser method"}])
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    changed_files = frozenset({"correct_module/UserController.java"})
    result = verify_tests([test], changed_files=changed_files, tools=RepoTools(repo), client=client, stats=_stats())
    assert result.status == STATUS_UNRESOLVED
    assert client.calls == 0


def test_path_and_test_verification_are_fully_independent(repo: Path):
    """A path that fails verification must not affect test verification,
    and vice versa -- they are called and scored completely separately."""
    route = _entity("route", "route.ts")
    handler = _entity("handlerFn", "route.ts")
    bad_edge = _edge(route, handler, evidence=[])  # will be rejected (no evidence)
    path = _path("GET /x", "handlerFn", [route, handler], [bad_edge])

    (repo / "spec.ts").write_text("it('works', () => { expect(handlerFn()).toBe(1); });\n")
    target = _entity("handlerFn", "route.ts")
    good_test = RecoveredTest(file="spec.ts", test="works", covers="handlerFn behavior", target=target,
                               evidence=[{"file": "spec.ts", "line_start": 1, "line_end": 1, "fact": "calls handlerFn()"}])

    path_result = verify_paths([path], changed_files=frozenset(), tools=RepoTools(repo), client=ScriptedVerifier(), stats=_stats())
    test_result = verify_tests(
        [good_test], changed_files=frozenset(), tools=RepoTools(repo),
        client=ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "ok"}]}), stats=_stats(),
    )
    assert path_result.status == STATUS_UNRESOLVED
    assert test_result.status == STATUS_ESTABLISHED
