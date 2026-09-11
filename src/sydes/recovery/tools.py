"""Generic, repository-scoped read/search tools for the recovery agent.

The filesystem tools here (read a file, grep for text, list a directory)
have no framework or language awareness at all — this is what lets the
agent search "outside CBM-selected files", bounded only by the repository
root itself (`_resolve_within_repo`). `graph`, when supplied, additionally
offers CBM-backed callers/callees/symbol-search queries
(`sydes.recovery.graph_tools.CBMGraphTools`) — production-grade for the
lexically resolvable part of the call graph, and optional: every caller of
this class works identically with `graph=None`, just without those three
tools available.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sydes.recovery.graph_tools import CBMGraphTools

#: Bounds on every tool's output, independent of the recovery budget's turn
#: count — a single huge file or a greedy grep must not blow past the
#: prompt budget on its own.
_MAX_READ_CHARS = 6000
_MAX_GREP_MATCHES = 40
_MAX_LIST_ENTRIES = 200


class RepoToolError(RuntimeError):
    """A tool call was rejected or failed — reported back to the agent as
    an observation, never raised through the agent loop."""


@dataclass(frozen=True)
class ToolCallRecord:
    """One executed tool call, kept for the evaluation harness's
    files-searched/read accounting — not shown to the model."""

    tool: str
    args: dict[str, object]
    result_chars: int
    ok: bool


def _resolve_within_repo(repo_root: Path, rel_path: str) -> Path:
    candidate = (repo_root / rel_path).resolve()
    repo_resolved = repo_root.resolve()
    if repo_resolved not in candidate.parents and candidate != repo_resolved:
        raise RepoToolError(f"path {rel_path!r} escapes the repository root")
    return candidate


class RepoTools:
    """Bound to one repository root for the duration of a recovery run."""

    def __init__(self, repo_root: Path, *, graph: "CBMGraphTools | None" = None) -> None:
        # Resolved once here so every method's own resolved paths (via
        # `_resolve_within_repo`, which always returns an absolute,
        # symlink-resolved path) stay consistent with this root for
        # `relative_to` — a caller passing a relative root (e.g. `Path(".")`,
        # what every `--repo name=.` CLI invocation does) would otherwise
        # make `list_directory`'s `e.relative_to(self._repo_root)` raise
        # ValueError on its first entry (found empirically: a repo whose
        # first alphabetically-sorted dotfile is `.env.sample` crashed here,
        # which looked file-specific but was really "any entry, first repo
        # root listing").
        self._repo_root = repo_root.resolve()
        self._graph = graph
        self.calls: list[ToolCallRecord] = []

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        try:
            resolved = _resolve_within_repo(self._repo_root, path)
            if not resolved.is_file():
                raise RepoToolError(f"{path!r} is not a file")
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except (OSError, RepoToolError) as exc:
            self.calls.append(ToolCallRecord("read_file", {"path": path}, 0, False))
            return f"ERROR: {exc}"

        lines = text.splitlines()
        if start_line is not None or end_line is not None:
            lo = max(1, start_line or 1)
            hi = min(len(lines), end_line or len(lines))
            numbered = [f"{n}: {lines[n - 1]}" for n in range(lo, hi + 1)]
        else:
            numbered = [f"{n}: {line}" for n, line in enumerate(lines, start=1)]
        out = "\n".join(numbered)[:_MAX_READ_CHARS]
        self.calls.append(
            ToolCallRecord("read_file", {"path": path, "start_line": start_line, "end_line": end_line}, len(out), True)
        )
        return out

    def search_text(self, pattern: str, path_glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            self.calls.append(ToolCallRecord("search_text", {"pattern": pattern}, 0, False))
            return f"ERROR: invalid pattern: {exc}"

        glob = path_glob or "**/*"
        matches: list[str] = []
        try:
            for candidate in sorted(self._repo_root.glob(glob)):
                if not candidate.is_file():
                    continue
                if len(matches) >= _MAX_GREP_MATCHES:
                    break
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = candidate.relative_to(self._repo_root)
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if len(matches) >= _MAX_GREP_MATCHES:
                        break
                    if regex.search(line):
                        matches.append(f"{rel}:{line_no}: {line.strip()[:200]}")
        except (OSError, ValueError) as exc:
            self.calls.append(ToolCallRecord("search_text", {"pattern": pattern}, 0, False))
            return f"ERROR: {exc}"

        out = "\n".join(matches) if matches else "(no matches)"
        self.calls.append(
            ToolCallRecord("search_text", {"pattern": pattern, "path_glob": path_glob}, len(out), True)
        )
        return out

    def list_directory(self, path: str = ".") -> str:
        try:
            resolved = _resolve_within_repo(self._repo_root, path)
            if not resolved.is_dir():
                raise RepoToolError(f"{path!r} is not a directory")
            entries = sorted(resolved.iterdir())[:_MAX_LIST_ENTRIES]
        except (OSError, RepoToolError) as exc:
            self.calls.append(ToolCallRecord("list_directory", {"path": path}, 0, False))
            return f"ERROR: {exc}"
        out = "\n".join(
            f"{'d' if e.is_dir() else 'f'} {e.relative_to(self._repo_root)}" for e in entries
        )
        self.calls.append(ToolCallRecord("list_directory", {"path": path}, len(out), True))
        return out

    def trace_callers(self, symbol: str, depth: int = 3) -> str:
        if self._graph is None:
            return "ERROR: CBM graph tools not available in this run"
        out = self._graph.trace_callers(symbol, depth=depth)
        self.calls.append(ToolCallRecord("trace_callers", {"symbol": symbol, "depth": depth}, len(out), not out.startswith("ERROR")))
        return out

    def trace_callees(self, symbol: str, depth: int = 3) -> str:
        if self._graph is None:
            return "ERROR: CBM graph tools not available in this run"
        out = self._graph.trace_callees(symbol, depth=depth)
        self.calls.append(ToolCallRecord("trace_callees", {"symbol": symbol, "depth": depth}, len(out), not out.startswith("ERROR")))
        return out

    def search_symbol(self, name_pattern: str, file_pattern: str | None = None) -> str:
        if self._graph is None:
            return "ERROR: CBM graph tools not available in this run"
        out = self._graph.search_symbol(name_pattern, file_pattern=file_pattern)
        self.calls.append(ToolCallRecord("search_symbol", {"name_pattern": name_pattern, "file_pattern": file_pattern}, len(out), not out.startswith("ERROR")))
        return out
