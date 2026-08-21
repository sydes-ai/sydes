"""Serializable result model for `sydes verify-change`.

The model is the product; CLI/JSON/GitHub output are renderers over it. Keep
field names stable and renderer-neutral so GitHub Actions, PR comments, VS Code
and a future web UI can all consume the same artifact.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sydes.core.models import EvidenceRef, RepoRef

# Change classification.
CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"
CHANGE_RENAMED = "renamed"

# Behavior verification states. Deliberately coarse: no invented confidence.
#
# `passed`/`failed` require that a mapped test was actually executed. The mere
# presence of a test is never `passed` — that distinction is the whole point of
# this stage.
VERIFICATION_PASSED = "passed"
VERIFICATION_FAILED = "failed"
VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_UNKNOWN = "unknown"

# Why an execution could not produce a pass/fail answer. Kept separate from
# status so an infrastructure problem is never reported as a product failure.
BLOCKER_TIMEOUT = "timeout"
BLOCKER_MISSING_DEPENDENCY = "missing_dependency"
BLOCKER_RUNNER_MISSING = "runner_missing"
BLOCKER_FRAMEWORK_UNSUPPORTED = "framework_unsupported"
BLOCKER_COLLECTION_ERROR = "collection_error"
BLOCKER_NO_TESTS_COLLECTED = "no_tests_collected"
BLOCKER_EXECUTION_DISABLED = "execution_disabled"
BLOCKER_PROCESS_ERROR = "process_error"

# Cross-repo link resolution states. Kept separate from the behavior
# verification vocabulary: resolving a call target says nothing about whether
# the behavior passes.
LINK_RESOLVED = "resolved"
LINK_UNRESOLVED = "unresolved"

# Evidence tiers for test-to-obligation mapping. A mapping below Tier C is not
# evidence at all and is never recorded as one.
TIER_DIRECT_ROUTE = "A_direct_route_exercise"
TIER_DIRECT_INVOCATION = "A_direct_invocation"
TIER_ASSERTED_EFFECT = "B_asserted_effect"
TIER_DECLARED = "C_declared"
TIER_REJECTED = "rejected"

# Obligation kinds, each backed by an existing artifact source.
OBLIGATION_ROUTE_CONTRACT = "route_contract"
OBLIGATION_VALIDATION = "validation"
OBLIGATION_SIDE_EFFECT = "side_effect"
OBLIGATION_STATE_CONSISTENCY = "state_consistency"
OBLIGATION_EVENT_EMISSION = "event_emission"
OBLIGATION_CROSS_REPO_CALL = "cross_repo_call"

# Where an obligation came from. Deterministic origins are authoritative;
# `llm_hypothesis` is supplementary and always labelled as such.
ORIGIN_API_CONTRACT = "api_contract"
ORIGIN_TEST_MATRIX = "test_matrix"
ORIGIN_TRACE_SINK = "trace_sink"
ORIGIN_TRACE_STEP = "trace_step"
ORIGIN_CROSS_REPO_LINK = "cross_repo_link"
ORIGIN_LLM_HYPOTHESIS = "llm_hypothesis"

# How complete the shared analysis was for a flow. Kept separate from
# verification status: not knowing about a downstream effect is not the same as
# there being none.
ANALYSIS_COMPLETE = "complete"
ANALYSIS_PARTIAL = "partial"
ANALYSIS_UNKNOWN = "unknown"

# How precisely the executed command targets the mapped test.
GRANULARITY_CASE = "case"
GRANULARITY_FILE = "file"
GRANULARITY_SUITE = "suite"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

VERDICT_OK = "OK"
VERDICT_VERIFIED = "VERIFIED"
VERDICT_INCOMPLETE = "VERIFICATION INCOMPLETE"
VERDICT_ACTION_REQUIRED = "ACTION REQUIRED"

# Node kinds used in affected system flows.
NODE_ROUTE = "route"
NODE_HANDLER = "handler"
NODE_SERVICE = "service"
NODE_REPOSITORY = "repository"
NODE_CLIENT = "client"
NODE_DATABASE = "database"
NODE_EVENT = "event"
NODE_CONSUMER = "consumer"
NODE_EXTERNAL = "external"
NODE_FUNCTION = "function"


class SourceRef(BaseModel):
    """Location reference used by findings, flows, and evidence."""

    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    line: int | None = None


class Hunk(BaseModel):
    """Single changed line range in a file, from `git diff -U0`."""

    start_line: int
    end_line: int


class ChangedSymbol(BaseModel):
    """Symbol whose body overlaps a diff hunk."""

    id: str
    repo: str
    file: str
    name: str
    qualified_name: str | None = None
    kind: str = "function"
    language: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    change_type: str = CHANGE_MODIFIED
    changed_lines: int = 0
    decorators: list[str] = Field(default_factory=list)


class ChangedFile(BaseModel):
    """One file in the diff, with role classification and symbol overlap."""

    repo: str
    path: str
    old_path: str | None = None
    change_type: str = CHANGE_MODIFIED
    role: str | None = None
    added_lines: int = 0
    removed_lines: int = 0
    hunks: list[Hunk] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    binary: bool = False


class ChangeSet(BaseModel):
    """Resolved git change against a base revision."""

    base: str
    head: str | None = None
    merge_base: str | None = None
    repos: list[RepoRef] = Field(default_factory=list)
    includes_working_tree: bool = False
    files: list[ChangedFile] = Field(default_factory=list)
    symbols: list[ChangedSymbol] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class VerificationCounts(BaseModel):
    """Concrete counts backing the summary. No synthetic confidence scores."""

    changed_files: int = 0
    changed_source_files: int = 0
    changed_test_files: int = 0
    changed_symbols: int = 0
    affected_flows: int = 0
    flows_partially_analyzed: int = 0
    code_findings: int = 0
    obligations: int = 0
    obligations_introduced_by_change: int = 0
    obligations_passed: int = 0
    obligations_failed: int = 0
    obligations_unverified: int = 0
    obligations_unknown: int = 0
    mapped_tests: int = 0
    tests_executed: int = 0
    verification_gaps: int = 0
    runtime_dependencies: int = 0
    cross_repo_impacts: int = 0


class ChangeSummary(BaseModel):
    """Top-level verdict derived deterministically from counts and evidence."""

    risk: str = RISK_LOW
    verdict: str = VERDICT_OK
    headline: str | None = None
    counts: VerificationCounts = Field(default_factory=VerificationCounts)
    risk_reasons: list[str] = Field(default_factory=list)


class CodeFinding(BaseModel):
    """Code-level semantic finding about the diff itself."""

    id: str
    severity: Literal["P0", "P1", "P2", "P3"] = "P2"
    title: str
    file: str | None = None
    line: int | None = None
    repo: str | None = None
    explanation: str | None = None
    impact: str | None = None
    suggested_fix: str | None = None
    source: str = "llm"
    evidence: list[EvidenceRef] = Field(default_factory=list)


class MappedTest(BaseModel):
    """An existing test mapped to one obligation, with the reason it was chosen."""

    id: str
    name: str
    case_name: str | None = None
    repo: str | None = None
    file: str | None = None
    line: int | None = None
    suite: str | None = None
    # Why Sydes believes this test verifies the obligation. Both are required:
    # a mapping without an inspectable reason is not evidence.
    match_rule: str = ""
    evidence_tier: str = TIER_REJECTED
    source_refs: list[str] = Field(default_factory=list)
    changed_in_diff: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list)


class TestExecution(BaseModel):
    """Result of actually running one mapped test.

    Retains the exact command, so a reported result can be reproduced by hand.
    """

    # Not a pytest test class, despite the name.
    __test__ = False

    test_id: str
    obligation_id: str | None = None
    framework: str
    command: list[str] = Field(default_factory=list)
    granularity: str = GRANULARITY_CASE
    status: str = VERIFICATION_UNKNOWN
    blocker: str | None = None
    reason: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None
    output_truncated: bool = False
    failure_summary: str | None = None
    missing_dependency: str | None = None
    blocking_runtime_dependency_ids: list[str] = Field(default_factory=list)
    evidence: SourceRef | None = None

    @property
    def command_text(self) -> str:
        """The executed command as a copy-pasteable string."""
        return " ".join(self.command)


class VerificationObligation(BaseModel):
    """Something that must be demonstrated about an affected flow.

    Distinct from the flow itself: a flow is system topology, an obligation is a
    claim about that topology which evidence can confirm or refute.
    """

    id: str
    flow_id: str
    kind: str
    statement: str
    origin: str
    # References into the shared artifacts this obligation was derived from:
    # contract refs, layered-trace step ids, sink ids, test-matrix entries.
    source_refs: list[str] = Field(default_factory=list)
    introduced_by_change: bool = False
    required: bool = True
    mapped_tests: list[MappedTest] = Field(default_factory=list)
    executions: list[TestExecution] = Field(default_factory=list)
    supporting_tests: list[MappedTest] = Field(default_factory=list)
    status: str = VERIFICATION_UNVERIFIED
    reason: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class AffectedFlow(BaseModel):
    """One backend behavior path touched by the change.

    Topology only. It carries references to the shared trace artifacts that
    describe it rather than re-materializing them, and its status is derived
    from its obligations, never asserted directly.
    """

    id: str
    entry_kind: str = NODE_ROUTE
    entry_label: str
    repo: str | None = None
    method: str | None = None
    path: str | None = None
    handler: str | None = None
    # Pointers into the shared artifacts that are the source of truth.
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    changed_nodes: list[SourceRef] = Field(default_factory=list)
    # Concise materialized summary for reporting; the artifacts remain canonical.
    steps: list[dict[str, Any]] = Field(default_factory=list)
    sinks: list[dict[str, Any]] = Field(default_factory=list)
    cross_repo_links: list[dict[str, Any]] = Field(default_factory=list)
    analysis_status: str = ANALYSIS_COMPLETE
    analysis_notes: list[str] = Field(default_factory=list)
    obligations: list[VerificationObligation] = Field(default_factory=list)
    reason: str | None = None
    status: str = VERIFICATION_UNVERIFIED


class VerificationGap(BaseModel):
    """Externally meaningful behavior with no located verification evidence."""

    id: str
    behavior: str
    why: str | None = None
    related_node_ids: list[str] = Field(default_factory=list)
    related_flow_ids: list[str] = Field(default_factory=list)
    existing_evidence_found: bool = False
    status: str = VERIFICATION_UNVERIFIED
    source: str = "llm"
    evidence: list[EvidenceRef] = Field(default_factory=list)


class RuntimeDependency(BaseModel):
    """Dependency that must be running/reachable to exercise affected behavior.

    Sydes does not provision, mock, or contact any of these.
    """

    id: str
    name: str
    kind: str
    repo: str | None = None
    required_for_flow_ids: list[str] = Field(default_factory=list)
    detected_from: list[EvidenceRef] = Field(default_factory=list)
    scope: str = "repository"


class CrossRepoImpact(BaseModel):
    """Relationship crossing a repository/service boundary."""

    id: str
    target_repo: str | None = None
    target_label: str
    kind: str = "http_call"
    status: str = LINK_UNRESOLVED
    reason: str | None = None
    related_flow_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ChangeVerificationResult(BaseModel):
    """Top-level `verify-change` artifact."""

    version: str = "v3"
    kind: str = "sydes_change_verification"
    generated_at: str | None = None
    change: ChangeSet
    summary: ChangeSummary = Field(default_factory=ChangeSummary)
    code_findings: list[CodeFinding] = Field(default_factory=list)
    affected_flows: list[AffectedFlow] = Field(default_factory=list)
    analysis_status: str = ANALYSIS_COMPLETE
    analysis_notes: list[str] = Field(default_factory=list)
    test_executions: list[TestExecution] = Field(default_factory=list)
    verification_gaps: list[VerificationGap] = Field(default_factory=list)
    runtime_dependencies: list[RuntimeDependency] = Field(default_factory=list)
    cross_repo_impacts: list[CrossRepoImpact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
