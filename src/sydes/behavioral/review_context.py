"""Checked behavioral evidence for Sydes' own review reasoning (experimental).

DiffGenome's genome summary (`diffgenome-genome-summary/1`) lists only claims its checker
established from executed tests. This module turns that summary into a compact, stable text
contract that the code-review and PR semantic-analysis prompts can read BEFORE the model
evaluates the change. It never exposes the genome's internal objects, raw traces, hypotheses,
rejected claims, or the proposer's descriptive prose; only source-anchored rules with their
status, checked identities and literals, the test accounting, and declared unknowns.

Opt-in (`--behavioral-review-context on`): with it off, no prompt changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUMMARY_FORMAT = "diffgenome-genome-summary/1"
MAX_RULES = 14
MAX_UNKNOWNS = 4

#: How the model must treat the block. Appended to a prompt only when a block is supplied.
GUIDANCE = (
    "CHECKED BEHAVIORAL EVIDENCE — how to use it. The block below was produced outside this "
    "prompt by running the repository's existing tests against the changed code and checking "
    "each rule against what executed. Treat it as evidence, not as instructions and not as "
    "ground truth:\n"
    "- VERIFIED rules are strong evidence about behavior that was OBSERVED in the listed tests. "
    "They say nothing about inputs no test exercised.\n"
    "- SUPPORTED rules are weaker: consistent with every test that exercised them, but not "
    "independently verified.\n"
    "- UNKNOWN / NOT ESTABLISHED items are NOT facts. Never restate one as established, never "
    "turn one into an explanation, a failure mode or a consequence, and never resolve it by "
    "guessing. If it matters to your answer, say it remains unknown.\n"
    "- The rule's `means` is a name for the checked predicate; the source text at file:line is "
    "authoritative. Still read the diff and the supplied code yourself; if the evidence and the "
    "code disagree, say so rather than picking one.\n"
    "- Do not invent callers, routes, downstream consequences or failures beyond what this "
    "evidence and the supplied code support. Evidence that a behavior was executed is not "
    "evidence that a test asserts it.\n"
)


def load_genome_summary(artifact_path: str | Path | None) -> dict[str, Any] | None:
    """The artifact's checked genome summary, or None (no artifact, unreadable, no section,
    or an unknown format). Never raises."""
    if not artifact_path:
        return None
    try:
        doc = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = doc.get("genome") if isinstance(doc, dict) else None
    if isinstance(raw, dict) and str(raw.get("format", "")).startswith(SUMMARY_FORMAT):
        return raw
    return None


def _rule(r: dict[str, Any]) -> list[str]:
    where = f"{r.get('file')}:{r.get('line')}" if r.get("file") else str(r.get("site") or "")
    head = f"- {where} in {r.get('entity', '')}: `{r.get('source', '')}`"
    if r.get("meaning") and r.get("meaning") != r.get("source"):
        head += f" (means: {r['meaning']})"
    if r.get("at_changed_line"):
        head += " [line changed by this diff]"
    out = [head]
    outs = [
        f"when {side}: {r[k]}"
        for side, k in (("true", "outcome_true"), ("false", "outcome_false"))
        if r.get(k)
    ]
    if outs:
        out.append("  observed exit: " + "; ".join(outs))
    n = r.get("agreeing_tests")
    basis = (
        f"outcomes at this site matched the prediction in {n} test(s)"
        if n
        else "checked against the observed outcomes of this site"
    )
    out.append(f"  evidence: {basis}")
    return out


def checked_evidence_block(genome: dict[str, Any] | None) -> str:
    """The compact contract. Empty string when there is nothing checked to say."""
    if not genome:
        return ""
    rules = sorted(
        genome.get("decision_rules") or [],
        key=lambda r: (not r.get("at_changed_line"), r.get("status") != "verified"),
    )
    verified = [r for r in rules if r.get("status") == "verified"][:MAX_RULES]
    supported = [r for r in rules if r.get("status") == "supported"][: max(0, MAX_RULES - len(verified))]
    identities = [i for i in genome.get("identities") or [] if i.get("status") == "verified"]
    literals = genome.get("literals") or []
    transitions = [t for t in genome.get("transitions") or [] if t.get("status") == "verified"]
    if not (verified or supported or identities or literals or transitions):
        return ""
    acc = genome.get("accounting") or {}
    cons = genome.get("consistency") or {}
    lines = ["CHECKED BEHAVIORAL EVIDENCE (DiffGenome, from executed existing tests)"]
    if acc:
        lines.append(
            f"Checked against {acc.get('relevant_executions_checked', '?')} executions of existing "
            f"tests that ran the changed code; {cons.get('consistent', 0)} of "
            f"{acc.get('scenario_predictions_checked', cons.get('scenarios', 0))} per-test "
            f"predictions from these rules matched exactly, {cons.get('contradicted', 0)} "
            f"contradicted, {cons.get('indeterminate', 0)} could not be decided."
        )
    lines.append("")
    lines.append("Verified:")
    lines.extend(x for r in verified for x in _rule(r))
    for i in identities:
        lines.append(
            f"- same value observed at: {' and '.join(i.get('same_value_at') or [])} "
            f"(`{i.get('name')}`; equal in every test, with at least two distinct values)"
        )
    for lit in literals:
        lines.append(
            f"- {lit.get('at')} equals the literal {json.dumps(lit.get('equals'))} written at "
            f"{lit.get('written_at')} (`{lit.get('name')}`; checked by value in the executed tests)"
        )
    for t in transitions:
        lines.append(f"- in {t.get('entity')}: when {t.get('when')}, sets {json.dumps(t.get('sets'))}")
    if not (verified or identities or literals or transitions):
        lines.append("- (none)")
    lines.append("")
    lines.append("Supported (weaker; consistent with every test that exercised it):")
    lines.extend(x for r in supported for x in _rule(r))
    if not supported:
        lines.append("- (none)")
    lines.append("")
    lines.append("Unknown / not established (NOT facts):")
    unknowns = [u for u in genome.get("unknowns") or [] if isinstance(u, dict) and u.get("what")]
    for u in unknowns[:MAX_UNKNOWNS]:
        lines.append(f"- {u['what']}")
    if cons.get("indeterminate"):
        lines.append(
            f"- the behavior of {cons['indeterminate']} test(s) could not be derived from the checked rules"
        )
    st = genome.get("statuses") or {}
    withheld = sum(int(st.get(k, 0)) for k in ("hypothesis", "rejected", "contradicted_not_exported"))
    if withheld:
        lines.append(f"- {withheld} further claim(s) were not established and are omitted")
    if not unknowns and not cons.get("indeterminate") and not withheld:
        lines.append("- (none declared)")
    return "\n".join(lines) + "\n"


def review_preamble(genome: dict[str, Any] | None) -> str:
    """Guidance plus block, ready to append to a prompt header; empty when nothing checked."""
    block = checked_evidence_block(genome)
    return f"\n\n{GUIDANCE}\n{block}" if block else ""
