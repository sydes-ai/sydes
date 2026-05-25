"""Resolve route handler hints to implementation symbols using symbol index."""

from __future__ import annotations

from pathlib import Path
import re

from sydes.core.models import EndpointCandidate


def _split_args(expr: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in expr:
        if quote is not None:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                args.append(part)
            buf = []
            continue
        buf.append(ch)
    part = "".join(buf).strip()
    if part:
        args.append(part)
    return args


def _unwrap_handler(expr: str) -> tuple[str, list[str]]:
    wrappers: list[str] = []
    current = expr.strip()
    while True:
        match = re.fullmatch(r"([A-Za-z_]\w*)\((.*)\)", current)
        if not match:
            break
        wrapper = match.group(1)
        inner = match.group(2).strip()
        if not inner:
            break
        inner_args = _split_args(inner)
        if not inner_args:
            break
        wrappers.append(wrapper)
        current = inner_args[-1].strip()
    return current, wrappers


def extract_handler_candidates(handler_hint: str | None) -> dict:
    """Extract ordered handler-like candidates from a route handler hint."""
    if not handler_hint:
        return {"raw": handler_hint, "candidates": [], "primary": None}

    raw = handler_hint.strip()
    if not raw:
        return {"raw": handler_hint, "candidates": [], "primary": None}

    args = _split_args(raw)
    if len(args) <= 1:
        args = [raw]

    candidates: list[dict] = []
    for idx, part in enumerate(args):
        normalized, wrappers = _unwrap_handler(part)
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", normalized):
            candidates.append(
                {
                    "hint": part.strip(),
                    "normalized": normalized,
                    "wrappers": wrappers,
                    "position": idx,
                }
            )
    primary = candidates[-1] if candidates else None
    return {"raw": raw, "candidates": candidates, "primary": primary}


def _build_file_maps(handler_symbol_index: dict) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    if "files" in handler_symbol_index and isinstance(handler_symbol_index.get("files"), list):
        index_payload = handler_symbol_index
    elif isinstance(handler_symbol_index.get("index"), dict):
        index_payload = handler_symbol_index["index"]
    elif isinstance(handler_symbol_index.get("handler_symbol_index"), dict):
        index_payload = handler_symbol_index["handler_symbol_index"]
    else:
        index_payload = handler_symbol_index
    files_by_path: dict[str, dict] = {}
    symbols_by_name: dict[str, list[dict]] = {}
    for file_item in index_payload.get("files", []):
        path = file_item.get("path")
        if not isinstance(path, str):
            continue
        files_by_path[path] = file_item
        for symbol in file_item.get("symbols", []):
            if not isinstance(symbol, dict):
                continue
            name = symbol.get("name")
            if isinstance(name, str) and name:
                symbols_by_name.setdefault(name, []).append(symbol)
            qualified = symbol.get("qualified_name")
            if isinstance(qualified, str) and qualified:
                symbols_by_name.setdefault(qualified, []).append(symbol)
    return files_by_path, symbols_by_name


def _resolve_from_imports(
    route_file_payload: dict,
    normalized: str,
    files_by_path: dict[str, dict],
) -> tuple[dict | None, list[dict]]:
    chain: list[dict] = []
    parts = normalized.split(".")
    root_symbol = parts[0]
    target_member = parts[-1] if len(parts) > 1 else None
    imports = route_file_payload.get("imports", [])
    import_entry = None
    for item in imports:
        if isinstance(item, dict) and item.get("local") == root_symbol:
            import_entry = item
            break
    if import_entry is None:
        return None, chain

    chain.append(
        {
            "kind": "import_resolution",
            "from_file": route_file_payload.get("path"),
            "local": root_symbol,
            "source": import_entry.get("source"),
            "resolved_file": import_entry.get("resolved_file"),
        }
    )
    resolved_file = import_entry.get("resolved_file")
    if not isinstance(resolved_file, str):
        return None, chain
    target_file = files_by_path.get(resolved_file)
    if not target_file:
        return None, chain
    chain.append(
        {
            "kind": "resolved_file_index_check",
            "resolved_file": resolved_file,
            "file_indexed": True,
            "symbols_in_file": len(target_file.get("symbols", [])),
            "class_candidates": [
                item.get("name")
                for item in target_file.get("symbols", [])
                if isinstance(item, dict) and item.get("kind") == "class"
            ][:12],
        }
    )

    if target_member is None:
        for symbol in target_file.get("symbols", []):
            if symbol.get("name") == root_symbol and symbol.get("kind") == "function":
                return symbol, chain
        for symbol in target_file.get("symbols", []):
            if symbol.get("name") == root_symbol and symbol.get("kind") == "class":
                return symbol, chain
        return None, chain

    for symbol in target_file.get("symbols", []):
        if (
            symbol.get("kind") == "class_method"
            and symbol.get("parent") == root_symbol
            and symbol.get("name") == target_member
        ):
            return symbol, chain
    for symbol in target_file.get("symbols", []):
        if (
            symbol.get("kind") == "class_method"
            and symbol.get("qualified_name") == f"{root_symbol}.{target_member}"
        ):
            return symbol, chain
    chain.append(
        {
            "kind": "method_candidates",
            "root_symbol": root_symbol,
            "member": target_member,
            "candidates": [
                item.get("qualified_name") or f"{item.get('parent')}.{item.get('name')}"
                for item in target_file.get("symbols", [])
                if isinstance(item, dict) and item.get("kind") == "class_method" and item.get("parent") == root_symbol
            ][:20],
        }
    )
    return None, chain


def _to_candidate(symbol: dict) -> dict:
    return {
        "qualified_name": symbol.get("qualified_name") or symbol.get("name"),
        "kind": symbol.get("kind"),
        "file": symbol.get("file"),
        "line": symbol.get("line"),
        "start_line": symbol.get("start_line"),
        "end_line": symbol.get("end_line"),
        "static": symbol.get("static"),
        "async": symbol.get("async"),
    }


def resolve_handler_reference(
    endpoint: EndpointCandidate,
    handler_symbol_index: dict | None,
) -> dict:
    """Resolve endpoint handler hints to implementation symbols."""
    hint = endpoint.handler
    extracted = extract_handler_candidates(hint)
    result: dict = {
        "handler_hint": hint,
        "route_file": endpoint.file,
        "resolved": False,
        "primary_handler": None,
        "prehandlers": [],
        "unresolved_handlers": [],
        "resolution_chain": [],
        "confidence": 0.0,
    }
    if not handler_symbol_index:
        result["unresolved_handlers"] = extracted["candidates"]
        return result

    files_by_path, symbols_by_name = _build_file_maps(handler_symbol_index)
    route_file = Path(endpoint.file).as_posix()
    route_file_payload = files_by_path.get(route_file)

    resolved_items: list[dict] = []
    unresolved_items: list[dict] = []

    for candidate in extracted["candidates"]:
        normalized = candidate["normalized"]
        chain: list[dict] = [
            {"kind": "route_handler_hint", "value": candidate["hint"]},
        ]
        if candidate["wrappers"]:
            for wrapper in candidate["wrappers"]:
                chain.append({"kind": "unwrap_handler_wrapper", "value": wrapper})
            chain.append({"kind": "normalized_handler", "value": normalized})

        symbol = None
        if route_file_payload is not None:
            # Same file function
            for local_symbol in route_file_payload.get("symbols", []):
                if (
                    local_symbol.get("kind") == "function"
                    and local_symbol.get("name") == normalized
                ):
                    symbol = local_symbol
                    chain.append(
                        {
                            "kind": "same_file_symbol_match",
                            "file": route_file_payload.get("path"),
                            "symbol": normalized,
                        }
                    )
                    break
            if symbol is None:
                symbol, import_chain = _resolve_from_imports(
                    route_file_payload, normalized, files_by_path
                )
                chain.extend(import_chain)

        if symbol is None:
            matches = symbols_by_name.get(normalized, [])
            if "." in normalized:
                matches = [item for item in matches if item.get("qualified_name") == normalized]
            if len(matches) == 1:
                symbol = matches[0]
                chain.append({"kind": "symbol_match", "qualified_name": normalized})
            elif len(matches) > 1:
                unresolved_items.append(
                    {
                        "normalized_handler": normalized,
                        "reason": "ambiguous",
                        "candidates": [_to_candidate(item) for item in matches[:10]],
                        "resolution_chain": chain,
                    }
                )
                continue

        if symbol is None:
            parts = normalized.split(".")
            root_symbol = parts[0] if parts else normalized
            member_name = parts[-1] if len(parts) > 1 else None
            import_source = None
            resolved_file = None
            file_indexed = False
            symbols_in_resolved_file: list[str] = []
            class_candidates: list[str] = []
            method_candidates: list[str] = []
            if route_file_payload is not None:
                for item in route_file_payload.get("imports", []):
                    if isinstance(item, dict) and item.get("local") == root_symbol:
                        import_source = item.get("source")
                        resolved_file = item.get("resolved_file")
                        if isinstance(resolved_file, str):
                            target_file = files_by_path.get(resolved_file)
                            file_indexed = target_file is not None
                            if target_file is not None:
                                symbols_in_resolved_file = [
                                    s.get("name") if isinstance(s, dict) else None
                                    for s in target_file.get("symbols", [])
                                ]
                                class_candidates = [
                                    s.get("name")
                                    for s in target_file.get("symbols", [])
                                    if isinstance(s, dict) and s.get("kind") == "class"
                                ]
                                if member_name:
                                    method_candidates = [
                                        s.get("qualified_name") or f"{s.get('parent')}.{s.get('name')}"
                                        for s in target_file.get("symbols", [])
                                        if isinstance(s, dict)
                                        and s.get("kind") == "class_method"
                                        and (s.get("parent") == root_symbol or s.get("qualified_name") == normalized)
                                    ]
                        break
            unresolved_items.append(
                {
                    "normalized_handler": normalized,
                    "reason": "not_found",
                    "diagnostics": {
                        "normalized": normalized,
                        "route_file": route_file_payload.get("path") if isinstance(route_file_payload, dict) else route_file,
                        "root_symbol": root_symbol,
                        "member_name": member_name,
                        "import_source": import_source,
                        "resolved_file": resolved_file,
                        "file_indexed": file_indexed,
                        "symbols_in_resolved_file": [x for x in symbols_in_resolved_file if isinstance(x, str)][:30],
                        "class_candidates": [x for x in class_candidates if isinstance(x, str)][:20],
                        "method_candidates": [x for x in method_candidates if isinstance(x, str)][:20],
                    },
                    "resolution_chain": chain,
                }
            )
            continue

        resolved_items.append(
            {
                "handler_hint": candidate["hint"],
                "normalized_handler": normalized,
                "resolved": True,
                "symbol": _to_candidate(symbol),
                "resolution_chain": chain,
                "wrappers": candidate["wrappers"],
                "confidence": 1.0 if route_file_payload is not None else 0.8,
            }
        )

    if resolved_items:
        result["resolved"] = True
        result["primary_handler"] = resolved_items[-1]
        result["prehandlers"] = resolved_items[:-1]
        result["confidence"] = result["primary_handler"].get("confidence", 0.8)
    result["unresolved_handlers"] = unresolved_items
    if extracted["primary"] is not None:
        result["normalized_handler"] = extracted["primary"]["normalized"]
    if result["primary_handler"] is not None:
        result["resolution_chain"] = result["primary_handler"].get("resolution_chain", [])
    return result
