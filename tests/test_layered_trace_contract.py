from __future__ import annotations

from sydes.trace.layered_contract import build_layered_trace_contract, canonical_step_kind


def test_canonical_step_kind_normalization() -> None:
    assert canonical_step_kind("db_write") == "database_write"
    assert canonical_step_kind("external_api") == "external_call"
    assert canonical_step_kind("assignment", ["request_body_read"], "const x = req.body") == "request_input"
    assert canonical_step_kind("statement", ["possible_external_call"], "await uploadBase64(file)") == "storage_call"


def test_layered_contract_contains_required_step_fields() -> None:
    payload = build_layered_trace_contract(
        matched_endpoint={"repo": "api", "method": "POST", "path": "/items", "file": "src/routes.ts", "handler": "Controller.create"},
        primary_slice={
            "file": "src/controller.ts",
            "statements": [
                {
                    "index": 1,
                    "kind_hint": "assignment",
                    "signals": ["request_body_read"],
                    "text": "const data = req.body;",
                    "line_start": 12,
                    "line_end": 12,
                    "confidence": 0.9,
                }
            ],
        },
        resolved_handlers={"resolution": {"primary_handler": {"normalized_handler": "Controller.create", "symbol": {"file": "src/controller.ts", "line": 10}}, "prehandlers": []}},
        layered_trace_expansion=None,
        llm_summary=None,
        budgets={"max_depth": 2, "max_steps": 40},
        artifact_paths={"trace_result": "/tmp/trace_result.json"},
    )
    assert "flow" in payload and payload["flow"]["steps"]
    step = payload["flow"]["steps"][0]
    for key in ("id", "kind", "name", "repo", "evidence", "confidence", "status"):
        assert key in step


def test_layered_contract_generates_deterministic_summary_without_llm() -> None:
    payload = build_layered_trace_contract(
        matched_endpoint={"repo": "api", "method": "POST", "path": "/items", "file": "src/routes.ts", "handler": "Controller.create"},
        primary_slice={
            "file": "src/controller.ts",
            "statements": [
                {"index": 1, "kind_hint": "assignment", "signals": ["request_body_read"], "text": "const data = req.body;"},
                {"index": 2, "kind_hint": "statement", "signals": ["possible_db_call"], "text": "await db.query(q, []);"},
                {"index": 3, "kind_hint": "return", "signals": ["response_return"], "text": "return res.json(data);"},
            ],
        },
        resolved_handlers=None,
        layered_trace_expansion=None,
        llm_summary=None,
        budgets={"max_depth": 2, "max_steps": 40},
        artifact_paths={},
    )
    assert isinstance(payload["summary"], str) and payload["summary"]
    assert "response" in payload["summary"]


def test_layered_contract_sink_normalization_database_and_storage() -> None:
    payload = build_layered_trace_contract(
        matched_endpoint={"repo": "api", "method": "POST", "path": "/x", "file": "src/routes.ts", "handler": "Controller.create"},
        primary_slice={
            "file": "src/controller.ts",
            "statements": [
                {"index": 1, "kind_hint": "statement", "signals": ["possible_db_call"], "text": "await db.query('INSERT INTO x', []);"},
                {"index": 2, "kind_hint": "statement", "signals": ["possible_external_call"], "text": "await uploadBase64(file, key);"},
            ],
        },
        resolved_handlers=None,
        layered_trace_expansion=None,
        llm_summary=None,
        budgets={"max_depth": 2, "max_steps": 40},
        artifact_paths={},
    )
    kinds = {item["kind"] for item in payload["sinks"]}
    assert "database" in kinds
    assert "storage" in kinds

