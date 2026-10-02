"""Merge policy: static/structural evidence × DiffGenome runtime evidence.

    STATIC (CBM, route/flow traces)  what may be connected
    OBSERVED (DiffGenome)            what actually executed, in some isolated test
    COMPOSED (DiffGenome)            what can be reconstructed across a stand-in seam,
                                     graded SYMBOL < ARG_SHAPE < VALUE < STATE
    GAP / UNRESOLVED / EXTERNAL      where neither side has enough

Nothing is flattened. A static hop DiffGenome never executed stays STATIC_ONLY. A
runtime edge the static trace does not have is kept and marked runtime_only. A
composed edge keeps its grade as its class. Matching between the two worlds is by
file plus symbol name only — no framework knowledge, no name heuristics beyond
"same file, same trailing qualified name".
"""

from __future__ import annotations

from typing import Any

from sydes.behavioral.models import (
    COMPOSED_BY_GRADE,
    EXTERNAL,
    GAP,
    OBSERVED_RUNTIME,
    STATIC_ONLY,
    STATUS_AVAILABLE,
    UNRESOLVED,
    BehavioralEdge,
    BehavioralEvidence,
    BehavioralNode,
    BehavioralProbeSummary,
    BehavioralSeam,
    StaticOnlyStep,
)

_BOUNDARY_CLASS = {
    "gap": GAP,
    "declaration": GAP,
    "unresolved": UNRESOLVED,
    "external": EXTERNAL,
    "os": EXTERNAL,
}


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1].split("@")[0]


def _norm_file(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return path.lstrip("./")


def static_steps(result: Any) -> list[dict[str, Any]]:
    """The distinct steps of every affected flow's recorded trace, in order, plus its
    sinks. Sydes records a flow as a linear statement trace, not a call tree, so two
    consecutive steps are not necessarily caller and callee; the merge therefore matches
    at the step (symbol) level and never turns adjacency into an edge. When one symbol
    spans several consecutive steps the non-endpoint occurrence keeps the definition's
    file (the endpoint step carries the router's file)."""
    steps: list[dict[str, Any]] = []
    for flow in getattr(result, "affected_flows", []) or []:
        last: str | None = None
        for step in flow.steps or []:
            if not isinstance(step, dict):
                continue
            ident = step.get("symbol") or step.get("name")
            if not ident:
                continue
            if ident == last:
                if steps and steps[-1].get("kind") == "endpoint" and step.get("kind") != "endpoint":
                    steps[-1] = step
                continue
            last = ident
            steps.append(step)
        for sink in flow.sinks or []:
            if isinstance(sink, dict):
                steps.append({"kind": "sink", "name": sink.get("name") or sink.get("kind"), "file": sink.get("file")})
    return steps


def _step_identity(step: dict[str, Any]) -> tuple[str | None, str]:
    return _norm_file(step.get("file")), str(step.get("symbol") or step.get("name") or "")


def match_node(step: dict[str, Any], nodes: list[dict[str, Any]]) -> str | None:
    """A static step matches a DiffGenome node when the files agree and the step's
    symbol is the node's name or its trailing component."""
    file, ident = _step_identity(step)
    if not ident:
        return None
    short = _short(ident)
    candidates = []
    for node in nodes:
        nfile = _norm_file(node.get("file"))
        name = str(node.get("name") or "")
        if file and nfile and file != nfile:
            continue
        if name == ident or name.endswith("." + ident) or _short(name) == short:
            candidates.append(node["id"])
    if len(candidates) == 1:
        return candidates[0]
    if candidates and file:
        return sorted(candidates)[0]
    return None


def _genome_summary(raw: Any) -> dict[str, Any] | None:
    """Only a summary in a format this reader knows; anything else is ignored, never guessed."""
    if isinstance(raw, dict) and str(raw.get("format", "")).startswith("diffgenome-genome-summary/1"):
        return raw
    return None


def merge(result: Any, artifact: dict[str, Any], *, artifact_path: str | None = None) -> BehavioralEvidence:
    nodes_raw = list(artifact.get("nodes") or [])
    node_ids = {n["id"] for n in nodes_raw}
    ev = BehavioralEvidence(
        status=STATUS_AVAILABLE,
        artifact_format=artifact.get("format"),
        artifact_path=artifact_path,
        runtime=(artifact.get("repository") or {}).get("runtime"),
        revision=(artifact.get("repository") or {}).get("revision"),
        changed_symbols=list((artifact.get("change") or {}).get("symbols") or []),
        changed_symbols_never_executed=list((artifact.get("change") or {}).get("symbols_never_executed") or []),
        facts=dict(artifact.get("facts") or {}),
        notes=list(artifact.get("notes") or []),
        genome=_genome_summary(artifact.get("genome")),
    )

    # --- match static steps to runtime nodes (node level; see static_steps)
    steps = static_steps(result)
    static_match: dict[str, str] = {}  # node id -> static identity
    static_only: list[dict[str, Any]] = []
    for step in steps:
        ident = match_node(step, nodes_raw)
        if ident:
            static_match.setdefault(ident, _step_identity(step)[1])
        else:
            static_only.append(step)
    matched_ids = set(static_match)

    for n in nodes_raw:
        ev.nodes.append(
            BehavioralNode(
                id=n["id"], name=n.get("name", n["id"]), file=n.get("file"), line=n.get("line"),
                kind=n.get("kind", "callable"), changed=bool(n.get("changed")),
                executed_by=int(n.get("executed_by") or 0), static_match=static_match.get(n["id"]),
            )
        )

    # --- changed symbols the diff has but DiffGenome did not see (never silently dropped);
    # symbols in test files are evidence, not behavior, and are not expected as nodes
    test_files = {
        f.path for f in getattr(result.change, "files", []) if "test" in str(getattr(f, "role", "") or "")
    }
    diff_symbols = {
        getattr(s, "qualified_name", None) or s.name
        for s in getattr(result.change, "symbols", [])
        if s.file not in test_files
    }
    runtime_names = {n.get("name") for n in nodes_raw if n.get("changed")}
    ev.changed_symbols_unmatched = sorted(
        s for s in diff_symbols
        if not any(rn == s or str(rn).endswith("." + s) or _short(str(rn)) == _short(s) for rn in runtime_names)
    )

    # --- runtime edges, with their class and static corroboration
    for e in artifact.get("edges") or []:
        if e.get("evidence") == "observed":
            cls = OBSERVED_RUNTIME
        else:
            cls = COMPOSED_BY_GRADE.get(str(e.get("join")), STATIC_ONLY)
        corroborated = e["caller"] in matched_ids and e["callee"] in matched_ids
        ev.edges.append(
            BehavioralEdge(
                caller=e["caller"], callee=e["callee"], evidence_class=cls,
                runtime_evidence=e.get("evidence"), join=e.get("join"), state=e.get("state"),
                exit=e.get("exit"), probe_derived=bool(e.get("probe_derived")),
                ambiguous=bool(e.get("ambiguous")), static_corroborated=corroborated,
                runtime_only=bool(steps) and not corroborated,
                executions=list(e.get("executions") or []), probes=list(e.get("probes") or []),
                rules=list(e.get("rules") or []),
            )
        )
    for b in artifact.get("boundaries") or []:
        ev.edges.append(
            BehavioralEdge(
                caller=b["caller"], callee=b["target"],
                evidence_class=_BOUNDARY_CLASS.get(str(b.get("kind")), UNRESOLVED),
                runtime_evidence=b.get("kind"), rules=list(b.get("rules") or []),
                executions=list(b.get("executions") or []),
            )
        )
    # --- static steps with no runtime counterpart: possible, never proven here
    seen: set[str] = set()
    for step in static_only:
        ident = _step_identity(step)[1]
        if not ident or ident in seen:
            continue
        seen.add(ident)
        ev.static_only_steps.append(
            StaticOnlyStep(symbol=ident, file=_norm_file(step.get("file")), kind=step.get("kind"))
        )

    for s in artifact.get("ambiguous_seams") or []:
        ev.seams.append(BehavioralSeam(**{k: s[k] for k in ("caller", "target", "accepted_candidates", "rejected_candidates", "rejection_reasons") if k in s}))

    changed_ids = {n["id"] for n in nodes_raw if n.get("changed")}
    tests: set[str] = set()
    for e in artifact.get("edges") or []:
        if e["caller"] in changed_ids or e["callee"] in changed_ids:
            tests.update(x for x in e.get("executions") or [] if "diffgenome_probe" not in x.lower())
    outcomes = {
        str(x["id"]): x.get("outcome")
        for x in artifact.get("executions") or []
        if isinstance(x, dict) and "id" in x
    }
    ev.tests_failed_on_path = sorted(t for t in tests if outcomes.get(t) == "failed")
    # only passing executions support the change; failing ones are reported on their own
    ev.tests_on_behavioral_path = sorted(t for t in tests if outcomes.get(t) != "failed")
    ev.test_files = {
        str(x["id"]): str(x["file"])
        for x in artifact.get("executions") or []
        if isinstance(x, dict) and x.get("file") and x.get("id") in tests
    }

    probes = artifact.get("probes") or []
    budget = artifact.get("budget") or {}
    ev.probes = BehavioralProbeSummary(
        writer=str(budget.get("writer", "none")).split(":")[0],
        requested=int(budget.get("probes_requested") or 0), attempts=len(probes),
        accepted=sum(1 for p in probes if p.get("verdict") == "accepted"),
        rejected=sum(1 for p in probes if p.get("verdict") != "accepted"),
        llm_calls=int(budget.get("llm_calls") or 0),
    )
    ev.counts = {
        "observed_runtime": len(ev.by_class(OBSERVED_RUNTIME)),
        "composed_state": len(ev.by_class("COMPOSED_STATE")),
        "composed_value": len(ev.by_class("COMPOSED_VALUE")),
        "composed_arg_shape": len(ev.by_class("COMPOSED_ARG_SHAPE")),
        "composed_symbol": len(ev.by_class("COMPOSED_SYMBOL")),
        "static_only": len(ev.static_only_steps),
        "gaps": len(ev.by_class(GAP)),
        "unresolved": len(ev.by_class(UNRESOLVED)),
        "external": len(ev.by_class(EXTERNAL)),
        "static_steps": len({_step_identity(x)[1] for x in steps}),
        "static_steps_executed": len(matched_ids),
        "runtime_only_edges": sum(1 for e in ev.edges if e.runtime_only and e.runtime_evidence in ("observed", "composed")),
        "tests_on_behavioral_path": len(ev.tests_on_behavioral_path),
    }
    return ev
