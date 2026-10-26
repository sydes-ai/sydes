"""Sydes result JSON -> one AACR-Bench result file, Code Review findings only.

The benchmark boundary: AACR scores review comments, so only Sydes' Code Review
findings (`code_findings`) are submitted. Everything else in a Sydes result --
the change summary, affected flows, routes, runtime and test evidence,
verification status, framework gaps, and the semantic pass's `local_risks` --
is left out of the submission (some of it is kept in telemetry, never scored).

The file is written in the shape AACR's evaluator reads for generic comments
(its `ocr` reader in `evaluation/evaluate.py`):

    {"review": {"comments": [{"path", "start_line", "end_line", "content"}],
                "summary": {"input_tokens", "output_tokens"}},
     "duration_seconds": ...}

Line mapping: a Sydes finding carries one `line`, so start_line = end_line =
line; a finding without a line keeps null lines, which AACR's evaluator reads
as the whole file. No range is invented from evidence spans.
"""

from __future__ import annotations

from typing import Any

BOUNDARY = "code_findings"


def finding_text(finding: dict[str, Any]) -> str:
    """The comment body: what the finding says, in the order Sydes renders it."""
    parts = [
        (finding.get("title") or "").strip(),
        (finding.get("explanation") or "").strip(),
    ]
    impact = (finding.get("impact") or "").strip()
    fix = (finding.get("suggested_fix") or "").strip()
    if impact:
        parts.append(f"Impact: {impact}")
    if fix:
        parts.append(f"Fix: {fix}")
    return "\n".join(part for part in parts if part)


def to_comment(finding: dict[str, Any]) -> dict[str, Any] | None:
    text = finding_text(finding)
    if not text:
        return None
    line = finding.get("line")
    line = line if isinstance(line, int) and line > 0 else None
    return {
        "path": (finding.get("file") or "").lstrip("./") or None,
        "start_line": line,
        "end_line": line,
        "content": text,
        "severity": finding.get("severity"),  # archived only; AACR does not score it
    }


def to_aacr_result(instance_id: str, sydes: dict[str, Any] | None, *, duration_seconds: float,
                   status: str) -> dict[str, Any]:
    """One AACR result file. A failed or missing Sydes run still yields a file with
    no comments, so the case stays in AACR's denominators instead of vanishing."""
    findings = (sydes or {}).get("code_findings") or []
    comments = [c for c in (to_comment(f) for f in findings if isinstance(f, dict)) if c]
    totals = (((sydes or {}).get("run_metrics") or {}).get("llm_totals")) or {}
    return {
        "instance_id": instance_id,
        "reviewer": "sydes",
        "boundary": BOUNDARY,
        "status": status,
        "duration_seconds": round(float(duration_seconds), 1),
        "review": {
            "comments": comments,
            "summary": {
                "input_tokens": int(totals.get("input_tokens") or 0),
                "output_tokens": int(totals.get("output_tokens") or 0),
            },
        },
    }
