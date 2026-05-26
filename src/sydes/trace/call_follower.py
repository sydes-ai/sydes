"""Bounded deterministic call-following for layered trace expansion."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from pathlib import Path

from sydes.trace.function_body_slicer import slice_resolved_handler_body


@dataclass(slots=True)
class CallFollowBudgets:
    max_depth: int = 2
    max_files: int = 10
    max_functions: int = 20
    max_steps: int = 40
    max_calls_per_function: int = 8
    max_branch_steps: int = 5


def _build_file_maps(repo_index: dict) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    files_by_path: dict[str, dict] = {}
    symbols_by_name: dict[str, list[dict]] = {}
    for file_item in repo_index.get("files", []):
        path = file_item.get("path")
        if not isinstance(path, str):
            continue
        files_by_path[path] = file_item
        for symbol in file_item.get("symbols", []):
            if not isinstance(symbol, dict):
                continue
            name = symbol.get("name")
            if isinstance(name, str):
                symbols_by_name.setdefault(name, []).append(symbol)
            qualified = symbol.get("qualified_name")
            if isinstance(qualified, str):
                symbols_by_name.setdefault(qualified, []).append(symbol)
    return files_by_path, symbols_by_name


def _extract_calls_from_statement_text(text: str) -> list[str]:
    sanitized = _strip_string_and_comment_noise(text)
    calls: list[str] = []
    for match in re.finditer(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(", sanitized):
        name = match.group(1)
        if name in {"if", "for", "while", "switch", "return", "new"}:
            continue
        if name.isupper():
            continue
        calls.append(name)
    return calls


def _is_followable_call_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(("res.", "response.", "db.", "console.", "logger.", "math.", "json.", "string.")):
        return False
    if lowered in {"status", "send", "json", "query"}:
        return False
    return True


def _strip_string_and_comment_noise(text: str) -> str:
    out: list[str] = []
    i = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False
    template_expr_depth = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if template_expr_depth == 0 and not in_single and not in_double and not in_backtick:
            if ch == "/" and nxt == "/":
                break
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        if in_single:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            out.append(" ")
            i += 1
            continue
        if in_double:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            out.append(" ")
            i += 1
            continue
        if in_backtick and template_expr_depth == 0:
            if ch == "`":
                in_backtick = False
                out.append(" ")
                i += 1
                continue
            if ch == "$" and nxt == "{":
                template_expr_depth = 1
                out.extend([" ", " "])
                i += 2
                continue
            out.append(" ")
            i += 1
            continue
        if template_expr_depth > 0:
            if ch == "{":
                template_expr_depth += 1
            elif ch == "}":
                template_expr_depth -= 1
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
            out.append(" ")
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(" ")
            i += 1
            continue
        if ch == "`":
            in_backtick = True
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _skip_reason(call_name: str, statement: dict) -> str | None:
    lowered = call_name.lower()
    text = str(statement.get("text") or "")
    lowered_text = text.lower()
    if re.search(rf"\bnew\s+{re.escape(call_name)}\s*\(", text):
        if any(token in lowered for token in ("serverresponse", "apiresponse", "responsedto")):
            return "low_value_response_wrapper"
        return "constructor_wrapper_skipped"
    if lowered in {"serverresponse", "apiresponse", "responsedto"}:
        return "low_value_response_wrapper"
    if lowered in {"getstorageurl", "getrootdir", "getconfig", "getenv"}:
        return "low_value_config_getter"
    if lowered in {"humanfilesize", "formatdate", "smallid"}:
        return "low_value_formatting_helper"
    if lowered in {"getkey", "getavatarkey", "buildpath"}:
        return "low_value_key_builder"
    if re.fullmatch(r"[A-Z_]+", call_name):
        return "string_literal_false_positive"
    if any(tok in lowered_text for tok in ("insert into", "update ", "delete from", "select ")):
        if lowered in {"values", "concat"} or "task_attachments" in lowered:
            return "string_literal_false_positive"
    return None


def _importance_score(name: str, statement: dict) -> int:
    lowered = name.lower()
    score = 0
    if any(token in lowered for token in ("uploadbase64", "uploadbuffer", "uploadfile", "downloadfile", "putobject")):
        score += 5
    elif any(token in lowered for token in ("service", "repository", "repo", "upload", "storage", "client", "api", "publish", "sendemail")):
        score += 3
    if "await_call" in statement.get("signals", []):
        score += 2
    if "response_return" in statement.get("signals", []):
        score += 1
    if any(token in lowered for token in ("humanfilesize", "getkey", "getstorageurl", "serverresponse", "getrootdir")):
        score -= 3
    return score


def _resolve_call(
    *,
    call_name: str,
    current_file_payload: dict,
    files_by_path: dict[str, dict],
    symbols_by_name: dict[str, list[dict]],
) -> tuple[dict | None, str | None]:
    def _choose_best_match(matches: list[dict], symbol_name: str) -> dict | None:
        if not matches:
            return None
        function_matches = [item for item in matches if item.get("kind") == "function" and item.get("name") == symbol_name]
        if len(function_matches) == 1:
            return function_matches[0]
        exported_function_matches = [item for item in function_matches if item.get("exported") is True]
        if len(exported_function_matches) == 1:
            return exported_function_matches[0]
        non_test = [item for item in function_matches if "/tests/" not in str(item.get("file") or "").replace("\\", "/")]
        if len(non_test) == 1:
            return non_test[0]
        if symbol_name.lower().startswith(("upload", "download", "putobject")) and non_test:
            return non_test[0]
        exported_any = [item for item in matches if item.get("exported") is True]
        if len(exported_any) == 1:
            return exported_any[0]
        return None

    parts = call_name.split(".")
    imports = current_file_payload.get("imports", [])
    if len(parts) == 1:
        symbol_name = parts[0]
        for symbol in current_file_payload.get("symbols", []):
            if symbol.get("kind") == "function" and symbol.get("name") == symbol_name:
                return symbol, None
        import_entry = next(
            (item for item in imports if isinstance(item, dict) and item.get("local") == symbol_name),
            None,
        )
        if import_entry and isinstance(import_entry.get("resolved_file"), str):
            resolved_file = import_entry["resolved_file"]
            target_file = files_by_path.get(resolved_file)
            if target_file:
                exact_name = [
                    symbol
                    for symbol in target_file.get("symbols", [])
                    if symbol.get("kind") == "function" and symbol.get("name") == symbol_name
                ]
                if len(exact_name) == 1:
                    return exact_name[0], None
                if len(exact_name) > 1:
                    exported_matches = [item for item in exact_name if item.get("exported") is True]
                    if len(exported_matches) == 1:
                        return exported_matches[0], None
                    return exact_name[0], None
                for symbol in target_file.get("symbols", []):
                    if symbol.get("name") == symbol_name and symbol.get("kind") in {"function", "class"}:
                        return symbol, None
                    if (
                        symbol.get("kind") == "class"
                        and import_entry.get("imported") == "default"
                        and symbol.get("export_kind") == "default"
                    ):
                        return symbol, None
            return None, "resolved_import_not_indexed"
        matches = symbols_by_name.get(symbol_name, [])
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            best = _choose_best_match(matches, symbol_name)
            if best is not None:
                return best, None
            return None, "ambiguous"
        return None, "not_found"

    root, member = parts[0], parts[1]
    import_entry = next(
        (item for item in imports if isinstance(item, dict) and item.get("local") == root),
        None,
    )
    if import_entry and isinstance(import_entry.get("resolved_file"), str):
        target_file = files_by_path.get(import_entry["resolved_file"])
        if target_file:
            for symbol in target_file.get("symbols", []):
                if (
                    symbol.get("kind") == "class_method"
                    and symbol.get("parent") == root
                    and symbol.get("name") == member
                ):
                    return symbol, None
            # fallback for function exported with alias
            for symbol in target_file.get("symbols", []):
                if symbol.get("kind") == "function" and symbol.get("name") == member:
                    return symbol, None
    matches = symbols_by_name.get(call_name, [])
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "not_found"


def build_layered_trace_expansion(
    *,
    repo_root: Path,
    matched_endpoint: dict,
    resolution: dict,
    primary_slice: dict,
    repo_index: dict,
    budgets: CallFollowBudgets,
) -> dict:
    """Follow bounded important project-local calls from the primary handler slice."""
    budgets_payload = asdict(budgets)
    files_by_path, symbols_by_name = _build_file_maps(repo_index)
    visited_functions: set[tuple[str, str]] = set()
    visited_files: set[str] = set()
    followed_calls: list[dict] = []
    skipped_calls: list[dict] = []
    unresolved_calls: list[dict] = []
    cycles: list[str] = []
    layers: list[dict] = []
    steps_total = 0

    primary_handler_name = (
        resolution.get("primary_handler", {}).get("normalized_handler")
        or resolution.get("normalized_handler")
        or "handler"
    )
    layers.append(
        {
            "depth": 1,
            "handler": primary_handler_name,
            "file": primary_slice.get("file"),
            "steps": primary_slice.get("statements", []),
        }
    )
    steps_total += len(primary_slice.get("statements", []))
    visited_files.add(primary_slice.get("file", ""))
    visited_functions.add((primary_slice.get("file", ""), primary_handler_name))

    if budgets.max_depth < 2:
        return {
            "target_route": matched_endpoint,
            "budgets": budgets_payload,
            "layers": layers,
            "followed_calls": followed_calls,
            "skipped_calls": skipped_calls,
            "unresolved_calls": unresolved_calls,
            "cycles": cycles,
            "summary": {
                "functions_followed": 0,
                "files_visited": len([x for x in visited_files if x]),
                "steps_added": steps_total,
            },
        }

    current_file = primary_slice.get("file")
    current_file_payload = files_by_path.get(current_file or "")
    if current_file_payload is None:
        return {
            "target_route": matched_endpoint,
            "budgets": budgets_payload,
            "layers": layers,
            "followed_calls": followed_calls,
            "skipped_calls": skipped_calls,
            "unresolved_calls": unresolved_calls,
            "cycles": cycles,
            "summary": {
                "functions_followed": 0,
                "files_visited": len([x for x in visited_files if x]),
                "steps_added": steps_total,
            },
        }

    candidates: list[tuple[int, str, dict]] = []
    for stmt in primary_slice.get("statements", []):
        for call_name in _extract_calls_from_statement_text(stmt.get("text", "")):
            if not _is_followable_call_name(call_name):
                skipped_calls.append({"call": call_name, "reason": "not_followable"})
                continue
            score = _importance_score(call_name, stmt)
            candidates.append((score, call_name, stmt))
    candidates.sort(key=lambda item: item[0], reverse=True)

    processed_calls = 0
    for score, call_name, stmt in candidates:
        if processed_calls >= budgets.max_calls_per_function:
            break
        processed_calls += 1
        skip_reason = _skip_reason(call_name, stmt)
        if skip_reason is not None:
            skipped_calls.append({"call": call_name, "reason": skip_reason})
            continue
        if score < 0:
            skipped_calls.append({"call": call_name, "reason": "low_importance"})
            continue
        symbol, reason = _resolve_call(
            call_name=call_name,
            current_file_payload=current_file_payload,
            files_by_path=files_by_path,
            symbols_by_name=symbols_by_name,
        )
        if symbol is None:
            unresolved_calls.append({"call": call_name, "reason": reason or "not_found"})
            continue

        symbol_file = symbol.get("file")
        if not isinstance(symbol_file, str):
            unresolved_calls.append({"call": call_name, "reason": "symbol_missing_file"})
            continue
        symbol_name = symbol.get("qualified_name") or symbol.get("name") or call_name
        key = (symbol_file, str(symbol_name))
        if key in visited_functions:
            cycles.append(f"call_following_cycle_skipped={symbol_name}")
            continue
        if len(visited_functions) >= budgets.max_functions:
            skipped_calls.append({"call": call_name, "reason": "max_functions"})
            continue
        if len([x for x in visited_files if x]) >= budgets.max_files and symbol_file not in visited_files:
            skipped_calls.append({"call": call_name, "reason": "max_files"})
            continue

        slice_payload = slice_resolved_handler_body(
            repo_root=repo_root,
            handler_name=str(symbol_name),
            symbol=symbol,
            language=str(symbol.get("language") or "unknown"),
        )
        if slice_payload is None:
            unresolved_calls.append({"call": call_name, "reason": "slice_unavailable"})
            continue

        followed_calls.append(
            {
                "call": call_name,
                "resolved_to": symbol_name,
                "file": symbol_file,
                "importance": score,
                "called_from_statement": stmt.get("text"),
            }
        )
        visited_functions.add(key)
        visited_files.add(symbol_file)
        steps = slice_payload.get("statements", [])
        remaining = budgets.max_steps - steps_total
        if remaining <= 0:
            skipped_calls.append({"call": call_name, "reason": "max_steps"})
            break
        if len(steps) > remaining:
            steps = steps[:remaining]
            skipped_calls.append({"call": call_name, "reason": f"truncated_to_{remaining}_steps"})
        steps_total += len(steps)
        layers.append(
            {
                "depth": 2,
                "handler": str(symbol_name),
                "file": symbol_file,
                "called_from": primary_handler_name,
                "steps": steps,
            }
        )

    return {
        "target_route": matched_endpoint,
        "budgets": budgets_payload,
        "layers": layers,
        "followed_calls": followed_calls,
        "skipped_calls": skipped_calls,
        "unresolved_calls": unresolved_calls,
        "cycles": cycles,
        "summary": {
            "functions_followed": len(followed_calls),
            "files_visited": len([x for x in visited_files if x]),
            "steps_added": steps_total,
        },
    }
