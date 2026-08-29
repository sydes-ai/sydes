"""Bounded LLM reasoning over an already-computed change context.

The LLM never rediscovers the repository. It receives the diff plus the
deterministic Sydes graph slice, and returns structured findings whose file,
line, and node references are validated against that context before they enter
the result. Anything referencing something Sydes did not supply is rejected.

Two tasks live here:
- code findings: semantic defects in the diff that static rules cannot see.
- verification gaps: system behaviors that the change may affect and for which
  no existing verification was located.
"""

from __future__ import annotations

import json
from typing import Any

from pathlib import Path

from sydes.core.models import EvidenceRef
from sydes.impact.investigate import changed_region_source
from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    create_default_llm_client,
)
from sydes.verify.models import (
    VERIFICATION_UNVERIFIED,
    AffectedFlow,
    ChangeSet,
    CodeFinding,
    VerificationGap,
)

MAX_DIFF_CHARS = 14_000
MAX_PROMPT_CHARS = 26_000
MAX_FLOWS_IN_PROMPT = 8
MAX_NODES_PER_FLOW = 14
MAX_FINDINGS = 10
MAX_GAPS = 8

#: Code review gets a larger per-symbol source window than the impact guide's
#: 400-char preview: judging whether a change is *defective* needs to see the
#: whole changed construct, where judging what it *affects* only needs to see
#: which construct moved. Still explicitly bounded, and still the same
#: selection algorithm — only the budget differs.
CODE_REVIEW_REGION_CONTEXT_LINES = 4
CODE_REVIEW_REGION_MAX_LINES = 40
CODE_REVIEW_REGION_MAX_CHARS = 1_600
#: How many changed symbols carry a source region. Beyond this the symbol is
#: still listed (file/name/kind/lines), just without its own region — the raw
#: diff still covers it.
MAX_CODE_REVIEW_REGIONS = 12

_SEVERITIES = {"P0", "P1", "P2", "P3"}
#: Conservative fallback for an unrecognized severity. Lowest actionable
#: rank on purpose: a model that could not name a severity has not earned a
#: reviewer's urgent attention, and silently promoting it to P2 (as this
#: previously did, without recording anything) overstates it.
_DEFAULT_SEVERITY = "P3"
_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _strip_fences(text: str) -> str:
    """Remove markdown code fences some models wrap JSON in."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object in a model response."""
    candidate = _strip_fences(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def build_change_context(
    *,
    change: ChangeSet,
    flows: list[AffectedFlow],
    verification: list[Any],
    diff_text: str,
) -> dict[str, Any]:
    """Build the bounded context payload handed to the LLM."""
    flow_payload: list[dict[str, Any]] = []
    for flow in flows[:MAX_FLOWS_IN_PROMPT]:
        flow_payload.append(
            {
                "id": flow.id,
                "entry": flow.entry_label,
                "handler": flow.handler,
                "entry_kind": flow.entry_kind,
                "nodes": [
                    {
                        "id": str(step.get("id") or ""),
                        "kind": str(step.get("kind") or ""),
                        "name": str(step.get("detail") or step.get("name") or ""),
                        "file": str(step.get("file") or ""),
                    }
                    for step in flow.steps[:MAX_NODES_PER_FLOW]
                ],
            }
        )

    return {
        "version": "v1",
        "change": {
            "base": change.base,
            "files": [
                {
                    "path": item.path,
                    "change_type": item.change_type,
                    "role": item.role,
                    "added_lines": item.added_lines,
                    "removed_lines": item.removed_lines,
                }
                for item in change.files[:40]
            ],
            "symbols": [
                {
                    "id": item.id,
                    "file": item.file,
                    "name": item.qualified_name or item.name,
                    "kind": item.kind,
                    "lines": f"{item.start_line}-{item.end_line}",
                }
                for item in change.symbols[:40]
            ],
        },
        "affected_flows": flow_payload,
        "existing_verification": [
            {
                "statement": getattr(item, "statement", ""),
                "kind": getattr(item, "kind", ""),
                "status": getattr(item, "status", ""),
            }
            for item in verification[:25]
        ],
        "diff": diff_text[:MAX_DIFF_CHARS],
    }


def build_code_review_context(
    *,
    change: ChangeSet,
    diff_text: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Deterministic-only context for the independent code-review branch.

    Structurally incapable of carrying a system-analysis conclusion: it takes
    no flows, no impacts, no boundaries, no semantic analysis, no verification
    and no verdict, so independence is enforced by this signature rather than
    by reviewer discipline. Code review answers "does this patch introduce a
    defect?" — a question about the changed code itself — and a semantic
    conclusion from the other branch could only bias that judgement or, worse,
    launder an inferred impact into something that reads like a defect.

    Every field is a deterministic fact: git's own diff and hunks, and the
    changed-symbol attribution the impact branch also consumes. `changed_region`
    reuses `changed_region_source`, so the model sees the source that actually
    changed rather than the head of a large symbol, and a change in attached
    declaration metadata (a decorator, a Rust outer attribute) is inside the
    region exactly as attribution defined it.
    """
    files: list[dict[str, Any]] = []
    hunks_by_file: dict[str, list[tuple[int, int]]] = {}
    for item in change.files[:40]:
        ranges = [(hunk.start_line, hunk.end_line) for hunk in item.hunks]
        hunks_by_file.setdefault(item.path, []).extend(ranges)
        files.append({
            "path": item.path,
            "change_type": item.change_type,
            "role": item.role,
            "added_lines": item.added_lines,
            "removed_lines": item.removed_lines,
            "changed_ranges": [[low, high] for low, high in ranges],
        })

    symbols: list[dict[str, Any]] = []
    for position, item in enumerate(change.symbols[:40]):
        ranges = hunks_by_file.get(item.file, [])
        entry: dict[str, Any] = {
            "file": item.file,
            "name": item.qualified_name or item.name,
            "kind": item.kind,
            "language": item.language or "",
            "start_line": item.start_line,
            "end_line": item.end_line,
            "changed_line_ranges": [[low, high] for low, high in ranges],
        }
        if repo_root is not None and position < MAX_CODE_REVIEW_REGIONS and ranges:
            region = changed_region_source(
                {
                    "file": item.file,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "language": item.language or "",
                },
                ranges,
                repo_root=repo_root,
                context_lines=CODE_REVIEW_REGION_CONTEXT_LINES,
                max_lines=CODE_REVIEW_REGION_MAX_LINES,
                max_chars=CODE_REVIEW_REGION_MAX_CHARS,
            )
            if region:
                entry["changed_region"] = region
        symbols.append(entry)

    return {
        "version": "v1",
        "change": {"base": change.base, "files": files, "symbols": symbols},
        "diff": diff_text[:MAX_DIFF_CHARS],
    }


def _bounded_prompt(header: str, context: dict[str, Any]) -> str:
    """Serialize a prompt, shrinking the diff first when over budget."""
    payload = dict(context)
    prompt = header + "\nContext:\n" + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt

    diff = str(payload.get("diff") or "")
    overflow = len(prompt) - MAX_PROMPT_CHARS
    payload["diff"] = diff[: max(0, len(diff) - overflow - 200)] + "\n... [truncated]"
    prompt = header + "\nContext:\n" + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt

    payload["affected_flows"] = payload.get("affected_flows", [])[:3]
    prompt = header + "\nContext:\n" + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return prompt[:MAX_PROMPT_CHARS]


_CODE_FINDINGS_HEADER = (
    "You are reviewing a backend code change for concrete implementation defects.\n"
    "The one question you answer is: does this patch introduce a defect?\n"
    "You are given only the change itself — the diff, the changed files and symbols, and the "
    "source of the changed regions. Reason from that alone.\n"
    "Report a finding only for a defect in one of these classes:\n"
    "- null/nil/undefined failure\n"
    "- wrong conditional or inverted branch\n"
    "- off-by-one or bounds error\n"
    "- incorrect arithmetic or units\n"
    "- loop that may not terminate, or runs away\n"
    "- missing or incorrect error handling\n"
    "- resource lifecycle error (leak, use-after-close, missing cleanup)\n"
    "- concurrency or locking defect visible in the supplied evidence\n"
    "- async ordering error\n"
    "- transaction misuse\n"
    "- state-machine inconsistency\n"
    "- authorization or security weakening\n"
    "- incorrect API usage visible in the supplied code\n"
    "Never report: style, naming, formatting, documentation, cleanup or refactor suggestions, "
    "generic performance speculation, 'add tests', 'handle edge cases', or any risk you cannot "
    "tie to a concrete failure scenario.\n"
    "Rules:\n"
    "- Every finding MUST cite a `file` from change.files and a `line` that is inside one of that "
    "file's changed ranges. A defect in unchanged code is out of scope unless this change is what "
    "makes it fail.\n"
    "- `impact` must state a concrete failure scenario: the input or state, and the resulting "
    "wrong behavior. Not 'this could be risky'.\n"
    "- `evidence_snippet` must be copied verbatim from the supplied diff or changed region.\n"
    "- Do not invent files, symbols, or behavior not visible in the provided context.\n"
    "Default to silence. Most changes contain no defect; returning an empty list is the correct, "
    "expected answer, not a failure.\n"
    "Return strict JSON only:\n"
    '{"version":"v1","findings":[{"severity":"P0|P1|P2|P3","title":"...","file":"...","line":123,'
    '"explanation":"...","impact":"...","suggested_fix":"...","evidence_snippet":"..."}]}\n'
    'With nothing to report: {"version":"v1","findings":[]}'
)

_GAPS_HEADER = (
    "You are identifying verification obligations for a backend change. Sydes has already computed "
    "the diff, the affected system flows, and the existing verification it located.\n"
    "Propose externally meaningful system behaviors that this change may affect and for which the "
    "provided existing_verification shows no evidence.\n"
    "Rules:\n"
    "- Each behavior must be observable at a system boundary (HTTP response, database state, emitted "
    "event, downstream consumer effect), not an internal implementation detail.\n"
    "- Never propose generic items such as 'add more tests', 'handle edge cases', or 'improve coverage'.\n"
    "- Every gap MUST reference at least one `related_node_ids` value taken from affected_flows[].nodes[].id.\n"
    "- `why` must explain how the change could alter that behavior.\n"
    "- If existing verification already covers a behavior, do not list it.\n"
    "Return strict JSON only:\n"
    '{"version":"v1","gaps":[{"behavior":"...","why":"...","related_node_ids":["..."],'
    '"related_flow_ids":["..."]}]}'
)


def _normalize_for_containment(text: str) -> str:
    """Collapse whitespace so a snippet quoted with different indentation
    still matches its source exactly. Deliberately not fuzzy: this only
    removes formatting variance, never allows a near-match."""
    return " ".join(text.split())


def _changed_ranges_by_file(context: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    """Per-file changed line ranges, from the code-review context.

    Falls back to a symbol's own `changed_line_ranges` so a context built by
    an older/other builder (which carries no per-file `changed_ranges`) still
    yields the same attributable set rather than silently admitting every line.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    change = context.get("change", {})
    for item in change.get("files", []) or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        for pair in item.get("changed_ranges", []) or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                ranges.setdefault(path, []).append((int(pair[0]), int(pair[1])))
    for item in change.get("symbols", []) or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file") or "")
        for pair in item.get("changed_line_ranges", []) or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                ranges.setdefault(path, []).append((int(pair[0]), int(pair[1])))
    return ranges


def _validate_findings(raw: dict[str, Any], context: dict[str, Any]) -> tuple[list[CodeFinding], list[str]]:
    """Validate LLM code findings against the supplied change context.

    Three deterministic gates, in order of how much they protect a reader:

    - **file** must be one this change touched (pre-existing rule);
    - **line** must fall inside a changed range for that file. Code review's
      whole question is "does this *patch* introduce a defect", so a finding
      on unchanged code elsewhere in the file is out of scope by construction,
      not merely unlikely. The ranges come from the diff's own hunks, widened
      by the same attribution span that decided a symbol changed — so a defect
      introduced by a changed decorator or attribute still lands;
    - **evidence_snippet** must be verbatim-present (whitespace-normalized) in
      the diff or a changed region. A snippet is a quotation; an unverifiable
      one is dropped rather than rendered as `diff_hunk`, which would present
      invented text as if it came from the patch. The finding itself survives,
      because the snippet is optional supporting detail, not the grounding.

    Sorting happens before the `MAX_FINDINGS` cap so a late P0 is never
    discarded in favour of an earlier P3.
    """
    warnings: list[str] = []
    change = context.get("change", {})
    known_files = {
        item["path"] for item in change.get("files", []) if isinstance(item, dict) and "path" in item
    }
    ranges_by_file = _changed_ranges_by_file(context)
    haystack = _normalize_for_containment(
        " ".join([
            str(context.get("diff") or ""),
            *(
                str(item.get("changed_region") or "")
                for item in change.get("symbols", []) or []
                if isinstance(item, dict)
            ),
        ])
    )
    candidates: list[CodeFinding] = []

    for position, item in enumerate(raw.get("findings", [])):
        if not isinstance(item, dict):
            continue
        file_path = item.get("file")
        if not isinstance(file_path, str) or file_path not in known_files:
            warnings.append(f"Rejected code finding referencing unknown file: {file_path!r}")
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            warnings.append("Rejected code finding with no title.")
            continue

        line = item.get("line")
        line_value = int(line) if isinstance(line, int | float) and int(line) > 0 else None
        file_ranges = ranges_by_file.get(file_path, [])
        if file_ranges:
            if line_value is None:
                warnings.append(
                    f"Rejected code finding with no line in a changed file: {title[:80]!r}"
                )
                continue
            if not any(low <= line_value <= high for low, high in file_ranges):
                warnings.append(
                    f"Rejected code finding at {file_path}:{line_value} — outside every "
                    "changed range for that file."
                )
                continue

        severity = str(item.get("severity") or "").upper()
        if severity not in _SEVERITIES:
            warnings.append(
                f"Code finding {title[:80]!r} supplied invalid severity "
                f"{item.get('severity')!r}; normalized to {_DEFAULT_SEVERITY}."
            )
            severity = _DEFAULT_SEVERITY

        snippet = item.get("evidence_snippet")
        snippet_text: str | None = None
        if isinstance(snippet, str) and snippet.strip():
            if _normalize_for_containment(snippet) in haystack:
                snippet_text = snippet[:300]
            else:
                warnings.append(
                    f"Dropped unverifiable evidence snippet on code finding {title[:80]!r}: "
                    "not found verbatim in the diff or changed regions."
                )

        candidates.append(
            CodeFinding(
                id=f"finding-{position + 1}",
                severity=severity,
                title=title[:200],
                file=file_path,
                line=line_value,
                explanation=str(item.get("explanation") or "").strip() or None,
                impact=str(item.get("impact") or "").strip() or None,
                suggested_fix=str(item.get("suggested_fix") or "").strip() or None,
                source="llm",
                evidence=[EvidenceRef(file=file_path, label="diff_hunk", snippet=snippet_text)],
            )
        )

    candidates.sort(key=lambda finding: _SEVERITY_ORDER.get(finding.severity, 9))
    if len(candidates) > MAX_FINDINGS:
        warnings.append(
            f"Truncated {len(candidates)} code finding(s) to the {MAX_FINDINGS} most severe."
        )
    return candidates[:MAX_FINDINGS], warnings


_GENERIC_GAP_MARKERS = (
    "add more test",
    "add tests",
    "increase coverage",
    "improve coverage",
    "handle edge case",
    "edge cases",
    "write unit test",
    "more testing",
)


def _validate_gaps(
    raw: dict[str, Any],
    context: dict[str, Any],
    covered_flow_ids: set[str],
) -> tuple[list[VerificationGap], list[str]]:
    """Validate LLM verification gaps against known flow node ids."""
    warnings: list[str] = []
    node_ids: set[str] = set()
    flow_ids: set[str] = set()
    for flow in context.get("affected_flows", []):
        if not isinstance(flow, dict):
            continue
        flow_ids.add(str(flow.get("id")))
        for node in flow.get("nodes", []):
            if isinstance(node, dict) and isinstance(node.get("id"), str):
                node_ids.add(node["id"])

    gaps: list[VerificationGap] = []
    for position, item in enumerate(raw.get("gaps", [])):
        if not isinstance(item, dict):
            continue
        behavior = str(item.get("behavior") or "").strip()
        if not behavior:
            continue
        lowered = behavior.lower()
        if any(marker in lowered for marker in _GENERIC_GAP_MARKERS):
            warnings.append(f"Rejected generic verification gap: {behavior[:80]!r}")
            continue
        related_nodes = [
            value
            for value in item.get("related_node_ids", [])
            if isinstance(value, str) and value in node_ids
        ]
        if not related_nodes:
            warnings.append(f"Rejected verification gap without a known graph node: {behavior[:80]!r}")
            continue
        related_flows = [
            value
            for value in item.get("related_flow_ids", [])
            if isinstance(value, str) and value in flow_ids
        ]
        gaps.append(
            VerificationGap(
                id=f"gap-{position + 1}",
                behavior=behavior[:300],
                why=str(item.get("why") or "").strip() or None,
                related_node_ids=related_nodes[:6],
                related_flow_ids=related_flows[:6],
                existing_evidence_found=any(flow in covered_flow_ids for flow in related_flows),
                status=VERIFICATION_UNVERIFIED,
                source="llm",
                evidence=[
                    EvidenceRef(file=node.split(":")[1] if node.count(":") >= 2 else node, label="graph_node")
                    for node in related_nodes[:3]
                ],
            )
        )
        if len(gaps) >= MAX_GAPS:
            break
    return gaps, warnings


def _run(client: LLMClient, prompt: str) -> dict[str, Any]:
    """Call the model and parse a strict JSON object response."""
    response = client.generate(LLMRequest(prompt=prompt, temperature=0))
    parsed = _extract_json_object(response.text)
    if parsed is None:
        raise LLMClientError("model output parse failure: verify-change output was not valid JSON.")
    return parsed


def generate_code_findings(
    *,
    context: dict[str, Any],
    model_spec: str | None = None,
    llm_client: LLMClient | None = None,
) -> tuple[list[CodeFinding], list[str]]:
    """Run the code-findings LLM pass over the bounded change context."""
    client = llm_client or create_default_llm_client(model_spec=model_spec, stage="code_review")
    prompt = _bounded_prompt(_CODE_FINDINGS_HEADER, context)
    raw = _run(client, prompt)
    findings, warnings = _validate_findings(raw, context)
    warnings.append(f"code_findings_prompt_chars={len(prompt)}")
    return findings, warnings


def generate_verification_gaps(
    *,
    context: dict[str, Any],
    covered_flow_ids: set[str],
    model_spec: str | None = None,
    llm_client: LLMClient | None = None,
) -> tuple[list[VerificationGap], list[str]]:
    """Run the verification-gap LLM pass over the bounded change context."""
    client = llm_client or create_default_llm_client(model_spec=model_spec, stage="verification_gaps")
    prompt = _bounded_prompt(_GAPS_HEADER, context)
    raw = _run(client, prompt)
    gaps, warnings = _validate_gaps(raw, context, covered_flow_ids)
    warnings.append(f"verification_gaps_prompt_chars={len(prompt)}")
    return gaps, warnings
