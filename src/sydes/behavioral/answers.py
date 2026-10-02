"""Which first-pass gaps DiffGenome's executed evidence already answers.

Read by the AI-recovery trigger and by the renderers so that a question the
behavioral run answered is neither re-investigated at model cost nor still
reported as open. Generic: reads only `BehavioralEvidence` fields.
"""

from __future__ import annotations

from sydes.behavioral.models import (
    COMPOSED_ARG_SHAPE,
    COMPOSED_STATE,
    COMPOSED_SYMBOL,
    COMPOSED_VALUE,
    OBSERVED_RUNTIME,
    STATUS_AVAILABLE,
    BehavioralEvidence,
)

_REACHING = (OBSERVED_RUNTIME, COMPOSED_STATE, COMPOSED_VALUE, COMPOSED_ARG_SHAPE, COMPOSED_SYMBOL)


def available(ev: BehavioralEvidence | None) -> bool:
    return ev is not None and ev.status == STATUS_AVAILABLE


def tests_observed(ev: BehavioralEvidence | None) -> list[str]:
    """Existing tests observed executing the changed code (empty when unavailable)."""
    return list(ev.tests_on_behavioral_path) if available(ev) else []


def changed_symbols_reached(ev: BehavioralEvidence | None) -> bool:
    """True iff every changed symbol the diff has was seen by the behavioral run, was
    executed, and is connected by observed or reconstructed calls to a step of one of
    Sydes' own affected flows (a node with `static_match`). Reaching *some* caller is
    not enough: a service method called only by its unit tests is not an entry path."""
    return available(ev) and not symbols_without_entry(ev)


def symbols_without_entry(ev: BehavioralEvidence | None) -> list[str]:
    """Changed symbols for which executed evidence does not connect to any step of the
    static flows (empty when unavailable: nothing is claimed either way)."""
    if not available(ev):
        return []
    assert ev is not None
    changed = [n for n in ev.nodes if n.changed]
    if not changed:
        return []
    anchored = {n.id for n in ev.nodes if n.static_match}
    parents: dict[str, set[str]] = {}
    for e in ev.edges:
        if e.evidence_class in _REACHING and e.caller != e.callee:
            parents.setdefault(e.callee, set()).add(e.caller)
    missing: list[str] = []
    for node in changed:
        if node.executed_by <= 0:
            missing.append(node.id)
            continue
        seen = {node.id}
        frontier = [node.id]
        found = node.id in anchored
        while frontier and not found:
            nxt = []
            for sym in frontier:
                for up in parents.get(sym, ()):
                    if up in anchored:
                        found = True
                        break
                    if up not in seen:
                        seen.add(up)
                        nxt.append(up)
                if found:
                    break
            frontier = nxt
        if not found:
            missing.append(node.id)
    missing.extend(ev.changed_symbols_unmatched)
    return sorted(missing)


def ancestors_of_changed(ev: BehavioralEvidence | None) -> set[str]:
    """Changed symbols plus every symbol with an observed or reconstructed call path down
    to one: the nodes whose presence in a static flow puts that flow on the change's
    executed path."""
    if not available(ev):
        return set()
    assert ev is not None
    parents: dict[str, set[str]] = {}
    for e in ev.edges:
        if e.evidence_class in _REACHING and e.caller != e.callee:
            parents.setdefault(e.callee, set()).add(e.caller)
    out = {n.id for n in ev.nodes if n.changed}
    frontier = list(out)
    while frontier:
        nxt = []
        for sym in frontier:
            for up in parents.get(sym, ()):
                if up not in out:
                    out.add(up)
                    nxt.append(up)
        frontier = nxt
    return out


def runtime_executed_any(ev: BehavioralEvidence | None) -> bool:
    """True iff runtime evidence shows at least one changed function executed by a test."""
    rt = ev.runtime_evidence if available(ev) and ev is not None else None
    return bool(rt) and any(f.get("executed") for f in rt.get("functions") or [])


def runtime_open_symbols(ev: BehavioralEvidence | None, unresolved: list[str]) -> list[str]:
    """Of Sydes' unresolved changed symbols, those runtime evidence does not answer. A symbol
    is answered only when it is a changed function the tests executed AND an observed chain
    entered it through application code (an entry root). Everything else stays open: a
    function no test ran, one reached only from test code, or a symbol that is not a changed
    function in the evidence (a constant, a class)."""
    rt = ev.runtime_evidence if available(ev) and ev is not None else None
    if not rt:
        return list(unresolved)
    functions = rt.get("functions") or []
    open_: list[str] = []
    for name in unresolved:
        want = name.split(".")[-1]
        hits = [f for f in functions if f.get("name") == name or str(f.get("symbol", "")).split(".")[-1] == want]
        if not (len(hits) == 1 and hits[0].get("executed") and hits[0].get("entry_roots")):
            open_.append(name)
    return open_
