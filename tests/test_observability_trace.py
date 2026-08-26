"""Passive, opt-in evaluation tracing (`SYDES_TRACE_DIR`).

Pins the contract the tracing layer must hold: disabled by default with
zero behavioral or output difference, best-effort (a broken trace directory
must never break a real analysis), no new LLM/CBM calls, and — when
enabled — the exact structured artifacts an external harness needs to
reconstruct what happened, including *why* a candidate was rejected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.code_intelligence.cbm_client import ClientMetrics, StdioMCPSession
from sydes.impact.models import ImpactResult
from sydes.llm.client import (
    LLMClientError,
    LLMRequest,
    LLMResponse,
    TracingLLMClient,
    create_default_llm_client,
)
from sydes.observability import trace
from sydes.verify.analyzer import _trace_impact_decisions, _trace_verification_decisions
from sydes.verify.models import AcceptedImpact, AffectedFlow


# --------------------------------------------------------------------------
# Core writer contract
# --------------------------------------------------------------------------


def test_disabled_by_default_and_is_a_complete_noop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SYDES_TRACE_DIR", raising=False)
    assert trace.is_enabled() is False

    trace.start_run(run_id="r1", options={"a": 1}, repos=[])
    trace.record_llm_call(
        call_id="c1", stage="x", provider="openai", model="gpt", request=LLMRequest(prompt="p"),
        response_text="hi", error=None, latency_ms=1.0,
    )
    trace.record_cbm_call(
        call_id="c2", operation="search_graph", arguments={}, duration_ms=1.0,
        success=True, error=None, result_summary={}, raw_response={},
    )
    trace.record_final_decision(
        run_id="r1", risk="low", verdict="verified", headline="h", counts={}, reasons=[],
    )

    # Nothing was ever created anywhere — in particular, no directory named
    # after any path we might plausibly have used was created under tmp_path.
    assert list(tmp_path.iterdir()) == []


def test_enabling_creates_the_directory_only_when_a_call_actually_happens(
    monkeypatch, tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("SYDES_TRACE_DIR", str(trace_dir))
    assert trace.is_enabled() is True
    assert not trace_dir.exists()

    trace.start_run(run_id="r1", options={"base": "main"}, repos=[{"name": "api", "root": "/x"}])

    assert trace_dir.exists()
    run_json = json.loads((trace_dir / "run.json").read_text())
    assert run_json["run_id"] == "r1"
    assert run_json["options"] == {"base": "main"}
    assert run_json["final_decision"] is None
    # Every category file is truncated fresh at run start.
    for filename in (
        "llm_calls.jsonl", "cbm_calls.jsonl", "impact_decisions.jsonl",
        "verification_decisions.jsonl", "test_decisions.jsonl",
    ):
        assert (trace_dir / filename).exists()
        assert (trace_dir / filename).read_text() == ""


def test_final_decision_is_merged_into_the_same_run_json(monkeypatch, tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("SYDES_TRACE_DIR", str(trace_dir))
    trace.start_run(run_id="r1", options={}, repos=[])

    trace.record_final_decision(
        run_id="r1", risk="high", verdict="action_required", headline="2 failed",
        counts={"obligations": 2}, reasons=["1 obligation(s) failed an executed test"],
    )

    run_json = json.loads((trace_dir / "run.json").read_text())
    assert run_json["run_id"] == "r1"
    assert run_json["final_decision"] == {
        "risk": "high", "verdict": "action_required", "headline": "2 failed",
        "counts": {"obligations": 2},
        "reasons": ["1 obligation(s) failed an executed test"],
    }
    assert run_json["ended_at"] is not None


def test_sanitization_never_raises_on_an_unserializable_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))

    class Unserializable:
        def __repr__(self) -> str:
            return "<Unserializable>"

    trace.record_impact_decision(
        changed_symbol="s", candidate_label="c", kind="k", source="llm", status="inferred",
        accepted=True, rejection_reason="", corroborated=None, confidence=0.5,
        reason="r", evidence={"weird": Unserializable()},
    )
    lines = (tmp_path / "traces" / "impact_decisions.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "Unserializable" in record["evidence"]["weird"]


def test_trace_writer_failure_never_raises(monkeypatch, tmp_path: Path) -> None:
    """Point tracing at a location writing cannot succeed against (a file,
    not a directory) — every writer must swallow the failure silently."""
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("i am a file")
    monkeypatch.setenv("SYDES_TRACE_DIR", str(blocked))

    trace.start_run(run_id="r1", options={}, repos=[])
    trace.record_llm_call(
        call_id="c1", stage="x", provider="p", model="m", request=LLMRequest(prompt="p"),
        response_text="t", error=None, latency_ms=1.0,
    )
    trace.record_cbm_call(
        call_id="c2", operation="op", arguments={}, duration_ms=1.0,
        success=False, error="boom", result_summary=None, raw_response=None,
    )
    trace.record_impact_decision(
        changed_symbol="s", candidate_label="c", kind="", source="llm", status="rejected",
        accepted=False, rejection_reason="self_referential", corroborated=False,
        confidence=0.1, reason="r", evidence=None,
    )
    trace.record_verification_decision(
        impact_id="i", label="l", status="proven", verification_model_status="modeled",
        reason="r", obligations=1,
    )
    trace.record_test_decision(
        flow_id="f", obligation_id="o", obligation_description="d",
        mapped_tests=[], supporting_tests=[], status="unverified", reason="none",
    )
    trace.record_final_decision(
        run_id="r1", risk="low", verdict="ok", headline="h", counts={}, reasons=[],
    )
    # No exception reached here — every writer above swallowed the failure.


# --------------------------------------------------------------------------
# B. LLM call tracing
# --------------------------------------------------------------------------


class _CountingLLMClient:
    def __init__(self, text: str = "ok") -> None:
        self.calls = 0
        self._text = text

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self._text)


class _FailingLLMClient:
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("provider unreachable")


def test_tracing_llm_client_adds_no_calls_and_returns_the_same_response(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    inner = _CountingLLMClient("hello world")
    wrapped = TracingLLMClient(inner, stage="impact_guide", provider="openai", model="gpt-5.5")

    response = wrapped.generate(LLMRequest(prompt="do the thing", system="be helpful"))

    assert response.text == "hello world"
    assert inner.calls == 1  # exactly the one call the caller asked for

    lines = (tmp_path / "traces" / "llm_calls.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["stage"] == "impact_guide"
    assert record["provider"] == "openai"
    assert record["model"] == "gpt-5.5"
    assert record["success"] is True
    assert record["error"] is None
    assert record["latency_ms"] >= 0
    assert record["raw_path"] == f"raw/llm/{record['call_id']}.json"

    raw = json.loads((tmp_path / "traces" / record["raw_path"]).read_text())
    assert raw["request"]["prompt"] == "do the thing"
    assert raw["request"]["system"] == "be helpful"
    assert raw["response_text"] == "hello world"


def test_tracing_llm_client_records_and_reraises_provider_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    wrapped = TracingLLMClient(_FailingLLMClient(), stage="code_review", provider="openai", model="gpt-5.5")

    with pytest.raises(LLMClientError, match="provider unreachable"):
        wrapped.generate(LLMRequest(prompt="p"))

    lines = (tmp_path / "traces" / "llm_calls.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["success"] is False
    assert "provider unreachable" in record["error"]


def test_create_default_llm_client_is_unwrapped_when_tracing_is_disabled(monkeypatch) -> None:
    monkeypatch.delenv("SYDES_TRACE_DIR", raising=False)
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SYDES_LLM_MODEL", "llama3.1:8b")

    client = create_default_llm_client(stage="impact_guide")
    assert not isinstance(client, TracingLLMClient)


def test_create_default_llm_client_wraps_when_tracing_is_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SYDES_LLM_MODEL", "llama3.1:8b")

    client = create_default_llm_client(stage="impact_guide")
    assert isinstance(client, TracingLLMClient)


# --------------------------------------------------------------------------
# C. CBM call tracing
# --------------------------------------------------------------------------


def _bare_session() -> StdioMCPSession:
    """A `StdioMCPSession` with no subprocess spawned — only the attributes
    `call_tool` actually touches are set, and `_request` is monkeypatched by
    each test. Avoids needing a real CBM daemon to test the tracing wrapped
    around the transport's own `call_tool`."""
    session = object.__new__(StdioMCPSession)
    session.metrics = ClientMetrics()
    return session


def test_cbm_call_tool_records_operation_arguments_and_success(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    session = _bare_session()
    session._request = lambda method, params, *, timeout: {
        "structuredContent": {"rows": [["a", "b"], ["c", "d"]]},
    }

    result = session.call_tool("search_graph", {"project": "app", "limit": 10})

    assert result == {"rows": [["a", "b"], ["c", "d"]]}
    lines = (tmp_path / "traces" / "cbm_calls.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["operation"] == "search_graph"
    assert record["arguments"] == {"project": "app", "limit": 10}
    assert record["success"] is True
    assert record["error"] is None
    assert record["result_summary"]["row_count"] == 2
    raw = json.loads((tmp_path / "traces" / record["raw_path"]).read_text())
    assert raw["structuredContent"]["rows"] == [["a", "b"], ["c", "d"]]


def test_cbm_call_tool_records_tool_reported_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    session = _bare_session()
    session._request = lambda method, params, *, timeout: {
        "isError": True, "content": [{"type": "text", "text": "boom"}],
    }

    with pytest.raises(CodeIntelligenceError):
        session.call_tool("get_architecture", {"project": "app"})

    lines = (tmp_path / "traces" / "cbm_calls.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["operation"] == "get_architecture"
    assert record["success"] is False
    assert "boom" in record["error"]


def test_cbm_call_tool_records_transport_level_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    session = _bare_session()

    def _boom(method: str, params: dict, *, timeout: float) -> dict[str, Any]:
        raise CodeIntelligenceError("connection closed")

    session._request = _boom

    with pytest.raises(CodeIntelligenceError, match="connection closed"):
        session.call_tool("index_status", {"project": "app"})

    lines = (tmp_path / "traces" / "cbm_calls.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["success"] is False
    assert "connection closed" in record["error"]


def test_cbm_call_tool_behavior_is_identical_whether_tracing_is_on_or_off(monkeypatch, tmp_path: Path) -> None:
    """The exact requirement: tracing must never change what the caller
    gets back, success or failure."""
    def make_session() -> StdioMCPSession:
        session = _bare_session()
        session._request = lambda method, params, *, timeout: {
            "structuredContent": {"project": "app"},
        }
        return session

    monkeypatch.delenv("SYDES_TRACE_DIR", raising=False)
    result_off = make_session().call_tool("index_status", {"project": "app"})

    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    result_on = make_session().call_tool("index_status", {"project": "app"})

    assert result_off == result_on == {"project": "app"}


# --------------------------------------------------------------------------
# D. Impact decisions — rejection reasons must be explicit, not inferred
# --------------------------------------------------------------------------


def test_rejected_candidate_has_an_explicit_rejection_reason_event(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    result = ImpactResult(
        llm_candidate_log=[
            {
                "changed_symbol": "set_expires", "turn": 1,
                "candidate_entrypoint": "set_expires", "candidate_symbol": "set_expires",
                "confidence": 0.6, "rationale": "restates itself",
                "inference_type": "", "uncertainty": "",
                "corroborated": False, "corroboration_evidence": "",
                "accepted": False,
                "rejection_reason": (
                    "self_referential: candidate restates the changed symbol itself "
                    "rather than naming a downstream affected behavior"
                ),
            },
        ],
    )

    _trace_impact_decisions(result)

    lines = (tmp_path / "traces" / "impact_decisions.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["source"] == "llm"
    assert record["accepted"] is False
    assert record["rejection_reason"].startswith("self_referential")
    assert record["status"] == "rejected"


def test_accepted_candidate_and_proven_entry_are_both_traced_distinctly(monkeypatch, tmp_path: Path) -> None:
    from sydes.impact.models import AffectedEntrypoint, IMPACT_STATUS_PROVEN

    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    proven = AffectedEntrypoint(
        repo="app", symbol="handler", qualified_name="app.handler", file="app/views.py",
        route_method="GET", route_path="/x", changed_symbols=["helper"],
        status=IMPACT_STATUS_PROVEN,
    )
    result = ImpactResult(
        affected=[proven],
        llm_candidate_log=[
            {
                "changed_symbol": "apply_never_cache", "turn": 1,
                "candidate_entrypoint": "GET /y", "candidate_symbol": "other_handler",
                "confidence": 0.7, "rationale": "plausible", "inference_type": "",
                "uncertainty": "", "corroborated": True, "corroboration_evidence": "matched",
                "accepted": True, "rejection_reason": "",
            },
        ],
    )

    _trace_impact_decisions(result)

    lines = (tmp_path / "traces" / "impact_decisions.jsonl").read_text().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    sources = {record["source"] for record in records}
    assert sources == {"deterministic", "llm"}
    deterministic = next(r for r in records if r["source"] == "deterministic")
    llm = next(r for r in records if r["source"] == "llm")
    assert deterministic["status"] == IMPACT_STATUS_PROVEN
    assert deterministic["accepted"] is True
    assert llm["accepted"] is True
    assert llm["status"] == "inferred"


# --------------------------------------------------------------------------
# E. Verification-modeling decisions
# --------------------------------------------------------------------------


def test_unsupported_or_partial_impact_has_an_explicit_reason(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    impact = AcceptedImpact(
        id="impact:app:helper", label="some behavior", repo="app", kind="unknown",
        status="inferred", changed_symbols=["helper"],
        verification_model_status="unsupported_or_partial",
    )

    _trace_verification_decisions([impact], affected_flows=[])

    lines = (tmp_path / "traces" / "verification_decisions.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["verification_model_status"] == "unsupported_or_partial"
    assert record["reason"]
    assert record["obligations"] == 0


def test_modeled_impact_reports_its_obligation_count(monkeypatch, tmp_path: Path) -> None:
    from sydes.verify.models import VerificationObligation

    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    flow = AffectedFlow(
        id="flow:GET:/x", entry_label="GET /x", repo="app", method="GET", path="/x",
        obligations=[
            VerificationObligation(
                id="ob1", flow_id="flow:GET:/x", kind="response", statement="returns 200",
                origin="contract",
            ),
        ],
    )
    impact = AcceptedImpact(
        id="flow:GET:/x", label="GET /x", repo="app", kind="http_route", status="proven",
        changed_symbols=["handler"], route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )

    _trace_verification_decisions([impact], affected_flows=[flow])

    lines = (tmp_path / "traces" / "verification_decisions.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["verification_model_status"] == "modeled"
    assert record["obligations"] == 1
