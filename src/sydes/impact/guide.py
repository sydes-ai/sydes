"""An LLM that chooses the next investigation step, never the verdict.

`ImpactGuide` answers exactly one question: given an `ImpactQuestion` the
deterministic interpreter could not resolve, which one bounded action should
be tried next? It never sees verifier internals, never touches CBM or a
repository directly, and its answer is not itself evidence — a plausible
`InvestigationDecision` is only a suggestion until `InvestigationExecutor`
(in `investigate.py`) carries it out and reports what was actually found.

Reuses Sydes' existing provider-neutral `LLMClient` — no second provider
system. The client has no structured-output mode, so the contract here is:
ask for JSON in the prompt, then parse and validate strictly. Anything that
does not parse, does not match the schema, or names an action outside
`INVESTIGATION_ACTIONS` raises `GuideError` rather than being coerced into a
best-effort guess — a malformed decision must fail closed, not steer the
loop on a guess about what the model meant.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sydes.impact.models import (
    ACTIONS_REQUIRING_SOUGHT_SYMBOL,
    ACTIONS_REQUIRING_TARGET,
    ACTION_STOP_UNRESOLVED,
    INVESTIGATION_ACTIONS,
    ImpactQuestion,
    InvestigationDecision,
)
from sydes.llm.client import LLMClient, LLMClientError, LLMRequest

#: Maximum characters of model output considered for JSON extraction. A
#: reasoning-heavy response that wanders past this is treated as malformed
#: rather than scanned indefinitely for a stray brace.
_MAX_RESPONSE_CHARS = 8000


class GuideError(RuntimeError):
    """The guide could not produce a usable decision.

    Covers provider failure, a timeout, non-JSON output, a missing/invalid
    field, and an action outside the allowed vocabulary. The caller's only
    correct response is to record it and leave the symbol unresolved — never
    to substitute a guessed decision.
    """


class ImpactGuide(Protocol):
    """Investigates one unresolved impact question.

    Implementations must not know about `AffectedFlow`, obligations, or
    verdicts — only about `ImpactQuestion` and `InvestigationDecision`. This
    keeps a fake guide (for tests) and a real one interchangeable, and keeps
    the guide itself incapable of declaring anything "affected."
    """

    def investigate(self, question: ImpactQuestion) -> InvestigationDecision:
        """Return the next action to try, or raise `GuideError`."""


_SYSTEM_PROMPT = """You are assisting a deterministic code-impact analyzer, not replacing it.

Ground rules:
- You do NOT decide whether any route or entrypoint is affected. That is decided later, only from concrete evidence, not from your answer.
- Your only job is to choose the single most useful next investigation step for a human-auditable tool to carry out.
- You must choose exactly one action from the allowed list below.
- `target`, if given, must be one of the exact names or files listed in the question's partial paths, nearby facts, or candidate entrypoints. Do not invent a name that was not shown to you.
- INSPECT_SYMBOL, INSPECT_ENCLOSING_FUNCTION, and INSPECT_SOURCE_SPAN each check a concrete relationship: "does `target`'s source actually reference `sought_symbol`?" You must also supply `sought_symbol` for these three actions, chosen from `candidate_origins` only — never from `target`'s own vocabulary and never invented. Pick whichever candidate origin is the actual unresolved relationship you are trying to confirm, not just the first one listed.
- Do not assume how any framework, decorator, or library behaves unless the supplied evidence already shows it.
- Prefer the cheapest, most direct structural check before a broader one. Choose STOP_UNRESOLVED if none of the actions seem likely to help, rather than guessing.
- Respond with a single JSON object and nothing else: {"action": "<ACTION>", "target": "<name or empty>", "sought_symbol": "<name from candidate_origins, or empty if not applicable>", "rationale": "<one sentence>"}

Allowed actions: TRACE_CALLERS, TRACE_USAGES, INSPECT_SYMBOL, INSPECT_ENCLOSING_FUNCTION, INSPECT_SOURCE_SPAN, FIND_DECORATOR_REFERENCES, FIND_SIGNATURE_REFERENCES, INSPECT_NEARBY_ENTRYPOINTS, STOP_UNRESOLVED."""


def build_guide_prompt(question: ImpactQuestion) -> str:
    """Render one `ImpactQuestion` as the user turn of the guide prompt."""
    lines = [
        f"changed_symbol: {question.changed_symbol}",
        f"qualified_name: {question.qualified_name or '(unknown)'}",
        f"file: {question.file}",
        f"reason_unresolved: {question.reason}",
        f"remaining_investigation_budget: {question.remaining_budget}",
    ]
    if question.partial_paths:
        lines.append("partial_paths_already_found:")
        lines.extend(f"  - {item}" for item in question.partial_paths)
    if question.nearby_facts:
        lines.append("nearby_graph_facts:")
        lines.extend(f"  - {item}" for item in question.nearby_facts)
    if question.candidate_entrypoints:
        lines.append("candidate_entrypoints:")
        lines.extend(f"  - {item}" for item in question.candidate_entrypoints)
    if question.candidate_origins:
        lines.append("candidate_origins (legal sought_symbol values):")
        lines.extend(f"  - {item}" for item in question.candidate_origins)
    if question.source_context:
        lines.append("bounded_source_context:")
        lines.append(question.source_context)
    lines.append("\nWhich single action should be tried next?")
    return "\n".join(lines)


def _extract_json_object(text: str) -> Any | None:
    """Best-effort parse for a single JSON object in model output.

    Deliberately narrower than free-form JSON extraction elsewhere in Sydes:
    the guide contract asks for exactly one object, so a response containing
    a list or several objects is already off-contract and is not rescued.
    """
    stripped = text.strip()[:_MAX_RESPONSE_CHARS]
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_guide_decision(text: str) -> InvestigationDecision:
    """Parse and strictly validate one guide response.

    Raises `GuideError` for anything that is not exactly the documented
    shape: a JSON object, an `action` string in `INVESTIGATION_ACTIONS`, and
    (when the action requires one) a non-empty `target`. There is no
    best-effort coercion here on purpose — a guide response close to valid
    but not valid is exactly the case a fail-closed contract exists for.
    """
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise GuideError("guide output was not a single JSON object")

    raw_action = payload.get("action")
    if not isinstance(raw_action, str):
        raise GuideError("guide output missing string 'action'")
    action = raw_action.strip().lower()
    if action not in INVESTIGATION_ACTIONS:
        raise GuideError(f"guide chose an unsupported action: {raw_action!r}")

    raw_target = payload.get("target", "")
    target = raw_target.strip() if isinstance(raw_target, str) else ""
    if action in ACTIONS_REQUIRING_TARGET and not target:
        raise GuideError(f"action {action!r} requires a non-empty target")

    raw_sought = payload.get("sought_symbol", "")
    sought_symbol = raw_sought.strip() if isinstance(raw_sought, str) else ""
    if action in ACTIONS_REQUIRING_SOUGHT_SYMBOL and not sought_symbol:
        raise GuideError(f"action {action!r} requires a non-empty sought_symbol")

    raw_rationale = payload.get("rationale", "")
    rationale = raw_rationale.strip() if isinstance(raw_rationale, str) else ""

    raw_parameters = payload.get("parameters", {})
    parameters = raw_parameters if isinstance(raw_parameters, dict) else {}

    return InvestigationDecision(
        action=action, target=target, sought_symbol=sought_symbol,
        rationale=rationale, parameters=parameters,
    )


class LLMImpactGuide:
    """The real guide: one LLM call per `investigate`, strictly validated."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def investigate(self, question: ImpactQuestion) -> InvestigationDecision:
        prompt = build_guide_prompt(question)
        try:
            response = self._client.generate(
                LLMRequest(prompt=prompt, system=_SYSTEM_PROMPT, temperature=0.0)
            )
        except LLMClientError as exc:
            raise GuideError(f"guide provider failed: {exc}") from exc
        return parse_guide_decision(response.text)


class FixedImpactGuide:
    """A guide that always answers `STOP_UNRESOLVED`.

    Used when a guide policy is active but no LLM client could be built (e.g.
    provider unavailable) and the caller still wants a uniform `ImpactGuide`
    to loop over, rather than special-casing "no guide" throughout the
    interpreter.
    """

    def investigate(self, question: ImpactQuestion) -> InvestigationDecision:
        return InvestigationDecision(
            action=ACTION_STOP_UNRESOLVED, rationale="no guide provider available",
        )
