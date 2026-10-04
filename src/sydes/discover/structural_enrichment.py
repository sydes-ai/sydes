"""Targeted structural enrichment after ordinary traversal stalls.

Track A first walks CBM's call graph. Where that walk stops at code a framework invokes --
a message handler nothing calls directly, a callback a framework registers, a route whose
path is assembled from several declarations -- this module asks CBM a few more structural
questions before anything escalates to runtime evidence or AI:

* message dispatch: a changed method left unresolved, whose class is decorated `@X(T)` with
  `T` a repository class. Who constructs or references `T` (CBM CALLS/USAGE) and what call
  hands it on (source); where the handler class is wired in (CBM imports + decorators).
* callback registration: a reached entrypoint that is only decorated metadata (no route),
  i.e. a framework callback. Where its class is referenced (CBM USAGE) and which call
  passes an instance of it to something else (source).
* route registration: for every route in an affected flow, the decorator and route metadata
  CBM recorded, the router it is declared on and the mount that prefixes it (Sydes' own
  route index, a source scan) -- flagging a path or prefix that is not a literal instead of
  trusting a composed route built without it.

Nothing here creates an edge. Each finding is a `FrameworkBoundaryCandidate`: the facts
found, each with its provenance, the candidate targets and what is still missing. A
candidate is `resolved` only when the facts compose deterministically (a literal route) or
an existing rule closed it (composed dispatch); otherwise it stays `unresolved`, which is
what selective runtime evidence is for.

Budget: decorators, symbol spans, imports and route declarations are facts Sydes already
fetched (no query). All CBM reference lookups are batched into one request per run
(`refs`, cached by the adapter); source reads are bounded to the spans involved.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where a fact came from. CBM facts are graph facts; anything read from source text says so.
PROVENANCE_CBM_SYMBOL = "cbm_symbol"
PROVENANCE_CBM_CALL = "cbm_call"
PROVENANCE_CBM_USAGE = "cbm_usage"
PROVENANCE_CBM_DECORATOR = "cbm_decorator"
PROVENANCE_CBM_ENTRYPOINT = "cbm_entrypoint"
PROVENANCE_CBM_REGISTRATION = "cbm_registration"
PROVENANCE_SOURCE_FALLBACK = "source_fallback"

CATEGORY_MESSAGE_DISPATCH = "message_dispatch"
CATEGORY_CALLBACK_REGISTRATION = "callback_registration"
CATEGORY_ROUTE_REGISTRATION = "route_registration"

STATUS_UNRESOLVED = "unresolved"
STATUS_RESOLVED = "resolved"

MAX_SEEDS = 60
MAX_SPAN_LINES = 400
_DECORATOR_WITH_TYPE = re.compile(r"@\s*([A-Za-z_][\w.]*)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*$")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|\.(spec|test)\.[jt]sx?$|Test\.java$|(^|/)test_[^/]*\.py$")
_STRING_ARG = re.compile(r"""^\s*(?:[rbuf]{0,2})(['"`])(?P<value>[^'"`]*)\1\s*$""")


@dataclass
class StructuralFact:
    provenance: str
    fact: str
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"provenance": self.provenance, "fact": self.fact, "file": self.file, "line": self.line}


@dataclass
class FrameworkBoundaryCandidate:
    """Explicit traversal stopped here, but structural metadata suggests the framework
    continues it. Not an edge: `targets` are candidates, `missing` says what would close it."""

    category: str
    source_symbol: str
    via: str
    targets: list[str]
    status: str = STATUS_UNRESOLVED
    message_type: str | None = None
    facts: list[StructuralFact] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    resolved_by: str | None = None
    #: CBM reference lookups this candidate drew on (the run's batched request), for budgeting
    queries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "status": self.status, "source_symbol": self.source_symbol,
            "via": self.via, "targets": list(self.targets), "message_type": self.message_type,
            "facts": [f.to_dict() for f in self.facts], "missing": list(self.missing),
            "resolved_by": self.resolved_by, "queries": self.queries,
        }


@dataclass
class EnrichmentInput:
    repo: str
    repo_root: Path
    #: changed symbols ordinary traversal left unresolved: {"name", "file", "qualified_name"}
    unresolved: list[dict[str, Any]]
    #: entrypoints the traversal reached: {"symbol", "file", "kind", "route_method"}
    reached_entrypoints: list[dict[str, Any]]
    #: route flows: {"method", "path", "handler", "handler_file"}
    route_flows: list[dict[str, Any]]
    #: CBM decorated symbols (classes, functions, methods), as Sydes already fetched them
    decorated: list[dict[str, Any]]
    symbol_index: dict[str, Any]
    route_index: dict[str, Any]
    #: existing composed dispatch records (result.composed_dispatch_edges)
    composed: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EnrichmentResult:
    candidates: list[FrameworkBoundaryCandidate] = field(default_factory=list)
    triggered: list[str] = field(default_factory=list)
    reference_targets: int = 0
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- indexes


class _Symbols:
    """The code-intelligence symbol index for one repository, looked up by name/file."""

    def __init__(self, symbol_index: dict[str, Any], repo: str) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        for payload in symbol_index.get("repos", []) or []:
            if payload.get("repo") == repo:
                for item in payload.get("files", []) or []:
                    self.files[str(item.get("path"))] = item

    def in_file(self, file: str) -> list[dict[str, Any]]:
        return list((self.files.get(file) or {}).get("symbols", []) or [])

    def find(self, name: str, file: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        out = []
        for path, item in self.files.items():
            if file is not None and path != file:
                continue
            for sym in item.get("symbols", []) or []:
                if sym.get("name") == name and (kind is None or sym.get("kind") == kind):
                    out.append({**sym, "file": path})
        return out

    def enclosing(self, file: str, line: int) -> dict[str, Any] | None:
        best = None
        for sym in self.in_file(file):
            start, end = sym.get("start_line") or 0, sym.get("end_line") or 0
            covers = sym.get("kind") in ("function", "class_method") and start <= line <= end
            if covers and (best is None or start >= (best.get("start_line") or 0)):
                best = sym
        return best

    def imports_of(self, file: str) -> list[dict[str, Any]]:
        return list((self.files.get(file) or {}).get("imports", []) or [])


def _display(sym: dict[str, Any]) -> str:
    parent = sym.get("parent")
    return f"{parent}.{sym.get('name')}" if parent else str(sym.get("name"))


def _decorators(decorated: list[dict[str, Any]], symbol: str, file: str) -> list[str]:
    for item in decorated:
        if item.get("symbol") == symbol and item.get("file") == file:
            text = str(item.get("decorators") or "")
            parts = [d.strip() for d in text.split("\n@") if d.strip()]
            return [d if d.startswith("@") else "@" + d for d in parts]
    return []


def _first_line(text: str, limit: int = 80) -> str:
    """The decorator text on one line (whitespace collapsed), cut at `limit`."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


class _Source:
    """Bounded source reads, cached per file; every fact built from one is `source_fallback`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache: dict[str, list[str]] = {}

    def lines(self, file: str) -> list[str]:
        if file not in self.cache:
            try:
                self.cache[file] = (self.root / file).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self.cache[file] = []
        return self.cache[file]

    def span(self, file: str, start: int | None, end: int | None) -> list[tuple[int, str]]:
        lines = self.lines(file)
        lo = max(1, int(start or 1))
        hi = min(len(lines), int(end or lo), lo + MAX_SPAN_LINES)
        return [(n, lines[n - 1]) for n in range(lo, hi + 1)]


# ----------------------------------------------------------------------------- detectors


def _call_passing(text_lines: Iterable[tuple[int, str]], names: set[str]) -> tuple[int, str, str, str] | None:
    """The first call `recv.member(..., name, ...)` passing one of `names` (or `new T(...)`
    when `names` holds `new T`): (line, receiver expression, member, the argument text)."""
    call = re.compile(r"([A-Za-z_$][\w$.]*)\.([A-Za-z_$][\w$]*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
    for number, text in text_lines:
        for match in call.finditer(text):
            args = match.group(3)
            for name in names:
                if re.search(rf"(^|[\s,(]){re.escape(name)}\b", args):
                    return number, match.group(1), match.group(2), args.strip()
    return None


def _declared_type(lines: list[str], field_name: str) -> str | None:
    """`field: T` / `private readonly field: T` (TS) or `T field;` (Java) in a class body."""
    for text in lines:
        ts = re.search(rf"\b{re.escape(field_name)}\s*:\s*([A-Z][\w]*)", text)
        if ts:
            return ts.group(1)
        java = re.search(rf"\b([A-Z][\w<>]*)\s+{re.escape(field_name)}\s*[;=,)]", text)
        if java:
            return java.group(1)
    return None


def _message_dispatch(ctx: EnrichmentInput, symbols: _Symbols) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    """(changed symbol, message type, handler class symbol) triples to enrich."""
    out = []
    for changed in ctx.unresolved:
        file, name = str(changed.get("file") or ""), str(changed.get("name") or "").rsplit(".", 1)[-1]
        methods = [s for s in symbols.find(name, file=file) if s.get("parent")]
        for method in methods:
            owner = str(method["parent"])
            for decorator in _decorators(ctx.decorated, owner, file):
                match = _DECORATOR_WITH_TYPE.match(decorator if decorator.startswith("@") else "@" + decorator)
                if match and symbols.find(match.group(2), kind="class"):
                    out.append((method, match.group(2), {"name": owner, "file": file,
                                                         "decorator": f"@{match.group(1)}({match.group(2)})"}))
    return out


_ROUTE_DECLARATION = re.compile(
    r"(?:@|\.)\s*(?:get|post|put|patch|delete|options|head|all|route|api_route|"
    r"requestmapping|getmapping|postmapping|putmapping|deletemapping|patchmapping)\b\s*(?:\(\s*([^,)]*))?",
    re.IGNORECASE,
)


def _route_argument(snippet: str) -> str | None:
    """The path argument of a route declaration as written: '' for none (`@Get()`), the
    literal with its quotes, or the expression (`routes.audio.speech`); None if unseen."""
    match = _ROUTE_DECLARATION.search(snippet)
    if match is None:
        return None
    argument = (match.group(1) or "").strip()
    return re.sub(r"^(?:value|path)\s*=\s*", "", argument)


def _route_registrations(ctx: EnrichmentInput, symbols: _Symbols, source: _Source,
                         refs: dict[str, list[list[str]]]) -> list[FrameworkBoundaryCandidate]:
    files = {str(f.get("path")): f for p in ctx.route_index.get("repos", []) or [] if p.get("repo") == ctx.repo
             for f in p.get("files", []) or []}
    out = []
    for flow in ctx.route_flows:
        handler, hfile = str(flow.get("handler") or ""), str(flow.get("handler_file") or "")
        route_file = files.get(hfile) or {}
        decl = next((r for r in route_file.get("route_calls", []) or [] if r.get("handler_hint") == handler), None)
        facts: list[StructuralFact] = []
        missing: list[str] = []
        for decorator in _decorators(ctx.decorated, handler, hfile):
            facts.append(StructuralFact(PROVENANCE_CBM_DECORATOR, f"{handler} is decorated {_first_line(decorator)}", hfile))
        entry = next((d for d in ctx.decorated if d.get("symbol") == handler and d.get("file") == hfile), None)
        if entry and entry.get("route_path"):
            facts.append(StructuralFact(PROVENANCE_CBM_ENTRYPOINT,
                                        f"CBM route metadata: {entry.get('route_method')} {entry.get('route_path')}", hfile))
        if decl is None:
            continue
        receiver = str(decl.get("receiver") or "")
        declared = str(decl.get("path") or "")
        snippet = str(decl.get("snippet") or "")
        expression = _route_argument(snippet)
        literal = expression is not None and (expression == "" or _STRING_ARG.match(expression) is not None)
        facts.append(StructuralFact(PROVENANCE_SOURCE_FALLBACK,
                                    f"{handler} is registered on `{receiver}` with path "
                                    + (f"'{declared}'" if literal else f"expression `{expression or '?'}`"),
                                    hfile, decl.get("line")))
        if not literal:
            missing.append(f"route path `{expression or '?'}` is not a literal")
        # a decorator-declared container (a controller class) carries its own prefix
        for decorator in _decorators(ctx.decorated, receiver, hfile):
            match = re.match(r"@?\s*[A-Za-z_]\w*\s*\(\s*([^,)]*?)\s*\)\s*$", decorator)
            argument = (match.group(1) if match else "").strip()
            argument = re.sub(r"^(?:value|path)\s*=\s*", "", argument)
            if argument and "." in argument and _STRING_ARG.match(argument) is None:
                facts.append(StructuralFact(PROVENANCE_CBM_DECORATOR,
                                            f"{receiver} is decorated {_first_line(decorator)}", hfile))
                missing.append(f"container prefix `{argument}` is not a literal")
        # mounts of this router anywhere: a child named like the router, or an alias imported from its module
        mounts = []
        for path, item in files.items():
            aliases = {receiver}
            for imp in symbols.imports_of(path):
                # CBM records `from m import router as r` as source="router", local="r"
                if imp.get("resolved_file") == hfile and receiver in (imp.get("source"), imp.get("imported")):
                    aliases.add(str(imp.get("local") or ""))
            for mount in item.get("mount_calls", []) or []:
                if mount.get("child") in aliases:
                    mounts.append((path, mount))
        for path, mount in mounts:
            text = str(mount.get("snippet") or "")
            prefix_expr = re.search(r"\b(?:prefix|url_prefix)\s*=\s*([^,)]+)", text)
            symbolic = prefix_expr is not None and _STRING_ARG.match(prefix_expr.group(1)) is None
            facts.append(StructuralFact(
                PROVENANCE_SOURCE_FALLBACK,
                f"`{mount.get('child')}` is mounted"
                + (f" with prefix expression `{prefix_expr.group(1).strip()}`" if symbolic
                   else f" with prefix '{mount.get('prefix') or ''}'" if mount.get("prefix") else " without a prefix"),
                path, mount.get("line")))
            if symbolic:
                missing.append(f"mount prefix `{prefix_expr.group(1).strip()}` is not a literal; "
                               f"the route as shown omits it")
        for row in refs.get("router", []):
            if row[0].rsplit(".", 1)[-1] == receiver and row[4] in {p for p, _ in mounts}:
                facts.append(StructuralFact(PROVENANCE_CBM_USAGE, f"CBM: `{receiver}` is used in {row[4]}", row[4]))
                break
        label = f"{flow.get('method')} {flow.get('path')}"
        out.append(FrameworkBoundaryCandidate(
            category=CATEGORY_ROUTE_REGISTRATION, source_symbol=handler, via=label, targets=[handler],
            status=STATUS_UNRESOLVED if missing else STATUS_RESOLVED, facts=facts, missing=missing,
            resolved_by=None if missing else "route composition (literal path and prefixes)",
        ))
    return out


def enrich(ctx: EnrichmentInput, refs_fetch: Callable[[list[str]], list[list[str]]] | None) -> EnrichmentResult:
    """Run the triggered detectors; at most one batched CBM reference request in total."""
    result = EnrichmentResult()
    symbols, source = _Symbols(ctx.symbol_index, ctx.repo), _Source(ctx.repo_root)

    dispatch = _message_dispatch(ctx, symbols)
    callbacks = []
    for e in ctx.reached_entrypoints:
        if e.get("kind") != "decorated" or e.get("route_method"):
            continue
        parts = str(e.get("qualified_name") or e.get("symbol") or "").rsplit(".", 2)
        if len(parts) >= 2 and parts[-2][:1].isupper():  # a method of a class: Owner.method
            callbacks.append({**e, "symbol": f"{parts[-2]}.{parts[-1]}"})
    if dispatch:
        result.triggered.append(f"{CATEGORY_MESSAGE_DISPATCH}: {len(dispatch)} unresolved handler(s)")
    if callbacks:
        result.triggered.append(f"{CATEGORY_CALLBACK_REGISTRATION}: {len(callbacks)} callback entrypoint(s)")
    if ctx.route_flows:
        result.triggered.append(f"{CATEGORY_ROUTE_REGISTRATION}: {len(ctx.route_flows)} route(s)")

    # ---- one batched CBM request for every reference the detectors need
    seeds: dict[str, str] = {}
    for _method, message, _handler in dispatch:
        for sym in symbols.find(message, kind="class"):
            if sym.get("cbm_qualified_name"):
                seeds[str(sym["cbm_qualified_name"])] = "message"
    for entry in callbacks:
        owner = str(entry["symbol"]).rsplit(".", 1)[0]
        for sym in symbols.find(owner, file=str(entry.get("file")), kind="class"):
            if sym.get("cbm_qualified_name"):
                seeds[str(sym["cbm_qualified_name"])] = "callback"
    route_files = {str(f.get("handler_file")) for f in ctx.route_flows}
    for path in route_files:
        for sym in symbols.in_file(path):
            if sym.get("kind") == "variable" and sym.get("cbm_qualified_name"):
                seeds[str(sym["cbm_qualified_name"])] = "router"
    chosen = sorted(seeds)[:MAX_SEEDS]
    if len(seeds) > MAX_SEEDS:
        result.notes.append(f"structural enrichment looked up {MAX_SEEDS} of {len(seeds)} reference targets")
    rows = refs_fetch(chosen) if (refs_fetch and chosen) else []
    result.reference_targets = len(chosen)
    queries = 1 if (refs_fetch and chosen) else 0
    refs: dict[str, list[list[str]]] = {"message": [], "callback": [], "router": []}
    for row in rows:
        refs.setdefault(seeds.get(row[0], ""), []).append(row)

    # ---- message dispatch: producers of the message, the call that hands it on, the wiring
    for method, message, handler in dispatch:
        target = f"{handler['name']}.{method['name']}"
        facts = [StructuralFact(PROVENANCE_CBM_DECORATOR, f"{handler['name']} is decorated {handler['decorator']}",
                                handler["file"])]
        producers = [r for r in refs["message"] if r[0].rsplit(".", 1)[-1] == message
                     and r[3] in ("Method", "Function") and not _TEST_PATH.search(r[4] or "")]
        facts.extend(_registrations(ctx, symbols, source, handler["name"], handler["file"]))
        if not producers:
            result.candidates.append(FrameworkBoundaryCandidate(
                category=CATEGORY_MESSAGE_DISPATCH, source_symbol="?", via=f"? ({message})", targets=[target],
                message_type=message, facts=facts, queries=queries,
                missing=[f"no repository code that constructs or references {message} was found"]))
            continue
        for row in producers:
            parts = row[2].rsplit(".", 2)
            producer_name = ".".join(parts[-2:]) if len(parts) > 2 else parts[-1]
            pfacts = list(facts) + [StructuralFact(PROVENANCE_CBM_CALL, f"{producer_name} constructs/references {message}",
                                                   row[4], int(row[5]) if str(row[5]).isdigit() else None)]
            via = f"? ({message})"
            psym = next((s for s in symbols.find(parts[-1], file=row[4]) if _display(s) == producer_name), None)
            if psym is not None:
                span = source.span(row[4], psym.get("start_line"), psym.get("end_line"))
                bound = {m.group(1) for _, t in span for m in re.finditer(rf"\b(\w+)\s*(?::[^=]+)?=\s*new\s+{message}\b", t)}
                found = _call_passing(span, bound | {f"new {message}"})
                if found:
                    line, receiver, member, _args = found
                    field_name = receiver.split(".")[-1]
                    owner_class = next((s for s in symbols.find(str(psym.get("parent") or ""), file=row[4], kind="class")), None)
                    owner_lines = [t for _, t in source.span(row[4], owner_class.get("start_line"), owner_class.get("end_line"))] if owner_class else []
                    rtype = _declared_type(owner_lines, field_name) or receiver
                    via = f"{rtype}.{member}({message})"
                    pfacts.append(StructuralFact(PROVENANCE_SOURCE_FALLBACK,
                                                 f"{producer_name} calls {receiver}.{member}(…{message}…)"
                                                 + (f"; `{field_name}` is declared {rtype}" if rtype != receiver else ""),
                                                 row[4], line))
            closed = next((c for c in ctx.composed if c.get("to") == target and c.get("from") == producer_name), None)
            result.candidates.append(FrameworkBoundaryCandidate(
                category=CATEGORY_MESSAGE_DISPATCH, source_symbol=producer_name, via=via, targets=[target],
                message_type=message, facts=pfacts, queries=queries,
                status=STATUS_RESOLVED if closed else STATUS_UNRESOLVED,
                resolved_by=f"composed dispatch ({closed.get('rule')}: static + runtime)" if closed else None,
                missing=[] if closed else [(f"no evidence that the framework delivers {message} to {target} "
                                            "(the call goes into framework code; runtime through_external "
                                            "evidence would show it)")]))

    # ---- callback registration: where the callback's class is handed to the framework
    for entry in callbacks:
        owner, member = str(entry["symbol"]).rsplit(".", 1)
        facts = [StructuralFact(PROVENANCE_CBM_ENTRYPOINT,
                                f"{entry['symbol']} is a reached entrypoint with decorator metadata and no route",
                                entry.get("file"))]
        for decorator in _decorators(ctx.decorated, owner, str(entry.get("file"))):
            facts.append(StructuralFact(PROVENANCE_CBM_DECORATOR, f"{owner} is decorated {_first_line(decorator)}", entry.get("file")))
        users = sorted({r[4] for r in refs["callback"] if r[0].rsplit(".", 1)[-1] == owner
                        and r[4] != entry.get("file") and not _TEST_PATH.search(r[4] or "")})
        registered = False
        for path in users:
            facts.append(StructuralFact(PROVENANCE_CBM_USAGE, f"CBM: {path} references {owner}", path))
            lines = source.lines(path)
            names = {m.group(1) for t in lines for m in re.finditer(rf"\b{owner}\s+(\w+)\s*[;=,)]", t)}
            names |= {m.group(1) for t in lines for m in re.finditer(rf"\b(\w+)\s*:\s*{owner}\b", t)}
            names |= {m.group(1) for t in lines for m in re.finditer(rf"\b(\w+)\s*=\s*new\s+{owner}\b", t)}
            found = _call_passing(list(enumerate(lines, start=1)), names | {f"new {owner}"}) if names else None
            if not found:
                continue
            line, receiver, call_member, args = found
            container = symbols.enclosing(path, line)
            container_name = _display(container) if container else _enclosing_by_source(symbols, source, path, line)
            facts.append(StructuralFact(PROVENANCE_SOURCE_FALLBACK,
                                        f"{container_name} passes {owner} to {receiver}.{call_member}({args})", path, line))
            first_arg = args.split(",")[0].strip()
            result.candidates.append(FrameworkBoundaryCandidate(
                category=CATEGORY_CALLBACK_REGISTRATION, source_symbol=container_name,
                via=f"{call_member}({first_arg}, …)" if "," in args else f"{call_member}({first_arg})",
                targets=[str(entry["symbol"])], facts=list(facts), queries=queries,
                missing=[f"the framework's invocation of {entry['symbol']} (lifecycle, not a call in the source)"]))
            registered = True
            break
        if not registered:
            result.candidates.append(FrameworkBoundaryCandidate(
                category=CATEGORY_CALLBACK_REGISTRATION, source_symbol="?", via="?", targets=[str(entry["symbol"])],
                facts=facts, queries=queries,
                missing=[f"no repository code that hands {owner} to the framework was found"]))

    # ---- route registration: decorator, router, mount and prefix behind each route
    for candidate in _route_registrations(ctx, symbols, source, refs):
        candidate.queries = queries
        result.candidates.append(candidate)
    return result


_SIGNATURE = re.compile(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\{\s*$")


def _enclosing_by_source(symbols: _Symbols, source: _Source, path: str, line: int) -> str:
    """`Class.method` around `line` when no CBM method span covers it (CBM keeps one
    symbol per qualified name, so overloads such as Java's `configure(...)` lose theirs):
    the CBM class span that contains it, and the nearest method signature above it."""
    owner = next((s for s in symbols.in_file(path) if s.get("kind") == "class"
                  and (s.get("start_line") or 0) <= line <= (s.get("end_line") or 0)), None)
    lines = source.lines(path)
    for number in range(line, max(0, line - MAX_SPAN_LINES), -1):
        match = _SIGNATURE.search(lines[number - 1]) if number - 1 < len(lines) else None
        if match and match.group(1) not in ("if", "for", "while", "switch", "catch", "synchronized"):
            return f"{owner['name']}.{match.group(1)}" if owner else match.group(1)
    return owner["name"] if owner else path


def _registrations(ctx: EnrichmentInput, symbols: _Symbols, source: _Source,
                   handler: str, handler_file: str) -> list[StructuralFact]:
    """Decorated container classes in files that import the handler and name it (wiring)."""
    out = []
    for path in symbols.files:
        if path == handler_file or _TEST_PATH.search(path):
            continue
        if not any(imp.get("local") == handler or imp.get("imported") == handler for imp in symbols.imports_of(path)):
            continue
        containers = [d for d in ctx.decorated if d.get("file") == path and d.get("symbol")
                      and any(s.get("kind") == "class" and s.get("name") == d.get("symbol") for s in symbols.in_file(path))]
        named = next((n for n, t in enumerate(source.lines(path), start=1)
                      if re.search(rf"\b{handler}\b", t) and "import" not in t), None)
        for container in containers:
            decorator = _first_line(str(container.get("decorators") or ""), 60)
            out.append(StructuralFact(PROVENANCE_CBM_REGISTRATION,
                                      f"{container['symbol']} ({decorator}) is in a file that imports {handler}", path))
        if named is not None:
            out.append(StructuralFact(PROVENANCE_SOURCE_FALLBACK, f"{path} names {handler} outside its import", path, named))
    return out
