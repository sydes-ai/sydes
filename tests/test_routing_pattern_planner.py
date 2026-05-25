"""Tests for bounded routing-pattern planner foundations."""

import json

import pytest

from sydes.discover.routing_pattern_planner import (
    build_routing_pattern_planner_input,
    build_routing_pattern_planner_prompt,
    run_routing_pattern_planner,
    select_representative_snippets,
    validate_routing_pattern_plan,
)
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse


class _FakePlannerClient:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        assert "Do NOT enumerate all routes" in request.prompt
        return LLMResponse(text=self.text)


def _route_index_repo() -> dict:
    return {
        "repo": "demo",
        "summary": {
            "files_indexed": 20,
            "files_with_route_calls": 10,
            "route_call_count": 50,
            "mount_call_count": 6,
            "router_symbol_count": 8,
        },
        "files": [
            {
                "path": "src/routes/a.ts",
                "router_symbols": ["aRouter"],
                "route_calls": [
                    {
                        "receiver": "aRouter",
                        "method": "get",
                        "path": "/items/:id",
                        "line": 10,
                        "snippet": 'aRouter.get("/items/:id", handler)',
                    }
                ],
                "mount_calls": [
                    {
                        "receiver": "api",
                        "prefix": "/a",
                        "child": "aRouter",
                        "line": 20,
                        "snippet": 'api.use("/a", aRouter)',
                    }
                ],
            },
            {
                "path": "src/routes/b.ts",
                "router_symbols": ["bRouter"],
                "route_calls": [
                    {
                        "receiver": "bRouter",
                        "method": "post",
                        "path": "/items",
                        "line": 8,
                        "snippet": 'bRouter.post("/items", create)',
                    }
                ],
                "mount_calls": [],
            },
        ],
    }


def test_select_representative_snippets_is_bounded_and_diverse() -> None:
    snippets = select_representative_snippets(
        _route_index_repo(),
        max_router_declarations=1,
        max_route_declarations=1,
        max_mount_calls=1,
    )

    assert len(snippets["router_declarations"]) == 1
    assert len(snippets["route_declarations"]) == 1
    assert len(snippets["mount_calls"]) == 1
    assert "snippet" in snippets["route_declarations"][0]


def test_build_planner_input_uses_compact_artifacts_not_full_files() -> None:
    planner_input = build_routing_pattern_planner_input(
        repo_name="demo",
        repo_map_repo={"candidate_route_dirs": ["src/routes"], "summary": {"total_files_seen": 100}},
        route_index_repo=_route_index_repo(),
        route_graph_repo={"summary": {"containers": 8, "declarations": 50, "mount_edges": 6, "composed_routes": 48}},
        coverage={"label": "weak", "score": 0.5, "reasons": ["needs enrichment"]},
    )

    prompt = build_routing_pattern_planner_prompt(planner_input)
    assert "candidate_route_dirs" in prompt
    assert "snippets" in prompt
    assert "content\":" not in prompt


def test_validate_routing_pattern_plan_accepts_valid_schema() -> None:
    payload = {
        "version": "v1",
        "repo": "demo",
        "framework_family": "express",
        "routing_convention": "modular_router_mount_graph",
        "confidence": 0.88,
        "route_container_patterns": [],
        "route_declaration_patterns": [],
        "mount_patterns": [],
        "entrypoint_hints": [],
        "route_dir_hints": [],
        "ignore_hints": [],
        "risks": [],
        "recommended_next_action": "apply_mount_graph_extraction",
    }
    assert validate_routing_pattern_plan(payload)["framework_family"] == "express"


def test_validate_routing_pattern_plan_rejects_malformed_schema() -> None:
    with pytest.raises(ValueError):
        validate_routing_pattern_plan({"repo": "demo"})


def test_run_routing_pattern_planner_parses_json_and_returns_plan() -> None:
    plan_payload = {
        "version": "v1",
        "repo": "ignored",
        "framework_family": "express",
        "routing_convention": "modular_router_mount_graph",
        "confidence": 0.9,
        "route_container_patterns": [],
        "route_declaration_patterns": [],
        "mount_patterns": [],
        "entrypoint_hints": ["src/app.ts"],
        "route_dir_hints": ["src/routes"],
        "ignore_hints": ["node_modules"],
        "risks": [],
        "recommended_next_action": "apply_mount_graph_extraction",
    }
    client = _FakePlannerClient(json.dumps(plan_payload))
    plan = run_routing_pattern_planner(
        repo_name="demo",
        planner_input=build_routing_pattern_planner_input(
            repo_name="demo",
            repo_map_repo={},
            route_index_repo=_route_index_repo(),
            route_graph_repo={"summary": {}},
            coverage={"label": "weak", "score": 0.5, "reasons": []},
        ),
        llm_client=client,
    )

    assert client.calls == 1
    assert plan["repo"] == "demo"
    assert plan["routing_convention"] == "modular_router_mount_graph"


def test_run_routing_pattern_planner_rejects_non_json() -> None:
    client = _FakePlannerClient("not json")
    with pytest.raises(LLMClientError):
        run_routing_pattern_planner(
            repo_name="demo",
            planner_input={"repo": "demo"},
            llm_client=client,
        )
