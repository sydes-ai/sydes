"""Tests for deterministic route index artifact builder."""

from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.route_index import build_route_index


def _write_route_index_fixture(root: Path) -> None:
    (root / "src" / "routes" / "apis").mkdir(parents=True)
    (root / "src" / "controllers").mkdir(parents=True)
    (root / "node_modules").mkdir()

    (root / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    (root / "src" / "routes" / "apis" / "tasks-api-router.ts").write_text(
        "\n".join(
            [
                'import express from "express";',
                'import { TasksController } from "../../controllers/tasks-controller";',
                'const tasksApiRouter = express.Router();',
                'tasksApiRouter.post("/", taskCreateBodyValidator, safeControllerFunction(TasksController.create));',
                'tasksApiRouter.get("/project/:id", idParamValidator, safeControllerFunction(TasksController.getTasksByProject));',
                'export default tasksApiRouter;',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "src" / "routes" / "apis" / "index.ts").write_text(
        "\n".join(
            [
                'import express from "express";',
                'import tasksApiRouter from "./tasks-api-router";',
                'const api = express.Router();',
                'api.use("/tasks", tasksApiRouter);',
                'export default api;',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "src" / "app.ts").write_text(
        "\n".join(
            [
                'import apiRouter from "./routes/apis";',
                'app.use("/api/v1", apiLimiter, isLoggedIn, apiRouter);',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "src" / "controllers" / "tasks-controller.ts").write_text(
        "export class TasksController {}\n", encoding="utf-8"
    )
    (root / "node_modules" / "ignore.js").write_text("ignored\n", encoding="utf-8")


def test_route_index_extracts_route_calls_mounts_symbols_and_imports(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_route_index_fixture(repo_root)

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))

    files = {item["path"]: item for item in payload["files"]}
    assert "src/routes/apis/tasks-api-router.ts" in files
    assert "src/routes/apis/index.ts" in files

    tasks = files["src/routes/apis/tasks-api-router.ts"]
    assert "tasksApiRouter" in tasks["router_symbols"]
    assert any(call["method"] == "post" and call["path"] == "/" for call in tasks["route_calls"])
    assert any(call["method"] == "get" and call["path"] == "/project/:id" for call in tasks["route_calls"])
    assert any(item["kind"] == "default" and item["symbol"] == "tasksApiRouter" for item in tasks["exports"])

    api_index = files["src/routes/apis/index.ts"]
    assert any(mount["prefix"] == "/tasks" and mount["child"] == "tasksApiRouter" for mount in api_index["mount_calls"])

    app_file = files["src/app.ts"]
    assert any(mount["prefix"] == "/api/v1" and mount["child"] == "apiRouter" for mount in app_file["mount_calls"])

    assert payload["summary"]["route_call_count"] >= 2
    assert payload["summary"]["mount_call_count"] >= 2
    assert payload["summary"]["router_symbol_count"] >= 2


def test_route_index_skips_ignored_dirs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_route_index_fixture(repo_root)

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    indexed_paths = {item["path"] for item in payload["files"]}
    assert all(not path.startswith("node_modules/") for path in indexed_paths)
