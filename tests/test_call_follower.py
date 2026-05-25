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
    assert any(item["reason"] == "low_importance" for item in out["skipped_calls"])

