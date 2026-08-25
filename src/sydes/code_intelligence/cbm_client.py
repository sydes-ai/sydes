"""A persistent session against a Codebase Memory server.

Every CBM query used to cost a process spawn. The binary is ~283 MB and its
one-shot CLI path re-runs daemon bootstrap, project-lock acquisition and
version-cohort coordination on every invocation, so a trivial query cost
seconds. A structural build issues several queries per repository, and the
overhead dominated everything else Sydes did.

CBM also speaks MCP over stdio. Spawning it once and holding the pipe turns
that per-call bootstrap into a one-time session cost: measured here, session
setup is ~1.5 s and each subsequent query is ~12 ms.

This module is the only place in Sydes that knows how CBM is invoked. Above it
sits `CBMCodeIntelligence`, which asks for structural facts and neither knows
nor cares that a JSON-RPC frame crossed a pipe. Below it is a subprocess whose
lifetime this class owns.

Two rules shape the error handling. A CBM that cannot answer raises, because a
verdict assembled from facts the operator did not ask for is unreadable — there
is no fallback to the CLI and none to the native backend. And the typed methods
below prefer CBM's own higher-level tools (`search_graph`, `trace_path`,
`get_architecture`) over hand-written Cypher, so Sydes is coupled to a
documented tool surface rather than to a query dialect of a pre-1.0 tool.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Protocol

from sydes.code_intelligence.base import CodeIntelligenceError

#: Overrides executable discovery, for a pinned or non-PATH install.
CBM_EXECUTABLE_ENV_VAR = "SYDES_CBM_EXECUTABLE"

_DEFAULT_EXECUTABLE = "codebase-memory-mcp"

#: MCP revision this client negotiates. CBM accepts it; a server that does not
#: will fail the handshake loudly rather than degrade.
_PROTOCOL_VERSION = "2024-11-05"

_HANDSHAKE_TIMEOUT_SECONDS = 120.0
_DEFAULT_CALL_TIMEOUT_SECONDS = 300.0
#: Indexing walks and parses a whole repository, so it gets its own budget.
_INDEX_TIMEOUT_SECONDS = 1800.0

#: How much of the child's stderr to keep for diagnostics — enough to carry
#: a bootstrap/native-runtime-download failure message, not a log dump.
_STDERR_TAIL_LINES = 20
_STDERR_LINE_MAX_CHARS = 300

#: Bulk Cypher sweeps (`all_symbols`, `all_imports`, `all_call_edges`,
#: `all_usage_edges`) page at this row count — conservative enough that a
#: typical repository's whole sweep fits in one page well under CBM's ~10 MB
#: transport cap, and large enough that pagination costs nothing extra for
#: repositories that don't need it.
_BULK_QUERY_PAGE_SIZE = 4000
#: Defensive cap on pages, so a server that never signals "last page" (by
#: consistently returning a full page) cannot loop forever.
_BULK_QUERY_MAX_PAGES = 500


def resolve_executable(candidate: str | None = None) -> str:
    """Locate the CBM binary, or say plainly that it is absent."""
    name = (candidate or os.environ.get(CBM_EXECUTABLE_ENV_VAR, "").strip()
            or _DEFAULT_EXECUTABLE)
    resolved = shutil.which(name) or (name if Path(name).is_file() else None)
    if resolved is None:
        raise CodeIntelligenceError(
            f"Codebase Memory executable {name!r} was not found. Install it, set "
            f"{CBM_EXECUTABLE_ENV_VAR}, or select the native backend explicitly "
            "with SYDES_CODE_INTELLIGENCE=native."
        )
    return resolved


@dataclass
class ClientMetrics:
    """What the session cost, for the diagnostics section."""

    session_start_ms: float = 0.0
    calls: int = 0
    call_ms: float = 0.0
    #: Per-tool call counts, so a slow build can be attributed.
    calls_by_tool: dict[str, int] = field(default_factory=dict)

    def record(self, tool: str, elapsed_ms: float) -> None:
        self.calls += 1
        self.call_ms += elapsed_ms
        self.calls_by_tool[tool] = self.calls_by_tool.get(tool, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_start_ms": round(self.session_start_ms, 1),
            "calls": self.calls,
            "call_ms": round(self.call_ms, 1),
            "mean_call_ms": round(self.call_ms / self.calls, 1) if self.calls else 0.0,
            "calls_by_tool": dict(sorted(self.calls_by_tool.items())),
        }


class CBMSession(Protocol):
    """The transport a `CBMClient` drives.

    Extracted so tests can supply a fake without a live daemon, and so the
    typed query layer above is testable in isolation from process management.
    """

    def call_tool(self, tool: str, arguments: dict[str, Any],
                  *, timeout: float | None = None) -> dict[str, Any]:
        """Invoke one CBM tool and return its structured payload."""
        ...

    def close(self) -> None:
        """Release the session. Safe to call more than once."""
        ...


class StdioMCPSession:
    """One long-lived CBM process, spoken to over MCP/JSON-RPC on stdio."""

    def __init__(self, executable: str) -> None:
        self._executable = executable
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 0
        # One structural build issues several queries; a lock keeps request ids
        # and pipe reads paired even if a caller ever parallelises them.
        self._lock = threading.Lock()
        self.metrics = ClientMetrics()
        # The public `codebase-memory-mcp` wrapper downloads and caches the
        # native runtime on first use; on a fresh machine that first launch
        # can take real time. Sydes does not duplicate that bootstrap — it
        # only keeps a bounded tail of the child's stderr so a bootstrap
        # failure (download/verification/native-launch) is reported with the
        # underlying reason rather than a bare "connection closed".
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        #: `initialize`'s `serverInfo`, when the server reports one — the
        #: least invasive way to see the running CBM version: it rides the
        #: handshake Sydes already performs, no extra process or request.
        self.server_info: dict[str, Any] | None = None
        self._start()

    # -- lifecycle --------------------------------------------------------

    def _start(self) -> None:
        started = time.perf_counter()
        try:
            self._process = subprocess.Popen(
                [self._executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CodeIntelligenceError(
                f"Could not start Codebase Memory at {self._executable!r}: {exc}"
            ) from exc

        # Drained continuously on a daemon thread — the pipe must never fill
        # and block the child mid-query (CBM logs progress to stderr), but a
        # bounded recent tail is worth keeping for exactly the startup
        # failure this task cares about.
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "sydes", "version": "1"},
                },
                timeout=_HANDSHAKE_TIMEOUT_SECONDS,
            )
        except CodeIntelligenceError as exc:
            raise CodeIntelligenceError(
                f"Sydes could not initialize code intelligence (Codebase Memory).\n{exc}"
                f"{self._stderr_context()}"
            ) from exc
        self.server_info = result.get("serverInfo") if isinstance(result, dict) else None
        self._notify("notifications/initialized", {})
        self.metrics.session_start_ms = (time.perf_counter() - started) * 1000.0

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                text = line.rstrip("\n")
                if text:
                    self._stderr_tail.append(text[:_STDERR_LINE_MAX_CHARS])
        except (OSError, ValueError):
            # The pipe went away (process killed mid-read); nothing to drain.
            pass

    def _stderr_context(self) -> str:
        if not self._stderr_tail:
            return ""
        return "\n" + "\n".join(self._stderr_tail)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def __enter__(self) -> StdioMCPSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- JSON-RPC ---------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CodeIntelligenceError("The Codebase Memory session is not running")
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodeIntelligenceError(
                f"The Codebase Memory session closed unexpectedly: {exc}"
            ) from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                         "params": params})
            deadline = time.monotonic() + timeout
            while True:
                message = self._read_line(deadline, method)
                # Notifications and responses to other ids share the pipe;
                # skip anything that is not the reply being waited on.
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise CodeIntelligenceError(
                        f"Codebase Memory {method} failed: "
                        f"{error.get('message', error)}"
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    def _read_line(self, deadline: float, method: str) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise CodeIntelligenceError("The Codebase Memory session is not running")
        if time.monotonic() > deadline:
            raise CodeIntelligenceError(
                f"Codebase Memory {method} timed out"
            )
        line = process.stdout.readline()
        if line == "":
            raise CodeIntelligenceError(
                f"Codebase Memory closed the connection during {method}"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # CBM occasionally emits a non-JSON progress line; ignore and read on.
            return {"id": None}
        return message if isinstance(message, dict) else {"id": None}

    # -- tool surface -----------------------------------------------------

    def call_tool(self, tool: str, arguments: dict[str, Any],
                  *, timeout: float | None = None) -> dict[str, Any]:
        """Invoke a CBM tool, returning its structured payload."""
        budget = timeout if timeout is not None else _DEFAULT_CALL_TIMEOUT_SECONDS
        started = time.perf_counter()
        result = self._request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout=budget
        )
        self.metrics.record(tool, (time.perf_counter() - started) * 1000.0)

        if result.get("isError"):
            raise CodeIntelligenceError(
                f"Codebase Memory tool {tool!r} reported an error: "
                f"{_text_of(result)[:300]}"
            )
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            if isinstance(structured.get("error"), str):
                raise CodeIntelligenceError(
                    f"Codebase Memory tool {tool!r} failed: {structured['error']}"
                )
            return structured
        return {"_text": _text_of(result)}


def _text_of(payload: dict[str, Any]) -> str:
    """The text block of an MCP tool result, if it carries one."""
    for block in payload.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def parse_rows(payload: dict[str, Any], *, columns: int) -> tuple[list[list[str]], int]:
    """Rows from a tabular CBM payload, plus the count of unparseable ones.

    CBM renders query results as text: a header, then one whitespace-separated
    line per row. Values in the fields Sydes requests do not contain spaces,
    but that is a property of the data rather than a guarantee of the format,
    so every row is checked against the expected arity. A row that does not
    match is dropped and counted rather than silently mis-split.
    """
    structured = payload.get("rows")
    if isinstance(structured, list):
        return [[str(cell) for cell in row] for row in structured], 0

    text = payload.get("_text") or _text_of(payload)
    rows: list[list[str]] = []
    malformed = 0
    for line in str(text).splitlines():
        if not line.startswith("  ") or line.lstrip().startswith(("total:", "rows:")):
            continue
        fields = [field.strip('"') for field in line.split()]
        if len(fields) != columns:
            malformed += 1
            continue
        rows.append(fields)
    return rows, malformed


class CBMClient:
    """Typed access to the structural facts Sydes consumes.

    Each method names a capability rather than a query. Where CBM offers a
    first-class tool for a capability this calls it; `query_graph` is used only
    for the bulk fact sweeps that no higher-level tool covers, and is kept
    private so no caller above this class handles Cypher.
    """

    def __init__(self, session: CBMSession) -> None:
        self._session = session
        self.malformed_rows = 0

    @classmethod
    def spawn(cls, executable: str | None = None) -> CBMClient:
        """Start a persistent CBM session and wrap it."""
        return cls(StdioMCPSession(resolve_executable(executable)))

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> CBMClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def metrics(self) -> dict[str, Any]:
        session_metrics = getattr(self._session, "metrics", None)
        return session_metrics.to_dict() if session_metrics else {}

    @property
    def server_version(self) -> str | None:
        """The running CBM server's own reported version, from the MCP
        handshake `serverInfo` — diagnostics only, never a compatibility
        gate: the pinned package dependency is what actually controls
        compatibility, and imperfect version text is never a reason to
        reject an otherwise-working session."""
        info = getattr(self._session, "server_info", None)
        version = info.get("version") if isinstance(info, dict) else None
        return str(version) if version else None

    # -- indexing ---------------------------------------------------------

    def index_repository(self, repo_path: str | Path, *, mode: str = "fast") -> dict[str, Any]:
        """Index or incrementally update a repository, returning CBM's report."""
        payload = self._session.call_tool(
            "index_repository",
            {"repo_path": str(Path(repo_path).expanduser().resolve()), "mode": mode},
            timeout=_INDEX_TIMEOUT_SECONDS,
        )
        project = payload.get("project")
        if not isinstance(project, str) or not project:
            raise CodeIntelligenceError(
                f"Codebase Memory did not return a project id for {repo_path!r}"
            )
        return payload

    def index_status(self, project: str) -> dict[str, Any]:
        return self._session.call_tool("index_status", {"project": project})

    def graph_schema(self, project: str) -> dict[str, Any]:
        """Node labels and edge types actually present in this project."""
        return self._session.call_tool("get_graph_schema", {"project": project})

    def architecture(self, project: str, aspects: list[str] | None = None) -> dict[str, Any]:
        """High-level architecture facts, including declared entry points."""
        arguments: dict[str, Any] = {"project": project}
        if aspects:
            arguments["aspects"] = aspects
        return self._session.call_tool("get_architecture", arguments)

    # -- symbol lookup ----------------------------------------------------

    def search_symbols(
        self,
        project: str,
        *,
        name_pattern: str | None = None,
        query: str | None = None,
        label: str | None = None,
        file_pattern: str | None = None,
        fields: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Find symbols by regex, keyword, or label via CBM's own search."""
        arguments: dict[str, Any] = {"project": project, "limit": limit, "format": "json"}
        if name_pattern:
            arguments["name_pattern"] = name_pattern
        if query:
            arguments["query"] = query
        if label:
            arguments["label"] = label
        if file_pattern:
            arguments["file_pattern"] = file_pattern
        if fields:
            arguments["fields"] = fields
        return self._session.call_tool("search_graph", arguments)

    def code_snippet(self, project: str, qualified_name: str) -> dict[str, Any]:
        """Source for one symbol, for evidence rather than for parsing."""
        return self._session.call_tool(
            "get_code_snippet", {"project": project, "qualified_name": qualified_name}
        )

    # -- relationship traversal -------------------------------------------

    def trace_callers(
        self, project: str, function_name: str, *, depth: int = 3, limit: int = 200
    ) -> dict[str, Any]:
        """Inbound CALLS closure for a symbol, via CBM's own traversal.

        Preferred over hand-rolled BFS: CBM already bounds depth, excludes test
        files, paginates, and reports exact totals.
        """
        return self._session.call_tool(
            "trace_path",
            {
                "project": project,
                "function_name": function_name,
                "direction": "inbound",
                "mode": "calls",
                "depth": depth,
                "limit": limit,
                "format": "json",
            },
        )

    def trace_callees(
        self, project: str, function_name: str, *, depth: int = 3, limit: int = 200
    ) -> dict[str, Any]:
        """Outbound CALLS closure for a symbol."""
        return self._session.call_tool(
            "trace_path",
            {
                "project": project,
                "function_name": function_name,
                "direction": "outbound",
                "mode": "calls",
                "depth": depth,
                "limit": limit,
                "format": "json",
            },
        )

    # -- bulk fact sweeps -------------------------------------------------

    def all_symbols(self, project: str, label: str) -> list[list[str]]:
        """Every definition of one label, with span and export flag."""
        return self._rows(
            project,
            f"MATCH (n:{label}) WHERE n.file_path <> '' RETURN n.name, n.file_path, "
            "n.start_line, n.end_line, n.parent_class, n.is_exported",
            columns=6, order_by="n.file_path, n.name, n.start_line",
        )

    def all_imports(self, project: str) -> list[list[str]]:
        """Import edges, already carrying CBM's resolved target file."""
        return self._rows(
            project,
            "MATCH (a)-[r:IMPORTS]->(b) WHERE a.file_path <> '' "
            "RETURN a.file_path, r.local_name, b.file_path, b.qualified_name",
            columns=4, order_by="a.file_path, r.local_name",
        )

    def all_call_edges(self, project: str) -> list[list[str]]:
        """The call graph, language-general and free of Sydes parsing."""
        return self._rows(
            project,
            "MATCH (a)-[:CALLS]->(b) WHERE a.file_path <> '' AND b.file_path <> '' "
            "RETURN a.qualified_name, a.file_path, a.start_line, "
            "b.qualified_name, b.file_path, b.start_line",
            columns=6, order_by="a.qualified_name, a.start_line, b.qualified_name",
        )

    def all_usage_edges(self, project: str) -> list[list[str]]:
        """USAGE references: a symbol named inside another symbol's body.

        Distinct from CALLS, and the relationship that connects a symbol to the
        composing symbol that mentions it without invoking it.
        """
        return self._rows(
            project,
            "MATCH (a)-[:USAGE]->(b) WHERE a.file_path <> '' AND b.file_path <> '' "
            "RETURN a.qualified_name, a.file_path, b.qualified_name, b.file_path",
            columns=4, order_by="a.qualified_name, b.qualified_name",
        )

    def decorated_symbols(self, project: str, *, page_size: int = 500) -> list[dict[str, Any]]:
        """Symbols carrying route metadata or decorator source.

        Uses `search_graph`'s structured form rather than a tabular query,
        because decorator text and signatures contain whitespace and newlines
        that a row-oriented parser cannot carry intact.

        Everything here is a *fact about a symbol*, not framework knowledge:
        `route_method`/`route_path` are properties CBM set, and `decorators`
        is verbatim source it captured. Interpreting them is the caller's job.
        """
        out: list[dict[str, Any]] = []
        for label in ("Function", "Method"):
            offset = 0
            while True:
                payload = self._session.call_tool(
                    "search_graph",
                    {
                        "project": project,
                        "label": label,
                        # Everything with a body; the decorator/route filter is
                        # applied below so a symbol without either is skipped
                        # without a second round trip.
                        "name_pattern": ".+",
                        "fields": ["decorators", "signature", "route_method", "route_path"],
                        "format": "json",
                        "limit": page_size,
                        "offset": offset,
                    },
                )
                rows, has_more = _search_rows(payload)
                for row in rows:
                    if row.get("decorators") or row.get("route_path") or row.get("route_method"):
                        out.append(row)
                if not has_more:
                    break
                offset += page_size
        return out

    # -- internals --------------------------------------------------------

    def _query_graph(self, project: str, query: str) -> dict[str, Any]:
        return self._session.call_tool(
            "query_graph", {"project": project, "query": query}
        )

    def _rows(self, project: str, query: str, *, columns: int, order_by: str) -> list[list[str]]:
        """Run a bulk Cypher sweep, paging with `SKIP`/`LIMIT` rather than
        requesting every row in one response.

        CBM's MCP transport caps a single tool response (~10 MB); an
        unbounded whole-repository sweep — every symbol, every call edge —
        can exceed that on a large enough codebase, and the failure was a
        hard error with no result at all. `order_by` is required, not just a
        nicety: `SKIP` with no stable `ORDER BY` has no guaranteed row order
        between calls, which could silently duplicate or drop rows across
        pages. Most repositories return everything in a single page (zero
        extra round trips); pagination only engages for the ones that would
        otherwise fail outright.
        """
        all_rows: list[list[str]] = []
        for page in range(_BULK_QUERY_MAX_PAGES):
            offset = page * _BULK_QUERY_PAGE_SIZE
            paged_query = f"{query} ORDER BY {order_by} SKIP {offset} LIMIT {_BULK_QUERY_PAGE_SIZE}"
            rows, malformed = parse_rows(self._query_graph(project, paged_query), columns=columns)
            self.malformed_rows += malformed
            all_rows.extend(rows)
            if len(rows) < _BULK_QUERY_PAGE_SIZE:
                break
        return all_rows


def _escape(value: str) -> str:
    """Neutralise quotes in an identifier interpolated into a query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _search_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Flatten `search_graph`'s prefix-grouped JSON into per-symbol mappings.

    The grouped form shares a qualified-name prefix and file across a group's
    rows, so the full qualified name is reassembled here rather than left for
    every caller to rebuild.
    """
    columns = payload.get("cols")
    if not isinstance(columns, list):
        return [], False
    rows: list[dict[str, Any]] = []
    for group in payload.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        prefix = str(group.get("qn_prefix") or "")
        file_path = str(group.get("file") or "")
        for values in group.get("rows", []) or []:
            if not isinstance(values, list):
                continue
            record = dict(zip(columns, values))
            name = str(record.get("name") or "")
            if not name:
                continue
            record["name"] = name
            record["file"] = file_path
            record["qualified_name"] = f"{prefix}.{name}" if prefix else name
            # `decorators` arrives as a list of decorator sources; join so
            # downstream sees one searchable block of text.
            decorators = record.get("decorators")
            if isinstance(decorators, list):
                record["decorators"] = "\n".join(str(item) for item in decorators)
            rows.append(record)
    return rows, bool(payload.get("has_more"))
