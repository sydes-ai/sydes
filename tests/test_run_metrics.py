"""Run economics are aggregated from the existing LLM/CBM hooks, always on, as metadata."""

from __future__ import annotations

from sydes.cli.verify_change import PRICE_ENV_VAR, run_metrics, run_metrics_line
from sydes.observability import trace
from sydes.verify.models import ChangeSet, ChangeVerificationResult


def test_llm_and_cbm_usage_is_aggregated_by_purpose_and_priced_only_when_configured(monkeypatch) -> None:
    trace.reset_run_usage()
    usage = {"prompt_tokens": 1000, "completion_tokens": 200}
    for stage in ("code_review", "impact_guide", "impact_guide"):
        trace.note_llm_usage(stage, "m", usage, None)
    trace.record_cbm_call(call_id="c", operation="query_graph", arguments={}, duration_ms=1.0,
                          success=True, error=None, result_summary=None, raw_response=None)
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    result.diagnostics.append("structural_enrichment: triggered=[x] cbm_requests=7 (relations:2)")

    monkeypatch.delenv(PRICE_ENV_VAR, raising=False)
    metrics = run_metrics(result, wall_seconds=12.34)
    assert metrics["llm_totals"]["calls"] == 3 and metrics["llm_by_purpose"]["impact_guide"]["calls"] == 2
    assert metrics["llm_totals"]["input_tokens"] == 3000 and metrics["llm_totals"]["output_tokens"] == 600
    assert metrics["cbm"]["calls"] == 1 and metrics["cbm"]["enrichment_requests"] == 7
    assert "estimated_cost_usd" not in metrics
    assert "estimated_cost_usd=unavailable" in run_metrics_line(metrics)

    monkeypatch.setenv(PRICE_ENV_VAR, "2.0,8.0")
    assert run_metrics(result, wall_seconds=1)["estimated_cost_usd"] == round(3000 / 1e6 * 2 + 600 / 1e6 * 8, 4)


def test_provider_clients_count_their_calls_under_the_stage_they_were_created_for(monkeypatch) -> None:
    from sydes.llm.client import LLMRequest, create_default_llm_client

    class _Completions:
        @staticmethod
        def create(**kwargs):
            usage = type("U", (), {"prompt_tokens": 7, "completion_tokens": 3, "completion_tokens_details": None})()
            message = type("M", (), {"content": "ok"})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": usage})()

    class _OpenAI:
        def __init__(self, **_kwargs):
            self.chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr("sydes.llm.client.OpenAI", _OpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("SYDES_TRACE_DIR", raising=False)
    trace.reset_run_usage()
    create_default_llm_client("openai:gpt-x", stage="code_review").generate(LLMRequest(prompt="hi"))
    entry = trace.run_usage()["llm_by_purpose"]["code_review"]
    assert (entry["calls"], entry["input_tokens"], entry["output_tokens"]) == (1, 7, 3)
