"""Codebase Memory as a source of structural facts.

CBM is a language-general structural engine: it parses many languages and
stores files, symbols, spans, imports and call edges in a persistent graph.
That is exactly the half of the problem Sydes should stop owning.

It is *not* a system-semantics engine, and this adapter does not pretend
otherwise. A capability study established that CBM does not compose framework
router prefixes — it reports the leaf path for a handler mounted under a
prefix — and models no persistence sinks. So this backend supplies the generic
facts and leaves route composition and sink interpretation to Sydes, which is
the split the architecture already assumes.

Two things are deliberate:

*One session, many queries.* All CBM access goes through `CBMClient`, which
holds a single long-lived process. Nothing in this module knows how CBM is
invoked.

*No fallback to native.* If CBM cannot answer, this backend reports the gap and
raises. Silently switching engines would mean a verdict built on facts from a
backend the operator did not choose.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from sydes.code_intelligence.base import StructuralFacts
from sydes.code_intelligence.cbm_client import (
    CBM_EXECUTABLE_ENV_VAR,
    CBMClient,
    resolve_executable,
)
from sydes.core.models import RepoRef

CBM_BACKEND = "cbm"

__all__ = ["CBM_BACKEND", "CBM_EXECUTABLE_ENV_VAR", "CBMCodeIntelligence"]

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

    def __init__(self, executable: str | None = None, client: CBMClient | None = None) -> None:
        # An injected client is how tests drive this without a live daemon.
        self._client = client
        self._owns_client = client is None
        self._executable = None if client is not None else resolve_executable(executable)
        self._version = "session"

    def _ensure_client(self) -> CBMClient:
        if self._client is None:
            self._client = CBMClient.spawn(self._executable)
        return self._client

    def close(self) -> None:
        """Release the CBM session if this adapter started one."""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> CBMCodeIntelligence:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- fact translation -------------------------------------------------

    def _symbols_for(self, client: CBMClient, project: str) -> dict[str, list[dict[str, Any]]]:
        """Every definition CBM knows, grouped by repository-relative file."""
        by_file: dict[str, list[dict[str, Any]]] = {}
        for label, kind in _KIND_BY_LABEL.items():
            for row in client.all_symbols(project, label):
                name, path, start, end, parent, exported = (row + [None] * 6)[:6]
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

    def _imports_for(self, client: CBMClient, project: str) -> dict[str, list[dict[str, Any]]]:
        """Import edges, already carrying the resolved target file."""
        by_file: dict[str, list[dict[str, Any]]] = {}
        for row in client.all_imports(project):
            importer, local, target, qualified = (row + [None] * 4)[:4]
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

    def _call_edges_for(self, client: CBMClient, project: str, repo: str) -> list[dict[str, Any]]:
        """CBM's call graph, normalized away from its row shape.

        This is the fact that makes the backend worth having: it is produced
        without Sydes parsing a single function body, so it holds for every
        language CBM indexes rather than only the two Sydes can read.
        """
        edges: list[dict[str, Any]] = []
        for row in client.all_call_edges(project):
            caller_q, caller_file, caller_line, callee_q, callee_file, callee_line = (
                (row + [None] * 6)[:6]
            )
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

    def _usage_edges_for(self, client: CBMClient, project: str, repo: str) -> list[dict[str, Any]]:
        """USAGE references: a symbol named inside another symbol's body."""
        edges: list[dict[str, Any]] = []
        for row in client.all_usage_edges(project):
            user_q, user_file, used_q, used_file = (row + [None] * 4)[:4]
            if not _is_repository_path(user_file) or not _is_repository_path(used_file):
                continue
            edges.append({
                "repo": repo,
                "user_file": user_file,
                "user_symbol": str(user_q).rsplit(".", 1)[-1],
                "user_qualified_name": user_q,
                "used_file": used_file,
                "used_symbol": str(used_q).rsplit(".", 1)[-1],
                "used_qualified_name": used_q,
                "source": CBM_BACKEND,
            })
        return edges

    def _entrypoints_for(self, client: CBMClient, project: str, repo: str) -> list[dict[str, Any]]:
        """Symbols CBM annotates with route metadata or decorator source.

        Reported verbatim. Whether a decorator makes a symbol an entrypoint,
        and of what kind, is interpretation and belongs above this layer.
        """
        entrypoints: list[dict[str, Any]] = []
        for record in client.decorated_symbols(project):
            path = record.get("file")
            if not _is_repository_path(path):
                continue
            method = record.get("route_method")
            route_path = record.get("route_path")
            entrypoints.append({
                "repo": repo,
                "qualified_name": str(record.get("qualified_name") or ""),
                "symbol": str(record.get("name") or ""),
                "file": str(path),
                "line": _line(str(record.get("lines", "")).split("-")[0]),
                "route_method": str(method) if method and method != "-" else None,
                "route_path": str(route_path) if route_path and route_path != "-" else None,
                "decorators": str(record.get("decorators") or ""),
                "signature": str(record.get("signature") or ""),
                "source": CBM_BACKEND,
            })
        return entrypoints

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
        client = self._ensure_client()

        repo_payloads: list[dict[str, Any]] = []
        totals = {"files_indexed": 0, "symbols": 0, "imports": 0, "exports": 0}
        index_ms = 0.0
        query_ms = 0.0
        call_edges: list[dict[str, Any]] = []
        usage_edges: list[dict[str, Any]] = []
        entrypoints: list[dict[str, Any]] = []
        gaps: list[str] = []

        for repo in repos:
            index_started = time.perf_counter()
            index_payload = client.index_repository(repo.root)
            project = str(index_payload["project"])
            index_ms += (time.perf_counter() - index_started) * 1000.0

            query_started = time.perf_counter()
            symbols_by_file = self._symbols_for(client, project)
            imports_by_file = self._imports_for(client, project)
            call_edges.extend(self._call_edges_for(client, project, repo.name))
            usage_edges.extend(self._usage_edges_for(client, project, repo.name))
            entrypoints.extend(self._entrypoints_for(client, project, repo.name))
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
        # parsing: composing a router prefix with a mount is framework
        # semantics, which the architecture assigns to Sydes and which CBM
        # demonstrably gets wrong. Sydes' route parser is retained here purely
        # for that enrichment.
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
        if client.malformed_rows:
            gaps.append(
                f"{client.malformed_rows} query row(s) did not match the expected "
                "column arity and were dropped rather than mis-parsed"
            )

        total_ms = (time.perf_counter() - started) * 1000.0
        session = client.metrics
        diagnostics = [
            "code_intelligence_backend=cbm transport=persistent_mcp_stdio",
            f"cbm_session_start_ms={session.get('session_start_ms', 0)}"
            f" cbm_calls={session.get('calls', 0)}"
            f" cbm_mean_call_ms={session.get('mean_call_ms', 0)}",
            f"cbm_index_ms={index_ms:.1f} cbm_query_ms={query_ms:.1f}"
            f" sydes_route_semantics_ms={semantics_ms:.1f} total_ms={total_ms:.1f}",
            "cbm_supplied=symbols,spans,imports,exports,call_edges,usage_edges,entrypoints"
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
                "transport": "persistent_mcp_stdio",
                "files_total": totals["files_indexed"],
                "symbols": totals["symbols"],
                "call_edges": len(call_edges),
                "usage_edges": len(usage_edges),
                "entrypoints": len(entrypoints),
                "index_ms": round(index_ms, 1),
                "query_ms": round(query_ms, 1),
                "sydes_route_semantics_ms": round(semantics_ms, 1),
                "total_index_ms": round(total_ms, 1),
                "session": session,
                "gaps": gaps,
            },
            call_edges=call_edges,
            usage_edges=usage_edges,
            entrypoints=entrypoints,
            provides_call_graph=True,
            diagnostics=diagnostics,
            backend=self.name,
        )
