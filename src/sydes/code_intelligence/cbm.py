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

from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
from typing import Any

from sydes.code_intelligence.base import CodeIntelligenceError, StructuralFacts
from sydes.code_intelligence.cbm_client import (
    CBM_EXECUTABLE_ENV_VAR,
    CBMClient,
    resolve_executable,
)
from sydes.code_intelligence.symbol_identity import (
    SeedRequest,
    resolve_seed_identities,
)
from sydes.code_intelligence.graph_slice import (
    GraphQueryCache,
    GraphSliceLimits,
    build_graph_slice,
    graph_slice_call_edges,
    graph_slice_usage_edges,
)
from sydes.core.models import RepoRef
from sydes.observability import trace as _trace

CBM_BACKEND = "cbm"

__all__ = [
    "CBM_BACKEND",
    "CBM_EXECUTABLE_ENV_VAR",
    "BoundedEdgeOutcome",
    "CBMCodeIntelligence",
]


@dataclass
class BoundedEdgeOutcome:
    """What one bounded edge acquisition actually did.

    Reported so an operator can tell, for any run, whether the bounded path
    was used, how much of the graph it saw, whether it was cut short, and
    whether it had to fall back to a repository-wide sweep — without
    inferring any of that from timings.
    """

    seed_count: int = 0
    used_slice: bool = False
    fell_back: bool = False
    reason: str | None = None
    truncated: bool = False
    truncation_reason: str | None = None
    graph_calls: int = 0
    node_count: int = 0
    call_edge_count: int = 0
    usage_edge_count: int = 0
    #: Seeds that resolved to a CBM canonical graph identity.
    canonical_seed_count: int = 0
    #: Seed labels no canonical identity could be found for. Distinct from
    #: "resolved but no edges": this one means Sydes could not look.
    unresolved_seeds: list[str] = field(default_factory=list)
    #: Split of the above. An unresolved CHANGED symbol bounds what was
    #: explored; an unresolved auxiliary route alias only costs some
    #: outbound route coverage.
    unresolved_changed_seeds: list[str] = field(default_factory=list)
    unresolved_auxiliary_seeds: list[str] = field(default_factory=list)
    #: Seed label -> the several identities it legitimately matched.
    ambiguous_seeds: dict[str, list[str]] = field(default_factory=dict)
    limits: GraphSliceLimits | None = None

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


# -- fast-mode exclusion detection -----------------------------------------
#
# CBM's `mode="fast"` index can exclude entire directories from a repository
# (observed on spring-petclinic: `src/main/java/org/springframework/samples`
# came back in `excluded.dirs`, silently omitting the changed production
# file it contained — GraphSlice then requested seeds that could never
# resolve). This is read from whatever `index_repository()` already
# returned; it costs no extra CBM call to detect.


def _excluded_dirs_from_index_payload(payload: dict[str, Any]) -> list[str]:
    """`excluded.dirs` from an `index_repository` payload, read defensively.

    Any shape other than `{"excluded": {"dirs": [str, ...]}}` — the key
    absent, `excluded` not a dict, `dirs` not a list, a non-string entry —
    yields an empty list rather than raising. A payload shape Sydes cannot
    read is not evidence that indexing was incomplete.
    """
    excluded = payload.get("excluded")
    if not isinstance(excluded, dict):
        return []
    dirs = excluded.get("dirs")
    if not isinstance(dirs, list):
        return []
    return [item for item in dirs if isinstance(item, str) and item]


def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.replace("\\", "/").strip("/").split("/") if part)


def _is_under_excluded_dir(changed_path: str, excluded_dir: str) -> bool:
    """Whether `changed_path` is inside `excluded_dir`, by path SEGMENT —
    never by raw string prefix.

    A naive `changed_path.startswith(excluded_dir)` would treat
    `src/main/java/foobar/X.java` as being inside `src/main/java/foo`,
    because the character sequence matches even though the directory does
    not. Comparing segment tuples instead makes `foo` and `foobar` the
    distinct path components they are.
    """
    excluded_parts = _path_segments(excluded_dir)
    if not excluded_parts:
        return False
    changed_parts = _path_segments(changed_path)
    return changed_parts[: len(excluded_parts)] == excluded_parts


def _changed_files_under_excluded_dirs(
    changed_files: list[str], excluded_dirs: list[str],
) -> list[str]:
    """Changed files that fall under any excluded directory, in input order,
    de-duplicated. Empty in the (normal) case where nothing was excluded or
    nothing changed there — never guessed at."""
    if not changed_files or not excluded_dirs:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for changed_path in changed_files:
        if not isinstance(changed_path, str) or not changed_path or changed_path in seen:
            continue
        if any(_is_under_excluded_dir(changed_path, excluded_dir) for excluded_dir in excluded_dirs):
            seen.add(changed_path)
            hits.append(changed_path)
    return hits


class CBMCodeIntelligence:
    """Structural facts from a Codebase Memory persistent index."""

    name = CBM_BACKEND

    def __init__(self, executable: str | None = None, client: CBMClient | None = None) -> None:
        # An injected client is how tests drive this without a live daemon.
        self._client = client
        self._owns_client = client is None
        self._executable = None if client is not None else resolve_executable(executable)
        self._version = "session"
        # repo name -> CBM project id, recorded during `build_or_update` so a
        # later bounded slice fetch can address the same index without
        # re-indexing. Only populated for repos this adapter actually indexed.
        self._projects: dict[str, str] = {}
        # One cache per adapter instance, and an adapter lives exactly as long
        # as one `verify-change` run — so memoization is run-local by
        # construction and never spans repos or commits.
        self._graph_cache = GraphQueryCache()

    def _ensure_client(self) -> CBMClient:
        if self._client is None:
            # Not a progress bar — a first-run native-runtime download (the
            # official wrapper's own bootstrap, never duplicated here) can
            # take real time, and session startup itself is ~1.5s even when
            # cached; one line so a first-time user isn't staring at nothing.
            print("Preparing Sydes code intelligence...", file=sys.stderr)
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
                name, path, start, end, parent, exported, cbm_qualified = (
                    (row + [None] * 7)[:7]
                )
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
                # CBM's own canonical graph identity, kept under a distinct key.
                # `qualified_name` below stays Sydes' shorter display form,
                # which identity matching elsewhere already depends on;
                # overwriting it would change unrelated behavior.
                if isinstance(cbm_qualified, str) and cbm_qualified:
                    symbol["cbm_qualified_name"] = cbm_qualified
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

    # -- bounded edge acquisition -----------------------------------------

    def attach_bounded_edges(
        self,
        facts: StructuralFacts,
        *,
        seed_symbols: list[SeedRequest] | list[str],
        limits: GraphSliceLimits | None = None,
    ) -> BoundedEdgeOutcome:
        """Populate `facts.call_edges`/`facts.usage_edges` from a bounded
        neighborhood around `seed_symbols`, in place.

        The counterpart to `build_or_update(defer_edges=True)`. Produces the
        exact same edge dict shapes the repository-wide sweep produces, so
        `ImpactInterpreter`/`_FactIndex` and boundary discovery consume this
        completely unchanged.

        Fallback is deliberately narrow. A slice that succeeds but finds
        nothing is a real, valid answer ("no edges touch these symbols") and
        is kept as-is — falling back to a repository-wide sweep there would
        reintroduce exactly the cost this exists to avoid, for a query that
        already answered correctly. Only an actual CBM/transport failure
        (`CodeIntelligenceError`) falls back to the full sweep, and that is
        recorded on the outcome rather than passing silently.
        """
        limits = limits or GraphSliceLimits()
        requests = [
            item if isinstance(item, SeedRequest) else SeedRequest(name=str(item))
            for item in seed_symbols
        ]
        requests = [item for item in requests if item.name or item.qualified_name]
        outcome = BoundedEdgeOutcome(seed_count=len(requests), limits=limits)

        # Display names are not CBM graph identities. Resolve them against
        # the symbol index already loaded, so the bounded edge query matches
        # the names CBM's own edges are keyed by rather than returning zero
        # rows against a shorter display form.
        resolution = resolve_seed_identities(facts.symbol_index, requests)
        seeds = resolution.canonical
        outcome.canonical_seed_count = len(seeds)
        outcome.unresolved_seeds = list(resolution.unresolved)
        outcome.unresolved_changed_seeds = list(resolution.unresolved_changed)
        outcome.unresolved_auxiliary_seeds = list(resolution.unresolved_auxiliary)
        outcome.ambiguous_seeds = dict(resolution.ambiguous)
        _trace.record_seed_resolution(
            requested=len(requests), canonical=len(seeds),
            unresolved=resolution.unresolved, ambiguous=resolution.ambiguous,
            unresolved_changed=resolution.unresolved_changed,
            unresolved_auxiliary=resolution.unresolved_auxiliary,
            canonical_seeds=seeds,
        )

        if not seeds:
            # Nothing addressable to seed from. A repository-wide sweep would
            # cost the most and tell impact analysis nothing it can use,
            # since every consumer of these edges starts from a symbol.
            outcome.used_slice = False
            outcome.reason = (
                "no seed symbol resolved to a CBM graph identity"
                if requests else "no seed symbols to build a bounded slice from"
            )
            return outcome

        client = self._ensure_client()
        call_edges: list[dict[str, Any]] = []
        usage_edges: list[dict[str, Any]] = []

        for repo_name, project in sorted(self._projects.items()):
            try:
                graph_slice = build_graph_slice(
                    client, project, repo_name, seeds,
                    limits=limits, cache=self._graph_cache,
                )
            except CodeIntelligenceError as exc:
                # A genuine tool/protocol failure — not merely an empty
                # result. Fall back to the repository-wide sweep so this
                # validation phase never loses analysis to a transient CBM
                # problem, and say so out loud.
                outcome.used_slice = False
                outcome.fell_back = True
                outcome.reason = f"bounded graph slice failed: {exc}"
                facts.call_edges = self._call_edges_for(client, project, repo_name)
                facts.usage_edges = self._usage_edges_for(client, project, repo_name)
                _trace.record_graph_slice_fallback(
                    reason=outcome.reason, seed_count=len(seeds),
                    call_edges=len(facts.call_edges), usage_edges=len(facts.usage_edges),
                )
                return outcome

            call_edges.extend(graph_slice_call_edges(graph_slice))
            usage_edges.extend(graph_slice_usage_edges(graph_slice))
            outcome.graph_calls += graph_slice.source_call_count
            outcome.node_count += graph_slice.node_count()
            if graph_slice.truncated:
                outcome.truncated = True
                if graph_slice.truncation_reason and not outcome.truncation_reason:
                    outcome.truncation_reason = graph_slice.truncation_reason

        facts.call_edges = call_edges
        facts.usage_edges = usage_edges
        outcome.used_slice = True
        outcome.call_edge_count = len(call_edges)
        outcome.usage_edge_count = len(usage_edges)
        return outcome

    # -- interface --------------------------------------------------------

    def build_or_update(
        self,
        repos: list[RepoRef],
        *,
        workspace_id: str | None = None,
        root: Path | None = None,
        defer_edges: bool = False,
        changed_files_by_repo: dict[str, list[str]] | None = None,
    ) -> StructuralFacts:
        """Index each repository through CBM and translate its facts for Sydes.

        `defer_edges=True` skips the repository-wide CALLS/USAGE sweeps
        (`all_call_edges`/`all_usage_edges`), whose paginated `query_graph`
        cost scales with total repository edge count rather than change size.
        The caller then supplies the changed symbols it has since resolved to
        `attach_bounded_edges`, which fetches only their bounded neighborhood.
        Everything else — symbols, imports, entrypoints, route semantics —
        is unaffected and stays repository-wide, because those are cheap and
        are needed before any changed symbol can be identified at all.

        `changed_files_by_repo` guards against a `mode="fast"` index that
        excluded a directory the change actually touches: when any changed
        file for a repo falls under one of that repo's `excluded.dirs`, this
        indexes that repo again with `mode="full"` — once — and continues
        with the full result. A repository whose fast index excluded nothing
        relevant to this change is indexed exactly as before.
        """
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
        index_mode_notes: list[str] = []

        for repo in repos:
            index_started = time.perf_counter()
            index_payload = client.index_repository(repo.root)
            index_mode = "fast"
            retried = False
            retry_reason: str | None = None
            triggering_files: list[str] = []

            excluded_dirs = _excluded_dirs_from_index_payload(index_payload)
            repo_changed_files = (changed_files_by_repo or {}).get(repo.name, [])
            triggering_files = _changed_files_under_excluded_dirs(
                repo_changed_files, excluded_dirs,
            )
            if triggering_files:
                # One retry, never more: a full index either covers the
                # excluded directory or it does not, and CBM's own failure
                # semantics (raise, no silent degrade) apply exactly as they
                # would to the original fast-mode call — no fallback to the
                # incomplete fast graph is attempted here.
                index_payload = client.index_repository(repo.root, mode="full")
                index_mode = "full"
                retried = True
                retry_reason = "changed_file_under_excluded_dir"
                index_mode_notes.append(
                    f"{repo.name}: cbm_index_mode retried fast->full "
                    f"({len(triggering_files)} changed file(s) under an excluded "
                    "directory; see trace for detail)"
                )

            _trace.record_index_mode_decision(
                repo=repo.name, initial_mode="fast", retried=retried,
                retry_reason=retry_reason, excluded_dir_count=len(excluded_dirs),
                triggering_changed_files=triggering_files, decided_mode=index_mode,
            )

            project = str(index_payload["project"])
            self._projects[repo.name] = project
            index_ms += (time.perf_counter() - index_started) * 1000.0

            query_started = time.perf_counter()
            symbols_by_file = self._symbols_for(client, project)
            imports_by_file = self._imports_for(client, project)
            if not defer_edges:
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
        if defer_edges:
            gaps.append(
                "CALLS/USAGE edges deferred: they are fetched as a bounded "
                "neighborhood around the changed symbols instead of a "
                "repository-wide sweep"
            )
        elif not call_edges:
            gaps.append("CBM returned no CALLS edges for this repository set")
        if client.malformed_rows:
            gaps.append(
                f"{client.malformed_rows} query row(s) did not match the expected "
                "column arity and were dropped rather than mis-parsed"
            )

        total_ms = (time.perf_counter() - started) * 1000.0
        session = client.metrics
        diagnostics = [
            f"code_intelligence_backend=cbm transport=persistent_mcp_stdio"
            f" cbm_server_version={client.server_version or 'unknown'}",
            f"cbm_session_start_ms={session.get('session_start_ms', 0)}"
            f" cbm_calls={session.get('calls', 0)}"
            f" cbm_mean_call_ms={session.get('mean_call_ms', 0)}",
            f"cbm_index_ms={index_ms:.1f} cbm_query_ms={query_ms:.1f}"
            f" sydes_route_semantics_ms={semantics_ms:.1f} total_ms={total_ms:.1f}",
            "cbm_supplied=symbols,spans,imports,exports,call_edges,usage_edges,entrypoints"
            "  sydes_semantics=route_index,route_graph,repo_map",
        ]
        diagnostics.extend(f"cbm_gap: {gap}" for gap in gaps)
        diagnostics.extend(index_mode_notes)

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
