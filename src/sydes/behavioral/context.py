"""DiffGenome runtime evidence as input to Sydes' own analysis.

A thin adapter over `sydes.behavioral.runtime.RuntimeEvidence` (the `diffgenome-runtime/1`
contract), shaped for the analyzer's stages:

1. `observed_call_edges`: observed call edges join `structural.call_edges`, tagged
   `diffgenome:observed-runtime`; Sydes' impact interpreter still decides which of ITS
   entrypoints the change reaches.
2. `candidate_files`: files of observed application callers, for route discovery.
3. `compact_evidence`: a bounded text of the runtime facts (and, if present, DiffGenome's
   checked genome facts) for the model stages; used only with `--behavioral-context on`.

Sydes reads only the contract: never trace files, mechanics or other DiffGenome internals.
Nothing here reads or writes obligations, test mappings or the verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sydes.behavioral.review_context import checked_evidence_block
from sydes.behavioral.runtime import EDGE_SOURCE, RuntimeEvidence

__all__ = ["EDGE_SOURCE", "RUNTIME_GUIDANCE", "BehavioralContext", "review_preamble"]


@dataclass
class BehavioralContext:
    runtime: RuntimeEvidence
    genome: dict[str, Any] | None = None
    #: file -> changed head line ranges (set by the analyzer; kept for reporting)
    changed_lines: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @classmethod
    def load(cls, artifact_path: str | Path | None) -> BehavioralContext | None:
        """None when there is no readable artifact or it carries no runtime contract."""
        if not artifact_path:
            return None
        try:
            doc = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        runtime = RuntimeEvidence.from_artifact(doc)
        if runtime is None:
            return None
        genome = doc.get("genome") if isinstance(doc, dict) else None
        return cls(runtime=runtime, genome=genome if isinstance(genome, dict) else None)

    def observed_call_edges(
        self, symbol_index: dict[str, Any], repo: str, existing: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        return self.runtime.observed_call_edges(symbol_index, repo, existing)

    def candidate_files(self) -> set[str]:
        return self.runtime.candidate_files()

    def compact_evidence(self) -> str:
        text = self.runtime.compact_evidence()
        checked = checked_evidence_block(self.genome)
        return text + ("\n" + checked if checked else "")


RUNTIME_GUIDANCE = (
    "RUNTIME EVIDENCE — how to use it. The block below was produced outside this prompt by "
    "running the repository's existing tests against the changed code and recording what "
    "executed. Use it as evidence about which code paths actually execute the change, what "
    "conditions on changed lines were observed, and how the changed code exits:\n"
    "- Observed facts hold for the executions listed, not for inputs no test exercised. A changed "
    "function the selected tests did not run is NOT shown to be untested: other tests in the suite, "
    "or subprocesses the tests start, may run it. Say 'not run by the selected tests', never 'untested'.\n"
    "- VERIFIED rules (if present) are strong; SUPPORTED rules are weaker; UNKNOWN items are NOT "
    "facts: never state one as established or turn it into a consequence.\n"
    "- Read the diff and code yourself; if the evidence and the code disagree, say so. Do not "
    "invent callers, routes or consequences beyond what the evidence and the code support. "
    "Execution is not assertion: a test that ran the code does not show the behavior is checked.\n"
)


def review_preamble(ctx: BehavioralContext | None) -> str:
    """Guidance plus the compact runtime evidence, ready to append to a prompt header."""
    if ctx is None:
        return ""
    return f"\n\n{RUNTIME_GUIDANCE}\n{ctx.compact_evidence()}"
