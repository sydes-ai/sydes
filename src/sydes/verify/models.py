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

# Fixed, small, forward-looking vocabulary for `ChangeSemanticAnalysis`'
# boundary-type hints. These are hints for *later* boundary discovery only —
# this task never performs boundary discovery from them.
BOUNDARY_TYPE_API = "api"
BOUNDARY_TYPE_CALLABLE = "callable"
BOUNDARY_TYPE_ASYNC = "async"
BOUNDARY_TYPE_EXTERNAL = "external"
BOUNDARY_TYPE_UNKNOWN = "unknown"
SEMANTIC_BOUNDARY_TYPES = frozenset({
    BOUNDARY_TYPE_API, BOUNDARY_TYPE_CALLABLE, BOUNDARY_TYPE_ASYNC,
    BOUNDARY_TYPE_EXTERNAL, BOUNDARY_TYPE_UNKNOWN,
})

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
    supporting_tests: int = 0
    tests_executed: int = 0
    verification_gaps: int = 0
    runtime_dependencies: int = 0
    cross_repo_impacts: int = 0
    #: Derived from the same canonical `accepted_impacts` list the report
    #: renders — never a separately-counted view of PROVEN/INFERRED.
    impacts_proven: int = 0
    impacts_inferred: int = 0
    #: Accepted impacts whose `verification_model_status != "modeled"` — no
    #: `AffectedFlow`/obligations exist for them, so nothing about them has
    #: actually been checked. VERIFIED is impossible while this is nonzero;
    #: see `_compute_summary`.
    impacts_not_modeled: int = 0
    #: Changed symbols the deterministic impact interpreter never reached any
    #: entrypoint from (an `ImpactCandidate` may still have been proposed for
    #: one — that does not remove it from this count). Mirrors
    #: `ChangeVerificationResult.unresolved_changed_symbols`; VERIFIED is
    #: impossible while this is nonzero.
    unresolved_changed_symbols: int = 0


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


class CiSuiteRun(BaseModel):
    """One execution of the repository's own test command.

    This is the regression baseline: it says whether the repository is healthy
    after the change, which is not the same as saying every changed behavior is
    demonstrated. Obligation status is decided separately.
    """

    command: list[str] = Field(default_factory=list)
    source: str = ""
    working_dir: str = "."
    framework: str = ""
    status: str = VERIFICATION_UNKNOWN
    blocker: str | None = None
    reason: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    summary_line: str | None = None
    failed_test_ids: list[str] = Field(default_factory=list)
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None
    output_truncated: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list)

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


class AcceptedImpact(BaseModel):
    """One canonical merged impact — the single source of truth every
    downstream layer (obligations, the human-readable report, structured
    output for evaluation) reads from. Never a second, competing view of
    "what Sydes found": built once from `ImpactResult.affected` (which
    already merges PROVEN deterministic entries and INFERRED LLM candidates,
    PROVEN winning on any duplicate), so nothing here is re-derived.

    `id` matches the corresponding `AffectedFlow.id`/`VerificationObligation
    .flow_id` when this impact was modeled into one (`verification_model_
    status == "modeled"`) — that shared id is the provenance link from an
    obligation back to the impact that produced it, without a duplicate
    field to keep in sync.
    """

    id: str
    label: str
    repo: str | None = None
    kind: str = "unknown"
    #: `IMPACT_STATUS_PROVEN` or `IMPACT_STATUS_INFERRED` — see `sydes.impact.models`.
    status: str = "proven"
    changed_symbols: list[str] = Field(default_factory=list)
    route_method: str | None = None
    route_path: str | None = None
    #: The fields below are populated only for `status == "inferred"`. Model
    #: self-assessment, not a calibrated probability — presented as "LLM
    #: confidence" everywhere it is shown, never as verification confidence.
    llm_confidence: float | None = None
    llm_reason: str | None = None
    llm_inference_type: str | None = None
    llm_uncertainty: str | None = None
    #: Whether cheap corroboration matched this candidate against an
    #: already-known fact. `False` does not remove the impact from this
    #: list — an uncorroborated inference is still shown, never dropped.
    corroborated: bool | None = None
    #: The reviewer-facing behavior description an inferred impact carried,
    #: preserved explicitly and separately from `label`. `label` above is the
    #: *resolved display value* (route, else this, else the anchor symbol);
    #: this field is the semantic label alone, so a consumer can always tell
    #: whether the displayed text describes a behavior or is falling back to
    #: an identifier. Empty for every deterministic (PROVEN) impact.
    #: The grounding anchor itself remains recoverable from `id`
    #: (`impact:{repo}:{qualified_name or symbol}`) — no separate anchor
    #: field is introduced here.
    behavior_label: str = ""
    #: "modeled": this impact reached a full `AffectedFlow` with obligations
    #: (an HTTP route that could be reconciled). "unsupported_or_partial":
    #: accepted by the impact layer but not yet representable as a full
    #: verification flow — a generic backend behavior with no HTTP shape,
    #: or an inferred route that never matched a real one. Recorded rather
    #: than silently dropped either way.
    verification_model_status: str = "modeled"


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
    #: "proven" (default) for a flow the deterministic impact layer reached
    #: on its own; "inferred" when it was proposed by the M4 semantic
    #: inference guide and corroborated against a real, already-known route.
    #: Provenance only — never read by verdict aggregation, which decides
    #: VERIFIED/INCOMPLETE/etc. purely from `obligations`/test evidence
    #: regardless of how a flow was found. An inferred flow's obligations
    #: start `VERIFICATION_UNVERIFIED` exactly like any other and can only
    #: change status through the same real evidence any obligation needs.
    impact_status: str = "proven"


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


class SemanticBehaviorChange(BaseModel):
    """One behavior the PR-level semantic pass believes changed.

    A hypothesis, not a proven impact — see `ChangeSemanticAnalysis`."""

    description: str
    changed_symbols: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    #: The model's own self-assessment, same convention as
    #: `AcceptedImpact.llm_confidence` — never a verification confidence.
    confidence: float | None = None


class SemanticKeySymbol(BaseModel):
    """One changed symbol/file the semantic pass judges most worth
    attention, and why — not a claim that it was structurally reached."""

    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    reason: str


class SemanticInvestigationHint(BaseModel):
    """A pointer for structural analysis to look at — never itself a
    discovered boundary. `likely_boundary_types` values are restricted to
    `SEMANTIC_BOUNDARY_TYPES`."""

    description: str
    related_symbols: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    likely_boundary_types: list[str] = Field(default_factory=list)


class ChangeSemanticAnalysis(BaseModel):
    """One bounded, PR-level LLM read of the change as a whole — the
    semantic perspective, complementary to (never a replacement for) Sydes'
    structural/CBM analysis.

    Everything here is `origin=ORIGIN_LLM_HYPOTHESIS`: it can never create a
    PROVEN/INFERRED impact, an `AffectedFlow`, a `VerificationObligation`, or
    move a verdict toward VERIFIED — see `sydes.verify.pr_semantic_analysis`
    for the boundary this is deliberately kept on the far side of. It exists
    to give a reviewer (and later, structural reconciliation) a starting
    read of the change and where to look, never a claim about what was
    actually found in the running system.
    """

    origin: str = ORIGIN_LLM_HYPOTHESIS
    change_summary: str = ""
    behavior_changes: list[SemanticBehaviorChange] = Field(default_factory=list)
    important_symbols: list[SemanticKeySymbol] = Field(default_factory=list)
    investigation_hints: list[SemanticInvestigationHint] = Field(default_factory=list)
    likely_boundary_types: list[str] = Field(default_factory=list)
    local_risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class AffectedBoundary(BaseModel):
    """One typed, transport-neutral software boundary reachable from the
    change — Increment C. Complementary to `AcceptedImpact`/`AffectedFlow`,
    never a replacement: an HTTP boundary here and an HTTP `AffectedFlow`
    may describe the same real route from two different angles, and this
    model deliberately carries no `route_method`/`route_path` of its own —
    those stay HTTP-only fields on `AcceptedImpact`/`AffectedFlow`.

    One representation, two epistemic states, distinguished by `status`:

    - `"proven"` (Increment C, the default): reached by a real,
      deterministically-walked structural edge — never a signature/type-only
      reference, never a semantic hint alone. Produced by
      `sydes.impact.boundary_discovery` and reshaped here by
      `verify.analyzer`. This is structural proof.
    - `"inferred"` (Increment D): proposed by one bounded LLM reasoning pass
      over a compact packet of real evidence, for cases current extraction
      cannot fully ground. Produced by `verify.boundary_reasoning`. This is
      a hypothesis backed by supplied evidence — NOT proof, and deliberately
      invisible to verdict math: nothing here creates an `AffectedFlow`, a
      `VerificationObligation`, or an `AcceptedImpact`, and
      `affected_boundaries` is not an input to `_compute_summary`.

    An inferred boundary is never emitted for a boundary a deterministic one
    already covers — see `boundary_reasoning` for that precedence rule.
    """

    id: str
    kind: str  # api | callable | async | external | unknown
    subtype: str | None = None
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    label: str = ""
    changed_symbols: list[str] = Field(default_factory=list)
    #: A one-line, human-readable rendering of the path that reached this
    #: boundary (e.g. `"calls:helper -> usage:reserve"`) — never the raw
    #: `ImpactPath` object, to keep this model small and JSON-simple. For an
    #: inferred boundary, the specific supplied facts the model cited.
    evidence: list[str] = Field(default_factory=list)
    distance: int = 0
    #: `EDGE_STRENGTH_STRONG`/`EDGE_STRENGTH_MEDIUM` — see
    #: `sydes.impact.models`. Never `weak`: a boundary reached only through
    #: an import/signature-only reference is never emitted at all.
    evidence_strength: str = "medium"
    #: `IMPACT_STATUS_PROVEN` | `IMPACT_STATUS_INFERRED` — see the class
    #: docstring. The single field separating structural proof from
    #: evidence-backed inference.
    status: str = "proven"
    #: Populated only when `status == "inferred"`: why the model believes
    #: this boundary is affected, what it could not establish, and its own
    #: bounded self-assessment. Never a calibrated probability, and never
    #: read by anything that decides a verdict.
    reason: str | None = None
    uncertainty: str | None = None
    llm_confidence: float | None = None


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
    #: The one canonical merged impact set (PROVEN + INFERRED) — see
    #: `AcceptedImpact`. Always populated, regardless of backend: `affected_flows`
    #: below remains the detailed verification view for impacts that reached
    #: it, but this list is the complete "what did Sydes find" answer, so no
    #: accepted impact can be visible in one place and missing from another.
    accepted_impacts: list[AcceptedImpact] = Field(default_factory=list)
    #: One bounded, PR-level semantic read of the change — always
    #: `origin=ORIGIN_LLM_HYPOTHESIS`, `None` when the pass did not run or
    #: could not produce a usable result (see `analysis_notes`/`diagnostics`
    #: for why). Never contributes to `accepted_impacts`, `affected_flows`,
    #: obligations, or `summary.verdict` — see `ChangeSemanticAnalysis`.
    pr_semantic_analysis: ChangeSemanticAnalysis | None = None
    #: Typed, transport-neutral boundaries (api/callable/async) the ranked
    #: frontier walk found reachable from the change — Increment C. Always
    #: `status="proven"` (see `AffectedBoundary`), never HTTP-gated: a
    #: callable or async boundary needs no `AffectedFlow` to be visible here.
    #: Empty (cbm backend with nothing found, or native backend, which does
    #: not run boundary discovery at all) is a normal, non-error result.
    affected_boundaries: list[AffectedBoundary] = Field(default_factory=list)
    affected_flows: list[AffectedFlow] = Field(default_factory=list)
    #: Changed symbols the deterministic impact interpreter never reached any
    #: entrypoint from (`ImpactResult.unresolved`, cbm backend only) — set by
    #: the pipeline before `_compute_summary` runs. An `ImpactCandidate` may
    #: still have been proposed and accepted for one of these symbols; that
    #: is a separate, weaker claim (INFERRED, not PROVEN) and does not reduce
    #: this count. Always 0 for the native backend, which has no concept of
    #: an unresolved symbol distinct from "no route found."
    unresolved_changed_symbols: int = 0
    analysis_status: str = ANALYSIS_COMPLETE
    analysis_notes: list[str] = Field(default_factory=list)
    test_executions: list[TestExecution] = Field(default_factory=list)
    ci_suite: CiSuiteRun | None = None
    verification_gaps: list[VerificationGap] = Field(default_factory=list)
    runtime_dependencies: list[RuntimeDependency] = Field(default_factory=list)
    cross_repo_impacts: list[CrossRepoImpact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
