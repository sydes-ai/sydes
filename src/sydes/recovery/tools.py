"""Generic, repository-scoped read/search tools for the recovery agent.

Deliberately not CBM: these are plain filesystem operations (read a file,
grep for text, list a directory) with no framework or language awareness at
all. This is what lets the agent search "outside CBM-selected files" — its
only boundary is the repository root itself, enforced by
`_resolve_within_repo` on every call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

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

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
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
