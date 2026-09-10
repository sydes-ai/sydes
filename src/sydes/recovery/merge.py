"""Read-only, additive views of a `RecoveryResult` — never a write path.

Nothing here mutates `ChangeVerificationResult` or the CBM graph. This
prototype keeps recovered edges/results run-local: `build_recovery_view`
produces a plain dict a caller (the evaluation harness, or eventually a
renderer) can inspect or print, with every recovered element's provenance
kept distinguishable from anything the first pass established
structurally. A real merge into the canonical result — if this prototype
proves out — is deliberately out of scope here; see the module-level
"experiment first, integration second" framing in `sydes.recovery`.
"""

from __future__ import annotations

from typing import Any

from sydes.recovery.schema import RecoveryResult, STATUS_ESTABLISHED


def build_recovery_view(recovery: RecoveryResult) -> dict[str, Any]:
    """A plain, JSON-ready dict summarizing what recovery found, with every
    path/test tagged by its own provenance — never presented as though CBM
    itself established it."""
    return {
        "status": recovery.status,
        "recovered_paths": [
            {
                "entrypoint": path.entrypoint,
                "steps": [s.model_dump() for s in path.steps],
                "evidence": [e.model_dump() for e in path.evidence],
                "status": path.status,
                "provenance": path.provenance,
                "rejection_reason": path.rejection_reason,
            }
            for path in recovery.recovered_paths
        ],
        "recovered_tests": [t.model_dump() for t in recovery.recovered_tests],
        "corrected_first_pass_claims": [c.model_dump() for c in recovery.corrected_first_pass_claims],
        "unresolved": [u.model_dump() for u in recovery.unresolved],
    }


def summarize_for_notes(recovery: RecoveryResult) -> str:
    """One line suitable for `ChangeVerificationResult.notes` — the only
    touch point this prototype has with the canonical result today (see
    `sydes.cli.verify_change`'s `--ai-recovery` flag). Never claims
    structural provenance for what recovery found."""
    established = [p for p in recovery.recovered_paths if p.status == STATUS_ESTABLISHED]
    if established:
        entrypoints = ", ".join(p.entrypoint for p in established)
        return (
            f"AI recovery (experimental, provenance=ai_recovery, run-local, not merged into "
            f"structural results): established {len(established)} path(s) not found by the "
            f"first pass: {entrypoints}. Also recovered {len(recovery.recovered_tests)} test(s)."
        )
    return (
        "AI recovery (experimental) ran and could not establish a source-backed path beyond "
        f"the first pass; {len(recovery.unresolved)} question(s) remain unresolved."
    )
