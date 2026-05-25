from pathlib import Path

from sydes.core.models import RepoRef
from sydes.trace.handler_symbol_index import (
    build_handler_symbol_index,
    resolve_local_import,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_extracts_default_and_named_imports(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/routes/attachments.ts",
        "\n".join(
            [
                'import AttachmentController from "../../controllers/attachment-controller";',
                'import { getList as listFn } from "../tasks";',
            ]
        ),
    )
    _write(tmp_path / "src/controllers/attachment-controller.ts", "export default class AttachmentController {}")
    _write(tmp_path / "src/tasks.ts", "export function getList() {}")
    repo = RepoRef(name="api", root=str(tmp_path))
    index = build_handler_symbol_index(repo)
    route_file = next(item for item in index["files"] if item["path"] == "src/routes/attachments.ts")
    imports = route_file["imports"]
    assert any(item["kind"] == "default" and item["local"] == "AttachmentController" for item in imports)
    assert any(
        item["kind"] == "named"
        and item["local"] == "listFn"
        and item["imported"] == "getList"
        for item in imports
    )


def test_extracts_default_export_class_and_static_async_method(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/attachment-controller.ts",
        "\n".join(
            [
                "export default class AttachmentController {",
                "  public static async createTaskAttachment(req, res) {",
                "    return res.status(201).json({ ok: true });",
                "  }",
                "}",
            ]
        ),
    )
    repo = RepoRef(name="api", root=str(tmp_path))
    index = build_handler_symbol_index(repo)
    controller_file = next(
        item for item in index["files"] if item["path"] == "src/controllers/attachment-controller.ts"
    )
    symbols = controller_file["symbols"]
    assert any(
        item["kind"] == "class" and item["name"] == "AttachmentController" and item["export_kind"] == "default"
        for item in symbols
    )
    assert any(
        item["kind"] == "class_method"
        and item["qualified_name"] == "AttachmentController.createTaskAttachment"
        and item["static"] is True
        and item["async"] is True
        for item in symbols
    )


def test_extracts_class_with_separate_default_export(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/tasks-controller.ts",
        "\n".join(
            [
                "class TasksController {",
                "  static create(req, res) { return res.json({}); }",
                "}",
                "export default TasksController;",
            ]
        ),
    )
    repo = RepoRef(name="api", root=str(tmp_path))
    index = build_handler_symbol_index(repo)
    file_payload = next(item for item in index["files"] if item["path"] == "src/controllers/tasks-controller.ts")
    assert any(
        item["kind"] == "class" and item["name"] == "TasksController" and item["exported"] is True
        for item in file_payload["symbols"]
    )


def test_extracts_exported_functions_and_const_arrow(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/users.ts",
        "\n".join(
            [
                "export async function getList(req, res) { return []; }",
                "export const create = async (req, res) => ({ ok: true });",
            ]
        ),
    )
    repo = RepoRef(name="api", root=str(tmp_path))
    index = build_handler_symbol_index(repo)
    file_payload = next(item for item in index["files"] if item["path"] == "src/controllers/users.ts")
    symbols = file_payload["symbols"]
    assert any(
        item["kind"] == "function"
        and item["name"] == "getList"
        and item["exported"] is True
        and item["async"] is True
        for item in symbols
    )
    assert any(
        item["kind"] == "function"
        and item["name"] == "create"
        and item["exported"] is True
        and item["async"] is True
        for item in symbols
    )


def test_relative_import_and_directory_resolution(tmp_path: Path) -> None:
    _write(tmp_path / "src/controllers/attachment-controller.ts", "export default class AttachmentController {}")
    _write(tmp_path / "src/routes/apis/index.ts", "export default api;")
    resolved_controller = resolve_local_import(
        tmp_path,
        "src/routes/attachments.ts",
        "../controllers/attachment-controller",
    )
    resolved_index = resolve_local_import(tmp_path, "src/app.ts", "./routes/apis")
    assert resolved_controller == "src/controllers/attachment-controller.ts"
    assert resolved_index == "src/routes/apis/index.ts"


def test_ignores_test_and_noise_directories(tmp_path: Path) -> None:
    _write(tmp_path / "src/routes/api.ts", "export const list = (req, res) => res.json([]);")
    _write(tmp_path / "tests/controllers/test_controller.ts", "export const fake = () => null;")
    _write(tmp_path / "node_modules/lib/index.js", "module.exports = {};")
    _write(tmp_path / "dist/out.js", "console.log('ignore');")
    repo = RepoRef(name="api", root=str(tmp_path))
    index = build_handler_symbol_index(repo)
    indexed_paths = {item["path"] for item in index["files"]}
    assert "src/routes/api.ts" in indexed_paths
    assert "tests/controllers/test_controller.ts" not in indexed_paths
    assert "node_modules/lib/index.js" not in indexed_paths
    assert "dist/out.js" not in indexed_paths
