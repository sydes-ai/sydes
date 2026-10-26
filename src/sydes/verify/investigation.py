"""Evidence-constrained code review: Finder -> Investigator -> Verifier (experimental).

A prototype of a review protocol, opt-in behind `--investigator`:

    existing Sydes evidence (packet) -> Finder: <= MAX_HYPOTHESES falsifiable hypotheses
      -> Investigator: bounded, recorded lookups (<= MAX_LOOKUPS per PR)
      -> Verifier: an independent pass per hypothesis -> CONFIRMED | REFUTED | UNKNOWN
      -> only CONFIRMED claims, source-verified and causally tied to the change, become findings

The packet is what Sydes already established for the change -- diff, changed symbols,
structural relations, route flows, mapped and executed tests, runtime evidence, open
gaps, the change analysis -- each item carrying its provenance. Nothing is recomputed.

Claim discipline: a CONFIRMED status needs (1) defect evidence -- source text
`verify_citation` finds in the repository, on any line of any file -- and (2) the change's
own part in it -- verified quotes of the changed lines plus a stated causal link -- with
the claim stated at the scope its evidence supports (`supported_claim`); that narrower
claim, not the finder's, is what a finding says. Anything else is UNKNOWN, never a warning.

Every hypothesis keeps its trace -- why it was proposed, what was looked up and why,
what decided it -- in a language-neutral artifact (`investigation.json`). No bug
pattern, language or framework rule appears in this module.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sydes.impact.citation_check import verify_citation
from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    create_default_llm_client,
)
from sydes.recovery.react import extract_turn
from sydes.recovery.schema import RecoveryError
from sydes.recovery.tools import RepoTools
from sydes.verify.models import CodeFinding, EvidenceRef

VERSION = "sydes-investigation/1"
MAX_HYPOTHESES = 5
MAX_LOOKUPS = 12
MAX_LOOKUPS_PER_HYPOTHESIS = 4
DEFAULT_REASONING_EFFORT = "high"
_ITEM_CHARS = 2_500
_PACKET_CHARS = 60_000
_OBSERVATION_CHARS = 6_000
_SEVERITIES = ("P0", "P1", "P2", "P3")
STATUS_CONFIRMED, STATUS_REFUTED, STATUS_UNKNOWN = "CONFIRMED", "REFUTED", "UNKNOWN"


# ----------------------------------------------------------------------------- evidence packet


@dataclass
class Evidence:
    id: str
    kind: str          # provenance class: diff | changed_symbol | structural | route_flow | test_mapping |
    #                    test_execution | runtime | gap | change_analysis
    ref: str           # where it came from (file:line, flow id, test id, ...)
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "ref": self.ref, "text": self.text}


def _clip(text: Any, limit: int = _ITEM_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def split_diff(diff_text: str) -> dict[str, str]:
    chunks: dict[str, list[str]] = {}
    current = None
    for raw in diff_text.splitlines():
        match = _DIFF_FILE.match(raw)
        if match:
            current = match.group(2)
            chunks.setdefault(current, [])
        if current is not None:
            chunks[current].append(raw)
    return {path: "\n".join(lines) for path, lines in chunks.items()}


def build_packet(context: dict[str, Any], result: Any) -> list[Evidence]:
    """What Sydes already has about the change, each item with its provenance. `context`
    is the code-review context; `result` the analysis so far (flows, tests, runtime, gaps,
    change analysis). Bounded; nothing here runs a new analysis."""
    items: list[Evidence] = []

    def add(kind: str, ref: str, text: Any) -> None:
        items.append(Evidence(f"E{len(items) + 1}", kind, ref, _clip(text)))

    change = context.get("change") or {}
    for path, chunk in split_diff(str(context.get("diff") or "")).items():
        add("diff", path, chunk)
    for symbol in change.get("symbols") or []:
        add("changed_symbol", f"{symbol.get('file')}:{symbol.get('start_line')}-{symbol.get('end_line')}",
            f"{symbol.get('kind')} {symbol.get('name')}\n{symbol.get('changed_region') or ''}")
    for rel in context.get("related_code") or []:
        add("structural", f"{rel.get('file')}:{rel.get('lines')}", f"{rel.get('why')}\n{rel.get('source') or ''}")
    for flow in getattr(result, "affected_flows", None) or []:
        chain = " -> ".join(str(s.get("detail") or s.get("name") or s.get("id") or "") for s in flow.steps[:12])
        add("route_flow", flow.id, f"{flow.entry_label}: {chain}")
        for obligation in flow.obligations[:4]:
            tests = ", ".join(t.id for t in obligation.mapped_tests[:6]) or "none mapped"
            add("test_mapping", obligation.id, f"{obligation.statement} [status {obligation.status}; tests: {tests}]")
    for execution in (getattr(result, "test_executions", None) or [])[:10]:
        add("test_execution", execution.test_id,
            f"status {execution.status}; {execution.reason or execution.blocker or ''} {execution.failure_summary or ''}")
    suite = getattr(result, "ci_suite", None)
    if suite is not None:
        add("test_execution", "ci_suite", f"{getattr(suite, 'status', '')} {getattr(suite, 'reason', '') or ''}")
    behavioral = getattr(result, "behavioral", None)
    if behavioral is not None:
        add("runtime", "diffgenome", (
            f"status {behavioral.status}; {behavioral.reason or ''}; changed symbols executed: "
            f"{', '.join(behavioral.changed_symbols[:12]) or 'none'}; never executed: "
            f"{', '.join(behavioral.changed_symbols_never_executed[:12]) or 'none'}; tests on path: "
            f"{', '.join(behavioral.tests_on_behavioral_path[:8]) or 'none'}"))
    for gap in (getattr(result, "verification_gaps", None) or [])[:6]:
        add("gap", gap.id, f"{gap.behavior} -- {gap.why or ''}")
    for name in (getattr(result, "unresolved_changed_symbol_names", None) or [])[:12]:
        add("gap", f"unresolved:{name}", f"changed symbol {name}: no structural path to an entrypoint was established")
    semantic = getattr(result, "pr_semantic_analysis", None)
    if semantic is not None:
        add("change_analysis", "summary", semantic.change_summary)
        for risk in semantic.local_risks[:5]:
            add("change_analysis", "local_risk", f"[inference, not verified] {risk.description}")
        for question in semantic.uncertainties[:5]:
            add("change_analysis", "uncertainty", question)

    kept, total = [], 0
    for item in items:  # diff and changed code first: already in that order
        if total + len(item.text) > _PACKET_CHARS:
            break
        kept.append(item)
        total += len(item.text)
    return kept


def changed_ranges(context: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for item in (context.get("change") or {}).get("files") or []:
        for pair in item.get("changed_ranges") or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                out.setdefault(str(item.get("path")), []).append((int(pair[0]), int(pair[1])))
    return out


# ----------------------------------------------------------------------------- prompts


FINDER_SYSTEM = (
    "You are the FINDER in an evidence-constrained code review. The question is whether this "
    "change introduces a defect. You are given the evidence Sydes already has about the change, "
    "each item with an id and its provenance. Propose at most "
    f"{MAX_HYPOTHESES} hypotheses: each one atomic, falsifiable, tied to the changed behavior, and "
    "phrased as a specific property the change may violate -- never a vague concern, style, naming "
    "or a request for tests. Look beyond the direct callers where the evidence warrants it: what "
    "consumes the values the change produces, state the change mutates, sibling or alternate paths "
    "that should behave alike, error and cleanup behavior, and contracts stated by existing code, "
    "tests or comments. Cite the evidence ids that made you suspect each one. Propose none when "
    "nothing in the evidence points at a concrete defect.\n"
    'Reply with exactly one JSON object: {"hypotheses": [{"id": "H1", "claim": "...", '
    '"why_suspected": "...", "file": "path", "line": int, "severity": "P0|P1|P2|P3", '
    '"initial_evidence": ["E3"], "questions_to_resolve": ["..."]}]}'
)

INVESTIGATOR_SYSTEM = (
    "You are the INVESTIGATOR. You gather evidence that decides ONE hypothesis about a code change, "
    "using read-only lookups. You have a small budget: request only what can confirm or refute the "
    "hypothesis or answer one of its questions, and say why. Call exactly one lookup per turn as a "
    'single JSON object: {"tool": "<name>", "args": {...}, "why": "what this decides"}. Tools:\n'
    '- read_source {"path": str, "start_line": int, "end_line": int}\n'
    '- search_text {"pattern": regex, "path_glob": optional glob}\n'
    '- find_symbol {"name_pattern": regex}\n'
    '- callers {"symbol": name}  /  callees {"symbol": name}\n'
    '- evidence {"id": "E7"}   (the full text of a packet item)\n'
    '- tests_for {"term": file or symbol}   (tests Sydes mapped or ran)\n'
    '- runtime_for {"term": symbol}   (what runtime evidence observed)\n'
    'When the hypothesis is decided, or no lookup would help, reply {"final": {"summary": "..."}}.'
)

VERIFIER_SYSTEM = (
    "You are the VERIFIER. You did not propose this hypothesis and have no stake in it. Using ONLY "
    "the evidence given (the change, packet items and lookup results), look explicitly for: evidence "
    "that confirms it, evidence that refutes it, and facts whose absence prevents a conclusion. A "
    "defect may manifest anywhere -- on an unchanged line or in an unchanged file -- but it counts "
    "only if THIS change causes it: confirm only when you can establish both (1) the defect from "
    "evidence and (2) why the changed code introduces, exposes or materially changes that behavior. "
    "A pre-existing defect the change does not affect is not confirmed. Give exactly one status:\n"
    "- CONFIRMED: both hold; quote the defect evidence, quote the changed code that causes it "
    "(lines inside the change), and state the causal link;\n"
    "- REFUTED: the evidence shows it cannot happen, misreads the code, or the change does not affect it; say which evidence;\n"
    "- UNKNOWN: the evidence decides neither way; say precisely what could not be established.\n"
    "Do not confirm on plausibility. State `supported_claim` no broader than its evidence: if the "
    "evidence covers one path, the claim is about that path, not all paths.\n"
    'Reply with exactly one JSON object: {"status": "CONFIRMED|REFUTED|UNKNOWN", '
    '"supported_claim": "...", "reasoning": "...", "defect_evidence": [{"source": "E3 or L2", "file": '
    '"path", "line": int, "quote": "verbatim code"}], "changed_evidence": [{"source": "...", "file": '
    '"path", "line": int, "quote": "verbatim changed code"}], "causal_link": "...", "refuting": '
    '[{"source": "...", "explanation": "..."}], "missing": ["..."]}'
)


# ----------------------------------------------------------------------------- run state


@dataclass
class Stats:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    seconds: float = 0.0
    by_stage: dict[str, int] = field(default_factory=dict)


def _call(client: LLMClient, stats: Stats, stage: str, *, system: str, prompt: str) -> str:
    started = time.monotonic()
    response = client.generate(LLMRequest(prompt=prompt, system=system, temperature=None))
    stats.seconds += time.monotonic() - started
    stats.llm_calls += 1
    stats.by_stage[stage] = stats.by_stage.get(stage, 0) + 1
    usage = response.usage or {}
    stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
    stats.completion_tokens += int(usage.get("completion_tokens") or 0)
    stats.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
    return response.text


def _json(text: str) -> dict[str, Any]:
    try:
        return extract_turn(text, max_chars=400)
    except RecoveryError:
        return {}


def with_reasoning_effort(client: LLMClient, effort: str | None) -> LLMClient:
    """High effort for these sessions on reasoning models, unless one is configured run-wide."""
    target = getattr(client, "_inner", client)
    if effort and hasattr(target, "reasoning_effort") and not getattr(target, "reasoning_effort", None):
        target.reasoning_effort = effort
    return client


# ----------------------------------------------------------------------------- tools


class Lookups:
    """The Investigator's read-only tool surface, with a hard per-PR budget. Every call is
    recorded with the reason it was requested and the provenance of what it returned."""

    def __init__(self, repo: RepoTools, packet: list[Evidence], *, max_lookups: int = MAX_LOOKUPS) -> None:
        self.repo, self.packet = repo, {e.id: e for e in packet}
        self.max_lookups = max_lookups
        self.records: list[dict[str, Any]] = []

    @property
    def left(self) -> int:
        return self.max_lookups - len(self.records)

    def _from_packet(self, kinds: tuple[str, ...], term: str) -> str:
        hits = [e for e in self.packet.values() if e.kind in kinds and term and term.lower() in (e.ref + e.text).lower()]
        return "\n\n".join(f"[{e.id} {e.kind} {e.ref}]\n{e.text}" for e in hits[:6]) or f"no {'/'.join(kinds)} evidence mentions {term!r}"

    def _packet_item(self, evidence_id: str) -> str:
        item = self.packet.get(evidence_id)
        return f"[{item.id} {item.kind} {item.ref}]\n{item.text}" if item else "no such evidence id"

    def run(self, hypothesis: str, tool: str, args: dict[str, Any], why: str) -> tuple[str, dict[str, Any]]:
        handlers: dict[str, tuple[str, Callable[[], str]]] = {
            "read_source": ("source_read", lambda: self.repo.read_file(str(args.get("path") or ""), start_line=args.get("start_line"), end_line=args.get("end_line"))),
            "search_text": ("source_search", lambda: self.repo.search_text(str(args.get("pattern") or ""), path_glob=args.get("path_glob"))),
            "find_symbol": ("structural_query", lambda: self.repo.search_symbol(str(args.get("name_pattern") or ""))),
            "callers": ("structural_query", lambda: self.repo.trace_callers(str(args.get("symbol") or ""), depth=1)),
            "callees": ("structural_query", lambda: self.repo.trace_callees(str(args.get("symbol") or ""), depth=1)),
            "evidence": ("packet", lambda: self._packet_item(str(args.get("id") or ""))),
            "tests_for": ("test_evidence", lambda: self._from_packet(("test_mapping", "test_execution"), str(args.get("term") or ""))),
            "runtime_for": ("runtime_evidence", lambda: self._from_packet(("runtime",), str(args.get("term") or ""))),
        }
        if tool not in handlers:
            observation, provenance = f"ERROR: unknown lookup {tool!r}", "none"
        else:
            provenance, handler = handlers[tool]
            try:
                observation = handler()
            except Exception as exc:  # noqa: BLE001 - a failed lookup costs its answer, never the review
                observation = f"ERROR: {type(exc).__name__}: {exc}"
        observation = _clip(observation, _OBSERVATION_CHARS)
        record = {"n": len(self.records) + 1, "id": f"L{len(self.records) + 1}", "hypothesis": hypothesis,
                  "tool": tool, "args": args, "why": why, "provenance": provenance,
                  "result_chars": len(observation), "result_excerpt": observation[:600]}
        self.records.append(record)
        return observation, record


# ----------------------------------------------------------------------------- stages


def find(packet: list[Evidence], *, client: LLMClient, stats: Stats) -> list[dict[str, Any]]:
    prompt = "Evidence Sydes has about this change:\n" + json.dumps([e.to_dict() for e in packet], indent=1)
    raw = _json(_call(client, stats, "finder", system=FINDER_SYSTEM, prompt=prompt))
    ids = {e.id for e in packet}
    out = []
    for item in (raw.get("hypotheses") or [])[:MAX_HYPOTHESES]:
        if not isinstance(item, dict) or not str(item.get("claim") or "").strip():
            continue
        line = item.get("line")
        severity = str(item.get("severity") or "P2").upper()
        out.append({
            "id": f"H{len(out) + 1}", "claim": str(item["claim"]).strip(),
            "why_suspected": str(item.get("why_suspected") or "").strip(),
            "file": str(item.get("file") or ""), "line": int(line) if isinstance(line, int | float) else None,
            "severity": severity if severity in _SEVERITIES else "P2",
            "initial_evidence": [e for e in item.get("initial_evidence") or [] if e in ids],
            "questions_to_resolve": [str(q) for q in item.get("questions_to_resolve") or []][:4],
        })
    return out


def investigate_one(hypothesis: dict[str, Any], lookups: Lookups, *, client: LLMClient, stats: Stats) -> list[tuple[dict, str]]:
    """Bounded lookups for one hypothesis; returns (record, observation) pairs."""
    gathered: list[tuple[dict, str]] = []
    transcript = json.dumps({k: hypothesis[k] for k in ("id", "claim", "file", "line", "questions_to_resolve")}, indent=1)
    for _ in range(min(MAX_LOOKUPS_PER_HYPOTHESIS, lookups.left)):
        turn = _json(_call(client, stats, "investigator", system=INVESTIGATOR_SYSTEM,
                           prompt=f"Hypothesis:\n{transcript}\n\nLookups left in the run: {lookups.left}"))
        if "final" in turn or not turn.get("tool"):
            break
        observation, record = lookups.run(hypothesis["id"], str(turn["tool"]), turn.get("args") or {}, str(turn.get("why") or ""))
        gathered.append((record, observation))
        transcript += f"\n\nLOOKUP {record['id']} {record['tool']}({record['args']}) because: {record['why']}\nRESULT:\n{observation}"
    return gathered


def verify(hypothesis: dict[str, Any], packet: dict[str, Evidence], gathered: list[tuple[dict, str]], *,
           client: LLMClient, stats: Stats) -> dict[str, Any]:
    """Independent: sees the claim, the cited packet items and the raw lookup results --
    not the investigator's reasons or summary."""
    diff_items = [e for e in packet.values() if e.kind == "diff"]
    shown = {"hypothesis": {k: hypothesis[k] for k in ("claim", "file", "line", "questions_to_resolve")},
             "change": {"diffs": {e.ref: e.text for e in diff_items}},
             "packet_evidence": [packet[i].to_dict() for i in hypothesis["initial_evidence"]
                                 if i in packet and packet[i].kind != "diff"],
             "lookup_results": [{"id": r["id"], "tool": r["tool"], "args": r["args"], "provenance": r["provenance"],
                                 "result": obs} for r, obs in gathered]}
    raw = _json(_call(client, stats, "verifier", system=VERIFIER_SYSTEM, prompt=json.dumps(shown, indent=1)))
    status = str(raw.get("status") or STATUS_UNKNOWN).upper()
    return {"status": status if status in (STATUS_CONFIRMED, STATUS_REFUTED, STATUS_UNKNOWN) else STATUS_UNKNOWN,
            "supported_claim": str(raw.get("supported_claim") or "").strip(),
            "reasoning": str(raw.get("reasoning") or "").strip(),
            # `confirming` is accepted as an older name for the defect evidence
            "defect_evidence": [c for c in raw.get("defect_evidence") or raw.get("confirming") or [] if isinstance(c, dict)],
            "changed_evidence": [c for c in raw.get("changed_evidence") or [] if isinstance(c, dict)],
            "causal_link": str(raw.get("causal_link") or "").strip(),
            "refuting": [c for c in raw.get("refuting") or [] if isinstance(c, dict)],
            "missing": [str(m) for m in raw.get("missing") or []]}


def _verified_quotes(items: list[dict[str, Any]], repo_root: Path) -> list[dict[str, Any]]:
    out = []
    for item in items:
        line = item.get("line") if isinstance(item.get("line"), int) else 0
        ok, reason = verify_citation(file=str(item.get("file") or ""), line=line,
                                     citation_text=str(item.get("quote") or ""), repo_root=repo_root)
        if ok:
            out.append({**item, "verification": reason})
    return out


def _on_change(item: dict[str, Any], ranges: dict[str, list[tuple[int, int]]]) -> bool:
    line = item.get("line")
    return isinstance(line, int) and any(low - 1 <= line <= high + 1 for low, high in ranges.get(str(item.get("file")), []))


def check_confirmation(outcome: dict[str, Any], hypothesis: dict[str, Any], *, repo_root: Path,
                       ranges: dict[str, list[tuple[int, int]]]) -> dict[str, Any]:
    """Claim discipline. CONFIRMED stands only when the defect is shown by source text
    verified in the repository (anywhere: changed or unchanged lines, any file), the PR is
    tied to it by verified quotes of the change itself, a causal link is stated, and the
    claim is stated at the scope of its evidence. Otherwise it is UNKNOWN."""
    defect = _verified_quotes(outcome["defect_evidence"], repo_root)
    changed = [q for q in _verified_quotes(outcome["changed_evidence"], repo_root) if _on_change(q, ranges)]
    if outcome["status"] != STATUS_CONFIRMED:
        return {**outcome, "verified_defect_evidence": defect, "verified_changed_evidence": changed}
    problems = []
    if not defect:
        problems.append("no defect evidence could be verified in the repository")
    if not changed:
        problems.append("no verified quote of the changed code ties the defect to this change")
    if not outcome["causal_link"]:
        problems.append("no causal link from the change to the defect was stated")
    if not outcome["supported_claim"]:
        problems.append("no supported_claim was stated")
    result = {**outcome, "verified_defect_evidence": defect, "verified_changed_evidence": changed}
    if problems:
        return {**result, "status": STATUS_UNKNOWN,
                "missing": outcome["missing"] + [f"downgraded from CONFIRMED: {p}" for p in problems]}
    return result


# ----------------------------------------------------------------------------- run


def run(*, context: dict[str, Any], result: Any, repo_root: Path, model_spec: str | None = None,
        llm_client: LLMClient | None = None, tools: RepoTools | None = None,
        max_lookups: int = MAX_LOOKUPS) -> tuple[list[CodeFinding], dict[str, Any], list[str]]:
    """The whole protocol. Returns the findings (CONFIRMED only), the investigation
    artifact and diagnostics notes."""
    effort = os.environ.get("SYDES_INVESTIGATOR_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip() or None
    client = llm_client or with_reasoning_effort(
        create_default_llm_client(model_spec=model_spec, temperature=None, stage="investigator"), effort)
    stats = Stats()
    graph = None
    if tools is None:
        try:
            from sydes.recovery.graph_tools import CBMGraphTools
            graph = CBMGraphTools(repo_root)
        except Exception:  # noqa: BLE001 - the graph is optional; source reads still work
            graph = None
        tools = RepoTools(repo_root, graph=graph)
    packet = build_packet(context, result)
    by_id = {e.id: e for e in packet}
    lookups = Lookups(tools, packet, max_lookups=max_lookups)
    ranges = changed_ranges(context)
    hypotheses, outcomes, errors = [], [], []
    try:
        try:
            hypotheses = find(packet, client=client, stats=stats)
        except LLMClientError as exc:
            errors.append(f"finder: {exc}")
        for hypothesis in hypotheses:
            try:
                gathered = investigate_one(hypothesis, lookups, client=client, stats=stats) if lookups.left else []
                outcome = check_confirmation(verify(hypothesis, by_id, gathered, client=client, stats=stats), hypothesis,
                                             repo_root=repo_root, ranges=ranges)
            except LLMClientError as exc:
                errors.append(f"{hypothesis['id']}: {exc}")
                gathered, outcome = [], {"status": STATUS_UNKNOWN, "supported_claim": "", "reasoning": "",
                                         "defect_evidence": [], "changed_evidence": [], "causal_link": "",
                                         "refuting": [], "missing": [f"provider error: {exc}"],
                                         "verified_defect_evidence": [], "verified_changed_evidence": []}
            outcomes.append({"hypothesis": hypothesis["id"], "lookups": [r["id"] for r, _ in gathered], **outcome})
    finally:
        if graph is not None:
            graph.close()

    findings = []
    for hypothesis, outcome in zip(hypotheses, outcomes):
        if outcome["status"] != STATUS_CONFIRMED:
            continue
        quotes = outcome["verified_defect_evidence"][:3] + outcome["verified_changed_evidence"][:1]
        anchor = outcome["verified_defect_evidence"][0]
        findings.append(CodeFinding(
            id=f"investigator-{hypothesis['id']}", severity=hypothesis["severity"],
            title=outcome["supported_claim"][:200], file=hypothesis["file"] or str(anchor.get("file")),
            line=hypothesis["line"] or anchor.get("line"),
            explanation=outcome["reasoning"] or None, impact=f"Caused by the change: {outcome['causal_link']}",
            source="investigator",
            evidence=[EvidenceRef(file=str(q.get("file")), label=f"verified_quote:{q.get('line')}", snippet=str(q.get("quote"))[:300])
                      for q in quotes[:4]],
        ))
    findings.sort(key=lambda f: _SEVERITIES.index(f.severity))
    artifact = {
        "version": VERSION,
        "packet": [{"id": e.id, "kind": e.kind, "ref": e.ref, "chars": len(e.text)} for e in packet],
        "hypotheses": hypotheses,
        "lookups": lookups.records,
        "outcomes": outcomes,
        "confirmed": [o["hypothesis"] for o in outcomes if o["status"] == STATUS_CONFIRMED],
        "refuted": [o["hypothesis"] for o in outcomes if o["status"] == STATUS_REFUTED],
        "unknown": [o["hypothesis"] for o in outcomes if o["status"] == STATUS_UNKNOWN],
        "findings": [f.id for f in findings],
        "budget": {"max_hypotheses": MAX_HYPOTHESES, "max_lookups": max_lookups, "used_lookups": len(lookups.records),
                   "max_lookups_per_hypothesis": MAX_LOOKUPS_PER_HYPOTHESIS},
        "usage": {"llm_calls": stats.llm_calls, "by_stage": stats.by_stage, "prompt_tokens": stats.prompt_tokens,
                  "completion_tokens": stats.completion_tokens, "reasoning_tokens": stats.reasoning_tokens,
                  "llm_seconds": round(stats.seconds, 1)},
        "errors": errors,
    }
    notes = [(f"investigator: hypotheses={len(hypotheses)} lookups={len(lookups.records)}/{max_lookups} "
              f"confirmed={len(artifact['confirmed'])} refuted={len(artifact['refuted'])} unknown={len(artifact['unknown'])} "
              f"findings={len(findings)} llm_calls={stats.llm_calls} errors={len(errors)}")]
    return findings, artifact, notes
