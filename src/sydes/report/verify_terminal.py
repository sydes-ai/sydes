"""Terminal rendering for `sydes verify-change`.

A renderer over `ChangeVerificationResult` and nothing more: it reads no
terminal state and holds no analysis logic, so the same model can be rendered
for GitHub or a UI later. Sections are emitted in a fixed order so a compact
GitHub summary can be produced by taking the section headers alone.
"""

from __future__ import annotations

from sydes.verify.models import (
    VERIFICATION_FAILED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    AffectedFlow,
    ChangeVerificationResult,
)

_STATUS_MARK = {
    VERIFICATION_VERIFIED: "✓",
    VERIFICATION_FAILED: "✗",
    VERIFICATION_UNVERIFIED: "?",
    VERIFICATION_UNKNOWN: "-",
}

_KIND_LABEL = {
    "route": "route",
    "handler": "handler",
    "service": "service",
    "repository": "repository",
    "client": "client",
    "database": "database",
    "event": "event",
    "consumer": "consumer",
    "external": "external",
    "function": "function",
}


def _section(lines: list[str], title: str) -> None:
    """Append a section header."""
    lines.append("")
    lines.append(title)
    lines.append("")


def _flow_paths(flow: AffectedFlow) -> list[list[str]]:
    """Linearize a flow's edges into readable root-to-leaf paths."""
    by_id = {node.id: node for node in flow.nodes}
    children: dict[str, list[str]] = {}
    has_parent: set[str] = set()
    for edge in flow.edges:
        if edge.source not in by_id or edge.target not in by_id:
            continue
        children.setdefault(edge.source, []).append(edge.target)
        has_parent.add(edge.target)

    roots = [node.id for node in flow.nodes if node.id not in has_parent]
    if not roots:
        roots = [flow.nodes[0].id] if flow.nodes else []

    paths: list[list[str]] = []

    def walk(node_id: str, trail: list[str]) -> None:
        trail = [*trail, node_id]
        next_ids = children.get(node_id, [])
        if not next_ids or len(trail) > 12:
            paths.append(trail)
            return
        for child in next_ids:
            if child in trail:
                continue
            walk(child, trail)

    for root in roots:
        walk(root, [])
    return paths


def _render_flow(flow: AffectedFlow, lines: list[str], *, verbose: bool) -> None:
    """Render one affected flow as an indented path tree with evidence."""
    by_id = {node.id: node for node in flow.nodes}
    lines.append(f"{flow.entry_label}")
    if flow.reason:
        lines.append(f"  why: {flow.reason}")

    rendered: set[tuple[str, ...]] = set()
    for path in _flow_paths(flow):
        for depth, node_id in enumerate(path):
            branch = tuple(path[: depth + 1])
            if branch in rendered:
                continue
            rendered.add(branch)
            node = by_id.get(node_id)
            if node is None or depth == 0:
                continue
            indent = "  " + "  " * depth
            marker = "  [CHANGED]" if node.changed else ""
            label = _KIND_LABEL.get(node.kind, node.kind)
            lines.append(f"{indent}→ {node.name}   ({label}){marker}")
            if node.file:
                lines.append(f"{indent}    {node.file}")

    if verbose:
        for edge in flow.edges:
            if not edge.evidence:
                continue
            source = by_id.get(edge.source)
            target = by_id.get(edge.target)
            if source is None or target is None:
                continue
            lines.append(
                f"    evidence: {source.name} -{edge.kind}-> {target.name}"
            )
            for item in edge.evidence[:2]:
                where = item.file or ""
                snippet = (item.snippet or "").strip()
                lines.append(f"      {item.label or 'evidence'}: {where}")
                if snippet:
                    lines.append(f"        {snippet[:160]}")
    for note in flow.notes:
        lines.append(f"    note: {note}")
    lines.append("")


def render_verify_change_terminal(
    result: ChangeVerificationResult,
    *,
    verbose: bool = False,
) -> str:
    """Render a detailed, inspectable terminal report for a verification result."""
    lines: list[str] = []
    counts = result.summary.counts

    lines.append("SYDES CHANGE VERIFICATION")
    lines.append("")
    lines.append(f"Repo:    {result.change.repos[0].name if result.change.repos else '-'}")
    lines.append(f"Base:    {result.change.base}")
    if result.change.merge_base:
        lines.append(f"Merge base: {result.change.merge_base[:12]}")
    if result.change.includes_working_tree:
        lines.append("Includes uncommitted working-tree changes: yes")
    lines.append("")
    lines.append(f"Risk:    {result.summary.risk}")
    lines.append(f"Verdict: {result.summary.verdict}")
    if result.summary.headline:
        lines.append("")
        lines.append(result.summary.headline)
    lines.append("")
    lines.append(f"Changed files:         {counts.changed_files}")
    lines.append(f"Changed symbols:       {counts.changed_symbols}")
    lines.append(f"Affected flows:        {counts.affected_flows}")
    lines.append(f"Code findings:         {counts.code_findings}")
    lines.append(f"Existing verification: {counts.existing_verification}")
    lines.append(f"Verification gaps:     {counts.verification_gaps}")
    lines.append(f"Runtime dependencies:  {counts.runtime_dependencies}")
    if result.summary.risk_reasons:
        lines.append("")
        lines.append("Risk drivers:")
        for reason in result.summary.risk_reasons:
            lines.append(f"  - {reason}")

    _section(lines, "CHANGED SURFACE")
    if not result.change.files:
        lines.append(f"  No changes against {result.change.base}.")
    for changed_file in result.change.files:
        role = changed_file.role or "unknown"
        lines.append(
            f"  [{changed_file.change_type}] {changed_file.path}  (+{changed_file.added_lines}/-{changed_file.removed_lines}, {role})"
        )
    if result.change.symbols:
        lines.append("")
        lines.append("  Changed symbols:")
        for symbol in result.change.symbols:
            location = f"{symbol.file}:{symbol.start_line}"
            lines.append(
                f"    {symbol.qualified_name or symbol.name}  ({symbol.kind}, {symbol.change_type})  {location}"
            )

    _section(lines, "CODE FINDINGS")
    if not result.code_findings:
        lines.append("  No code-level findings reported.")
    for finding in result.code_findings:
        location = finding.file or ""
        if finding.line:
            location = f"{location}:{finding.line}"
        lines.append(f"  [{finding.severity}] {finding.title}")
        lines.append(f"        {location}")
        if finding.explanation:
            lines.append("")
            lines.append(f"        {finding.explanation}")
        if finding.impact:
            lines.append("")
            lines.append("        Impact:")
            lines.append(f"        {finding.impact}")
        if finding.suggested_fix:
            lines.append("")
            lines.append("        Suggested fix:")
            lines.append(f"        {finding.suggested_fix}")
        lines.append("")

    _section(lines, "AFFECTED SYSTEM FLOWS")
    if not result.affected_flows:
        lines.append("  No route or event flow resolved to the changed symbols.")
    for flow in result.affected_flows:
        _render_flow(flow, lines, verbose=verbose)

    _section(lines, "EXISTING VERIFICATION")
    lines.append("  (located statically; Sydes did not execute any tests)")
    lines.append("")
    if not result.verification:
        lines.append("  No verification evidence located.")
    for item in result.verification:
        mark = _STATUS_MARK.get(item.status, "-")
        lines.append(f"  {mark} {item.name}")
        if item.file:
            lines.append(f"      {item.file}" + (f":{item.line}" if item.line else ""))
        if item.covers:
            lines.append("      Covers:")
            for entry in item.covers:
                lines.append(f"      - {entry}")
        if item.status == VERIFICATION_UNVERIFIED:
            lines.append("      No applicable verification found")
        if verbose:
            for evidence in item.evidence[:3]:
                lines.append(
                    f"      evidence: {evidence.label or 'ref'} {evidence.snippet or ''}".rstrip()
                )
        lines.append("")

    _section(lines, "POTENTIAL VERIFICATION GAPS")
    if not result.verification_gaps:
        lines.append("  None reported.")
    node_names = {
        node.id: node.name for flow in result.affected_flows for node in flow.nodes
    }
    for gap in result.verification_gaps:
        lines.append(f"  ? {gap.behavior}")
        if gap.why:
            lines.append(f"      why: {gap.why}")
        if gap.related_node_ids:
            related = [node_names.get(node_id, node_id) for node_id in gap.related_node_ids[:3]]
            lines.append(f"      related: {', '.join(related)}")
        lines.append("")

    _section(lines, "RUNTIME REQUIREMENTS")
    lines.append("  To fully exercise the affected flow (Sydes does not provision or mock these):")
    lines.append("")
    if not result.runtime_dependencies:
        lines.append("  None detected.")
    scoped = [item for item in result.runtime_dependencies if item.scope == "affected_flow"]
    other = [item for item in result.runtime_dependencies if item.scope != "affected_flow"]
    for dependency in scoped:
        lines.append(f"  {dependency.name}   ({dependency.kind})")
    if other:
        lines.append("")
        lines.append("  Present in repository, not tied to the affected flow:")
        for dependency in other:
            lines.append(f"    {dependency.name}   ({dependency.kind})")
    if result.runtime_dependencies:
        lines.append("")
        lines.append("  Detected from:")
        for dependency in result.runtime_dependencies:
            for evidence in dependency.detected_from[: 3 if verbose else 1]:
                label = evidence.symbol or evidence.label or "config"
                lines.append(f"  - {label}  ({evidence.file})")

    _section(lines, "CROSS-REPO IMPACT")
    if not result.cross_repo_impacts:
        lines.append("  No related repositories configured.")
    for impact in result.cross_repo_impacts:
        target = impact.target_repo or "unresolved repository"
        lines.append(f"  {_STATUS_MARK.get(impact.status, '-')} {impact.target_label}  ({target})")
        if impact.reason:
            lines.append(f"      {impact.reason}")

    if result.notes:
        _section(lines, "NOTES")
        for note in result.notes:
            lines.append(f"  - {note}")

    if verbose and result.diagnostics:
        _section(lines, "DIAGNOSTICS")
        for note in result.diagnostics:
            lines.append(f"  - {note}")

    return "\n".join(lines).rstrip() + "\n"
