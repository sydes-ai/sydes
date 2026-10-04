"""Targeted structural enrichment when ordinary traversal stalls at a framework boundary.

Track A first walks CBM's call graph. Where that walk stops at code a framework invokes, this
module plans the structural questions that are still missing and asks the CBM fact layer
(`sydes.code_intelligence.cbm_facts`) before anything escalates to runtime evidence or AI.
What is asked is decided by what is missing, never by a framework name:

* message dispatch -- a changed method left unresolved whose class is decorated `@X(T)` with
  `T` a repository class: the handler's DECORATES; who invokes or references `T` (CALLS,
  CALL_REFERENCE and USAGE, each kept as itself, with the call's line, arguments and
  resolution strategy); the producer's own route (HANDLES) and decorators; the call that
  hands `T` on and its receiver's declared type (CBM source search); where the handler is
  wired (CBM file search, the wiring class's DECORATES, imports, CONFIGURES); then a
  direct-relation check and a bounded CALLS path between producer and handler before
  saying there is no structural path.
* callback registration -- a reached entrypoint that is decorator metadata with no route:
  who references its class (typed relations), its INHERITS / IMPLEMENTS / OVERRIDE (an
  INHERITS CBM resolved to a non-type node is reported and ignored), CONFIGURES, the call
  that hands an instance of it on (CBM source search), and the same direct/path checks.
* route registration -- for every route in an affected flow: CBM's HANDLES and Route node,
  the HTTP_CALLS declaration with its arguments, the handler's DECORATES, the router's mount
  (USAGE, which carries the import alias) and prefix (CBM source search) -- compared with
  Sydes' own route; a disagreement is kept and flagged, and a path or prefix that is not a
  literal is reported instead of trusting a route composed without it.

Nothing here creates an edge. Each finding is a `FrameworkBoundaryCandidate`: every fact with
its provenance, the checks run and what they found (including "none"), candidate targets,
what is still missing and which runtime edge would close it. `resolved` only when the facts
compose deterministically (literal routes) or an existing rule closed it (composed dispatch).

Evidence classes stay distinct: explicit CBM relations (`cbm_call`, `cbm_call_reference`,
`cbm_usage`, `cbm_decorator`, `cbm_handles`, `cbm_http_calls`, `cbm_configures`,
`cbm_inherits`, `cbm_override`, `cbm_registration`, `cbm_entrypoint`), CBM path checks
(`cbm_path`), CBM's source search (`cbm_source`) and, last, a local read
(`source_fallback`) -- used only when CBM had no answer, each one saying why. Deterministic
composition is the candidate's `resolved_by`; runtime evidence is `runtime_needed`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROVENANCE_CBM_SYMBOL = "cbm_symbol"
PROVENANCE_CBM_CALL = "cbm_call"
PROVENANCE_CBM_CALL_REFERENCE = "cbm_call_reference"
PROVENANCE_CBM_USAGE = "cbm_usage"
PROVENANCE_CBM_DECORATOR = "cbm_decorator"
PROVENANCE_CBM_HANDLES = "cbm_handles"
PROVENANCE_CBM_HTTP_CALLS = "cbm_http_calls"
PROVENANCE_CBM_CONFIGURES = "cbm_configures"
PROVENANCE_CBM_INHERITS = "cbm_inherits"
PROVENANCE_CBM_OVERRIDE = "cbm_override"
PROVENANCE_CBM_ENTRYPOINT = "cbm_entrypoint"
PROVENANCE_CBM_REGISTRATION = "cbm_registration"
PROVENANCE_CBM_PATH = "cbm_path"
PROVENANCE_CBM_SOURCE = "cbm_source"
PROVENANCE_SOURCE_FALLBACK = "source_fallback"

#: CALLS, CALL_REFERENCE and USAGE are different claims and stay so downstream.
REFERENCE_PROVENANCE = {"CALLS": PROVENANCE_CBM_CALL, "CALL_REFERENCE": PROVENANCE_CBM_CALL_REFERENCE,
                        "USAGE": PROVENANCE_CBM_USAGE}
_REFERENCE_VERB = {"CALLS": "calls", "CALL_REFERENCE": "passes/stores a reference to", "USAGE": "uses"}

CATEGORY_MESSAGE_DISPATCH = "message_dispatch"
CATEGORY_CALLBACK_REGISTRATION = "callback_registration"
CATEGORY_ROUTE_REGISTRATION = "route_registration"

STATUS_UNRESOLVED = "unresolved"
STATUS_RESOLVED = "resolved"

MAX_SEEDS = 80
MAX_SPAN_LINES = 400
_DECORATOR_WITH_TYPE = re.compile(r"@\s*([A-Za-z_][\w.]*)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*$")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|\.(spec|test)\.[jt]sx?$|Test\.java$|(^|/)test_[^/]*\.py$")
_STRING_ARG = re.compile(r"""^\s*(?:[rbuf]{0,2})(['"`])(?P<value>[^'"`]*)\1\s*$""")
_ROUTE_QN = re.compile(r"__route__([A-Z]+)__(.*)$")
_MOUNT_CALL = re.compile(r"\.\s*(include_router|register_blueprint|mount|use|add_router)\s*\(")
_PREFIX_ARG = re.compile(r"\b(?:prefix|url_prefix)\s*=\s*([^,)]+)")
_TYPE_LABELS = {"Class", "Interface", "Type", "Enum"}
_CONFIG_FILE = re.compile(r"\.(xml|ya?ml|json|properties|toml|ini|cfg|conf)$", re.IGNORECASE)


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
    continues it. Not an edge: `targets` are candidates, `checks` record the path lookups
    made (and what they found, "none" included), `missing` / `runtime_needed` say what
    would close it."""

    category: str
    source_symbol: str
    via: str
    targets: list[str]
    status: str = STATUS_UNRESOLVED
    message_type: str | None = None
    facts: list[StructuralFact] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    runtime_needed: str | None = None
    resolved_by: str | None = None
    #: CBM requests the enrichment pass made (shared by the run's candidates), for budgeting
    queries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "status": self.status, "source_symbol": self.source_symbol,
            "via": self.via, "targets": list(self.targets), "message_type": self.message_type,
            "facts": [f.to_dict() for f in self.facts], "checks": dict(self.checks),
            "missing": list(self.missing), "runtime_needed": self.runtime_needed,
            "resolved_by": self.resolved_by, "queries": self.queries,
        }


@dataclass
class EnrichmentInput:
    repo: str
    repo_root: Path
    #: changed symbols ordinary traversal left unresolved: {"name", "file", "qualified_name"}
    unresolved: list[dict[str, Any]]
    #: entrypoints the traversal reached: {"symbol", "qualified_name", "file", "kind", "route_method"}
    reached_entrypoints: list[dict[str, Any]]
    #: route flows: {"method", "path", "handler", "handler_file"}
    route_flows: list[dict[str, Any]]
    #: CBM decorated symbols (the decorator sweep), as Sydes already fetched them
    decorated: list[dict[str, Any]]
    symbol_index: dict[str, Any]
    route_index: dict[str, Any]
    #: existing composed dispatch records (result.composed_dispatch_edges)
    composed: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EnrichmentResult:
    candidates: list[FrameworkBoundaryCandidate] = field(default_factory=list)
    triggered: list[str] = field(default_factory=list)
    #: fact families that contributed at least one fact or check
    families: list[str] = field(default_factory=list)
    #: the fact layer's request / cache-hit counters after this pass
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- local indexes


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

    def qn(self, name: str, file: str | None = None, kind: str | None = None, parent: str | None = None) -> str | None:
        """The CBM qualified name when exactly one symbol matches (overloads collapse to one)."""
        hits = [s for s in self.find(name, file, kind) if parent is None or s.get("parent") == parent]
        names = {s.get("cbm_qualified_name") for s in hits if s.get("cbm_qualified_name")}
        return names.pop() if len(names) == 1 else None

    def imports_of(self, file: str) -> list[dict[str, Any]]:
        return list((self.files.get(file) or {}).get("imports", []) or [])


class _Source:
    """Local reads -- the last deterministic source, used only after CBM had no answer."""

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

    def span(self, file: str, start: Any, end: Any) -> list[tuple[int, str]]:
        lines = self.lines(file)
        lo = max(1, int(start or 1))
        hi = min(len(lines), int(end or lo), lo + MAX_SPAN_LINES)
        return [(n, lines[n - 1]) for n in range(lo, hi + 1)]


def _who(relation: Any) -> str:
    """The source of a relation, readably: a file-level use is named by its file."""
    return str(relation.source_file) if str(relation.source).endswith("__file__") else _short(relation.source)


def _short(qn: str, parts: int = 2) -> str:
    pieces = str(qn).rsplit(".", parts)
    return ".".join(pieces[-parts:]) if len(pieces) > parts else str(qn)


def _first_line(text: str, limit: int = 80) -> str:
    """The text on one line (whitespace collapsed), cut at `limit`."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _sweep_decorators(decorated: list[dict[str, Any]], symbol: str, file: str) -> list[str]:
    for item in decorated:
        if item.get("symbol") == symbol and item.get("file") == file:
            parts = [d.strip() for d in str(item.get("decorators") or "").split("\n@") if d.strip()]
            return [d if d.startswith("@") else "@" + d for d in parts]
    return []


def _call_props(props: dict[str, Any]) -> str:
    """A CALLS relation's own properties, as CBM recorded them."""
    bits = []
    if props.get("line"):
        bits.append(f"line {props['line']}")
    args = [str(a.get("e")) for a in props.get("args", []) or [] if isinstance(a, dict) and a.get("e")]
    if args:
        bits.append(f"args ({_first_line(', '.join(args), 50)})")
    if props.get("strategy"):
        bits.append(f"resolved by {props['strategy']} at {props.get('confidence')}")
    return f" [{'; '.join(bits)}]" if bits else ""


def _call_passing(text_lines: Iterable[tuple[int, str]], names: set[str]) -> tuple[int, str, str, str] | None:
    """The first call `recv.member(..., name, ...)` passing one of `names` (or `new T(...)`
    when `names` holds `new T`): (line, receiver expression, member, the argument text)."""
    call = re.compile(r"([A-Za-z_$][\w$.]*)\.([A-Za-z_$][\w$]*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
    for number, text in text_lines:
        for match in call.finditer(text):
            for name in names:
                if re.search(rf"(^|[\s,(]){re.escape(name)}\b", match.group(3)):
                    return number, match.group(1), match.group(2), match.group(3).strip()
    return None


def _declared_type(text: str, field_name: str) -> str | None:
    """`field: T` / `private readonly field: T` (TS) or `T field;` (Java)."""
    ts = re.search(rf"\b{re.escape(field_name)}\s*:\s*([A-Z][\w]*)", text)
    if ts:
        return ts.group(1)
    java = re.search(rf"\b([A-Z][\w<>]*)\s+{re.escape(field_name)}\s*[;=,)]", text)
    return java.group(1) if java else None


def numbered(row: dict[str, Any], pattern: str) -> list[tuple[int, str]]:
    """A CBM search row's source as (line, text). CBM returns a small symbol whole and a
    large one as a window around its matches, so lines are numbered from the match lines
    (`matches`, in order of the lines holding `pattern`), falling back to the span start."""
    lines = str(row.get("source") or "").splitlines()
    hits = [i for i, text in enumerate(lines) if pattern in text]
    matches = [int(m) for m in row.get("matches", []) or [] if str(m).isdigit()]
    if not hits or not matches:
        span = str(row.get("lines") or "").split("-")[0]
        start = int(span) if span.isdigit() else 1
        return [(start + i, text) for i, text in enumerate(lines)]
    anchors = list(zip(hits, matches))
    out = []
    for i, text in enumerate(lines):
        hit, line = next(((h, m) for h, m in reversed(anchors) if h <= i), anchors[0])
        out.append((line + i - hit, text))
    return out


# ----------------------------------------------------------------------------- planner


class _Planner:
    """Asks the fact layer only for what each trigger is missing, in batched waves."""

    def __init__(self, ctx: EnrichmentInput, facts: Any) -> None:
        self.ctx, self.facts = ctx, facts
        self.symbols, self.source = _Symbols(ctx.symbol_index, ctx.repo), _Source(ctx.repo_root)
        self.relations: dict[str, list[Any]] = {}
        self.families: set[str] = set()

    def fetch(self, seeds: Iterable[str | None]) -> list[str]:
        wanted = [s for s in dict.fromkeys(seeds) if s]
        dropped = wanted[MAX_SEEDS:]
        if self.facts is not None and wanted[:MAX_SEEDS]:
            self.relations.update(self.facts.relations(wanted[:MAX_SEEDS]))
        return dropped

    def rel(self, qn: str | None, types: Iterable[str], *, inbound: bool | None = None) -> list[Any]:
        if not qn:
            return []
        wanted = set(types)
        out = []
        for r in self.relations.get(qn, []):
            if r.type not in wanted or (inbound is True and r.target != qn) or (inbound is False and r.source != qn):
                continue
            out.append(r)
            self.families.add(r.type)
        return out

    def supports(self, relation_type: str) -> bool:
        return self.facts is not None and self.facts.supports(relation_type)

    def search(self, pattern: str, path_filter: str | None = None, mode: str = "full") -> dict[str, Any] | None:
        """CBM source search; None when there is no CBM session or the budget is spent."""
        if self.facts is None:
            return None
        found = self.facts.search(pattern, path_filter, mode)
        if found is not None and (found.get("rows") or found.get("files")):
            self.families.add("search_code")
        return found

    def decorators(self, qn: str | None, name: str, file: str) -> list[tuple[str, str]]:
        """A symbol's decorators with their source: CBM DECORATES relations first
        (structured), then any the decorator sweep (CBM's captured decorator source) holds
        that DECORATES does not -- CBM keeps one DECORATES edge per decorator name, so a
        repeated decorator (`@ApiResponse` twice) survives only in the sweep."""
        native = [str(r.props["decorator"]) for r in self.rel(qn, ["DECORATES"], inbound=False) if r.props.get("decorator")]
        seen = [re.sub(r"\s+", "", d) for d in native]
        swept = [d for d in _sweep_decorators(self.ctx.decorated, name, file)
                 if not any(k.startswith(re.sub(r"\s+", "", d)) or re.sub(r"\s+", "", d).startswith(k) for k in seen)]
        return [(d, "DECORATES") for d in native] + [(d, "decorator sweep") for d in swept]

    def path_checks(self, left: list[str], right: list[str]) -> dict[str, str]:
        """Any direct relation, then a bounded CALLS path, before 'no structural path'."""
        left, right = [q for q in left if q], [q for q in right if q]
        if self.facts is None or not left or not right:
            return {}
        self.families.update({"direct_relation", "calls_path"})
        direct = self.facts.direct(left, right)
        paths = self.facts.calls_path(left, right)
        return {
            "direct_relation": "; ".join(f"{_short(r[0])} -{r[1]}-> {_short(r[2])}" for r in direct) or "none",
            "calls_path": "; ".join(f"{_short(a)} ->…-> {_short(b)}" for a, b, *_ in paths) or "none within 5 hops",
        }


def enrich(ctx: EnrichmentInput, facts: Any) -> EnrichmentResult:
    """Plan and run the triggered fact families. `facts` is the CBM fact layer for the
    repository (`CBMFacts`), or None without a CBM session -- only local facts then, each
    labelled `source_fallback`."""
    plan = _Planner(ctx, facts)
    result = EnrichmentResult()
    symbols = plan.symbols

    # ---- triggers: decided from structure already in hand, no query
    dispatch = []
    for changed in ctx.unresolved:
        file, name = str(changed.get("file") or ""), str(changed.get("name") or "").rsplit(".", 1)[-1]
        for method in [s for s in symbols.find(name, file=file) if s.get("parent")]:
            owner = str(method["parent"])
            for decorator in _sweep_decorators(ctx.decorated, owner, file):
                match = _DECORATOR_WITH_TYPE.match(decorator)
                if match and symbols.find(match.group(2), kind="class"):
                    dispatch.append((method, owner, file, match.group(2)))
    callbacks = []
    for e in ctx.reached_entrypoints:
        parts = str(e.get("qualified_name") or e.get("symbol") or "").rsplit(".", 2)
        if e.get("kind") == "decorated" and not e.get("route_method") and len(parts) >= 2 and parts[-2][:1].isupper():
            callbacks.append({**e, "owner": parts[-2], "member": parts[-1]})
    if dispatch:
        result.triggered.append(f"{CATEGORY_MESSAGE_DISPATCH}: {len(dispatch)} unresolved handler(s)")
    if callbacks:
        result.triggered.append(f"{CATEGORY_CALLBACK_REGISTRATION}: {len(callbacks)} callback entrypoint(s)")
    if ctx.route_flows:
        result.triggered.append(f"{CATEGORY_ROUTE_REGISTRATION}: {len(ctx.route_flows)} route(s)")
    if not (dispatch or callbacks or ctx.route_flows):
        return result

    # ---- wave 1: relations of the symbols each trigger starts from (one request)
    wave1: list[str | None] = []
    for method, owner, file, message in dispatch:
        wave1 += [symbols.qn(owner, file, "class"), method.get("cbm_qualified_name"), symbols.qn(message, kind="class")]
    for cb in callbacks:
        wave1 += [symbols.qn(cb["owner"], cb.get("file"), "class"), cb.get("qualified_name")]
    for flow in ctx.route_flows:
        wave1.append(symbols.qn(str(flow.get("handler")), str(flow.get("handler_file"))))
    dropped = plan.fetch(wave1)

    pending: list[tuple[str, dict[str, Any]]] = []
    for method, owner, file, message in dispatch:
        pending.append(("dispatch", _dispatch_wave1(plan, method, owner, file, message)))
    for cb in callbacks:
        pending.append(("callback", _callback_wave1(plan, cb)))
    for flow in ctx.route_flows:
        pending.append(("route", _route_wave1(plan, flow)))

    # ---- wave 2: relations of what wave 1 discovered: producers, wiring, routes, routers (one request)
    dropped += plan.fetch([s for _, state in pending for s in state.get("wave2", [])])
    if dropped:
        result.notes.append(f"structural enrichment skipped relations of {len(dropped)} symbol(s) over the {MAX_SEEDS}-seed cap")

    for kind, state in pending:
        if kind == "dispatch":
            result.candidates.extend(_dispatch_finish(plan, state))
        elif kind == "callback":
            result.candidates.extend(_callback_finish(plan, state))
        else:
            candidate = _route_finish(plan, state)
            if candidate is not None:
                result.candidates.append(candidate)

    if facts is not None:
        result.stats = facts.stats()
        result.notes.extend(facts.notes)
        for candidate in result.candidates:
            candidate.queries = int(result.stats.get("total_requests", 0))
    result.families = sorted(plan.families)
    return result


# ----------------------------------------------------------------------------- message dispatch


def _dispatch_wave1(plan: _Planner, method: dict[str, Any], owner: str, file: str, message: str) -> dict[str, Any]:
    symbols = plan.symbols
    owner_qn, message_qn = symbols.qn(owner, file, "class"), symbols.qn(message, kind="class")
    decorator, how = next(((d, h) for d, h in plan.decorators(owner_qn, owner, file)
                           if _DECORATOR_WITH_TYPE.match(" ".join(d.split()))), (f"@?({message})", "decorator sweep"))
    facts = [StructuralFact(PROVENANCE_CBM_DECORATOR, f"{owner} is decorated {_first_line(decorator)} ({how})", file)]
    producers = []
    for r in plan.rel(message_qn, ["CALLS", "CALL_REFERENCE", "USAGE"], inbound=True):
        if r.source_label in ("Method", "Function") and not _TEST_PATH.search(r.source_file or ""):
            producers.append(r)
            facts.append(StructuralFact(REFERENCE_PROVENANCE[r.type],
                                        f"{_short(r.source)} {_REFERENCE_VERB[r.type]} {message}{_call_props(r.props)}",
                                        r.source_file, r.props.get("line")))
    # where the handler class is wired: files naming it (CBM), else files importing it (local index)
    found = plan.search(owner, None, "files")
    if found is not None:
        wiring = [(f, PROVENANCE_CBM_SOURCE) for f in found.get("files", []) if f != file and not _TEST_PATH.search(f)]
    else:
        wiring = [(p, PROVENANCE_SOURCE_FALLBACK) for p in symbols.files if p != file and not _TEST_PATH.search(p)
                  and any(i.get("local") == owner or i.get("imported") == owner for i in symbols.imports_of(p))]
    wiring_classes = [s.get("cbm_qualified_name") for f, _ in wiring for s in symbols.in_file(f)
                      if s.get("kind") == "class" and s.get("cbm_qualified_name")]
    # ensure each producer is a single record even when it both CALLS and USEs the message
    unique = {r.source: r for r in sorted(producers, key=lambda r: r.type != "CALLS")}
    return {"method": method, "owner": owner, "owner_qn": owner_qn, "file": file, "message": message,
            "target": f"{owner}.{method['name']}", "target_qn": method.get("cbm_qualified_name"),
            "facts": facts, "producers": list(unique.values()), "wiring": wiring,
            "wave2": list(unique) + wiring_classes}


def _dispatch_finish(plan: _Planner, s: dict[str, Any]) -> list[FrameworkBoundaryCandidate]:
    symbols, message, target, owner = plan.symbols, s["message"], s["target"], s["owner"]
    wiring: list[StructuralFact] = []
    for path, provenance in s["wiring"]:
        imports = any(i.get("local") == owner or i.get("imported") == owner for i in symbols.imports_of(path))
        wiring.append(StructuralFact(provenance, f"{path} names {owner}"
                                     + ("" if provenance == PROVENANCE_CBM_SOURCE else " (CBM source search unavailable; local import index)")
                                     + (" and imports it" if imports else ""), path))
        for cls in [c for c in symbols.in_file(path) if c.get("kind") == "class"]:
            for decorator, how in plan.decorators(cls.get("cbm_qualified_name"), str(cls["name"]), path):
                wiring.append(StructuralFact(PROVENANCE_CBM_REGISTRATION if imports else PROVENANCE_CBM_DECORATOR,
                                             f"{cls['name']} is decorated {_first_line(decorator, 70)} ({how})"
                                             + (f" in a file that imports {owner}" if imports else ""), path))
            for r in plan.rel(cls.get("cbm_qualified_name"), ["CONFIGURES"]):
                wiring.append(StructuralFact(PROVENANCE_CBM_CONFIGURES, f"{_short(r.source)} configures {_short(r.target)}"
                                             f" ({r.props.get('strategy') or 'configuration'})", path))
    base = [f for f in s["facts"] if f.provenance == PROVENANCE_CBM_DECORATOR]
    if not s["producers"]:
        return [FrameworkBoundaryCandidate(
            category=CATEGORY_MESSAGE_DISPATCH, source_symbol="?", via=f"? ({message})", targets=[target],
            message_type=message, facts=base + wiring,
            missing=[(f"no repository code that constructs or references {message} was found "
                      "(CBM CALLS / CALL_REFERENCE / USAGE into it: none outside tests)")])]
    out = []
    for producer in s["producers"]:
        name = _short(producer.source)
        facts = base + [f for f in s["facts"] if f.provenance in REFERENCE_PROVENANCE.values()
                        and f.fact.startswith(name + " ")]
        handles = plan.rel(producer.source, ["HANDLES"], inbound=False)
        for r in handles:
            route = _ROUTE_QN.search(r.target)
            facts.append(StructuralFact(PROVENANCE_CBM_HANDLES, f"{name} HANDLES "
                                        + (f"{route.group(1)} {route.group(2)}" if route else _short(r.target)),
                                        producer.source_file))
        if not handles and plan.facts is not None:
            facts.append(StructuralFact(PROVENANCE_CBM_HANDLES, f"CBM has no HANDLES relation for {name}"
                                        + ("" if plan.supports("HANDLES") else " (no HANDLES anywhere in this graph)")))
        for decorator, how in plan.decorators(producer.source, name.rsplit(".", 1)[-1], producer.source_file):
            facts.append(StructuralFact(PROVENANCE_CBM_DECORATOR, f"{name} is decorated {_first_line(decorator, 70)} ({how})",
                                        producer.source_file))
        facts += wiring
        via = f"? ({message})"
        handoff = _producer_call(plan, producer, message)
        if handoff is not None:
            line, receiver, member, receiver_type, provenance, why = handoff
            via = f"{receiver_type or receiver}.{member}({message})"
            facts.append(StructuralFact(provenance, f"{name} calls {receiver}.{member}(…{message}…)"
                                        + (f"; `{receiver.split('.')[-1]}` is declared {receiver_type}" if receiver_type else "")
                                        + (f" ({why})" if why else ""), producer.source_file, line))
        checks = plan.path_checks([producer.source], [s["target_qn"], s["owner_qn"]])
        for key, value in checks.items():
            facts.append(StructuralFact(PROVENANCE_CBM_PATH, f"{key.replace('_', ' ')} {name} → {target}: {value}"))
        closed = next((c for c in plan.ctx.composed if c.get("to") == target and c.get("from") == name), None)
        out.append(FrameworkBoundaryCandidate(
            category=CATEGORY_MESSAGE_DISPATCH, source_symbol=name, via=via, targets=[target], message_type=message,
            facts=facts, checks=checks, status=STATUS_RESOLVED if closed else STATUS_UNRESOLVED,
            resolved_by=f"composed dispatch ({closed.get('rule')}: static + runtime)" if closed else None,
            runtime_needed=None if closed else f"{name} → {target} observed through the framework (through_external)",
            missing=[] if closed else [(f"no evidence that the framework delivers {message} to {target}: "
                                        "the call goes into framework code")]))
    return out


def _producer_call(plan: _Planner, producer: Any, message: str) -> tuple[int, str, str, str | None, str, str] | None:
    """The call in the producer that hands the message on, and its receiver's declared type:
    CBM source search first, a local read only when CBM had nothing."""
    pattern = f"new {message}"
    found = plan.search(pattern, producer.source_file)
    rows = [r for r in (found or {}).get("rows", []) if r.get("qn") == producer.source]
    if rows:
        lines, provenance, why = numbered(rows[0], pattern), PROVENANCE_CBM_SOURCE, ""
    else:
        sym = next((x for x in plan.symbols.in_file(producer.source_file)
                    if x.get("cbm_qualified_name") == producer.source), None)
        if sym is None:
            return None
        lines = plan.source.span(producer.source_file, sym.get("start_line"), sym.get("end_line"))
        provenance = PROVENANCE_SOURCE_FALLBACK
        why = "CBM source search unavailable" if found is None else f"CBM source search found no `{pattern}` in it"
    bound = {m.group(1) for _, t in lines for m in re.finditer(rf"\b(\w+)\s*(?::[^=]+)?=\s*new\s+{message}\b", t)}
    call = _call_passing(lines, bound | {pattern})
    if call is None:
        return None
    line, receiver, member, _args = call
    field_name = receiver.split(".")[-1]
    declared = plan.search(field_name, producer.source_file)
    receiver_type = next((t for r in (declared or {}).get("rows", []) if (t := _declared_type(r.get("source", ""), field_name))), None)
    if receiver_type is None and declared is None:
        receiver_type = next((t for x in plan.source.lines(producer.source_file) if (t := _declared_type(x, field_name))), None)
    return line, receiver, member, receiver_type, provenance, why


# ----------------------------------------------------------------------------- callback registration


def _callback_wave1(plan: _Planner, cb: dict[str, Any]) -> dict[str, Any]:
    symbols, owner, file = plan.symbols, cb["owner"], str(cb.get("file"))
    owner_qn, target_qn, target = symbols.qn(owner, file, "class"), cb.get("qualified_name"), f"{owner}.{cb['member']}"
    facts = [StructuralFact(PROVENANCE_CBM_ENTRYPOINT, f"{target} is a reached entrypoint with decorator metadata and no route", file)]
    facts += [StructuralFact(PROVENANCE_CBM_DECORATOR, f"{owner} is decorated {_first_line(d)} ({how})", file)
              for d, how in plan.decorators(owner_qn, owner, file)]
    for r in plan.rel(owner_qn, ["INHERITS", "IMPLEMENTS"], inbound=False):
        if r.target_label in _TYPE_LABELS and _CONFIG_FILE.search(r.target_file or "") and not _CONFIG_FILE.search(file):
            facts.append(StructuralFact(PROVENANCE_CBM_INHERITS, f"ignored: CBM resolves {owner} {r.type} to "
                                        f"`{_short(r.target, 1)}` from {r.target_file} (a configuration file), not a type", file))
        elif r.target_label in _TYPE_LABELS:
            facts.append(StructuralFact(PROVENANCE_CBM_INHERITS, f"{owner} {r.type.lower()} {_short(r.target, 1)}", file))
        else:
            facts.append(StructuralFact(PROVENANCE_CBM_INHERITS, f"ignored: CBM resolves {owner} {r.type} to a "
                                        f"{r.target_label} node ({_short(r.target)}), not a type", file))
    for r in plan.rel(target_qn, ["OVERRIDE"], inbound=False):
        facts.append(StructuralFact(PROVENANCE_CBM_OVERRIDE, f"{target} overrides {_short(r.target)}", file))
    users = []
    for r in plan.rel(owner_qn, ["CALLS", "CALL_REFERENCE", "USAGE"], inbound=True):
        if r.source_file != file and not _TEST_PATH.search(r.source_file or ""):
            users.append(r)
            facts.append(StructuralFact(REFERENCE_PROVENANCE[r.type], f"{_who(r)} {_REFERENCE_VERB[r.type]} {owner}",
                                        r.source_file, r.props.get("line")))
    user_files = sorted({r.source_file for r in users})
    containers = [s.get("cbm_qualified_name") for f in user_files for s in symbols.in_file(f)
                  if s.get("kind") == "class" and s.get("cbm_qualified_name")]
    return {"owner": owner, "owner_qn": owner_qn, "target": target, "target_qn": target_qn, "file": file,
            "facts": facts, "user_files": user_files, "wave2": containers}


def _callback_finish(plan: _Planner, s: dict[str, Any]) -> list[FrameworkBoundaryCandidate]:
    owner, target, facts = s["owner"], s["target"], list(s["facts"])
    configures = plan.rel(s["owner_qn"], ["CONFIGURES"]) + plan.rel(s["target_qn"], ["CONFIGURES"])
    for r in configures:
        facts.append(StructuralFact(PROVENANCE_CBM_CONFIGURES, f"{_short(r.source)} configures {_short(r.target)}"
                                    f" ({r.props.get('strategy') or 'configuration'})", s["file"]))
    if not configures and plan.facts is not None:
        facts.append(StructuralFact(PROVENANCE_CBM_CONFIGURES, f"CBM records no CONFIGURES relation for {owner}"
                                    + (" (CBM emits CONFIGURES for configuration keys and environment access, not registrations)"
                                       if plan.supports("CONFIGURES") else " (no CONFIGURES anywhere in this graph)")))
    for path in s["user_files"]:
        for cls in [c for c in plan.symbols.in_file(path) if c.get("kind") == "class"]:
            facts += [StructuralFact(PROVENANCE_CBM_DECORATOR, f"{cls['name']} is decorated {_first_line(d)} ({how})", path)
                      for d, how in plan.decorators(cls.get("cbm_qualified_name"), str(cls["name"]), path)]
        registration = _registration_call(plan, owner, path)
        if registration is None:
            continue
        line, receiver, member, args, container, container_qn, provenance, why = registration
        facts.append(StructuralFact(provenance, f"{container} passes {owner} to {receiver}.{member}({args})"
                                    + (f" ({why})" if why else ""), path, line))
        checks = plan.path_checks([container_qn], [s["target_qn"], s["owner_qn"]])
        for key, value in checks.items():
            facts.append(StructuralFact(PROVENANCE_CBM_PATH, f"{key.replace('_', ' ')} {container} → {target}: {value}"))
        first = args.split(",")[0].strip()
        return [FrameworkBoundaryCandidate(
            category=CATEGORY_CALLBACK_REGISTRATION, source_symbol=container,
            via=f"{member}({first}, …)" if "," in args else f"{member}({first})", targets=[target], facts=facts,
            checks=checks, runtime_needed=f"{target} invoked by the framework after {container} registers it",
            missing=[f"the framework's invocation of {target} (lifecycle, not a call in the source)"])]
    return [FrameworkBoundaryCandidate(
        category=CATEGORY_CALLBACK_REGISTRATION, source_symbol="?", via="?", targets=[target], facts=facts,
        missing=[f"no repository code that hands {owner} to the framework was found"])]


def _bindings(owner: str, texts: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for text in texts:
        names |= {m.group(1) for m in re.finditer(rf"\b{owner}\s+(\w+)\s*[;=,)]", text)}
        names |= {m.group(1) for m in re.finditer(rf"\b(\w+)\s*:\s*{owner}\b", text)}
        names |= {m.group(1) for m in re.finditer(rf"\b(\w+)\s*=\s*new\s+{owner}\b", text)}
    return names


def _registration_call(plan: _Planner, owner: str, path: str) -> tuple | None:
    """Where `path` hands an instance of `owner` to another call: the binding (field,
    parameter or local) and the call passing it, from CBM source search -- with the
    enclosing method from CBM, or from a local signature scan when CBM reports only the
    class (it keeps one symbol per qualified name, so overloads lose their spans)."""
    found = plan.search(owner, path)
    rows = (found or {}).get("rows", [])
    if rows:
        for name in sorted(_bindings(owner, [r.get("source", "") for r in rows])):
            for row in (plan.search(name, path) or {}).get("rows", []):
                matched = {int(n) for n in row.get("matches", []) or [] if str(n).isdigit()}
                call = _call_passing([(n, t) for n, t in numbered(row, name) if n in matched], {name})
                if call is None:
                    continue
                line, receiver, member, args = call
                qn, label = str(row.get("qn") or ""), str(row.get("label") or "")
                if label in ("Method", "Function"):
                    return line, receiver, member, args, _short(qn), qn, PROVENANCE_CBM_SOURCE, ""
                method = _enclosing_by_signature(plan.source.lines(path), line)
                cls = _short(qn, 1)
                return (line, receiver, member, args, f"{cls}.{method}" if method else cls,
                        (plan.symbols.qn(method, path, parent=cls) if method else None) or qn, PROVENANCE_SOURCE_FALLBACK,
                        (f"CBM search places line {line} only in {label or 'a symbol'} {cls} "
                         "(overloads share one symbol); method from a local signature scan"))
        return None
    # CBM had nothing for this file: the local read, said so
    lines = plan.source.lines(path)
    names = _bindings(owner, lines)
    call = _call_passing(list(enumerate(lines, start=1)), names) if names else None
    if call is None:
        return None
    line, receiver, member, args = call
    method = _enclosing_by_signature(lines, line)
    cls = next((c["name"] for c in plan.symbols.in_file(path) if c.get("kind") == "class"
                and (c.get("start_line") or 0) <= line <= (c.get("end_line") or 0)), None)
    container = ".".join(x for x in (cls, method) if x) or path
    why = "CBM source search unavailable" if found is None else f"CBM source search found no `{owner}` in {path}"
    return line, receiver, member, args, container, plan.symbols.qn(method, path, parent=cls) if method else None, \
        PROVENANCE_SOURCE_FALLBACK, why


_SIGNATURE = re.compile(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\{\s*$")


def _enclosing_by_signature(lines: list[str], line: int) -> str | None:
    for number in range(min(line, len(lines)), max(0, line - MAX_SPAN_LINES), -1):
        match = _SIGNATURE.search(lines[number - 1])
        if match and match.group(1) not in ("if", "for", "while", "switch", "catch", "synchronized"):
            return match.group(1)
    return None


# ----------------------------------------------------------------------------- route registration

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
    return re.sub(r"^(?:value|path)\s*=\s*", "", (match.group(1) or "").strip())


def _route_wave1(plan: _Planner, flow: dict[str, Any]) -> dict[str, Any]:
    handler, hfile = str(flow.get("handler") or ""), str(flow.get("handler_file") or "")
    handler_qn = plan.symbols.qn(handler, hfile)
    routes = plan.rel(handler_qn, ["HANDLES"], inbound=False)
    routers = [s.get("cbm_qualified_name") for s in plan.symbols.in_file(hfile)
               if s.get("kind") == "variable" and s.get("cbm_qualified_name")]
    return {"flow": flow, "handler": handler, "hfile": hfile, "handler_qn": handler_qn, "routes": routes,
            "wave2": [r.target for r in routes] + routers}


def _route_finish(plan: _Planner, s: dict[str, Any]) -> FrameworkBoundaryCandidate | None:
    ctx, flow, handler, hfile = plan.ctx, s["flow"], s["handler"], s["hfile"]
    files = {str(f.get("path")): f for p in ctx.route_index.get("repos", []) or [] if p.get("repo") == ctx.repo
             for f in p.get("files", []) or []}
    decl = next((r for r in (files.get(hfile) or {}).get("route_calls", []) or [] if r.get("handler_hint") == handler), None)
    if decl is None and not s["routes"]:
        return None
    facts: list[StructuralFact] = []
    missing: list[str] = []
    decorators = [d for d, _ in plan.decorators(s["handler_qn"], handler, hfile)]
    facts += [StructuralFact(PROVENANCE_CBM_DECORATOR, f"{handler} is decorated {_first_line(d)} ({how})", hfile)
              for d, how in plan.decorators(s["handler_qn"], handler, hfile)]
    entry = next((d for d in ctx.decorated if d.get("symbol") == handler and d.get("file") == hfile), None)
    if entry and entry.get("route_path"):
        facts.append(StructuralFact(PROVENANCE_CBM_ENTRYPOINT,
                                    f"CBM route metadata: {entry.get('route_method')} {entry.get('route_path')}", hfile))

    # CBM's own route (HANDLES -> Route), compared with Sydes'
    sydes = (str(flow.get("method") or "").upper(), str(flow.get("path") or ""))
    declared_on, declared_http = None, None
    for r in s["routes"]:
        route = _ROUTE_QN.search(r.target)
        cbm = (route.group(1), route.group(2)) if route else ("?", _short(r.target))
        facts.append(StructuralFact(PROVENANCE_CBM_HANDLES, f"CBM: {handler} HANDLES {cbm[0]} {cbm[1]}", hfile))
        if cbm != sydes:
            prefixed = cbm[0] == sydes[0] and sydes[1].endswith(cbm[1].rstrip("/") or "/") and cbm[1] != sydes[1]
            facts.append(StructuralFact(PROVENANCE_CBM_HANDLES,
                                        f"discrepancy: CBM {cbm[0]} {cbm[1]} vs Sydes {sydes[0]} {sydes[1]}"
                                        + (" (Sydes includes a mount/container prefix CBM does not compose)" if prefixed else ""),
                                        hfile))
        for http in plan.rel(r.target, ["HTTP_CALLS"], inbound=True):
            if http.source_file != hfile:
                continue
            callee = str(http.props.get("callee") or "")
            args = [str(a.get("e")) for a in http.props.get("args", []) or [] if isinstance(a, dict) and a.get("e")]
            declared_on = declared_on or (callee.rsplit(".", 1)[0] if "." in callee else None)
            declared_http = declared_http if declared_http is not None else (args[0] if args else None)
            facts.append(StructuralFact(PROVENANCE_CBM_HTTP_CALLS, f"CBM: route declared by {callee}({', '.join(args)})", hfile))
    if not s["routes"] and plan.facts is not None:
        facts.append(StructuralFact(PROVENANCE_CBM_HANDLES, f"CBM has no HANDLES relation for {handler}"
                                    + ("" if plan.supports("HANDLES") else " (no HANDLES anywhere in this graph)"), hfile))

    # the declaration's path expression and container: CBM (HTTP_CALLS, DECORATES) first, Sydes' route index last
    handler_sym = next(iter(plan.symbols.find(handler, hfile)), {})
    container = str(handler_sym.get("parent") or "") or (str(decl.get("receiver") or "") if decl else "")
    receiver = declared_on or container or (str(decl.get("receiver") or "") if decl else "")
    declaration = next((d for d in decorators if _route_argument(d) is not None), None)
    if declaration is not None or declared_http is not None or decl is not None:
        if declared_http is not None:
            expression, provenance, how = declared_http, PROVENANCE_CBM_HTTP_CALLS, "HTTP_CALLS"
        elif declaration is not None:
            expression, provenance, how = _route_argument(declaration), PROVENANCE_CBM_DECORATOR, "decorator"
        else:
            expression, provenance, how = _route_argument(str(decl.get("snippet") or "")), PROVENANCE_SOURCE_FALLBACK, \
                "Sydes route index; CBM has no declaration for it"
        literal = expression is not None and (expression == "" or _STRING_ARG.match(expression) is not None)
        shown = (_STRING_ARG.match(expression).group("value") if literal and expression else "")
        facts.append(StructuralFact(provenance, f"{handler} is registered on `{receiver}` with path "
                                    + (f"'{shown}'" if literal else f"expression `{expression or '?'}`") + f" ({how})",
                                    hfile, decl.get("line") if decl else None))
        if not literal:
            missing.append(f"route path `{expression or '?'}` is not a literal")
        for decorator, _how in plan.decorators(plan.symbols.qn(container, hfile, "class"), container, hfile):
            match = re.match(r"@?\s*[A-Za-z_]\w*\s*\(\s*([^,)]*?)\s*\)\s*$", " ".join(decorator.split()))
            argument = re.sub(r"^(?:value|path)\s*=\s*", "", (match.group(1) if match else "").strip())
            if argument and "." in argument and _STRING_ARG.match(argument) is None:
                facts.append(StructuralFact(PROVENANCE_CBM_DECORATOR, f"{container} is decorated {_first_line(decorator)}", hfile))
                missing.append(f"container prefix `{argument}` is not a literal")

    # the router's mount: CBM USAGE (carrying the import alias), prefix from CBM source
    router_qn = plan.symbols.qn(receiver, hfile, "variable") if receiver else None
    mounted = False
    for use in plan.rel(router_qn, ["USAGE"], inbound=True):
        if use.source_file == hfile:  # its own routes' decorators
            continue
        alias = str(use.props.get("callee") or receiver)
        facts.append(StructuralFact(PROVENANCE_CBM_USAGE, f"CBM: `{receiver}` is used as `{alias}` in {use.source_file}",
                                    use.source_file))
        mount = _mount(plan, alias, use.source_file, files)
        if mount is not None:
            mounted = True
            text, line, symbolic, provenance = mount
            facts.append(StructuralFact(provenance, f"`{alias}` is mounted {text}", use.source_file, line))
            if symbolic:
                missing.append(f"mount prefix `{symbolic}` is not a literal; the route as shown omits it")
    if not mounted and receiver:
        for path, item in files.items():
            aliases = {receiver}
            for imp in plan.symbols.imports_of(path):
                # CBM import records: `from m import router as r` is source="router", local="r"
                if imp.get("resolved_file") == hfile and receiver in (imp.get("source"), imp.get("imported")):
                    aliases.add(str(imp.get("local") or ""))
            for m in item.get("mount_calls", []) or []:
                if m.get("child") in aliases:
                    text, symbolic = _prefix_text(str(m.get("snippet") or ""), m.get("prefix"))
                    facts.append(StructuralFact(PROVENANCE_SOURCE_FALLBACK, f"`{m.get('child')}` is mounted {text} "
                                                "(Sydes route index; CBM has no USAGE of the router)", path, m.get("line")))
                    if symbolic:
                        missing.append(f"mount prefix `{symbolic}` is not a literal; the route as shown omits it")
    return FrameworkBoundaryCandidate(
        category=CATEGORY_ROUTE_REGISTRATION, source_symbol=handler, via=f"{flow.get('method')} {flow.get('path')}",
        targets=[handler], status=STATUS_UNRESOLVED if missing else STATUS_RESOLVED, facts=facts, missing=missing,
        resolved_by=None if missing else "route composition (literal path and prefixes)")


def _prefix_text(snippet: str, prefix: Any) -> tuple[str, str | None]:
    expr = _PREFIX_ARG.search(snippet)
    if expr is not None and _STRING_ARG.match(expr.group(1)) is None:
        return f"with prefix expression `{expr.group(1).strip()}`", expr.group(1).strip()
    if expr is not None:
        return f"with prefix '{_STRING_ARG.match(expr.group(1)).group('value')}'", None
    return (f"with prefix '{prefix}'" if prefix else "without a prefix"), None


def _mount(plan: _Planner, alias: str, path: str, files: dict[str, Any]) -> tuple[str, int | None, str | None, str] | None:
    """Where `alias` is mounted in `path`, and with which prefix. CBM's search excerpt of a
    large symbol (a module) covers only its first match, so a mount further down is asked
    for as the call that takes the alias as its first argument (`(alias`)."""
    found = None
    for pattern in (alias, f"({alias}"):
        found = plan.search(pattern, path)
        for row in (found or {}).get("rows", []):
            for number, text in numbered(row, pattern):
                if alias in text and _MOUNT_CALL.search(text):
                    shown, symbolic = _prefix_text(text, None)
                    return shown, number, symbolic, PROVENANCE_CBM_SOURCE
    why = "CBM source search unavailable" if found is None else "CBM source search found no mount call"
    for m in (files.get(path) or {}).get("mount_calls", []) or []:
        if m.get("child") == alias:
            shown, symbolic = _prefix_text(str(m.get("snippet") or ""), m.get("prefix"))
            return f"{shown} (Sydes route index; {why})", m.get("line"), symbolic, PROVENANCE_SOURCE_FALLBACK
    return None
