"""Evidence-checking ONE "likely, not fully established" impact candidate
against the actual repository, before it is ever shown to a reviewer.

EXPERIMENTAL — not wired into the default `verify-change` pipeline yet.
See `scripts/evaluate_likely_impact_evidence.py` for the small evaluation
harness this was built to run against before any integration decision.

This exists because the deterministic impact interpreter's "likely, not
fully established" bucket has a measured, repeatable false-positive
pattern: a method on the same class/file/module as a genuinely-changed
symbol gets proposed as "possibly affected" with no real call-chain
evidence behind it (see a real fork PR run's `GET /dev/model`,
`POST /dev/reload` candidates — neither calls anything that PR changed,
they just live in the same router file as routes that do).

Reuses the exact same ReAct tool-use loop, repo-scoped `RepoTools`, and
LLM client machinery as `sydes.recovery.discovery`/`sydes.recovery.evidence`
— this is the SAME recovery/reasoning capability, pointed at a narrower
question. No new agent architecture.

The question asked is deliberately narrow and evidence-based, never an
opinion: "does this specific candidate actually connect this
entrypoint/entity to this specific changed target" — answered only from
what the agent can find and cite by file, never "does this sound
related". See `_SYSTEM_PROMPT` for the exact framing and the asymmetric
fail-safe: uncertain always means RETAIN, never SUPPRESS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sydes.llm.client import LLMClient
from sydes.recovery.react import TOOL_PROMPT_BLOCK, run_react_loop
from sydes.recovery.schema import RecoveryError
from sydes.recovery.tools import RepoTools

PROMOTE = "promote"
RETAIN = "retain"
SUPPRESS = "suppress"

_VALID_DECISIONS = (PROMOTE, RETAIN, SUPPRESS)


@dataclass(frozen=True)
class LikelyImpactCandidate:
    """One first-pass "likely, not fully established" item to check.

    `entry_label`/`candidate_label` are display-only context for the
    prompt; `proposed_target` and `changed_files` are the two pieces of
    canonical identity the agent's answer is actually judged against —
    see `sydes.recovery.verify` for the same file/qualified-name identity
    discipline applied to recovered PATHS.
    """

    entry_label: str
    candidate_label: str
    proposed_target: str
    changed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceCheckResult:
    decision: str
    reason: str
    evidence_files: tuple[str, ...] = ()


@dataclass
class EvidenceCheckStats:
    """Same shape `run_react_loop` needs from any stats object (see its
    docstring) — kept separate from `sydes.recovery.agent.RecoveryRunStats`
    so this experimental module has no dependency on the path-recovery
    agent module."""

    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    llm_calls: int = 0
    tool_calls: list = field(default_factory=list)
    files_read: list = field(default_factory=list)


_SYSTEM_PROMPT = f"""You are checking ONE specific candidate claim against the actual repository, not judging whether it "sounds plausible".

You will be given: an entrypoint, a proposed changed target it might reach, and the actual list of files this diff changed. Your ONLY job is to determine, by searching and reading the repository, whether the entrypoint's code ACTUALLY reaches the proposed target -- a real call chain, real registration/dispatch, or real indirection you can point to in the source -- or whether it does not.

This is a narrow factual question: "Does this specific candidate actually connect this entrypoint/entity to this specific changed target?" It is NOT "does this sound related" or "could this plausibly be affected". Two symbols living in the same file, class, or module as each other is NOT evidence of a connection by itself -- you must find (or fail to find) an actual path: a direct call, a registration, a decorator/dependency-injection wiring, an event/dispatch mechanism, or similar, cited by file and line. A shared class or a shared "belongs to the same service" relationship, with no actual call/dispatch edge you can point to, is exactly the kind of false candidate this check exists to catch -- do not treat it as evidence.

A connection is not only a direct call in a handler's own body. A target can also be reached through the SHAPE of data the entrypoint accepts or returns: if the entrypoint's own request or response type has a field, constraint, default, or wrapper that is wired to the proposed target -- so that constructing, validating, or serializing that type invokes it -- that is a real connection, exactly as real as an explicit call, even though no line in the handler function names it directly. The same is true one level further: a value or object the target reads (a shared config/settings object, a constant, a registry) that IS itself read by something already connected to the entrypoint is a real, if indirect, connection -- trace what a candidate actually reads or is read by, not only what calls it by name. Before you conclude "suppress", you must have positively ruled out this kind of connection too, not merely failed to find a direct call -- search for the proposed target's name across the whole repository (not only inside the handler function) and check anything that references the entrypoint's own request/response type. Absence of a direct call in the handler body is NOT, by itself, positive evidence of no connection.

{TOOL_PROMPT_BLOCK}

When you are done investigating (or out of turns), respond with your FINAL answer as a single JSON object and nothing else:
{{"final": {{
  "decision": "promote" | "suppress" | "retain",
  "reason": "one or two sentences citing what you found or did not find",
  "evidence_files": ["file paths you actually read that support this decision"]
}}}}

Decision rules -- read carefully, these are NOT symmetric:
- "promote": you found a REAL, citable connection (by file/line) from the entrypoint to the specific proposed target -- a direct call, OR wiring through the entrypoint's own request/response type, OR a traced read/write relationship to something already connected.
- "suppress": you actively searched -- including for indirect wiring and shared reads, per above, not just a direct call -- and found clear evidence the entrypoint's code does NOT reach the proposed target -- e.g. it calls a different, unrelated method, or the proposed target is provably unreachable from it.
- "retain": you could not find clear evidence either way after genuinely searching -- the honest answer is "unresolved", not a guess.

FAIL SAFE: if you are not confident, answer "retain", never "suppress". Suppressing a real impact is far worse than leaving one labeled "likely, not fully established" a little longer. Only answer "suppress" when you have positive evidence the connection does NOT exist, not merely an absence of evidence FOR it -- and "I found no direct call in the handler" is not, by itself, that evidence.

Respond with exactly one JSON object per turn: either a tool call or your final answer. Nothing else -- no prose outside the JSON."""


def _build_initial_prompt(candidate: LikelyImpactCandidate) -> str:
    lines = [
        f"entrypoint: {candidate.entry_label}",
        f"candidate (what the first pass proposed as possibly reached): {candidate.candidate_label}",
        f"proposed_target: {candidate.proposed_target}",
        "",
        "changed_files (the ONLY files this diff actually changed):",
        *(
            [f"  - {f}" for f in candidate.changed_files]
            if candidate.changed_files
            else ["  (none recorded)"]
        ),
        "",
        "Investigate now. Call one tool per turn, or give your final decision when ready.",
    ]
    return "\n".join(lines)


def _parse_final(raw: str) -> EvidenceCheckResult:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecoveryError(f"evidence-check final answer was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecoveryError("evidence-check final answer was not a JSON object")

    decision = str(payload.get("decision", "")).strip().lower()
    if decision not in _VALID_DECISIONS:
        raise RecoveryError(f"evidence-check final answer had an invalid decision: {decision!r}")

    reason = str(payload.get("reason", "") or "").strip()
    raw_files = payload.get("evidence_files", [])
    evidence_files = tuple(str(f) for f in raw_files if isinstance(f, str)) if isinstance(raw_files, list) else ()

    return EvidenceCheckResult(decision=decision, reason=reason, evidence_files=evidence_files)


def check_candidate_evidence(
    candidate: LikelyImpactCandidate,
    *,
    tools: RepoTools,
    client: LLMClient,
    max_turns: int = 6,
    max_response_chars: int = 4000,
    stats: EvidenceCheckStats | None = None,
) -> EvidenceCheckResult:
    """Evidence-check ONE candidate against the repository `tools` is
    scoped to.

    Fail-safe by construction: a provider failure or malformed final
    answer is caught here and turned into a `RETAIN` result with the
    failure recorded in `reason` — this function never raises, and never
    lets an infrastructure failure read as a positive suppression
    decision. Uncertainty inside a well-formed answer is the model's own
    job to resolve to `retain` (see `_SYSTEM_PROMPT`'s fail-safe rule);
    this is the second, independent layer of the same guarantee.
    """
    stats = stats if stats is not None else EvidenceCheckStats()
    try:
        return run_react_loop(
            client=client,
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            initial_prompt=_build_initial_prompt(candidate),
            max_turns=max_turns,
            max_response_chars=max_response_chars,
            stats=stats,
            parse_final=_parse_final,
        )
    except RecoveryError as exc:
        return EvidenceCheckResult(
            decision=RETAIN,
            reason=f"evidence check could not complete, retaining conservatively: {exc}",
        )
