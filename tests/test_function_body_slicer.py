from pathlib import Path

from sydes.trace.function_body_slicer import slice_resolved_handler_body


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_slices_static_async_class_method_with_line_range(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/attachment-controller.ts",
        "\n".join(
            [
                "export default class AttachmentController {",
                "  public static async createTaskAttachment(req, res) {",
                "    const data = req.body;",
                "    return res.status(200).send(data);",
                "  }",
                "}",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="AttachmentController.createTaskAttachment",
        symbol={
            "file": "src/controllers/attachment-controller.ts",
            "line": 2,
            "start_line": 2,
            "kind": "class_method",
        },
        language="typescript",
    )
    assert payload is not None
    assert payload["start_line"] == 2
    assert payload["end_line"] >= 5
    assert payload["statements"][0]["line_start"] >= 3


def test_slices_top_level_function(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/handlers.ts",
        "\n".join(
            [
                "export async function getList(req, res) {",
                "  const x = req.query;",
                "  return res.json(x);",
                "}",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="getList",
        symbol={"file": "src/handlers.ts", "line": 1, "start_line": 1, "kind": "function"},
        language="typescript",
    )
    assert payload is not None
    assert any("request_query_read" in stmt["signals"] for stmt in payload["statements"])


def test_slices_arrow_function(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/handlers.ts",
        "\n".join(
            [
                "export const create = async (req, res) => {",
                "  await uploadBase64(req.body.file, 'k');",
                "  return res.send({ ok: true });",
                "};",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="create",
        symbol={"file": "src/handlers.ts", "line": 1, "start_line": 1, "kind": "function"},
        language="typescript",
    )
    assert payload is not None
    assert any("possible_external_call" in stmt["signals"] for stmt in payload["statements"])
    assert any("response_return" in stmt["signals"] for stmt in payload["statements"])


def test_preserves_multiline_sql_template_as_statement(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/a.ts",
        "\n".join(
            [
                "export class A {",
                "  static async create(req, res) {",
                "    const q = `",
                "      INSERT INTO task_attachments (id)",
                "      VALUES ($1)",
                "    `;",
                "    const result = await db.query(q, [1]);",
                "    return res.send(result);",
                "  }",
                "}",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="A.create",
        symbol={"file": "src/controllers/a.ts", "line": 2, "start_line": 2, "kind": "class_method"},
        language="typescript",
    )
    assert payload is not None
    assert any("sql_literal" in stmt["signals"] for stmt in payload["statements"])
    assert any("possible_db_call" in stmt["signals"] for stmt in payload["statements"])


def test_detects_branch_and_response_return(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/a.ts",
        "\n".join(
            [
                "function x(req, res) {",
                "  if (!req.body?.id) return res.status(200).send({ ok: false });",
                "  return res.status(200).send({ ok: true });",
                "}",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="x",
        symbol={"file": "src/controllers/a.ts", "line": 1, "start_line": 1, "kind": "function"},
        language="typescript",
    )
    assert payload is not None
    assert any("branch" in stmt["signals"] for stmt in payload["statements"])
    assert any("response_return" in stmt["signals"] for stmt in payload["statements"])


def test_filters_logging_only_lines(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/a.ts",
        "\n".join(
            [
                "function x(req, res) {",
                "  console.log('debug');",
                "  return res.send({ ok: true });",
                "}",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="x",
        symbol={"file": "src/controllers/a.ts", "line": 1, "start_line": 1, "kind": "function"},
        language="typescript",
    )
    assert payload is not None
    assert all("console.log" not in stmt["text"] for stmt in payload["statements"])


def test_preserves_line_numbers(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/a.ts",
        "\n".join(
            [
                "",
                "",
                "function x(req, res) {",
                "  const a = req.body;",
                "  return res.send(a);",
                "}",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="x",
        symbol={"file": "src/controllers/a.ts", "line": 3, "start_line": 3, "kind": "function"},
        language="typescript",
    )
    assert payload is not None
    assert payload["statements"][0]["line_start"] == 4
    assert payload["statements"][-1]["line_end"] == 5


def test_malformed_function_does_not_crash(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/controllers/a.ts",
        "\n".join(
            [
                "function x(req, res) {",
                "  const a = req.body;",
            ]
        ),
    )
    payload = slice_resolved_handler_body(
        repo_root=tmp_path,
        handler_name="x",
        symbol={"file": "src/controllers/a.ts", "line": 1, "start_line": 1, "kind": "function"},
        language="typescript",
    )
    assert payload is None

