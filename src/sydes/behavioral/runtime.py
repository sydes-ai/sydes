"""Runtime evidence for Sydes: the `diffgenome-runtime/1` contract, read and applied.

DiffGenome runs the repository's existing tests against the change and reports what executed.
Sydes depends on that report only (the `runtime` section of a `diffgenome-change/1` artifact),
never on DiffGenome's trace files or internals. Sydes stays the decision-maker; the contract is
evidence it applies in four places:

- reachability: observed call edges join the static call graph (tagged, additive), so Sydes' own
  impact interpreter can follow dynamic dispatch the static graph cannot;
- test mapping: the exact tests (and subtests) that executed each changed function;
- verification gaps: changed functions no test executed, decision outcomes on changed lines
  never observed, stand-ins reached from changed code, as `VerificationGap(source="runtime")`;
- AI recovery: which first-pass gaps observation already answers, and which changed functions
  still need it (those no test executed, which runtime evidence cannot speak to).

Every "not executed" is relative to the contract's test scope. Values, except-handler coverage
and per-line coverage are not in the contract and are never inferred here.
"""

from __future__ import annotations

import functools

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sydes.verify.models import VerificationGap

FORMAT = "diffgenome-runtime/1"
EDGE_SOURCE = "diffgenome:observed-runtime"
_WRAPPERS = ("<locals>", "<lambda>", "<anon>")


def short_name(symbol: str) -> str:
    """`py:pkg.mod.Class.method` -> `Class.method`; `go:pkg.Type.Method` -> `Type.Method`;
    a closure keeps its own name: `Class.method.<locals>.inner` -> `Class.method.inner`."""
    body = symbol.split(":", 1)[-1]
    if ".<locals>." in body:
        outer, _, inner = body.partition(".<locals>.")
        return f"{short_name(outer)}.{inner.split('.<locals>.')[-1]}"
    parts = body.split(".")
    if len(parts) >= 2 and parts[-2][:1].isupper():
        return ".".join(parts[-2:])
    return parts[-1]


@dataclass
class RuntimeEvidence:
    contract: dict[str, Any]

    # -- loading -------------------------------------------------------------------------------

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any] | None) -> RuntimeEvidence | None:
        raw = (artifact or {}).get("runtime") if isinstance(artifact, dict) else None
        if isinstance(raw, dict) and str(raw.get("format", "")).startswith(FORMAT):
            return cls(raw)
        return None

    @classmethod
    def load(cls, artifact_path: str | Path | None) -> RuntimeEvidence | None:
        """None when there is no readable artifact or it carries no runtime contract."""
        if not artifact_path:
            return None
        try:
            doc = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls.from_artifact(doc)

    # -- facts -----------------------------------------------------------------------------------

    @property
    def functions(self) -> list[dict[str, Any]]:
        return list(self.contract.get("changed_functions") or [])

    @property
    def edges(self) -> list[dict[str, Any]]:
        return list(self.contract.get("edges") or [])

    @property
    def test_scope(self) -> str:
        return str((self.contract.get("universe") or {}).get("test_scope") or "the tests DiffGenome ran")

    def executed(self) -> list[dict[str, Any]]:
        return [f for f in self.functions if f.get("executed")]

    def not_executed(self) -> list[dict[str, Any]]:
        return [f for f in self.functions if not f.get("executed")]

    def tests(self) -> list[str]:
        """Every test that executed at least one changed function."""
        return sorted({t for f in self.functions for t in f.get("tests") or []})

    def find(self, name: str, file: str | None = None) -> dict[str, Any] | None:
        """The changed function a Sydes symbol name refers to (short or qualified), or None."""
        want = name.split(".")[-1]
        hits = [
            f for f in self.functions
            if (f["symbol"].split(":", 1)[-1] == name or short_name(f["symbol"]) == name
                or f["symbol"].split(".")[-1] == want)
            and (file is None or f.get("file") == file)
        ]
        if len(hits) > 1:
            exact = [f for f in hits if short_name(f["symbol"]) == name or f["symbol"].endswith("." + name)]
            hits = exact or hits
        return hits[0] if len(hits) == 1 else None

    def entry_roots(self, symbol: str) -> list[dict[str, Any]]:
        """The topmost non-test application frames on observed chains into `symbol`: where real
        execution entered the code (a request handler, task, command, dispatcher), as observed.
        Test frames are stimuli, not entrypoints."""
        callers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in self.edges:
            callers[e["callee"]["symbol"]].append(e["caller"])
        roots: dict[str, dict[str, Any]] = {}
        seen = {symbol}
        frontier = [symbol]
        while frontier:
            nxt = []
            for s in frontier:
                app_callers = [c for c in callers.get(s, []) if c.get("origin") != "test"]
                entered_from_tests = any(c.get("origin") == "test" for c in callers.get(s, []))
                # an application frame entered from outside application code (no application
                # caller, or a test calling it directly) is where execution entered; the changed
                # function itself never counts as its own entry
                if s != symbol and (not app_callers or entered_from_tests):
                    roots[s] = self._loc_of(s)
                for c in app_callers:
                    if c["symbol"] not in seen:
                        seen.add(c["symbol"])
                        nxt.append(c["symbol"])
            frontier = nxt
        return list(roots.values())

    def observed_paths(self, limit: int = 3, max_len: int = 6) -> list[list[dict[str, Any]]]:
        """Representative observed execution paths into the changed code, for people: from an
        entry root (see `entry_roots`) along application frames to the deepest changed function
        it reached. One path per entry root, the roots reaching the most changed functions first.
        Each step is `{"name", "changed"}`; a long path keeps its ends around an ellipsis step."""
        app = defaultdict(list)
        for e in self.edges:
            if e["caller"].get("origin") != "test" and e["callee"].get("origin") != "test":
                app[e["caller"]["symbol"]].append(e["callee"]["symbol"])
        changed = {f["symbol"] for f in self.functions if f.get("executed")}
        roots: dict[str, int] = defaultdict(int)
        for sym in changed:
            for r in self.entry_roots(sym):
                roots[r["symbol"]] += 1
        paths: list[list[dict[str, Any]]] = []
        for root, _n in sorted(roots.items(), key=lambda kv: (-kv[1], kv[0])):
            prev: dict[str, str | None] = {root: None}
            order = [root]
            for node in order:  # breadth-first: shortest observed chain to each frame
                for nxt in app.get(node, []):
                    if nxt not in prev:
                        prev[nxt] = node
                        order.append(nxt)
            targets = [n for n in order if n in changed and n != root]
            if not targets:
                continue
            chain: list[str] = []
            node: str | None = targets[-1]
            while node is not None:
                chain.append(node)
                node = prev[node]
            chain.reverse()
            steps = [{"name": self.display_name(s), "changed": s in changed} for s in chain]
            if len(steps) > max_len:
                steps = [*steps[:2], {"name": "…", "changed": False}, *steps[-(max_len - 3):]]
            if any(p[len(p) - len(steps):] == steps for p in paths if len(p) >= len(steps)):
                continue  # the same chain, or the tail of one already shown
            paths.append(steps)
            if len(paths) >= limit:
                break
        return paths

    def _loc_of(self, symbol: str) -> dict[str, Any]:
        for e in self.edges:
            for end in (e["caller"], e["callee"]):
                if end["symbol"] == symbol:
                    return end
        return {"symbol": symbol}

    # -- 1. reachability -------------------------------------------------------------------------

    def observed_call_edges(
        self, symbol_index: dict[str, Any], repo: str, existing: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Contract edges between application symbols the code-intelligence index resolves
        (same file, same name, enclosing definition lines), minus edges it already has."""
        resolver = _Resolver(symbol_index, repo)
        static = {(e.get("caller_qualified_name"), e.get("callee_qualified_name")) for e in existing}
        out: list[dict[str, Any]] = []
        stats = {"observed_pairs": 0, "already_static": 0, "unresolved_ends": 0}
        seen: set[tuple[str, str]] = set()
        for e in self.edges:
            if e["caller"].get("origin") == "test":
                continue
            caller, callee = resolver.resolve(e["caller"]), resolver.resolve(e["callee"])
            if caller is None or callee is None:
                stats["unresolved_ends"] += 1
                continue
            key = (caller["qualified"], callee["qualified"])
            if key[0] == key[1] or key in seen:
                continue
            seen.add(key)
            stats["observed_pairs"] += 1
            if key in static:
                stats["already_static"] += 1
                continue
            tests = e.get("tests") or []
            out.append({
                "repo": repo,
                "caller_file": caller["file"], "caller_symbol": caller["name"],
                "caller_qualified_name": key[0], "caller_line": caller["line"],
                "callee_file": callee["file"], "callee_symbol": callee["name"],
                "callee_qualified_name": key[1], "callee_line": callee["line"],
                "source": EDGE_SOURCE,
                "evidence": (
                    f"observed at runtime in {e.get('tests_total', len(tests))} existing test(s)"
                    + (f", e.g. {tests[0]}" if tests else "")
                ),
            })
        stats["added"] = len(out)
        return out, stats

    def candidate_files(self) -> set[str]:
        """Files of application frames on observed chains into the changed code."""
        return {
            end["file"]
            for e in self.edges
            for end in (e["caller"], e["callee"])
            if end.get("file") and end.get("origin") != "test"
        }

    # -- 3. verification gaps --------------------------------------------------------------------

    def verification_gaps(self) -> list[VerificationGap]:
        out: list[VerificationGap] = []
        for i, g in enumerate(self.contract.get("gaps") or []):
            name = self.display_name(str(g.get("symbol") or ""))
            where = f"{g.get('file')}:{g.get('line')}" if g.get("file") else name
            kind = g.get("kind")
            if kind == "function_not_executed":
                behavior = f"{name} ({where}) changed, but no existing test executed it"
            elif kind == "branch_outcome_not_observed":
                behavior = (
                    f"{name}: the changed condition `{g.get('predicate')}` ({where}) was never "
                    f"observed {g.get('outcome')} in any existing test"
                )
            elif kind == "branch_not_evaluated":
                behavior = (
                    f"{name}: the changed condition `{g.get('predicate')}` ({where}) was never "
                    "evaluated by any existing test, although the function ran"
                )
            elif kind == "stand_in_reached":
                behavior = (
                    f"{name} ({where}) reached `{short_name(str(g.get('target')))}` only through a "
                    "stand-in (mock/fake/eager) in existing tests: the real dependency was not exercised"
                )
            else:
                continue
            out.append(VerificationGap(
                id=f"runtime:{kind}:{i}",
                behavior=behavior,
                why=f"Observed by running existing tests ({self.test_scope}); "
                "absence is relative to those tests.",
                source="runtime",
            ))
        return out

    # -- 4. AI recovery --------------------------------------------------------------------------

    def unresolved_needing_recovery(self, unresolved_names: list[str]) -> list[str]:
        """Of the changed symbols Sydes could not connect to an entrypoint, those runtime
        evidence cannot speak to: changed functions no test executed, or executed with no
        observed application entry root. Names that are not changed functions in the contract
        (constants, classes, test code) are kept: nothing observed answers them."""
        out: list[str] = []
        for name in unresolved_names:
            f = self.find(name)
            if f is None or not f.get("executed") or not self.entry_roots(f["symbol"]):
                out.append(name)
        return out

    # -- prompt enrichment (opt-in) ---------------------------------------------------------------

    def compact_evidence(self, max_functions: int = 24) -> str:
        u = self.contract.get("universe") or {}
        ex, ne = self.executed(), self.not_executed()
        lines = [
            "DIFFGENOME RUNTIME EVIDENCE (existing tests run against the head; "
            f"scope: {self.test_scope}; {u.get('executions', 0)} execution(s)). "
            "Facts about what executed; not a verdict; execution is not assertion.",
            f"Changed functions executed: {len(ex)} of {len(self.functions)}.",
        ]
        if ne:
            lines.append("Changed functions NO test executed: " + ", ".join(short_name(f["symbol"]) for f in ne))
        lines.append("")
        lines.append("Per changed function (tests, exits, observed entry roots, changed-line conditions):")
        for f in ex[:max_functions]:
            roots = ", ".join(short_name(r["symbol"]) for r in self.entry_roots(f["symbol"])[:3]) or "test only"
            exits = ", ".join(f"{k} {v}" for k, v in (f.get("exits") or {}).items())
            lines.append(f"- {short_name(f['symbol'])} ({f.get('file')}:{f.get('line')}): "
                         f"{f.get('tests_total', 0)} test(s); exits {exits}; entered via {roots}")
            for s in f.get("changed_sites") or []:
                lines.append(f"    `{s['predicate']}` (line {s['line']}): true {s['true']}, false {s['false']}")
            if f.get("stand_ins"):
                lines.append("    stand-ins reached: " + ", ".join(short_name(t) for t in f["stand_ins"]))
        gaps = self.verification_gaps()
        if gaps:
            lines += ["", "Not exercised by existing tests:"] + [f"- {g.behavior}" for g in gaps[:20]]
        lines += ["", "Not reported by this evidence: " + "; ".join(self.contract.get("not_reported") or [])]
        return "\n".join(lines) + "\n"

    # -- summary for the result/report -----------------------------------------------------------

    @functools.cached_property
    def _short_collisions(self) -> set[str]:
        """Short names that more than one observed symbol shares (`main.create_order` and
        `service.create_order` are both `create_order`)."""
        owners: dict[str, set[str]] = defaultdict(set)
        symbols = {f["symbol"] for f in self.functions}
        symbols |= {end["symbol"] for e in self.edges for end in (e["caller"], e["callee"])}
        for sym in symbols:
            owners[short_name(sym)].add(sym)
        return {name for name, syms in owners.items() if len(syms) > 1}

    def display_name(self, symbol: str) -> str:
        """`short_name`, with the enclosing module added when the short name alone is shared by
        another observed symbol."""
        name = short_name(symbol)
        if name not in self._short_collisions:
            return name
        body = symbol.split(":", 1)[-1].split(".<locals>")[0]
        prefix = body[: len(body) - len(name)].rstrip(".").split(".")[-1]
        return f"{prefix}.{name}" if prefix else name

    def summary(self) -> dict[str, Any]:
        """The subset Sydes keeps on its result for reporting."""
        return {
            "format": self.contract.get("format"),
            "test_scope": self.test_scope,
            "executions": (self.contract.get("universe") or {}).get("executions", 0),
            "functions": [
                {
                    "symbol": f["symbol"], "name": self.display_name(f["symbol"]), "file": f.get("file"),
                    "line": f.get("line"), "executed": bool(f.get("executed")),
                    "ran_at_import": bool(f.get("ran_at_import")),
                    "tests": list(f.get("tests") or [])[:50], "tests_total": f.get("tests_total", 0),
                    "exits": dict(f.get("exits") or {}),
                    "entry_roots": [self.display_name(r["symbol"]) for r in self.entry_roots(f["symbol"])][:5],
                    "changed_sites": list(f.get("changed_sites") or []),
                    "stand_ins": dict(f.get("stand_ins") or {}),
                }
                for f in self.functions
            ],
            "paths": self.observed_paths(),
            "tests_exercised": len({t for f in self.functions for t in f.get("tests") or []}),
            "gaps": [g.model_dump() for g in self.verification_gaps()],
            "not_reported": list(self.contract.get("not_reported") or []),
        }


class _Resolver:
    """A contract location -> the code-intelligence symbol for the same definition."""

    def __init__(self, symbol_index: dict[str, Any], repo: str) -> None:
        self.by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in symbol_index.get("repos", []) or []:
            if r.get("repo") != repo:
                continue
            for item in r.get("files", []) or []:
                for s in item.get("symbols", []) or []:
                    if s.get("cbm_qualified_name") and s.get("name"):
                        self.by_key[(str(item.get("path")), str(s["name"]))].append(s)
        self.cache: dict[str, dict[str, Any] | None] = {}

    def resolve(self, end: dict[str, Any]) -> dict[str, Any] | None:
        symbol = str(end.get("symbol") or "")
        if symbol in self.cache:
            return self.cache[symbol]
        out = None
        body = symbol.split(":", 1)[-1]
        file, line = end.get("file"), end.get("line")
        if file and not any(w in body for w in _WRAPPERS):
            name = body.rsplit(".", 1)[-1]
            cands = self.by_key.get((file, name), [])
            if line is not None:
                inside = [
                    s for s in cands
                    if (s.get("start_line") or 0) - 3 <= line <= (s.get("end_line") or s.get("start_line") or 0)
                ]
                cands = inside or cands
            if len(cands) == 1:
                s = cands[0]
                out = {"qualified": s["cbm_qualified_name"], "name": name, "file": file,
                       "line": s.get("start_line")}
        self.cache[symbol] = out
        return out
