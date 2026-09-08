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
                '// tasksApiRouter.delete("/commented/:id", safeControllerFunction(TasksController.delete));',
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
    assert any(call["handler_hint"] == "TasksController.create" for call in tasks["route_calls"])
    assert any(call["handler_hint"] == "TasksController.getTasksByProject" for call in tasks["route_calls"])
    assert not any(call["path"] == "/commented/:id" for call in tasks["route_calls"])
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


def test_route_index_recognizes_decorator_based_controllers(tmp_path: Path) -> None:
    """TS decorator routing (NestJS, routing-controllers) declares routes with
    a class-prefix decorator and per-method verb decorators rather than a
    call on a receiver — a shape the container/mount regexes above cannot
    see at all. The class itself becomes a container (its decorator's string
    argument is the `own_prefix`); each decorated method becomes a
    `route_calls` entry whose receiver is the class name."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "PetController.ts").write_text(
        "\n".join(
            [
                "@Authorized()",
                "@JsonController('/pets')",
                "export class PetController {",
                "    constructor(private petService: PetService) { }",
                "    @Post()",
                "    @ResponseSchema(PetResponse)",
                "    public create(@Body() body: CreatePetBody): Promise<Pet> {",
                "        return this.petService.create(body);",
                "    }",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    files = {item["path"]: item for item in payload["files"]}
    controller = files["src/PetController.ts"]

    assert "PetController" in controller["router_symbols"]
    assert any(
        item["symbol"] == "PetController" and item["prefix"] == "/pets"
        for item in controller["containers"]
    )
    assert any(
        call["receiver"] == "PetController" and call["method"] == "post" and call["handler_hint"] == "create"
        for call in controller["route_calls"]
    )


def test_route_index_recognizes_spring_controllers_without_a_class_prefix(tmp_path: Path) -> None:
    """Spring MVC is the same class-decorator/method-decorator shape again,
    just with Java syntax and no class-level `@RequestMapping` in this
    (common) case — each method's own path is already absolute. No container
    should be declared (there is no prefix to declare), but the route call
    must still be captured against the class as receiver."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "UserController.java").write_text(
        "\n".join(
            [
                "package com.example.controller;",
                "",
                "@RestController",
                "public class UserController {",
                '    @PostMapping("/user")',
                "    public Dict save(@RequestBody User user) {",
                "        return userService.save(user);",
                "    }",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    files = {item["path"]: item for item in payload["files"]}
    controller = files["src/UserController.java"]

    assert controller["containers"] == []
    assert any(
        call["receiver"] == "UserController" and call["method"] == "post"
        and call["path"] == "/user" and call["handler_hint"] == "save"
        for call in controller["route_calls"]
    )


def test_route_index_recognizes_spring_controllers_with_a_class_prefix(tmp_path: Path) -> None:
    """The class-level `@RequestMapping` prefix case, mirroring the existing
    `_extract_spring_routes` test in test_deterministic_route_extractors.py
    but through the route_index/route_graph container model instead."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "BookController.java").write_text(
        "\n".join(
            [
                '@RequestMapping("/db")',
                "public class BookController {",
                '    @GetMapping("/books")',
                "    public List<Book> getBooks() { return List.of(); }",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    files = {item["path"]: item for item in payload["files"]}
    controller = files["src/BookController.java"]

    assert "BookController" in controller["router_symbols"]
    assert any(
        item["symbol"] == "BookController" and item["prefix"] == "/db"
        for item in controller["containers"]
    )
    assert any(
        call["receiver"] == "BookController" and call["method"] == "get"
        and call["path"] == "/books" and call["handler_hint"] == "getBooks"
        for call in controller["route_calls"]
    )


def test_route_index_records_java_interface_and_implementing_class(tmp_path: Path) -> None:
    """Unrelated to route composition — feeds interface_bridge.py's bridge
    from a call on an interface-typed field to its sole implementation."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "IUserService.java").write_text(
        "public interface IUserService {\n    Boolean save(User user);\n}\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "UserServiceImpl.java").write_text(
        "public class UserServiceImpl implements IUserService {\n"
        "    public Boolean save(User user) { return true; }\n"
        "}\n",
        encoding="utf-8",
    )

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    files = {item["path"]: item for item in payload["files"]}

    assert files["src/IUserService.java"]["java_type"] == {
        "kind": "interface", "name": "IUserService", "implements": [],
    }
    assert files["src/UserServiceImpl.java"]["java_type"] == {
        "kind": "class", "name": "UserServiceImpl", "implements": ["IUserService"],
    }


def test_route_index_java_type_is_none_for_a_class_implementing_nothing(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "Plain.java").write_text(
        "public class Plain {\n    void run() {}\n}\n", encoding="utf-8",
    )

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    files = {item["path"]: item for item in payload["files"]}
    assert files["src/Plain.java"]["java_type"] == {"kind": "class", "name": "Plain", "implements": []}


def test_route_index_recognizes_rocket_route_attributes(tmp_path: Path) -> None:
    """Rocket declares a route as an attribute directly on a free function
    (`#[get("/<id>")]` above `fn retrieve(...)`), not on a method inside a
    class/struct the way every other framework recognized so far does —
    there is no container to look up a prefix from, so each route's own
    attribute path is used as the whole path (RS-S-01's real shape:
    `examples/pastebin`, mounted at the root path)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "main.rs").write_text(
        "\n".join(
            [
                "#[macro_use] extern crate rocket;",
                "",
                '#[post("/", data = "<paste>")]',
                "async fn upload(paste: Data<'_>) -> io::Result<String> {",
                "    Ok(String::new())",
                "}",
                "",
                '#[get("/<id>")]',
                "async fn retrieve(id: PasteId<'_>) -> Option<RawText<File>> {",
                "    None",
                "}",
                "",
                "#[launch]",
                "fn rocket() -> _ {",
                '    rocket::build().mount("/", routes![upload, retrieve])',
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_route_index(RepoRef(name="demo", root=str(repo_root)))
    files = {item["path"]: item for item in payload["files"]}
    main = files["src/main.rs"]

    assert main["containers"] == []
    keys = {(c["method"], c["path"], c["handler_hint"]) for c in main["route_calls"]}
    assert ("post", "/", "upload") in keys
    assert ("get", "/{id}", "retrieve") in keys


def test_route_index_ignores_a_rust_attribute_with_no_following_function() -> None:
    """A route attribute followed by something else entirely (a struct, a
    comment, another unrelated item) must not misattribute the path to the
    wrong symbol — or to none at all, silently."""
    from sydes.discover.route_index import _extract_index_for_file

    text = '#[get("/health")]\nstruct NotAHandler;\n'
    result = _extract_index_for_file("src/main.rs", text, "source_route_candidate")
    assert result["route_calls"] == []
