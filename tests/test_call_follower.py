from pathlib import Path

from sydes.trace.call_follower import CallFollowBudgets, build_layered_trace_expansion


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_follows_high_importance_service_call(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/service.ts",
        "\n".join(
            [
                "export class UserService {",
                "  static async create(input) {",
                "    return db.query('INSERT INTO users VALUES ($1)', [input.name]);",
                "  }",
                "}",
            ]
        ),
    )
    repo_index = {
        "files": [
            {
                "path": "src/controller.ts",
                "imports": [{"local": "UserService", "source": "./service", "resolved_file": "src/service.ts"}],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/service.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "UserService"}],
                "symbols": [
                    {"name": "UserService", "kind": "class", "file": "src/service.ts", "line": 1},
                    {
                        "name": "create",
                        "qualified_name": "UserService.create",
                        "kind": "class_method",
                        "parent": "UserService",
                        "file": "src/service.ts",
                        "line": 2,
                        "start_line": 2,
                    },
                ],
            },
        ]
    }
    primary_slice = {
        "file": "src/controller.ts",
        "statements": [
            {
                "index": 1,
                "text": "const user = await UserService.create(input);",
                "signals": ["await_call"],
            }
        ],
    }
    resolution = {"primary_handler": {"normalized_handler": "UserController.create"}}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/users"},
        resolution=resolution,
        primary_slice=primary_slice,
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert out["summary"]["functions_followed"] == 1
    assert any(item["handler"] == "UserService.create" for item in out["layers"])


def test_bridge_edges_resolve_a_constructor_injected_member_call(tmp_path: Path) -> None:
    """Regression test for the real, confirmed bug behind a 0/20
    deterministic structural-resolution failure on a real PR: a text-
    extracted call name for a member call through `this` is `field.method`
    (`petService.create`), not the bare method name `call_edges`-mode
    (a backend-supplied full call graph) keys its own `edge_targets` by --
    `bridge_targets` (built from the separate, backend-agnostic
    `bridge_edges` parameter) must be keyed the same way the text
    extractor actually produces, or the lookup always misses even when the
    correct edge is present. This is the SAME shape
    `sydes.discover.member_call_bridge.bridge_member_call_edges` produces
    for a TypeScript constructor-injected field."""
    _write(
        tmp_path / "src/controller.ts",
        "\n".join(
            [
                "export class PetController {",
                "  constructor(private petService: PetService) {}",
                "  async create(pet) {",
                "    return this.petService.create(pet);",
                "  }",
                "}",
            ]
        ),
    )
    _write(
        tmp_path / "src/service.ts",
        "\n".join(
            [
                "export class PetService {",
                "  async create(pet) {",
                "    return pet;",
                "  }",
                "}",
            ]
        ),
    )
    repo_index = {
        "files": [
            {
                "path": "src/controller.ts",
                "imports": [],
                "exports": [],
                "symbols": [
                    {
                        "name": "create", "qualified_name": "PetController.create",
                        "kind": "class_method", "parent": "PetController",
                        "file": "src/controller.ts", "line": 3, "start_line": 3,
                    },
                ],
            },
            {
                "path": "src/service.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "PetService"}],
                "symbols": [
                    {"name": "PetService", "kind": "class", "file": "src/service.ts", "line": 1},
                    {
                        "name": "create", "qualified_name": "PetService.create",
                        "kind": "class_method", "parent": "PetService",
                        "file": "src/service.ts", "line": 2, "start_line": 2,
                    },
                ],
            },
        ]
    }
    primary_slice = {
        "file": "src/controller.ts",
        "statements": [
            {
                "index": 1,
                "text": "return this.petService.create(pet);",
                "signals": ["await_call"],
            }
        ],
    }
    resolution = {"primary_handler": {"normalized_handler": "PetController.create"}}
    bridge_edges = [{
        "repo": "app",
        "caller_file": "src/controller.ts", "caller_symbol": "create",
        "callee_file": "src/service.ts", "callee_symbol": "create",
        "source": "member_call_bridge", "bridge_reason": "constructor_typed_member_call",
        "bridge_receiver": "petService", "bridge_receiver_type": "PetService",
        "bridge_target_method": "create",
    }]

    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/pets"},
        resolution=resolution,
        primary_slice=primary_slice,
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
        bridge_edges=bridge_edges,
    )
    assert out["unresolved_calls"] == [], out["unresolved_calls"]
    assert any(item["handler"] == "PetService.create" for item in out["layers"])


def test_does_not_follow_response_or_db_calls(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/controller.ts", "imports": [], "exports": [], "symbols": []}]}
    primary_slice = {
        "file": "src/controller.ts",
        "statements": [
            {"index": 1, "text": "return res.status(200).json(data);", "signals": ["response_return"]},
            {"index": 2, "text": "await db.query(q, []);", "signals": ["await_call", "possible_db_call"]},
        ],
    }
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "X.y"}},
        primary_slice=primary_slice,
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert out["summary"]["functions_followed"] == 0


def test_respects_max_depth(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/a.ts", "imports": [], "exports": [], "symbols": []}]}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "A.x"}},
        primary_slice={"file": "src/a.ts", "statements": []},
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=1),
    )
    assert len(out["layers"]) == 1


def test_cycle_prevention(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/a.ts",
        "\n".join(
            [
                "export function a() {",
                "  return b();",
                "}",
                "export function b() {",
                "  return a();",
                "}",
            ]
        ),
    )
    repo_index = {
        "files": [
            {
                "path": "src/a.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "a"}, {"kind": "named", "symbol": "b"}],
                "symbols": [
                    {"name": "a", "kind": "function", "file": "src/a.ts", "line": 1, "start_line": 1},
                    {"name": "b", "kind": "function", "file": "src/a.ts", "line": 4, "start_line": 4},
                ],
            }
        ]
    }
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "a"}},
        primary_slice={"file": "src/a.ts", "statements": [{"index": 1, "text": "return b();", "signals": []}]},
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2, max_calls_per_function=2),
    )
    assert out["summary"]["functions_followed"] >= 1


def test_unresolved_call_does_not_crash(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/a.ts", "imports": [], "exports": [], "symbols": []}]}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "A.y"}},
        primary_slice={"file": "src/a.ts", "statements": [{"index": 1, "text": "return Missing.call();", "signals": []}]},
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert out["unresolved_calls"]


def test_low_importance_helper_skipped(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/a.ts", "imports": [], "exports": [], "symbols": []}]}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "A.y"}},
        primary_slice={"file": "src/a.ts", "statements": [{"index": 1, "text": "data.size = humanFileSize(data.size);", "signals": []}]},
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert any(item["reason"] == "low_value_formatting_helper" for item in out["skipped_calls"])


def test_sql_string_contents_are_not_extracted_as_calls(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/a.ts", "imports": [], "exports": [], "symbols": []}]}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "A.y"}},
        primary_slice={
            "file": "src/a.ts",
            "statements": [
                {
                    "index": 1,
                    "text": "const q = `INSERT INTO task_attachments VALUES (CONCAT($1, $2))`;",
                    "signals": ["sql_literal"],
                }
            ],
        },
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    unresolved_names = {item["call"] for item in out["unresolved_calls"]}
    assert "task_attachments" not in unresolved_names
    assert "VALUES" not in unresolved_names
    assert "CONCAT" not in unresolved_names


def test_response_wrapper_constructor_skipped(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/a.ts", "imports": [], "exports": [], "symbols": []}]}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "A.y"}},
        primary_slice={
            "file": "src/a.ts",
            "statements": [{"index": 1, "text": "return res.status(200).send(new ServerResponse(true, data));", "signals": ["response_return"]}],
        },
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert any(item["reason"] == "low_value_response_wrapper" for item in out["skipped_calls"])


def test_config_getters_skipped_by_default(tmp_path: Path) -> None:
    repo_index = {"files": [{"path": "src/a.ts", "imports": [], "exports": [], "symbols": []}]}
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "A.y"}},
        primary_slice={
            "file": "src/a.ts",
            "statements": [{"index": 1, "text": "const base = `${getStorageUrl()}/${getRootDir()}`;", "signals": []}],
        },
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    reasons = {item["reason"] for item in out["skipped_calls"]}
    assert "low_value_config_getter" in reasons


def test_imported_upload_function_resolves_high_priority(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/storage.ts",
        "\n".join(
            [
                "export async function uploadBase64(file, key) {",
                "  return putObject(file, key);",
                "}",
            ]
        ),
    )
    repo_index = {
        "files": [
            {
                "path": "src/controller.ts",
                "imports": [{"local": "uploadBase64", "source": "./storage", "resolved_file": "src/storage.ts"}],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/storage.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "uploadBase64"}],
                "symbols": [
                    {"name": "uploadBase64", "kind": "function", "file": "src/storage.ts", "line": 1, "start_line": 1},
                ],
            },
        ]
    }
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/upload"},
        resolution={"primary_handler": {"normalized_handler": "Controller.create"}},
        primary_slice={
            "file": "src/controller.ts",
            "statements": [{"index": 1, "text": "const s3Url = await uploadBase64(file, key);", "signals": ["await_call", "possible_external_call"]}],
        },
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert any(item["call"] == "uploadBase64" for item in out["followed_calls"])


def test_imported_upload_buffer_function_resolves_high_priority(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/storage.ts",
        "\n".join(
            [
                "export async function uploadBuffer(buf, key) {",
                "  return putObject(buf, key);",
                "}",
            ]
        ),
    )
    repo_index = {
        "files": [
            {
                "path": "src/controller.ts",
                "imports": [{"local": "uploadBuffer", "source": "./storage", "resolved_file": "src/storage.ts"}],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/storage.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "uploadBuffer"}],
                "symbols": [
                    {"name": "uploadBuffer", "kind": "function", "file": "src/storage.ts", "line": 1, "start_line": 1},
                ],
            },
        ]
    }
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/avatar"},
        resolution={"primary_handler": {"normalized_handler": "Controller.avatar"}},
        primary_slice={
            "file": "src/controller.ts",
            "statements": [{"index": 1, "text": "const url = await uploadBuffer(buffer, key);", "signals": ["await_call", "possible_external_call"]}],
        },
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert any(item["call"] == "uploadBuffer" for item in out["followed_calls"])


def test_ambiguous_global_matches_choose_exported_function_for_upload_calls(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/a.ts",
        "\n".join(
            [
                "export async function uploadBase64(file, key) {",
                "  return key;",
                "}",
            ]
        ),
    )
    _write(
        tmp_path / "src/b.ts",
        "\n".join(
            [
                "class UploadSvc {",
                "  static uploadBase64(file, key) {",
                "    return key;",
                "  }",
                "}",
            ]
        ),
    )
    repo_index = {
        "files": [
            {
                "path": "src/controller.ts",
                "imports": [],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/a.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "uploadBase64"}],
                "symbols": [{"name": "uploadBase64", "kind": "function", "file": "src/a.ts", "line": 1, "start_line": 1, "exported": True}],
            },
            {
                "path": "src/b.ts",
                "imports": [],
                "exports": [],
                "symbols": [{"name": "uploadBase64", "kind": "class_method", "parent": "UploadSvc", "qualified_name": "UploadSvc.uploadBase64", "file": "src/b.ts", "line": 1}],
            },
        ]
    }
    out = build_layered_trace_expansion(
        repo_root=tmp_path,
        matched_endpoint={"path": "/x"},
        resolution={"primary_handler": {"normalized_handler": "Controller.y"}},
        primary_slice={
            "file": "src/controller.ts",
            "statements": [{"index": 1, "text": "await uploadBase64(file, key);", "signals": ["await_call", "possible_external_call"]}],
        },
        repo_index=repo_index,
        budgets=CallFollowBudgets(max_depth=2),
    )
    assert any(item["call"] == "uploadBase64" for item in out["followed_calls"])
    assert not any(item["call"] == "uploadBase64" and item["reason"] == "ambiguous" for item in out["unresolved_calls"])
