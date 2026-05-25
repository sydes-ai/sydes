"""Deterministic route index artifact builder.

This index captures compact, route-like structural signals to support later
hierarchical discovery planning. It is not the final discovered route list.
"""

from __future__ import annotations

from collections import Counter
import re
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.repo_map import IGNORED_DIRS, build_repo_map
from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    classify_candidate_file_role,
)

SUPPORTED_EXTS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".java",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".kt",
}

LANGUAGE_BY_EXT = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
}

_ROUTE_METHODS = "get|post|put|patch|delete|head|options|all"
_ROUTE_CALL_RE = re.compile(
    rf"(?P<receiver>[A-Za-z_][\w\.]*)\s*\.\s*(?P<method>{_ROUTE_METHODS})\s*\(\s*['\"](?P<path>/[^'\"]*)['\"]",
    re.IGNORECASE,
)
_MOUNT_CALL_RE = re.compile(
    r"(?P<receiver>[A-Za-z_][\w\.]*)\s*\.\s*use\s*\(\s*['\"](?P<prefix>/[^'\"]*)['\"]\s*,\s*(?P<args>[^\)]*)\)",
    re.IGNORECASE,
)
_ROUTER_DECL_RE = re.compile(
    r"(?:const|let|var)\s+(?P<symbol>[A-Za-z_][\w]*)\s*=\s*(?:[A-Za-z_][\w]*\.)?Router\s*\(\s*\)",
)
_IMPORT_DEFAULT_RE = re.compile(
    r"import\s+(?P<local>[A-Za-z_][\w]*)\s+from\s+['\"](?P<source>[^'\"]+)['\"]"
)
_IMPORT_NAMED_RE = re.compile(
    r"import\s*\{(?P<named>[^}]+)\}\s*from\s*['\"](?P<source>[^'\"]+)['\"]"
)
_EXPORT_DEFAULT_RE = re.compile(r"export\s+default\s+(?P<symbol>[A-Za-z_][\w]*)")
_EXPORT_NAMED_RE = re.compile(r"export\s*\{(?P<named>[^}]+)\}")
_MODULE_EXPORT_RE = re.compile(r"module\.exports\s*=\s*(?P<symbol>[A-Za-z_][\w]*)")
_PATH_LITERAL_RE = re.compile(r"['\"](?P<path>/[^'\"\s]*)['\"]")

_MAX_FILE_SIZE = 2_000_000
_MAX_SNIPPET_CHARS = 300
_MAX_PATH_LITERALS_PER_FILE = 100


def _trim(text: str) -> str:
    text = " ".join(text.strip().split())
    if len(text) > _MAX_SNIPPET_CHARS:
        return text[: _MAX_SNIPPET_CHARS - 3] + "..."
    return text


def _path_in_dirs(relative_path: str, preferred_dirs: set[str]) -> bool:
    if not preferred_dirs:
        return True
    for directory in preferred_dirs:
        if relative_path == directory or relative_path.startswith(directory + "/"):
            return True
    return False


def _extract_imports(line: str) -> list[dict[str, str]]:
    imports: list[dict[str, str]] = []
    match = _IMPORT_DEFAULT_RE.search(line)
    if match:
        imports.append({"local": match.group("local"), "source": match.group("source")})
    match = _IMPORT_NAMED_RE.search(line)
    if match:
        source = match.group("source")
        for item in match.group("named").split(","):
            name = item.strip()
            if not name:
                continue
            imports.append({"local": name.split(" as ")[-1].strip(), "source": source})
    return imports


def _extract_exports(line: str) -> list[dict[str, str]]:
    exports: list[dict[str, str]] = []
    match = _EXPORT_DEFAULT_RE.search(line)
    if match:
        exports.append({"kind": "default", "symbol": match.group("symbol")})
    match = _EXPORT_NAMED_RE.search(line)
    if match:
        for item in match.group("named").split(","):
            name = item.strip()
            if not name:
                continue
            exports.append({"kind": "named", "symbol": name.split(" as ")[0].strip()})
    match = _MODULE_EXPORT_RE.search(line)
    if match:
        exports.append({"kind": "commonjs", "symbol": match.group("symbol")})
    return exports


def _extract_index_for_file(relative_path: str, text: str, role: str) -> dict:
    ext = Path(relative_path).suffix.lower()
    language = LANGUAGE_BY_EXT.get(ext, "unknown")

    route_calls: list[dict] = []
    mount_calls: list[dict] = []
    router_symbols: list[str] = []
    imports: list[dict] = []
    exports: list[dict] = []
    path_literals: list[str] = []
    signals: set[str] = set()

    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        for match in _ROUTER_DECL_RE.finditer(line):
            symbol = match.group("symbol")
            router_symbols.append(symbol)
            signals.add("router_instance:express.Router")

        for match in _ROUTE_CALL_RE.finditer(line):
            method = match.group("method").lower()
            path = match.group("path")
            receiver = match.group("receiver")
            route_calls.append(
                {
                    "receiver": receiver,
                    "method": method,
                    "path": path,
                    "handler_hint": None,
                    "line": idx,
                    "snippet": _trim(raw_line),
                }
            )
            signals.add(f"route_call:{method}")

        for match in _MOUNT_CALL_RE.finditer(line):
            args = [part.strip() for part in match.group("args").split(",") if part.strip()]
            child = None
            for arg in reversed(args):
                if re.fullmatch(r"[A-Za-z_][\w\.]*", arg):
                    child = arg
                    break
            mount_calls.append(
                {
                    "receiver": match.group("receiver"),
                    "prefix": match.group("prefix"),
                    "child": child,
                    "line": idx,
                    "snippet": _trim(raw_line),
                }
            )
            signals.add("mount_call:use")

        imports.extend(_extract_imports(line))
        exports.extend(_extract_exports(line))

        for match in _PATH_LITERAL_RE.finditer(line):
            candidate = match.group("path")
            if candidate.startswith("//"):
                continue
            if candidate.startswith("/http"):
                continue
            if candidate not in path_literals:
                path_literals.append(candidate)
            if len(path_literals) >= _MAX_PATH_LITERALS_PER_FILE:
                break

    if path_literals:
        signals.add("path_literals")
    if any(item.get("kind") == "default" for item in exports):
        signals.add("default_export")

    return {
        "path": relative_path,
        "language": language,
        "role": role,
        "signals": sorted(signals),
        "router_symbols": sorted(set(router_symbols)),
        "route_calls": route_calls,
        "mount_calls": mount_calls,
        "imports": imports,
        "exports": exports,
        "path_literals": path_literals,
    }


def build_route_index(repo: RepoRef, *, repo_map: dict | None = None) -> dict:
    """Build compact deterministic route-signal index for one repository."""
    root = Path(repo.root).expanduser().resolve()
    repo_map_payload = repo_map or build_repo_map(repo)
    preferred_dirs = {
        item for item in (
            repo_map_payload.get("candidate_route_dirs", [])
            + repo_map_payload.get("candidate_backend_dirs", [])
        )
        if item and item != "."
    }

    files: list[dict] = []
    files_indexed = 0
    files_with_route_calls = 0
    route_call_count = 0
    mount_call_count = 0
    router_symbol_count = 0

    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
        for filename in filenames:
            path = dirpath / filename
            rel = path.relative_to(root).as_posix()
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTS:
                continue
            role = classify_candidate_file_role(rel)
            if role != FILE_ROLE_SOURCE_ROUTE_CANDIDATE:
                continue
            if not _path_in_dirs(rel, preferred_dirs) and not _path_in_dirs(str(Path(rel).parent), preferred_dirs):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_SIZE:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            entry = _extract_index_for_file(rel, text, role)
            files.append(entry)
            files_indexed += 1
            if entry["route_calls"]:
                files_with_route_calls += 1
            route_call_count += len(entry["route_calls"])
            mount_call_count += len(entry["mount_calls"])
            router_symbol_count += len(entry["router_symbols"])

    files.sort(key=lambda item: item["path"])
    signal_counts = Counter()
    for item in files:
        for signal in item["signals"]:
            signal_counts[signal] += 1

    return {
        "repo": repo.name,
        "root": str(root),
        "files": files,
        "summary": {
            "files_indexed": files_indexed,
            "files_with_route_calls": files_with_route_calls,
            "route_call_count": route_call_count,
            "mount_call_count": mount_call_count,
            "router_symbol_count": router_symbol_count,
            "signal_counts": dict(sorted(signal_counts.items())),
        },
    }


def build_route_index_batch(repos: list[RepoRef], *, repo_map_batch: dict | None = None) -> dict:
    """Build deterministic route indexes for many repositories."""
    repo_maps_by_name: dict[str, dict] = {}
    if repo_map_batch and isinstance(repo_map_batch.get("repos"), list):
        for item in repo_map_batch["repos"]:
            name = item.get("repo") if isinstance(item, dict) else None
            if isinstance(name, str):
                repo_maps_by_name[name] = item

    indexes = [
        build_route_index(repo, repo_map=repo_maps_by_name.get(repo.name))
        for repo in repos
    ]
    totals = {
        "files_indexed": sum(item["summary"].get("files_indexed", 0) for item in indexes),
        "files_with_route_calls": sum(item["summary"].get("files_with_route_calls", 0) for item in indexes),
        "route_call_count": sum(item["summary"].get("route_call_count", 0) for item in indexes),
        "mount_call_count": sum(item["summary"].get("mount_call_count", 0) for item in indexes),
        "router_symbol_count": sum(item["summary"].get("router_symbol_count", 0) for item in indexes),
    }
    return {"version": "v1", "repos": indexes, "summary": totals}
