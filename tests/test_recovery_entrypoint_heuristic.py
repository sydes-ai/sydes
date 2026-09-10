"""`sydes.recovery.entrypoint_heuristic` -- the one generic decorator-shape
rule for declarative HTTP entrypoints. Deterministic, no LLM, no CBM."""

from __future__ import annotations

from pathlib import Path

from sydes.recovery.entrypoint_heuristic import find_declarative_entrypoints, nearby_files


def test_verb_decorator_directly_above_method_is_found(tmp_path: Path):
    (tmp_path / "controller.ts").write_text(
        "@Controller('users')\n"
        "export class UserController {\n"
        "  @Get('/user/:id')\n"
        "  async getUser(id) {}\n"
        "}\n"
    )
    found = find_declarative_entrypoints(tmp_path, ["controller.ts"])
    assert len(found) == 1
    assert found[0].symbol == "getUser"
    assert found[0].http_method == "GET"
    assert found[0].path == "/user/:id"
    assert found[0].class_prefix == "'users'"


def test_symbolic_non_literal_path_and_prefix_are_still_captured(tmp_path: Path):
    """A shared routes-config constant instead of a string literal is
    common in real code -- this must not be required to match."""
    (tmp_path / "controller.ts").write_text(
        "@Controller(routesV1.version)\n"
        "export class FindUsersHttpController {\n"
        "  @Get(routesV1.user.root)\n"
        "  async findUsers() {}\n"
        "}\n"
    )
    found = find_declarative_entrypoints(tmp_path, ["controller.ts"])
    assert len(found) == 1
    assert found[0].symbol == "findUsers"
    assert found[0].path == "routesV1.user.root"
    assert found[0].class_prefix == "routesV1.version"


def test_multiline_decorator_between_route_and_method_does_not_break_matching(tmp_path: Path):
    """A real reliability-experiment run showed this exact shape (an
    inline options object spanning several lines between the route
    decorator and the method) silently swallow the match."""
    (tmp_path / "controller.ts").write_text(
        "@Controller(routesV1.version)\n"
        "export class FindUsersHttpController {\n"
        "  @Get(routesV1.user.root)\n"
        "  @ApiOperation({ summary: 'Find users' })\n"
        "  @ApiResponse({\n"
        "    status: HttpStatus.OK,\n"
        "    type: UserPaginatedResponseDto,\n"
        "  })\n"
        "  async findUsers(\n"
        "    @Body() request: FindUsersRequestDto,\n"
        "  ): Promise<UserPaginatedResponseDto> {}\n"
        "}\n"
    )
    found = find_declarative_entrypoints(tmp_path, ["controller.ts"])
    assert len(found) == 1
    assert found[0].symbol == "findUsers"


def test_java_annotation_style_mapping_suffix_is_recognized(tmp_path: Path):
    (tmp_path / "UserController.java").write_text(
        "public class UserController {\n"
        "    @GetMapping(\"/user/{id}\")\n"
        "    public Dict getUser(@PathVariable Long id) {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    found = find_declarative_entrypoints(tmp_path, ["UserController.java"])
    assert len(found) == 1
    assert found[0].symbol == "getUser"
    assert found[0].http_method == "GET"


def test_overloaded_methods_with_different_routes_are_both_found_distinctly(tmp_path: Path):
    """A real overload-disambiguation defect was found in CBM's own graph
    engine on exactly this shape (two same-named methods, different
    routes) -- this regex-based heuristic must not repeat it."""
    (tmp_path / "UserController.java").write_text(
        "public class UserController {\n"
        "    @GetMapping(\"/user/{id}\")\n"
        "    public Dict getUser(@PathVariable Long id) { return null; }\n"
        "\n"
        "    @GetMapping(\"/user\")\n"
        "    public Dict getUser(User user) { return null; }\n"
        "}\n"
    )
    found = find_declarative_entrypoints(tmp_path, ["UserController.java"])
    assert len(found) == 2
    paths = {ep.path for ep in found}
    assert paths == {"/user/{id}", "/user"}


def test_non_verb_decorator_is_not_a_false_positive(tmp_path: Path):
    (tmp_path / "resolver.ts").write_text(
        "@Resolver()\n"
        "export class FindUsersGraphqlResolver {\n"
        "  @Query(() => [User])\n"
        "  async findUsers() {}\n"
        "}\n"
    )
    found = find_declarative_entrypoints(tmp_path, ["resolver.ts"])
    assert found == []


def test_non_code_file_is_skipped(tmp_path: Path):
    (tmp_path / "notes.md").write_text("@Get('/x')\ngetX() {}\n")
    assert find_declarative_entrypoints(tmp_path, ["notes.md"]) == []


def test_missing_file_does_not_raise(tmp_path: Path):
    assert find_declarative_entrypoints(tmp_path, ["nope.ts"]) == []


def test_nearby_files_scans_the_changed_files_own_directory(tmp_path: Path):
    module_dir = tmp_path / "src" / "user"
    module_dir.mkdir(parents=True)
    (module_dir / "handler.ts").write_text("changed\n")
    (module_dir / "controller.ts").write_text("sibling\n")
    (module_dir / "handler.spec.ts").write_text("spec\n")
    other_dir = tmp_path / "src" / "other"
    other_dir.mkdir(parents=True)
    (other_dir / "unrelated.ts").write_text("unrelated\n")

    files = nearby_files(tmp_path, ("src/user/handler.ts",))
    assert set(files) == {
        "src/user/handler.ts", "src/user/controller.ts", "src/user/handler.spec.ts",
    }


def test_nearby_files_respects_max_files_cap(tmp_path: Path):
    module_dir = tmp_path / "src"
    module_dir.mkdir()
    (module_dir / "changed.ts").write_text("x\n")
    for i in range(10):
        (module_dir / f"sibling_{i}.ts").write_text("x\n")

    files = nearby_files(tmp_path, ("src/changed.ts",), max_files=3)
    assert len(files) == 3
