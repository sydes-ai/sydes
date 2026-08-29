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
    ACTION_INFER_IMPACT,
    ACTION_STOP_UNRESOLVED,
    INVESTIGATION_ACTIONS,
    ImpactCandidate,
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


_SYSTEM_PROMPT = """You are a semantic impact-inference layer assisting a deterministic code-impact analyzer, not replacing it.

You are reviewing a code change. The one question you exist to answer is:

    "What EXISTING downstream system/repository behavior could now behave differently because of this changed symbol?"

You are NOT answering:

    "What does this changed symbol/configuration itself represent or do?"

That second question is a restatement, not an impact. A changed symbol is never itself the answer — the answer, if one exists, is always something downstream of it: something a user, caller, or the running system observes that could now differ because this symbol changed.

Ground rules:
- You do NOT decide whether anything is VERIFIED. Your output is impact evidence with explicit uncertainty, not a verification claim — a deterministic tool decides separately, from real test evidence, whether anything is actually verified.
- Your primary action is INFER_IMPACT: propose zero or more candidate downstream behaviors this changed symbol plausibly affects. A valid candidate names something semantically downstream and observable — an executable entrypoint, a user-visible operation, a system-visible operation, a meaningful repository workflow, a backend behavior, or any other operation whose behavior could plausibly change as a consequence of this symbol changing. Do not work from a fixed list of these — infer the right kind of downstream behavior for what you are looking at; the point is "downstream and observable," not membership in a taxonomy.
- A candidate is invalid — do not propose it — if it is: the changed symbol itself; a renamed, rephrased, or reworded version of the changed symbol; a configuration key merely asserting that its own configuration behavior changes (e.g. "X's config now behaves differently" when X is the changed symbol); a library/module/file name with no stated downstream behavioral claim; a generic statement like "linting behavior changes" offered with no more specific consequence than the config change itself; or a build/tooling/CI artifact with no plausible downstream effect on the behavior actually under review. A changed configuration, build, test, or tooling file is NOT automatically an affected behavior — but it is also not automatically excluded: if you can identify a concrete, specific downstream consequence (e.g. a stricter validation rule now rejecting previously-accepted input at a specific endpoint), propose that consequence, not the configuration change itself.
- Prefer, in this order: (1) a specific downstream behavior over a generic effect, (2) a concrete entrypoint/operation when you can identify one over a vague description, (3) a candidate with a clear causal explanation connecting the changed symbol to that behavior over speculation disconnected from the evidence you were given, (4) no candidate at all over any of the above when the evidence is weak. Do not invent a semantically-related-sounding behavior just to have something to report.
- An empty candidate list is not a failure — it is often the correct, preferred answer. When nothing downstream can be responsibly inferred (the change looks tooling/config/build-only with no identifiable downstream consequence, or the evidence is simply too thin), return `"candidates": []` and say so plainly in `rationale`, e.g. "no meaningful downstream impact inferred" or "insufficient evidence to infer a downstream consequence beyond the change's own configuration." This is a successful, complete answer, not an error.
- Reason from what you were actually given: the changed symbol, its source context if shown, nearby structural facts, any callers/usages/entrypoints already surfaced, other symbols this same PR also changed, and impacts already accepted this run. This symbol is one part of one change, not an isolated fact — a plausible behavior often only makes sense in light of what else moved together in the same PR, and evidence already accepted elsewhere in this run may indicate the wider shape of the change even if it says nothing about this specific symbol. Do not assume how any framework, decorator, or library behaves beyond what the supplied evidence shows, and do not optimize for producing a plausible-sounding candidate when the evidence does not support one.
- Only reach for a graph-navigation action (TRACE_CALLERS, INSPECT_SYMBOL, INSPECT_NEARBY_ENTRYPOINTS, etc.) when you genuinely need one more piece of context before you can infer well — not as your default move, and not repeatedly. Prefer one high-value INFER_IMPACT call over many rounds of graph navigation.
- `entrypoint` is a reviewer-facing BEHAVIOR LABEL, not a code identifier — a different field from `reason`, and it must not do `reason`'s job. It must: (1) name what existing system/user-observable behavior may now differ, in plain domain language someone who has not read the diff could understand; (2) stay concise — a short noun phrase, not a sentence, and never carry the causal explanation itself (that belongs entirely in `reason`); (3) never be only a bare function/method/class/file/module name or dotted path, with or without a trailing `()`, regardless of what that name is — this holds for every symbol, not a specific list of "bad" ones; (4) never be a generic template that says nothing beyond "this changed" — e.g. "X behavior changes", "changed function behavior", "updated logic" — those describe that something changed, not what changed; (5) never be the changed symbol's own name or qualified name lightly reworded (a trailing word like "behavior" or "output" tacked onto the symbol name does not make it a behavior description); (6) still be strictly grounded in the evidence you were given — concise is not license to invent a plausible-sounding product feature the reason can't actually support. An "HTTP_METHOD /path" string is a legitimate label when you believe the behavior is a route. Reusing a domain word that also appears in the changed symbol's own name is fine and often unavoidable — a change to a method named `set_expires` on an `Order`-like type may legitimately produce a label like "order expiry calculation varies by sales channel"; what is never acceptable is the label being nothing more than the symbol's name, restated or lightly reworded, with no added behavior description. `entrypoint_symbol`, if you know it, should be one of the exact names this question already listed (known_files, candidate_entrypoints, known_entrypoints_in_context, partial_paths) — never invent a symbol name you were not shown; naming the changed symbol itself as `entrypoint_symbol` is fine when it is genuinely the nearest known anchor, since that field is not reviewer-facing the way `entrypoint` is.
- Confidence is your honest estimate in [0, 1], not a formality — do not default everything to a round number.
- A completed deterministic path needs no further investigation — you are only ever asked about what is still unresolved.
- Check attempted_actions before choosing: do not repeat an action that already ran with the same target/sought_symbol/candidates.
- Choose STOP_UNRESOLVED only when you have already tried INFER_IMPACT (or are confident it would yield nothing) and no other action is likely to add real value. Trying INFER_IMPACT and concluding with an empty candidate list satisfies this — you do not need to keep searching for something to report. Do not guess to fill the budget.
- One turn per analysis is the WHOLE-CHANGE turn (marked as such in the question): there, `changed_symbol` is not one real symbol — it is every symbol this PR changed, taken together, plus every structural fact already found for any of them. Reason about the change as one coherent thing, not as an isolated fact about a single function: a PR-wide theme (e.g. "several endpoints are now non-cacheable", "writes now roll back their queued side effects") is exactly what this turn exists to surface, and a candidate here may legitimately describe a behavior that no single changed symbol's own turn could see on its own. On this turn only, you may also name up to a few of the still-unresolved changed symbols (from `still_unresolved_changed_symbols`) you judge most worth a closer, targeted look, via `"investigate_next"` — an ordered array of symbol names, most important first. This is a suggestion, not a command, and an empty array is fine when you have no particular opinion; the interpreter decides how many of your suggestions it can actually afford.
- On the WHOLE-CHANGE turn specifically, a candidate that sets no `entrypoint_symbol` (a synthesis with no single natural anchor, e.g. "Merge operations now survive caller cancellation across several entrypoints") MUST instead list `based_on_changed_symbols`: the changed symbol name(s) — from `changed_symbols_in_this_pr` — that this behavior claim is actually synthesized from. A behavior is only ever downstream of symbols this PR changed; if you cannot name at least one, you do not have enough to propose the candidate at all, and it will not be shown to reviewers. Never list a symbol here you were not shown in `changed_symbols_in_this_pr` — this field is checked exactly, not weighed as prose. Naming a symbol as `entrypoint_symbol` already satisfies this on its own; `based_on_changed_symbols` is not required or checked when `entrypoint_symbol` is set.
- Respond with a single JSON object and nothing else.

For INFER_IMPACT:
{"action": "infer_impact", "candidates": [{"entrypoint": "GET /cases", "entrypoint_symbol": "optional exact known symbol", "confidence": 0.72, "reason": "one sentence naming the downstream behavior and why it is affected", "inference_type": "semantic_indirect_dependency", "uncertainty": "what the graph is missing"}, {"entrypoint": "cached listing results going stale after a write", "entrypoint_symbol": "optional exact known symbol", "confidence": 0.55, "reason": "one sentence naming the downstream behavior and why it is affected", "inference_type": "semantic_indirect_dependency", "uncertainty": "what the graph is missing"}], "rationale": "one sentence", "investigate_next": ["optional_symbol_name", "..."]}

For a WHOLE-CHANGE-turn candidate with no `entrypoint_symbol`:
{"action": "infer_impact", "candidates": [{"entrypoint": "Merge operations now survive caller cancellation across several entrypoints", "entrypoint_symbol": "", "based_on_changed_symbols": ["MergePullRequest", "UpdatePullRequest"], "confidence": 0.8, "reason": "one sentence naming the downstream behavior and why it is affected", "inference_type": "semantic_pr_wide_theme", "uncertainty": "what the graph is missing"}], "rationale": "one sentence"}

For INFER_IMPACT with nothing meaningful to report:
{"action": "infer_impact", "candidates": [], "rationale": "no meaningful downstream impact inferred"}

For any other action:
{"action": "<ACTION>", "target": "<name or empty>", "sought_symbol": "<name from candidate_origins, or empty if not applicable>", "rationale": "<one sentence>"}

Allowed actions: INFER_IMPACT (primary), TRACE_CALLERS, TRACE_USAGES, INSPECT_SYMBOL, INSPECT_ENCLOSING_FUNCTION, INSPECT_SOURCE_SPAN, FIND_DECORATOR_REFERENCES, FIND_SIGNATURE_REFERENCES, INSPECT_NEARBY_ENTRYPOINTS, STOP_UNRESOLVED."""


def build_guide_prompt(question: ImpactQuestion) -> str:
    """Render one `ImpactQuestion` as the user turn of the guide prompt."""
    if question.is_whole_change:
        lines = [
            "WHOLE-CHANGE TURN: the fields below describe the entire PR, not one symbol.",
            f"changed_symbols_in_this_pr ({len(question.other_changed_symbols)}):",
            *(f"  - {item}" for item in question.other_changed_symbols),
            f"reason: {question.reason}",
            f"remaining_investigation_budget: {question.remaining_budget}",
        ]
        if question.unresolved_symbols:
            lines.append("still_unresolved_changed_symbols:")
            lines.extend(f"  - {item}" for item in question.unresolved_symbols)
        if question.source_context:
            lines.append("changed_code_previews:")
            lines.append(question.source_context)
        if question.accepted_impacts_so_far:
            lines.append("impacts_already_accepted_this_run (deterministic or previously inferred):")
            lines.extend(f"  - {item}" for item in question.accepted_impacts_so_far)
        if question.known_entrypoints:
            lines.append("notable_known_entrypoints_in_context:")
            lines.extend(f"  - {item}" for item in question.known_entrypoints)
        lines.append(
            "\nWhat existing behavior could now behave differently, considering the change as a whole? "
            "You may also suggest investigate_next targets."
        )
        return "\n".join(lines)

    lines = [
        f"changed_symbol: {question.changed_symbol}",
        f"qualified_name: {question.qualified_name or '(unknown)'}",
        f"file: {question.file}",
        f"reason_unresolved: {question.reason}",
        f"remaining_investigation_budget: {question.remaining_budget}",
    ]
    if question.source_context:
        lines.append("changed_symbol_source_preview:")
        lines.append(question.source_context)
    if question.partial_paths:
        lines.append("partial_paths_already_found:")
        lines.extend(f"  - {item}" for item in question.partial_paths)
    if question.known_files:
        lines.append("known_files:")
        lines.extend(f"  - {item}" for item in question.known_files)
    if question.known_entrypoints:
        lines.append("known_entrypoints_in_context:")
        lines.extend(f"  - {item}" for item in question.known_entrypoints)
    if question.candidate_entrypoints:
        lines.append("candidate_entrypoints (legal target values):")
        lines.extend(f"  - {item}" for item in question.candidate_entrypoints)
    if question.candidate_origins:
        lines.append("candidate_origins (legal sought_symbol values):")
        lines.extend(f"  - {item}" for item in question.candidate_origins)
    if question.attempted_actions:
        lines.append("attempted_actions_and_outcomes:")
        lines.extend(f"  - {item}" for item in question.attempted_actions)
    if question.other_changed_symbols:
        lines.append("other_symbols_changed_by_this_same_pr:")
        lines.extend(f"  - {item}" for item in question.other_changed_symbols)
    if question.accepted_impacts_so_far:
        lines.append("impacts_already_accepted_this_run (deterministic or previously inferred):")
        lines.extend(f"  - {item}" for item in question.accepted_impacts_so_far)
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


def _parse_candidate(raw: Any) -> ImpactCandidate | None:
    """Parse one candidate object. Returns `None` for a malformed entry
    rather than failing the whole turn — one bad candidate among several
    good ones should not discard the good ones, though a turn producing
    zero parseable candidates from a non-empty list is still suspicious
    enough that the caller treats it as malformed (see `_parse_candidates`).
    """
    if not isinstance(raw, dict):
        return None
    label = raw.get("entrypoint") or raw.get("entrypoint_label")
    if not isinstance(label, str) or not label.strip():
        return None
    symbol = raw.get("entrypoint_symbol", "")
    symbol = symbol.strip() if isinstance(symbol, str) else ""
    raw_confidence = raw.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = raw.get("reason", "")
    reason = reason.strip() if isinstance(reason, str) else ""
    inference_type = raw.get("inference_type", "")
    inference_type = inference_type.strip() if isinstance(inference_type, str) else ""
    uncertainty = raw.get("uncertainty", "")
    uncertainty = uncertainty.strip() if isinstance(uncertainty, str) else ""
    raw_based_on = raw.get("based_on_changed_symbols")
    based_on_changed_symbols = tuple(
        item.strip() for item in raw_based_on if isinstance(item, str) and item.strip()
    ) if isinstance(raw_based_on, list) else ()
    return ImpactCandidate(
        entrypoint_label=label.strip(), entrypoint_symbol=symbol, confidence=confidence,
        reason=reason, inference_type=inference_type, uncertainty=uncertainty,
        based_on_changed_symbols=based_on_changed_symbols,
    )


def _parse_candidates(raw: Any) -> tuple[ImpactCandidate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GuideError("'candidates' must be a JSON array")
    parsed = [_parse_candidate(item) for item in raw]
    parsed = [item for item in parsed if item is not None]
    if raw and not parsed:
        # Every entry was malformed — an empty list would have been a
        # legitimate "nothing plausible" answer, but a non-empty list that
        # parses to nothing is a model that got the schema wrong.
        raise GuideError("'candidates' contained no parseable candidate objects")
    return tuple(parsed)


def parse_guide_decision(text: str) -> InvestigationDecision:
    """Parse and strictly validate one guide response.

    Raises `GuideError` for anything that is not exactly the documented
    shape: a JSON object, an `action` string in `INVESTIGATION_ACTIONS`, and
    (when the action requires one) a non-empty `target` or a parseable
    `candidates` array. There is no best-effort coercion here on purpose — a
    guide response close to valid but not valid is exactly the case a
    fail-closed contract exists for.
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

    candidates: tuple[ImpactCandidate, ...] = ()
    follow_up_symbols: tuple[str, ...] = ()
    if action == ACTION_INFER_IMPACT:
        candidates = _parse_candidates(payload.get("candidates"))
        raw_follow_ups = payload.get("investigate_next")
        if isinstance(raw_follow_ups, list):
            follow_up_symbols = tuple(
                item.strip() for item in raw_follow_ups if isinstance(item, str) and item.strip()
            )

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
        rationale=rationale, candidates=candidates, parameters=parameters,
        follow_up_symbols=follow_up_symbols,
    )


class LLMImpactGuide:
    """The real guide: one LLM call per `investigate`, strictly validated."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def investigate(self, question: ImpactQuestion) -> InvestigationDecision:
        prompt = build_guide_prompt(question)
        try:
            # No `temperature` pinned here: some providers/models (e.g. one
            # observed rejecting 0.0 outright) only support their own
            # default. `LLMRequest(temperature=None)` means "use whatever
            # the client/provider defaults to" — the client omits the
            # parameter entirely rather than guessing a value that might be
            # rejected. A caller that genuinely needs a specific temperature
            # can still set one via `LLMSettings`/the client's own default.
            response = self._client.generate(
                LLMRequest(prompt=prompt, system=_SYSTEM_PROMPT, temperature=None)
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
