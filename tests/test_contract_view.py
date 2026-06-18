from __future__ import annotations

from sydes.generate.contract_view import build_contract_view


def _express_contract() -> dict:
    return {
        "version": "v1",
        "routes": [
            {
                "method": "POST",
                "path": "/api/v1/home/personal-task",
                "repo": "worklenz",
                "handler": "HomePageController.createPersonalTask",
                "file": "worklenz-backend/src/routes/apis/home-page-api-router.ts",
                "request": {
                    "path_params": {},
                    "query_params": {},
                    "headers": {},
                    "body": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "unknown"},
                            "color_code": {"type": "unknown"},
                        },
                        "required": [],
                        "description": "Inferred request body schema from layered trace evidence.",
                    },
                },
                "responses": {
                    "200": {
                        "status": "200",
                        "description": "ServerResponse wrapper containing returned handler data.",
                        "body": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "unknown"},
                                "name": {"type": "unknown"},
                            },
                            "required": [],
                        },
                        "confidence": "high",
                    }
                },
                "confidence": "medium",
                "notes": [
                    "Handler reads authenticated user context from req.user?.id.",
                    "Handler writes to personal_todo_list.",
                ],
            }
        ],
    }


def _layered_trace_contract() -> dict:
    return {
        "target": {"method": "POST", "path": "/api/v1/home/personal-task"},
        "matched_endpoint": {
            "method": "POST",
            "path": "/api/v1/home/personal-task",
            "repo": "worklenz",
            "handler": "HomePageController.createPersonalTask",
            "file": "worklenz-backend/src/routes/apis/home-page-api-router.ts",
        },
        "summary": "Creates a personal task and returns a wrapped response.",
        "flow": {
            "steps": [
                {
                    "kind": "request_input",
                    "detail": "const result = await db.query(q, [req.body.name, req.body.color_code, req.user?.id]);",
                    "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                    "symbol": "HomePageController.createPersonalTask",
                    "line_start": 64,
                    "evidence": [
                        {
                            "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                            "symbol": "HomePageController.createPersonalTask",
                            "snippet": "const result = await db.query(q, [req.body.name, req.body.color_code, req.user?.id]);",
                        }
                    ],
                },
                {
                    "kind": "database_write",
                    "detail": "INSERT INTO personal_todo_list (name, color_code, user_id, index) ... RETURNING id, name",
                    "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                    "symbol": "HomePageController.createPersonalTask",
                    "evidence": [
                        {
                            "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                            "symbol": "HomePageController.createPersonalTask",
                            "snippet": "INSERT INTO personal_todo_list (name, color_code, user_id, index) ... RETURNING id, name",
                        }
                    ],
                },
                {
                    "kind": "response",
                    "detail": "return res.status(200).send(new ServerResponse(true, data));",
                    "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                    "symbol": "HomePageController.createPersonalTask",
                    "evidence": [
                        {
                            "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                            "symbol": "HomePageController.createPersonalTask",
                            "snippet": "return res.status(200).send(new ServerResponse(true, data));",
                        }
                    ],
                },
            ]
        },
        "layers": [
            {
                "depth": 1,
                "kind": "handler",
                "name": "HomePageController.createPersonalTask",
                "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                "steps": [],
            }
        ],
        "sinks": [
            {
                "kind": "database",
                "operation": "write",
                "name": "INSERT personal_todo_list",
                "evidence": [
                    {
                        "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                        "snippet": "INSERT INTO personal_todo_list ... RETURNING id, name",
                    }
                ],
            }
        ],
    }


def test_contract_view_merges_rich_express_contract() -> None:
    payload = build_contract_view(
        api_contract=_express_contract(),
        trace_result={
            "target": {"method": "POST", "path": "/api/v1/home/personal-task"},
            "matched_endpoint": {
                "method": "POST",
                "path": "/api/v1/home/personal-task",
                "repo": "worklenz",
                "handler": "HomePageController.createPersonalTask",
                "file": "worklenz-backend/src/routes/apis/home-page-api-router.ts",
            },
            "resolved_handlers": [
                {
                    "primary_handler": {
                        "symbol": {"file": "worklenz-backend/src/controllers/home-page-controller.ts"}
                    }
                }
            ],
            "sinks": [{"kind": "database", "name": "INSERT personal_todo_list"}],
        },
        layered_trace_contract=_layered_trace_contract(),
        handler_body_slices={
            "slices": [
                {
                    "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                    "statements": [
                        {"text": "const result = await db.query(q, [req.body.name, req.body.color_code, req.user?.id]);", "line_start": 64},
                        {"text": "const q = `INSERT INTO personal_todo_list (name, color_code, user_id, index) VALUES ($1, $2, $3, 1) RETURNING id, name`;", "line_start": 60},
                        {"text": "return res.status(200).send(new ServerResponse(true, data));", "line_start": 67},
                    ],
                }
            ]
        },
    )

    assert payload["route"]["method"] == "POST"
    assert payload["route"]["path"] == "/api/v1/home/personal-task"
    assert payload["route"]["handler"] == "HomePageController.createPersonalTask"
    assert payload["route"]["handler_file"] == "worklenz-backend/src/controllers/home-page-controller.ts"
    assert {field["name"] for field in payload["request"]["body_fields"]} >= {"name", "color_code"}
    assert any(item["source_expr"] == "req.user?.id" for item in payload["context"])
    assert any(item["status"] == "200" for item in payload["responses"])
    response_200 = next(item for item in payload["responses"] if item["status"] == "200")
    assert {field["name"] for field in response_200["fields"]} >= {"id", "name"}
    assert any(item["target"] == "personal_todo_list" for item in payload["side_effects"])
    assert "layered_trace_contract" in payload["quality"]["used_artifacts"]
    assert "api_contract" in payload["quality"]["used_artifacts"]
    assert all(item["status"] != "201" for item in payload["responses"])


def test_contract_view_scaffold_only_fallback() -> None:
    payload = build_contract_view(
        api_contract={
            "version": "v1",
            "routes": [
                {
                    "method": "POST",
                    "path": "/items",
                    "repo": "api",
                    "file": "app.py",
                    "request": {
                        "path_params": {},
                        "query_params": {},
                        "headers": {},
                        "body": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "description": "Unknown request body shape (basic skeleton).",
                        },
                    },
                    "responses": {
                        "201": {
                            "status": "201",
                            "description": "Default 201 response skeleton.",
                            "body": {"type": "object", "properties": {}, "required": []},
                            "confidence": "low",
                        }
                    },
                    "notes": ["Handler source unavailable; using scaffold-only contract fields."],
                }
            ],
        }
    )
    assert payload["route"]["path"] == "/items"
    assert payload["unknowns"]
    assert payload["quality"]["scaffold_facts"] >= 1


def test_contract_view_excludes_llm_endpoint_mismatch() -> None:
    payload = build_contract_view(
        api_contract=_express_contract(),
        layered_trace_contract=_layered_trace_contract(),
        llm_contract_refinement={
            "ok": False,
            "error": "endpoint_mismatch",
            "parsed_output": {"method": "POST", "path": "/wrong"},
        },
    )
    assert payload["quality"]["llm_rejected"] is True
    assert payload["developer"]["excluded_candidates"]


def test_contract_view_deterministic_status_beats_llm() -> None:
    payload = build_contract_view(
        api_contract=_express_contract(),
        layered_trace_contract=_layered_trace_contract(),
        llm_contract_refinement={
            "ok": True,
            "parsed_output": {
                "method": "POST",
                "path": "/api/v1/home/personal-task",
                "responses": {
                    "201": {
                        "status": "201",
                        "description": "LLM guessed 201",
                        "body": {"type": "object", "properties": {}, "required": []},
                    }
                },
            },
        },
    )
    assert any(item["status"] == "200" for item in payload["responses"])
    assert all(item["status"] != "201" for item in payload["responses"])
