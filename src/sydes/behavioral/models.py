"""Serializable behavioral-evidence model attached to `ChangeVerificationResult`."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Evidence classes of a merged edge. Ordered from strongest runtime standing to
# "nobody executed this". They are classes, not a score: a consumer renders
# them as labels and never sums them.
OBSERVED_RUNTIME = "OBSERVED_RUNTIME"
COMPOSED_STATE = "COMPOSED_STATE"
COMPOSED_VALUE = "COMPOSED_VALUE"
COMPOSED_ARG_SHAPE = "COMPOSED_ARG_SHAPE"
COMPOSED_SYMBOL = "COMPOSED_SYMBOL"
STATIC_ONLY = "STATIC_ONLY"
GAP = "GAP"
UNRESOLVED = "UNRESOLVED"
EXTERNAL = "EXTERNAL"

COMPOSED_BY_GRADE = {
    "STATE": COMPOSED_STATE,
    "VALUE": COMPOSED_VALUE,
    "ARG_SHAPE": COMPOSED_ARG_SHAPE,
    "SYMBOL": COMPOSED_SYMBOL,
}

# Why behavioral evidence is absent. Never silently "no impact".
STATUS_AVAILABLE = "available"
STATUS_NOT_REQUESTED = "not_requested"
STATUS_UNAVAILABLE = "unavailable"


class BehavioralNode(BaseModel):
    id: str  # DiffGenome's stable symbol id, e.g. go:api.Server.createTransfer
    name: str
    file: str | None = None
    line: int | None = None
    kind: str = "callable"
    changed: bool = False
    executed_by: int = 0  # existing executions that ran the real symbol
    static_match: str | None = None  # the static-flow step identity this node matched


class BehavioralEdge(BaseModel):
    caller: str
    callee: str
    evidence_class: str
    #: DiffGenome's own evidence kind (observed | composed | gap | unresolved | external ...)
    runtime_evidence: str | None = None
    join: str | None = None
    state: str | None = None  # matched | unavailable | not_consulted | n/a
    exit: str | None = None  # same | kind | unknown
    probe_derived: bool = False
    ambiguous: bool = False
    static_corroborated: bool = False  # Sydes' structural analysis also has this hop
    runtime_only: bool = False  # DiffGenome saw it; structural analysis did not
    executions: list[str] = Field(default_factory=list)
    probes: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


class StaticOnlyStep(BaseModel):
    """A step of the structural trace no execution or reconstruction reached: possible."""

    symbol: str
    file: str | None = None
    kind: str | None = None


class BehavioralSeam(BaseModel):
    caller: str
    target: str
    accepted_candidates: int = 0
    rejected_candidates: int = 0
    rejection_reasons: dict[str, int] = Field(default_factory=dict)


class BehavioralProbeSummary(BaseModel):
    writer: str = "none"
    requested: int = 0
    attempts: int = 0
    accepted: int = 0
    rejected: int = 0
    llm_calls: int = 0


class BehavioralEvidence(BaseModel):
    """What DiffGenome established about the change, merged with the static view."""

    status: str = STATUS_NOT_REQUESTED
    reason: str | None = None  # why unavailable, when it is
    source: str = "diffgenome"
    artifact_format: str | None = None
    artifact_path: str | None = None
    runtime: str | None = None
    revision: str | None = None
    changed_symbols: list[str] = Field(default_factory=list)
    changed_symbols_never_executed: list[str] = Field(default_factory=list)
    changed_symbols_unmatched: list[str] = Field(default_factory=list)  # in the diff, not in DiffGenome
    #: changed symbols executed evidence does not connect to any step of the static flows
    changed_symbols_without_entry: list[str] = Field(default_factory=list)
    nodes: list[BehavioralNode] = Field(default_factory=list)
    edges: list[BehavioralEdge] = Field(default_factory=list)
    static_only_steps: list[StaticOnlyStep] = Field(default_factory=list)
    seams: list[BehavioralSeam] = Field(default_factory=list)
    tests_on_behavioral_path: list[str] = Field(default_factory=list)
    #: test id -> test source file, when the behavioral run recorded one
    test_files: dict[str, str] = Field(default_factory=dict)
    #: tests that executed the changed code and FAILED in the isolated run
    tests_failed_on_path: list[str] = Field(default_factory=list)
    probes: BehavioralProbeSummary = Field(default_factory=BehavioralProbeSummary)
    facts: dict[str, object] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    #: DiffGenome's checked behavioral rules (`diffgenome-genome-summary/1`), when the
    #: artifact carries them: only claims its checker verified or supported from the traces
    genome: dict[str, Any] | None = None
    #: DiffGenome's runtime evidence (`diffgenome-runtime/1`) as Sydes applied it: per changed
    #: function executed / tests / exits / observed entry roots / changed-line conditions, and
    #: the runtime gaps. None when the artifact carried none or `--runtime-evidence off`.
    runtime_evidence: dict[str, Any] | None = None

    def by_class(self, evidence_class: str) -> list[BehavioralEdge]:
        return [e for e in self.edges if e.evidence_class == evidence_class]
