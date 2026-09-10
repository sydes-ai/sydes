"""Adversarial verification of AI-recovered paths.

Two layers, cheapest first:

1. A deterministic evidence-existence check (no LLM call): every cited
   `(file, line_start, line_end)` must actually exist and be readable. A
   path citing a file/line range that isn't there is rejected immediately —
   no amount of a confident-sounding verifier response should be needed to
   catch a fabricated citation.
2. For paths that pass (1), one LLM adversarial pass asking exactly the
   question this prototype cares about: can every bridge be supported by
   the cited evidence, is any runtime connection merely assumed, is there
   contradictory evidence, does the claimed entrypoint actually reach the
   changed behavior? A path not explicitly accepted is rejected — silence
   or an ambiguous answer is not acceptance.

`sydes.recovery.agent.recover` allows at most one retry after a rejection
here, then one more verification pass — never an unbounded loop.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sydes.llm.client import LLMClient, LLMClientError, LLMRequest
from sydes.recovery.schema import (
    PROVENANCE_AI_RECOVERY,
    PROVENANCE_AI_RECOVERY_EXHAUSTED,
    RecoveredPath,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_UNRESOLVED,
)
from sydes.recovery.tools import RepoTools

if TYPE_CHECKING:
    from sydes.recovery.agent import RecoveryRunStats

_VERIFIER_SYSTEM_PROMPT = """You are an adversarial verifier for AI-recovered code-impact paths. You did not propose these paths; your job is to try to break them, not to agree with them.

For EACH path below, decide accept or reject by asking:
- Can every bridge (step) in this path be supported by the cited evidence?
- Is any runtime connection merely assumed rather than shown by the evidence?
- Is there any evidence here that actually contradicts the claimed path?
- Does the claimed entrypoint actually reach the changed behavior, per the evidence shown?

Accept only when the answer to the first and last questions is clearly yes, and to the middle two is clearly no. If you are unsure, reject — silence or a hedge is not acceptance.

Respond with a single JSON object and nothing else:
{"verdicts": [{"index": 0, "accept": true, "reason": "one sentence"}, {"index": 1, "accept": false, "reason": "one sentence naming exactly what is missing or contradicted"}]}"""


def _evidence_exists_on_disk(path: RecoveredPath, tools: RepoTools) -> str | None:
    """Returns a rejection reason, or `None` if every evidence citation on
    this path is at least readable at the claimed location."""
    for item in path.evidence:
        content = tools.read_file(item.file, start_line=item.line_start, end_line=item.line_end)
        if content.startswith("ERROR:"):
            return f"cited evidence file {item.file!r} could not be read: {content}"
        if not content.strip():
            return f"cited evidence file {item.file!r} was empty at the claimed location"
    return None


def _build_verifier_prompt(paths: list[RecoveredPath]) -> str:
    lines = ["Paths to verify:"]
    for index, path in enumerate(paths):
        lines.append(f"\n[{index}] entrypoint: {path.entrypoint}")
        for step in path.steps:
            lines.append(f"  step: {step.symbol} in {step.file} -- {step.relationship}")
        for item in path.evidence:
            lines.append(f"  evidence: {item.file}:{item.line_start}-{item.line_end} -- {item.fact}")
    return "\n".join(lines)


def _parse_verdicts(text: str, *, count: int) -> dict[int, tuple[bool, str]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    raw_verdicts = payload.get("verdicts")
    if not isinstance(raw_verdicts, list):
        return {}
    out: dict[int, tuple[bool, str]] = {}
    for item in raw_verdicts:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        accept = item.get("accept")
        reason = item.get("reason", "")
        if not isinstance(index, int) or index < 0 or index >= count or not isinstance(accept, bool):
            continue
        out[index] = (accept, reason if isinstance(reason, str) else "")
    return out


def verify_recovery_result(
    draft: RecoveryResult, *, tools: RepoTools, client: LLMClient, stats: "RecoveryRunStats",
) -> RecoveryResult:
    established = [p for p in draft.recovered_paths if p.status == STATUS_ESTABLISHED]
    if not established:
        return draft

    deterministic_rejections: dict[int, str] = {}
    survivors: list[tuple[int, RecoveredPath]] = []
    for original_index, path in enumerate(draft.recovered_paths):
        if path.status != STATUS_ESTABLISHED:
            continue
        reason = _evidence_exists_on_disk(path, tools)
        if reason is not None:
            deterministic_rejections[original_index] = reason
        else:
            survivors.append((original_index, path))

    llm_verdicts: dict[int, tuple[bool, str]] = {}
    if survivors:
        prompt = _build_verifier_prompt([p for _, p in survivors])
        try:
            response = client.generate(
                LLMRequest(prompt=prompt, system=_VERIFIER_SYSTEM_PROMPT, temperature=None)
            )
        except LLMClientError:
            # Fail closed: a verifier that could not run must not be treated
            # as tacit acceptance.
            llm_verdicts = {}
        else:
            stats.llm_calls += 1
            if response.usage:
                stats.prompt_tokens += response.usage.get("prompt_tokens", 0)
                stats.completion_tokens += response.usage.get("completion_tokens", 0)
            parsed = _parse_verdicts(response.text, count=len(survivors))
            for local_index, (original_index, _path) in enumerate(survivors):
                llm_verdicts[original_index] = parsed.get(
                    local_index, (False, "verifier gave no explicit verdict for this path")
                )

    new_paths: list[RecoveredPath] = []
    for original_index, path in enumerate(draft.recovered_paths):
        if path.status != STATUS_ESTABLISHED:
            new_paths.append(path)
            continue
        if original_index in deterministic_rejections:
            new_paths.append(
                path.model_copy(
                    update={
                        "status": STATUS_UNRESOLVED,
                        "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                        "rejection_reason": deterministic_rejections[original_index],
                    }
                )
            )
            continue
        accept, reason = llm_verdicts.get(original_index, (False, "no verifier verdict recorded"))
        if accept:
            new_paths.append(path.model_copy(update={"provenance": PROVENANCE_AI_RECOVERY}))
        else:
            new_paths.append(
                path.model_copy(
                    update={
                        "status": STATUS_UNRESOLVED,
                        "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                        "rejection_reason": reason or "verifier rejected this path",
                    }
                )
            )

    overall_status = STATUS_ESTABLISHED if any(p.status == STATUS_ESTABLISHED for p in new_paths) else STATUS_UNRESOLVED
    return draft.model_copy(update={"recovered_paths": new_paths, "status": overall_status})
