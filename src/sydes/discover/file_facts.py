"""Persistent per-file structural facts for incremental discovery.

This is the substrate under the existing discovery pipeline, not a second one:
the facts stored here are produced by the *same* extractors the cold path uses
(`route_index._extract_index_for_file`, the `HandlerSymbolExtractor` adapters),
so a reused fact is byte-identical to the one a cold build would compute.

The governing invariant is:

    For the same repository state, an incremental build must produce
    semantically equivalent structural facts to a clean cold build.

Two properties of the fact set make that provable rather than hopeful.

*Route-index facts* are a pure function of `(relative_path, text, role)`. Nothing
about any other file can change them, so they may be reused whenever the file's
own SHA-256 is unchanged.

*Handler-symbol facts* are not. Each import record carries `resolved_file`,
which the extractors compute by probing the filesystem for candidate modules,
so its value depends on **which paths exist in the repository**, not only on the
importing file's bytes. Reusing such a fact across an add/delete is precisely
how a stale cross-file edge survives a rebuild. This module therefore binds
every symbol fact to a fingerprint of the repository's path set and discards all
of them when that set moves. Recomputing them is the cheap half of the work;
correctness is not negotiable against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from sydes.core.models import RepoRef
from sydes.discover.repo_map import IGNORED_DIRS
from sydes.store.workspace import ensure_workspace

FILE_FACTS_VERSION = "v1"
FACTS_SUBDIR = "cache/file_facts"
MANIFEST_FILE = "manifest.json"
FACTS_FILE = "facts.json"

#: Bumped when an extractor's output shape changes, so stale facts are dropped
#: rather than silently mixed with newly-shaped ones.
ROUTE_INDEX_FACT_VERSION = "route_index/v1"
# v2: the JS/TS extractor gained multi-line import joining, destructured
# require, anonymous default-export handlers, and type exports. Facts
# cached by v1 predate those and must not be reused.
SYMBOL_FACT_VERSION = "handler_symbols/v2"

KIND_ROUTE_INDEX = "route_index"
KIND_SYMBOLS = "symbols"

STATE_COLD = "cold"
STATE_WARM = "warm"
STATE_UPDATED = "updated"

_HASH_CHUNK_BYTES = 1 << 16


def sha256_file(path: Path) -> str:
    """Content hash of one file. Authoritative for source equality."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    """Identity of one source file at a point in time."""

    repo: str
    path: str
    sha256: str
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        return f"{self.repo}:{self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FileEntry | None:
        try:
            return cls(
                repo=str(payload["repo"]),
                path=str(payload["path"]),
                sha256=str(payload["sha256"]),
                size=int(payload.get("size", 0)),
                mtime_ns=int(payload.get("mtime_ns", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class ManifestDiff:
    """What moved between two manifests."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def path_set_changed(self) -> bool:
        """True when files appeared or disappeared.

        Import resolution probes the filesystem for candidate modules, so its
        results are a function of which paths exist. Any movement in the path
        set invalidates every previously resolved import, including those in
        files whose own bytes did not change.
        """
        return bool(self.added or self.deleted)

    @property
    def changed_count(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


@dataclass
class IndexMetrics:
    """Observability for one index build."""

    index_state: str = STATE_COLD
    files_total: int = 0
    files_reused: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_reparsed: int = 0
    file_fact_load_ms: float = 0.0
    file_hash_ms: float = 0.0
    file_parse_ms: float = 0.0
    global_derivation_ms: float = 0.0
    total_index_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_state": self.index_state,
            "files_total": self.files_total,
            "files_reused": self.files_reused,
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "files_reparsed": self.files_reparsed,
            "file_fact_load_ms": round(self.file_fact_load_ms, 3),
            "file_hash_ms": round(self.file_hash_ms, 3),
            "file_parse_ms": round(self.file_parse_ms, 3),
            "global_derivation_ms": round(self.global_derivation_ms, 3),
            "total_index_ms": round(self.total_index_ms, 3),
            "notes": list(self.notes),
        }


def collect_file_manifest(repos: list[RepoRef], *, extensions: set[str]) -> dict[str, FileEntry]:
    """Hash every indexable file across the given repositories.

    SHA-256 decides equality. `size`/`mtime_ns` are recorded alongside so a
    future build can use them as a fast pre-check, but nothing here consults
    them: a hash is computed for every file on every pass, so a touched-but-
    identical file is correctly classified as unchanged.
    """
    manifest: dict[str, FileEntry] = {}
    for repo in repos:
        root = Path(repo.root).expanduser().resolve()
        for raw_dirpath, dirnames, filenames in os.walk(root):
            dirpath = Path(raw_dirpath)
            dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
            for filename in filenames:
                path = dirpath / filename
                if path.suffix.lower() not in extensions:
                    continue
                try:
                    stat = path.stat()
                    digest = sha256_file(path)
                except OSError:
                    continue
                entry = FileEntry(
                    repo=repo.name,
                    path=path.relative_to(root).as_posix(),
                    sha256=digest,
                    size=stat.st_size,
                    mtime_ns=getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
                )
                manifest[entry.key] = entry
    return dict(sorted(manifest.items()))


def diff_manifests(
    previous: dict[str, FileEntry], current: dict[str, FileEntry]
) -> ManifestDiff:
    """Classify every file as added, modified, deleted, or unchanged."""
    added: list[str] = []
    modified: list[str] = []
    unchanged: list[str] = []
    for key, entry in current.items():
        old = previous.get(key)
        if old is None:
            added.append(key)
        elif old.sha256 != entry.sha256:
            modified.append(key)
        else:
            unchanged.append(key)
    deleted = sorted(set(previous) - set(current))
    return ManifestDiff(
        added=sorted(added),
        modified=sorted(modified),
        deleted=deleted,
        unchanged=sorted(unchanged),
    )


def path_set_fingerprint(manifest: dict[str, FileEntry]) -> str:
    """Fingerprint of *which* files exist, independent of their contents.

    Import resolution depends on this and not on file bytes, so it is tracked
    separately from the per-file hashes.
    """
    digest = hashlib.sha256()
    for key in sorted(manifest):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class FileFactStore:
    """Versioned JSON persistence for per-file facts.

    One file per repository set, holding facts keyed by `kind`, then by file
    key. Facts are plain JSON — never pickled objects — so a format change is a
    version bump rather than an unpickling hazard.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest: dict[str, FileEntry] = {}
        self.facts: dict[str, dict[str, Any]] = {KIND_ROUTE_INDEX: {}, KIND_SYMBOLS: {}}
        self.path_fingerprint: str = ""
        self.loaded = False
        self.load_reason = "not_loaded"

    # -- persistence ------------------------------------------------------

    @classmethod
    def for_repos(
        cls, workspace_id: str, repos: list[RepoRef], *, root: Path | None = None
    ) -> FileFactStore:
        """Store location for one repository set, inside the Sydes workspace."""
        canonical = sorted(
            f"{repo.name}={Path(repo.root).expanduser().resolve().as_posix()}" for repo in repos
        )
        key = hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()[:16]
        workspace = ensure_workspace(workspace_id=workspace_id, root=root)
        return cls(workspace.workspace_dir / FACTS_SUBDIR / key)

    def load(self) -> str:
        """Load persisted facts. Returns the reason a cold build is required."""
        manifest_path = self.directory / MANIFEST_FILE
        facts_path = self.directory / FACTS_FILE
        if not manifest_path.exists() or not facts_path.exists():
            self.load_reason = "no_previous_index"
            return self.load_reason
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_reason = "index_unreadable"
            return self.load_reason
        if not isinstance(manifest_payload, dict) or not isinstance(facts_payload, dict):
            self.load_reason = "index_malformed"
            return self.load_reason
        if manifest_payload.get("file_facts_version") != FILE_FACTS_VERSION:
            self.load_reason = "file_facts_version_changed"
            return self.load_reason
        versions = manifest_payload.get("fact_versions") or {}
        if versions.get(KIND_ROUTE_INDEX) != ROUTE_INDEX_FACT_VERSION:
            self.load_reason = "route_index_fact_version_changed"
            return self.load_reason
        if versions.get(KIND_SYMBOLS) != SYMBOL_FACT_VERSION:
            self.load_reason = "symbol_fact_version_changed"
            return self.load_reason

        entries: dict[str, FileEntry] = {}
        for key, payload in (manifest_payload.get("files") or {}).items():
            if isinstance(payload, dict):
                entry = FileEntry.from_dict(payload)
                if entry is not None:
                    entries[key] = entry
        self.manifest = entries
        self.path_fingerprint = str(manifest_payload.get("path_set_fingerprint") or "")
        for kind in (KIND_ROUTE_INDEX, KIND_SYMBOLS):
            stored = facts_payload.get(kind)
            self.facts[kind] = stored if isinstance(stored, dict) else {}
        self.loaded = True
        self.load_reason = "loaded"
        return self.load_reason

    def save(self, manifest: dict[str, FileEntry]) -> Path:
        """Persist the manifest and the current fact set."""
        self.directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(tz=UTC).isoformat()
        payload = {
            "file_facts_version": FILE_FACTS_VERSION,
            "updated_at": now,
            "fact_versions": {
                KIND_ROUTE_INDEX: ROUTE_INDEX_FACT_VERSION,
                KIND_SYMBOLS: SYMBOL_FACT_VERSION,
            },
            "path_set_fingerprint": path_set_fingerprint(manifest),
            "files": {key: entry.to_dict() for key, entry in sorted(manifest.items())},
        }
        (self.directory / MANIFEST_FILE).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.directory / FACTS_FILE).write_text(
            json.dumps(self.facts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # The in-memory state must track what was just persisted. Leaving it
        # stale would make a second build in the same process compare against
        # an older baseline and misreport which files moved.
        self.manifest = dict(manifest)
        self.path_fingerprint = str(payload["path_set_fingerprint"])
        self.loaded = True
        self.load_reason = "saved"
        return self.directory

    def size_bytes(self) -> int:
        """On-disk size of the persisted index."""
        total = 0
        for name in (MANIFEST_FILE, FACTS_FILE):
            path = self.directory / name
            if path.exists():
                total += path.stat().st_size
        return total

    # -- fact access ------------------------------------------------------

    def get(self, kind: str, key: str, sha256: str) -> Any | None:
        """A stored fact, only when it was derived from exactly this content."""
        record = self.facts.get(kind, {}).get(key)
        if not isinstance(record, dict) or record.get("sha256") != sha256:
            return None
        return record.get("fact")

    def put(self, kind: str, key: str, sha256: str, fact: Any) -> None:
        self.facts.setdefault(kind, {})[key] = {"sha256": sha256, "fact": fact}

    def drop(self, kind: str, key: str) -> None:
        self.facts.get(kind, {}).pop(key, None)

    def drop_all(self, kind: str) -> None:
        self.facts[kind] = {}

    def prune_to(self, keys: set[str]) -> None:
        """Remove facts for files that no longer exist.

        Deleted files must leave no residue: a fact that outlives its file is
        the mechanism by which a stale cross-file edge survives a rebuild.
        """
        for kind in (KIND_ROUTE_INDEX, KIND_SYMBOLS):
            for key in list(self.facts.get(kind, {})):
                if key not in keys:
                    del self.facts[kind][key]


class _Clock:
    """Accumulates elapsed milliseconds under a named bucket."""

    def __init__(self) -> None:
        self.totals: dict[str, float] = {}

    def measure(self, name: str):
        return _Span(self, name)

    def add(self, name: str, elapsed_ms: float) -> None:
        self.totals[name] = self.totals.get(name, 0.0) + elapsed_ms

    def get(self, name: str) -> float:
        return self.totals.get(name, 0.0)


class _Span:
    def __init__(self, clock: _Clock, name: str) -> None:
        self._clock = clock
        self._name = name
        self._start = 0.0

    def __enter__(self) -> _Span:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._clock.add(self._name, (time.perf_counter() - self._start) * 1000.0)


@dataclass
class FactCache:
    """Per-file reuse hook handed to an existing builder.

    The builder stays the single source of parsing; this only lets it skip the
    extraction step for a file whose content hash already has a stored fact.
    A miss is always safe — it falls through to the normal parse.
    """

    repo: str
    kind: str
    store: FileFactStore
    manifest: dict[str, FileEntry]
    enabled: bool = True
    hits: int = 0
    misses: int = 0

    #: Distinct file keys this cache served or had to extract, so callers can
    #: report *files* rather than fact-extractions (a file yields one fact per
    #: kind, and counting those double-counts the file).
    reused_keys: set[str] = field(default_factory=set)
    parsed_keys: set[str] = field(default_factory=set)

    def lookup(self, relative_path: str) -> Any | None:
        if not self.enabled:
            return None
        key = f"{self.repo}:{relative_path}"
        entry = self.manifest.get(key)
        if entry is None:
            return None
        fact = self.store.get(self.kind, key, entry.sha256)
        if fact is None:
            self.misses += 1
            return None
        self.hits += 1
        self.reused_keys.add(key)
        # Facts round-trip through JSON, so a reused fact is a fresh object and
        # a caller mutating it cannot corrupt the store.
        return json.loads(json.dumps(fact))

    def record(self, relative_path: str, fact: Any) -> None:
        key = f"{self.repo}:{relative_path}"
        entry = self.manifest.get(key)
        if entry is None:
            return
        self.parsed_keys.add(key)
        self.store.put(self.kind, key, entry.sha256, fact)


# --------------------------------------------------------------------------
# Incremental structural index
# --------------------------------------------------------------------------

#: Extensions that can carry structural facts. A superset of what either
#: builder indexes, so the manifest never misses a file one of them would read.
INDEXABLE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".java", ".go", ".rb", ".php", ".cs", ".kt",
}


@dataclass
class StructuralIndex:
    """Structural facts for a repository set, however they were obtained."""

    repo_map_batch: dict[str, Any]
    route_index_batch: dict[str, Any]
    handler_symbol_batch: dict[str, Any]
    route_graph_facts: dict[str, Any]
    metrics: IndexMetrics
    manifest: dict[str, FileEntry] = field(default_factory=dict)


def build_structural_index(
    repos: list[RepoRef],
    *,
    workspace_id: str | None = None,
    store: FileFactStore | None = None,
    root: Path | None = None,
    persist: bool = True,
) -> StructuralIndex:
    """Build the structural fact set, reusing per-file facts where sound.

    The algorithm is deliberately asymmetric, and that asymmetry is the whole
    correctness argument:

    * **Per-file extraction is reused.** This is the expensive half and the only
      half that is genuinely file-local.
    * **Every cross-file and global derivation is recomputed from scratch**, on
      every build, from the reassembled fact set — route composition, mount
      resolution, the route graph, and the summaries. Nothing derived is ever
      carried forward, so no derived fact can outlive the inputs that justified
      it.

    That is why a change followed by its revert lands exactly where a cold build
    lands: the reused inputs are content-addressed, and everything computed from
    them is computed fresh.
    """
    from sydes.discover.repo_map import build_repo_map_batch
    from sydes.discover.route_graph import build_route_graph_facts_from_route_index_batch
    from sydes.discover.route_index import build_route_index
    from sydes.trace.handler_symbols.index import build_handler_symbol_index

    clock = _Clock()
    started = time.perf_counter()
    metrics = IndexMetrics()

    # Callers name a workspace; only this module knows where facts live.
    if store is None and workspace_id is not None:
        store = FileFactStore.for_repos(workspace_id, repos, root=root)

    with clock.measure("load"):
        if store is not None and not store.loaded:
            store.load()
        previous = dict(store.manifest) if store is not None else {}
        previous_fingerprint = store.path_fingerprint if store is not None else ""

    with clock.measure("hash"):
        manifest = collect_file_manifest(repos, extensions=INDEXABLE_EXTS)

    diff = diff_manifests(previous, manifest)
    fingerprint = path_set_fingerprint(manifest)

    had_previous = bool(previous) and (store is not None and store.loaded)
    if not had_previous:
        metrics.index_state = STATE_COLD
    elif diff.changed_count == 0:
        metrics.index_state = STATE_WARM
    else:
        metrics.index_state = STATE_UPDATED

    metrics.files_total = len(manifest)
    metrics.files_added = len(diff.added)
    metrics.files_modified = len(diff.modified)
    metrics.files_deleted = len(diff.deleted)

    # A file that vanished must take its facts with it.
    if store is not None:
        store.prune_to(set(manifest))

    # Import resolution probes the filesystem, so a moved path set invalidates
    # every symbol fact — including those of files whose bytes are unchanged.
    symbols_reusable = had_previous and not diff.path_set_changed and fingerprint == previous_fingerprint
    if store is not None and not symbols_reusable:
        store.drop_all(KIND_SYMBOLS)
        if had_previous and diff.path_set_changed:
            metrics.notes.append(
                "symbol facts rebuilt: repository path set changed, so every "
                "previously resolved import had to be recomputed"
            )

    caches: dict[str, dict[str, FactCache]] = {}
    if store is not None:
        for repo in repos:
            caches[repo.name] = {
                KIND_ROUTE_INDEX: FactCache(
                    repo=repo.name, kind=KIND_ROUTE_INDEX, store=store, manifest=manifest
                ),
                KIND_SYMBOLS: FactCache(
                    repo=repo.name,
                    kind=KIND_SYMBOLS,
                    store=store,
                    manifest=manifest,
                    enabled=symbols_reusable,
                ),
            }

    with clock.measure("parse"):
        repo_map_batch = build_repo_map_batch(repos)
        repo_maps = {
            item.get("repo"): item
            for item in (repo_map_batch.get("repos") or [])
            if isinstance(item, dict)
        }
        route_indexes = []
        symbol_indexes = []
        for repo in repos:
            repo_caches = caches.get(repo.name, {})
            route_indexes.append(
                build_route_index(
                    repo,
                    repo_map=repo_maps.get(repo.name),
                    fact_cache=repo_caches.get(KIND_ROUTE_INDEX),
                )
            )
            symbol_indexes.append(
                build_handler_symbol_index(repo, fact_cache=repo_caches.get(KIND_SYMBOLS))
            )

    route_index_batch = {
        "version": "v1",
        "repos": route_indexes,
        "summary": {
            key: sum(item["summary"].get(key, 0) for item in route_indexes)
            for key in (
                "files_indexed",
                "files_with_route_calls",
                "route_call_count",
                "mount_call_count",
                "router_symbol_count",
            )
        },
    }
    handler_symbol_batch = _combine_symbol_indexes(symbol_indexes)

    # Global derivation always runs, from the reassembled facts only.
    with clock.measure("global"):
        route_graph_facts = build_route_graph_facts_from_route_index_batch(route_index_batch)

    # Counted as distinct files, not fact-extractions: one file contributes a
    # route-index fact and a symbol fact, and reporting both would double it.
    parsed_keys: set[str] = set()
    reused_keys: set[str] = set()
    for repo_caches in caches.values():
        for cache in repo_caches.values():
            parsed_keys |= cache.parsed_keys
            reused_keys |= cache.reused_keys
    metrics.files_reparsed = len(parsed_keys)
    metrics.files_reused = len(reused_keys - parsed_keys)

    if store is not None and persist:
        store.save(manifest)

    metrics.file_fact_load_ms = clock.get("load")
    metrics.file_hash_ms = clock.get("hash")
    metrics.file_parse_ms = clock.get("parse")
    metrics.global_derivation_ms = clock.get("global")
    metrics.total_index_ms = (time.perf_counter() - started) * 1000.0

    return StructuralIndex(
        repo_map_batch=repo_map_batch,
        route_index_batch=route_index_batch,
        handler_symbol_batch=handler_symbol_batch,
        route_graph_facts=route_graph_facts,
        metrics=metrics,
        manifest=manifest,
    )


def _combine_symbol_indexes(indexes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-repo symbol indexes exactly as the batch builder does."""
    totals: dict[str, int] = {}
    for item in indexes:
        for key, value in (item.get("summary") or {}).items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return {"version": "v1", "repos": indexes, "summary": dict(sorted(totals.items()))}


def structural_index_diagnostics(index: StructuralIndex) -> list[str]:
    """Compact, human-readable metric lines for a diagnostics section."""
    metrics = index.metrics
    lines = [
        f"index_state={metrics.index_state}"
        f" files_total={metrics.files_total}"
        f" files_reused={metrics.files_reused}"
        f" files_reparsed={metrics.files_reparsed}"
        f" added={metrics.files_added}"
        f" modified={metrics.files_modified}"
        f" deleted={metrics.files_deleted}",
        f"index_total_ms={metrics.total_index_ms:.1f}"
        f" hash_ms={metrics.file_hash_ms:.1f}"
        f" parse_ms={metrics.file_parse_ms:.1f}"
        f" global_ms={metrics.global_derivation_ms:.1f}",
    ]
    lines.extend(f"index_note: {note}" for note in metrics.notes)
    return lines
