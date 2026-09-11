"""Shared pieces between `layer2_declaration_bridge.py` (Python, `ast`-based)
and `layer2_treesitter_bridge.py` (TypeScript/Java/Go, tree-sitter-based):
the feature flag, `SymbolIdentity` qualified-name bridging, and citation
verification. Language-specific AST/tree-sitter extraction stays in each
bridge module — only the parts that never depended on which parser produced
a candidate edge live here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

LAYER2_ENV_VAR = "SYDES_LAYER2_GENERIC_EDGES"
LAYER2_SOURCE = "layer2_declaration_reference"

_SYMBOL_KINDS = ("function", "class", "class_method")


def layer2_generic_edges_enabled() -> bool:
    """Re-read on every call (not cached at import time) so it can be
    toggled mid-process, e.g. in tests — same pattern as
    `observability/trace.py`'s `SYDES_TRACE_DIR` check."""
    return os.environ.get(LAYER2_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}


def files_by_path(symbol_index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for repo_payload in symbol_index.get("repos", []) or []:
        for file_item in repo_payload.get("files", []) or []:
            path = file_item.get("path")
            if isinstance(path, str):
                result[path] = file_item
    return result


def qualified_name_for(file: str, short_name: str, by_path: dict[str, dict[str, Any]]) -> str:
    """Look up the backend's own canonical qualified name for (file,
    short_name) from the already-computed symbol index, so a Layer 2 edge
    resolves to the SAME `SymbolIdentity` tier-1 key the changed symbol
    itself already carries (`change.symbols[*].cbm_qualified_name`) --
    without this, an edge with no qualified name resolves at tier 3
    (file+short_name) while the changed symbol resolves at tier 1 (its own
    canonical qualified name), two different keys for the same real symbol
    that would silently never connect in `_FactIndex`. Found empirically
    validating the Python integration against a real repo; applies
    identically regardless of source language."""
    file_item = by_path.get(file)
    if file_item is None:
        return ""
    for entry in file_item.get("symbols", []) or []:
        if entry.get("name") == short_name:
            qualified = entry.get("cbm_qualified_name") or entry.get("qualified_name")
            if isinstance(qualified, str) and qualified:
                return qualified
    return ""


def defining_file_for(file: str, symbol_name: str, by_path: dict[str, dict[str, Any]]) -> str:
    """If `file` re-exports `symbol_name` via a one-hop import rather than
    defining it itself, follow that one hop to the file that actually
    defines it. Bounded to one hop; see `layer2_declaration_bridge.py`'s
    fuller docstring on why this matters for identity, not just style."""
    file_item = by_path.get(file)
    if file_item is None:
        return file
    defined = {
        s.get("name") for s in file_item.get("symbols", []) or []
        if s.get("kind") in _SYMBOL_KINDS
    }
    if symbol_name in defined:
        return file
    import_entry = next(
        (imp for imp in file_item.get("imports", []) or []
         if imp.get("local") == symbol_name and imp.get("resolved_file")),
        None,
    )
    if import_entry is None:
        return file
    return str(import_entry["resolved_file"])


# --- citation verification (mandatory gate; language-agnostic) -------------

_CONTEXT_WINDOW = 2
_MAX_STATEMENT_LINES = 15


def _statement_span(lines: list[str], line_idx: int) -> tuple[int, int]:
    start = max(0, line_idx - _CONTEXT_WINDOW)
    depth = 0
    idx = line_idx
    while idx < len(lines) and idx < line_idx + _MAX_STATEMENT_LINES:
        depth += lines[idx].count("(") + lines[idx].count("[") - lines[idx].count(")") - lines[idx].count("]")
        idx += 1
        if depth <= 0:
            break
    end = min(len(lines), max(idx, line_idx + 1))
    return start, end


def _whole_word_present(name: str, text: str) -> bool:
    if not name:
        return False
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def citation_verified(edge: dict[str, Any], repo_root: Path, source_cache: dict[str, list[str]]) -> bool:
    """Re-read the actual cited file/line and confirm the claimed
    `used_symbol` really appears there, as a whole identifier, within its
    own (possibly multi-line) statement span. A fabricated or stale
    citation fails here, not a hypothesis -- a direct re-read of the file
    this edge itself names. Works identically for any source language:
    the check is purely textual."""
    user_file = edge.get("user_file")
    line = edge.get("line")
    used_symbol = edge.get("used_symbol")
    if not user_file or not used_symbol or not isinstance(line, int):
        return False
    lines = source_cache.get(user_file)
    if lines is None:
        path = repo_root / user_file
        if not path.is_file():
            return False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        source_cache[user_file] = lines
    if not (1 <= line <= len(lines)):
        return False
    start, end = _statement_span(lines, line - 1)
    window_text = "\n".join(lines[start:end])
    return _whole_word_present(used_symbol, window_text)
