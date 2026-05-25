"""Tests for generic route-graph facts and Express mount composition."""

from sydes.discover.route_graph import build_route_graph_facts_from_route_index_batch


def _mini_index_batch() -> dict:
    return {
        "version": "v1",
        "repos": [
            {
                "repo": "demo",
                "root": "/tmp/demo",
                "files": [
                    {
                        "path": "src/routes/apis/tasks-api-router.ts",
                        "language": "typescript",
                        "role": "source_route_candidate",
                        "signals": ["router_instance:express.Router"],
                        "router_symbols": ["tasksApiRouter"],
                        "route_calls": [
                            {
                                "receiver": "tasksApiRouter",
                                "method": "post",
                                "path": "/",
                                "handler_hint": "TasksController.create",
                                "line": 31,
                                "snippet": 'tasksApiRouter.post("/", safeControllerFunction(TasksController.create));',
                            },
                            {
                                "receiver": "tasksApiRouter",
                                "method": "get",
                                "path": "/project/:id",
                                "handler_hint": "TasksController.getTasksByProject",
                                "line": 40,
                                "snippet": 'tasksApiRouter.get("/project/:id", safeControllerFunction(TasksController.getTasksByProject));',
                            },
                        ],
                        "mount_calls": [],
                        "imports": [],
                        "exports": [{"kind": "default", "symbol": "tasksApiRouter"}],
                        "path_literals": ["/", "/project/:id"],
                    },
                    {
                        "path": "src/routes/apis/index.ts",
                        "language": "typescript",
                        "role": "source_route_candidate",
                        "signals": ["router_instance:express.Router"],
                        "router_symbols": ["apiRouter"],
                        "route_calls": [],
                        "mount_calls": [
                            {
                                "receiver": "apiRouter",
                                "prefix": "/tasks",
                                "child": "tasksApiRouter",
                                "line": 10,
                                "snippet": 'apiRouter.use("/tasks", tasksApiRouter);',
                            }
                        ],
                        "imports": [{"local": "tasksApiRouter", "source": "./tasks-api-router"}],
                        "exports": [{"kind": "default", "symbol": "apiRouter"}],
                        "path_literals": ["/tasks"],
                    },
                    {
                        "path": "src/app.ts",
                        "language": "typescript",
                        "role": "source_route_candidate",
                        "signals": [],
                        "router_symbols": [],
                        "route_calls": [],
                        "mount_calls": [
                            {
                                "receiver": "app",
                                "prefix": "/api/v1",
                                "child": "apiRouter",
                                "line": 7,
                                "snippet": 'app.use("/api/v1", apiLimiter, isLoggedIn, apiRouter);',
                            }
                        ],
                        "imports": [{"local": "apiRouter", "source": "./routes/apis"}],
                        "exports": [],
                        "path_literals": ["/api/v1"],
                    },
                ],
                "summary": {},
            }
        ],
    }


def test_route_graph_composes_nested_mount_prefixes_and_normalizes_params() -> None:
    payload = build_route_graph_facts_from_route_index_batch(_mini_index_batch())
    repo = payload["repos"][0]
    paths = {(item["method"], item["path"]) for item in repo["composed_routes"]}

    assert ("POST", "/api/v1/tasks") in paths
    assert ("GET", "/api/v1/tasks/project/{id}") in paths
    assert repo["summary"]["mount_edges"] >= 2
    assert repo["summary"]["containers"] >= 3


def test_route_graph_handles_mount_with_middleware_and_unresolved_mount_reporting() -> None:
    index = _mini_index_batch()
    # Add unresolved mount candidate where child symbol does not resolve.
    index["repos"][0]["files"][2]["mount_calls"].append(
        {
            "receiver": "app",
            "prefix": "/broken",
            "child": "missingRouter",
            "line": 20,
            "snippet": 'app.use("/broken", authz, missingRouter);',
        }
    )

    payload = build_route_graph_facts_from_route_index_batch(index)
    repo = payload["repos"][0]
    assert repo["summary"]["unresolved_mounts"] >= 1
    assert any(item["child_symbol"] == "missingRouter" for item in repo["unresolved_mounts"])


def test_route_graph_preserves_route_declaration_and_mount_evidence() -> None:
    payload = build_route_graph_facts_from_route_index_batch(_mini_index_batch())
    repo = payload["repos"][0]
    composed = next(item for item in repo["composed_routes"] if item["method"] == "GET")
    snippets = [e.get("snippet") for e in composed["evidence"] if isinstance(e, dict)]

    assert any("tasksApiRouter.get" in (s or "") for s in snippets)
    assert any("apiRouter.use" in (s or "") for s in snippets)
    assert any("app.use" in (s or "") for s in snippets)
