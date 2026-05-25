"""Bounded LLM summarization and call-ranking for deterministic trace slices."""

from __future__ import annotations

import json
from typing import Literal

from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    create_default_llm_client,
)

MAX_PROMPT_CHARS = 12_000
MAX_STEPS = 40
MAX_CANDIDATE_CALLS = 20
MAX_SNIPPET_CHARS = 300


def should_run_trace_llm(
    *,
    policy: Literal["auto", "always", "never"],
    step_count: int,
    candidate_call_count: int,
) -> bool:
    """Decide if trace LLM summarizer should run for this trace context."""
    if policy == "never":
        return False
    if policy == "always":
        return True
    return step_count >= 8 or candidate_call_count >= 6


def _trim_text(value: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def build_trace_llm_input(
    *,
    route: dict,
    resolved_handlers: dict | None,
    primary_slice: dict | None,
    layered_trace_expansion: dict | None,
    budgets: dict | None = None,
) -> dict:
    """Build compact evidence-backed input for LLM trace summarization."""
    steps: list[dict] = []
    if isinstance(primary_slice, dict):
        for stmt in primary_slice.get("statements", [])[:MAX_STEPS]:
            if not isinstance(stmt, dict):
                continue
            step_id = f"step-{stmt.get('index')}"
            steps.append(
                {
                    "id": step_id,
                    "kind": stmt.get("kind_hint") or "statement",
                    "signals": list(stmt.get("signals", [])),
                    "file": primary_slice.get("file"),
                    "line_start": stmt.get("line_start"),
                    "line_end": stmt.get("line_end"),
                    "snippet": _trim_text(str(stmt.get("text") or "")),
                }
            )

    candidate_calls: list[dict] = []
    if isinstance(layered_trace_expansion, dict):
        for item in layered_trace_expansion.get("followed_calls", []):
            if isinstance(item, dict):
                candidate_calls.append(
                    {
                        "call": item.get("call"),
                        "resolved_to": item.get("resolved_to"),
                        "importance": item.get("importance"),
                        "file": item.get("file"),
                    }
                )
        for item in layered_trace_expansion.get("unresolved_calls", []):
            if isinstance(item, dict):
                candidate_calls.append(
                    {
                        "call": item.get("call"),
                        "resolved_to": None,
                        "importance": None,
                        "file": None,
                        "reason": item.get("reason"),
                    }
                )
    seen_calls: set[str] = set()
    deduped_calls: list[dict] = []
    for call in candidate_calls:
        name = call.get("call")
        if not isinstance(name, str) or not name.strip():
            continue
        if name in seen_calls:
            continue
        seen_calls.add(name)
        deduped_calls.append(call)
        if len(deduped_calls) >= MAX_CANDIDATE_CALLS:
            break

    matched_endpoint = route.get("matched_endpoint") if isinstance(route, dict) else None
    endpoint = matched_endpoint if isinstance(matched_endpoint, dict) else route
    method = endpoint.get("method") if isinstance(endpoint, dict) else None
    path = endpoint.get("path") if isinstance(endpoint, dict) else None
    route_file = endpoint.get("file") if isinstance(endpoint, dict) else None

    primary_handler = None
    prehandlers: list[str] = []
    if isinstance(resolved_handlers, dict):
        resolution = resolved_handlers.get("resolution", {})
        if isinstance(resolution, dict):
            primary = resolution.get("primary_handler")
            if isinstance(primary, dict):
                primary_handler = primary.get("normalized_handler") or primary.get("handler_hint")
            for item in resolution.get("prehandlers", []):
                if isinstance(item, dict):
                    name = item.get("normalized_handler") or item.get("handler_hint")
                    if isinstance(name, str) and name.strip():
                        prehandlers.append(name)

    return {
        "version": "v1",
        "route": {
            "method": method,
            "path": path,
            "file": route_file,
        },
        "primary_handler": primary_handler,
        "prehandlers": prehandlers[:10],
        "steps": steps,
        "candidate_follow_calls": deduped_calls,
        "budgets": budgets or {},
    }


def build_trace_llm_prompt(input_payload: dict) -> str:
    """Build bounded prompt with strict evidence constraints."""
    payload_json = json.dumps(input_payload, ensure_ascii=True, separators=(",", ":"))
    prompt = (
        "You are assisting API trace summarization.\n"
        "Use ONLY provided evidence.\n"
        "Do NOT invent steps, sinks, files, calls, or side-effects.\n"
        "Every step_summaries item MUST include source_step_ids and evidence_refs referencing existing step ids.\n"
        "Every ranked_follow_calls.call MUST be from candidate_follow_calls.call.\n"
        "Return strict JSON only with schema:\n"
        "{"
        '"version":"v1",'
        '"summary":"...",'
        '"step_summaries":[{"source_step_ids":["step-1"],"name":"...","kind":"...","detail":"...","evidence_refs":["step-1"],"confidence":0.9}],'
        '"ranked_follow_calls":[{"call":"uploadBase64","reason":"...","priority":"high|medium|low","should_follow":true}],'
        '"risks":["..."]'
        "}\n"
        "Input:\n"
        f"{payload_json}"
    )
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    overflow = len(prompt) - MAX_PROMPT_CHARS
    # Trim candidate calls first to preserve core step evidence.
    reduced = dict(input_payload)
    calls = list(reduced.get("candidate_follow_calls", []))
    if overflow > 0 and len(calls) > 8:
        reduced["candidate_follow_calls"] = calls[:8]
    payload_json = json.dumps(reduced, ensure_ascii=True, separators=(",", ":"))
    prompt = prompt.split("Input:\n", 1)[0] + "Input:\n" + payload_json
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    # Final cap: trim steps.
    reduced_steps = list(reduced.get("steps", []))
    reduced["steps"] = reduced_steps[:20]
    payload_json = json.dumps(reduced, ensure_ascii=True, separators=(",", ":"))
    prompt = prompt.split("Input:\n", 1)[0] + "Input:\n" + payload_json
    return prompt[:MAX_PROMPT_CHARS]


def _validate_trace_llm_output(raw: dict, input_payload: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    step_ids = {item["id"] for item in input_payload.get("steps", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    candidate_calls = {
        item.get("call")
        for item in input_payload.get("candidate_follow_calls", [])
        if isinstance(item, dict) and isinstance(item.get("call"), str)
    }

    summary = raw.get("summary")
    if not isinstance(summary, str):
        summary = ""

    valid_step_summaries: list[dict] = []
    for item in raw.get("step_summaries", []):
        if not isinstance(item, dict):
            continue
        src_ids = item.get("source_step_ids")
        refs = item.get("evidence_refs")
        if not isinstance(src_ids, list) or not src_ids:
            warnings.append("Rejected LLM step summary missing source_step_ids.")
            continue
        if not isinstance(refs, list) or not refs:
            warnings.append("Rejected LLM step summary missing evidence_refs.")
            continue
        normalized_src = [sid for sid in src_ids if isinstance(sid, str)]
        normalized_refs = [ref for ref in refs if isinstance(ref, str)]
        if not normalized_src or any(sid not in step_ids for sid in normalized_src):
            warnings.append("Rejected LLM step summary with unknown source_step_ids.")
            continue
        if any(ref not in step_ids for ref in normalized_refs):
            warnings.append("Rejected LLM step summary with unknown evidence_refs.")
            continue
        valid_step_summaries.append(
            {
                "source_step_ids": normalized_src,
                "name": str(item.get("name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "detail": str(item.get("detail") or "").strip(),
                "evidence_refs": normalized_refs,
                "confidence": float(item.get("confidence") or 0.0),
            }
        )

    valid_ranked_calls: list[dict] = []
    for item in raw.get("ranked_follow_calls", []):
        if not isinstance(item, dict):
            continue
        call = item.get("call")
        if not isinstance(call, str) or call not in candidate_calls:
            warnings.append("Rejected LLM ranked call not present in deterministic candidates.")
            continue
        priority = str(item.get("priority") or "low").lower()
        if priority not in {"high", "medium", "low"}:
            priority = "low"
        valid_ranked_calls.append(
            {
                "call": call,
                "reason": str(item.get("reason") or "").strip(),
                "priority": priority,
                "should_follow": bool(item.get("should_follow")),
            }
        )

    risks = [str(item).strip() for item in raw.get("risks", []) if isinstance(item, str)]
    return (
        {
            "version": "v1",
            "summary": summary.strip(),
            "step_summaries": valid_step_summaries,
            "ranked_follow_calls": valid_ranked_calls,
            "risks": risks[:8],
        },
        warnings,
    )


def run_trace_llm_summarizer(
    *,
    model_spec: str | None,
    route: dict,
    resolved_handlers: dict | None,
    primary_slice: dict | None,
    layered_trace_expansion: dict | None,
    policy: Literal["auto", "always", "never"] = "auto",
    llm_client: LLMClient | None = None,
    budgets: dict | None = None,
) -> dict:
    """Run bounded trace LLM summarization and candidate-call ranking."""
    input_payload = build_trace_llm_input(
        route=route,
        resolved_handlers=resolved_handlers,
        primary_slice=primary_slice,
        layered_trace_expansion=layered_trace_expansion,
        budgets=budgets,
    )
    step_count = len(input_payload.get("steps", []))
    candidate_count = len(input_payload.get("candidate_follow_calls", []))
    should_run = should_run_trace_llm(
        policy=policy,
        step_count=step_count,
        candidate_call_count=candidate_count,
    )
    if not should_run:
        return {
            "version": "v1",
            "policy": policy,
            "skipped": True,
            "reason": "policy_skip_or_simple_trace",
            "input_summary": {"steps": step_count, "candidate_calls": candidate_count},
            "result": None,
            "warnings": [],
        }

    prompt = build_trace_llm_prompt(input_payload)
    if llm_client is None:
        llm_client = create_default_llm_client(model_spec=model_spec)

    response = llm_client.generate(LLMRequest(prompt=prompt, temperature=0))
    try:
        raw = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise LLMClientError("model output parse failure: trace summary output was not valid JSON.") from exc
    if not isinstance(raw, dict):
        raise LLMClientError("model output parse failure: trace summary output was not a JSON object.")

    validated, warnings = _validate_trace_llm_output(raw, input_payload)
    return {
        "version": "v1",
        "policy": policy,
        "skipped": False,
        "reason": None,
        "input_summary": {"steps": step_count, "candidate_calls": candidate_count, "prompt_chars": len(prompt)},
        "result": validated,
        "warnings": warnings,
    }

