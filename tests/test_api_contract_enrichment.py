from __future__ import annotations

from sydes.core.models import (
    ApiContractArtifact,
    ApiRequestContract,
    ApiResponseContract,
    ApiRouteContract,
    ApiSchema,
)
from sydes.generate.contracts import enrich_api_contract_from_layered_trace


def test_enrich_api_contract_from_layered_trace_updates_express_route() -> None:
    contract = ApiContractArtifact(
        routes=[
            ApiRouteContract(
                method="POST",
                path="/api/v1/home/personal-task",
                repo="worklenz",
                handler="HomePageController.createPersonalTask",
                file="worklenz-backend/src/routes/apis/home-page-api-router.ts",
                request=ApiRequestContract(
                    body=ApiSchema(
                        type="object",
                        required=[],
                        properties={},
                        additional_properties=True,
                        description="Unknown request body shape (basic skeleton).",
                    )
                ),
                responses={
                    "201": ApiResponseContract(
                        status="201",
                        description="Default 201 response skeleton.",
                        body=ApiSchema(
                            type="object",
                            required=[],
                            properties={},
                            additional_properties=True,
                            description="Unknown response body shape (basic skeleton).",
                        ),
                        confidence="low",
                    )
                },
                notes=[
                    "No concrete request contract fields inferred from handler source; using scaffold defaults.",
                    "Could not parse handler source for response inference; using scaffold defaults.",
                ],
            )
        ]
    )
    handler_body_slices = {
        "slices": [
            {
                "handler": "HomePageController.createPersonalTask",
                "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                "statements": [
                    {
                        "index": 1,
                        "text": "const result = await db.query(q, [req.body.name, req.body.color_code, req.user?.id]);",
                    },
                    {
                        "index": 2,
                        "text": "return res.status(200).send(new ServerResponse(true, data));",
                    },
                    {
                        "index": 3,
                        "text": "const q = `INSERT INTO personal_todo_list (name, color_code, user_id, index) VALUES ($1, $2, $3, 1) RETURNING id, name`;",
                    },
                ],
            }
        ]
    }
    layered_trace_contract = {
        "target": {"method": "POST", "path": "/api/v1/home/personal-task"},
        "flow": {
            "steps": [
                {"kind": "request_input", "detail": "req.body.name"},
                {"kind": "request_input", "detail": "req.body.color_code"},
                {"kind": "response", "detail": "return res.status(200).send(new ServerResponse(true, data));"},
            ]
        },
        "sinks": [
            {
                "kind": "database",
                "name": "INSERT personal_todo_list",
                "evidence": [{"snippet": "INSERT INTO personal_todo_list ... RETURNING id, name"}],
            }
        ],
    }

    enriched = enrich_api_contract_from_layered_trace(
        contract,
        layered_trace_contract=layered_trace_contract,
        handler_body_slices=handler_body_slices,
        trace_result={"target": {"method": "POST", "path": "/api/v1/home/personal-task"}},
    )

    route = enriched.routes[0]
    assert route.request.body is not None
    assert route.request.body.description == "Inferred request body schema from layered trace evidence."
    assert "name" in route.request.body.properties
    assert "color_code" in route.request.body.properties
    assert "200" in route.responses
    assert "201" not in route.responses
    assert route.responses["200"].body is not None
    assert route.responses["200"].body.description == "ServerResponse wrapper containing returned handler data."
    assert "id" in route.responses["200"].body.properties
    assert "name" in route.responses["200"].body.properties
    assert any("req.user?.id" in note for note in route.notes)
    assert any("personal_todo_list" in note for note in route.notes)
    assert all("Could not parse handler source for response inference" not in note for note in route.notes)
    assert all("No concrete request contract fields inferred" not in note for note in route.notes)
