"""Optional CBM-backed graph queries for the recovery agent -- callers,
callees, and symbol search over an already-built Codebase Memory index,
offered alongside the plain-filesystem tools in `sydes.recovery.tools`.

This wraps `sydes.code_intelligence.cbm_client.CBMClient` read-only; it
never modifies CBM's own indexing or query logic. CBM's `trace_path` is
"preferred over hand-rolled BFS" for exactly the reason this module exists:
it already bounds depth, excludes test files, paginates, and reports exact
totals for the *lexically resolvable* part of the call graph -- ordinary
function/method calls. It does not (measured directly, not assumed) resolve
decorator/DI-mediated wiring such as an HTTP route's mount or a message-bus
dispatch; `sydes.recovery.entrypoint_heuristic` is the piece that covers the
entrypoint half of that gap.

Every method here degrades to an "ERROR: ..." observation string rather
than raising: CBM being unavailable (not installed, index missing,
provider down, or the repo was never indexed with the `cbm` backend) must
never fail a recovery run -- it just means these tools return nothing
useful and the agent falls back to `read_file`/`search_text`.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.code_intelligence.cbm_client import CBMClient

#: Same spirit as `sydes.recovery.tools`'s `_MAX_READ_CHARS` -- one graph
#: query must not blow past the prompt budget on its own.
_MAX_OBSERVATION_CHARS = 6000


class CBMGraphTools:
    """Bound to one repository root; lazily connects to CBM on first use
    and remembers a connection failure so later calls don't repeat a slow,
    doomed spawn attempt on every single tool call in the run."""

    def __init__(self, repo_root: Path, *, client_factory=CBMClient.spawn) -> None:
        self._repo_root = repo_root
        self._client_factory = client_factory
        self._client: CBMClient | None = None
        self._project: str | None = None
        self._unavailable_reason: str | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _ensure_project(self) -> str | None:
        if self._project is not None:
            return self._project
        if self._unavailable_reason is not None:
            return None
        try:
            client = self._client_factory()
            payload = client.index_repository(str(self._repo_root), mode="fast")
        except (CodeIntelligenceError, OSError) as exc:
            self._unavailable_reason = str(exc)
            return None
        self._client = client
        self._project = str(payload["project"])
        return self._project

    def _unavailable(self) -> str:
        return f"ERROR: CBM graph unavailable ({self._unavailable_reason or 'could not index this repository'})"

    def trace_callers(self, symbol: str, *, depth: int = 3) -> str:
        """Inbound CALLS closure for a symbol -- who calls it, transitively.

        Only follows literal, lexically-visible calls. A symbol reached only
        through a decorator/DI/message-bus binding will report zero callers
        here even though it IS reachable at runtime -- that is the known,
        measured boundary of this tool, not a bug to route around by
        guessing.
        """
        project = self._ensure_project()
        if project is None or self._client is None:
            return self._unavailable()
        try:
            payload = self._client.trace_callers(project, symbol, depth=depth)
        except CodeIntelligenceError as exc:
            return f"ERROR: {exc}"
        return json.dumps(payload, separators=(",", ":"))[:_MAX_OBSERVATION_CHARS]

    def trace_callees(self, symbol: str, *, depth: int = 3) -> str:
        """Outbound CALLS closure for a symbol -- what it calls, transitively."""
        project = self._ensure_project()
        if project is None or self._client is None:
            return self._unavailable()
        try:
            payload = self._client.trace_callees(project, symbol, depth=depth)
        except CodeIntelligenceError as exc:
            return f"ERROR: {exc}"
        return json.dumps(payload, separators=(",", ":"))[:_MAX_OBSERVATION_CHARS]

    def search_symbol(self, name_pattern: str, *, file_pattern: str | None = None) -> str:
        """Find symbols by name/regex across the whole repository -- for
        locating a symbol whose exact qualified name or file isn't known
        yet, faster and more precise than grepping file contents."""
        project = self._ensure_project()
        if project is None or self._client is None:
            return self._unavailable()
        try:
            payload = self._client.search_symbols(
                project, name_pattern=name_pattern, file_pattern=file_pattern, limit=25,
            )
        except CodeIntelligenceError as exc:
            return f"ERROR: {exc}"
        return json.dumps(payload, separators=(",", ":"))[:_MAX_OBSERVATION_CHARS]
