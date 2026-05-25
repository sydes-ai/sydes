"""Route-graph facts and Express-style mount composition from route-index signals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from sydes.core.models import EndpointCandidate, EvidenceRef, RepoRef
from sydes.discover.route_index import build_route_index_batch

_EXTS = (".ts", ".tsx", ".js", ".jsx")


@dataclass(frozen=True)
class _Container:
    id: str
    symbol: str
    file: str
    kind: str


@dataclass(frozen=True)
class _Declaration:
    container_id: str
    method: str
    path: str
    handler_hint: str | None
    file: str
    line: int | None
    snippet: str | None


@dataclass(frozen=True)
class _Mount:
    parent_container_id: str
    child_symbol: str
    child_container_id: str | None
    prefix: str
    file: str
    line: int | None
    snippet: str | None


def _normalize_basic_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        p = "/"
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/+", "/", p)
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p


def _normalize_express_path(path: str) -> str:
    normalized = re.sub(r":([A-Za-z_]\w*)", r"{\1}", path)
    return _normalize_basic_path(normalized)


def _join_paths(prefix: str, leaf: str) -> str:
    p = _normalize_basic_path(prefix)
    l = _normalize_basic_path(leaf)
    if p == "/":
        return l
    if l == "/":
        return p
    return _normalize_basic_path(f"{p.rstrip('/')}/{l.lstrip('/')}")


def _import_target_candidates(parent_file: str, source: str) -> list[str]:
    base = Path(parent_file).parent
    raw = (base / source).as_posix()
    candidates: list[str] = []
    if Path(raw).suffix:
        candidates.append(raw)
    else:
        for ext in _EXTS:
            candidates.append(raw + ext)
        for ext in _EXTS:
            candidates.append((Path(raw) / f"index{ext}").as_posix())
    seen: set[str] = set()
    ordered: list[str] = []
    for item in candidates:
        normalized = Path(item).as_posix()
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _build_route_graph_for_repo(repo_payload: dict) -> dict:
    repo = str(repo_payload.get("repo") or "")
    files_payload = repo_payload.get("files") or []

    containers: dict[str, _Container] = {}
    containers_by_file_symbol: dict[tuple[str, str], str] = {}
    default_export_symbol_by_file: dict[str, str] = {}
    imports_by_file: dict[str, dict[str, list[str]]] = {}
    unresolved_imports: list[dict] = []

    for file_item in files_payload:
        file_path = str(file_item.get("path") or "")
        for symbol in file_item.get("router_symbols") or []:
            container_id = f"{file_path}::{symbol}"
            container = _Container(id=container_id, symbol=symbol, file=file_path, kind="express_router")
            containers[container_id] = container
            containers_by_file_symbol[(file_path, symbol)] = container_id

        for export in file_item.get("exports") or []:
            if export.get("kind") == "default" and isinstance(export.get("symbol"), str):
                default_export_symbol_by_file[file_path] = export["symbol"]

        local_map: dict[str, list[str]] = {}
        for imp in file_item.get("imports") or []:
            local = imp.get("local")
            source = imp.get("source")
            if not isinstance(local, str) or not isinstance(source, str):
                continue
            local_map[local] = _import_target_candidates(file_path, source)
        imports_by_file[file_path] = local_map

    declarations: list[_Declaration] = []
    mounts: list[_Mount] = []
    unresolved_mounts: list[dict] = []

    for file_item in files_payload:
        file_path = str(file_item.get("path") or "")

        def resolve_local_symbol(symbol: str) -> str | None:
            direct = containers_by_file_symbol.get((file_path, symbol))
            if direct:
                return direct
            import_targets = imports_by_file.get(file_path, {}).get(symbol, [])
            for target_file in import_targets:
                exported_symbol = default_export_symbol_by_file.get(target_file)
                if exported_symbol:
                    cid = containers_by_file_symbol.get((target_file, exported_symbol))
                    if cid:
                        return cid
                cid = containers_by_file_symbol.get((target_file, symbol))
                if cid:
                    return cid
            return None

        for call in file_item.get("route_calls") or []:
            receiver = call.get("receiver")
            method = call.get("method")
            path = call.get("path")
            if not isinstance(receiver, str) or not isinstance(method, str) or not isinstance(path, str):
                continue
            container_id = resolve_local_symbol(receiver)
            if container_id is None:
                container_id = f"{file_path}::{receiver}"
                if container_id not in containers:
                    containers[container_id] = _Container(
                        id=container_id,
                        symbol=receiver,
                        file=file_path,
                        kind="synthetic_receiver",
                    )
                    containers_by_file_symbol[(file_path, receiver)] = container_id
            declarations.append(
                _Declaration(
                    container_id=container_id,
                    method=method.upper(),
                    path=_normalize_express_path(path),
                    handler_hint=call.get("handler_hint") if isinstance(call.get("handler_hint"), str) else None,
                    file=file_path,
                    line=call.get("line") if isinstance(call.get("line"), int) else None,
                    snippet=call.get("snippet") if isinstance(call.get("snippet"), str) else None,
                )
            )

        for mount in file_item.get("mount_calls") or []:
            receiver = mount.get("receiver")
            child_symbol = mount.get("child")
            prefix = mount.get("prefix")
            if not isinstance(receiver, str) or not isinstance(prefix, str):
                continue

            parent_container_id = resolve_local_symbol(receiver)
            if parent_container_id is None:
                parent_container_id = f"{file_path}::{receiver}"
                if parent_container_id not in containers:
                    containers[parent_container_id] = _Container(
                        id=parent_container_id,
                        symbol=receiver,
                        file=file_path,
                        kind="synthetic_root" if receiver == "app" else "synthetic_receiver",
                    )
                    containers_by_file_symbol[(file_path, receiver)] = parent_container_id

            resolved_child_id = None
            if isinstance(child_symbol, str) and child_symbol:
                resolved_child_id = resolve_local_symbol(child_symbol)

            if isinstance(child_symbol, str) and child_symbol:
                for target_file in imports_by_file.get(file_path, {}).get(child_symbol, []):
                    if target_file not in default_export_symbol_by_file and target_file not in [f for f, _ in containers_by_file_symbol.keys()]:
                        unresolved_imports.append(
                            {
                                "file": file_path,
                                "symbol": child_symbol,
                                "import_target": target_file,
                            }
                        )

            if resolved_child_id is None:
                unresolved_mounts.append(
                    {
                        "file": file_path,
                        "receiver": receiver,
                        "prefix": _normalize_basic_path(prefix),
                        "child_symbol": child_symbol,
                    }
                )

            mounts.append(
                _Mount(
                    parent_container_id=parent_container_id,
                    child_symbol=child_symbol if isinstance(child_symbol, str) else "",
                    child_container_id=resolved_child_id,
                    prefix=_normalize_basic_path(prefix),
                    file=file_path,
                    line=mount.get("line") if isinstance(mount.get("line"), int) else None,
                    snippet=mount.get("snippet") if isinstance(mount.get("snippet"), str) else None,
                )
            )

    incoming: dict[str, list[_Mount]] = {}
    for m in mounts:
        if m.child_container_id is None:
            continue
        incoming.setdefault(m.child_container_id, []).append(m)

    cache: dict[str, list[tuple[str, list[_Mount]]]] = {}

    def prefixes_for(container_id: str, seen: set[str] | None = None) -> list[tuple[str, list[_Mount]]]:
        if container_id in cache:
            return cache[container_id]
        seen = seen or set()
        if container_id in seen:
            return []
        seen = set(seen)
        seen.add(container_id)

        parents = incoming.get(container_id, [])
        if not parents:
            result = [("", [])]
            cache[container_id] = result
            return result

        combos: list[tuple[str, list[_Mount]]] = []
        for edge in parents:
            parent_prefixes = prefixes_for(edge.parent_container_id, seen)
            if not parent_prefixes:
                parent_prefixes = [("", [])]
            for prefix, chain in parent_prefixes:
                combined = _join_paths(prefix or "/", edge.prefix)
                combos.append((combined, [*chain, edge]))

        dedup: dict[tuple[str, tuple[str, ...]], tuple[str, list[_Mount]]] = {}
        for prefix, chain in combos:
            key = (prefix, tuple(m.file + ":" + str(m.line or 0) + ":" + m.prefix for m in chain))
            dedup[key] = (prefix, chain)
        result = list(dedup.values())
        cache[container_id] = result
        return result

    composed: list[EndpointCandidate] = []
    composed_fact_rows: list[dict] = []

    for dec in declarations:
        combos = prefixes_for(dec.container_id)
        if not combos:
            combos = [("", [])]
        for prefix, chain in combos:
            full_path = _join_paths(prefix or "/", dec.path)
            evidence: list[EvidenceRef] = []
            if dec.snippet:
                evidence.append(
                    EvidenceRef(
                        file=dec.file,
                        symbol=dec.handler_hint,
                        label="route_declaration",
                        snippet=dec.snippet,
                    )
                )
            for edge in chain:
                if edge.snippet:
                    evidence.append(
                        EvidenceRef(
                            file=edge.file,
                            symbol=edge.child_symbol or None,
                            label="mount_edge",
                            snippet=edge.snippet,
                        )
                    )
            composed.append(
                EndpointCandidate(
                    method=dec.method,
                    path=full_path,
                    handler=dec.handler_hint,
                    file=dec.file,
                    repo=repo,
                    evidence=evidence,
                    confidence=1.0,
                    status="deterministic_composed",
                )
            )
            composed_fact_rows.append(
                {
                    "method": dec.method,
                    "path": full_path,
                    "file": dec.file,
                    "handler": dec.handler_hint,
                    "evidence": [item.model_dump() for item in evidence],
                }
            )

    container_rows = [
        {
            "id": item.id,
            "symbol": item.symbol,
            "file": item.file,
            "kind": item.kind,
        }
        for item in sorted(containers.values(), key=lambda c: c.id)
    ]
    declaration_rows = [
        {
            "container_id": d.container_id,
            "method": d.method,
            "path": d.path,
            "handler_hint": d.handler_hint,
            "file": d.file,
            "line": d.line,
            "evidence": [
                {
                    "file": d.file,
                    "symbol": d.handler_hint,
                    "label": "route_declaration",
                    "snippet": d.snippet,
                }
            ],
        }
        for d in declarations
    ]
    mount_rows = [
        {
            "parent_container_id": m.parent_container_id,
            "child_symbol": m.child_symbol,
            "child_container_id": m.child_container_id,
            "prefix": m.prefix,
            "file": m.file,
            "line": m.line,
            "evidence": [
                {
                    "file": m.file,
                    "symbol": m.child_symbol or None,
                    "label": "mount_edge",
                    "snippet": m.snippet,
                }
            ],
        }
        for m in mounts
    ]

    return {
        "repo": repo,
        "containers": container_rows,
        "declarations": declaration_rows,
        "mount_edges": mount_rows,
        "composed_routes": composed_fact_rows,
        "unresolved_imports": unresolved_imports,
        "unresolved_mounts": unresolved_mounts,
        "summary": {
            "containers": len(container_rows),
            "declarations": len(declaration_rows),
            "mount_edges": len(mount_rows),
            "composed_routes": len(composed_fact_rows),
            "unresolved_mounts": len(unresolved_mounts),
        },
        "_composed_endpoint_candidates": composed,
    }


def build_route_graph_facts_from_route_index_batch(route_index_batch: dict) -> dict:
    """Build route-graph facts from route-index batch payload."""
    repos_payload = route_index_batch.get("repos") if isinstance(route_index_batch, dict) else []
    if not isinstance(repos_payload, list):
        repos_payload = []

    repo_facts = [_build_route_graph_for_repo(item) for item in repos_payload if isinstance(item, dict)]

    totals = {
        "containers": sum(item["summary"]["containers"] for item in repo_facts),
        "declarations": sum(item["summary"]["declarations"] for item in repo_facts),
        "mount_edges": sum(item["summary"]["mount_edges"] for item in repo_facts),
        "composed_routes": sum(item["summary"]["composed_routes"] for item in repo_facts),
        "unresolved_mounts": sum(item["summary"]["unresolved_mounts"] for item in repo_facts),
    }

    return {
        "version": "v1",
        "repos": [
            {
                "repo": item["repo"],
                "containers": item["containers"],
                "declarations": item["declarations"],
                "mount_edges": item["mount_edges"],
                "composed_routes": item["composed_routes"],
                "unresolved_imports": item["unresolved_imports"],
                "unresolved_mounts": item["unresolved_mounts"],
                "summary": item["summary"],
            }
            for item in repo_facts
        ],
        "summary": totals,
        "_repo_endpoint_candidates": {
            item["repo"]: item["_composed_endpoint_candidates"] for item in repo_facts
        },
    }


def build_route_graph_facts_batch(repos: list[RepoRef], *, route_index_batch: dict | None = None) -> dict:
    """Build route-graph facts batch from repos or provided route-index batch."""
    index_batch = route_index_batch or build_route_index_batch(repos)
    return build_route_graph_facts_from_route_index_batch(index_batch)
