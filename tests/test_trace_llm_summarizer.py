"""Tests for bounded trace LLM summarizer and ranked follow-call validation."""

from __future__ import annotations

import json

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.trace.trace_llm_summarizer import (
    MAX_PROMPT_CHARS,
    build_trace_llm_input,
    build_trace_llm_prompt,
    run_trace_llm_summarizer,
)


class _FakeClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self.payload)


def _sample_primary_slice() -> dict:
    return {
        "file": "src/controller.ts",
        "statements": [
            {
                "index": 1,
                "line_start": 10,
                "line_end": 10,
                "kind_hint": "assignment",
                "text": "const { file, task_id } = req.body;",
                "signals": ["request_body_read"],
            },
            {
                "index": 2,
                "line_start": 11,
                "line_end": 11,
                "kind_hint": "await_call",
                "text": "const url = await uploadBase64(file, key);",
                "signals": ["await_call", "possible_external_call"],
            },
            {
                "index": 3,
                "line_start": 12,
                "line_end": 12,
                "kind_hint": "return",
                "text": "return res.status(200).send(data);",
                "signals": ["response_return"],
            },
        ],
    }


def _sample_layered() -> dict:
    return {
        "followed_calls": [
            {"call": "uploadBase64", "resolved_to": "uploadBase64", "importance": 5, "file": "src/storage.ts"},
            {"call": "humanFileSize", "resolved_to": "humanFileSize", "importance": 0, "file": "src/format.ts"},
        ],
        "unresolved_calls": [{"call": "unknownHelper", "reason": "not_found"}],
    }


def test_build_trace_llm_input_is_compact_and_excludes_full_file_contents() -> None:
    input_payload = build_trace_llm_input(
        route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice={
            "file": "src/controller.ts",
            "statements": [
                {"index": 1, "line_start": 1, "line_end": 1, "kind_hint": "assignment", "text": "x" * 2000, "signals": []}
            ],
        },
        layered_trace_expansion=_sample_layered(),
    )
    assert input_payload["steps"][0]["snippet"] != "x" * 2000
    assert len(input_payload["steps"][0]["snippet"]) <= 300


def test_build_trace_llm_prompt_respects_size_cap() -> None:
    big_steps = []
    for i in range(80):
        big_steps.append(
            {"index": i + 1, "line_start": i + 1, "line_end": i + 1, "kind_hint": "statement", "text": "very long snippet " * 50, "signals": []}
        )
    payload = build_trace_llm_input(
        route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice={"file": "src/controller.ts", "statements": big_steps},
        layered_trace_expansion=_sample_layered(),
    )
    prompt = build_trace_llm_prompt(payload)
    assert len(prompt) <= MAX_PROMPT_CHARS


def test_valid_llm_json_is_accepted() -> None:
    client = _FakeClient(
        json.dumps(
            {
                "version": "v1",
                "summary": "Writes metadata and returns a response.",
                "step_summaries": [
                    {
                        "source_step_ids": ["step-1"],
                        "name": "read request body",
                        "kind": "input",
                        "detail": "Reads req.body",
                        "evidence_refs": ["step-1"],
                        "confidence": 0.95,
                    }
                ],
                "ranked_follow_calls": [
                    {
                        "call": "uploadBase64",
                        "reason": "storage side effect",
                        "priority": "high",
                        "should_follow": True,
                    }
                ],
                "risks": ["dynamic runtime branches may be missed"],
            }
        )
    )
    out = run_trace_llm_summarizer(
        model_spec=None,
        route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice=_sample_primary_slice(),
        layered_trace_expansion=_sample_layered(),
        policy="always",
        llm_client=client,
    )
    assert out["skipped"] is False
    assert out["result"]["summary"]
    assert out["result"]["ranked_follow_calls"][0]["call"] == "uploadBase64"


def test_malformed_llm_output_fails_gracefully() -> None:
    client = _FakeClient("{not-json")
    with pytest.raises(LLMClientError, match="trace summary output was not valid JSON"):
        run_trace_llm_summarizer(
            model_spec=None,
            route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
            resolved_handlers=None,
            primary_slice=_sample_primary_slice(),
            layered_trace_expansion=_sample_layered(),
            policy="always",
            llm_client=client,
        )


def test_step_without_evidence_refs_is_rejected() -> None:
    client = _FakeClient(
        json.dumps(
            {
                "summary": "x",
                "step_summaries": [
                    {"source_step_ids": ["step-1"], "name": "bad", "kind": "x", "detail": "x", "evidence_refs": []}
                ],
                "ranked_follow_calls": [],
                "risks": [],
            }
        )
    )
    out = run_trace_llm_summarizer(
        model_spec=None,
        route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice=_sample_primary_slice(),
        layered_trace_expansion=_sample_layered(),
        policy="always",
        llm_client=client,
    )
    assert out["result"]["step_summaries"] == []
    assert any("missing evidence_refs" in warning for warning in out["warnings"])


def test_invented_ranked_call_is_rejected() -> None:
    client = _FakeClient(
        json.dumps(
            {
                "summary": "x",
                "step_summaries": [],
                "ranked_follow_calls": [
                    {"call": "madeUpCall", "reason": "invented", "priority": "high", "should_follow": True}
                ],
                "risks": [],
            }
        )
    )
    out = run_trace_llm_summarizer(
        model_spec=None,
        route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice=_sample_primary_slice(),
        layered_trace_expansion=_sample_layered(),
        policy="always",
        llm_client=client,
    )
    assert out["result"]["ranked_follow_calls"] == []
    assert any("not present in deterministic candidates" in warning for warning in out["warnings"])


def test_trace_llm_policy_never_does_not_call_llm(monkeypatch) -> None:
    called = {"count": 0}

    def _should_not_create(*_args, **_kwargs):
        called["count"] += 1
        raise AssertionError("LLM client should not be created in policy=never")

    monkeypatch.setattr(
        "sydes.trace.trace_llm_summarizer.create_default_llm_client",
        _should_not_create,
    )
    out = run_trace_llm_summarizer(
        model_spec=None,
        route={"matched_endpoint": {"method": "POST", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice=_sample_primary_slice(),
        layered_trace_expansion=_sample_layered(),
        policy="never",
    )
    assert out["skipped"] is True
    assert called["count"] == 0


def test_trace_llm_policy_auto_skips_small_trace() -> None:
    client = _FakeClient("{}")
    out = run_trace_llm_summarizer(
        model_spec=None,
        route={"matched_endpoint": {"method": "GET", "path": "/x", "file": "src/routes.ts"}},
        resolved_handlers=None,
        primary_slice={"file": "src/controller.ts", "statements": []},
        layered_trace_expansion={"followed_calls": []},
        policy="auto",
        llm_client=client,
    )
    assert out["skipped"] is True
    assert client.calls == 0

