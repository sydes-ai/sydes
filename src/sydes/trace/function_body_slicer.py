"""Bounded function body slicing for resolved handler symbols."""

from __future__ import annotations

from pathlib import Path
import re


def _scan_block_end(lines: list[str], start_line: int) -> int | None:
    """Find matching block end line for a function/method starting at start_line."""
    in_single = False
    in_double = False
    in_template = False
    escape = False
    started = False
    depth = 0
    for line_no in range(start_line, len(lines) + 1):
        line = lines[line_no - 1]
        for ch in line:
            if in_single:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "'":
                    in_single = False
                continue
            if in_double:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_double = False
                continue
            if in_template:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "`":
                    in_template = False
                continue
            if ch == "'":
                in_single = True
                continue
            if ch == '"':
                in_double = True
                continue
            if ch == "`":
                in_template = True
                continue
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    return line_no
    return None


def _detect_signals(statement_text: str) -> list[str]:
    text = statement_text.lower()
    signals: list[str] = []
    if "req.body" in text or "request.body" in text:
        signals.append("request_body_read")
    if "req.params" in text or "request.params" in text:
        signals.append("request_params_read")
    if "req.query" in text or "request.query" in text:
        signals.append("request_query_read")
    if any(token in text for token in ("insert into", "select ", "update ", "delete from")):
        signals.append("sql_literal")
    if "await " in text:
        signals.append("await_call")
    if any(token in text for token in ("db.query", ".query(", ".insert", ".update", ".delete", ".save", ".create", ".find")):
        signals.append("possible_db_call")
    if any(token in text for token in ("upload", "fetch(", "axios.", "request(", "s3", "queue", "publish", "sendemail")):
        signals.append("possible_external_call")
    if any(token in text for token in ("res.status", "res.send", "res.json", "return ")):
        signals.append("response_return")
    if re.search(r"\bif\s*\(", text):
        signals.append("branch")
    if re.search(r"\bdata\.[a-z_]\w*\s*=", text):
        signals.append("response_transform")
    return sorted(set(signals))


def _kind_hint(statement_text: str, signals: list[str]) -> str:
    text = statement_text.strip().lower()
    if text.startswith("if "):
        return "branch"
    if "sql_literal" in signals:
        return "sql_literal_assignment"
    if text.startswith("return "):
        return "return"
    if text.startswith("const ") or text.startswith("let ") or text.startswith("var "):
        return "assignment"
    if "await_call" in signals:
        return "await_call"
    return "statement"


def _confidence_for(signals: list[str], text: str) -> float:
    if signals:
        return 0.9
    if text.strip().startswith(("if ", "return ", "await ")):
        return 0.7
    return 0.5


def split_statements(lines: list[str], body_start_line: int) -> list[dict]:
    """Split function body into coarse ordered statements with line numbers."""
    statements: list[dict] = []
    buf: list[str] = []
    stmt_start = None
    paren = 0
    bracket = 0
    brace = 0
    in_single = False
    in_double = False
    in_template = False
    escape = False

    def flush(end_line: int) -> None:
        nonlocal buf, stmt_start
        if not buf or stmt_start is None:
            buf = []
            stmt_start = None
            return
        text = "\n".join(buf).strip()
        buf = []
        if not text:
            stmt_start = None
            return
        if text.startswith("//") or text.startswith("/*"):
            stmt_start = None
            return
        signals = _detect_signals(text)
        if (
            not signals
            and ("console.log" in text or "logger." in text or "print(" in text)
        ):
            stmt_start = None
            return
        statements.append(
            {
                "index": len(statements) + 1,
                "line_start": stmt_start,
                "line_end": end_line,
                "kind_hint": _kind_hint(text, signals),
                "text": re.sub(r"\s+", " ", text).strip(),
                "signals": signals,
                "confidence": _confidence_for(signals, text),
            }
        )
        stmt_start = None

    for offset, raw_line in enumerate(lines):
        line_no = body_start_line + offset
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stmt_start is None:
            stmt_start = line_no
        buf.append(raw_line.rstrip("\n"))
        for ch in raw_line:
            if in_single:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "'":
                    in_single = False
                continue
            if in_double:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_double = False
                continue
            if in_template:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "`":
                    in_template = False
                continue
            if ch == "'":
                in_single = True
                continue
            if ch == '"':
                in_double = True
                continue
            if ch == "`":
                in_template = True
                continue
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren = max(0, paren - 1)
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket = max(0, bracket - 1)
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace = max(0, brace - 1)
            elif ch == ";" and paren == 0 and bracket == 0 and brace == 0:
                flush(line_no)

        if (
            paren == 0
            and bracket == 0
            and brace == 0
            and stripped.startswith("if ")
            and "return " in stripped
        ):
            flush(line_no)
        elif (
            paren == 0
            and bracket == 0
            and brace == 0
            and stripped.startswith("return ")
            and not stripped.endswith(";")
        ):
            flush(line_no)

    if buf:
        flush(body_start_line + len(lines) - 1)
    return statements


def slice_resolved_handler_body(
    *,
    repo_root: Path,
    handler_name: str,
    symbol: dict,
    language: str | None = None,
) -> dict | None:
    """Build a bounded, ordered function body slice for a resolved symbol."""
    symbol_file = symbol.get("file")
    if not isinstance(symbol_file, str) or not symbol_file:
        return None
    source_path = (repo_root / symbol_file).resolve()
    if not source_path.is_file():
        return None

    text = source_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start_line = symbol.get("start_line") or symbol.get("line")
    if not isinstance(start_line, int) or start_line <= 0 or start_line > len(lines):
        return None

    end_line = symbol.get("end_line")
    if not isinstance(end_line, int) or end_line < start_line:
        end_line = _scan_block_end(lines, start_line)
    if end_line is None or end_line <= start_line:
        return None

    body_lines = lines[start_line:end_line - 1]
    statements = split_statements(body_lines, start_line + 1)
    if not statements:
        return None

    all_signals: set[str] = set()
    for item in statements:
        all_signals.update(item.get("signals", []))

    return {
        "handler": handler_name,
        "file": symbol_file,
        "start_line": start_line,
        "end_line": end_line,
        "language": language or "unknown",
        "statements": statements,
        "summary": {
            "statement_count": len(statements),
            "signals": sorted(all_signals),
        },
    }

