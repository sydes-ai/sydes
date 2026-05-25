"""Tests for safe routing-pattern plan executor."""

from sydes.discover.routing_pattern_executor import execute_routing_pattern_plan, extract_handler_from_call_snippet


def _plan() -> dict:
    return {
        "version": "v1",
        "repo": "demo",
        "framework_family": "express",
        "routing_convention": "modular_router_mount_graph",
        "confidence": 0.9,
        "route_container_patterns": [{"kind": "express_router_instance"}],
        "route_declaration_patterns": [{"kind": "method_call", "methods": ["get", "post"]}],
        "mount_patterns": [{"kind": "router_use"}],
        "entrypoint_hints": [],
        "route_dir_hints": [],
        "ignore_hints": [],
        "risks": [],
        "recommended_next_action": "apply_mount_graph_extraction",
    }


def _graph_repo() -> dict:
    return {
        "summary": {"mount_edges": 2, "unresolved_mounts": 0},
        "composed_routes": [
            {
                "method": "GET",
                "path": "/api/v1/tasks/project/{id}",
                "file": "src/routes/tasks.ts",
                "handler": None,
                "evidence": [
                    {
                        "file": "src/routes/tasks.ts",
                        "label": "route_declaration",
                        "snippet": 'tasksApiRouter.get("/project/:id", safeControllerFunction(TasksController.getTasksByProject));',
                    },
                    {
                        "file": "src/routes/index.ts",
                        "label": "mount_edge",
                        "snippet": 'api.use("/tasks", tasksApiRouter);',
                    },
                    {
                        "file": "src/app.ts",
                        "label": "mount_edge",
                        "snippet": 'app.use("/api/v1", apiLimiter, isLoggedIn, apiRouter);',
                    },
                ],
            }
        ],
    }


def test_executor_accepts_method_call_plan_and_uses_composed_routes() -> None:
    result = execute_routing_pattern_plan(repo_name="demo", plan=_plan(), route_graph_repo=_graph_repo())
    assert result["plan_applied"] is True
    assert result["routes_added"] == 1
    route = result["routes"][0]
    assert route.method == "GET"
    assert route.path == "/api/v1/tasks/project/{id}"


def test_executor_rejects_unsupported_plan_kinds() -> None:
    plan = _plan()
    plan["route_declaration_patterns"] = [{"kind": "free_form_regex"}]
    result = execute_routing_pattern_plan(repo_name="demo", plan=plan, route_graph_repo=_graph_repo())
    assert result["plan_applied"] is False
    assert "no_supported_plan_kinds" in result["warnings"]


def test_handler_extraction_supports_wrappers_and_symbols() -> None:
    assert (
        extract_handler_from_call_snippet(
            'tasksApiRouter.post("/", safeControllerFunction(TasksController.create));'
        )
        == "TasksController.create"
    )
    assert (
        extract_handler_from_call_snippet('router.get("/", asyncHandler(UserController.list));')
        == "UserController.list"
    )
    assert extract_handler_from_call_snippet('router.get("/", SomeController.method);') == "SomeController.method"


def test_mount_composition_result_preserved_and_param_normalized() -> None:
    result = execute_routing_pattern_plan(repo_name="demo", plan=_plan(), route_graph_repo=_graph_repo())
    route = result["routes"][0]
    assert route.path == "/api/v1/tasks/project/{id}"


def test_executor_ignores_unknown_fields_and_does_not_execute_code() -> None:
    plan = _plan()
    plan["arbitrary_regex"] = "(?P<evil>.*)"
    plan["code"] = "__import__('os').system('rm -rf /')"
    result = execute_routing_pattern_plan(repo_name="demo", plan=plan, route_graph_repo=_graph_repo())
    assert result["plan_applied"] is True
    assert result["routes_added"] == 1
