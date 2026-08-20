"""Expand a set of changed symbols into affected system flows.

This is the core of `verify-change`. It answers "what backend behavior does this
diff touch?" by walking the call graph *upward* from each changed symbol to the
routes and event consumers that can reach it, then *downward* to the databases,
outbound clients, and events the change can reach.

Every node and edge carries evidence explaining why Sydes believes the relation
exists. Nothing is added to a flow without a concrete source line behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from sydes.core.models import CandidateFileRead, EndpointCandidate, EvidenceRef, ReadFileSnippet, RepoRef
from sydes.discover.deterministic_routes import extract_deterministic_routes
from sydes.discover.route_graph import build_route_graph_facts_from_route_index_batch
from sydes.discover.route_index import SUPPORTED_EXTS, _extract_index_for_file
from sydes.ingest.file_roles import FILE_ROLE_SOURCE_ROUTE_CANDIDATE
from sydes.verify.events import CONSUME, PUBLISH, EventSignal
from sydes.verify.models import (
    NODE_CLIENT,
    NODE_CONSUMER,
    NODE_DATABASE,
    NODE_EVENT,
    NODE_EXTERNAL,
    NODE_FUNCTION,
    NODE_HANDLER,
    NODE_REPOSITORY,
    NODE_ROUTE,
    NODE_SERVICE,
    AffectedFlow,
    FlowEdge,
    FlowNode,
)
from sydes.verify.repo_scan import RepoScan
from sydes.verify.symbol_index import Symbol, SymbolIndex

MAX_UPSTREAM_DEPTH = 6
MAX_DOWNSTREAM_DEPTH = 3
MAX_FLOWS = 24
MAX_ROUTES_PER_SYMBOL = 6

_DB_PATTERNS = [
    (re.compile(r"\b(?:INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+.+\s+FROM)\b", re.IGNORECASE), "write_or_read"),
    (re.compile(r"\b(?:session|db|conn|client|pool)\s*\.\s*(?:execute|query|commit|add|delete|flush|refresh)\s*\("), "orm_or_driver"),
    (re.compile(r"\.\s*(?:save|create|update|insert|upsert|findOne|findAll|findMany|findById|deleteOne|destroy|bulkCreate)\s*\("), "orm_method"),
    (re.compile(r"\b(?:Repository|repository|repo)\s*\.\s*\w+\s*\("), "repository_call"),
]

_HTTP_PATTERNS = [
    re.compile(r"\b(?:axios|superagent|got)\s*\.\s*(?:get|post|put|patch|delete|request)\s*\("),
    re.compile(r"\bfetch\s*\(\s*[`'\"]"),
    re.compile(r"\b(?:requests|httpx|session)\s*\.\s*(?:get|post|put|patch|delete)\s*\("),
    re.compile(r"\b(?:HttpClient|RestTemplate|WebClient)\b"),
    re.compile(r"\bhttp\s*\.\s*(?:get|post|request)\s*\("),
    # Fluent clients (Spring WebClient, feign-style builders) split the verb and
    # the path across lines, so match the chain rather than a single call.
    re.compile(r"\.\s*(?:uri|retrieve|exchange|bodyToFlux|bodyToMono)\s*\("),
    re.compile(r"\b\w*[Cc]lient\s*\.\s*(?:get|post|put|patch|delete|head)\s*\("),
]

_CACHE_PATTERN = re.compile(r"\b(?:redis|cache|redisClient)\s*\.\s*(?:get|set|del|expire|hget|hset|incr)\s*\(")

_URL_LITERAL = re.compile(r"['\"`](?P<url>(?:https?://[^'\"`\s]+)|(?:/[A-Za-z0-9_{}:./\-]*))['\"`]")

_CONSUMER_HANDLER_ARG = re.compile(
    r"['\"`][^'\"`]+['\"`]\s*,\s*(?P<handler>[A-Za-z_]\w*)\s*[),]"
)

_ROUTE_DECORATOR = re.compile(
    r"@(?P<obj>[\w.]+)\.(?P<method>get|post|put|patch|delete|head|options|route)\s*\(",
    re.IGNORECASE,
)


@dataclass(slots=True)
class RouteBinding:
    """A discovered route bound (where possible) to its handler symbol."""

    endpoint: EndpointCandidate
    handler_symbol_id: str | None
    declaration_line: int | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Terminal-friendly `METHOD /path` label."""
        method = (self.endpoint.method or "ANY").upper()
        return f"{method} {self.endpoint.path or '/'}"


@dataclass(slots=True)
class SystemSurface:
    """Deterministic structural view of one repository used by flow expansion."""

    repo: str
    root: Path
    routes: list[RouteBinding] = field(default_factory=list)
    events: list[EventSignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def routes_by_handler(self) -> dict[str, list[RouteBinding]]:
        """Index route bindings by resolved handler symbol id."""
        mapping: dict[str, list[RouteBinding]] = {}
        for binding in self.routes:
            if binding.handler_symbol_id:
                mapping.setdefault(binding.handler_symbol_id, []).append(binding)
        return mapping


_REQUIRE_IMPORT_RE = re.compile(
    r"(?:const|let|var)\s+(?P<local>[A-Za-z_]\w*)\s*=\s*require\(\s*['\"](?P<source>[^'\"]+)['\"]\s*\)"
)

_OPEN_ROUTE_CALL = re.compile(
    r"\.\s*(?:get|post|put|patch|delete|head|options|all|use|route)\s*\(\s*$",
    re.IGNORECASE,
)


def _collapse_multiline_route_calls(text: str) -> str:
    """Join route calls whose arguments start on the next line.

    Sydes' route regexes expect the path literal on the same line as the verb.
    Content is pulled up rather than deleted, so the total line count — and
    therefore every reported line number — stays correct.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not _OPEN_ROUTE_CALL.search(lines[index]):
            index += 1
            continue
        merged = lines[index]
        depth = merged.count("(") - merged.count(")")
        cursor = index + 1
        while cursor < len(lines) and depth > 0 and cursor - index <= 6:
            merged = merged.rstrip() + " " + lines[cursor].strip()
            depth += lines[cursor].count("(") - lines[cursor].count(")")
            lines[cursor] = ""
            cursor += 1
        lines[index] = merged
        index = cursor
    return "\n".join(lines)


def _candidate_reads(scan: RepoScan) -> list[CandidateFileRead]:
    """Adapt scanned files to the CandidateFileRead shape route extraction expects."""
    reads: list[CandidateFileRead] = []
    for scanned in scan.files:
        if not scanned.is_app_source:
            continue
        reads.append(
            CandidateFileRead(
                repo=scanned.repo,
                relative_path=scanned.path,
                role=FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
                snippet=ReadFileSnippet(
                    repo=scanned.repo,
                    relative_path=scanned.path,
                    text=_collapse_multiline_route_calls(scanned.text),
                    line_count=scanned.line_count,
                ),
            )
        )
    return reads


def _route_index_from_scan(scan: RepoScan) -> dict:
    """Build a route-index batch from the verify scan.

    Sydes' own `build_route_index` restricts itself to route-candidate
    directories, which hides root-level entrypoints such as `app.js` where the
    router mount prefix usually lives. The same per-file extractor is reused
    here over every scanned source file so mount composition sees the whole
    picture, instead of adding a second route indexer.
    """
    files: list[dict] = []
    for scanned in scan.files:
        if not scanned.is_app_source or scanned.extension not in SUPPORTED_EXTS:
            continue
        entry = _extract_index_for_file(
            scanned.path,
            _collapse_multiline_route_calls(scanned.text),
            FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
        )
        # Mount composition only follows `default` exports, so `module.exports =
        # router` (the CommonJS Express idiom) would otherwise leave every mount
        # prefix unresolved.
        for export in list(entry.get("exports") or []):
            if export.get("kind") == "commonjs":
                entry["exports"].append({"kind": "default", "symbol": export["symbol"]})
        # Likewise, mount resolution reads `imports`, which only models ESM
        # `import ... from`; `require()` is the other half of the same idiom.
        known_locals = {item.get("local") for item in entry.get("imports") or []}
        for match in _REQUIRE_IMPORT_RE.finditer(scanned.text):
            local = match.group("local")
            if local in known_locals:
                continue
            entry.setdefault("imports", []).append(
                {"local": local, "imported": local, "source": match.group("source"), "kind": "commonjs"}
            )
        files.append(entry)
    files.sort(key=lambda item: item["path"])
    return {"version": "v1", "repos": [{"repo": scan.repo, "root": str(scan.root), "files": files}]}


def _resolve_handler_symbol(index: SymbolIndex, file_path: str, handler_hint: str | None) -> str | None:
    """Resolve a route's handler hint to an indexed symbol id."""
    if not handler_hint:
        return None
    hint = handler_hint.strip()
    if not hint or hint in {"anonymous", "inline"}:
        return None
    last = hint.rsplit(".", 1)[-1]

    in_file = [item for item in index.symbols_in_file(file_path) if item.name == last or item.qualified_name == hint]
    if in_file:
        return in_file[0].id

    imported_files = {
        item.resolved_file
        for item in index.imports_by_file.get(file_path, [])
        if item.resolved_file
    }
    candidates = [
        item
        for item in index.symbols.values()
        if item.name == last and item.kind != "class"
    ]
    imported = [item for item in candidates if item.file in imported_files]
    if len(imported) == 1:
        return imported[0].id
    if len(candidates) == 1:
        return candidates[0].id
    receiver = hint.rsplit(".", 1)[0] if "." in hint else None
    if receiver:
        by_class = [
            item
            for item in candidates
            if item.class_name and item.class_name.lower() == receiver.lower()
        ]
        if len(by_class) == 1:
            return by_class[0].id
    return None


def _drop_unmounted_duplicates(routes: list[RouteBinding]) -> list[RouteBinding]:
    """Drop the un-prefixed copy of a route that mount composition already resolved.

    The same declaration is seen twice: once raw (`/login`) and once composed
    with its mount prefix (`/v1/login`). Only the composed one is a real route.
    """
    kept: list[RouteBinding] = []
    for binding in routes:
        path = binding.endpoint.path or ""
        file_path = binding.endpoint.file or ""
        method = (binding.endpoint.method or "").upper()
        superseded = any(
            other is not binding
            and (other.endpoint.file or "") == file_path
            and (other.endpoint.method or "").upper() == method
            and (other.endpoint.path or "") != path
            and (other.endpoint.path or "").endswith(path)
            and len(other.endpoint.path or "") > len(path)
            for other in routes
        )
        if not superseded:
            kept.append(binding)
    return kept


def _declaration_line(lines: list[str], route_path: str | None, handler: str | None = None) -> int | None:
    """Locate the source line a route is declared on.

    A mount-composed path (`/goodreads/books`) never appears verbatim in the
    source, so fall back to the handler name and then the leaf path segment.
    """
    if not lines:
        return None

    needles: list[str] = []
    if route_path:
        needles.append(route_path.split("{")[0].rstrip("/") or "/")
        leaf = "/" + route_path.rstrip("/").rsplit("/", 1)[-1]
        if leaf not in needles:
            needles.append(leaf)

    for needle in needles[:1]:
        for line_no, line in enumerate(lines, start=1):
            if needle in line and ("'" in line or '"' in line or "`" in line):
                return line_no

    if handler:
        name = handler.rsplit(".", 1)[-1]
        for line_no, line in enumerate(lines, start=1):
            if f"{name}(" in line:
                return line_no

    for needle in needles[1:]:
        for line_no, line in enumerate(lines, start=1):
            if needle in line and ("'" in line or '"' in line or "`" in line):
                return line_no
    return None


def _python_decorator_routes(index: SymbolIndex) -> list[tuple[Symbol, str, str]]:
    """Read routes straight off Python decorators, keeping the exact handler symbol."""
    found: list[tuple[Symbol, str, str]] = []
    for symbol in index.symbols.values():
        for decorator in symbol.decorators:
            match = _ROUTE_DECORATOR.search("@" + decorator if not decorator.startswith("@") else decorator)
            if match is None:
                continue
            path_match = re.search(r"['\"](?P<path>/[^'\"]*)['\"]", decorator)
            if path_match is None:
                continue
            method = match.group("method").upper()
            if method == "ROUTE":
                methods_match = re.search(r"methods\s*=\s*\[([^\]]*)\]", decorator)
                method = "GET"
                if methods_match:
                    first = re.search(r"['\"](\w+)['\"]", methods_match.group(1))
                    if first:
                        method = first.group(1).upper()
            found.append((symbol, method, path_match.group("path")))
    return found


def build_system_surface(
    *,
    repo: RepoRef,
    scan: RepoScan,
    index: SymbolIndex,
    events: list[EventSignal],
) -> SystemSurface:
    """Build the deterministic route/event surface for one repository."""
    surface = SystemSurface(repo=repo.name, root=scan.root, events=events)
    reads = _candidate_reads(scan)

    endpoints: list[EndpointCandidate] = []
    # Mount-composed routes come first: they carry the full path prefix and pick
    # the trailing handler argument rather than the leading middleware.
    try:
        facts = build_route_graph_facts_from_route_index_batch(_route_index_from_scan(scan))
        composed = facts.get("_repo_endpoint_candidates", {}).get(repo.name, [])
        endpoints.extend(item for item in composed if isinstance(item, EndpointCandidate))
        summary = facts.get("repos", [{}])[0].get("summary", {})
        surface.notes.append(
            "route_graph="
            + ",".join(f"{key}={value}" for key, value in sorted(summary.items()))
        )
    except Exception as exc:  # noqa: BLE001 - mount composition is best-effort
        surface.notes.append(f"route_graph_composition_failed={exc}")

    try:
        deterministic, frameworks = extract_deterministic_routes(reads)
        endpoints.extend(deterministic)
        if frameworks:
            surface.notes.append(f"route_frameworks={','.join(sorted(frameworks))}")
    except Exception as exc:  # noqa: BLE001 - route extraction must not abort analysis
        surface.notes.append(f"deterministic_route_extraction_failed={exc}")

    file_lines = {item.path: item.text.splitlines() for item in scan.files}

    seen: set[tuple[str, str, str]] = set()
    for endpoint in endpoints:
        key = (
            (endpoint.method or "").upper(),
            endpoint.path or "",
            endpoint.file or "",
        )
        if key in seen:
            continue
        seen.add(key)
        handler_id = _resolve_handler_symbol(index, endpoint.file or "", endpoint.handler)
        surface.routes.append(
            RouteBinding(
                endpoint=endpoint,
                handler_symbol_id=handler_id,
                declaration_line=_declaration_line(
                    file_lines.get(endpoint.file or "", []), endpoint.path, endpoint.handler
                ),
                evidence=[
                    EvidenceRef(
                        file=endpoint.file or "",
                        symbol=endpoint.handler,
                        label="route_declaration",
                        snippet=(endpoint.evidence[0].snippet if endpoint.evidence else None),
                    )
                ],
            )
        )

    # Python decorators bind a route to an exact symbol; prefer that binding.
    bound_by_symbol = {b.handler_symbol_id for b in surface.routes if b.handler_symbol_id}
    for symbol, method, path in _python_decorator_routes(index):
        if symbol.id in bound_by_symbol:
            continue
        key = (method, path, symbol.file)
        if key in seen:
            for binding in surface.routes:
                if (
                    (binding.endpoint.method or "").upper() == method
                    and binding.endpoint.path == path
                    and binding.endpoint.file == symbol.file
                    and binding.handler_symbol_id is None
                ):
                    binding.handler_symbol_id = symbol.id
            continue
        seen.add(key)
        surface.routes.append(
            RouteBinding(
                endpoint=EndpointCandidate(
                    method=method,
                    path=path,
                    handler=symbol.display_name,
                    file=symbol.file,
                    repo=repo.name,
                    confidence=1.0,
                    status="deterministic_decorator",
                ),
                handler_symbol_id=symbol.id,
                declaration_line=symbol.start_line,
                evidence=[
                    EvidenceRef(
                        file=symbol.file,
                        symbol=symbol.display_name,
                        label="route_decorator",
                        snippet="; ".join(symbol.decorators)[:220],
                    )
                ],
            )
        )

    surface.routes = _drop_unmounted_duplicates(surface.routes)
    surface.notes.append(f"routes_discovered={len(surface.routes)}")
    surface.notes.append(
        f"routes_bound_to_handler={sum(1 for item in surface.routes if item.handler_symbol_id)}"
    )
    return surface


_DTO_SUFFIXES = ("response", "request", "dto", "payload", "result", "schema", "view")


def classify_symbol_role(symbol: Symbol) -> str:
    """Classify an indexed symbol into a system-layer node kind."""
    owner = (symbol.class_name or symbol.name).lower()
    if owner.endswith(_DTO_SUFFIXES):
        # A `ServerResponse` under `models/` is a data shape, not a data store.
        return NODE_FUNCTION
    haystack = f"{symbol.file.lower()} {(symbol.class_name or '').lower()} {symbol.name.lower()}"
    if any(token in haystack for token in ("controller", "handler", "resolver", "view")):
        return NODE_HANDLER
    if any(token in haystack for token in ("repository", "repositories", "/dao", "dao.", "model", "entity", "/db/", "queries")):
        return NODE_REPOSITORY
    if any(token in haystack for token in ("client", "gateway", "adapter", "provider", "sdk")):
        return NODE_CLIENT
    if any(token in haystack for token in ("service", "usecase", "use_case", "domain", "manager")):
        return NODE_SERVICE
    return NODE_FUNCTION


def _node_for_symbol(symbol: Symbol, *, changed: bool) -> FlowNode:
    """Build a flow node for an indexed symbol."""
    return FlowNode(
        id=symbol.id,
        kind=classify_symbol_role(symbol),
        name=symbol.display_name,
        repo=symbol.repo,
        file=symbol.file,
        symbol=symbol.display_name,
        changed=changed,
        metadata={"language": symbol.language, "lines": f"{symbol.start_line}-{symbol.end_line}"},
    )


def _upstream_paths(
    index: SymbolIndex,
    start_id: str,
    route_handler_ids: set[str],
    consumer_symbol_ids: set[str],
) -> list[list[str]]:
    """Breadth-first search from a changed symbol up to entrypoint symbols."""
    targets = route_handler_ids | consumer_symbol_ids
    if start_id in targets:
        return [[start_id]]

    paths: list[list[str]] = []
    visited: set[str] = {start_id}
    frontier: list[list[str]] = [[start_id]]

    for _ in range(MAX_UPSTREAM_DEPTH):
        next_frontier: list[list[str]] = []
        for path in frontier:
            for edge in index.callers_of.get(path[-1], []):
                caller = edge.caller_id
                if caller in path:
                    continue
                extended = [*path, caller]
                if caller in targets:
                    paths.append(extended)
                    if len(paths) >= MAX_ROUTES_PER_SYMBOL:
                        return paths
                    continue
                if caller in visited:
                    continue
                visited.add(caller)
                next_frontier.append(extended)
        if not next_frontier:
            break
        frontier = next_frontier
    return paths


_URI_CALL = re.compile(r"\.\s*(?:uri|url|path)\s*\(\s*[`'\"](?P<url>[^`'\"]+)")


def _http_target_snippet(scan_text: list[str], line_no: int, end: int) -> str:
    """Widen an HTTP call site to the line that actually carries the URL.

    Fluent clients put the verb and the path on separate lines
    (`client.get()` then `.uri("/db/books")`), and the path is the part that
    identifies the downstream service.
    """
    stripped = scan_text[line_no - 1].strip()
    if _URL_LITERAL.search(stripped):
        return stripped[:220]
    for offset in range(line_no, min(line_no + 4, min(end, len(scan_text)))):
        candidate = scan_text[offset].strip()
        match = _URI_CALL.search(candidate) or _URL_LITERAL.search(candidate)
        if match:
            return f"{stripped} {candidate}"[:220]
    return stripped[:220]


def _line_signals(scan_text: list[str], start: int, end: int) -> list[tuple[str, int, str]]:
    """Detect database/http/cache side-effect lines inside a symbol body."""
    signals: list[tuple[str, int, str]] = []
    for line_no in range(start, min(end, len(scan_text)) + 1):
        line = scan_text[line_no - 1]
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("#"):
            continue
        for pattern, _label in _DB_PATTERNS:
            if pattern.search(line):
                signals.append(("database", line_no, stripped[:220]))
                break
        else:
            if any(pattern.search(line) for pattern in _HTTP_PATTERNS):
                signals.append(("http_client", line_no, _http_target_snippet(scan_text, line_no, end)))
            elif _CACHE_PATTERN.search(line):
                signals.append(("cache", line_no, stripped[:220]))
    return signals


def _downstream_symbols(index: SymbolIndex, start_id: str) -> list[tuple[str, str, object]]:
    """Collect symbols reachable downstream from a changed symbol, with edges."""
    results: list[tuple[str, str, object]] = []
    visited = {start_id}
    frontier = [start_id]
    for _ in range(MAX_DOWNSTREAM_DEPTH):
        next_frontier: list[str] = []
        for current in frontier:
            for edge in index.callees_of.get(current, []):
                if edge.callee_id in visited:
                    continue
                visited.add(edge.callee_id)
                results.append((current, edge.callee_id, edge))
                next_frontier.append(edge.callee_id)
        if not next_frontier:
            break
        frontier = next_frontier
    return results


def _events_in_span(events: list[EventSignal], file_path: str, start: int, end: int) -> list[EventSignal]:
    """Return event signals located inside a symbol body."""
    return [item for item in events if item.file == file_path and start <= item.line <= end]


_ANONYMOUS_HTTP_NODE = "http:outbound HTTP call"


def _prune_anonymous_http(flow: AffectedFlow) -> None:
    """Drop the unnamed HTTP node when the same flow already named its target.

    A fluent client chain hits the HTTP pattern on several lines; only the line
    carrying the path identifies the callee, and the rest are the same call.
    """
    named = any(
        node.kind == NODE_CLIENT and node.id != _ANONYMOUS_HTTP_NODE for node in flow.nodes
    )
    if not named:
        return
    flow.nodes = [node for node in flow.nodes if node.id != _ANONYMOUS_HTTP_NODE]
    flow.edges = [
        edge
        for edge in flow.edges
        if _ANONYMOUS_HTTP_NODE not in (edge.source, edge.target)
    ]


def _merge_flows_by_entry(flows: list[AffectedFlow]) -> list[AffectedFlow]:
    """Merge flows sharing an entrypoint so each entry is reported once."""
    merged: dict[tuple[str, str], AffectedFlow] = {}
    for flow in flows:
        key = (flow.entry_kind, flow.entry_label)
        current = merged.get(key)
        if current is None:
            merged[key] = flow
            continue
        known_nodes = {node.id for node in current.nodes}
        for node in flow.nodes:
            if node.id in known_nodes:
                if node.changed:
                    for existing in current.nodes:
                        if existing.id == node.id:
                            existing.changed = True
                continue
            current.nodes.append(node)
            known_nodes.add(node.id)
        known_edges = {(edge.source, edge.target, edge.kind) for edge in current.edges}
        for edge in flow.edges:
            key_edge = (edge.source, edge.target, edge.kind)
            if key_edge in known_edges:
                continue
            current.edges.append(edge)
            known_edges.add(key_edge)
        for node_id in flow.changed_node_ids:
            if node_id not in current.changed_node_ids:
                current.changed_node_ids.append(node_id)
        for note in flow.notes:
            if note not in current.notes:
                current.notes.append(note)
    return list(merged.values())


class FlowBuilder:
    """Builds affected system flows from changed symbols and the system surface."""

    def __init__(
        self,
        *,
        index: SymbolIndex,
        surface: SystemSurface,
        scan: RepoScan,
        events: list[EventSignal],
    ) -> None:
        self.index = index
        self.surface = surface
        self.scan = scan
        self.events = events
        self._file_lines = {item.path: item.text.splitlines() for item in scan.files}
        self._routes_by_handler = surface.routes_by_handler()
        self._consumers = [item for item in events if item.action == CONSUME]
        self._producers = [item for item in events if item.action == PUBLISH]
        self._consumer_symbol_ids = self._map_consumer_symbols()
        self._changed_symbol_ids: set[str] = set()

    def _consumer_symbol(self, signal: EventSignal) -> Symbol | None:
        """Resolve the symbol that actually handles a consumed event.

        A subscription usually names its handler as the trailing argument
        (`bus.subscribe("refund.created", handle_refund_created)`); that handler
        is the meaningful consumer, not the registration function around it.
        """
        arguments = _CONSUMER_HANDLER_ARG.search(signal.snippet)
        if arguments:
            name = arguments.group("handler")
            in_file = [
                item for item in self.index.symbols_in_file(signal.file) if item.name == name
            ]
            if in_file:
                return in_file[0]
            candidates = [
                item for item in self.index.symbols.values() if item.name == name and item.kind != "class"
            ]
            if len(candidates) == 1:
                return candidates[0]
        return self.index.symbol_at(signal.file, signal.line)

    def _map_consumer_symbols(self) -> dict[str, EventSignal]:
        """Map indexed symbols that handle a consumed event."""
        mapping: dict[str, EventSignal] = {}
        for signal in self._consumers:
            symbol = self._consumer_symbol(signal)
            if symbol is not None:
                mapping[symbol.id] = signal
        return mapping

    def build(
        self,
        changed_symbol_ids: list[str],
        changed_files: set[str],
        changed_hunks: dict[str, list[tuple[int, int]]] | None = None,
    ) -> list[AffectedFlow]:
        """Build one flow per (entrypoint, changed symbol) pair, bounded."""
        flows: list[AffectedFlow] = []
        route_handler_ids = set(self._routes_by_handler)
        emitted: set[str] = set()
        self._changed_symbol_ids = set(changed_symbol_ids)

        for symbol_id in changed_symbol_ids:
            symbol = self.index.symbols.get(symbol_id)
            if symbol is None:
                continue
            paths = _upstream_paths(
                self.index, symbol_id, route_handler_ids, set(self._consumer_symbol_ids)
            )
            if paths:
                for path in paths:
                    for flow in self._flows_for_path(path, symbol_id, changed_files):
                        if flow.id in emitted:
                            continue
                        emitted.add(flow.id)
                        flows.append(flow)
                        if len(flows) >= MAX_FLOWS:
                            return flows
                continue

            standalone = self._standalone_flow(symbol, changed_files)
            if standalone is not None and standalone.id not in emitted:
                emitted.add(standalone.id)
                flows.append(standalone)
                if len(flows) >= MAX_FLOWS:
                    return flows

        covered_routes = {flow.entry_label for flow in flows if flow.entry_kind == NODE_ROUTE}
        flows.extend(
            self._route_declaration_flows(changed_hunks or {}, changed_files, emitted, covered_routes)
        )
        flows.extend(self._unindexed_region_flows(changed_hunks or {}, emitted))
        merged = _merge_flows_by_entry(flows)[:MAX_FLOWS]
        for flow in merged:
            _prune_anonymous_http(flow)
        return merged

    def _unindexed_region_flows(
        self,
        changed_hunks: dict[str, list[tuple[int, int]]],
        emitted: set[str],
    ) -> list[AffectedFlow]:
        """Attribute changes in languages the symbol index does not parse.

        Java, Go, Ruby and friends have route extraction but no symbol index, so
        a diff there yields no changed symbols. Fall back to the enclosing route
        declaration and read side effects straight out of the changed region,
        rather than reporting nothing at all.
        """
        flows: list[AffectedFlow] = []
        routes_by_file: dict[str, list[RouteBinding]] = {}
        for binding in self.surface.routes:
            file_path = binding.endpoint.file or ""
            if file_path in changed_hunks and binding.declaration_line:
                routes_by_file.setdefault(file_path, []).append(binding)

        for file_path, bindings in routes_by_file.items():
            if self.index.symbols_in_file(file_path):
                continue
            lines = self._file_lines.get(file_path)
            if lines is None:
                continue
            ordered = sorted(bindings, key=lambda item: item.declaration_line or 0)
            boundaries = [item.declaration_line or 0 for item in ordered]

            for hunk_start, hunk_end in changed_hunks.get(file_path, []):
                position = None
                for offset, line in enumerate(boundaries):
                    if line <= hunk_end:
                        position = offset
                if position is None:
                    continue
                binding = ordered[position]
                region_start = boundaries[position]
                region_end = (
                    boundaries[position + 1] - 1 if position + 1 < len(boundaries) else len(lines)
                )
                if hunk_start > region_end:
                    continue
                flow_id = f"flow:region:{binding.label.replace(' ', ':')}"
                if flow_id in emitted:
                    continue
                emitted.add(flow_id)
                flows.append(
                    self._region_flow(flow_id, binding, file_path, lines, region_start, region_end)
                )
        return flows

    def _region_flow(
        self,
        flow_id: str,
        binding: RouteBinding,
        file_path: str,
        lines: list[str],
        region_start: int,
        region_end: int,
    ) -> AffectedFlow:
        """Build a route flow from a changed source region without symbol data."""
        route_node = FlowNode(
            id=f"route:{binding.label}",
            kind=NODE_ROUTE,
            name=binding.label,
            repo=self.surface.repo,
            file=file_path,
            method=(binding.endpoint.method or "ANY").upper(),
            path=binding.endpoint.path,
            changed=True,
        )
        nodes = [route_node]
        edges: list[FlowEdge] = []
        known = {route_node.id}

        for kind, line_no, snippet in _line_signals(lines, region_start, region_end):
            if kind == "database":
                node_id, node_kind, name = f"db:{self.surface.repo}", NODE_DATABASE, "database"
            elif kind == "cache":
                node_id, node_kind, name = f"cache:{self.surface.repo}", NODE_EXTERNAL, "cache (redis)"
            else:
                url_match = _URL_LITERAL.search(snippet)
                target = url_match.group("url") if url_match else "outbound HTTP call"
                node_id, node_kind, name = f"http:{target}", NODE_CLIENT, f"HTTP {target}"
            if node_id not in known:
                nodes.append(
                    FlowNode(id=node_id, kind=node_kind, name=name, repo=self.surface.repo, file=file_path)
                )
                known.add(node_id)
            edges.append(
                FlowEdge(
                    source=route_node.id,
                    target=node_id,
                    kind="queries" if kind == "database" else "http_call" if kind == "http_client" else "reads_writes",
                    reason=f"side-effect statement at {file_path}:{line_no}",
                    evidence=[EvidenceRef(file=file_path, label=kind, snippet=snippet)],
                )
            )

        for signal in _events_in_span(self.events, file_path, region_start, region_end):
            if signal.action != PUBLISH:
                continue
            node_id = f"event:{signal.label}"
            if node_id not in known:
                nodes.append(
                    FlowNode(
                        id=node_id,
                        kind=NODE_EVENT,
                        name=signal.label,
                        repo=self.surface.repo,
                        file=signal.file,
                        metadata={"technology": signal.technology},
                    )
                )
                known.add(node_id)
            edges.append(
                FlowEdge(
                    source=route_node.id,
                    target=node_id,
                    kind="publishes",
                    reason=f"publish site at {signal.file}:{signal.line}",
                    evidence=[EvidenceRef(file=signal.file, label="event_publish", snippet=signal.snippet)],
                )
            )
            self._attach_consumers(signal, node_id, nodes, edges, known)

        return AffectedFlow(
            id=flow_id,
            entry_kind=NODE_ROUTE,
            entry_label=binding.label,
            repo=self.surface.repo,
            nodes=nodes,
            edges=edges,
            changed_node_ids=[route_node.id],
            reason=(
                "changed lines fall inside this route's handler region "
                "(no symbol index for this language)"
            ),
            notes=["symbol_index_unavailable_for_language"],
        )

    def _flows_for_path(
        self, path: list[str], changed_symbol_id: str, changed_files: set[str]
    ) -> list[AffectedFlow]:
        """Build flows for one upstream path, one per bound route (or consumer)."""
        entry_symbol_id = path[-1]
        ordered = list(reversed(path))
        bindings = self._routes_by_handler.get(entry_symbol_id, [])
        consumer_signal = self._consumer_symbol_ids.get(entry_symbol_id)

        flows: list[AffectedFlow] = []
        if bindings:
            for binding in bindings:
                flows.append(
                    self._assemble_flow(
                        flow_id=f"flow:{binding.label.replace(' ', ':')}:{changed_symbol_id}",
                        entry_kind=NODE_ROUTE,
                        entry_label=binding.label,
                        entry_node=FlowNode(
                            id=f"route:{binding.label}",
                            kind=NODE_ROUTE,
                            name=binding.label,
                            repo=self.surface.repo,
                            file=binding.endpoint.file,
                            method=(binding.endpoint.method or "ANY").upper(),
                            path=binding.endpoint.path,
                            changed=(binding.endpoint.file or "") in changed_files,
                        ),
                        entry_evidence=binding.evidence,
                        chain=ordered,
                        changed_symbol_id=changed_symbol_id,
                        changed_files=changed_files,
                        reason="route handler reaches the changed symbol through resolved calls",
                    )
                )
        elif consumer_signal is not None:
            label = f"event {consumer_signal.label}"
            flows.append(
                self._assemble_flow(
                    flow_id=f"flow:consumer:{consumer_signal.label}:{changed_symbol_id}",
                    entry_kind=NODE_CONSUMER,
                    entry_label=label,
                    entry_node=FlowNode(
                        id=f"consumer:{consumer_signal.label}:{consumer_signal.file}",
                        kind=NODE_CONSUMER,
                        name=label,
                        repo=self.surface.repo,
                        file=consumer_signal.file,
                        changed=consumer_signal.file in changed_files,
                        metadata={"technology": consumer_signal.technology},
                    ),
                    entry_evidence=[
                        EvidenceRef(
                            file=consumer_signal.file,
                            label="event_consumer",
                            snippet=consumer_signal.snippet,
                        )
                    ],
                    chain=ordered,
                    changed_symbol_id=changed_symbol_id,
                    changed_files=changed_files,
                    reason="event consumer reaches the changed symbol through resolved calls",
                )
            )
        return flows

    def _standalone_flow(self, symbol: Symbol, changed_files: set[str]) -> AffectedFlow | None:
        """Build a flow anchored on the changed symbol when no entrypoint reaches it."""
        nodes = [_node_for_symbol(symbol, changed=True)]
        edges: list[FlowEdge] = []
        self._attach_downstream(symbol.id, nodes, edges, changed_files)
        if len(nodes) <= 1:
            return None
        return AffectedFlow(
            id=f"flow:symbol:{symbol.id}",
            entry_kind=NODE_FUNCTION,
            entry_label=symbol.display_name,
            repo=symbol.repo,
            nodes=nodes,
            edges=edges,
            changed_node_ids=[symbol.id],
            reason="no route or consumer resolves to this symbol; showing downstream reach only",
            notes=["entrypoint_unresolved"],
        )

    def _route_declaration_flows(
        self,
        changed_hunks: dict[str, list[tuple[int, int]]],
        changed_files: set[str],
        emitted: set[str],
        covered_routes: set[str],
    ) -> list[AffectedFlow]:
        """Add flows for routes whose *declaration line* was itself edited.

        Requiring hunk overlap keeps unrelated routes that merely share a file
        with the change out of the result.
        """
        flows: list[AffectedFlow] = []
        for binding in self.surface.routes:
            file_path = binding.endpoint.file or ""
            if file_path not in changed_files:
                continue
            if binding.label in covered_routes:
                continue
            line = binding.declaration_line
            hunks = changed_hunks.get(file_path, [])
            if line is None or not any(start <= line <= end for start, end in hunks):
                continue
            flow_id = f"flow:route-decl:{binding.label.replace(' ', ':')}"
            if flow_id in emitted:
                continue
            handler = (
                self.index.symbols.get(binding.handler_symbol_id)
                if binding.handler_symbol_id
                else None
            )
            nodes = [
                FlowNode(
                    id=f"route:{binding.label}",
                    kind=NODE_ROUTE,
                    name=binding.label,
                    repo=self.surface.repo,
                    file=file_path,
                    method=(binding.endpoint.method or "ANY").upper(),
                    path=binding.endpoint.path,
                    changed=True,
                )
            ]
            edges: list[FlowEdge] = []
            if handler is not None:
                nodes.append(_node_for_symbol(handler, changed=handler.id in self._changed_symbol_ids))
                edges.append(
                    FlowEdge(
                        source=nodes[0].id,
                        target=handler.id,
                        kind="routes_to",
                        reason="route declaration binds this handler",
                        evidence=binding.evidence,
                    )
                )
                self._attach_downstream(handler.id, nodes, edges, changed_files)
            emitted.add(flow_id)
            flows.append(
                AffectedFlow(
                    id=flow_id,
                    entry_kind=NODE_ROUTE,
                    entry_label=binding.label,
                    repo=self.surface.repo,
                    nodes=nodes,
                    edges=edges,
                    changed_node_ids=[nodes[0].id],
                    reason="route declaration file was modified in this change",
                )
            )
            if len(flows) >= MAX_FLOWS:
                break
        return flows

    def _assemble_flow(
        self,
        *,
        flow_id: str,
        entry_kind: str,
        entry_label: str,
        entry_node: FlowNode,
        entry_evidence: list[EvidenceRef],
        chain: list[str],
        changed_symbol_id: str,
        changed_files: set[str],
        reason: str,
    ) -> AffectedFlow:
        """Assemble nodes/edges for entry -> chain -> downstream side effects."""
        nodes: list[FlowNode] = [entry_node]
        edges: list[FlowEdge] = []
        previous_id = entry_node.id

        for position, symbol_id in enumerate(chain):
            symbol = self.index.symbols.get(symbol_id)
            if symbol is None:
                continue
            node = _node_for_symbol(symbol, changed=symbol_id == changed_symbol_id)
            if position == 0 and entry_kind in {NODE_ROUTE, NODE_CONSUMER}:
                node.kind = NODE_HANDLER if node.kind == NODE_FUNCTION else node.kind
            nodes.append(node)
            edge_evidence = entry_evidence if position == 0 else []
            edge_reason = "route declaration binds this handler" if position == 0 else None
            if position > 0:
                call_edge = next(
                    (
                        item
                        for item in self.index.callees_of.get(chain[position - 1], [])
                        if item.callee_id == symbol_id
                    ),
                    None,
                )
                if call_edge is not None:
                    edge_evidence = [
                        EvidenceRef(
                            file=call_edge.file,
                            symbol=call_edge.call_text,
                            label=f"call_site ({call_edge.resolution})",
                            snippet=call_edge.snippet,
                        )
                    ]
                    edge_reason = f"resolved call `{call_edge.call_text}` at {call_edge.file}:{call_edge.line}"
            edges.append(
                FlowEdge(
                    source=previous_id,
                    target=node.id,
                    kind="routes_to" if position == 0 else "calls",
                    reason=edge_reason,
                    evidence=edge_evidence,
                )
            )
            previous_id = node.id

        self._attach_downstream(changed_symbol_id, nodes, edges, changed_files)

        return AffectedFlow(
            id=flow_id,
            entry_kind=entry_kind,
            entry_label=entry_label,
            repo=self.surface.repo,
            nodes=nodes,
            edges=edges,
            changed_node_ids=[changed_symbol_id],
            reason=reason,
        )

    def _attach_downstream(
        self,
        origin_symbol_id: str,
        nodes: list[FlowNode],
        edges: list[FlowEdge],
        changed_files: set[str],
    ) -> None:
        """Attach downstream calls, side effects, and events reachable from a symbol."""
        known = {node.id for node in nodes}

        for source_id, callee_id, edge in _downstream_symbols(self.index, origin_symbol_id):
            callee = self.index.symbols.get(callee_id)
            if callee is None or callee_id in known:
                continue
            node = _node_for_symbol(callee, changed=callee.id in self._changed_symbol_ids)
            nodes.append(node)
            known.add(callee_id)
            edges.append(
                FlowEdge(
                    source=source_id,
                    target=callee_id,
                    kind="calls",
                    reason=f"resolved call `{edge.call_text}` at {edge.file}:{edge.line}",
                    evidence=[
                        EvidenceRef(
                            file=edge.file,
                            symbol=edge.call_text,
                            label=f"call_site ({edge.resolution})",
                            snippet=edge.snippet,
                        )
                    ],
                )
            )

        for symbol_id in list(known):
            symbol = self.index.symbols.get(symbol_id)
            if symbol is None:
                continue
            self._attach_side_effects(symbol, nodes, edges, known)

    def _attach_side_effects(
        self,
        symbol: Symbol,
        nodes: list[FlowNode],
        edges: list[FlowEdge],
        known: set[str],
    ) -> None:
        """Attach database/http/cache/event nodes found inside a symbol body."""
        lines = self._file_lines.get(symbol.file)
        if lines is None:
            return

        for kind, line_no, snippet in _line_signals(lines, symbol.start_line, symbol.end_line):
            if kind == "database":
                node_id = f"db:{symbol.repo}"
                node_kind, name = NODE_DATABASE, "database"
            elif kind == "cache":
                node_id = f"cache:{symbol.repo}"
                node_kind, name = NODE_EXTERNAL, "cache (redis)"
            else:
                url_match = _URL_LITERAL.search(snippet)
                target = url_match.group("url") if url_match else "outbound HTTP call"
                node_id = f"http:{target}"
                node_kind, name = NODE_CLIENT, f"HTTP {target}"

            if node_id not in known:
                nodes.append(
                    FlowNode(
                        id=node_id,
                        kind=node_kind,
                        name=name,
                        repo=symbol.repo,
                        file=symbol.file,
                        metadata={"detected_as": kind},
                    )
                )
                known.add(node_id)
            edge_key = (symbol.id, node_id)
            if any((e.source, e.target) == edge_key for e in edges):
                continue
            edges.append(
                FlowEdge(
                    source=symbol.id,
                    target=node_id,
                    kind="queries" if kind == "database" else "http_call" if kind == "http_client" else "reads_writes",
                    reason=f"side-effect statement at {symbol.file}:{line_no}",
                    evidence=[
                        EvidenceRef(file=symbol.file, symbol=symbol.display_name, label=kind, snippet=snippet)
                    ],
                )
            )

        for signal in _events_in_span(self.events, symbol.file, symbol.start_line, symbol.end_line):
            if signal.action != PUBLISH:
                continue
            node_id = f"event:{signal.label}"
            if node_id not in known:
                nodes.append(
                    FlowNode(
                        id=node_id,
                        kind=NODE_EVENT,
                        name=signal.label,
                        repo=symbol.repo,
                        file=signal.file,
                        metadata={"technology": signal.technology},
                    )
                )
                known.add(node_id)
            edges.append(
                FlowEdge(
                    source=symbol.id,
                    target=node_id,
                    kind="publishes",
                    reason=f"publish site at {signal.file}:{signal.line}",
                    evidence=[
                        EvidenceRef(file=signal.file, symbol=symbol.display_name, label="event_publish", snippet=signal.snippet)
                    ],
                )
            )
            self._attach_consumers(signal, node_id, nodes, edges, known)

    def _attach_consumers(
        self,
        producer: EventSignal,
        event_node_id: str,
        nodes: list[FlowNode],
        edges: list[FlowEdge],
        known: set[str],
    ) -> None:
        """Attach consumers subscribed to the same named topic."""
        if producer.topic is None:
            return
        for consumer in self._consumers:
            if consumer.topic != producer.topic:
                continue
            symbol = self._consumer_symbol(consumer)
            node_id = symbol.id if symbol is not None else f"consumer:{consumer.file}:{consumer.line}"
            name = symbol.display_name if symbol is not None else Path(consumer.file).stem
            if node_id not in known:
                nodes.append(
                    FlowNode(
                        id=node_id,
                        kind=NODE_CONSUMER,
                        name=name,
                        repo=consumer.repo,
                        file=consumer.file,
                        symbol=name,
                        metadata={"technology": consumer.technology, "topic": consumer.topic},
                    )
                )
                known.add(node_id)
            edges.append(
                FlowEdge(
                    source=event_node_id,
                    target=node_id,
                    kind="consumes",
                    reason=f"subscription to `{consumer.topic}` at {consumer.file}:{consumer.line}",
                    evidence=[
                        EvidenceRef(file=consumer.file, label="event_consumer", snippet=consumer.snippet)
                    ],
                )
            )
