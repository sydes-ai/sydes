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

from sydes.core.models import EvidenceRef
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
    VerificationItem,
)

MAX_DIFF_CHARS = 14_000
MAX_PROMPT_CHARS = 26_000
MAX_FLOWS_IN_PROMPT = 8
MAX_NODES_PER_FLOW = 14
MAX_FINDINGS = 10
MAX_GAPS = 8

_SEVERITIES = {"P0", "P1", "P2", "P3"}


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
    verification: list[VerificationItem],
    diff_text: str,
) -> dict[str, Any]:
    """Build the bounded context payload handed to the LLM."""
    flow_payload: list[dict[str, Any]] = []
    for flow in flows[:MAX_FLOWS_IN_PROMPT]:
        flow_payload.append(
            {
                "id": flow.id,
                "entry": flow.entry_label,
                "entry_kind": flow.entry_kind,
                "nodes": [
                    {
                        "id": node.id,
                        "kind": node.kind,
                        "name": node.name,
                        "file": node.file,
                        "changed": node.changed,
                    }
                    for node in flow.nodes[:MAX_NODES_PER_FLOW]
                ],
                "edges": [
                    {"from": edge.source, "to": edge.target, "kind": edge.kind}
                    for edge in flow.edges[: MAX_NODES_PER_FLOW * 2]
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
                "name": item.name,
                "file": item.file,
                "status": item.status,
                "covers": item.covers,
                "flows": item.related_flow_ids,
            }
            for item in verification[:25]
        ],
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
    "You are reviewing a backend code change. Sydes has already computed the diff and "
    "the affected system flows; do not restate them.\n"
    "Report only concrete defects introduced or exposed by this diff.\n"
    "Rules:\n"
    "- Every finding MUST cite a `file` that appears in change.files and a `line` inside the diff.\n"
    "- Do not invent files, symbols, or behavior that is not visible in the provided context.\n"
    "- Do not report style, formatting, naming, or 'add more tests' suggestions.\n"
    "- If the diff contains no real defect, return an empty findings list.\n"
    "Return strict JSON only:\n"
    '{"version":"v1","findings":[{"severity":"P0|P1|P2|P3","title":"...","file":"...","line":123,'
    '"explanation":"...","impact":"...","suggested_fix":"...","evidence_snippet":"..."}]}'
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


def _validate_findings(raw: dict[str, Any], context: dict[str, Any]) -> tuple[list[CodeFinding], list[str]]:
    """Validate LLM code findings against the supplied change context."""
    warnings: list[str] = []
    known_files = {
        item["path"] for item in context.get("change", {}).get("files", []) if isinstance(item, dict)
    }
    findings: list[CodeFinding] = []

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
        severity = str(item.get("severity") or "P2").upper()
        if severity not in _SEVERITIES:
            severity = "P2"
        line = item.get("line")
        line_value = int(line) if isinstance(line, int | float) and int(line) > 0 else None
        snippet = item.get("evidence_snippet")
        findings.append(
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
                evidence=[
                    EvidenceRef(
                        file=file_path,
                        label="diff_hunk",
                        snippet=str(snippet)[:300] if isinstance(snippet, str) else None,
                    )
                ],
            )
        )
        if len(findings) >= MAX_FINDINGS:
            break

    severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda item: severity_order.get(item.severity, 9))
    return findings, warnings


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
    client = llm_client or create_default_llm_client(model_spec=model_spec)
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
    client = llm_client or create_default_llm_client(model_spec=model_spec)
    prompt = _bounded_prompt(_GAPS_HEADER, context)
    raw = _run(client, prompt)
    gaps, warnings = _validate_gaps(raw, context, covered_flow_ids)
    warnings.append(f"verification_gaps_prompt_chars={len(prompt)}")
    return gaps, warnings
