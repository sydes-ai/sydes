"""Structured, strictly-parsed recovery output.

Mirrors the fail-closed philosophy of `sydes.impact.guide`: the recovery
agent's `LLMClient` has no structured-output mode, so the contract is "ask
for JSON in the prompt, then parse and validate strictly." Anything that
does not parse or does not match the schema raises `RecoveryError` rather
than being coerced into a best-effort guess.

Provenance is never ambiguous: every recovered path/test carries its own
`provenance` (`PROVENANCE_AI_RECOVERY` or `PROVENANCE_AI_RECOVERY_EXHAUSTED`)
so a caller can never mistake an AI-recovered edge for one CBM established
structurally. Nothing in this module writes into `ChangeVerificationResult`
or the CBM graph — see `sydes.recovery.merge` for the read-only, additive
view built from a `RecoveryResult`.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

STATUS_ESTABLISHED = "established"
STATUS_UNRESOLVED = "unresolved"
_VALID_STATUSES = (STATUS_ESTABLISHED, STATUS_UNRESOLVED)

#: An AI-recovered path/test that repository evidence supports.
PROVENANCE_AI_RECOVERY = "ai_recovery"
#: A path/test the agent could not establish even after the retry budget —
#: distinct from never having tried; see `sydes.recovery.agent`.
PROVENANCE_AI_RECOVERY_EXHAUSTED = "ai_recovery_exhausted"
_VALID_PROVENANCE = (PROVENANCE_AI_RECOVERY, PROVENANCE_AI_RECOVERY_EXHAUSTED)

#: Response text considered for JSON extraction, mirroring
#: `sydes.impact.guide._MAX_RESPONSE_CHARS` — a reasoning-heavy response
#: that wanders past this is treated as malformed rather than scanned
#: indefinitely for a stray brace.
_MAX_RESPONSE_CHARS = 16000


class RecoveryError(RuntimeError):
    """The recovery agent could not produce a usable structured result.

    Covers provider failure, non-JSON output, and a missing/invalid field.
    The caller's only correct response is to record the failure and leave
    the first-pass result exactly as it was — never to substitute a guessed
    recovery.
    """


class RecoveredEvidence(BaseModel):
    """One inspectable, re-readable fact backing a recovered claim.

    `fact` is a short, specific statement of what the cited lines show (e.g.
    "constructs the query object and dispatches it to the bus" or
    "decorator registers this handler for that message type") — never a
    restatement of the bridge it is supposed to support ("this connects A to
    B" is not evidence, it is the claim).
    """

    file: str
    line_start: int | None = None
    line_end: int | None = None
    fact: str


class RecoveredStep(BaseModel):
    """One hop in a recovered path, from the entrypoint toward the changed
    behavior. `relationship` is free text describing the connection actually
    found in source (e.g. "calls", "constructs and dispatches", "registered
    for via decorator", "invoked by scheduler config") — deliberately open,
    never a fixed framework-relationship taxonomy."""

    symbol: str
    file: str
    relationship: str


class RecoveredPath(BaseModel):
    """One recovered entrypoint-to-changed-behavior path.

    `status`/`provenance` are per-path: a single recovery run can establish
    some paths while leaving others unresolved, and a path's status can
    later be downgraded by `sydes.recovery.verify` independently of any
    other path this same run proposed.
    """

    entrypoint: str
    steps: list[RecoveredStep] = Field(default_factory=list)
    evidence: list[RecoveredEvidence] = Field(default_factory=list)
    status: str = STATUS_UNRESOLVED
    provenance: str = PROVENANCE_AI_RECOVERY_EXHAUSTED
    #: Set by `sydes.recovery.verify` when a verification pass rejected this
    #: path after it was initially proposed as established — kept distinct
    #: from `status`/`provenance` so the merge view can say *why*, not just
    #: *that*, a path was downgraded.
    rejection_reason: str | None = None


class RecoveredTest(BaseModel):
    """A test the agent found that directly exercises the changed behavior
    — not merely a test file that happens to have changed."""

    file: str
    test: str
    covers: str
    evidence: list[RecoveredEvidence] = Field(default_factory=list)


class CorrectedFirstPassClaim(BaseModel):
    """The agent found source evidence contradicting, not just extending,
    the first pass's own hypothesis (e.g. CBM implied `A -> B -> ?` but
    source shows `A -> C -> D`)."""

    claim: str
    original: str
    corrected: str
    evidence: list[RecoveredEvidence] = Field(default_factory=list)


class UnresolvedQuestion(BaseModel):
    """One thing the agent could not establish, and exactly what evidence
    would be needed to establish it — never a vague "insufficient
    context"."""

    question: str
    missing_evidence: str


class RecoveryResult(BaseModel):
    """The complete, strict output of one recovery run.

    `status` is the overall outcome: `established` iff at least one
    `recovered_paths` entry has `status=established`; `unresolved`
    otherwise. Never mutated in place after `sydes.recovery.verify` runs —
    `verify` returns a new `RecoveryResult` with any downgraded paths, so a
    caller always holds one coherent, internally-consistent object.
    """

    status: str = STATUS_UNRESOLVED
    recovered_paths: list[RecoveredPath] = Field(default_factory=list)
    recovered_tests: list[RecoveredTest] = Field(default_factory=list)
    corrected_first_pass_claims: list[CorrectedFirstPassClaim] = Field(default_factory=list)
    unresolved: list[UnresolvedQuestion] = Field(default_factory=list)


def _extract_json_object(text: str) -> Any | None:
    """Best-effort parse for a single JSON object in model output — the
    same tolerant-of-fences, strict-about-shape extraction
    `sydes.impact.guide._extract_json_object` uses."""
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


def _parse_evidence_list(raw: Any, *, context: str) -> list[RecoveredEvidence]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RecoveryError(f"{context}: 'evidence' must be a JSON array")
    out: list[RecoveredEvidence] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RecoveryError(f"{context}: evidence entry must be an object")
        file = item.get("file")
        fact = item.get("fact")
        if not isinstance(file, str) or not file.strip():
            raise RecoveryError(f"{context}: evidence entry missing non-empty 'file'")
        if not isinstance(fact, str) or not fact.strip():
            raise RecoveryError(f"{context}: evidence entry missing non-empty 'fact'")
        line_start = item.get("line_start")
        line_end = item.get("line_end")
        out.append(
            RecoveredEvidence(
                file=file.strip(),
                line_start=line_start if isinstance(line_start, int) else None,
                line_end=line_end if isinstance(line_end, int) else None,
                fact=fact.strip(),
            )
        )
    return out


def _parse_steps(raw: Any, *, context: str) -> list[RecoveredStep]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RecoveryError(f"{context}: 'steps' must be a JSON array")
    out: list[RecoveredStep] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RecoveryError(f"{context}: step entry must be an object")
        symbol = item.get("symbol")
        file = item.get("file")
        relationship = item.get("relationship")
        if not all(isinstance(v, str) and v.strip() for v in (symbol, file, relationship)):
            raise RecoveryError(f"{context}: step entry requires non-empty symbol/file/relationship")
        out.append(RecoveredStep(symbol=symbol.strip(), file=file.strip(), relationship=relationship.strip()))
    return out


def _parse_path(raw: Any, *, index: int) -> RecoveredPath:
    if not isinstance(raw, dict):
        raise RecoveryError(f"recovered_paths[{index}] must be an object")
    entrypoint = raw.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise RecoveryError(f"recovered_paths[{index}] missing non-empty 'entrypoint'")
    context = f"recovered_paths[{index}]"
    steps = _parse_steps(raw.get("steps"), context=context)
    evidence = _parse_evidence_list(raw.get("evidence"), context=context)
    # The agent proposes a path as established or not; deterministic
    # downgrade for empty evidence happens in `validate_result`, not here —
    # this function only parses what the model said, faithfully.
    raw_status = raw.get("status", STATUS_UNRESOLVED)
    status = raw_status.strip().lower() if isinstance(raw_status, str) else STATUS_UNRESOLVED
    if status not in _VALID_STATUSES:
        raise RecoveryError(f"{context}: unsupported status {raw_status!r}")
    provenance = PROVENANCE_AI_RECOVERY if status == STATUS_ESTABLISHED else PROVENANCE_AI_RECOVERY_EXHAUSTED
    return RecoveredPath(
        entrypoint=entrypoint.strip(), steps=steps, evidence=evidence,
        status=status, provenance=provenance,
    )


def _parse_test(raw: Any, *, index: int) -> RecoveredTest:
    if not isinstance(raw, dict):
        raise RecoveryError(f"recovered_tests[{index}] must be an object")
    file = raw.get("file")
    test = raw.get("test")
    covers = raw.get("covers")
    if not all(isinstance(v, str) and v.strip() for v in (file, test, covers)):
        raise RecoveryError(f"recovered_tests[{index}] requires non-empty file/test/covers")
    evidence = _parse_evidence_list(raw.get("evidence"), context=f"recovered_tests[{index}]")
    return RecoveredTest(file=file.strip(), test=test.strip(), covers=covers.strip(), evidence=evidence)


def _parse_corrected_claim(raw: Any, *, index: int) -> CorrectedFirstPassClaim:
    if not isinstance(raw, dict):
        raise RecoveryError(f"corrected_first_pass_claims[{index}] must be an object")
    claim = raw.get("claim")
    original = raw.get("original")
    corrected = raw.get("corrected")
    if not all(isinstance(v, str) and v.strip() for v in (claim, original, corrected)):
        raise RecoveryError(
            f"corrected_first_pass_claims[{index}] requires non-empty claim/original/corrected"
        )
    evidence = _parse_evidence_list(raw.get("evidence"), context=f"corrected_first_pass_claims[{index}]")
    return CorrectedFirstPassClaim(
        claim=claim.strip(), original=original.strip(), corrected=corrected.strip(), evidence=evidence,
    )


def _parse_unresolved(raw: Any, *, index: int) -> UnresolvedQuestion:
    if not isinstance(raw, dict):
        raise RecoveryError(f"unresolved[{index}] must be an object")
    question = raw.get("question")
    missing_evidence = raw.get("missing_evidence")
    if not isinstance(question, str) or not question.strip():
        raise RecoveryError(f"unresolved[{index}] missing non-empty 'question'")
    if not isinstance(missing_evidence, str) or not missing_evidence.strip():
        raise RecoveryError(f"unresolved[{index}] missing non-empty 'missing_evidence'")
    return UnresolvedQuestion(question=question.strip(), missing_evidence=missing_evidence.strip())


def parse_recovery_result(text: str) -> RecoveryResult:
    """Parse and strictly validate one final recovery response.

    Raises `RecoveryError` for anything not exactly the documented shape.
    No best-effort coercion: a response close to valid but not valid is
    exactly the case a fail-closed contract exists for — see the module
    docstring.
    """
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise RecoveryError("recovery output was not a single JSON object")

    raw_paths = payload.get("recovered_paths", [])
    if not isinstance(raw_paths, list):
        raise RecoveryError("'recovered_paths' must be a JSON array")
    paths = [_parse_path(item, index=i) for i, item in enumerate(raw_paths)]

    raw_tests = payload.get("recovered_tests", [])
    if not isinstance(raw_tests, list):
        raise RecoveryError("'recovered_tests' must be a JSON array")
    tests = [_parse_test(item, index=i) for i, item in enumerate(raw_tests)]

    raw_corrected = payload.get("corrected_first_pass_claims", [])
    if not isinstance(raw_corrected, list):
        raise RecoveryError("'corrected_first_pass_claims' must be a JSON array")
    corrected = [_parse_corrected_claim(item, index=i) for i, item in enumerate(raw_corrected)]

    raw_unresolved = payload.get("unresolved", [])
    if not isinstance(raw_unresolved, list):
        raise RecoveryError("'unresolved' must be a JSON array")
    unresolved = [_parse_unresolved(item, index=i) for i, item in enumerate(raw_unresolved)]

    result = RecoveryResult(
        recovered_paths=paths, recovered_tests=tests,
        corrected_first_pass_claims=corrected, unresolved=unresolved,
    )
    return validate_result(result)


def validate_result(result: RecoveryResult) -> RecoveryResult:
    """The deterministic evidence gate: a path claimed `established` with no
    evidence is downgraded to `unresolved` regardless of what the model
    said. This runs independent of (and before) `sydes.recovery.verify`'s
    LLM-based adversarial check — a structural backstop that needs no
    provider call and cannot be argued around by a confident-sounding
    response.

    Overall `status` is recomputed from the (possibly downgraded) paths:
    `established` iff at least one path remains `established`.
    """
    fixed_paths: list[RecoveredPath] = []
    for path in result.recovered_paths:
        if path.status == STATUS_ESTABLISHED and not path.evidence:
            fixed_paths.append(
                path.model_copy(
                    update={
                        "status": STATUS_UNRESOLVED,
                        "provenance": PROVENANCE_AI_RECOVERY_EXHAUSTED,
                        "rejection_reason": "claimed established with no cited evidence",
                    }
                )
            )
        else:
            fixed_paths.append(path)
    overall_status = (
        STATUS_ESTABLISHED
        if any(p.status == STATUS_ESTABLISHED for p in fixed_paths)
        else STATUS_UNRESOLVED
    )
    return result.model_copy(update={"recovered_paths": fixed_paths, "status": overall_status})
