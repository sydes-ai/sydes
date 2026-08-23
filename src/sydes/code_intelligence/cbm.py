"""Codebase Memory as a source of structural facts.

CBM is a language-general structural engine: it parses many languages and
stores files, symbols, spans, imports and call edges in a persistent graph.
That is exactly the half of the problem Sydes should stop owning.

It is *not* a system-semantics engine, and this adapter does not pretend
otherwise. A spike established that CBM does not compose framework router
prefixes — it reports `route_path: "/"` for a handler mounted at `/students`
— and models no persistence sinks at all. So this backend supplies the generic
facts and leaves route composition and sink interpretation to Sydes, which is
the split the architecture already assumes.

Two things are deliberate:

*No SQLite access.* Facts come through `query_graph`, the documented Cypher
interface. Reading CBM's database directly would be faster and would couple
Sydes to an undocumented schema of a pre-1.0 tool.

*No fallback to native.* If CBM cannot answer, this backend reports the gap and
returns what it has. Silently switching engines would mean a verdict built on
facts from a backend the operator did not choose.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from sydes.code_intelligence.base import CodeIntelligenceError, StructuralFacts
from sydes.core.models import RepoRef

CBM_BACKEND = "cbm"

#: Overrides executable discovery, for a pinned or non-PATH install.
CBM_EXECUTABLE_ENV_VAR = "SYDES_CBM_EXECUTABLE"

_DEFAULT_EXECUTABLE = "codebase-memory-mcp"
_INDEX_TIMEOUT_SECONDS = 900
_QUERY_TIMEOUT_SECONDS = 300

#: CBM node labels mapped onto the symbol kinds Sydes already consumes.
_KIND_BY_LABEL = {"Function": "function", "Method": "class_method", "Class": "class"}

#: CBM records builtins and other synthetic origins under placeholder paths.
#: They are not repository files and must not enter a file-keyed index.
_SYNTHETIC_PATH_PREFIX = "<"

#: CBM does not label a symbol's language, but Sydes' body slicing is
#: language-aware. The extension is a faithful reading of the fact rather than
#: an invented one, so it is filled in during translation.
_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
}


def _language_for(path: str) -> str:
    return _LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower(), "unknown")


def _executable() -> str:
    """Locate the CBM binary, or say plainly that it is absent."""
    import os

    override = os.environ.get(CBM_EXECUTABLE_ENV_VAR, "").strip()
    candidate = override or _DEFAULT_EXECUTABLE
    resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
    if resolved is None:
        raise CodeIntelligenceError(
            f"Codebase Memory executable {candidate!r} was not found. "
            f"Install it, or set {CBM_EXECUTABLE_ENV_VAR}, or select the native "
            "backend explicitly with SYDES_CODE_INTELLIGENCE=native."
        )
    return resolved


def _run_tool(executable: str, tool: str, arguments: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Invoke one CBM tool through its headless CLI and return the payload."""
    try:
        completed = subprocess.run(
            [executable, "cli", "--json", tool, json.dumps(arguments)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodeIntelligenceError(f"Codebase Memory tool {tool!r} timed out") from exc
    if not completed.stdout.strip():
        raise CodeIntelligenceError(
            f"Codebase Memory tool {tool!r} produced no output: "
            f"{completed.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CodeIntelligenceError(
            f"Codebase Memory tool {tool!r} returned unreadable output"
        ) from exc
    return payload.get("structuredContent", payload)


def _rows(payload: dict[str, Any], *, columns: int) -> tuple[list[list[str]], int]:
    """Rows from a `query_graph` response, with the count of unparseable ones.

    CBM 0.10.8 dropped the structured `columns`/`rows` payload that 0.9.0
    returned and now renders results as text: a header, then one
    whitespace-separated line per row. Values in the fields Sydes requests
    never contain spaces, but that is a property of the data rather than a
    guarantee of the format, so every row is checked against the expected
    arity. A row that does not match is dropped and counted rather than
    silently mis-split into the wrong columns.
    """
    if isinstance(payload.get("error"), str):
        raise CodeIntelligenceError(f"Codebase Memory query failed: {payload['error']}")

    structured = payload.get("rows")
    if isinstance(structured, list):
        return [[str(cell) for cell in row] for row in structured], 0

    text = ""
    for block in payload.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "")
            break

    rows: list[list[str]] = []
    malformed = 0
    for line in text.splitlines():
        if not line.startswith("  ") or line.startswith("total:"):
            continue
        fields = [field.strip('"') for field in line.split()]
        if len(fields) != columns:
            malformed += 1
            continue
        rows.append(fields)
    return rows, malformed


def _line(value: Any) -> int | None:
    """CBM returns numbers as strings; absent lines become None, not zero."""
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number or None


def _is_repository_path(path: Any) -> bool:
    return isinstance(path, str) and bool(path) and not path.startswith(_SYNTHETIC_PATH_PREFIX)


class CBMCodeIntelligence:
    """Structural facts from a Codebase Memory persistent index."""

    name = CBM_BACKEND

    def __init__(self, executable: str | None = None) -> None:
        self._malformed_rows = 0
        self._executable = executable or _executable()
        self._version = self._read_version()

    def _read_version(self) -> str:
        try:
            completed = subprocess.run(
                [self._executable, "--version"], capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return completed.stdout.strip() or "unknown"

    # -- fact collection --------------------------------------------------

    def _project_for(self, repo: RepoRef) -> tuple[str, dict[str, Any]]:
        """Index or incrementally update one repository, returning its project id."""
        root = str(Path(repo.root).expanduser().resolve())
        payload = _run_tool(
            self._executable, "index_repository", {"repo_path": root}, _INDEX_TIMEOUT_SECONDS
        )
        project = payload.get("project")
        if not isinstance(project, str) or not project:
            raise CodeIntelligenceError(
                f"Codebase Memory did not return a project id for {root!r}"
            )
        return project, payload

    def _query(self, project: str, query: str, *, columns: int) -> list[list[str]]:
        rows, malformed = _rows(
            _run_tool(
                self._executable,
                "query_graph",
                {"project": project, "query": query},
                _QUERY_TIMEOUT_SECONDS,
            ),
            columns=columns,
        )
        if malformed:
            self._malformed_rows += malformed
        return rows

    def _call_edges_for(self, project: str, repo: str) -> list[dict[str, Any]]:
        """CBM's call graph, normalized away from Cypher shape.

        This is the fact that makes the backend worth having: it is produced
        without Sydes parsing a single function body, so it holds for every
        language CBM indexes rather than only the two Sydes can read.
        """
        rows = self._query(
            project,
            "MATCH (a)-[:CALLS]->(b) WHERE a.file_path <> '' AND b.file_path <> '' "
            "RETURN a.qualified_name, a.file_path, a.start_line, "
            "b.qualified_name, b.file_path, b.start_line",
            columns=6,
        )
        edges: list[dict[str, Any]] = []
        for caller_q, caller_file, caller_line, callee_q, callee_file, callee_line in rows:
            if not _is_repository_path(caller_file) or not _is_repository_path(callee_file):
                continue
            edges.append({
                "repo": repo,
                "caller_file": caller_file,
                "caller_symbol": str(caller_q).rsplit(".", 1)[-1],
                "caller_qualified_name": caller_q,
                "caller_line": _line(caller_line),
                "callee_file": callee_file,
                "callee_symbol": str(callee_q).rsplit(".", 1)[-1],
                "callee_qualified_name": callee_q,
                "callee_line": _line(callee_line),
                "source": CBM_BACKEND,
            })
        return edges

    def _symbols_for(self, project: str) -> dict[str, list[dict[str, Any]]]:
        """Every definition CBM knows, grouped by repository-relative file."""
        by_file: dict[str, list[dict[str, Any]]] = {}
        for label, kind in _KIND_BY_LABEL.items():
            rows = self._query(
                project,
                f"MATCH (n:{label}) WHERE n.file_path <> '' RETURN n.name, n.file_path, "
                "n.start_line, n.end_line, n.parent_class, n.is_exported",
                columns=6,
            )
            for name, path, start, end, parent, exported in (
                (row + [None] * 6)[:6] for row in rows
            ):
                if not isinstance(name, str) or not _is_repository_path(path):
                    continue
                # CBM qualifies a parent class with the whole project prefix;
                # Sydes matches on the bare class name.
                parent_name = str(parent).rsplit(".", 1)[-1] if parent else None
                symbol: dict[str, Any] = {
                    "name": name,
                    "kind": kind,
                    "language": _language_for(path),
                    "file": path,
                    "line": _line(start),
                    "start_line": _line(start),
                    "end_line": _line(end),
                    "exported": str(exported).lower() == "true",
                    "export_kind": None,
                    "source": CBM_BACKEND,
                }
                if kind == "class_method" and parent_name:
                    symbol["parent"] = parent_name
                    symbol["qualified_name"] = f"{parent_name}.{name}"
                by_file.setdefault(path, []).append(symbol)
        return by_file

    def _imports_for(self, project: str) -> dict[str, list[dict[str, Any]]]:
        """Import edges, already carrying the resolved target file."""
        by_file: dict[str, list[dict[str, Any]]] = {}
        rows = self._query(
            project,
            "MATCH (a)-[r:IMPORTS]->(b) WHERE a.file_path <> '' "
            "RETURN a.file_path, r.local_name, b.file_path, b.qualified_name",
            columns=4,
        )
        for importer, local, target, qualified in ((row + [None] * 4)[:4] for row in rows):
            if not _is_repository_path(importer):
                continue
            local_name = local if isinstance(local, str) and local else None
            source = str(qualified).rsplit(".", 1)[-1] if qualified else None
            if local_name is None:
                local_name = source
            if local_name is None:
                continue
            by_file.setdefault(importer, []).append({
                "local": local_name,
                "imported": local_name,
                "source": source or local_name,
                "kind": "cbm_import",
                # CBM resolves the target itself, so Sydes does no path probing.
                "resolved_file": target if _is_repository_path(target) else None,
            })
        return by_file

    # -- interface --------------------------------------------------------

    def build_or_update(
        self,
        repos: list[RepoRef],
        *,
        workspace_id: str | None = None,
        root: Path | None = None,
    ) -> StructuralFacts:
        """Index each repository through CBM and translate its facts for Sydes."""
        started = time.perf_counter()
        repo_payloads: list[dict[str, Any]] = []
        totals = {"files_indexed": 0, "symbols": 0, "imports": 0, "exports": 0}
        index_ms = 0.0
        query_ms = 0.0
        call_edges: list[dict[str, Any]] = []
        gaps: list[str] = []

        for repo in repos:
            index_started = time.perf_counter()
            project, index_payload = self._project_for(repo)
            index_ms += (time.perf_counter() - index_started) * 1000.0

            query_started = time.perf_counter()
            symbols_by_file = self._symbols_for(project)
            imports_by_file = self._imports_for(project)
            call_edges.extend(self._call_edges_for(project, repo.name))
            query_ms += (time.perf_counter() - query_started) * 1000.0

            files = []
            for path in sorted(set(symbols_by_file) | set(imports_by_file)):
                symbols = symbols_by_file.get(path, [])
                files.append({
                    "path": path,
                    "language": _language_for(path),
                    "imports": imports_by_file.get(path, []),
                    # CBM marks definitions with `is_exported` but records no
                    # separate export statements, so exports are derived.
                    "exports": [
                        {"kind": "named", "symbol": item["name"]}
                        for item in symbols
                        if item.get("exported")
                    ],
                    "symbols": symbols,
                })
            totals["files_indexed"] += len(files)
            totals["symbols"] += sum(len(item["symbols"]) for item in files)
            totals["imports"] += sum(len(item["imports"]) for item in files)
            totals["exports"] += sum(len(item["exports"]) for item in files)
            repo_payloads.append({
                "repo": repo.name,
                "root": str(Path(repo.root).expanduser().resolve()),
                "files": files,
                "summary": {"files_indexed": len(files)},
            })
            if not index_payload.get("nodes"):
                gaps.append(f"{repo.name}: CBM reported an empty graph")

        # Route facts stay with Sydes. This is not a fallback for failed
        # parsing: composing `APIRouter(prefix="/students")` with a mount is
        # framework semantics, which the architecture assigns to Sydes and
        # which CBM demonstrably gets wrong (it reports the leaf path only).
        # Sydes' route parser is retained here purely for that enrichment.
        from sydes.discover.repo_map import build_repo_map_batch
        from sydes.discover.route_graph import (
            build_route_graph_facts_from_route_index_batch,
        )
        from sydes.discover.route_index import build_route_index_batch

        semantics_started = time.perf_counter()
        repo_map = build_repo_map_batch(repos)
        route_index = build_route_index_batch(repos, repo_map_batch=repo_map)
        route_graph = build_route_graph_facts_from_route_index_batch(route_index)
        semantics_ms = (time.perf_counter() - semantics_started) * 1000.0

        gaps.append(
            "route composition is computed by Sydes, not CBM: CBM reports "
            "uncomposed route paths and models no persistence sinks"
        )
        if not call_edges:
            gaps.append("CBM returned no CALLS edges for this repository set")
        if self._malformed_rows:
            gaps.append(
                f"{self._malformed_rows} query row(s) did not match the expected "
                "column arity and were dropped rather than mis-parsed"
            )

        total_ms = (time.perf_counter() - started) * 1000.0
        diagnostics = [
            f"code_intelligence_backend=cbm version={self._version}",
            f"cbm_index_ms={index_ms:.1f} cbm_query_ms={query_ms:.1f}"
            f" sydes_route_semantics_ms={semantics_ms:.1f} total_ms={total_ms:.1f}",
            "cbm_supplied=symbols,spans,imports,exports,call_edges"
            "  sydes_semantics=route_index,route_graph,repo_map",
        ]
        diagnostics.extend(f"cbm_gap: {gap}" for gap in gaps)

        return StructuralFacts(
            repo_map=repo_map,
            route_index=route_index,
            symbol_index={
                "version": "v1",
                "repos": repo_payloads,
                "summary": dict(sorted(totals.items())),
            },
            route_graph=route_graph,
            metrics={
                "backend": CBM_BACKEND,
                "cbm_version": self._version,
                "files_total": totals["files_indexed"],
                "symbols": totals["symbols"],
                "call_edges": len(call_edges),
                "index_ms": round(index_ms, 1),
                "query_ms": round(query_ms, 1),
                "sydes_route_semantics_ms": round(semantics_ms, 1),
                "total_index_ms": round(total_ms, 1),
                "gaps": gaps,
            },
            call_edges=call_edges,
            provides_call_graph=True,
            diagnostics=diagnostics,
            backend=self.name,
        )
