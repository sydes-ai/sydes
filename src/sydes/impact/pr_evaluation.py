"""Diagnostics schema for a frozen multi-PR M3 evaluation.

Not run here, and not wired into `verify-change`'s runtime path. This module
only defines what one PR's result should look like once captured, and how to
classify it, so the next batch — a small, frozen set of real PRs — can start
immediately after that set is chosen, without first inventing a schema under
time pressure.

`build_pr_evaluation_record` is a post-hoc summarizer: it takes the plain
values a `ChangeVerificationResult` and `ImpactResult` already expose (plus
whatever timing the caller measured around them), not those objects
themselves, so this module has no import-time dependency on the verifier and
stays testable with plain fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The change was already reachable through structural facts alone — the
#: guide was never triggered, or was triggered but contributed nothing that
#: mattered.
DETERMINISTIC_SOLVED = "deterministic_solved"
#: The guide's investigation produced a confirmed edge that closed the gap —
#: at least one final affected entrypoint carries `llm_guided_*` provenance.
LLM_GUIDED_SOLVED = "llm_guided_solved"
#: Some, but not all, of the expected affected entrypoints were found.
#: Requires a human reference to detect.
PARTIAL_IMPACT = "partial_impact"
#: Nothing was found, and nothing wrong was claimed — the honest, safe
#: outcome for a change the current facts/tools genuinely cannot connect.
UNRESOLVED_STRUCTURAL_GAP = "unresolved_structural_gap"
#: The run itself did not complete cleanly (CBM unavailable, indexing
#: failed, an unhandled exception) — not a judgement about impact quality.
INFRA_FAILURE = "infra_failure"
#: A final affected entrypoint was reported that a human reference says is
#: wrong — over-claiming, not under-claiming (see `PARTIAL_IMPACT`).
INCORRECT_IMPACT = "incorrect_impact"
#: The single outcome M3's every safety property exists to prevent: a
#: VERIFIED verdict despite impact being incomplete, wrong, or unresolved.
#: Always checked first and never overridden by a "solved" category.
DANGEROUS_FALSE_VERIFIED = "dangerous_false_verified"

RESULT_CATEGORIES = (
    DETERMINISTIC_SOLVED, LLM_GUIDED_SOLVED, PARTIAL_IMPACT,
    UNRESOLVED_STRUCTURAL_GAP, INFRA_FAILURE, INCORRECT_IMPACT,
    DANGEROUS_FALSE_VERIFIED,
)

#: Verdict strings that count as "VERIFIED" for the false-VERIFIED check.
#: Kept as a small local constant rather than importing `verify.models` here,
#: so this module has no dependency on the verifier's own vocabulary — it
#: only needs to recognise the one string that matters for the safety check.
_VERIFIED_VERDICT = "VERIFIED"


def classify_pr_result(
    *,
    verdict: str,
    final_affected_entrypoints: tuple[str, ...],
    expected_affected_entrypoints: tuple[str, ...] | None,
    guide_triggered: bool,
    llm_guided_entrypoints: tuple[str, ...],
    infra_error: str | None,
) -> str:
    """Assign exactly one `RESULT_CATEGORIES` value to a completed run.

    `expected_affected_entrypoints=None` means no human reference was
    captured for this PR — accuracy categories (`PARTIAL_IMPACT`,
    `INCORRECT_IMPACT`, and the missing-affected arm of
    `DANGEROUS_FALSE_VERIFIED`) cannot be determined without one, so the
    classification falls back to what the run's own signals support
    (solved/unresolved/infra), which is still meaningful on its own.
    """
    if infra_error:
        return INFRA_FAILURE

    found = set(final_affected_entrypoints)
    verified = verdict.upper() == _VERIFIED_VERDICT

    if expected_affected_entrypoints is not None:
        expected = set(expected_affected_entrypoints)
        if verified and expected - found:
            # Claimed done while a known-real affected entrypoint is missing.
            return DANGEROUS_FALSE_VERIFIED
        if expected and found and expected != found and found <= expected:
            return PARTIAL_IMPACT
        if found - expected:
            return INCORRECT_IMPACT
        if expected and found == expected:
            return LLM_GUIDED_SOLVED if llm_guided_entrypoints else DETERMINISTIC_SOLVED
        if not expected and not found:
            return DETERMINISTIC_SOLVED
        if expected and not found:
            return UNRESOLVED_STRUCTURAL_GAP

    # No reference available: judge only by what the run itself demonstrates.
    if verified and not found:
        # A VERIFIED verdict with no affected flow at all is only safe when
        # the change genuinely reaches nothing — but that is exactly the
        # unresolvable case without a reference, so this is flagged rather
        # than assumed benign.
        return DANGEROUS_FALSE_VERIFIED
    if found:
        return LLM_GUIDED_SOLVED if llm_guided_entrypoints else DETERMINISTIC_SOLVED
    return UNRESOLVED_STRUCTURAL_GAP


@dataclass(frozen=True)
class PrEvaluationRecord:
    """One PR's full M3 diagnostic record, ready to serialize for a batch."""

    pr_id: str
    repo: str
    changed_symbols: tuple[str, ...]

    #: Human-labelled ground truth, when the batch curator supplied one.
    expected_affected_entrypoints: tuple[str, ...] | None

    #: What the deterministic pass alone found, before any guide turn —
    #: captures `ImpactResult.affected` from a `guide_policy=off` run (or,
    #: equivalently, the entrypoints whose only strategy is a deterministic
    #: one) so deterministic and guided contribution can be told apart.
    deterministic_affected_entrypoints: tuple[str, ...]

    guide_triggered: bool
    guide_calls: int
    guide_actions: dict[str, int]
    confirmed_evidence_count: int
    #: Final affected entrypoints whose path carries at least one
    #: `llm_guided_*` provenance step.
    llm_guided_entrypoints: tuple[str, ...]

    final_affected_entrypoints: tuple[str, ...]
    obligations_generated: int
    #: How many of the mapped tests actually exercised an obligation —
    #: the evidence/test-mapping count `verify-change` already produces.
    evidence_mapped_count: int
    verdict: str

    total_latency_ms: float
    cbm_latency_ms: float | None
    guide_latency_ms: float
    #: Provider-reported usage, when the client surfaces it (Sydes'
    #: `LLMClient` does not today — recorded as `None` until it does, rather
    #: than estimated).
    token_usage: dict[str, Any] | None

    infra_error: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def category(self) -> str:
        return classify_pr_result(
            verdict=self.verdict,
            final_affected_entrypoints=self.final_affected_entrypoints,
            expected_affected_entrypoints=self.expected_affected_entrypoints,
            guide_triggered=self.guide_triggered,
            llm_guided_entrypoints=self.llm_guided_entrypoints,
            infra_error=self.infra_error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "repo": self.repo,
            "changed_symbols": list(self.changed_symbols),
            "expected_affected_entrypoints": (
                list(self.expected_affected_entrypoints)
                if self.expected_affected_entrypoints is not None else None
            ),
            "deterministic_affected_entrypoints": list(self.deterministic_affected_entrypoints),
            "guide_triggered": self.guide_triggered,
            "guide_calls": self.guide_calls,
            "guide_actions": dict(self.guide_actions),
            "confirmed_evidence_count": self.confirmed_evidence_count,
            "llm_guided_entrypoints": list(self.llm_guided_entrypoints),
            "final_affected_entrypoints": list(self.final_affected_entrypoints),
            "obligations_generated": self.obligations_generated,
            "evidence_mapped_count": self.evidence_mapped_count,
            "verdict": self.verdict,
            "total_latency_ms": self.total_latency_ms,
            "cbm_latency_ms": self.cbm_latency_ms,
            "guide_latency_ms": self.guide_latency_ms,
            "token_usage": self.token_usage,
            "infra_error": self.infra_error,
            "notes": list(self.notes),
            "category": self.category,
        }


def build_pr_evaluation_record(
    *,
    pr_id: str,
    repo: str,
    changed_symbols: list[str],
    impact_metrics: dict[str, Any],
    deterministic_affected_entrypoints: list[str],
    final_affected_entrypoints: list[str],
    llm_guided_entrypoints: list[str],
    verdict: str,
    obligations_generated: int,
    evidence_mapped_count: int,
    total_latency_ms: float,
    cbm_latency_ms: float | None = None,
    token_usage: dict[str, Any] | None = None,
    expected_affected_entrypoints: list[str] | None = None,
    infra_error: str | None = None,
    notes: list[str] | None = None,
) -> PrEvaluationRecord:
    """Assemble one record from a completed `ImpactResult.metrics` plus the
    surrounding verify-change output — the shape both already produce today,
    so capturing a batch needs no new instrumentation beyond timing."""
    return PrEvaluationRecord(
        pr_id=pr_id,
        repo=repo,
        changed_symbols=tuple(changed_symbols),
        expected_affected_entrypoints=(
            tuple(expected_affected_entrypoints) if expected_affected_entrypoints is not None else None
        ),
        deterministic_affected_entrypoints=tuple(deterministic_affected_entrypoints),
        guide_triggered=bool(impact_metrics.get("guide_triggered", False)),
        guide_calls=int(impact_metrics.get("guide_calls", 0)),
        guide_actions=dict(impact_metrics.get("guide_actions", {})),
        confirmed_evidence_count=int(impact_metrics.get("evidence_confirmed", 0)),
        llm_guided_entrypoints=tuple(llm_guided_entrypoints),
        final_affected_entrypoints=tuple(final_affected_entrypoints),
        obligations_generated=obligations_generated,
        evidence_mapped_count=evidence_mapped_count,
        verdict=verdict,
        total_latency_ms=total_latency_ms,
        cbm_latency_ms=cbm_latency_ms,
        guide_latency_ms=float(impact_metrics.get("guide_latency_ms", 0.0)),
        token_usage=token_usage,
        infra_error=infra_error,
        notes=tuple(notes or ()),
    )
