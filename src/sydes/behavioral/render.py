"""Render behavioral evidence for the terminal report and the PR comment.

Both renderers show the same thing: what was observed to execute, what was
reconstructed and at what grade, what is only possible according to static
analysis, and where knowledge stops. Tests and probes are evidence lines, not
graph branches. Join attempts, corpora and other DiffGenome internals never
appear; the artifact's own bounded lists are already the ceiling.
"""

from __future__ import annotations

from typing import Any

from collections import defaultdict

from sydes.behavioral.models import (
    COMPOSED_ARG_SHAPE,
    COMPOSED_STATE,
    COMPOSED_SYMBOL,
    COMPOSED_VALUE,
    EXTERNAL,
    GAP,
    OBSERVED_RUNTIME,
    STATIC_ONLY,
    STATUS_AVAILABLE,
    STATUS_NOT_REQUESTED,
    UNRESOLVED,
    BehavioralEdge,
    BehavioralEvidence,
)

_GRADE_LABEL = {
    COMPOSED_STATE: "STATE",
    COMPOSED_VALUE: "VALUE",
    COMPOSED_ARG_SHAPE: "ARG_SHAPE",
    COMPOSED_SYMBOL: "SYMBOL",
}
_MAX_TESTS_SHOWN = 4
_MAX_DEPTH = 4


def short(symbol: str) -> str:
    """`go:api.Server.createTransfer` → `api.Server.createTransfer`; a stand-in leaf
    (`stand-in:go:api.fakeDB.QueryRow`) loses both prefixes."""
    name = symbol
    for _ in range(2):
        head, sep, tail = name.partition(":")
        if sep and tail and " " not in head and "/" not in head:
            name = tail
        else:
            break
    return name


def _changed(ev: BehavioralEvidence) -> set[str]:
    return {n.id for n in ev.nodes if n.changed}


def _children(ev: BehavioralEvidence) -> dict[str, list[BehavioralEdge]]:
    out: dict[str, list[BehavioralEdge]] = defaultdict(list)
    for e in ev.edges:
        if e.evidence_class != STATIC_ONLY:
            out[e.caller].append(e)
    for k in out:
        out[k].sort(key=lambda e: (e.evidence_class != OBSERVED_RUNTIME, e.callee))
    return out


def _parents(ev: BehavioralEvidence) -> dict[str, list[BehavioralEdge]]:
    out: dict[str, list[BehavioralEdge]] = defaultdict(list)
    for e in ev.edges:
        if e.evidence_class in (OBSERVED_RUNTIME, COMPOSED_STATE, COMPOSED_VALUE, COMPOSED_ARG_SHAPE, COMPOSED_SYMBOL):
            out[e.callee].append(e)
    return out


def _edge_glyph(e: BehavioralEdge) -> str:
    if e.evidence_class == OBSERVED_RUNTIME:
        return "→"
    if e.evidence_class in _GRADE_LABEL:
        return "⇢"
    return "→"


def _edge_tag(e: BehavioralEdge) -> str:
    if e.evidence_class == OBSERVED_RUNTIME:
        return "observed" + (" · probe" if e.probe_derived else "") + (" · runtime-only" if e.runtime_only else "")
    if e.evidence_class in _GRADE_LABEL:
        bits = [f"reconstructed · {_GRADE_LABEL[e.evidence_class]}"]
        if e.state == "matched":
            bits.append("state matched")
        if e.ambiguous:
            bits.append("ambiguous")
        if e.probe_derived:
            bits.append("probe")
        return " · ".join(bits)
    return {GAP: "gap: nothing executed this", UNRESOLVED: "unresolved stand-in", EXTERNAL: "external"}.get(
        e.evidence_class, e.evidence_class.lower()
    )


def entry_chains(ev: BehavioralEvidence) -> list[list[str]]:
    """Observed/reconstructed paths that reach a changed symbol, entry first."""
    parents = _parents(ev)
    chains: list[list[str]] = []
    for changed in sorted(_changed(ev)):
        frontier = [[changed]]
        while frontier:
            path = frontier.pop()
            ups = [e for e in parents.get(path[0], []) if e.caller not in path]
            if not ups or len(path) > _MAX_DEPTH:
                chains.append(path)
                continue
            for e in ups:
                frontier.append([e.caller, *path])
    # per changed symbol, prefer a chain that starts at a step of Sydes' own flows, then
    # the longest; drop a chain that is a prefix of another
    anchored = {n.id for n in ev.nodes if n.static_match}

    def rank(c: list[str]) -> tuple[bool, int]:
        return (c[0] in anchored, len(c))

    best: dict[str, list[str]] = {}
    for c in chains:
        if c[-1] not in best or rank(c) > rank(best[c[-1]]):
            best[c[-1]] = c
    kept = [best[k] for k in sorted(best)]
    return [c for c in kept if not any(o != c and o[: len(c)] == c for o in kept)]


def _tree(ev: BehavioralEvidence, root: str, lines: list[str], indent: str, seen: set[str], depth: int) -> None:
    children = _children(ev)
    for e in children.get(root, []):
        tag = _edge_tag(e)
        lines.append(f"{indent}{_edge_glyph(e)} {short(e.callee)}    [{tag}]")
        if e.callee in seen or depth >= _MAX_DEPTH or e.evidence_class not in (OBSERVED_RUNTIME, *(_GRADE_LABEL)):
            continue
        seen.add(e.callee)
        _tree(ev, e.callee, lines, indent + "  ", seen, depth + 1)


def render_lines(ev: BehavioralEvidence, *, markdown: bool = False) -> list[str]:
    """The section body, shared by both renderers (markdown only changes the headers)."""
    h = (lambda t: f"**{t}**") if markdown else (lambda t: t)
    lines: list[str] = []
    if ev.status == STATUS_NOT_REQUESTED:
        return lines
    if ev.status != STATUS_AVAILABLE:
        lines.append(f"Behavioral execution evidence unavailable: {ev.reason or 'unknown reason'}.")
        lines.append("Structural analysis above stands on its own; nothing here is evidence of no impact.")
        return lines

    changed = _changed(ev)
    chains = entry_chains(ev)
    lines.append(h("Reaches the change (→ observed, ⇢ reconstructed)"))
    if not chains:
        lines.append("  (no existing execution reaches the changed symbols)")
    hop = {(e.caller, e.callee): e for e in ev.edges}
    for chain in chains:
        for i, sym in enumerate(chain):
            mark = "  [changed]" if sym in changed else ""
            if i == 0:
                lines.append(f"  {short(sym)}{mark}")
                continue
            e = hop.get((chain[i - 1], sym))
            glyph = _edge_glyph(e) if e else "→"
            tag = f"    [{_GRADE_LABEL[e.evidence_class]}]" if e and e.evidence_class in _GRADE_LABEL else ""
            lines.append(f"  {'  ' * i}{glyph} {short(sym)}{mark}{tag}")
    lines.append("")
    lines.append(h("Continues from the change"))
    seen: set[str] = set(changed)
    for sym in sorted(changed):
        lines.append(f"  {short(sym)}")
        _tree(ev, sym, lines, "    ", seen, 1)
    if ev.static_only_steps:
        lines.append("")
        lines.append(h("Possible only (static analysis; not executed by any test)"))
        for st in ev.static_only_steps[:8]:
            where = f"  ({st.file})" if st.file else ""
            lines.append(f"  {st.symbol}{where}")
    boundaries = [e for e in ev.edges if e.evidence_class in (GAP, UNRESOLVED, EXTERNAL)]
    if boundaries or ev.changed_symbols_without_entry:
        lines.append("")
        lines.append(h("Where evidence stops"))
        for sym in ev.changed_symbols_without_entry:
            lines.append(f"  {short(sym)}    [no executed path from an affected flow]")
        for e in boundaries:
            lines.append(f"  {short(e.caller)} → {short(e.callee)}    [{_edge_tag(e)}]")
    if ev.runtime_evidence:
        lines.append("")
        lines.extend(_runtime_lines(ev.runtime_evidence, h))
    if ev.genome:
        lines.append("")
        lines.extend(_genome_lines(ev.genome, h))
    lines.append("")
    lines.append(h("How we know"))
    tests = ev.tests_on_behavioral_path
    acc = (ev.genome or {}).get("accounting") or {}
    if tests:
        shown = ", ".join(tests[:_MAX_TESTS_SHOWN]) + (f" … +{len(tests) - _MAX_TESTS_SHOWN} more" if len(tests) > _MAX_TESTS_SHOWN else "")
        listed = " listed in the behavioral artifact" if acc else ""
        lines.append(f"  {len(tests)} existing test(s){listed} executed the changed code: {shown}")
    else:
        lines.append("  no existing test executed the changed code")
    if acc:
        lines.append(
            f"  checked rules: established over {acc.get('relevant_executions_checked', '?')} relevant "
            f"execution(s); {acc.get('scenario_predictions_checked', '?')} per-test prediction(s) checked"
        )
    if ev.tests_failed_on_path:
        lines.append(
            f"  {len(ev.tests_failed_on_path)} test(s) executed the changed code and FAILED in the "
            "isolated run: " + ", ".join(ev.tests_failed_on_path[:_MAX_TESTS_SHOWN])
        )
    if ev.changed_symbols_never_executed:
        lines.append("  never executed: " + ", ".join(short(s) for s in ev.changed_symbols_never_executed))
    if ev.changed_symbols_unmatched:
        lines.append("  changed in the diff but not seen by the behavioral run: " + ", ".join(ev.changed_symbols_unmatched))
    c = ev.counts
    lines.append(
        f"  edges: observed {c.get('observed_runtime', 0)} · reconstructed "
        f"{c.get('composed_state', 0) + c.get('composed_value', 0) + c.get('composed_arg_shape', 0) + c.get('composed_symbol', 0)}"
        f" (STATE {c.get('composed_state', 0)}, VALUE {c.get('composed_value', 0)}, ARG_SHAPE {c.get('composed_arg_shape', 0)},"
        f" SYMBOL {c.get('composed_symbol', 0)})"
        f" · static steps executed at runtime {c.get('static_steps_executed', 0)}/{c.get('static_steps', 0)}"
        f" · static-only {c.get('static_only', 0)}"
    )
    if ev.seams:
        for s in ev.seams[:3]:
            reasons = "; ".join(f"{n}× {r}" for r, n in sorted(s.rejection_reasons.items(), key=lambda x: -x[1])[:2])
            lines.append(f"  seam {short(s.caller)} ⇢ {short(s.target)}: {s.accepted_candidates} continuation(s) accepted, {s.rejected_candidates} rejected ({reasons})")
    p = ev.probes
    if p.requested or p.attempts:
        lines.append(f"  probes: {p.accepted} accepted of {p.attempts} attempt(s), budget {p.requested}, LLM calls {p.llm_calls}, writer {p.writer}")
    else:
        lines.append("  probes: none (existing tests only)")
    lines.append(f"  source: DiffGenome {ev.artifact_format} · runtime {ev.runtime} · revision {ev.revision}")
    return lines


_MAX_RUNTIME_FUNCTIONS = 14
_MAX_RUNTIME_GAPS = 12


def _runtime_lines(rt: dict[str, Any], h: Any) -> list[str]:
    """What executed, per changed function, and what no existing test exercised."""
    fns = rt.get("functions") or []
    executed = [f for f in fns if f.get("executed")]
    lines = [h(
        f"Runtime evidence (existing tests run against the change; scope: {rt.get('test_scope')})"
    )]
    lines.append(f"  {len(executed)} of {len(fns)} changed function(s) executed")
    for f in sorted(fns, key=lambda x: (not x.get("executed"), x.get("name") or ""))[:_MAX_RUNTIME_FUNCTIONS]:
        where = f"{f.get('file')}:{f.get('line')}" if f.get("file") else ""
        if not f.get("executed"):
            lines.append(f"  ✗ {f.get('name')}  {where}  — no existing test executed it")
            continue
        roots = ", ".join(f.get("entry_roots") or []) or "tests only"
        exits = ", ".join(f"{k} {v}" for k, v in (f.get("exits") or {}).items())
        lines.append(f"  ✓ {f.get('name')}  {where}  — {f.get('tests_total', 0)} test(s); entered via {roots}; {exits}")
        tests = f.get("tests") or []
        if tests:
            more = f" … +{f.get('tests_total', len(tests)) - 3}" if f.get("tests_total", len(tests)) > 3 else ""
            lines.append(f"      tests: {', '.join(tests[:3])}{more}")
    if len(fns) > _MAX_RUNTIME_FUNCTIONS:
        lines.append(f"  … +{len(fns) - _MAX_RUNTIME_FUNCTIONS} more changed function(s)")
    gaps = rt.get("gaps") or []
    if gaps:
        lines.append("  Not run by the selected tests (other tests in the suite may run them):")
        for g in gaps[:_MAX_RUNTIME_GAPS]:
            lines.append(f"  - {g.get('behavior')}")
        if len(gaps) > _MAX_RUNTIME_GAPS:
            lines.append(f"  … +{len(gaps) - _MAX_RUNTIME_GAPS} more")
    if rt.get("not_reported"):
        lines.append("  not reported by runtime evidence: " + "; ".join(rt["not_reported"]))
    return lines


_MAX_RULES_SHOWN = 8


def _rule_line(r: dict[str, Any]) -> str:
    where = f"{r.get('file')}:{r.get('line')}" if r.get("file") else (r.get("site") or "")
    src = f" `{r['source']}`" if r.get("source") else ""
    meaning = f" means `{r['meaning']}`" if r.get("meaning") and r.get("meaning") != r.get("source") else ""
    effects = []
    for side, key_o, key_e in (("true", "outcome_true", "when_true"), ("false", "outcome_false", "when_false")):
        text = r.get(key_o) or r.get(key_e)
        if text:
            effects.append(f"{side} → {str(text)[:90]}")
    tail = f"; {'; '.join(effects)}" if effects else ""
    return f"{r.get('entity', '')} {where}{src}{meaning}{tail}"


def _genome_lines(g: dict[str, Any], h: Any) -> list[str]:
    """DiffGenome's checked rules: what the changed code decides, in the model's words, each
    kept only because the checker verified it (or found it consistent) against the traces."""
    lines = [h("Checked behavioral rules (DiffGenome genome: meanings proposed by a model, statuses checked against executed tests)")]
    rules = sorted(
        g.get("decision_rules") or [],
        key=lambda r: (not r.get("at_changed_line"), r.get("status") != "verified"),
    )
    for r in rules[:_MAX_RULES_SHOWN]:
        label = "✔ verified  " if r.get("status") == "verified" else "~ consistent"
        mark = "  [changed line]" if r.get("at_changed_line") else ""
        lines.append(f"  {label} {_rule_line(r)}{mark}")
    if len(rules) > _MAX_RULES_SHOWN:
        lines.append(f"  … +{len(rules) - _MAX_RULES_SHOWN} more rule(s) (see the artifact)")
    for i in g.get("identities") or []:
        sides = " = ".join(i.get("same_value_at") or [])
        lines.append(f"  = same value ({i.get('status')}): {i.get('name')}: {sides}")
    for lit in g.get("literals") or []:
        lines.append(f"  = literal: {lit.get('name')}: {lit.get('at')} == {lit.get('equals')!r} (written at {lit.get('written_at')})")
    c = g.get("consistency") or {}
    if c:
        lines.append(
            f"  predicted from these rules: {c.get('consistent', 0)}/{c.get('scenarios', 0)} per-test prediction(s) exactly · "
            f"{c.get('contradicted', 0)} contradicted · {c.get('indeterminate', 0)} indeterminate"
        )
    st = g.get("statuses") or {}
    dropped = int(st.get("hypothesis", 0)) + int(st.get("rejected", 0)) + int(st.get("contradicted_not_exported", 0))
    if dropped:
        lines.append(f"  not shown: {dropped} claim(s) the traces did not support (hypothesis, rejected or contradicted)")
    for u in (g.get("unknowns") or [])[:2]:
        lines.append(f"  not modelled: {str(u.get('what', ''))[:120]}")
    return lines


def render_terminal(ev: BehavioralEvidence) -> list[str]:
    return render_lines(ev, markdown=False)


def render_markdown(ev: BehavioralEvidence) -> str:
    body = render_lines(ev, markdown=True)
    if not body:
        return ""
    out = ["### Behavioral effect", ""]
    block: list[str] = []

    def flush() -> None:
        if block:
            out.extend(["```", *block, "```", ""])
            block.clear()

    for line in body:
        if line.startswith("**"):
            flush()
            out.append(line)
            out.append("")
        elif line == "":
            continue
        else:
            block.append(line)
    flush()
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """`python -m sydes.behavioral.render sydes-result.json`: print the PR-comment
    section for a result's behavioral evidence (empty when none was requested), so a
    renderer outside this repository can append it without reading the schema."""
    import json
    import sys
    from pathlib import Path

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m sydes.behavioral.render <sydes-result.json>", file=sys.stderr)
        return 2
    doc = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    raw = doc.get("behavioral") if isinstance(doc, dict) else None
    if not raw:
        return 0
    print(render_markdown(BehavioralEvidence.model_validate(raw)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
