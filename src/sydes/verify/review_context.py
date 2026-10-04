"""Structurally related source for the code-review pass.

Code review judges the patch itself, but a changed line's correctness often depends on the
implementation it calls or constructs right there -- a wrapper the changed generator is
handed to, a writer whose `close()` the change now calls, a helper whose contract the change
relies on. The diff alone does not show that code.

This module selects it from the code graph, never by name or framework: for each changed
program symbol, the repository code it calls or constructs at a line within
`NEAR_CHANGE_LINES` of a changed line (CBM CALLS edges carry the call line), nearest first,
and its direct non-test callers. Each entry carries the source and the structural reason it
was included. These are deterministic source facts -- no flow, impact or verdict from the
system analysis enters the review context.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

NEAR_CHANGE_LINES = 6
MAX_RELATED = 8
MAX_RELATED_CALLERS = 3
MAX_RELATED_LINES = 120
MAX_RELATED_CHARS = 5_000
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|\.(spec|test)\.[jt]sx?$|Test\.java$|(^|/)test_[^/]*\.py$")


def _index_by_qn(symbol_index: dict[str, Any], repo: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for payload in symbol_index.get("repos", []) or []:
        if payload.get("repo") != repo:
            continue
        for item in payload.get("files", []) or []:
            for sym in item.get("symbols", []) or []:
                qn = sym.get("cbm_qualified_name")
                if qn:
                    out[str(qn)] = {**sym, "file": str(item.get("path"))}
    return out


def _source(repo_root: Path, file: str, start: int, end: int) -> str:
    try:
        lines = (repo_root / file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    end = min(len(lines), end, start + MAX_RELATED_LINES - 1)
    text = "\n".join(lines[start - 1:end])
    return text[:MAX_RELATED_CHARS]


def select_related_code(
    *, change: Any, facts: Any, symbol_index: dict[str, Any], repo: str, repo_root: Path,
    program_source: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """(related code entries for the review context, diagnostics naming what was added and
    why). `facts` is the CBM fact layer for `repo` (None without a code graph: nothing is
    added, and the diagnostics say so)."""
    if facts is None:
        return [], ["code_review_context_related=none (no code graph for this repository)"]
    by_qn = _index_by_qn(symbol_index, repo)
    changed = [s for s in change.symbols if s.repo == repo and s.cbm_qualified_name
               and program_source(s.file) and not _TEST_PATH.search(s.file)]
    if not changed:
        return [], ["code_review_context_related=none (no changed program symbols)"]
    changed_qns = {s.cbm_qualified_name for s in change.symbols if s.cbm_qualified_name}
    hunks = {f.path: [(h.start_line, h.end_line) for h in f.hunks] for f in change.files}
    relations = facts.relations([s.cbm_qualified_name for s in changed])

    candidates: dict[str, tuple[int, str, dict[str, Any]]] = {}
    callers: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for symbol in changed:
        ranges = hunks.get(symbol.file, [])
        for rel in relations.get(symbol.cbm_qualified_name, []):
            if rel.type != "CALLS":
                continue
            if rel.source == symbol.cbm_qualified_name:  # outgoing: what the changed code calls/constructs
                target = by_qn.get(rel.target)
                line = rel.props.get("line")
                if target is None or rel.target in changed_qns or _TEST_PATH.search(target["file"]):
                    continue
                if not isinstance(line, int) or not ranges:
                    continue
                distance = min(0 if lo <= line <= hi else min(abs(line - lo), abs(line - hi)) for lo, hi in ranges)
                if distance > NEAR_CHANGE_LINES:
                    continue
                verb = "constructs" if target.get("kind") == "class" else "calls"
                reason = (f"{symbol.name} {verb} {target.get('name')} at {symbol.file}:{line}"
                          + (" (a changed line)" if distance == 0 else f" ({distance} line(s) from a changed line)"))
                best = candidates.get(rel.target)
                if best is None or distance < best[0]:
                    candidates[rel.target] = (distance, reason, target)
            elif rel.target == symbol.cbm_qualified_name:  # incoming: who depends on the changed code
                caller = by_qn.get(rel.source)
                if caller is None or rel.source in changed_qns or _TEST_PATH.search(caller["file"]):
                    continue
                callers.setdefault(rel.source, (0, f"{caller.get('name')} calls the changed {symbol.name}", caller))

    chosen = sorted(candidates.values(), key=lambda item: (item[0], item[2]["file"], item[2].get("start_line") or 0))
    chosen = chosen[:MAX_RELATED] + list(callers.values())[:MAX_RELATED_CALLERS]
    entries: list[dict[str, Any]] = []
    notes: list[str] = []
    for _distance, reason, target in chosen:
        start, end = int(target.get("start_line") or 0), int(target.get("end_line") or 0)
        if start <= 0 or end < start:
            continue
        text = _source(repo_root, target["file"], start, end)
        if not text:
            continue
        entries.append({"file": target["file"], "symbol": target.get("name"), "kind": target.get("kind"),
                        "lines": f"{start}-{min(end, start + MAX_RELATED_LINES - 1)}", "why": reason, "source": text})
        notes.append(f"code_review_context_added: {target['file']}:{target.get('name')} -- {reason}")
    notes.append(f"code_review_context_related={len(entries)}")
    return entries, notes


MAX_REVIEW_QUESTIONS = 10


def review_questions(*, semantic_analysis: Any | None, framework_candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Open questions earlier analysis raised about this change, handed to code review as
    questions -- never as findings or conclusions: the change analysis' investigation hints,
    uncertainties and unverified local risks, and what structural enrichment could not
    establish at a framework boundary. Review decides from the supplied evidence whether any
    of them is an actual defect."""
    questions: list[dict[str, str]] = []

    def add(source: str, text: Any) -> None:
        text = " ".join(str(text or "").split())
        if text and len(questions) < MAX_REVIEW_QUESTIONS and all(q["question"] != text for q in questions):
            questions.append({"source": source, "question": text})

    if semantic_analysis is not None:
        for hint in getattr(semantic_analysis, "investigation_hints", []) or []:
            add("change analysis: to investigate", getattr(hint, "description", hint))
        for risk in getattr(semantic_analysis, "local_risks", []) or []:
            add("change analysis: unverified risk", getattr(risk, "description", risk))
        for item in getattr(semantic_analysis, "uncertainties", []) or []:
            add("change analysis: uncertain", item)
    for candidate in framework_candidates or []:
        if candidate.get("status") != "unresolved":
            continue
        for missing in candidate.get("missing") or []:
            add(f"structural analysis: unresolved {candidate.get('category')} at {candidate.get('source_symbol')}", missing)
    return questions
