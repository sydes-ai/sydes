"""The Investigator: a Finder/Verifier mode of Code Review (opt-in, minimal).

Same question as the single-pass review -- does this patch introduce a defect? --
and the same defect classes, but answered by investigating instead of in one shot:

1. **Units.** One unit per changed source file (at most `MAX_UNITS`), in the order
   the single-pass review already prioritises files.
2. **Finder** (one tool-using session per unit): reads the unit's diff and changed
   regions, then may read files, search, and walk the code graph (callers, callees,
   symbols) to follow what the changed code calls and where its results go. It
   proposes candidate defects, each citing code quotes as evidence.
3. **Quote check** (deterministic): every quote must be present, whitespace-normalised,
   in the cited file within `QUOTE_WINDOW` lines of the cited line. A candidate needs
   at least one verified quote and must anchor on a changed line, the same scope
   rule the single-pass review applies.
4. **Verifier** (a fresh session per candidate, never shown the finder's
   transcript): tries to refute the candidate with the same tools and must answer
   confirmed, refuted or uncertain, citing its own quotes, which are checked the same
   way. Only confirmed candidates become findings.

Nothing a model says is accepted on its word: findings rest on quotes the code
contains. LLM calls are capped per run (`MAX_LLM_CALLS`). No language- or
framework-specific text appears here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sydes.llm.client import LLMClient, create_default_llm_client
from sydes.recovery.react import TOOL_PROMPT_BLOCK, run_react_loop
from sydes.recovery.schema import RecoveryError
from sydes.recovery.tools import RepoTools
from sydes.verify.llm_findings import (
    _CODE_FINDINGS_HEADER,
    _changed_ranges_by_file,
    _normalize_for_containment,
)
from sydes.verify.models import CodeFinding, EvidenceRef

MAX_UNITS = 6
MAX_CANDIDATES_PER_UNIT = 4
MAX_VERIFIED_CANDIDATES = 8
FINDER_TURNS = 6
VERIFIER_TURNS = 4
MAX_LLM_CALLS = 60
QUOTE_WINDOW = 5
MIN_QUOTE_CHARS = 8
_MAX_RESPONSE_CHARS = 2000
_MAX_UNIT_DIFF_CHARS = 24_000
_SEVERITIES = ("P0", "P1", "P2", "P3")
#: Reasoning effort for Investigator sessions on reasoning models. Short tool-use turns
#: otherwise run at the provider default, which was observed answering with almost no
#: reasoning (6-25 tokens a turn). A run-wide SYDES_LLM_REASONING_EFFORT still wins.
DEFAULT_REASONING_EFFORT = "high"
_INVESTIGATE_FIRST = (
    "You concluded without investigating. Before answering, read the changed function in full "
    "and follow what the changed code produces (values, generators, resources) to the code that "
    "consumes it, and check the callers whose contract the change affects. Then give your final answer."
)


def _defect_classes() -> str:
    """The single-pass review's own defect classes and exclusions, verbatim, so the
    Investigator answers the same question with the same scope."""
    start = _CODE_FINDINGS_HEADER.index("Report a finding only for")
    end = _CODE_FINDINGS_HEADER.index("EVIDENTIARY STANDARD")
    return _CODE_FINDINGS_HEADER[start:end].strip()


FINDER_SYSTEM = (
    "You are the FINDER in a two-stage code review. The one question: does this patch "
    "introduce a defect? Investigate the change in your unit with the tools before answering. "
    "Read what the changed code calls, and follow where its results go: what consumes a value, "
    "a generator or a resource the change produces, and whether that consumer upholds what the "
    "change relies on. Check the callers when a changed function's contract changes.\n\n"
    + _defect_classes()
    + "\n\nEvery candidate must anchor on a CHANGED line of the unit's file and cite evidence: "
    "verbatim quotes of code (one line or a short span, copied exactly) with the file and the "
    "line where each quote starts. Quotes are checked against the repository; a candidate whose "
    "quotes are not in the code is discarded. Propose at most "
    f"{MAX_CANDIDATES_PER_UNIT} candidates; propose none when you find no concrete defect.\n\n"
    + TOOL_PROMPT_BLOCK
    + '\n\nWhen done, reply with {"final": {"candidates": [{"file": "path", "line": int, '
    '"severity": "P0|P1|P2|P3", "title": "short", "failure_scenario": "input/state -> wrong '
    'result", "suggested_fix": "optional", "evidence": [{"file": "path", "line": int, '
    '"quote": "exact code"}]}]}}'
)

VERIFIER_SYSTEM = (
    "You are the VERIFIER in a two-stage code review. You are given ONE candidate defect that "
    "another reviewer proposed. Your job is to try to refute it. Use the tools to read the cited "
    "code and anything that decides whether the failure can actually happen (callers, guards, "
    "the consumer of a value, the language's own semantics). Answer:\n"
    "- confirmed: the failure scenario holds; every step is shown by code you quote;\n"
    "- refuted: code you quote shows the scenario cannot happen, or the claim misreads the code;\n"
    "- uncertain: the evidence decides neither way.\n"
    "Do not confirm on plausibility. Quotes must be verbatim code with the file and starting line; "
    "they are checked against the repository.\n\n"
    + TOOL_PROMPT_BLOCK
    + '\n\nWhen done, reply with {"final": {"verdict": "confirmed|refuted|uncertain", '
    '"reason": "short", "evidence": [{"file": "path", "line": int, "quote": "exact code"}]}}'
)


@dataclass
class InvestigatorStats:
    """What `run_react_loop` accumulates, summed over every session of the run."""

    turns: int = 0
    llm_calls: int = 0
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    files_read: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    file: str
    line: int | None
    severity: str
    title: str
    failure_scenario: str
    suggested_fix: str
    evidence: list[dict[str, Any]]
    unit: str


# ----------------------------------------------------------------------------- evidence


def quote_present(repo_root: Path, file: str, line: int | None, quote: str) -> bool:
    """Whether `quote` is in `file`, whitespace-normalised, within QUOTE_WINDOW lines
    of `line` (anywhere in the file when no line is given). Never fuzzy."""
    if not isinstance(quote, str) or len(quote.strip()) < MIN_QUOTE_CHARS:
        return False
    try:
        root = repo_root.resolve()
        path = (root / file).resolve()
        if root not in path.parents or not path.is_file():
            return False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return False
    if isinstance(line, int) and line > 0:
        low, high = max(0, line - 1 - QUOTE_WINDOW), min(len(lines), line + QUOTE_WINDOW + len(quote.splitlines()))
        window = lines[low:high]
    else:
        window = lines
    return _normalize_for_containment(quote) in _normalize_for_containment(" ".join(window))


def verified_evidence(repo_root: Path, evidence: Any) -> list[dict[str, Any]]:
    out = []
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            continue
        file, line, quote = item.get("file"), item.get("line"), item.get("quote")
        line = int(line) if isinstance(line, int | float) else None
        if isinstance(file, str) and quote_present(repo_root, file, line, quote):
            out.append({"file": file, "line": line, "quote": str(quote)[:400]})
    return out


# ----------------------------------------------------------------------------- units


_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def split_diff(diff_text: str) -> dict[str, str]:
    """The unified diff, one chunk per (new-side) file path."""
    chunks: dict[str, list[str]] = {}
    current: str | None = None
    for raw in diff_text.splitlines():
        match = _DIFF_FILE.match(raw)
        if match:
            current = match.group(2)
            chunks.setdefault(current, [])
        if current is not None:
            chunks[current].append(raw)
    return {path: "\n".join(lines) for path, lines in chunks.items()}


def build_units(context: dict[str, Any], *, program_source: Any = None) -> list[dict[str, Any]]:
    """One unit per changed source file, in the review context's own order."""
    diffs = split_diff(str(context.get("diff") or ""))
    change = context.get("change") or {}
    units = []
    for item in change.get("files") or []:
        path = str(item.get("path") or "")
        if not path or item.get("change_type") == "deleted" or path not in diffs:
            continue
        if item.get("role") in ("test_usage_candidate",) or (program_source and not program_source(path)):
            continue
        units.append({
            "file": path,
            "diff": diffs[path][:_MAX_UNIT_DIFF_CHARS],
            "changed_ranges": item.get("changed_ranges") or [],
            "regions": [
                {"symbol": s.get("name"), "lines": [s.get("start_line"), s.get("end_line")], "source": s["changed_region"]}
                for s in change.get("symbols") or [] if s.get("file") == path and s.get("changed_region")
            ],
            # related code is linked to the change by its reason ("X calls Y at <changed file>:N")
            "related_code": [r for r in context.get("related_code") or []
                             if path in str(r.get("why") or "") or r.get("file") == path][:6],
        })
        if len(units) >= MAX_UNITS:
            break
    return units


# ----------------------------------------------------------------------------- sessions


def _parse_json(text: str) -> dict[str, Any]:
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _budget_left(stats: InvestigatorStats, turns: int) -> bool:
    # a session can use up to turns + 1 calls, plus one corrective re-ask per turn
    return stats.llm_calls + turns + 1 <= MAX_LLM_CALLS


def with_reasoning_effort(client: LLMClient, effort: str | None) -> LLMClient:
    """Set the reasoning effort on the provider client (through a tracing wrapper),
    unless one is already configured; clients without the setting are left alone."""
    target = getattr(client, "_inner", client)
    if effort and hasattr(target, "reasoning_effort") and not getattr(target, "reasoning_effort", None):
        target.reasoning_effort = effort
    return client


def find(unit: dict[str, Any], *, client: LLMClient, tools: RepoTools, stats: InvestigatorStats,
         questions: list[dict[str, str]]) -> list[Candidate]:
    prompt = json.dumps({"unit_file": unit["file"], "changed_ranges": unit["changed_ranges"], "diff": unit["diff"],
                         "changed_regions": unit["regions"], "related_code": unit["related_code"],
                         "open_questions": questions[:8]}, indent=1)
    initial = "Investigate this unit of the change:\n" + prompt
    tool_calls_before = len(tools.calls)
    final = run_react_loop(client=client, tools=tools, system_prompt=FINDER_SYSTEM, initial_prompt=initial,
                           max_turns=FINDER_TURNS, max_response_chars=_MAX_RESPONSE_CHARS,
                           stats=stats, parse_final=_parse_json)
    if len(tools.calls) == tool_calls_before and _budget_left(stats, FINDER_TURNS):
        # concluded without a single tool call: once more, told to investigate first
        final = run_react_loop(client=client, tools=tools, system_prompt=FINDER_SYSTEM,
                               initial_prompt=f"{initial}\n\n{_INVESTIGATE_FIRST}",
                               max_turns=FINDER_TURNS, max_response_chars=_MAX_RESPONSE_CHARS,
                               stats=stats, parse_final=_parse_json)
    out = []
    for raw in (final.get("candidates") or [])[:MAX_CANDIDATES_PER_UNIT]:
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        line = raw.get("line")
        severity = str(raw.get("severity") or "P2").upper()
        out.append(Candidate(
            file=str(raw.get("file") or unit["file"]), line=int(line) if isinstance(line, int | float) else None,
            severity=severity if severity in _SEVERITIES else "P2", title=str(raw["title"]).strip()[:200],
            failure_scenario=str(raw.get("failure_scenario") or "").strip(),
            suggested_fix=str(raw.get("suggested_fix") or "").strip(),
            evidence=raw.get("evidence") or [], unit=unit["file"],
        ))
    return out


def verify(candidate: Candidate, evidence: list[dict[str, Any]], unit: dict[str, Any], *, client: LLMClient,
           tools: RepoTools, stats: InvestigatorStats) -> dict[str, Any]:
    claim = {"file": candidate.file, "line": candidate.line, "title": candidate.title,
             "failure_scenario": candidate.failure_scenario, "cited_evidence": evidence, "unit_diff": unit["diff"]}
    final = run_react_loop(client=client, tools=tools, system_prompt=VERIFIER_SYSTEM,
                           initial_prompt="Try to refute this candidate defect:\n" + json.dumps(claim, indent=1),
                           max_turns=VERIFIER_TURNS, max_response_chars=_MAX_RESPONSE_CHARS,
                           stats=stats, parse_final=_parse_json)
    verdict = str(final.get("verdict") or "uncertain").lower()
    return {"verdict": verdict if verdict in ("confirmed", "refuted", "uncertain") else "uncertain",
            "reason": str(final.get("reason") or "").strip(), "evidence": final.get("evidence") or []}


def _same_place(a: Candidate, b: Candidate) -> bool:
    return a.file == b.file and a.line is not None and b.line is not None and abs(a.line - b.line) <= 3


# ----------------------------------------------------------------------------- run


def investigate(*, context: dict[str, Any], repo_root: Path, model_spec: str | None = None,
                llm_client: LLMClient | None = None, tools: RepoTools | None = None,
                program_source: Any = None) -> tuple[list[CodeFinding], list[str]]:
    """Run the Investigator over the same context the single-pass review gets.
    Returns findings (only verifier-confirmed, quote-checked ones) and diagnostics."""
    notes: list[str] = []
    stats = InvestigatorStats()
    effort = os.environ.get("SYDES_INVESTIGATOR_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip() or None
    finder_client = llm_client or with_reasoning_effort(
        create_default_llm_client(model_spec=model_spec, temperature=None, stage="investigator_finder"), effort)
    verifier_client = llm_client or with_reasoning_effort(
        create_default_llm_client(model_spec=model_spec, temperature=None, stage="investigator_verifier"), effort)
    graph = None
    if tools is None:
        try:
            from sydes.recovery.graph_tools import CBMGraphTools
            graph = CBMGraphTools(repo_root)
        except Exception as exc:  # noqa: BLE001 - the graph is optional; source reads still work
            notes.append(f"investigator_graph_unavailable: {exc}")
        tools = RepoTools(repo_root, graph=graph)
    ranges = _changed_ranges_by_file(context)
    questions = context.get("review_questions") or []
    counts = {"units": 0, "candidates": 0, "no_verified_quote": 0, "off_changed_lines": 0, "duplicates": 0,
              "confirmed": 0, "refuted": 0, "uncertain": 0, "session_errors": 0, "budget_skipped": 0}
    findings: list[CodeFinding] = []
    try:
        units = build_units(context, program_source=program_source)
        counts["units"] = len(units)
        accepted: list[tuple[Candidate, list[dict[str, Any]], dict[str, Any]]] = []
        for unit in units:
            if not _budget_left(stats, FINDER_TURNS):
                counts["budget_skipped"] += 1
                continue
            try:
                candidates = find(unit, client=finder_client, tools=tools, stats=stats, questions=questions)
            except RecoveryError as exc:
                counts["session_errors"] += 1
                notes.append(f"investigator_finder_failed: {unit['file']}: {exc}")
                continue
            for candidate in candidates:
                counts["candidates"] += 1
                evidence = verified_evidence(repo_root, candidate.evidence)
                file_ranges = ranges.get(candidate.file, [])
                if not evidence:
                    counts["no_verified_quote"] += 1
                    notes.append(f"investigator_dropped(no verified quote): {candidate.file}:{candidate.line} {candidate.title[:80]!r}")
                    continue
                if not (candidate.line and any(low <= candidate.line <= high for low, high in file_ranges)):
                    counts["off_changed_lines"] += 1
                    notes.append(f"investigator_dropped(not on a changed line): {candidate.file}:{candidate.line} {candidate.title[:80]!r}")
                    continue
                if any(_same_place(candidate, other) for other, _, _ in accepted):
                    counts["duplicates"] += 1
                    continue
                if len(accepted) >= MAX_VERIFIED_CANDIDATES or not _budget_left(stats, VERIFIER_TURNS):
                    counts["budget_skipped"] += 1
                    continue
                try:
                    outcome = verify(candidate, evidence, unit, client=verifier_client, tools=tools, stats=stats)
                except RecoveryError as exc:
                    counts["session_errors"] += 1
                    notes.append(f"investigator_verifier_failed: {candidate.file}:{candidate.line}: {exc}")
                    continue
                if outcome["verdict"] == "confirmed" and not verified_evidence(repo_root, outcome["evidence"]):
                    outcome = {**outcome, "verdict": "uncertain", "reason": "confirmed without a verified quote: " + outcome["reason"]}
                counts[outcome["verdict"]] += 1
                notes.append(f"investigator_verdict({outcome['verdict']}): {candidate.file}:{candidate.line} "
                             f"{candidate.title[:80]!r} -- {outcome['reason'][:160]}")
                accepted.append((candidate, evidence, outcome))
        for index, (candidate, evidence, outcome) in enumerate(c for c in accepted if c[2]["verdict"] == "confirmed"):
            quotes = evidence + verified_evidence(repo_root, outcome["evidence"])
            findings.append(CodeFinding(
                id=f"investigator-{index + 1}", severity=candidate.severity, title=candidate.title,
                file=candidate.file, line=candidate.line,
                explanation=candidate.failure_scenario or None,
                impact=f"Verifier: {outcome['reason']}" if outcome["reason"] else None,
                suggested_fix=candidate.suggested_fix or None, source="investigator",
                evidence=[EvidenceRef(file=q["file"], label=f"verified_quote:{q['line']}" if q["line"] else "verified_quote",
                                      snippet=q["quote"][:300]) for q in quotes[:4]],
            ))
    finally:
        if graph is not None:
            graph.close()
    findings.sort(key=lambda f: _SEVERITIES.index(f.severity))
    notes.append("investigator: " + " ".join(f"{k}={v}" for k, v in counts.items())
                 + f" llm_calls={stats.llm_calls} prompt_tokens={stats.prompt_tokens}"
                 f" completion_tokens={stats.completion_tokens} files_read={len(stats.files_read)}")
    return findings, notes
